"""What differs per operating system: finding a disk, getting it to ourselves, and making the OS re-read it.

Raw I/O itself is the same everywhere (blockdev.py). Each platform supplies the device path, whether a
disk is safe to write, how to unmount or lock its volumes, and how to make the OS pick up a new
partition table.
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .common import Failure, linux_mounts, run


@dataclass
class Volume:
    offset: int  # bytes from the start of the disk
    size: int
    mount: str | None


@dataclass
class Disk:
    id: str  # what the user typed, normalized: disk5, /dev/sdb, 2
    raw: str  # path for raw I/O
    size: int
    name: str
    removable: bool
    system: bool
    virtual: bool  # disk image, loop device or VHD: fine to write, used by the tests
    volumes: list[Volume] = field(default_factory=list)
    readonly: bool = False  # a locked card, or a read-only disk image


def get_disk(ident: str) -> Disk:
    if sys.platform == "darwin":
        return _mac_disk(ident)
    if sys.platform.startswith("linux"):
        return _linux_disk(ident)
    if sys.platform == "win32":
        return _win_disk(ident)
    raise Failure(f"raw disk access is not supported on {sys.platform}")


def list_disks() -> list[Disk]:
    """Every disk the raw commands would accept: removable or virtual, and not the system's own."""
    if sys.platform == "darwin":
        names = _plist(["/usr/sbin/diskutil", "list", "-plist"]).get("WholeDisks", [])
    elif sys.platform.startswith("linux"):
        names = sorted(n for n in os.listdir(f"{SYS}/block") if not n.startswith(("ram", "zram", "dm-", "nbd", "sr")))
    elif sys.platform == "win32":
        numbers = _powershell("@(Get-Disk | ForEach-Object { $_.Number }) | ConvertTo-Json -Compress") or []
        names = [str(n) for n in ([numbers] if isinstance(numbers, int) else numbers)]
    else:
        raise Failure(f"raw disk access is not supported on {sys.platform}")
    found = []
    for name in names:
        try:
            disk = get_disk(name)
        except Failure:
            continue
        if disk.size and not disk.system and (disk.removable or disk.virtual) and not (disk.virtual and disk.readonly):
            found.append(disk)
    return found


def check_disk(ident: str, expect_bytes: int | None, writing: bool = False) -> Disk:
    disk = get_disk(ident)
    if writing and disk.readonly:
        raise Failure(f"{disk.id} is read-only; if it is an SD card in an adapter, slide the lock switch off")
    if disk.system:
        raise Failure(f"{disk.id} holds the running system; refusing to touch it")
    if not (disk.removable or disk.virtual):
        raise Failure(f"{disk.id} is not a removable disk; refusing to touch it")
    if expect_bytes is not None and disk.size != expect_bytes:
        raise Failure(f"{disk.id} holds {disk.size} bytes, not the {expect_bytes} given with --expect-bytes")
    return disk


def same_disk(disk: Disk) -> None:
    """Raise unless the name still leads to the disk that was checked: readers re-enumerate, cards get swapped."""
    try:
        now = get_disk(disk.id)
    except Failure as e:
        raise Failure(f"{disk.id} can no longer be found ({e}); nothing was written") from None
    if (now.size, now.name) != (disk.size, disk.name) or now.system or now.readonly \
            or not (now.removable or now.virtual):
        raise Failure(f"{disk.id} is no longer the disk that was checked: it is now {now.name or 'unknown media'} "
                      f"with {now.size} bytes. Nothing was written.")


@contextlib.contextmanager
def exclusive(disk: Disk) -> Iterator[None]:
    """Unmount or lock every volume on the disk for the duration of a raw write."""
    if sys.platform == "darwin":
        run(["/usr/sbin/diskutil", "unmountDisk", disk.id])
        yield
    elif sys.platform.startswith("linux"):
        for point in _linux_mount_points(os.path.basename(disk.id)):
            run(["umount", point])
        yield
    else:
        with _win_locked(disk):
            yield


