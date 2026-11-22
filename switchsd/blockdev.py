"""Sector-aligned I/O on an image file or a raw disk: /dev/rdisk5, /dev/sdb or \\\\.\\PhysicalDrive2."""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import struct
import sys

from .common import CHUNK, Failure

SECTOR = 512


def open_error(path: str, err: OSError) -> str:
    if sys.platform == "win32" and err.errno in (errno.EACCES, errno.EPERM):
        return f"no access to {path}: run the terminal as administrator"
    if err.errno == errno.EPERM:
        return (f"the system refused raw access to {path}. On macOS, give your terminal app Full Disk Access "
                "(System Settings > Privacy & Security), restart it and run the command with sudo.")
    if err.errno == errno.EACCES:
        return f"no permission to open {path}: run this command with sudo"
    if err.errno == errno.EBUSY:
        return f"{path} is busy: unmount the card and try again"
    return f"cannot open {path}: {err.strerror}"


class BlockDev:
    def __init__(self, path: str, writable: bool = False) -> None:
        self.path = path
        flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
        if writable and sys.platform.startswith("linux"):
            with contextlib.suppress(OSError):
                if stat.S_ISBLK(os.stat(path).st_mode):
                    flags |= os.O_EXCL  # fails while a file system, LVM or LUKS still holds the device
        try:
            self.fd = os.open(path, flags)
        except OSError as e:
            raise Failure(open_error(path, e)) from None

    def __enter__(self) -> BlockDev:
        return self

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)

    def _pread(self, size: int, offset: int) -> bytes:
        if hasattr(os, "pread"):
            return os.pread(self.fd, size, offset)
        os.lseek(self.fd, offset, os.SEEK_SET)
        return os.read(self.fd, size)

    def _pwrite(self, data: memoryview, offset: int) -> int:
        if hasattr(os, "pwrite"):
            return os.pwrite(self.fd, data, offset)
        os.lseek(self.fd, offset, os.SEEK_SET)
        return os.write(self.fd, data)

    def read(self, offset: int, length: int) -> bytes:
        lo = offset - offset % SECTOR
        hi = -(-(offset + length) // SECTOR) * SECTOR
        buf = bytearray()
        while lo + len(buf) < hi:
            try:
                chunk = self._pread(min(CHUNK, hi - lo - len(buf)), lo + len(buf))
            except OSError as e:
                raise Failure(f"reading {self.path} failed at byte {lo + len(buf)}: {e.strerror}") from None
            if not chunk:
                raise Failure(f"{self.path} ends at byte {lo + len(buf)}")
            buf += chunk
        start = offset - lo
        return bytes(buf[start:start + length])

    def write(self, offset: int, data: bytes) -> None:
        if offset % SECTOR or len(data) % SECTOR:
            raise ValueError("writes must cover whole sectors")
        view = memoryview(data)
        while view:
            try:
                written = self._pwrite(view, offset)
            except OSError as e:
                raise Failure(f"writing {self.path} failed at byte {offset}: {e.strerror}") from None
            view, offset = view[written:], offset + written

    def zero(self, offset: int, length: int) -> None:
        block = bytes(min(CHUNK, length))
        for done in range(0, length, CHUNK):
            self.write(offset + done, block[:min(CHUNK, length - done)])

    def sync(self) -> None:
        os.fsync(self.fd)
        advise = getattr(os, "posix_fadvise", None)
        if advise:  # Linux: make the next read come from the device, not from the page cache
            with contextlib.suppress(OSError):
                advise(self.fd, 0, 0, getattr(os, "POSIX_FADV_DONTNEED", 4))

    def device_size(self) -> int | None:
        """The size in bytes of the device this descriptor has open, where the system can say."""
        try:
            if sys.platform == "darwin":
                import fcntl  # noqa: PLC0415 - not on Windows
                blocks = struct.unpack("<Q", fcntl.ioctl(self.fd, 0x40086419, bytes(8)))[0]  # DKIOCGETBLOCKCOUNT
                return blocks * struct.unpack("<I", fcntl.ioctl(self.fd, 0x40046418, bytes(4)))[0]  # ...BLOCKSIZE
            if sys.platform.startswith("linux"):
                return os.lseek(self.fd, 0, os.SEEK_END)
        except OSError:
            pass
        return None
