"""FAT32: a reader for existing volumes and a writer that builds a fresh volume in one pass."""

from __future__ import annotations

import hashlib
import secrets
import struct
import sys
import time
from array import array
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from .blockdev import SECTOR, BlockDev
from .common import CHUNK, Failure, Usage, gb

ATTR_VOLUME, ATTR_DIR, ATTR_ARCHIVE, ATTR_LFN = 0x08, 0x10, 0x20, 0x0F
EOC = 0x0FFFFFFF
MIN_CLUSTERS = 65525  # fewer clusters than this is FAT16 by definition
DATA_ALIGN = 8192  # sectors: start the data region on a 4 MiB boundary, which SD cards erase in
SHORT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")
BAD_LONG = set('"*/:<>?\\|')


@dataclass(frozen=True)
class Entry:
    path: str
    attr: int
    cluster: int
    size: int
    times: bytes  # creation, access and write stamps as stored: entry bytes 13-19 and 22-25

    @property
    def is_dir(self) -> bool:
        return bool(self.attr & ATTR_DIR)


def now_times() -> bytes:
    t = time.localtime()
    date = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    clock = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return struct.pack("<BHHHHH", 0, clock, date, date, clock, date)


def short_name(raw: bytes, case: int) -> str:
    base = (b"\xe5" + raw[1:8] if raw[0] == 0x05 else raw[:8]).decode("cp437").rstrip()
    ext = raw[8:11].decode("cp437").rstrip()
    if case & 0x08:
        base = base.lower()
    if case & 0x10:
        ext = ext.lower()
    return f"{base}.{ext}" if ext else base


def lfn_checksum(raw: bytes) -> int:
    total = 0
    for byte in raw[:11]:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


FAT_BLOCK = 128  # sectors of the FAT read at a time