def reread(disk: Disk) -> None:
    """Make the OS read the new partition table. macOS does it by itself once the raw device closes."""
    if sys.platform.startswith("linux"):
        import fcntl  # noqa: PLC0415 - Linux only
        fd = os.open(disk.raw, os.O_RDONLY)
        try:
            fcntl.ioctl(fd, 0x125F)  # BLKRRPART
        except OSError as e:
            raise Failure(f"the kernel did not re-read the partition table ({e.strerror}); unplug and replug the "
                          "card") from None
        finally:
            os.close(fd)
        with contextlib.suppress(Failure):
            run(["udevadm", "settle"])
    elif sys.platform == "win32":
        _win_update_properties(disk)


def wait_for(disk: Disk, expected: list[tuple[int, int]], timeout: float = 30) -> Disk:
    """Poll until the OS lists exactly the expected partitions (byte offset, size)."""
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(Failure):
            current = get_disk(disk.id)
            if sorted((v.offset, v.size) for v in current.volumes) == sorted(expected):
                return current
        if time.monotonic() > deadline:
            raise Failure("the system did not pick up the new partition table; unplug and replug the card, "
                          "it is complete")
        time.sleep(1)


# macOS: diskutil.

def _plist(cmd: list[str]) -> dict:
    return plistlib.loads(run(cmd).encode())


def _mac_disk(ident: str) -> Disk:
    match = re.fullmatch(r"(?:/dev/)?r?(disk\d+)", ident)
    if not match:
        raise Failure(f"{ident!r} is not a whole disk such as disk5 (see `diskutil list external`)")
    name = match.group(1)
    info = _plist(["/usr/sbin/diskutil", "info", "-plist", name])
    if not info.get("WholeDisk"):
        raise Failure(f"{name} is not a whole disk")
    root = _plist(["/usr/sbin/diskutil", "info", "-plist", "/"])
    # "/" sits in an APFS container, a disk of its own; the disk that physically holds it is its store.
    boot = {root.get("ParentWholeDisk")} | {re.sub(r"s\d+$", "", store.get("APFSPhysicalStore", ""))
                                            for store in root.get("APFSPhysicalStores") or []}
    listing = _plist(["/usr/sbin/diskutil", "list", "-plist", name]).get("AllDisksAndPartitions") or [{}]
    volumes = []
    for part in listing[0].get("Partitions", []):
        pinfo = _plist(["/usr/sbin/diskutil", "info", "-plist", part["DeviceIdentifier"]])
        volumes.append(Volume(pinfo.get("PartitionMapPartitionOffset", 0), pinfo.get("Size", 0),
                              pinfo.get("MountPoint") or None))
    return Disk(name, f"/dev/r{name}", info.get("Size", 0), info.get("MediaName") or "",
                bool(info.get("RemovableMedia") or info.get("Removable") or info.get("Ejectable")),
                name in boot, info.get("BusProtocol") == "Disk Image", volumes,
                info.get("WritableMedia") is False)


# Linux: sysfs for sizes, partitions and what is stacked on them, /proc for mounts and swap, BLKRRPART
# to re-read.

SYS, PROC = "/sys", "/proc"
# Where a disk is part of the running system, whether a partition is mounted there or a LUKS or LVM
# volume built on one.
SYSTEM_MOUNTS = frozenset({"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "/opt", "/srv", "/root",
                           "/tmp", "/nix", "/sysroot"})


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _device_key(path: str) -> str:
    """A block device's major:minor, which is the same under every name it has; else the path itself."""
    with contextlib.suppress(OSError):
        status = os.stat(path)
        if stat.S_ISBLK(status.st_mode):
            return f"{os.major(status.st_rdev)}:{os.minor(status.st_rdev)}"
    return os.path.realpath(path) if path.startswith("/") else path


def _linux_mounts() -> dict[str, list[str]]:
    """Every mount point of every mounted device, by device key."""
    mounts: dict[str, list[str]] = {}
    for dev, point, _ in linux_mounts(f"{PROC}/self/mounts"):
        mounts.setdefault(_device_key(dev), []).append(point)
    return mounts


