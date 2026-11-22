"""The FAT32 reader against an independent builder, the writer against the reader and the OS's own fsck."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import MIB, TestDisk, dir_node, file_node, rand

from switchsd.blockdev import BlockDev
from switchsd.common import Failure, is_junk
from switchsd.fat32 import (ATTR_ARCHIVE, ATTR_DIR, DATA_ALIGN, Fat32, geometry, lfn_checksum, make_names,
                            write_fat32)
from switchsd.mbr import Part, build_mbr


class FatBuilder:
    """A small volume with FAT32's on-disk structures, written independently of switchsd's writer."""

    def __init__(self, clusters: int = 600, cluster_bytes: int = 1024) -> None:
        self.cbytes, self.clusters = cluster_bytes, clusters
        self.fat = [0] * (clusters + 2)
        self.fat[0], self.fat[1] = 0x0FFFFFF8, 0x0FFFFFFF
        self.used: set[int] = set()
        self.data: dict[int, bytes] = {}
        self.seq = 0

    def alloc(self, n: int, step: int = 1) -> list[int]:
        picked, c = [], 2
        while len(picked) < n:
            if c in self.used:
                c += 1
                continue
            picked.append(c)
            self.used.add(c)
            c += step
        for a, b in zip(picked, picked[1:]):
            self.fat[a] = b
        if picked:
            self.fat[picked[-1]] = 0x0FFFFFFF
        return picked

    def store(self, clusters: list[int], data: bytes) -> None:
        for i, c in enumerate(clusters):
            self.data[c] = data[i * self.cbytes:(i + 1) * self.cbytes].ljust(self.cbytes, b"\0")

    @staticmethod
    def lfn(name: str, short: bytes) -> list[bytes]:
        raw = name.encode("utf-16-le")
        units = [raw[i:i + 2] for i in range(0, len(raw), 2)]
        if len(units) % 13:
            units.append(b"\0\0")
        while len(units) % 13:
            units.append(b"\xff\xff")
        pieces = [b"".join(units[i:i + 13]) for i in range(0, len(units), 13)]
        out = []
        for seq, piece in enumerate(pieces, 1):
            e = bytearray(32)
            e[0] = seq | (0x40 if seq == len(pieces) else 0)
            e[1:11], e[11], e[13] = piece[:10], 0x0F, lfn_checksum(short)
            e[14:26], e[28:32] = piece[10:22], piece[22:26]
            out.append(bytes(e))
        return out[::-1]

    @staticmethod
    def entry(short: bytes, attr: int, cluster: int, size: int) -> bytes:
        e = bytearray(32)
        e[:11], e[11] = short, attr
        struct.pack_into("<HHHHI", e, 20, cluster >> 16, 12 << 11, (45 << 9) | (6 << 5) | 15, cluster & 0xFFFF, size)
        return bytes(e)

    def directory(self, tree: dict, cluster: list[int], parent: int, root: bool, attrs: dict, path: str) -> None:
        records = [self.entry(b"TESTVOL    ", 0x08, 0, 0)] if root else [
            self.entry(b".          ", 0x10, cluster[0], 0), self.entry(b"..         ", 0x10, parent, 0)]
        records.append(b"\xe5" + bytes(31))
        for name, value in tree.items():
            child = f"{path}/{name}" if path else name
            if name.isupper() and len(name) <= 8 and "." not in name:
                short, names = name.encode().ljust(11), []
            else:
                self.seq += 1
                short = f"F{self.seq:06d}".encode().ljust(8) + b"BIN"
                names = self.lfn(name, short)
            if isinstance(value, dict):
                count = 3 + sum(2 + len(k) // 13 for k in value)
                clusters = self.alloc(-(-(count * 32 + 32) // self.cbytes))
                self.directory(value, clusters, 0 if root else cluster[0], False, attrs, child)
                records += [*names, self.entry(short, attrs.get(child, 0x10), clusters[0], 0)]
            else:
                clusters = self.alloc(-(-len(value) // self.cbytes), step=2 if len(value) > 3 * self.cbytes else 1)
                self.store(clusters, value)
                records += [*names, self.entry(short, 0x20, clusters[0] if clusters else 0, len(value))]
        self.store(cluster, b"".join(records))

    def build(self, tree: dict, attrs: dict) -> bytes:
        root = self.alloc(4)
        self.directory(tree, root, 0, True, attrs, "")
        fat_sectors = -(-(len(self.fat) * 4) // 512)
        reserved, spc = 32, self.cbytes // 512
        total = reserved + 2 * fat_sectors + self.clusters * spc
        bs = bytearray(512)
        bs[0:3], bs[3:11] = b"\xeb\x58\x90", b"TESTFAT "
        struct.pack_into("<HBHBHHBHHHII", bs, 11, 512, spc, reserved, 2, 0, 0, 0xF8, 0, 63, 255, 0, total)
        struct.pack_into("<IHHIHH", bs, 36, fat_sectors, 0, 0, root[0], 1, 6)
        bs[66], bs[71:82], bs[82:90], bs[510:512] = 0x29, b"BOOTLABEL  ", b"FAT32   ", b"\x55\xaa"
        fat = struct.pack(f"<{len(self.fat)}I", *self.fat).ljust(fat_sectors * 512, b"\0")
        data = bytearray(self.clusters * self.cbytes)
        for c, chunk in self.data.items():
            data[(c - 2) * self.cbytes:(c - 1) * self.cbytes] = chunk
        return bytes(bs) + bytes((reserved - 1) * 512) + fat + fat + bytes(data)


def flat(tree: dict, prefix: str = "") -> dict:
    out = {}
    for name, value in tree.items():
        path = f"{prefix}/{name}" if prefix else name
        out[path] = None if isinstance(value, dict) else value
        if isinstance(value, dict):
            out.update(flat(value, path))
    return out


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.big = rand(9000, 1)
        self.tree = {
            "emuMMC": {"emummc.ini": b"[emummc]\nsector=0x2368000\n", "RAW1": {"raw_based": b"\0\x80\x36\x02"}},
            "switch": {"app with spaces.nro": self.big, "empty.txt": b"", "._app.nro": b"junk"},
            "Nintendo": {"Contents": {"registered": {"00000000": {"abc.nca": {"00": b"x" * 100}}}}},
            "BOOTDIR": {"Æøå ünïcode name that is long.txt": b"hello"},
            "many": {f"file number {i:03d} with a long name.bin": bytes([i]) * i for i in range(40)},
            ".DS_Store": b"junk",
        }
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "fat.img")
        self.path.write_bytes(FatBuilder().build(self.tree, {"Nintendo/Contents/registered/00000000/abc.nca": 0x30}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_walk_finds_every_entry_with_its_content(self):
        with BlockDev(str(self.path)) as dev:
            fat = Fat32(dev, 0)
            found = {e.path: e for e in fat.walk()}
            self.assertEqual(set(found), set(flat(self.tree)))
            for path, content in flat(self.tree).items():
                self.assertEqual(found[path].is_dir, content is None, path)
                if content is not None:
                    self.assertEqual(fat.read_file(found[path]), content, path)
            self.assertTrue(found["Nintendo/Contents/registered/00000000/abc.nca"].attr & ATTR_ARCHIVE)
            self.assertEqual(fat.label, "TESTVOL")

    def test_parents_come_first_and_junk_is_skipped(self):
        skipped: list[str] = []
        with BlockDev(str(self.path)) as dev:
            order = [e.path for e in Fat32(dev, 0).walk(is_junk, skipped)]
        for i, path in enumerate(order):
            if "/" in path:
                self.assertIn(path.rsplit("/", 1)[0], order[:i])
        self.assertEqual(sorted(skipped), [".DS_Store", "switch/._app.nro"])

    def test_broken_chain_is_reported(self):
        data = bytearray(self.path.read_bytes())
        struct.pack_into("<I", data, 32 * 512 + 2 * 4, 0)
        self.path.write_bytes(bytes(data))
        with BlockDev(str(self.path)) as dev, self.assertRaisesRegex(Failure, "broken FAT chain"):
            list(Fat32(dev, 0).walk())


class NameTests(unittest.TestCase):
    def test_short_names(self):
        used: set[bytes] = set()
        self.assertEqual(make_names("PAYLOAD.BIN", used), (b"PAYLOAD BIN", []))
        short, lfn = make_names("payload.bin", set())
        self.assertEqual(short, b"PAYLOAD BIN")  # 8.3-only loaders still find it
        self.assertEqual(len(lfn), 1)
        short, lfn = make_names(".overlays", set())
        self.assertEqual(short, b"OVERLA~1   ")
        self.assertTrue(lfn)
        used = set()
        shorts = {make_names(f"file number {i} with a long name.bin", used)[0] for i in range(150)}
        self.assertEqual(len(shorts), 150)

    def test_bad_names(self):
        for name in ("", ".", "..", "a/b", "what?", "trailing.", "x" * 256, "tab\there"):
            with self.assertRaises(Failure, msg=name):
                make_names(name, set())


class WriterTests(unittest.TestCase):
    def build(self, root, size=96 * MIB, start=2048):  # type: ignore[no-untyped-def]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name, "vol.img")
        with open(path, "wb") as f:
            f.truncate(size)
        with BlockDev(str(path), writable=True) as dev:
            write_fat32(dev, start, size // 512 - start, "TESTVOL", root)
        return path

    def test_round_trip(self):
        big = rand(5 * MIB + 77, 2)
        many = [file_node(f"entry {i:03d} with quite a long name.txt", bytes([i % 256]) * i) for i in range(300)]
        root = dir_node("", file_node("payload.bin", b"payload"), file_node("empty", b""),
                        dir_node("switch", file_node("big.nro", big), dir_node(".overlays", file_node("o.ovl", b"o"))),
                        dir_node("many", *many), dir_node("x.nca", file_node("00", b"n"), attr=ATTR_DIR | ATTR_ARCHIVE),
                        file_node("Æøå.txt", "æøå".encode()))
        path = self.build(root)
        with BlockDev(str(path)) as dev:
            fat = Fat32(dev, 2048 * 512)
            found = {e.path: e for e in fat.walk()}
            self.assertEqual(fat.volume_label(), "TESTVOL")
            self.assertEqual(fat.read_file(found["switch/big.nro"]), big)
            self.assertEqual(hashlib.sha256(fat.read_file(found["payload.bin"])).hexdigest(),
                             root.children[0].sha256)
            self.assertEqual(fat.read_file(found["empty"]), b"")
            self.assertEqual(fat.read_file(found["Æøå.txt"]), "æøå".encode())
            self.assertTrue(found["x.nca"].attr & ATTR_ARCHIVE)
            self.assertFalse(found["switch"].attr & ATTR_ARCHIVE)
            self.assertEqual(len([p for p in found if p.startswith("many/")]), 300)
            last = found["many/entry 299 with quite a long name.txt"]
            self.assertEqual(fat.read_file(last), bytes([299 % 256]) * 299)

    def test_case_duplicates_are_refused(self):
        with self.assertRaisesRegex(Failure, "differ only in case"):
            self.build(dir_node("", file_node("A.txt", b"1"), file_node("a.TXT", b"2")))

    def test_short_source_is_refused(self):
        lying = file_node("x", b"abc")
        lying.size = 10
        with self.assertRaisesRegex(Failure, "expected 10 bytes"):
            self.build(dir_node("", lying))

    def test_geometry(self):
        spc, reserved, fat, clusters = geometry(32_768, 463_273_984)
        self.assertEqual(spc, 128)  # 64 KiB clusters like hekate
        self.assertEqual((32_768 + reserved + 2 * fat) % DATA_ALIGN, 0)
        self.assertGreaterEqual(fat * 512, (clusters + 2) * 4)
        small = geometry(2048, 96 * MIB // 512 - 2048)
        self.assertLess(small[0], 128)
        self.assertGreaterEqual(small[3], 65525)
        with self.assertRaisesRegex(Failure, "too small"):
            geometry(2048, 20 * MIB // 512)

    def test_macos_fsck_accepts_it(self):
        if not (sys.platform == "darwin" and TestDisk.available()):  # asked here: the answer attaches an image
            self.skipTest("needs macOS and disk image attach")
        size = 160 * MIB
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name, "card.img")
        with open(path, "wb") as f:
            f.truncate(size)
        part = Part(1, 0x0C, 32_768, size // 512 - 32_768)
        root = dir_node("", file_node("payload.bin", b"p"), dir_node("switch", file_node("a.nro", rand(70_000, 3))))
        with BlockDev(str(path), writable=True) as dev:
            write_fat32(dev, part.start, part.size, "SWITCH SD", root)
            dev.write(0, build_mbr(bytes(440), [part], 1))
        disk = TestDisk(path)
        disk.attach()
        try:
            result = subprocess.run(["/sbin/fsck_msdos", "-n", disk.raw() + "s1"], capture_output=True, text=True,
                                    check=False, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            disk.detach()

    @unittest.skipUnless(shutil.which("fsck.fat"), "needs dosfstools")
    def test_linux_fsck_accepts_it(self):
        root = dir_node("", file_node("payload.bin", b"p"), dir_node("switch", file_node("a.nro", rand(70_000, 4))))
        path = self.build(root, start=0)
        result = subprocess.run(["fsck.fat", "-n", "-v", str(path)], capture_output=True, text=True, check=False,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
