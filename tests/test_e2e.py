"""The raw commands against image files attached as disks: macOS, Linux (root) and Windows (admin)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import MIB, TestDisk, build_card, card_tree, dir_node, file_node, rand, run_tool

from switchsd import cli, disks, games, migrate
from switchsd.blockdev import BlockDev
from switchsd.common import Failure, is_junk
from switchsd.components import Component
from switchsd.disks import check_disk
from switchsd.fat32 import ATTR_ARCHIVE, ATTR_DIR, Fat32
from switchsd.imagefile import CompactImage
from switchsd.install import Item
from switchsd.mbr import Part, format_layout, parse_mbr, plan_layout

SRC_BYTES, DST_BYTES = 160 * MIB, 512 * MIB
FAT = Part(1, 0x0C, 32_768, 196_608)
EMU = Part(2, 0xE0, 229_376, 81_920)
EMU_SECTOR = EMU.start + 0x8000


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TestDisk.available():  # asked here, not at import: on macOS the answer takes attaching an image
            raise unittest.SkipTest("needs a disk-image attach this OS lets us write raw (root on Linux)")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.big = rand(3 * MIB + 123, 5)
        self.emu = rand(EMU.size * 512, 6)
        self.src = self.dir / "old card.img"
        build_card(self.src, SRC_BYTES, FAT, EMU, card_tree(self.big, EMU_SECTOR), self.emu)

    def disk(self, size: int, image: Path | None = None) -> TestDisk:
        path = image or self.dir / f"disk{size}.img"
        if not path.exists():
            with open(path, "wb") as f:
                f.truncate(size)
        disk = TestDisk(path)
        disk.attach()
        self.addCleanup(disk.detach)
        return disk

    def os_check(self, disk: TestDisk) -> None:
        if sys.platform == "darwin":
            result = subprocess.run(["/sbin/fsck_msdos", "-n", disk.raw() + "s1"], capture_output=True, text=True,
                                    check=False, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        elif sys.platform.startswith("linux") and shutil.which("fsck.fat"):
            result = subprocess.run(["fsck.fat", "-n", disk.id + "p1"], capture_output=True, text=True, check=False,
                                    timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux keeps a cache in front of a block device")
    def test_linux_reads_after_a_sync_come_from_the_card(self):
        disk = self.disk(64 * MIB)
        with BlockDev(disk.raw(), writable=True) as dev:
            dev.write(MIB, b"A" * 4096)
            dev.sync()
            with open(disk.image, "r+b") as backing:  # the card holds something else than was written, as a fake does
                backing.seek(MIB)
                backing.write(b"B" * 4096)
                backing.flush()
                os.fsync(backing.fileno())
            self.assertEqual(dev.read(MIB, 4096), b"B" * 4096, "the read-back was answered from memory")

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("dmsetup") and shutil.which("mkfs.vfat"),
                         "needs Linux with dmsetup and dosfstools")
    def test_linux_sees_the_system_through_a_mapped_volume_and_cannot_open_a_disk_in_use(self):
        disk = self.disk(DST_BYTES)
        made = run_tool("format", disk.id, "--expect-bytes", str(DST_BYTES), "--yes")
        self.assertEqual(made.returncode, 0, made.stderr)
        name, point = f"switchsd-test-{os.getpid()}", self.dir / "mapped"
        point.mkdir()

        def sh(*cmd: str) -> None:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)

        sectors = int(Path(f"/sys/class/block/{Path(disk.id).name}p1/size").read_text())
        # As LUKS or LVM would sit on the partition. Without udev, as in a container, dmsetup makes the node.
        sh("dmsetup", "create", "--noudevsync", name, "--table", f"0 {sectors} linear {disk.id}p1 0")

        def unmap() -> None:
            subprocess.run(["dmsetup", "remove", "--noudevsync", "--retry", name], capture_output=True, check=False)
            if subprocess.run(["dmsetup", "info", name], capture_output=True, check=False).returncode:
                with contextlib.suppress(OSError):  # the volume is gone; without udev its node stays behind
                    os.unlink(f"/dev/mapper/{name}")

        self.addCleanup(unmap)
        sh("dmsetup", "mknodes", name)
        for _ in range(10):  # a mapping made a moment ago can still refuse the mount (seen once in a container)
            mounted = subprocess.run(["mount", "-t", "vfat", f"/dev/mapper/{name}", str(point)],
                                     capture_output=True, text=True, check=False, timeout=60)
            if mounted.returncode == 0:
                break
            time.sleep(0.5)
        self.assertEqual(mounted.returncode, 0, mounted.stderr)
        self.addCleanup(subprocess.run, ["umount", str(point)], capture_output=True, check=False)

        self.assertFalse(disks.get_disk(disk.id).system)
        with mock.patch.object(disks, "SYSTEM_MOUNTS", frozenset({str(point)})):
            self.assertTrue(disks.get_disk(disk.id).system)
            with self.assertRaisesRegex(Failure, "holds the running system"):
                check_disk(disk.id, DST_BYTES, writing=True)
        with self.assertRaisesRegex(Failure, "is busy"):  # the kernel's own refusal, whatever the check thought
            BlockDev(disk.raw(), writable=True)
        result = run_tool("format", disk.id, "--expect-bytes", str(DST_BYTES), "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is busy", result.stderr)
        with BlockDev(disk.raw()) as dev:
            self.assertEqual([(p.type, p.start) for p in parse_mbr(dev.read(0, 512))], [(0x0C, 32_768)])

    def mounted(self, disk: TestDisk) -> Path:
        """Mount the disk's first partition through the system's own FAT driver."""
        if sys.platform == "darwin":
            part = disk.id + "s1"
            subprocess.run(["/usr/sbin/diskutil", "mount", part], capture_output=True, check=True, timeout=120)
            self.addCleanup(subprocess.run, ["/usr/sbin/diskutil", "unmount", part], capture_output=True,
                            check=False, timeout=120)
            info = subprocess.run(["/usr/sbin/diskutil", "info", part], capture_output=True, text=True, check=True,
                                  timeout=120).stdout
            return Path(next(line.split(":", 1)[1].strip() for line in info.splitlines() if "Mount Point" in line))
        point = self.dir / "mounted"
        point.mkdir()
        subprocess.run(["mount", "-t", "vfat", disk.id + "p1", str(point)], capture_output=True, check=True, timeout=60)
        self.addCleanup(subprocess.run, ["umount", str(point)], capture_output=True, check=False, timeout=60)
        return point

    @unittest.skipIf(sys.platform == "win32", "mounts the test card as a folder, which the Windows tests do not")
    def test_games_on_a_real_fat32_volume(self):
        disk = self.disk(DST_BYTES)
        made = run_tool("format", disk.id, "--expect-bytes", str(DST_BYTES), "--yes")
        self.assertEqual(made.returncode, 0, made.stderr)
        card = self.mounted(disk)
        big, small = self.dir / "Big Game [0100ABC000000000][v0].nsp", self.dir / "Small Game.xci"
        big.write_bytes(rand(1_000_123, 21))
        small.write_bytes(rand(70_000, 22))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(games, "LIMIT", 300_000), mock.patch.object(games, "PART", 262_144), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["games", "--card", str(card), str(big), str(small)]), 0, err.getvalue())
            self.assertEqual(cli.main(["games", "--card", str(card), str(big), str(small)]), 0, err.getvalue())
        self.assertEqual(err.getvalue().count("already on the card"), 2)
        marked = games.archive_state(card / "games" / big.name)
        if sys.platform.startswith("linux"):  # Linux sets the attribute through the mounted card
            self.assertTrue(marked)
            self.assertNotIn("Fix Archive Bit", out.getvalue())
        else:  # macOS cannot, and says where it gets done
            self.assertIsNone(marked)
            self.assertIn("In hekate: Tools > Arch bit (the second tab) > Fix Archive Bit", out.getvalue())
        subprocess.run(["/usr/sbin/diskutil", "unmount", disk.id + "s1"] if sys.platform == "darwin"
                       else ["umount", str(card)], capture_output=True, check=True, timeout=120)

        with BlockDev(disk.raw()) as dev:  # and what our own reader finds on the card afterwards
            fat = Fat32(dev, 32_768 * 512)
            found = {e.path: e for e in fat.walk() if not is_junk(e.path)}
            folder = found[f"games/{big.name}"]
            self.assertTrue(folder.attr & ATTR_DIR)
            self.assertEqual(bool(folder.attr & ATTR_ARCHIVE), sys.platform.startswith("linux"))
            parts = [found[f"games/{big.name}/{number:02d}"] for number in range(4)]
            self.assertEqual([e.size for e in parts], [262_144, 262_144, 262_144, 1_000_123 - 3 * 262_144])
            self.assertEqual(b"".join(fat.read_file(e) for e in parts), big.read_bytes())
            self.assertEqual(fat.read_file(found[f"games/{small.name}"]), small.read_bytes())
            self.assertFalse([p for p in found if p.endswith(games.TMP)])
        self.os_check(disk)

    def test_clone(self):
        disk = self.disk(DST_BYTES)
        size = str(DST_BYTES)
        dry = run_tool("clone", str(self.src), disk.id, "--expect-bytes", size, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        wrong = run_tool("clone", str(self.src), disk.id, "--expect-bytes", "12345", "--yes")
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("--expect-bytes", wrong.stderr)
        unconfirmed = run_tool("clone", str(self.src), disk.id, "--expect-bytes", size)
        self.assertIn("confirmation", unconfirmed.stderr)

        result = run_tool("clone", str(self.src), disk.id, "--expect-bytes", size, "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = plan_layout([FAT, EMU], SRC_BYTES // 512, DST_BYTES // 512)
        new_sector = EMU_SECTOR + plan.shift
        with BlockDev(disk.raw()) as dev:
            self.assertEqual(parse_mbr(dev.read(0, 512)), plan.parts)
            self.assertEqual(dev.read(plan.emummc[0].start * 512, len(self.emu)), self.emu)
            fat = Fat32(dev, plan.fat.start * 512)
            found = {e.path: e for e in fat.walk()}
            self.assertEqual(fat.read_file(found["switch/app.nro"]), self.big)
            self.assertEqual(fat.read_file(found["Folder With Spaces/fil æøå.txt"]), b"hei")
            ini = fat.read_file(found["emuMMC/emummc.ini"])
            self.assertIn(f"sector={new_sector:#x}\r\n".encode(), ini)
            self.assertEqual(fat.read_file(found["emuMMC/RAW1/raw_based"]), new_sector.to_bytes(4, "little"))
            nca = "emuMMC/RAW1/Nintendo/Contents/registered/00000000/fake.nca"
            self.assertTrue(found[nca].attr & ATTR_ARCHIVE)
            self.assertFalse(found["switch"].attr & ATTR_ARCHIVE)
            self.assertFalse([p for p in found if is_junk(p)])
        self.os_check(disk)

    def test_format(self):
        disk = self.disk(DST_BYTES)
        result = run_tool("format", disk.id, "--expect-bytes", str(DST_BYTES), "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        with BlockDev(disk.raw()) as dev:
            parts = parse_mbr(dev.read(0, 512))
            self.assertEqual([(p.type, p.start) for p in parts], [(0x0C, 32_768)])
            fat = Fat32(dev, parts[0].start * 512)
            self.assertEqual(list(fat.walk()), [])
            self.assertEqual(fat.volume_label(), "SWITCH SD")
        self.os_check(disk)

    def test_image(self):
        disk = self.disk(SRC_BYTES, self.src)
        out = self.dir / "card.swimg"
        result = run_tool("image", disk.id, str(out), "--expect-bytes", str(SRC_BYTES))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(out.stat().st_size, SRC_BYTES // 2)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        self.assertEqual(result.stdout.strip(), digest)
        self.assertEqual(Path(str(out) + ".sha256").read_text(), f"{digest}  card.swimg\n")
        with CompactImage(str(out)) as image:
            image.verify()
            self.assertEqual(image.read(EMU.start * 512, len(self.emu)), self.emu)
        again = run_tool("image", disk.id, str(out))
        self.assertEqual(again.returncode, 1)
        self.assertIn("already exists", again.stderr)

        full = self.dir / "copy.img"
        result = run_tool("image", disk.id, str(full), "--full", "--expect-bytes", str(SRC_BYTES))
        self.assertEqual(result.returncode, 0, result.stderr)
        digest = hashlib.sha256(self.src.read_bytes()).hexdigest()
        self.assertEqual(hashlib.sha256(full.read_bytes()).hexdigest(), digest)
        self.assertEqual(Path(str(full) + ".sha256").read_text(), f"{digest}  copy.img\n")
        self.assertIn("matches copy.img.sha256", run_tool("info", str(full), "--verify").stdout)

    def card_files(self, disk: TestDisk, parts: list[Part]) -> dict[str, bytes]:
        """Check the partition table and the emuMMC; returns every file on the card."""
        with BlockDev(disk.raw()) as dev:
            self.assertEqual(parse_mbr(dev.read(0, 512)), parts)
            if len(parts) > 1:
                self.assertEqual(dev.read(parts[1].start * 512, len(self.emu)), self.emu)
            fat = Fat32(dev, parts[0].start * 512)
            return {e.path: fat.read_file(e) for e in fat.walk() if not e.is_dir}

    def test_restore(self):
        image = self.dir / "card.swimg"
        made = run_tool("image", self.disk(SRC_BYTES, self.src).id, str(image))
        self.assertEqual(made.returncode, 0, made.stderr)
        old_ini = f"sector={EMU_SECTOR:#x}\r\n".encode()

        same = self.disk(SRC_BYTES)
        result = run_tool("restore", str(image), same.id, "--expect-bytes", str(SRC_BYTES), "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("matches the checksum stored in it", result.stderr)
        files = self.card_files(same, [FAT, EMU])
        self.assertEqual(files["switch/app.nro"], self.big)
        self.assertIn(old_ini, files["emuMMC/emummc.ini"])
        self.assertEqual(files["emuMMC/RAW1/raw_based"], EMU_SECTOR.to_bytes(4, "little"))
        self.assertFalse([p for p in files if is_junk(p)])
        self.os_check(same)

        bigger = self.disk(DST_BYTES)
        result = run_tool("restore", str(image), bigger.id, "--expect-bytes", str(DST_BYTES), "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = plan_layout([FAT, EMU], SRC_BYTES // 512, DST_BYTES // 512)
        moved = self.card_files(bigger, plan.parts)
        self.assertIn(f"sector={EMU_SECTOR + plan.shift:#x}\r\n".encode(), moved["emuMMC/emummc.ini"])
        self.assertEqual({p: d for p, d in moved.items() if not p.startswith("emuMMC/")},
                         {p: d for p, d in files.items() if not p.startswith("emuMMC/")})

        damaged = self.dir / "damaged.swimg"
        data = bytearray(image.read_bytes())
        data[len(data) // 2] ^= 1
        damaged.write_bytes(data)
        with BlockDev(same.raw()) as dev:
            before = dev.read(0, 512)
        result = run_tool("restore", str(damaged), same.id, "--expect-bytes", str(SRC_BYTES), "--yes")
        self.assertEqual(result.returncode, 3)
        self.assertIn("damaged", result.stderr)
        small = self.disk(64 * MIB)
        result = run_tool("restore", str(image), small.id, "--expect-bytes", str(64 * MIB), "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("too small", result.stderr)
        cut = self.dir / "cut.img"
        cut.write_bytes(self.src.read_bytes()[:EMU.start * 512 + MIB])  # ends inside the emuMMC partition
        result = run_tool("restore", str(cut), same.id, "--expect-bytes", str(SRC_BYTES), "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ends before its partition 2 does", result.stderr)
        with BlockDev(same.raw()) as dev, BlockDev(small.raw()) as untouched:
            self.assertEqual(dev.read(0, 512), before)
            self.assertEqual(untouched.read(0, 512), bytes(512))

    def test_new(self):
        disk = self.disk(DST_BYTES)
        files = {"payload.bin": b"hekate", "bootloader/hekate_ipl.ini": b"[config]\n",
                 "switch/App/App.nro": rand(200_000, 9)}
        plan = {dest.lower(): Item(dest, "core", lambda data=data: data, hashlib.sha256(data).hexdigest(),  # type: ignore[misc]
                                   len(data), False) for dest, data in files.items()}
        chosen = ([Component("core", "Core", "core", True, "", "x/core", None, ())],
                  {"core": {"tag": "v1", "assets": {}}}, plan, {})
        args = argparse.Namespace(disk=disk.id, expect_bytes=DST_BYTES, emummc="none", label="SWITCH SD",
                                  dry_run=False, yes=True, skip_probe=False)
        out = io.StringIO()
        with mock.patch.object(cli, "resolve_plan", lambda *_: chosen), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_new(args), 0)
        self.assertIn("4 files from 1 components", out.getvalue())
        self.assertIn("1. back up the console's own storage", out.getvalue())
        self.assertIn("Tools > Partition SD Card", out.getvalue())
        found = self.card_files(disk, [format_layout(DST_BYTES // 512)])
        record = json.loads(found.pop("config/switchsd/installed.json"))
        self.assertEqual(found, files)
        self.assertEqual(record["components"]["core"]["version"], "v1")
        self.assertEqual(set(record["components"]["core"]["files"]), set(files))
        self.os_check(disk)

    def test_a_new_card_can_carry_the_partition_for_an_emummc(self):
        disk = self.disk(SRC_BYTES)
        root = dir_node("", file_node("payload.bin", b"hekate"))
        with contextlib.redirect_stderr(io.StringIO()):
            count = migrate.build_card(check_disk(disk.id, SRC_BYTES, writing=True), [FAT, EMU], "SWITCH SD", root,
                                       skip_probe=False)
        self.assertEqual(count, 1)
        with BlockDev(disk.raw()) as dev:
            self.assertEqual(parse_mbr(dev.read(0, 512)), [FAT, EMU])
            fat = Fat32(dev, FAT.start * 512)
            self.assertEqual([e.path for e in fat.walk()], ["payload.bin"])
        self.os_check(disk)


if __name__ == "__main__":
    unittest.main()
