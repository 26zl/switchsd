"""Compact images, and the parts of restore and new that need no disk: all on plain files."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from support import MIB, build_card, card_tree, dir_node, file_node, rand, run_tool
from test_fat32 import FatBuilder

from switchsd.blockdev import BlockDev
from switchsd.common import Failure, Mismatch
from switchsd.fat32 import Fat32, check_fits, write_fat32
from switchsd.imagefile import MAGIC, CompactImage, is_compact, merge, write_image
from switchsd.mbr import Part, parse_mbr, plan_layout
from switchsd.migrate import (flatten, forget_old_layout, open_image, tree_from_fat, tree_from_files, used_regions,
                              verify_fat32)

SIZE = 160 * MIB
FAT = Part(1, 0x0C, 32_768, 196_608)
EMU = Part(2, 0xE0, 229_376, 81_920)
EMU_SECTOR = EMU.start + 0x8000


class UsedRunsTests(unittest.TestCase):
    def test_runs_are_the_clusters_the_fat_marks_used(self):
        builder = FatBuilder()
        tree = {"switch": {"app.nro": rand(9000, 1), "small.txt": b"x"}, "empty.txt": b""}
        builder.fat[500] = 0x0FFFFFF7  # a bad cluster is not worth reading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "fat.img")
            path.write_bytes(builder.build(tree, {}))
            with BlockDev(str(path)) as dev:
                fat = Fat32(dev, 0)
                runs = fat.used_runs()
        kept = set()
        for offset, length in runs:
            first = (offset - fat.data_offset) // fat.cluster_bytes + 2
            kept |= set(range(first, first + length // fat.cluster_bytes))
        self.assertEqual(kept, builder.used)
        self.assertGreater(len(runs), 2)  # the big file sits in every other cluster
        self.assertEqual(runs, sorted(runs))


class ImageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.big = rand(3 * MIB + 123, 5)
        self.emu = rand(EMU.size * 512, 6)
        self.card = self.dir / "card.img"
        build_card(self.card, SIZE, FAT, EMU, card_tree(self.big, EMU_SECTOR), self.emu)

    def compact(self, name: str = "card.swimg") -> Path:
        out = self.dir / name
        with BlockDev(str(self.card)) as src, open(out, "xb") as dst:
            write_image(src, SIZE, used_regions(src, SIZE // 512), dst, {"source": "test"}, "reading")
        return out

    def test_only_what_is_in_use_is_kept_and_reads_back_as_the_card(self):
        out = self.compact()
        self.assertTrue(is_compact(out))
        self.assertFalse(is_compact(self.card))
        self.assertLess(out.stat().st_size, SIZE // 2)
        self.assertGreater(out.stat().st_size, len(self.emu) + len(self.big))
        with CompactImage(str(out)) as image, BlockDev(str(self.card)) as card:
            image.verify()
            self.assertEqual((image.size, image.meta["source"]), (SIZE, "test"))
            self.assertEqual(parse_mbr(image.read(0, 512)), [FAT, EMU])
            self.assertEqual(image.read(EMU.start * 512, len(self.emu)), self.emu)
            self.assertEqual(sum(image.regions[-1]), EMU.end * 512)  # the emuMMC, kept to its last sector
            for offset, length in image.regions:
                self.assertEqual(image.read(offset, length), card.read(offset, length))
            free = sum(image.regions[-2])  # behind the last file, where nothing was written
            self.assertEqual(image.read(free, 4096), bytes(4096))
            across = image.read(free - 1024, 4096)  # a read that leaves a region
            self.assertEqual(across, card.read(free - 1024, 1024) + bytes(3072))
            old, new = Fat32(card, FAT.start * 512), Fat32(image, FAT.start * 512)
            want = {e.path: (e.attr, None if e.is_dir else old.read_file(e)) for e in old.walk()}
            got = {e.path: (e.attr, None if e.is_dir else new.read_file(e)) for e in new.walk()}
            self.assertEqual(got, want)
            self.assertEqual(got["switch/app.nro"][1], self.big)
            with self.assertRaisesRegex(Failure, "beyond"):
                image.read(SIZE - 512, 1024)
            with self.assertRaisesRegex(Failure, "cannot be written"):
                image.write(0, bytes(512))
        dev, sectors = open_image(str(out))
        with dev:
            self.assertIsInstance(dev, CompactImage)
            self.assertEqual(sectors, SIZE // 512)

    def test_damage_is_noticed(self):
        out = self.compact()
        data = bytearray(out.read_bytes())
        flipped = bytearray(data)
        flipped[len(data) // 2] ^= 1
        (self.dir / "flipped.swimg").write_bytes(flipped)
        with CompactImage(str(self.dir / "flipped.swimg")) as image, self.assertRaisesRegex(Mismatch, "damaged"):
            image.verify()
        (self.dir / "short.swimg").write_bytes(data[:-4096])
        with self.assertRaisesRegex(Failure, "incomplete"):
            CompactImage(str(self.dir / "short.swimg"))
        for index in (b"[1,2]", b'{"version":2,"bytes":1e999,"regions":[]}',
                      b'{"version":2,"bytes":512,"regions":[[0,1024]]}'):
            (self.dir / "index.swimg").write_bytes(MAGIC + index + struct.pack("<Q", len(index)) + bytes(32))
            with self.assertRaisesRegex(Failure, "index cannot be read"):
                CompactImage(str(self.dir / "index.swimg"))
        with self.assertRaisesRegex(Failure, "not a complete switchsd image"):
            CompactImage(str(self.card))
        (self.dir / "old.swimg").write_bytes(b"SWITCHSD IMAGE 1" + bytes(4096))
        with self.assertRaisesRegex(Failure, "earlier build"):
            open_image(str(self.dir / "old.swimg"))

    def test_blank_stretches_are_left_out_and_come_back_as_zeros(self):
        blank = self.dir / "blank.img"
        emu = rand(MIB, 7) + bytes(20 * MIB) + rand(MIB + 512, 8) + bytes(EMU.size * 512 - 22 * MIB - 512)
        build_card(blank, SIZE, FAT, EMU, card_tree(self.big, EMU_SECTOR), emu)
        out = self.dir / "blank.swimg"
        with BlockDev(str(blank)) as src, open(out, "xb") as dst:
            _, kept = write_image(src, SIZE, used_regions(src, SIZE // 512), dst, {}, "reading")
        self.assertLess(kept, 30 * MIB)  # of a 40 MiB partition, 2 MiB and a sector hold something
        self.assertLess(out.stat().st_size, kept + 4096)
        with CompactImage(str(out)) as image, BlockDev(str(blank)) as card:
            image.verify()
            self.assertEqual(image.stored, kept)
            self.assertEqual(image.read(EMU.start * 512, len(emu)), emu)
            self.assertEqual(image.read(0, SIZE), card.read(0, SIZE))

    def test_info_reads_and_verifies_an_image(self):
        out = self.compact()
        result = run_tool("info", str(out), "--verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("compact image", result.stdout)
        self.assertIn("matches the checksum stored in it", result.stdout)
        self.assertIn(f"sector': '{EMU_SECTOR:#x}'", result.stdout)
        data = bytearray(out.read_bytes())
        data[len(data) // 2] ^= 1
        out.write_bytes(data)
        self.assertEqual(run_tool("info", str(out), "--verify").returncode, 3)
        raw = run_tool("info", str(self.card), "--verify")
        self.assertEqual(raw.returncode, 1)
        self.assertIn("no checksum", raw.stderr)

    def test_a_layout_restore_cannot_rebuild_is_refused(self):
        linux = self.dir / "linux.img"
        build_card(linux, SIZE, FAT, Part(2, 0x83, EMU.start, EMU.size), dir_node(""), bytes(512))
        with BlockDev(str(linux)) as src, self.assertRaisesRegex(Failure, "unsupported partition types"):
            used_regions(src, SIZE // 512)
        with BlockDev(str(self.card)) as src, self.assertRaisesRegex(Failure, "beyond the end"):
            used_regions(src, EMU.end - 1)

    def test_merge(self):
        self.assertEqual(merge([(100, 50), (0, 10), (10, 5), (150, 1), (400, 0)]), [(0, 15), (100, 51)])
        self.assertEqual(merge([(0, 10), (20, 10), (200, 10)], gap=10), [(0, 30), (200, 10)])
        self.assertEqual(merge([(0, 100), (10, 20)]), [(0, 100)])


class RestoreTests(unittest.TestCase):
    """What `restore` does to the files, on image files instead of a card."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def card(self, emu_sector: int) -> Path:
        path = self.dir / f"card{emu_sector}.img"
        build_card(path, SIZE, FAT, EMU, card_tree(b"app", emu_sector), bytes(512))
        return path

    def test_the_same_size_keeps_every_file_as_it_is(self):
        with BlockDev(str(self.card(EMU_SECTOR))) as src:
            plan = plan_layout(parse_mbr(src.read(0, 512)), SIZE // 512, SIZE // 512, restore=True)
            self.assertEqual((plan.parts, plan.shift), ([FAT, EMU], 0))
            root, skipped, patched, notes = tree_from_fat(Fat32(src, FAT.start * 512), plan, lenient=True)
            self.assertEqual((patched, notes), ([], []))
            self.assertEqual(sorted(skipped), [".DS_Store", "switch/._app.nro"])
            target = self.dir / "restored.img"
            with open(target, "wb") as f:
                f.truncate(SIZE)
            with BlockDev(str(target), writable=True) as dst:
                write_fat32(dst, plan.fat.start, plan.fat.size, "SWITCH SD", root)
                self.assertEqual(verify_fat32(dst, plan.fat.start, root), len(flatten(root)))
                ini = next(e for e in Fat32(dst, plan.fat.start * 512).walk() if e.path == "emuMMC/emummc.ini")
                self.assertIn(f"sector={EMU_SECTOR:#x}\r\n".encode(), Fat32(dst, plan.fat.start * 512).read_file(ini))

    def test_in_the_consoles_own_folders_only_real_macos_files_are_left_out(self):
        tree = card_tree(b"app", EMU_SECTOR)
        tree.children.append(dir_node("JKSV", dir_node("Game", file_node("._settings", b"a save's own file"),
                                                        file_node("._save", bytes.fromhex("00051607") + bytes(60)),
                                                        file_node("save", b"data"))))
        path = self.dir / "saves.img"
        build_card(path, SIZE, FAT, EMU, tree, bytes(512))
        with BlockDev(str(path)) as src:
            plan = plan_layout(parse_mbr(src.read(0, 512)), SIZE // 512, SIZE // 512, restore=True)
            root, skipped, _, _ = tree_from_fat(Fat32(src, FAT.start * 512), plan, lenient=True)
        self.assertIn("jksv/game/._settings", flatten(root))
        self.assertNotIn("jksv/game/._save", flatten(root))
        self.assertEqual(sorted(skipped), [".DS_Store", "JKSV/Game/._save", "switch/._app.nro"])

    def test_a_reference_that_cannot_be_moved_stops_a_clone_but_not_a_restore(self):
        bigger = 512 * MIB // 512
        with BlockDev(str(self.card(0x8000))) as src:  # points into the FAT32 partition, not the emuMMC
            parts = parse_mbr(src.read(0, 512))
            fat = Fat32(src, FAT.start * 512)
            with self.assertRaisesRegex(Failure, "outside the emuMMC partition"):
                tree_from_fat(fat, plan_layout(parts, SIZE // 512, bigger))
            _, _, patched, notes = tree_from_fat(fat, plan_layout(parts, SIZE // 512, bigger, restore=True),
                                                 lenient=True)
            self.assertEqual(patched, [])
            self.assertEqual(len(notes), 2)
            self.assertIn("emuMMC/emummc.ini is kept as it is", notes[0] + notes[1])

    def test_a_damaged_source_is_named_before_anything_is_written(self):
        path = self.card(EMU_SECTOR)
        data = bytearray(path.read_bytes())
        entry = data.index(b"PAYLOAD BIN")
        struct.pack_into("<I", data, entry + 28, 900_000)  # the file claims more than its clusters hold
        path.write_bytes(data)
        with BlockDev(str(path)) as src:
            with self.assertRaisesRegex(Failure, "payload.bin is damaged on the source"):
                used_regions(src, SIZE // 512)
            plan = plan_layout(parse_mbr(src.read(0, 512)), SIZE // 512, SIZE // 512, restore=True)
            with self.assertRaisesRegex(Failure, "payload.bin is damaged on the source"):
                tree_from_fat(Fat32(src, FAT.start * 512), plan, lenient=True)

    def test_the_old_partition_table_goes_before_anything_else(self):
        path = self.card(EMU_SECTOR)
        with BlockDev(str(path), writable=True) as dst:
            forget_old_layout(dst)
            with self.assertRaisesRegex(Failure, "no MBR"):
                parse_mbr(dst.read(0, 512))

    def test_files_that_do_not_fit_are_refused_before_anything_is_written(self):
        root = tree_from_files({"switch/big.nro": bytes(80 * MIB), "payload.bin": b"p"})
        check_fits(32_768, 196_608, root)
        with self.assertRaisesRegex(Failure, r"the files need 0\.1 GB, but the FAT32 partition holds 45 MB"):
            check_fits(32_768, 96_000, root)

    def test_tree_from_files_makes_folders_once_whatever_their_case(self):
        root = tree_from_files({"switch/a.nro": b"a", "Switch/b/c.nro": b"c", "payload.bin": b"p"})
        self.assertEqual(sorted(flatten(root)), ["payload.bin", "switch", "switch/a.nro", "switch/b", "switch/b/c.nro"])
        self.assertEqual([child.is_dir for child in root.children], [True, False])
        with self.assertRaisesRegex(Failure, "differ only in case"):
            check_fits(32_768, 196_608, tree_from_files({"a/x": b"1", "a": b"2"}))


if __name__ == "__main__":
    unittest.main()
