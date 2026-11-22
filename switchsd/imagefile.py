"""switchsd's compact card image: one file that holds only the parts of a card that are in use.

    16 bytes   "SWITCHSD IMAGE 2"
    data       the regions back to back, in the order listed
    JSON       {"bytes": the card's size, "regions": [[offset, length], ...], ...}
     8 bytes   length of that JSON, little-endian
    32 bytes   SHA-256 of everything before them

Whatever lies outside the regions was unused or blank on the card and reads back as zeros.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import struct
from pathlib import Path
from typing import BinaryIO

from .blockdev import SECTOR, BlockDev
from .common import CHUNK, Failure, Mismatch, Progress

MAGIC = b"SWITCHSD IMAGE 2"
REPLACED = b"SWITCHSD IMAGE 1"  # the layout of an earlier build, which no release ever wrote
TRAILER = 8 + 32
MAX_INDEX = 256 << 20
BLANK = 1 << 20  # a stretch of zeros this long inside a region is left out


def merge(regions: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    """Sort regions and join those that touch, overlap or lie no more than `gap` bytes apart."""
    out: list[tuple[int, int]] = []
    for offset, length in sorted(r for r in regions if r[1] > 0):
        if out and offset <= out[-1][0] + out[-1][1] + gap:
            start, old = out[-1]
            out[-1] = (start, max(old, offset + length - start))
        else:
            out.append((offset, length))
    return out


def is_compact(path: Path) -> bool:
    with open(path, "rb") as f:
        head = f.read(len(MAGIC))
    if head == REPLACED:
        raise Failure(f"{path} was made by an earlier build of this tool, whose image layout was replaced; make "
                      "the image again")
    return head == MAGIC


def write_image(src: BlockDev, size: int, regions: list[tuple[int, int]], dst: BinaryIO, about: dict,
                label: str) -> tuple[str, int]:
    """Copy `regions` of `src` into the new file `dst`, leaving out what is blank in them.

    Returns the SHA-256 of the whole file and the number of bytes kept.
    """
    inner, outer = hashlib.sha256(), hashlib.sha256()
    progress = Progress(label, max(sum(length for _, length in regions), 1))
    kept: list[tuple[int, int]] = []
    zeros = bytes(BLANK)

    def put(data: bytes) -> None:
        inner.update(data)
        outer.update(data)
        dst.write(data)

    put(MAGIC)
    for offset, length in regions:
        for done in range(0, length, CHUNK):
            buf = src.read(offset + done, min(CHUNK, length - done))
            for at in range(0, len(buf), BLANK):
                piece = buf[at:at + BLANK]
                if piece == zeros[:len(piece)]:
                    continue
                put(piece)
                if kept and sum(kept[-1]) == offset + done + at:
                    kept[-1] = (kept[-1][0], kept[-1][1] + len(piece))
                else:
                    kept.append((offset + done + at, len(piece)))
            progress.add(len(buf))
    index = json.dumps({"format": "switchsd-image", "version": 2, "bytes": size, **about,
                        "regions": [list(region) for region in kept]}, separators=(",", ":")).encode()
    put(index + struct.pack("<Q", len(index)))
    tail = inner.digest()
    outer.update(tail)
    dst.write(tail)
    dst.flush()
    os.fsync(dst.fileno())
    return outer.hexdigest(), sum(length for _, length in kept)


class CompactImage(BlockDev):
    """A compact image, read as if it were the card it was made from."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        try:
            self._load()
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self) -> CompactImage:
        return self

    def _file(self, offset: int, length: int) -> bytes:
        buf = bytearray()
        while len(buf) < length:
            try:
                chunk = self._pread(min(CHUNK, length - len(buf)), offset + len(buf))
            except OSError as e:
                raise Failure(f"reading {self.path} failed at byte {offset + len(buf)}: {e.strerror}") from None
            if not chunk:
                raise Failure(f"{self.path} ends at byte {offset + len(buf)}; the image is incomplete")
            buf += chunk
        return bytes(buf)

    def _load(self) -> None:
        incomplete = Failure(f"{self.path} is incomplete or damaged: its index cannot be read")
        actual = os.fstat(self.fd).st_size
        if actual < len(MAGIC) + TRAILER or self._file(0, len(MAGIC)) != MAGIC:
            raise Failure(f"{self.path} is not a complete switchsd image")
        (length,) = struct.unpack("<Q", self._file(actual - TRAILER, 8))
        self._end = actual - 32  # where the checksum starts
        index_at = actual - TRAILER - length
        if length > MAX_INDEX or index_at < len(MAGIC):
            raise incomplete
        try:
            meta = json.loads(self._file(index_at, length))
            if meta.get("version") != 2:
                raise Failure(f"{self.path} was made by a newer switchsd (image version {meta.get('version')!r})")
            self.size = int(meta["bytes"])
            self.regions = [(int(offset), int(count)) for offset, count in meta["regions"]]
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise incomplete from None
        position, end = len(MAGIC), 0
        self._at: list[int] = []
        for offset, count in self.regions:
            if offset < end or count <= 0 or offset % SECTOR or count % SECTOR:
                raise incomplete
            self._at.append(position)
            position, end = position + count, offset + count
        if self.size <= 0 or self.size % SECTOR or end > self.size or position != index_at:
            raise incomplete
        self.meta = meta
        self.stored = position - len(MAGIC)
        self._starts = [offset for offset, _ in self.regions]

    def read(self, offset: int, length: int) -> bytes:
        end = offset + length
        if offset < 0 or end > self.size:
            raise Failure(f"{self.path}: bytes {offset}-{end} lie beyond the card it holds")
        i = max(bisect.bisect_right(self._starts, offset) - 1, 0)
        if i < len(self.regions) and self.regions[i][0] <= offset and end <= sum(self.regions[i]):
            return self._file(self._at[i] + offset - self.regions[i][0], length)
        out = bytearray(length)
        while i < len(self.regions) and self.regions[i][0] < end:
            start, count = self.regions[i]
            lo, hi = max(start, offset), min(start + count, end)
            if lo < hi:
                out[lo - offset:hi - offset] = self._file(self._at[i] + lo - start, hi - lo)
            i += 1
        return bytes(out)

    def write(self, offset: int, data: bytes) -> None:
        raise Failure(f"{self.path} is an image and cannot be written to")

    def verify(self, label: str = "checking the image") -> None:
        """Compare the file with the checksum stored at its end."""
        digest, progress = hashlib.sha256(), Progress(label, max(self._end, 1))
        for done in range(0, self._end, CHUNK):
            buf = self._file(done, min(CHUNK, self._end - done))
            digest.update(buf)
            progress.add(len(buf))
        if digest.digest() != self._file(self._end, 32):
            raise Mismatch(f"{self.path} is damaged: its contents do not match the checksum stored in it")