class Fat32:
    """Read-only FAT32 walker."""

    def __init__(self, dev: BlockDev, offset: int) -> None:
        bs = dev.read(offset, SECTOR)
        bps, spc, reserved, fats, root_entries, total16, _media, fat16 = struct.unpack_from("<HBHBHHBH", bs, 11)
        total32, fat_sectors, flags, _version, root = struct.unpack_from("<IIHHI", bs, 32)
        if (bs[510:512] != b"\x55\xaa" or bps != SECTOR or not spc or spc & (spc - 1) or fat16
                or root_entries or not fat_sectors or not fats):
            raise Failure("no FAT32 file system with 512-byte sectors at the start of the partition")
        active = flags & 0x0F if flags & 0x80 else 0
        self.dev = dev
        self.cluster_bytes = spc * SECTOR
        self.fat_offset = offset + (reserved + active * fat_sectors) * SECTOR
        self.data_offset = offset + (reserved + fats * fat_sectors) * SECTOR
        self.clusters = ((total32 or total16) - reserved - fats * fat_sectors) // spc
        self.root = root
        self.label = bs[71:82].decode("ascii", "replace").strip()
        self._fat: dict[int, bytes] = {}

    def _next(self, cluster: int) -> int:
        number, index = divmod(cluster * 4, FAT_BLOCK * SECTOR)
        block = self._fat.get(number)
        if block is None:
            start = number * FAT_BLOCK * SECTOR
            length = min(FAT_BLOCK * SECTOR, -(-(self.clusters + 2) * 4 // SECTOR) * SECTOR - start)
            block = self._fat[number] = self.dev.read(self.fat_offset + start, length)
        return struct.unpack_from("<I", block, index)[0] & 0x0FFFFFFF

    def chain(self, first: int) -> list[int]:
        if first == 0:
            return []
        clusters, seen, cluster = [], set(), first
        while 2 <= cluster <= self.clusters + 1:
            if cluster in seen:
                raise Failure("the FAT has a loop; run First Aid or chkdsk on the card")
            seen.add(cluster)
            clusters.append(cluster)
            cluster = self._next(cluster)
        if cluster < 0x0FFFFFF8:
            raise Failure(f"broken FAT chain at cluster {cluster:#x}; run First Aid or chkdsk on the card")
        return clusters

    def covers(self, entry: Entry) -> None:
        """Raise unless the entry's cluster chain is sound and long enough for its size."""
        if len(self.chain(entry.cluster)) * self.cluster_bytes < (0 if entry.is_dir else entry.size):
            raise Failure("it is longer than its cluster chain; run First Aid or chkdsk on the card")

    def _cluster_offset(self, cluster: int) -> int:
        return self.data_offset + (cluster - 2) * self.cluster_bytes

    def read_chain(self, first: int, size: int | None = None) -> Iterator[bytes]:
        """The data of a cluster chain, in reads of up to CHUNK; `size` trims a file's tail."""
        clusters = self.chain(first)
        remaining = size
        i = 0
        while i < len(clusters) and remaining != 0:
            j = i + 1
            while (j < len(clusters) and clusters[j] == clusters[j - 1] + 1
                   and (j - i) * self.cluster_bytes < CHUNK):
                j += 1
            length = (j - i) * self.cluster_bytes
            if remaining is not None:
                length = min(length, remaining)
                remaining -= length
            yield self.dev.read(self._cluster_offset(clusters[i]), length)
            i = j
        if remaining:
            raise Failure("a file is longer than its cluster chain; run First Aid or chkdsk on the card")

    def read_file(self, entry: Entry) -> bytes:
        return b"".join(self.read_chain(entry.cluster, entry.size))

    def entries(self, cluster: int, parent: str) -> Iterator[Entry]:
        lfn: dict[int, tuple[bytes, int]] = {}
        for c in self.chain(cluster):
            base = self._cluster_offset(c)
            block = self.dev.read(base, self.cluster_bytes)
            for pos in range(0, len(block), 32):
                raw = block[pos:pos + 32]
                if raw[0] == 0x00:
                    return
                attr = raw[11]
                if raw[0] == 0xE5:
                    lfn = {}
                    continue
                if attr & 0x3F == ATTR_LFN:
                    if raw[0] & 0x40:
                        lfn = {}
                    lfn[raw[0] & 0x1F] = (raw[1:11] + raw[14:26] + raw[28:32], raw[13])
                    continue
                if attr & ATTR_VOLUME:
                    lfn = {}
                    if not parent:
                        self.label = raw[:11].decode("ascii", "replace").strip()
                    continue
                name = None
                if lfn and sorted(lfn) == list(range(1, len(lfn) + 1)) and \
                        all(check == lfn_checksum(raw) for _, check in lfn.values()):
                    name = b"".join(lfn[k][0] for k in sorted(lfn)).decode("utf-16-le", "replace").split("\x00")[0]
                lfn = {}
                name = name or short_name(raw[:11], raw[12])
                if name in (".", ".."):
                    continue
                if "/" in name or not name.strip():
                    raise Failure(f"invalid file name {name!r} in {parent or 'the root folder'}")
                hi, lo, size = struct.unpack_from("<H", raw, 20)[0], *struct.unpack_from("<HI", raw, 26)
                yield Entry(f"{parent}/{name}" if parent else name, attr, hi << 16 | lo, size,
                            raw[13:20] + raw[22:26])

    def walk(self, skip: Callable[[str], bool] = lambda path: False,
             skipped: list[str] | None = None) -> Iterator[Entry]:
        """Every entry, each directory before its contents; skipped directories are not entered."""
        stack, seen = [(self.root, "")], {self.root}
        while stack:
            cluster, parent = stack.pop()
            for entry in self.entries(cluster, parent):
                if skip(entry.path):
                    if skipped is not None:
                        skipped.append(entry.path)
                    continue
                yield entry
                if entry.is_dir:
                    if entry.cluster < 2 or entry.cluster in seen:
                        raise Failure(f"directory {entry.path} is damaged; run First Aid or chkdsk on the card")
                    seen.add(entry.cluster)
                    stack.append((entry.cluster, entry.path))

    def used_runs(self) -> list[tuple[int, int]]:
        """Byte ranges (offset, length) of the data clusters in use, neighbours merged."""
        runs: list[tuple[int, int]] = []
        start: int | None = None

        def close(cluster: int) -> None:
            nonlocal start
            if start is not None:
                runs.append((self._cluster_offset(start), (cluster - start) * self.cluster_bytes))
                start = None

        total, step = self.clusters + 2, CHUNK // 4
        for base in range(0, total, step):
            data = self.dev.read(self.fat_offset + base * 4, min(step, total - base) * 4)
            if data.count(0) == len(data):  # a stretch of free clusters
                close(max(base, 2))
                continue
            table = array("I")
            table.frombytes(data)
            if sys.byteorder != "little":
                table.byteswap()
            for cluster, entry in enumerate(table, base):
                if cluster < 2:
                    continue
                if entry & 0x0FFFFFFF in (0, 0x0FFFFFF7):  # free, or marked bad
                    close(cluster)
                elif start is None:
                    start = cluster
        close(total)
        return runs

    def volume_label(self) -> str:
        """The root folder's label entry if there is one, else the boot sector's."""
        for _ in self.entries(self.root, ""):
            pass
        return self.label


@dataclass
class Node:
    """A file or folder for the writer. `data` is called once and yields the file's bytes."""

    name: str
    is_dir: bool
    attr: int
    times: bytes
    size: int = 0
    data: Callable[[], Iterable[bytes]] | None = None
    children: list[Node] = field(default_factory=list)
    first: int = 0
    clusters: int = 0
    sha256: str = ""
    names: list[tuple[bytes, list[bytes]]] = field(default_factory=list)


def is_83(name: str) -> bool:
    base, dot, ext = name.partition(".")
    return (1 <= len(base) <= 8 and len(ext) <= 3 and "." not in ext and (not dot or bool(ext))
            and all(c in SHORT_CHARS for c in base + ext))


def lfn_entries(name: str, short: bytes) -> list[bytes]:
    raw = name.encode("utf-16-le")
    units = [raw[i:i + 2] for i in range(0, len(raw), 2)]
    if len(units) % 13:
        units.append(b"\0\0")
    while len(units) % 13:
        units.append(b"\xff\xff")
    pieces = [b"".join(units[i:i + 13]) for i in range(0, len(units), 13)]
    check, out = lfn_checksum(short), []
    for seq, piece in enumerate(pieces, 1):
        e = bytearray(32)
        e[0] = seq | (0x40 if seq == len(pieces) else 0)
        e[1:11], e[11], e[13] = piece[0:10], ATTR_LFN, check
        e[14:26], e[28:32] = piece[10:22], piece[22:26]
        out.append(bytes(e))
    return out[::-1]


def make_names(name: str, used: set[bytes]) -> tuple[bytes, list[bytes]]:
    """The 8.3 name for `name` and the long-name entries that go before it (none if 8.3 is enough)."""
    if (not name or name in (".", "..") or len(name) > 255 or name.endswith((" ", "."))
            or any(c in BAD_LONG or ord(c) < 32 for c in name)):
        raise Failure(f"{name!r} cannot be a FAT file name")
    # Names that fit 8.3 once upper-cased keep that exact short name (PAYLOAD.BIN, not PAYLOA~1.BIN), so
    # loaders that only read 8.3 names, as small modchip loaders may, still find them.
    upper = name.upper()
    if is_83(upper):
        base, _, ext = upper.partition(".")
        short = base.ljust(8).encode() + ext.ljust(3).encode()
        if short not in used:
            used.add(short)
            return short, [] if name == upper else lfn_entries(name, short)
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem.strip("."):
        stem, ext = name, ""

    def clean(text: str) -> str:
        return "".join(c if c in SHORT_CHARS else "_" for c in text.upper() if c not in " .")

    base, ext3 = clean(stem) or "_", clean(ext)[:3]
    for n in range(1, 1_000_000):
        tail = f"~{n}"
        short = (base[:8 - len(tail)] + tail).ljust(8).encode() + ext3.ljust(3).encode()
        if short not in used:
            used.add(short)
            return short, lfn_entries(name, short)
    raise Failure(f"no free short name for {name!r}")


def dirent(short: bytes, attr: int, cluster: int, size: int, times: bytes) -> bytes:
    e = bytearray(32)
    e[0:11], e[11] = short, attr
    e[13:20], e[22:26] = times[:7], times[7:11]
    struct.pack_into("<H", e, 20, cluster >> 16)
    struct.pack_into("<HI", e, 26, cluster & 0xFFFF, size)
    return bytes(e)


def geometry(start: int, sectors: int) -> tuple[int, int, int, int]:
    """Sectors per cluster, reserved sectors, sectors per FAT and cluster count: 64 KiB clusters, like hekate."""
    for spc in (128, 64, 32, 16, 8, 4, 2, 1):
        fat = -(-(sectors - 32) // ((256 * spc + 2) // 2))
        reserved = 32 + (-(start + 32 + 2 * fat)) % DATA_ALIGN
        clusters = (sectors - reserved - 2 * fat) // spc
        if MIN_CLUSTERS + 16 <= clusters < 0x0FFFFFF5 and (clusters + 2) * 4 <= fat * SECTOR:
            return spc, reserved, fat, clusters
    raise Failure("the partition is too small for FAT32")


def label_bytes(label: str) -> bytes:
    text = label.upper()
    if not 1 <= len(text) <= 11 or any(c not in SHORT_CHARS and c != " " for c in text):
        raise Usage(f"{label!r} is not a valid FAT volume label (up to 11 letters, digits, spaces)")
    return text.ljust(11).encode()


def allocate(root: Node, cbytes: int, clusters: int) -> tuple[list[tuple[Node, int]], int]:
    """Give every folder and file its clusters, in the order they are written.

    Returns the folders, each with its parent's first cluster (0 for the root's children), and the
    number of clusters used. Raises when the tree needs more than `clusters`.
    """
    next_free = 2

    def alloc(count: int) -> int:
        nonlocal next_free
        first = next_free if count else 0
        next_free += count
        return first

    dirs: list[tuple[Node, int]] = []

    def plan(node: Node, parent_first: int, is_root: bool) -> None:
        used: set[bytes] = set()
        seen: set[str] = set()
        node.names = []
        for child in node.children:
            if child.name.lower() in seen:
                raise Failure(f"two entries named {child.name!r} differ only in case; FAT cannot hold both")
            seen.add(child.name.lower())
            node.names.append(make_names(child.name, used))
        count = (1 if is_root else 2) + sum(len(lfn) + 1 for _, lfn in node.names) + 1
        node.clusters = -(-count * 32 // cbytes)
        node.first = alloc(node.clusters)
        dirs.append((node, parent_first))
        for child in node.children:
            if child.is_dir:
                plan(child, 0 if is_root else node.first, False)
            else:
                child.clusters = -(-child.size // cbytes)
                child.first = alloc(child.clusters)

    plan(root, 0, True)
    used = next_free - 2
    if used > clusters:
        raise Failure(f"the files need {gb(used * cbytes)}, but the FAT32 partition holds {gb(clusters * cbytes)}")
    return dirs, used


def check_fits(start: int, sectors: int, root: Node) -> None:
    """Raise unless `root`'s tree fits in a volume written to the partition [start, start + sectors)."""
    spc, _reserved, _fat_sectors, clusters = geometry(start, sectors)
    allocate(root, spc * SECTOR, clusters)


def write_fat32(dev: BlockDev, start: int, sectors: int, label: str, root: Node,
                progress: Callable[[int], None] = lambda n: None) -> tuple[int, int]:
    """Build a FAT32 volume holding `root`'s tree in the partition [start, start + sectors).

    File data goes first and the boot sector last, so an interrupted run never looks like a valid
    volume. Every node gets its `sha256`; returns (files, used clusters).
    """
    spc, reserved, fat_sectors, clusters = geometry(start, sectors)
    cbytes = spc * SECTOR
    base = start * SECTOR
    data_offset = base + (reserved + 2 * fat_sectors) * SECTOR
    vol = label_bytes(label)
    dirs, used = allocate(root, cbytes, clusters)
    if root.first != 2:
        raise Failure("internal error: the root folder must start at cluster 2")
    fat = array("I", bytes(4 * (clusters + 2)))
    fat[0], fat[1] = 0x0FFFFFF8, EOC
    for folder, _ in dirs:
        for node in (folder, *(child for child in folder.children if not child.is_dir)):
            if node.clusters:
                last = node.first + node.clusters - 1
                fat[node.first:last] = array("I", range(node.first + 1, last + 1))
                fat[last] = EOC

    dev.zero(base, reserved * SECTOR)
    files = 0
    for node, _ in dirs:
        for child in node.children:
            if child.is_dir:
                continue
            digest, written = hashlib.sha256(), 0
            offset = data_offset + (child.first - 2) * cbytes
            pending = b""
            for chunk in (child.data() if child.data else ()):
                digest.update(chunk)
                pending += chunk
                usable = len(pending) - len(pending) % SECTOR
                if usable >= CHUNK:
                    dev.write(offset + written, pending[:usable])
                    written += usable
                    pending = pending[usable:]
            if pending:
                tail = pending + bytes(-len(pending) % SECTOR)
                dev.write(offset + written, tail)
            total = written + len(pending)
            if total != child.size:
                raise Failure(f"{child.name}: expected {child.size} bytes, the source gave {total}")
            child.sha256 = digest.hexdigest()
            files += 1
            progress(child.size)

    for node, parent_first in dirs:
        is_root = node is root
        records = [dirent(vol, ATTR_VOLUME, 0, 0, node.times)] if is_root else [
            dirent(b".          ", ATTR_DIR, node.first, 0, node.times),
            dirent(b"..         ", ATTR_DIR, parent_first, 0, node.times)]
        for child, (short, lfn) in zip(node.children, node.names):
            records += lfn
            records.append(dirent(short, child.attr, child.first, 0 if child.is_dir else child.size, child.times))
        blob = b"".join(records)
        dev.write(data_offset + (node.first - 2) * cbytes, blob + bytes(node.clusters * cbytes - len(blob)))

    table = fat.tobytes() if struct.pack("=I", 1) == struct.pack("<I", 1) else _swap(fat)
    table += bytes(fat_sectors * SECTOR - len(table))
    for copy in range(2):
        dev.write(base + (reserved + copy * fat_sectors) * SECTOR, table)

    fsinfo = bytearray(SECTOR)
    struct.pack_into("<I", fsinfo, 0, 0x41615252)
    struct.pack_into("<IIII", fsinfo, 484, 0x61417272, clusters - used, used + 2, 0)
    struct.pack_into("<I", fsinfo, 508, 0xAA550000)
    boot = bytearray(SECTOR)
    boot[0:3], boot[3:11] = b"\xeb\x58\x90", b"MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", boot, 11, SECTOR, spc, reserved, 2, 0, 0, 0xF8, 0, 63, 255, start, sectors)
    struct.pack_into("<IHHIHH", boot, 36, fat_sectors, 0, 0, root.first, 1, 6)
    boot[64], boot[66] = 0x80, 0x29
    struct.pack_into("<I", boot, 67, secrets.randbits(32))
    boot[71:82], boot[82:90], boot[510:512] = vol, b"FAT32   ", b"\x55\xaa"
    dev.write(base + SECTOR, bytes(fsinfo))
    dev.write(base + 6 * SECTOR, bytes(boot))
    dev.write(base + 7 * SECTOR, bytes(fsinfo))
    dev.write(base, bytes(boot))
    dev.sync()
    return files, used


def _swap(fat: array) -> bytes:
    copy = array("I", fat)
    copy.byteswap()
    return copy.tobytes()
