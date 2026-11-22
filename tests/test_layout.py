"""MBR, hekate's layout and the emuMMC sector references."""

from __future__ import annotations

import struct
import unittest

import support  # noqa: F401  (puts the repository on sys.path)
from switchsd.common import Failure
from switchsd.emummc import config_patcher, patch_emummc_ini, patch_raw_based
from switchsd.mbr import ALIGN, Part, build_mbr, emummc_mib, format_layout, new_layout, parse_mbr, plan_layout

# The real cards this tool was written for: a 32 GB card set up by hekate and a 256 GB card.
OLD_SECTORS = 62_333_952
OLD_PARTS = [Part(1, 0x0C, 32_768, 37_060_608), Part(2, 0xE0, 37_093_376, 25_229_312)]
NEW_SECTORS = 488_554_496


def plan_for(parts=OLD_PARTS, src=OLD_SECTORS, dst=NEW_SECTORS):  # type: ignore[no-untyped-def]
    return plan_layout(parts, src, dst)


class MbrTests(unittest.TestCase):
    def test_round_trip_keeps_boot_code(self):
        template = bytes(range(256)) * 2
        sector = build_mbr(template, OLD_PARTS, 0x12345678)
        self.assertEqual(len(sector), 512)
        self.assertEqual(sector[:440], template[:440])
        self.assertEqual(struct.unpack_from("<I", sector, 440)[0], 0x12345678)
        self.assertEqual(parse_mbr(sector), OLD_PARTS)

    def test_rejects_missing_signature_gpt_and_overlaps(self):
        with self.assertRaises(Failure):
            parse_mbr(bytes(512))
        with self.assertRaises(Failure):
            parse_mbr(build_mbr(bytes(440), [Part(1, 0xEE, 1, 1000)], 0))
        with self.assertRaises(Failure):
            parse_mbr(build_mbr(bytes(440), [Part(1, 0x0C, 2048, 4096), Part(2, 0xE0, 4000, 100)], 0))


