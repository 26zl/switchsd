"""Shared test helpers: synthetic cards and the per-OS way to attach an image as a disk."""

from __future__ import annotations

import os
import plistlib
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from switchsd.blockdev import BlockDev  # noqa: E402
from switchsd.fat32 import ATTR_ARCHIVE, ATTR_DIR, Node, now_times, write_fat32  # noqa: E402
from switchsd.mbr import Part, build_mbr  # noqa: E402

SCRIPT = str(ROOT / "switchsd.py")
MIB = 1 << 20


def run_tool(*args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", SCRIPT, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout, check=False)


def file_node(name: str, data: bytes, attr: int = ATTR_ARCHIVE) -> Node:
    return Node(name, False, attr, now_times(), len(data), lambda: [data])


def dir_node(name: str, *children: Node, attr: int = ATTR_DIR) -> Node:
    return Node(name, True, attr, now_times(), children=list(children))


def card_tree(big: bytes, emu_sector: int) -> Node:
    """A card as hekate's partition manager and a CFW pack leave it, in miniature."""
    ini = (b"[emummc]\r\nenabled=1\r\nsector=" + hex(emu_sector).encode() +
           b"\r\npath=emuMMC/RAW1\r\nid=0x31574152\r\nnintendo_path=emuMMC/RAW1/Nintendo\r\n")
    return dir_node(
        "",
        file_node("payload.bin", b"hekate payload"),
        dir_node("emuMMC", file_node("emummc.ini", ini),
                 dir_node("RAW1", file_node("raw_based", struct.pack("<I", emu_sector)),
                          dir_node("Nintendo", dir_node("Contents", dir_node("registered", dir_node(
                              "00000000", dir_node("fake.nca", file_node("00", b"nca" * 1000),
                                                   attr=ATTR_DIR | ATTR_ARCHIVE)))), dir_node("save")))),
        dir_node("atmosphere", dir_node("contents", dir_node("0100000000000352", dir_node(
            "flags", file_node("boot2.flag", b""))))),
        dir_node("switch", file_node("app.nro", big), file_node("._app.nro", b"appledouble")),
        dir_node("Folder With Spaces", file_node("fil æøå.txt", b"hei")),
        file_node(".DS_Store", b"finder"),
    )


def build_card(path: Path, size: int, fat: Part, emu: Part, tree: Node, emu_data: bytes,
               label: str = "SWITCH SD") -> None:
    with open(path, "wb") as f:
        f.truncate(size)
    with BlockDev(str(path), writable=True) as dev:
        write_fat32(dev, fat.start, fat.size, label, tree)
        dev.write(emu.start * 512, emu_data)
        dev.write(0, build_mbr(bytes(440), [fat, emu], 0xCAFE))


def rand(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)


class TestDisk:
    """An image file attached as a disk the tool can write raw. `available()` says whether this OS can."""

    def __init__(self, image: Path) -> None:
        self.image = image
        self.id = ""
        self._vhd: Path | None = None

    _available: bool | None = None

    @classmethod
    def available(cls) -> bool:
        """Asked once per run: on macOS the answer takes attaching a small image."""
        if cls._available is None:
            cls._available = cls._probe()
        return cls._available

    @staticmethod
    def _probe() -> bool:
        if sys.platform == "darwin":
            return shutil.which("diskutil") is not None and _mac_raw_ok()
        if sys.platform.startswith("linux"):
            return hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("losetup") is not None
        if sys.platform == "win32":
            return _win_admin()
        return False

    def attach(self) -> str:
        if sys.platform == "darwin":
            self.id = _mac_attach(self.image)
        elif sys.platform.startswith("linux"):
            self.id = subprocess.run(["losetup", "-f", "--show", "-P", str(self.image)], capture_output=True,
                                     text=True, check=True, timeout=60).stdout.strip()
        else:
            self._vhd = self.image.with_suffix(".vhd")
            size_mb = self.image.stat().st_size // MIB
            _diskpart(f'create vdisk file="{self._vhd}" maximum={size_mb} type=expandable\n'
                      f'select vdisk file="{self._vhd}"\nattach vdisk\n')
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-DiskImage -ImagePath '{self._vhd}' | Get-Disk).Number"],
                                 capture_output=True, text=True, check=True, timeout=120).stdout.strip()
            self.id = out
            data = self.image.read_bytes()
            if data.strip(b"\0"):
                with BlockDev(rf"\\.\PhysicalDrive{self.id}", writable=True) as dev:
                    dev.write(0, data)
        return self.id

    def raw(self) -> str:
        if sys.platform == "darwin":
            return self.id.replace("/dev/disk", "/dev/rdisk") if self.id.startswith("/dev/") else f"/dev/r{self.id}"
        if sys.platform == "win32":
            return rf"\\.\PhysicalDrive{self.id}"
        return self.id

    def detach(self) -> None:
        if not self.id:
            return
        if sys.platform == "darwin":
            subprocess.run(["/usr/sbin/diskutil", "eject", self.id], capture_output=True, check=False, timeout=120)
        elif sys.platform.startswith("linux"):
            subprocess.run(["umount", "-q", f"{self.id}p1"], capture_output=True, check=False, timeout=60)
            subprocess.run(["losetup", "-d", self.id], capture_output=True, check=False, timeout=60)
        else:
            _diskpart(f'select vdisk file="{self._vhd}"\ndetach vdisk\n')
        self.id = ""


