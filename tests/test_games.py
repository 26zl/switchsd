"""`games`: copying game files into a card's games folder, whole or in the parts Horizon reads as one file."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import GOOD, card, rand

from switchsd import cli, games

LIMIT, PART = 300_000, 262_144  # stand-ins for 4 GiB and Horizon's part size


class GamesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.card = card(self.tmp, **GOOD)
        self.folder = self.card / "games"
        for name, value in (("LIMIT", LIMIT), ("PART", PART)):
            patch = mock.patch.object(games, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def source(self, name: str, size: int, seed: int = 1) -> Path:
        path = self.tmp / "from" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(rand(size, seed))
        return path

    def games(self, *args: object) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["games", "--card", str(self.card), *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_a_file_that_fits_is_copied_as_it_is(self):
        small = self.source("Small Game [0100ABC000000000][v0].nsp", 70_000)
        code, out, err = self.games(small)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.folder / small.name).read_bytes(), small.read_bytes())
        self.assertIn(small.name, out)
        self.assertIn("DBI > Browse SD card > games", out)
        self.assertNotIn("Fix Archive Bit", out)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [small.name])

    def test_a_file_fat32_cannot_hold_is_stored_in_parts(self):
        big = self.source("Big Game.xci", 1_000_123)
        exact = self.source("Exactly Two Parts.nsp", 2 * PART, seed=2)
        edge = self.source("The Biggest Plain File.nsz", LIMIT, seed=3)
        code, out, err = self.games(big, exact, edge)
        self.assertEqual(code, 0, err)
        parts = {p.name: p.stat().st_size for p in (self.folder / big.name).iterdir()}
        self.assertEqual(parts, {"00": PART, "01": PART, "02": PART, "03": 1_000_123 - 3 * PART})
        self.assertEqual(b"".join((self.folder / big.name / name).read_bytes() for name in sorted(parts)),
                         big.read_bytes())
        self.assertEqual({p.name: p.stat().st_size for p in (self.folder / exact.name).iterdir()},
                         {"00": PART, "01": PART})
        self.assertTrue((self.folder / edge.name).is_file())
        self.assertIn("Big Game.xci  (4 parts)", out)
        if games.archive_state(self.folder / big.name) is not True:  # a test folder is no FAT volume
            self.assertIn("In hekate: Tools > Arch bit (the second tab) > Fix Archive Bit", out)
        self.assertFalse([p.name for p in self.folder.iterdir() if p.name.endswith(games.TMP)])

    def test_a_second_run_copies_nothing_and_a_different_file_of_that_name_is_refused(self):
        big, small = self.source("Big.nsp", 700_000), self.source("Small.nsp", 5_000)
        self.assertEqual(self.games(big, small)[0], 0)
        stamps = {p: p.stat().st_mtime_ns for p in self.folder.rglob("*")}
        code, _, err = self.games(big, small)
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("already on the card"), 2)
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.folder.rglob("*")}, stamps)
        for changed in (self.source("Small.nsp", 6_000), self.source("Big.nsp", 800_000)):
            code, _, err = self.games(changed)
            self.assertEqual(code, 1)
            self.assertIn("is not this file; delete it there first", err)
        (self.folder / "Big.nsp" / "04").write_bytes(b"x")  # a part too many is not this file either
        self.assertEqual(self.games(self.source("Big.nsp", 700_000))[0], 1)

    def test_listing_and_dry_run_change_nothing(self):
        code, out, _ = self.games()
        self.assertEqual((code, out.strip()), (0, f"no game files in {self.folder}"))
        big = self.source("Big.nsp", 700_000)
        code, _, err = self.games("--dry-run", big)
        self.assertEqual(code, 0)
        self.assertIn("would copy Big.nsp (1 MB) in 3 parts", err)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.games(big)[0], 0)
        (self.folder / "._Big.nsp").write_bytes(b"junk")
        (self.folder / "notes.txt").write_text("not a game")
        code, out, _ = self.games()
        self.assertEqual(code, 0)
        self.assertEqual([line.strip() for line in out.splitlines() if "Big" in line or "notes" in line],
                         ["1 MB  Big.nsp  (3 parts)"])

    def test_what_cannot_be_right_is_refused_before_a_card_is_looked_at(self):
        for args, text in ((["notes.txt"], "is not a game file"), (["bad:name.nsp"], "cannot be a file name"),
                           (["a/Game.nsp", "b/game.NSP"], "same name")):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["games", "--card", "/no/such/card", *args]), 2, args)
            self.assertIn(text, err.getvalue())
        code, _, err = self.games(self.tmp / "missing.nsp")
        self.assertEqual(code, 1)
        self.assertIn("is not a file", err)
        other = self.tmp / "documents"
        (other / "letters").mkdir(parents=True)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["games", "--card", str(other), str(self.source("G.nsp", 10))]), 1)
        self.assertIn("does not look like a Switch SD card", err.getvalue())
        self.assertEqual([p.name for p in other.iterdir()], ["letters"])

    def test_a_copy_that_reads_back_different_is_removed(self):
        big = self.source("Big.nsp", 700_000)
        with mock.patch.object(games, "read_back", lambda files, total, label: b"something else"):
            code, _, err = self.games(big)
        self.assertEqual(code, 3)
        self.assertIn("reads back different", err)
        self.assertEqual(list(self.folder.iterdir()), [])
        with mock.patch.object(games, "read_back", side_effect=AssertionError("must not be called")):
            self.assertEqual(self.games("--no-verify", big)[0], 0)

    def test_a_card_without_room_is_refused_before_anything_is_written(self):
        big = self.source("Big.nsp", 700_000)
        usage = mock.Mock(free=700_000 + games.MARGIN - 1)
        with mock.patch.object(games.shutil, "disk_usage", lambda path: usage):
            code, _, err = self.games(big)
        self.assertEqual(code, 1)
        self.assertIn("is free on", err)
        self.assertFalse(self.folder.exists())

    @unittest.skipIf(sys.platform == "win32", "needs symbolic links")
    def test_a_games_folder_that_leads_off_the_card_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, self.folder)
        code, _, err = self.games(self.source("G.nsp", 10))
        self.assertEqual(code, 1)
        self.assertIn("a link on the card", err)
        self.assertEqual(list(outside.iterdir()), [])

    def test_an_interrupted_copy_leaves_no_file_under_the_real_name(self):
        big = self.source("Big.nsp", 700_000)
        real = games.write_part
        calls = []

        def stop_at_the_second_part(*args):  # type: ignore[no-untyped-def]
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            real(*args)

        with mock.patch.object(games, "write_part", stop_at_the_second_part):
            code, _, err = self.games(big)
        self.assertEqual(code, 130)
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(self.games(big)[0], 0)  # and the same command finishes the job

    def test_the_real_sizes(self):
        patch_free = {"LIMIT": 0xFFFFFFFF, "PART": 0xFFFF0000}
        with mock.patch.multiple(games, **patch_free):
            self.assertEqual(games.parts_of(0xFFFFFFFF), [0xFFFFFFFF])
            self.assertEqual(games.parts_of(0x100000000), [0xFFFF0000, 0x10000])
            self.assertEqual(games.parts_of(3 * 0xFFFF0000), [0xFFFF0000] * 3)
            self.assertEqual(games.part_names(11)[-2:], ["09", "10"])


if __name__ == "__main__":
    unittest.main()