class PlanTests(unittest.TestCase):
    def test_real_cards(self):
        plan = plan_for()
        self.assertEqual(plan.fat, Part(1, 0x0C, 32_768, 463_273_984))
        self.assertEqual(plan.emummc, (Part(2, 0xE0, 463_306_752, 25_229_312),))
        self.assertEqual(plan.shift, 0x19678000)

    def test_formula_reproduces_hekates_layout_of_the_old_card(self):
        emu_mib = -(-OLD_PARTS[1].size // 2048)
        fat_mib = (OLD_SECTORS // 2048 - 16 - emu_mib) // 16 * 16
        self.assertEqual(fat_mib * 2048, OLD_PARTS[0].size)

    def test_refusals(self):
        with self.assertRaisesRegex(Failure, "bigger"):
            plan_for(dst=OLD_SECTORS)
        with self.assertRaisesRegex(Failure, "no emuMMC"):
            plan_for(parts=OLD_PARTS[:1])
        with self.assertRaisesRegex(Failure, "unsupported"):
            plan_for(parts=[*OLD_PARTS, Part(3, 0x83, 62_322_688, 2048)])
        with self.assertRaisesRegex(Failure, "slot 1"):
            plan_for(parts=[Part(2, 0x0C, 32_768, 1000), OLD_PARTS[1]])

    def test_two_emummc_partitions_move_together(self):
        parts = [Part(1, 0x0C, 32_768, 1_000_000), Part(2, 0xE0, 1_032_768, 50_000), Part(3, 0xE0, 1_082_768, 50_000)]
        plan = plan_for(parts=parts, src=1_200_000, dst=4_000_000)
        self.assertEqual([p.start - q.start for p, q in zip(plan.emummc, parts[1:])], [plan.shift] * 2)

    def test_restore_takes_any_card_that_holds_the_emummc(self):
        same = plan_layout(OLD_PARTS, OLD_SECTORS, OLD_SECTORS, restore=True)
        self.assertEqual((same.parts, same.shift), (OLD_PARTS, 0))
        smaller = plan_layout(OLD_PARTS, OLD_SECTORS, 31_116_288, restore=True)  # a 16 GB card
        self.assertEqual(smaller.emummc[0].size, OLD_PARTS[1].size)
        self.assertLess(smaller.shift, 0)
        self.assertLessEqual(smaller.emummc[0].end, 31_116_288)
        with self.assertRaisesRegex(Failure, "too small"):
            plan_layout(OLD_PARTS, OLD_SECTORS, 25_300_000, restore=True)
        files_only = plan_layout(OLD_PARTS[:1], OLD_SECTORS, NEW_SECTORS, restore=True)
        self.assertEqual(files_only.parts, [format_layout(NEW_SECTORS)])
        self.assertEqual((files_only.span, files_only.shift), ((0, 0), 0))
        with self.assertRaisesRegex(Failure, "unsupported"):
            plan_layout([*OLD_PARTS, Part(3, 0x83, 62_322_688, 2048)], OLD_SECTORS, NEW_SECTORS, restore=True)

    def test_a_new_card_gets_the_table_hekate_writes(self):
        # The table found on a 32 GB card set up for a 12 GiB emuMMC; hekate's formulas give the same.
        self.assertEqual(new_layout(OLD_SECTORS, emummc_mib("12")), OLD_PARTS)
        self.assertEqual(new_layout(NEW_SECTORS, emummc_mib("none")), [format_layout(NEW_SECTORS)])
        self.assertEqual((emummc_mib("full"), emummc_mib("full64"), emummc_mib("4")), (29856, 59680, 4128))
        for bad in ("3", "25", "half", "-4"):
            with self.assertRaisesRegex(Failure, "not an emuMMC size"):
                emummc_mib(bad)
        with self.assertRaisesRegex(Failure, "under 2 GB"):
            new_layout(OLD_SECTORS, emummc_mib("full"))

    def test_every_emummc_size_is_one_create_emummc_accepts(self):
        # hekate 6.5.5, gui_emummc_tools.c: a partition in MBR slot 2-4 that starts past 16 MiB and is either
        # 0x80F000 to 0x3810000 sectors (a resized emuMMC) or at least the eMMC's size plus 0xC000.
        emmc_32gb = 0x3A3E000
        full = new_layout(NEW_SECTORS, emummc_mib("full"))[1]
        self.assertEqual((full.slot, full.type), (2, 0xE0))
        self.assertGreaterEqual(full.size, emmc_32gb + 0xC000)
        self.assertLessEqual(full.end, NEW_SECTORS)
        for gib in (4, 12, 24):
            part = new_layout(NEW_SECTORS, emummc_mib(str(gib)))[1]
            self.assertGreater(part.start, 0x8000)
            self.assertTrue(0x80F000 <= part.size <= 0x3810000, gib)

    def test_format_layout(self):
        part = format_layout(NEW_SECTORS)
        self.assertEqual((part.type, part.start), (0x0C, ALIGN))
        self.assertEqual(part.size % 2048, 0)
        self.assertLessEqual(part.end, NEW_SECTORS)
        with self.assertRaisesRegex(Failure, "too small"):
            format_layout(40 * 2048)


class EmummcTests(unittest.TestCase):
    INI = (b"[emummc]\r\nenabled=1\r\nsector=0x2368000\r\npath=emuMMC/RAW1\r\nid=0x31574152\r\n"
           b"nintendo_path=emuMMC/RAW1/Nintendo\r\n")

    def test_emummc_ini_moves_sector_and_keeps_crlf(self):
        out = patch_emummc_ini(self.INI, plan_for())
        self.assertEqual(out, self.INI.replace(b"sector=0x2368000", b"sector=0x1b9e0000"))

    def test_the_sector_is_hexadecimal_with_or_without_the_prefix_as_hekate_reads_it(self):
        out = patch_emummc_ini(b"[emummc]\nsector=2368000\n", plan_for())
        self.assertEqual(out, b"[emummc]\nsector=0x1b9e0000\n")
        with self.assertRaisesRegex(Failure, "not UTF-8"):
            patch_emummc_ini(b"[emummc]\nsector=\xff\xfe\n", plan_for())

    def test_other_sections_and_file_based_emummc_are_untouched(self):
        data = b"[other]\nsector=0x2368000\n[emummc]\nenabled=1\nsector=0x0\npath=emuMMC/SD00\n"
        self.assertEqual(patch_emummc_ini(data, plan_for()), data)

    def test_sector_outside_the_partition_is_refused(self):
        with self.assertRaisesRegex(Failure, "outside"):
            patch_emummc_ini(b"[emummc]\nsector=0x8000\n", plan_for())

    def test_raw_based(self):
        self.assertEqual(patch_raw_based(bytes.fromhex("00803602"), plan_for()), struct.pack("<I", 0x1B9E0000))
        with self.assertRaises(Failure):
            patch_raw_based(b"\0\0\0", plan_for())

    def test_which_files_are_patched(self):
        self.assertIs(config_patcher("emuMMC/emummc.ini"), patch_emummc_ini)
        self.assertIs(config_patcher("emuMMC/RAW1/raw_based"), patch_raw_based)
        self.assertIsNone(config_patcher("emuMMC/RAW1/Nintendo/raw_based"))
        self.assertIsNone(config_patcher("bootloader/hekate_ipl.ini"))


if __name__ == "__main__":
    unittest.main()