def _linux_swaps() -> set[str]:
    return {_device_key(line.split()[0]) for line in _read(f"{PROC}/swaps").splitlines()[1:] if line.split()}


def _node_keys(node: str) -> list[str]:
    return [key for key in (_read(f"{SYS}/class/block/{node}/dev"), f"/dev/{node}") if key]


def _stack(node: str) -> list[str]:
    """A block device and everything built on it: LUKS and LVM volumes, and what is built on those."""
    found: list[str] = []
    todo = [node]
    while todo:
        current = todo.pop()
        if current not in found:
            found.append(current)
            with contextlib.suppress(OSError):
                todo += sorted(os.listdir(f"{SYS}/class/block/{current}/holders"))
    return found


def _linux_partitions(name: str) -> list[str]:
    sysdir = f"{SYS}/class/block/{name}"
    return [entry for entry in sorted(os.listdir(sysdir))
            if entry.startswith(name) and os.path.exists(f"{sysdir}/{entry}/partition")]


def _linux_mount_points(name: str) -> list[str]:
    """Where the disk itself and its partitions are mounted, the deepest first so that each one unmounts."""
    mounts = _linux_mounts()
    points = {point for node in [name, *_linux_partitions(name)] for key in _node_keys(node)
              for point in mounts.get(key, [])}
    return sorted(points, key=lambda point: (-point.count("/"), point))


def _linux_disk(ident: str) -> Disk:
    name = os.path.basename(ident)
    sysdir = f"{SYS}/class/block/{name}"
    if not re.fullmatch(r"[a-z0-9]+", name) or not os.path.isdir(sysdir):
        raise Failure(f"{ident!r} is not a disk such as /dev/sdb or /dev/mmcblk0 (see `lsblk`)")
    if os.path.exists(f"{sysdir}/partition"):
        raise Failure(f"{ident} is a partition; give the whole disk")
    mounts, swaps = _linux_mounts(), _linux_swaps()

    def points(node: str) -> list[str]:
        return [point for key in _node_keys(node) for point in mounts.get(key, [])]

    def runs_the_system(node: str) -> bool:
        return any(SYSTEM_MOUNTS.intersection(points(part)) or swaps.intersection(_node_keys(part))
                   for part in _stack(node))

    volumes, system = [], runs_the_system(name)
    for entry in _linux_partitions(name):
        system |= runs_the_system(entry)
        volumes.append(Volume(int(_read(f"{sysdir}/{entry}/start") or 0) * 512,
                              int(_read(f"{sysdir}/{entry}/size") or 0) * 512, next(iter(points(entry)), None)))
    device = os.path.realpath(f"{sysdir}/device")
    # An SD card in a built-in reader is not flagged removable; neither is the eMMC a laptop boots from.
    card = name.startswith("mmcblk") and _read(f"{sysdir}/device/type") == "SD"
    removable = _read(f"{sysdir}/removable") == "1" or "/usb" in device or card
    model = _read(f"{sysdir}/device/model") or _read(f"{sysdir}/device/name")
    return Disk(f"/dev/{name}", f"/dev/{name}", int(_read(f"{sysdir}/size") or 0) * 512, model, removable,
                system, name.startswith("loop"), volumes, _read(f"{sysdir}/ro") == "1")


# Windows: PowerShell's storage cmdlets to look, kernel32 to lock volumes and refresh the disk.

def _powershell_exe() -> str:
    """By full path: a bare name would also be looked for in the current folder, and this runs elevated."""
    return os.path.join(os.environ.get("SYSTEMROOT") or r"C:\Windows", "System32", "WindowsPowerShell", "v1.0",
                        "powershell.exe")