def _mac_attach(image: Path) -> str:
    out = subprocess.run(["/usr/sbin/diskutil", "image", "attach", "--noMount", "--plist", str(image)],
                         capture_output=True, check=False, timeout=120)
    if out.returncode == 0:
        for e in plistlib.loads(out.stdout).get("system-entities", []):
            dev = (e.get("dev-entry") or "").removeprefix("/dev/")
            if re.fullmatch(r"disk\d+", dev):
                return dev
    out = subprocess.run(["/usr/bin/hdiutil", "attach", "-nomount", "-imagekey", "diskimage-class=CRawDiskImage",
                          str(image)], capture_output=True, check=True, text=True, timeout=120)
    return out.stdout.split()[0].removeprefix("/dev/")


def _mac_raw_ok() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp, "probe.img")
        with open(image, "wb") as f:
            f.truncate(8 * MIB)
        try:
            dev = _mac_attach(image)
        except (subprocess.SubprocessError, OSError):
            return False
        try:
            os.close(os.open(f"/dev/r{dev}", os.O_RDWR))
            return True
        except OSError:
            return False
        finally:
            subprocess.run(["/usr/sbin/diskutil", "eject", dev], capture_output=True, check=False, timeout=120)


def _win_admin() -> bool:
    try:
        import ctypes  # noqa: PLC0415 - Windows only
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def _diskpart(script: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(script)
    try:
        subprocess.run(["diskpart", "/s", f.name], capture_output=True, text=True, check=True, timeout=300)
    finally:
        os.unlink(f.name)


def card(tmp: Path, **files: bytes) -> Path:
    root = tmp / "card"
    for rel, data in files.items():
        path = root / rel.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


GOOD = {
    "payload.bin": b"\0" * 0x118 + b"ICTC655\0",
    "bootloader__update.bin": b"x", "bootloader__sys__nyx.bin": b"x",
    "bootloader__hekate_ipl.ini": b"[config]\n[CFW (emuMMC)]\npkg3=atmosphere/package3\nemummcforce=1\n",
    "atmosphere__package3": b"x", "atmosphere__stratosphere.romfs": b"x", "hbmenu.nro": b"x",
    "emuMMC__emummc.ini": b"[emummc]\nenabled=1\nsector=0x1b9e0000\npath=emuMMC/RAW1\n",
    "emuMMC__RAW1__raw_based": struct.pack("<I", 0x1B9E0000),
    "exosphere.ini": b"[exosphere]\nblank_prodinfo_emummc=1\n",
    "atmosphere__config__system_settings.ini": b"[atmosphere]\nenable_dns_mitm = u8!0x1\n",
    "atmosphere__hosts__default.txt": b"127.0.0.1 *nintendo.*\n",
    "atmosphere__contents__420000000000000B__exefs.nsp": b"x", "switch__daybreak.nro": b"x",
}


def fuel_gauge_dump(designed: float, full: float, cycles: float = 123.4, charge: float = 81.5,
                    celsius: float = 32.4, millivolts: int = 4000) -> bytes:
    """What hekate's Dump Fuel Regs writes: the MAX17050's 256 registers, with these readings in them."""
    reg = [0] * 256
    reg[0x18], reg[0x10], reg[0x17] = int(designed * 2), int(full * 2), int(cycles * 100)
    reg[0x06], reg[0x08], reg[0x09] = int(charge * 256), int(celsius * 256) & 0xFFFF, int(millivolts / 0.625) << 3
    return struct.pack("<256H", *reg)


def code_image(revision: int | None, libnx: bool = True) -> bytes:
    """The start of a homebrew program: `revision` None leaves libnx's ABI marker out, as old builds do."""
    image = bytearray(0x100)
    struct.pack_into("<I", image, 4, 0x80)
    image[0x10:0x14] = b"NRO0"
    image[0x80:0x84] = b"MOD0"
    if libnx:
        image[0x9C:0xA0], image[0xA8:0xAC] = b"LNY0", b"LNY1"
    if revision is not None:
        image[0xB4:0xB8] = b"LNY2"
        struct.pack_into("<I", image, 0xB8, revision)
    return bytes(image)


def lz4_literals(data: bytes) -> bytes:
    """`data` as one literal-only LZ4 sequence, which is a valid block."""
    head = bytearray([min(len(data), 15) << 4])
    if len(data) >= 15:
        rest = len(data) - 15
        head += b"\xff" * (rest // 255) + bytes([rest % 255])
    return bytes(head) + data


def sysmodule_nsp(image: bytes, compressed: bool) -> bytes:
    """An exefs.nsp: a PFS0 archive holding `main`, an NSO whose text segment is `image`."""
    text = lz4_literals(image) if compressed else image
    nso = bytearray(0x100)
    nso[0:4] = b"NSO0"
    struct.pack_into("<I", nso, 0xC, 1 if compressed else 0)
    struct.pack_into("<III", nso, 0x10, 0x100, 0, len(image))
    struct.pack_into("<I", nso, 0x60, len(text))
    nso += text
    names = b"main\0\0\0\0"
    header = b"PFS0" + struct.pack("<III", 1, len(names), 0) + struct.pack("<QQII", 0, len(nso), 0, 0)
    return header + names + bytes(nso)
