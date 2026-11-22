"""`autocheck`: what it runs on, what it says, and the launchd and systemd files it writes."""

from __future__ import annotations

import contextlib
import io
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import GOOD, card

from switchsd import autocheck, doctor, install
from switchsd.common import Failure


class AutocheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_one_notification_per_card(self):
        good = card(self.tmp / "a", **GOOD)
        bad_files = dict(GOOD)
        del bad_files["switch__daybreak.nro"]
        bad = card(self.tmp / "b", **bad_files)
        told: list[tuple[str, str]] = []
        # As on a card: without this the answer would hang on the file system the test folder lies on.
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(doctor, "filesystem", lambda root: "vfat"):
            self.assertEqual(autocheck.run_once([good, bad], lambda title, text: told.append((title, text))), 2)
        self.assertIn("Daybreak is missing", out.getvalue())
        self.assertEqual(len(told), 2)
        self.assertTrue(told[0][0].startswith("Switch card"))
        self.assertIn("Daybreak", told[1][1])

    def test_a_card_that_cannot_be_read_is_reported(self):
        good = card(self.tmp, **GOOD)
        told: list[tuple[str, str]] = []

        def refuse(root):  # type: ignore[no-untyped-def]
            raise Failure("macOS does not let this program read the card")

        with mock.patch.object(autocheck, "check", refuse), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(autocheck.run_once([good], lambda title, text: told.append((title, text))), 1)
        self.assertEqual(told, [("Switch card could not be checked", "macOS does not let this program read the card")])
        self.assertIn("not checked", out.getvalue())

    def test_card_detection(self):
        self.assertTrue(install.looks_like_card(card(self.tmp, **GOOD)))
        other = self.tmp / "usb stick"
        other.mkdir()
        (other / "payload.bin").write_bytes(b"x")
        self.assertFalse(install.looks_like_card(other))

    def test_looking_for_cards_works_for_a_user_id_without_an_account(self):
        def nobody() -> str:
            raise KeyError("getpwuid(): uid not found: 1000")

        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(doctor.getpass, "getuser", nobody):
            self.assertEqual(doctor.mounted_cards(), [])

    def test_launch_files(self):
        path, data = autocheck.mac_plist(self.tmp)
        plist = plistlib.loads(data)
        self.assertEqual(path, self.tmp / "Library/LaunchAgents/switchsd.autocheck.plist")
        self.assertTrue(plist["StartOnMount"])
        self.assertEqual(plist["ProgramArguments"][2:], [str(autocheck.SCRIPT), "autocheck", "run"])
        units = autocheck.linux_units(self.tmp)
        self.assertIn("PathChanged=/run/media/%u", units[self.tmp / ".config/systemd/user/switchsd-autocheck.path"])
        self.assertIn("autocheck run", units[self.tmp / ".config/systemd/user/switchsd-autocheck.service"])
        self.assertEqual(autocheck.unit_word('/home/u/My Projects/50% off/$x "y"/switchsd.py'),
                         '"/home/u/My Projects/50%% off/$$x \\"y\\"/switchsd.py"')

    def test_a_check_that_blows_up_still_tells_the_user(self):
        told: list[tuple[str, str]] = []
        out = io.StringIO()
        with mock.patch.object(autocheck, "check", side_effect=RuntimeError("boom \x1b[2J")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(autocheck.run_once([self.tmp], lambda title, text: told.append((title, text))), 1)
        self.assertEqual(told[0][0], "Switch card could not be checked")
        self.assertIn("RuntimeError: boom ?[2J", out.getvalue())

    def test_install_writes_files_without_loading(self):
        if sys.platform == "win32":
            with self.assertRaisesRegex(Failure, "Windows"):
                autocheck.install(self.tmp, load=False)
            return
        written = autocheck.install(self.tmp, load=False)
        self.assertTrue(written)
        self.assertTrue(all(p.is_file() for p in written))

    def test_a_dry_run_names_the_files_and_commands_and_touches_nothing(self):
        from switchsd import cli  # noqa: PLC0415

        def run(*args: str) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(autocheck.Path, "home", lambda: self.tmp), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err), \
                    mock.patch.object(autocheck.subprocess, "run", side_effect=AssertionError("nothing may run")), \
                    mock.patch.object(autocheck, "run", side_effect=AssertionError("nothing may run")):
                return cli.main(["autocheck", *args]), out.getvalue(), err.getvalue()

        self.assertEqual(run("run", "--dry-run")[0], 2)
        if sys.platform == "win32":
            self.assertEqual(run("install", "--dry-run")[0], 1)
            return
        code, out, _ = run("install", "--dry-run")
        self.assertEqual(code, 0)
        files, commands = autocheck.job(self.tmp)
        self.assertEqual(out.splitlines(), [f"would write {path}" for path in files]
                         + ["would run " + " ".join(command) for command in commands])
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(run("uninstall", "--dry-run")[1].strip(), "autocheck is not installed")
        written = autocheck.install(self.tmp, load=False)
        code, out, _ = run("uninstall", "--dry-run")
        self.assertEqual(out.splitlines(), [f"would stop the job and remove {path}" for path in written])
        self.assertTrue(all(path.is_file() for path in written))

    def test_applescript_quoting(self):
        self.assertEqual(autocheck.applescript('say "hi" \\ there'), '"say \\"hi\\" \\\\ there"')


if __name__ == "__main__":
    unittest.main()