def _powershell(script: str) -> Any:
    out = run([_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command",
               "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script])
    return json.loads(out) if out.strip() else None


def _win_number(ident: str) -> int:
    match = re.fullmatch(r"(?:\\\\\.\\)?(?:PhysicalDrive)?(\d+)", ident, re.IGNORECASE)
    if not match:
        raise Failure(f"{ident!r} is not a disk number such as 2 (see Get-Disk in PowerShell)")
    return int(match.group(1))


def _win_disk(ident: str) -> Disk:
    number = _win_number(ident)
    info = _powershell(f"Get-Disk -Number {number} | Select-Object Number,FriendlyName,Size,IsBoot,IsSystem,IsReadOnly,"
                       "@{n='Bus';e={$_.BusType.ToString()}} | ConvertTo-Json -Compress")
    if not isinstance(info, dict):
        raise Failure(f"disk {number} not found")
    parts = _powershell(f"Get-Partition -DiskNumber {number} -ErrorAction SilentlyContinue | Select-Object "
                        "Offset,Size,DriveLetter,@{n='Paths';e={@($_.AccessPaths)}} | ConvertTo-Json -Compress")
    parts = [parts] if isinstance(parts, dict) else parts or []
    volumes = []
    for p in parts:
        letter = p.get("DriveLetter")
        mount = f"{letter}:\\" if isinstance(letter, str) and letter.strip("\0 ") else None
        volumes.append(Volume(int(p["Offset"]), int(p["Size"]), mount))
    bus = str(info.get("Bus", ""))
    return Disk(str(number), rf"\\.\PhysicalDrive{number}", int(info["Size"]), info.get("FriendlyName") or "",
                bus in ("USB", "SD", "MMC"), bool(info.get("IsBoot") or info.get("IsSystem")),
                bus == "File Backed Virtual", volumes, bool(info.get("IsReadOnly")))


def _kernel32():  # type: ignore[no-untyped-def]
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    wintypes.LPVOID]
    return ctypes, wintypes, k32


def _win_open(path: str):  # type: ignore[no-untyped-def]
    ctypes, wintypes, k32 = _kernel32()
    handle = k32.CreateFileW(path, 0xC0000000, 0x3, None, 3, 0, None)  # read+write, shared, OPEN_EXISTING
    if handle in (None, wintypes.HANDLE(-1).value):
        raise Failure(f"cannot open {path}: {ctypes.FormatError(ctypes.get_last_error())}")
    return handle


def _win_ioctl(handle: object, code: int, what: str) -> None:
    ctypes, wintypes, k32 = _kernel32()
    returned = wintypes.DWORD()
    if not k32.DeviceIoControl(handle, code, None, 0, None, 0, ctypes.byref(returned), None):
        raise Failure(f"{what} failed: {ctypes.FormatError(ctypes.get_last_error())}")


@contextlib.contextmanager
def _win_locked(disk: Disk) -> Iterator[None]:
    _, _, k32 = _kernel32()
    paths = _powershell(f"Get-Partition -DiskNumber {disk.id} -ErrorAction SilentlyContinue | "
                        "ForEach-Object { $_.AccessPaths } | Where-Object { $_ -like '\\\\?\\Volume*' } | "
                        "ConvertTo-Json -Compress")
    paths = [paths] if isinstance(paths, str) else paths or []
    handles = []
    try:
        for path in paths:
            handle = _win_open(path.rstrip("\\"))
            handles.append(handle)
            _win_ioctl(handle, 0x00090018, f"locking {path}")  # FSCTL_LOCK_VOLUME
            _win_ioctl(handle, 0x00090020, f"dismounting {path}")  # FSCTL_DISMOUNT_VOLUME
        yield
    finally:
        for handle in handles:
            k32.CloseHandle(handle)


def _win_update_properties(disk: Disk) -> None:
    _, _, k32 = _kernel32()
    handle = _win_open(disk.raw)
    try:
        _win_ioctl(handle, 0x00070140, "refreshing the partition table")  # IOCTL_DISK_UPDATE_PROPERTIES
    finally:
        k32.CloseHandle(handle)
    with contextlib.suppress(Failure):
        run([_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", f"Update-Disk -Number {disk.id}"])
