"""setup, check, list and lock without the network: synthetic releases in a temporary folder."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from support import GOOD, card, code_image, fuel_gauge_dump, run_tool, sysmodule_nsp

from switchsd import __version__, battery, common, components, doctor, install
from switchsd.cli import already_installed, next_steps
from switchsd.common import Failure
from switchsd.components import Component, Rule, load_lock, load_manifest, match_asset, safe_relpath, select
from switchsd.doctor import check
from switchsd.install import apply, build_plan, clean_legacy, remove_appledouble


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class ManifestTests(unittest.TestCase):
    def test_shipped_manifest_and_lock_agree(self):
        comps = load_manifest()
        lock = load_lock()
        self.assertEqual({c.id for c in comps}, set(lock))
        for comp in comps:
            names = list(lock[comp.id]["assets"])
            for rule in comp.files:
                if rule.content is None:
                    match_asset(rule, names, comp.id)
            for asset in lock[comp.id]["assets"].values():
                self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(asset["url"].startswith("https://"))

    def test_selection(self):
        comps = load_manifest()
        defaults = {c.id for c in select(comps, [], [], [])}
        self.assertIn("atmosphere", defaults)
        self.assertNotIn("saltynx", defaults)
        with_fps = {c.id for c in select(comps, ["fpslocker"], [], [])}
        self.assertIn("saltynx", with_fps)  # pulled in as a requirement
        self.assertEqual([c.id for c in select(comps, [], [], ["hekate", "atmosphere"])], ["atmosphere", "hekate"])
        both = [c.id for c in select(comps, ["checkpoint"], [], ["hekate"])]
        self.assertEqual(sorted(both), ["checkpoint", "hekate"])  # --with adds to --only as well
        with self.assertRaisesRegex(Failure, "unknown"):
            select(comps, ["nope"], [], [])
        with self.assertRaisesRegex(Failure, "needs saltynx"):
            select(comps, ["fpslocker"], ["saltynx"], [])

    def test_newest_release_honours_pre_release_only_projects(self):
        calls = []

        def fake_api(path):  # type: ignore[no-untyped-def]
            calls.append(path)
            return [{"tag_name": "beta9", "assets": []}] if "per_page" in path else {"tag_name": "1.0", "assets": []}

        stable = Component("a", "A", "core", True, "", "x/a", None, ())
        beta = Component("b", "B", "core", True, "", "x/b", None, (), prerelease=True)
        with mock.patch.object(components, "github_api", fake_api), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}):
            self.assertEqual(components.newest_tag(stable), "1.0")
            self.assertEqual(components.newest_tag(beta), "beta9")
        self.assertEqual(calls, ["repos/x/a/releases/latest", "repos/x/b/releases?per_page=1"])
        self.assertTrue(next(c for c in load_manifest() if c.id == "breeze").prerelease)

    def test_newest_tag_without_a_token_reads_github_com(self):
        stable = Component("a", "A", "core", True, "", "x/a", None, ())
        beta = Component("b", "B", "core", True, "", "x/b", None, (), prerelease=True)
        branch = Component("c", "C", "core", True, "", "x/c", "master", ())
        feeds = {
            "https://github.com/x/b/releases.atom":
                '<entry><link rel="alternate" type="text/html" href="https://github.com/x/b/releases/tag/beta%209"/>'
                '</entry><entry><link rel="alternate" href="https://github.com/x/b/releases/tag/beta8"/></entry>',
            "https://github.com/x/c/commits/master.atom": "<id>tag:github.com,2008:Grit::Commit/" + "ab" * 20 + "</id>",
        }
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(components, "web_text", feeds.__getitem__), \
                mock.patch.object(components, "web_redirect",
                                  lambda url: "https://github.com/x/a/releases/tag/12/02/2025"):
            self.assertEqual(components.newest_tag(stable), "12/02/2025")
            self.assertEqual(components.newest_tag(beta), "beta 9")
            self.assertEqual(components.newest_tag(branch), "ab" * 20)

    def test_verify_pin_compares_published_checksums(self):
        comp = Component("a", "A", "core", True, "", "x/a", None, ())
        pin = {"tag": "1.0", "assets": {"a.zip": {"url": "u", "sha256": "11" * 32},
                                         "b.nro": {"url": "u", "sha256": "22" * 32}}}
        release = {"assets": [{"name": "a.zip", "digest": "sha256:" + "11" * 32},
                              {"name": "b.nro", "digest": "sha256:" + "33" * 32}]}
        with mock.patch.object(components, "github_api", lambda path: release):
            problems, unchecked = components.verify_pin(comp, pin)
        self.assertEqual((len(problems), unchecked), (1, 0))
        self.assertIn("b.nro", problems[0])
        with mock.patch.object(components, "github_api", lambda path: {"assets": []}):
            self.assertEqual(len(components.verify_pin(comp, pin)[0]), 2)
        old_release = {"assets": [{"name": "a.zip"}, {"name": "b.nro", "digest": None}]}
        with mock.patch.object(components, "github_api", lambda path: old_release):
            self.assertEqual(components.verify_pin(comp, pin), ([], 2))  # nothing to compare with is not a match
        branch = Component("c", "C", "core", True, "", "x/c", "master", ())
        self.assertEqual(components.verify_pin(branch, {"tag": "ab", "assets": {"archive": {}}}), ([], 1))

    def test_a_pin_without_a_checksum_is_refused(self):
        comp = Component("a", "A", "core", True, "", "x/a", None, ())
        for sha in ("", "abc", None):
            with self.assertRaisesRegex(Failure, "no SHA-256"):
                components.fetch(comp, {"tag": "1.0", "assets": {"a.zip": {"url": "https://x.invalid", "sha256": sha}}})

    def test_the_token_is_not_passed_on_when_github_redirects(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token"}):
            api = components._request("https://api.github.com/repos/x/a")
            other = components._request("https://github.com/x/a/releases/latest")
        self.assertEqual(api.unredirected_hdrs, {"Authorization": "Bearer token"})
        self.assertNotIn("Authorization", api.headers)
        self.assertFalse(other.unredirected_hdrs)

    def test_supported_firmware_comes_from_the_release_s_own_source(self):
        comp = Component("ams", "A", "core", True, "", "x/ams", None, (), firmware="include/version.h")
        header = "#define ATMOSPHERE_RELEASE_VERSION_MAJOR 1\n" + "".join(
            f"#define ATMOSPHERE_SUPPORTED_HOS_VERSION_{name} {value}\n"
            for name, value in (("MAJOR", 23), ("MINOR", 5), ("MICRO", 0)))
        urls = []
        with mock.patch.object(components, "web_text", lambda url: urls.append(url) or header):
            self.assertEqual(components.supported_firmware(comp, "1.12.2"), "23.5.0")
        self.assertEqual(urls, ["https://raw.githubusercontent.com/x/ams/1.12.2/include/version.h"])
        with mock.patch.object(components, "web_text", lambda url: "nothing"), \
                self.assertRaisesRegex(Failure, "does not name"):
            components.supported_firmware(comp, "1.12.2")
        self.assertTrue(next(c for c in load_manifest() if c.id == "atmosphere").firmware)
        self.assertRegex(load_lock()["atmosphere"]["firmware"], r"^\d+\.\d+\.\d+$")

    def test_safe_paths(self):
        for good in ("switch/a.nro", "atmosphere/contents/x/flags/boot2.flag", ".overlays/x"):
            self.assertTrue(safe_relpath(good), good)
        for bad in ("", "/etc/passwd", "../x", "a/../b", "a//b", "C:/x", "a\\b", "./a",
                    "emuMMC./emummc.ini", "Nintendo /save", "switch/a.nro.", "switch/a\x1b[2J.nro"):
            self.assertFalse(safe_relpath(bad), bad)


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.card = self.base / "card"
        (self.card / "emuMMC").mkdir(parents=True)
        (self.card / "emuMMC" / "emummc.ini").write_text("[emummc]\nenabled=1\n")
        self.files: dict[str, dict[str, Path]] = {}

    def release(self, cid: str, assets: dict[str, bytes]) -> dict:
        folder = self.base / "cache" / cid
        folder.mkdir(parents=True, exist_ok=True)
        self.files[cid] = {}
        for name, data in assets.items():
            (folder / name).write_bytes(data)
            self.files[cid][name] = folder / name
        return {"tag": "v1", "assets": {name: {"url": "https://example.invalid/" + name,
                                               "sha256": hashlib.sha256(data).hexdigest()}
                                        for name, data in assets.items()}}

    def comps(self) -> tuple[list[Component], dict]:
        pins = {
            "core": self.release("core", {"core.zip": zip_bytes({
                "atmosphere/package3": b"pkg3", "switch/daybreak.nro": b"daybreak", "__MACOSX/._x": b"junk",
                "config/core/config.ini": b"setting=1"})}),
            "loader": self.release("loader", {"loader.zip": zip_bytes({"bootloader/update.bin": b"hekate",
                                                                       "loader.bin": b"hekate"})}),
            "amiibo": self.release("amiibo", {"amiibo.zip": zip_bytes({"SdOut/emuiibo/a.txt": b"a"})}),
            "app": self.release("app", {"App.nro": b"app"}),
        }
        comps = [
            Component("core", "Core", "core", True, "", "x/core", None,
                      (Rule("^core\\.zip$", unzip=""),)),
            Component("loader", "Loader", "core", True, "", "x/loader", None,
                      (Rule("^loader\\.zip$", unzip="", include="^bootloader/"),
                       Rule("^loader\\.zip$", member="^loader\\.bin$", to=("payload.bin",)))),
            Component("amiibo", "Amiibo", "utility", True, "", "x/amiibo", None,
                      (Rule("^amiibo\\.zip$", unzip="^SdOut/"),)),
            Component("app", "App", "installer", True, "", "x/app", None,
                      (Rule("^App\\.nro$", to=("switch/App/App.nro",)),)),
        ]
        return comps, pins

    def setup_card(self, **kw):  # type: ignore[no-untyped-def]
        comps, pins = self.comps()
        plan = build_plan(comps, pins, self.files)
        return apply(self.card, plan, comps, pins, reset=kw.get("reset"), prune=kw.get("prune", False),
                     dry_run=kw.get("dry_run", False)), plan

    def test_files_land_where_they_belong(self):
        report, _ = self.setup_card()
        self.assertEqual((self.card / "payload.bin").read_bytes(), b"hekate")
        self.assertEqual((self.card / "bootloader/update.bin").read_bytes(), b"hekate")
        self.assertFalse((self.card / "loader.bin").exists())
        self.assertEqual((self.card / "emuiibo/a.txt").read_bytes(), b"a")
        self.assertEqual((self.card / "switch/App/App.nro").read_bytes(), b"app")
        self.assertFalse((self.card / "__MACOSX").exists())
        self.assertTrue((self.card / "bootloader/hekate_ipl.ini").is_file())  # template
        self.assertTrue((self.card / "games/README.txt").is_file())
        self.assertEqual((self.card / "emuMMC/emummc.ini").read_text(), "[emummc]\nenabled=1\n")
        record = json.loads((self.card / install.RECORD).read_text())
        self.assertEqual(set(record["components"]), {"core", "loader", "amiibo", "app"})
        self.assertIn("payload.bin", record["components"]["loader"]["files"])
        self.assertTrue(report.written)

    def test_second_run_changes_nothing_and_settings_survive(self):
        self.setup_card()
        (self.card / "config/core/config.ini").write_bytes(b"setting=mine")
        (self.card / "bootloader/hekate_ipl.ini").write_text("[config]\nmine=1\n")
        report, _ = self.setup_card()
        self.assertEqual(report.written, [])
        self.assertEqual((self.card / "config/core/config.ini").read_bytes(), b"setting=mine")
        self.assertIn("bootloader/hekate_ipl.ini", report.kept)

    def test_reset_restores_a_template_and_backs_up_the_old_one(self):
        self.setup_card()
        (self.card / "bootloader/hekate_ipl.ini").write_text("[mine]\n")
        report, _ = self.setup_card(reset={"bootloader/hekate_ipl.ini"})
        self.assertIn("[CFW (emuMMC)]", (self.card / "bootloader/hekate_ipl.ini").read_text())
        self.assertEqual(len(report.backed_up), 1)
        self.assertEqual((self.card / report.backed_up[0]).read_text(), "[mine]\n")

    def test_files_a_new_version_dropped_are_removed_unless_changed(self):
        self.setup_card()
        record_path = self.card / install.RECORD
        record = json.loads(record_path.read_text())
        (self.card / "switch/old.nro").write_bytes(b"old")
        (self.card / "switch/edited.nro").write_bytes(b"edited by the user")
        record["components"]["app"]["files"]["switch/old.nro"] = hashlib.sha256(b"old").hexdigest()
        record["components"]["app"]["files"]["switch/edited.nro"] = hashlib.sha256(b"as installed").hexdigest()
        record_path.write_text(json.dumps(record))
        report, _ = self.setup_card()
        self.assertEqual(report.removed, ["switch/old.nro"])
        self.assertEqual(report.left, ["switch/edited.nro"])
        self.assertTrue((self.card / "switch/edited.nro").exists())

    def test_folders_left_empty_by_a_removal_go_too(self):
        self.setup_card()
        record_path = self.card / install.RECORD
        record = json.loads(record_path.read_text())
        gone = self.card / "atmosphere/contents/420000000000000F"
        gone.mkdir(parents=True)
        (gone / "exefs.nsp").write_bytes(b"old module")
        (gone / "._exefs.nsp").write_bytes(b"appledouble")
        other = self.card / "atmosphere/contents/4200000000000001/exefs.nsp"  # someone else's module stays
        other.parent.mkdir()
        other.write_bytes(b"not ours")
        shared = self.card / "switch/App/old.dat"
        shared.write_bytes(b"old data")
        files = record["components"]["app"]["files"]
        files["atmosphere/contents/420000000000000F/exefs.nsp"] = hashlib.sha256(b"old module").hexdigest()
        files["switch/App/old.dat"] = hashlib.sha256(b"old data").hexdigest()
        record_path.write_text(json.dumps(record))
        self.setup_card()
        self.assertFalse(gone.exists())
        self.assertTrue(other.is_file())
        self.assertTrue((self.card / "switch/App/App.nro").is_file())  # the folder still holds the app

    def test_dry_run_writes_nothing(self):
        report, _ = self.setup_card(dry_run=True)
        self.assertTrue(report.written)
        self.assertFalse((self.card / "payload.bin").exists())

    def test_a_record_someone_else_wrote_is_read_with_care(self):
        record = self.card / install.RECORD
        record.parent.mkdir(parents=True)
        for text in ("[]", '{"components": []}', '{"components": {"core": "x"}}',
                     '{"components": {"core": {"files": 1}}}', "not json"):
            record.write_text(text)
            with self.assertRaisesRegex(Failure, "is unreadable"):
                self.setup_card()
        outside = self.base / "outside.txt"
        outside.write_bytes(b"keep me")
        digest = hashlib.sha256(b"keep me").hexdigest()
        record.write_text(json.dumps({"components": {"core": {"version": "v0", "files": {
            "../outside.txt": digest, "/etc/hosts": digest, "emuMMC/emummc.ini": digest}}}}))
        report, _ = self.setup_card(prune=True)
        self.assertEqual(report.removed, [])
        self.assertEqual(outside.read_bytes(), b"keep me")
        self.assertTrue((self.card / "emuMMC/emummc.ini").is_file())

    def test_conflicts_protected_paths_and_zip_slip_are_refused(self):
        comps, pins = self.comps()
        clash = Component("clash", "Clash", "installer", True, "", "x/clash", None,
                          (Rule("^App\\.nro$", to=("switch/App/App.nro",)),))
        pins["clash"] = self.release("clash", {"App.nro": b"other"})
        with self.assertRaisesRegex(Failure, "both install"):
            build_plan([*comps, clash], pins, self.files)
        evil = Component("evil", "Evil", "installer", True, "", "x/evil", None, (Rule("^e\\.zip$", unzip=""),))
        pins["evil"] = self.release("evil", {"e.zip": zip_bytes({"../escape": b"x"})})
        with self.assertRaisesRegex(Failure, "unsafe path"):
            build_plan([evil], pins, self.files)
        nand = Component("nand", "N", "installer", True, "", "x/nand", None, (Rule("^n\\.nro$", to=("Nintendo/x",)),))
        pins["nand"] = self.release("nand", {"n.nro": b"x"})
        with self.assertRaisesRegex(Failure, "belongs to the console"):
            build_plan([nand], pins, self.files)

    def test_what_is_installed_is_what_was_pinned(self):
        comps, pins = self.comps()
        self.files["app"]["App.nro"].write_bytes(b"swapped after the download was checked")
        with self.assertRaisesRegex(Failure, "app: App.nro in the download cache no longer matches"):
            build_plan(comps, pins, self.files)
        broken = Component("broken", "Broken", "installer", True, "", "x/broken", None, (Rule("^b\\.zip$", unzip=""),))
        pins["broken"] = self.release("broken", {"b.zip": b"not an archive"})
        with self.assertRaisesRegex(Failure, "broken: b.zip is not a zip archive"):
            build_plan([broken], pins, self.files)

    def test_content_and_keep_rules(self):
        comps, pins = self.comps()
        bot = Component("bot", "Bot", "remote", True, "", "x/bot", None, (
            Rule("^bot\\.zip$", unzip="", exclude="/flags/boot2\\.flag$"),
            Rule("", content="{}\n", to=("atmosphere/contents/430000000000000B/toolbox.json",))))
        wine = Component("wine", "Wine", "games", True, "", "x/wine", None,
                         (Rule("^w\\.zip$", unzip="", keep="^switch/wine/"),))
        pins["bot"] = self.release("bot", {"bot.zip": zip_bytes({
            "atmosphere/contents/430000000000000B/exefs.nsp": b"bot",
            "atmosphere/contents/430000000000000B/flags/boot2.flag": b""})})
        pins["wine"] = self.release("wine", {"w.zip": zip_bytes({"switch/wine/config/settings.json": b"default"})})
        every = [*comps, bot, wine]
        apply(self.card, build_plan(every, pins, self.files), every, pins, reset=None, prune=False, dry_run=False)
        base = self.card / "atmosphere/contents/430000000000000B"
        self.assertTrue((base / "exefs.nsp").is_file())
        self.assertFalse((base / "flags/boot2.flag").exists())  # installed switched off
        self.assertEqual((base / "toolbox.json").read_text(), "{}\n")
        (self.card / "switch/wine/config/settings.json").write_bytes(b"mine")
        report = apply(self.card, build_plan(every, pins, self.files), every, pins, reset=None, prune=False,
                       dry_run=False)
        self.assertEqual((self.card / "switch/wine/config/settings.json").read_bytes(), b"mine")
        self.assertIn("switch/wine/config/settings.json", report.kept)

    def test_optional_components_on_the_card_stay_selected(self):
        comps, pins = self.comps()
        extra = Component("extra", "Extra", "utility", False, "", "x/extra", None,
                          (Rule("^e\\.nro$", to=("switch/extra/e.nro",)),))
        pins["extra"] = self.release("extra", {"e.nro": b"e"})
        every = [*comps, extra]
        apply(self.card, build_plan(every, pins, self.files), every, pins, reset=None, prune=False, dry_run=False)
        args = argparse.Namespace(with_ids=[], without_ids=[], only_ids=[])
        self.assertEqual(already_installed(self.card, every, args), ["extra"])
        args.without_ids = ["extra"]
        self.assertEqual(already_installed(self.card, every, args), [])
        args = argparse.Namespace(with_ids=[], without_ids=[], only_ids=["core"])
        self.assertEqual(already_installed(self.card, every, args), [])

    def test_summary_does_not_list_hundreds_of_kept_files(self):
        report = install.Report(kept=[f"switch/wine/file{i}" for i in range(200)])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            install.summarize(report, {}, dry_run=False)
        self.assertIn("kept 200 files", err.getvalue())
        self.assertIn("and 194 more", err.getvalue())
        self.assertLess(len(err.getvalue()), 500)

    def test_legacy_clean_and_appledouble(self):
        (self.card / "themes").mkdir()
        (self.card / "themes/AB.ips").write_bytes(b"patch")
        (self.card / "config/prodinforge").mkdir(parents=True)
        (self.card / "config/prodinforge/donor.keys").write_bytes(b"k")
        (self.card / "HATS_VERSION.txt").write_text("x")
        (self.card / "._payload.bin").write_bytes(b"ad")
        removed = clean_legacy(self.card, dry_run=False)
        self.assertEqual(len(removed), 3)
        self.assertFalse((self.card / "config/prodinforge").exists())
        self.assertFalse((self.card / "themes/AB.ips").exists())
        for junk in ("emuMMC/._emummc.ini", "JKSV/Game/._save", "Nintendo/._x"):
            (self.card / junk).parent.mkdir(parents=True, exist_ok=True)
            (self.card / junk).write_bytes(b"ad")
        self.assertEqual(remove_appledouble(self.card), 1)
        self.assertTrue((self.card / "JKSV/Game/._save").exists())  # the console's own folders stay as they are

    def test_the_record_removes_nothing_that_this_run_installs(self):
        comps, pins = self.comps()
        plan = build_plan(comps, pins, self.files)
        record = self.card / install.RECORD
        record.parent.mkdir(parents=True)
        theirs = {"payload.bin": plan["payload.bin"].sha256, "exosphere.ini": plan["exosphere.ini"].sha256}
        record.write_text(json.dumps({"components": {"gone": {"version": "v0", "files": theirs}}}))
        report = apply(self.card, plan, comps, pins, reset=None, prune=True, dry_run=False)
        self.assertEqual(report.removed, [])
        self.assertEqual((self.card / "payload.bin").read_bytes(), b"hekate")
        self.assertTrue((self.card / "exosphere.ini").is_file())

    @unittest.skipIf(sys.platform == "win32", "making links needs a privilege on Windows")
    def test_links_on_the_card_are_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep me")
        (self.card / "switch").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Failure, "leads its folder somewhere else"):
            self.setup_card()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["keep.txt"])
        self.assertFalse((self.card / "payload.bin").exists())  # refused before anything was written
        (self.card / "switch").unlink()
        (self.card / "payload.bin").symlink_to(outside / "keep.txt")
        with self.assertRaisesRegex(Failure, "payload.bin is a link"):
            self.setup_card()
        (self.card / "payload.bin").unlink()
        (self.card / "payload.bin.switchsd-tmp").symlink_to(outside / "keep.txt")
        self.setup_card()
        self.assertEqual((outside / "keep.txt").read_bytes(), b"keep me")
        self.assertEqual((self.card / "payload.bin").read_bytes(), b"hekate")

    def comps_v2(self) -> tuple[list[Component], dict]:
        """The same components a version later: package3 and the app change, one file is new and
        daybreak is no longer shipped."""
        comps, pins = self.comps()
        pins["core"] = self.release("core", {"core.zip": zip_bytes({
            "atmosphere/package3": b"pkg3 v2", "switch/new.nro": b"new", "config/core/config.ini": b"setting=2"})})
        pins["core"]["tag"] = "v2"
        pins["app"] = self.release("app", {"App.nro": b"app v2"})
        pins["app"]["tag"] = "v2"
        return comps, pins

    def update(self, comps: list[Component], pins: dict, **kw) -> install.Report:  # type: ignore[no-untyped-def]
        return apply(self.card, build_plan(comps, pins, self.files), comps, pins, reset=kw.get("reset"),
                     prune=False, dry_run=kw.get("dry_run", False))

    def snapshot(self) -> dict[str, bytes]:
        """Every file on the card but the backups."""
        return {p.relative_to(self.card).as_posix(): p.read_bytes() for p in sorted(self.card.rglob("*"))
                if p.is_file() and not p.relative_to(self.card).as_posix().startswith(install.BACKUPS)}

    def test_a_release_changed_after_pinning_leaves_out_that_component_and_what_needs_it(self):
        from switchsd import cli  # noqa: PLC0415
        comps, pins = self.comps()
        comps.append(Component("needs", "Needs", "utility", True, "", "x/needs", None,
                               (Rule("^Needs\\.nro$", to=("switch/Needs/Needs.nro",)),), requires=("app",)))
        pins["needs"] = self.release("needs", {"Needs.nro": b"needs"})
        changed: set[str] = set()

        def fetch(comp, pin):  # type: ignore[no-untyped-def]
            if comp.id in changed:
                raise components.Changed(f"https://example.invalid/{comp.id} is not the file that was pinned")
            return self.files[comp.id]

        def setup(*args: str) -> tuple[int, str]:
            out = io.StringIO()
            with mock.patch.object(cli, "load_manifest", lambda: comps), \
                    mock.patch.object(cli, "load_lock", lambda: pins), mock.patch.object(cli, "fetch", fetch), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                return cli.main(["setup", str(self.card), *args]), out.getvalue()

        self.assertEqual(setup()[0], 0)
        pins["core"] = self.release("core", {"core.zip": zip_bytes({
            "atmosphere/package3": b"pkg3 v2", "switch/daybreak.nro": b"daybreak",
            "config/core/config.ini": b"setting=1"})})
        pins["app"] = self.release("app", {"App.nro": b"app v2"})
        pins["core"]["tag"] = pins["app"]["tag"] = pins["needs"]["tag"] = "v2"
        changed.add("app")
        code, text = setup("--dry-run")
        self.assertEqual(code, 3)
        self.assertIn("Left out, because nothing unchecked goes on a card", text)
        code, text = setup("--prune")
        self.assertEqual(code, 3, text)
        self.assertIn("left out: https://example.invalid/app is not the file that was pinned", text)
        self.assertIn("app stays as it is on the card: its project changed the release on GitHub after it was "
                      "pinned", text)
        self.assertIn("needs stays as it is on the card: it needs app, which is left out", text)
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"pkg3 v2")  # the rest is updated
        self.assertEqual((self.card / "switch/App/App.nro").read_bytes(), b"app")  # even --prune leaves these
        self.assertEqual((self.card / "switch/Needs/Needs.nro").read_bytes(), b"needs")
        record = json.loads((self.card / install.RECORD).read_text())["components"]
        self.assertEqual([record[c]["version"] for c in ("core", "app", "needs")], ["v2", "v1", "v1"])
        self.assertEqual(set(record["app"]["files"]), {"switch/App/App.nro"})

        before = self.snapshot()
        changed.add("core")  # what a card needs to start stops the run instead, before anything changes
        code, text = setup()
        self.assertEqual(code, 3)
        self.assertIn("core v2: https://example.invalid/core is not the file that was pinned. Its project changed "
                      "the release after it was pinned, and a card needs core to start CFW, so nothing was changed",
                      text)
        self.assertEqual(self.snapshot(), before)

    def test_an_update_is_copied_first_and_undo_puts_the_card_back(self):
        self.setup_card()
        (self.card / "config/core/config.ini").write_bytes(b"setting=mine")
        before = self.snapshot()
        comps, pins = self.comps_v2()
        plan = build_plan(comps, pins, self.files)
        report = apply(self.card, plan, comps, pins, reset=None, prune=False, dry_run=False)
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"pkg3 v2")
        self.assertFalse((self.card / "switch/daybreak.nro").exists())
        journal = json.loads((self.card / f"{report.backup}.json").read_text())
        self.assertEqual(sorted(journal["created"]), ["switch/new.nro"])
        self.assertEqual(sorted(journal["replaced"]),
                         sorted([install.RECORD, "atmosphere/package3", "switch/App/App.nro"]))
        self.assertEqual(sorted(journal["removed"]), ["switch/daybreak.nro"])
        self.assertEqual((self.card / report.backup / "atmosphere/package3").read_bytes(), b"pkg3")
        self.assertEqual((self.card / report.backup / "switch/daybreak.nro").read_bytes(), b"daybreak")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            install.summarize(report, plan, False)
        self.assertIn("switchsd.py undo", err.getvalue())

        done = install.undo(self.card, dry_run=True)
        self.assertEqual((len(done.restored), len(done.deleted)), (4, 1))
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"pkg3 v2")  # a dry run changes nothing
        done = install.undo(self.card, dry_run=False)
        self.assertEqual(done.left, [])
        self.assertEqual(self.snapshot(), before)  # the settings file was never touched, and is still the user's
        self.assertTrue((self.card / f"{report.backup}.undone.json").is_file())
        self.assertFalse((self.card / f"{report.backup}.json").exists())

        install.undo(self.card, dry_run=False)  # one further back: the first setup, which added everything
        self.assertFalse((self.card / "payload.bin").exists())
        self.assertFalse((self.card / "switch").exists())
        self.assertTrue((self.card / "config/core/config.ini").is_file())  # changed since: left alone
        self.assertEqual((self.card / "emuMMC/emummc.ini").read_text(), "[emummc]\nenabled=1\n")
        with self.assertRaisesRegex(Failure, "nothing to undo"):
            install.undo(self.card, dry_run=False)

    def test_undo_leaves_what_changed_after_the_update(self):
        self.setup_card()
        self.update(*self.comps_v2())
        (self.card / "atmosphere/package3").write_bytes(b"put there by hand")
        (self.card / "switch/new.nro").write_bytes(b"also by hand")
        done = install.undo(self.card, dry_run=False)
        self.assertEqual(sorted(done.left), ["atmosphere/package3", "switch/new.nro"])
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"put there by hand")
        self.assertEqual((self.card / "switch/App/App.nro").read_bytes(), b"app")
        self.assertEqual((self.card / "switch/daybreak.nro").read_bytes(), b"daybreak")

    def test_undo_checks_every_copy_and_the_list_before_changing_anything(self):
        self.setup_card()
        report = self.update(*self.comps_v2())
        copy = self.card / report.backup / "switch/App/App.nro"
        copy.write_bytes(b"damaged")
        with self.assertRaisesRegex(Failure, "App.nro .* is missing or damaged; nothing was changed"):
            install.undo(self.card, dry_run=False)
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"pkg3 v2")
        copy.write_bytes(b"app")
        journal = self.card / f"{report.backup}.json"
        good = journal.read_text()
        for bad in ('{"created": {"emuMMC/emummc.ini": "' + "0" * 64 + '"}, "replaced": {}, "removed": {}}',
                    '{"created": {"../outside": "' + "0" * 64 + '"}, "replaced": {}, "removed": {}}',
                    '{"created": {}, "replaced": {"payload.bin": ["x", "y"]}, "removed": {}}', "[]", "not json"):
            journal.write_text(bad)
            with self.assertRaisesRegex(Failure, "nothing was changed"):
                install.undo(self.card, dry_run=False)
        journal.write_text(good)
        install.undo(self.card, dry_run=False)
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"pkg3")

    def test_only_the_newest_backups_stay_and_settings_copies_are_never_dropped(self):
        self.setup_card()
        (self.card / "bootloader/hekate_ipl.ini").write_text("[mine]\n")
        first = self.update(*self.comps(), reset={"bootloader/hekate_ipl.ini"})
        for _ in range(3):
            self.update(*self.comps_v2())
            self.update(*self.comps())
        self.assertEqual(len(install.backup_sets(self.card)), install.KEEP_BACKUPS)
        self.assertEqual((self.card / first.backed_up[0]).read_text(), "[mine]\n")
        self.assertFalse((self.card / f"{first.backup}.json").exists())

    def test_a_run_that_changes_nothing_or_only_looks_leaves_no_backup(self):
        self.setup_card(dry_run=True)
        self.assertFalse((self.card / install.BACKUPS).exists())
        self.setup_card()
        self.setup_card()
        self.assertEqual(len(install.backup_sets(self.card)), 1)
        self.update(*self.comps_v2(), dry_run=True)
        self.assertEqual(len(install.backup_sets(self.card)), 1)

    def test_room_for_the_copies_is_checked_before_anything_changes(self):
        self.setup_card()
        full = mock.Mock(return_value=mock.Mock(free=1 << 20))
        with mock.patch.object(install.shutil, "disk_usage", full), \
                self.assertRaisesRegex(Failure, "copies of the ones they replace.*nothing was changed"):
            self.update(*self.comps_v2())
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"pkg3")
        self.assertEqual(len(install.backup_sets(self.card)), 1)

    def test_a_release_that_writes_outside_the_known_folders_needs_a_look_first(self):
        comps, pins = self.comps()
        two = Component("two", "Two", "installer", True, "", "x/two", None, (Rule("^t\\.zip$", unzip=""),))
        pins["two"] = self.release("two", {"t.zip": zip_bytes({"switch/t/t.nro": b"t", "nx2/switch/t/t.nro": b"2"})})
        with self.assertRaisesRegex(Failure, "two v1 would write nx2/switch/t/t.nro, outside the folders"):
            build_plan([*comps, two], pins, self.files)
        two = Component("two", "Two", "installer", True, "", "x/two", None,
                        (Rule("^t\\.zip$", unzip="", exclude="^nx2/"),))
        self.assertIn("switch/t/t.nro", build_plan([*comps, two], pins, self.files))

    def test_legacy_clean_is_undone_too(self):
        self.setup_card()
        (self.card / "config/prodinforge").mkdir(parents=True)
        (self.card / "config/prodinforge/donor.keys").write_bytes(b"k")
        (self.card / "HATS_VERSION.txt").write_text("x")
        comps, pins = self.comps()
        journal = install.Journal(self.card, install.Links(self.card))
        self.assertEqual(len(clean_legacy(self.card, False, journal)), 2)
        apply(self.card, build_plan(comps, pins, self.files), comps, pins, reset=None, prune=False, dry_run=False,
              journal=journal)
        self.assertFalse((self.card / "HATS_VERSION.txt").exists())
        install.undo(self.card, dry_run=False)
        self.assertEqual((self.card / "config/prodinforge/donor.keys").read_bytes(), b"k")
        self.assertEqual((self.card / "HATS_VERSION.txt").read_text(), "x")

    @unittest.skipIf(sys.platform == "win32", "making links needs a privilege on Windows")
    def test_a_backup_folder_that_leads_off_the_card_is_refused(self):
        self.setup_card()
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "20200101-000000.json").write_text("{}")  # looks like an old backup's list
        backups = self.card / install.BACKUPS
        shutil.rmtree(backups)
        backups.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Failure, "somewhere else"):
            self.update(*self.comps_v2())
        with self.assertRaisesRegex(Failure, "somewhere else"):
            install.undo(self.card, dry_run=False)
        self.assertEqual([p.name for p in outside.iterdir()], ["20200101-000000.json"])
        self.assertEqual((self.card / "atmosphere/package3").read_bytes(), b"pkg3")

    def test_the_shipped_sphaira_settings_allow_installing_to_the_emummc_only(self):
        text = (install.TEMPLATES / "config/sphaira/config.ini").read_text()
        self.assertIn("install_emummc=1", text)
        self.assertNotIn("install_sysmmc", text)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def levels(self, root: Path) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for level, text in check(root):
            out.setdefault(level, []).append(text)
        return out

    def test_good_card(self):
        found = self.levels(card(self.tmp, **GOOD))
        self.assertNotIn("FAIL", found)
        # The temporary folder is not on FAT, which check rightly points out.
        warnings = [t for t in found.get("WARN", []) if not t.startswith("file system is")]
        self.assertEqual(warnings, [])
        self.assertTrue(any("hekate 6.5.5" in t for t in found["OK"]))

    def test_breeze_s_spare_menus_are_named_not_warned_about(self):
        root = card(self.tmp, **GOOD)
        for spare in ("switch/breeze/hbmenu.nro", "switch/breeze/reeze/hbmenu.nro"):
            (root / spare).parent.mkdir(parents=True, exist_ok=True)
            (root / spare).write_bytes(code_image(None))
        found = self.levels(root)
        self.assertFalse(any("ABI" in t for t in found.get("WARN", [])))
        self.assertTrue(any("apart from Breeze's spare homebrew menus" in t for t in found["OK"]))

    def test_tico_without_emulators_is_pointed_out(self):
        root = card(self.tmp, **GOOD)
        (root / "switch/tico").mkdir(parents=True)
        (root / "switch/tico/tico.nro").write_bytes(b"NRO")
        self.assertTrue(any("tico has no emulators yet" in t for t in self.levels(root)["WARN"]))
        for name in ("snes9x", "dolphin"):
            (root / "tico/modules" / name).mkdir(parents=True)
        found = self.levels(root)
        self.assertTrue(any(t.startswith("tico: 2 emulators") for t in found["OK"]))
        self.assertFalse(any("tico" in t for t in found.get("WARN", [])))

    def test_problems_are_found(self):
        bad = dict(GOOD)
        del bad["payload.bin"], bad["switch__daybreak.nro"]
        bad["emuMMC__RAW1__raw_based"] = struct.pack("<I", 0x2368000)
        bad["bootloader__hekate_ipl.ini"] += b"[CFW (SYSMMC)]\npkg3=atmosphere/package3\nemummc_force_disable=1\n"
        bad["themes__AB.ips"] = b"p"
        bad["config__prodinforge__donor.keys"] = b"k"
        bad["switch__._x.nro"] = b"ad"
        found = self.levels(card(self.tmp, **bad))
        fails, warns = " | ".join(found["FAIL"]), " | ".join(found["WARN"])
        self.assertIn("payload.bin is missing", fails)
        self.assertIn("raw_based says 0x2368000", fails)
        for text in ("own NAND", "Daybreak", "systemPatches", "another console", "._"):
            self.assertIn(text, warns)

    def test_remote_control_and_custom_kernels_are_flagged(self):
        files = dict(GOOD)
        files["atmosphere__contents__430000000000000B__flags__boot2.flag"] = b""
        files["atmosphere__contents__00FF0000A53BB665__flags__boot2.flag"] = b""
        files["bootloader__hekate_ipl.ini"] += (b"[Autorun]\npkg3=atmosphere/package3\nemummcforce=1\n"
                                                b"kernel=atmosphere/mesosphere.bin\n")
        warns = " | ".join(self.levels(card(self.tmp, **files)).get("WARN", []))
        self.assertIn("sys-botbase starts at boot, so anyone on the network can control the console", warns)
        self.assertIn("SysDVR starts at boot", warns)
        self.assertIn("its own kernel", warns)

    def test_firmware_is_compared_with_what_atmosphere_supports(self):
        pins = {"atmosphere": {"tag": "1.12.2", "firmware": "23.5.0", "assets": {}}}

        def found(firmware: bytes, atmosphere: bytes) -> dict[str, list[str]]:
            files = dict(GOOD)
            files["config__sys-patch__log.ini"] = (b"[stats]\nfw_version=" + firmware + b"\nams_version="
                                                   + atmosphere + b"\nis_emummc=1\n")
            with mock.patch.object(doctor, "load_lock", lambda: pins):
                return self.levels(card(self.tmp / (firmware + atmosphere).decode(), **files))

        behind = found(b"22.5.0", b"1.12.2")
        self.assertTrue(any("runs up to 23.5.0; updating from 22.5.0 is optional" in t for t in behind["OK"]))
        self.assertFalse(any("runs up to" in t for t in found(b"23.5.0", b"1.12.2")["OK"]))
        self.assertFalse(any("runs up to" in t for t in found(b"22.5.0", b"1.11.0")["OK"]))  # not the pinned release
        ahead = " | ".join(found(b"24.0.0", b"1.12.2")["WARN"])
        self.assertIn("firmware 24.0.0 is newer than Atmosphère 1.12.2 supports (23.5.0)", ahead)

    @unittest.skipIf(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "needs file permissions that the user cannot bypass")
    def test_an_unreadable_card_is_an_error_not_a_list_of_false_alarms(self):
        root = card(self.tmp, **GOOD)
        for rel, is_dir in (("bootloader/hekate_ipl.ini", False), ("themes", True)):
            path = root / rel
            if is_dir:
                path.mkdir()
            path.chmod(0)
            self.addCleanup(path.chmod, 0o700)
            with self.assertRaisesRegex(Failure, "read"):
                check(root)
            path.chmod(0o700)
        result_ok = check(root)
        self.assertFalse([t for level, t in result_ok if level == "FAIL"])

    def test_an_emummc_folder_off_the_card_is_a_finding_and_is_not_read(self):
        files = dict(GOOD)
        files["emuMMC__emummc.ini"] = b"[emummc]\nenabled=1\nsector=0x1b9e0000\npath=/etc\n"
        fails = " | ".join(self.levels(card(self.tmp / "a", **files))["FAIL"])
        self.assertIn("etc/raw_based says nothing", fails)  # read as a folder on the card, as hekate would
        files["emuMMC__emummc.ini"] = b"[emummc]\nenabled=1\nsector=0x1b9e0000\npath=../../etc\n"
        fails = " | ".join(self.levels(card(self.tmp / "b", **files))["FAIL"])
        self.assertIn("names the folder '../../etc', which is not on the card", fails)

    def test_junk_in_the_consoles_own_folders_is_not_counted_as_removable(self):
        files = dict(GOOD)
        files["Nintendo__save__._x"] = b"ad"
        self.assertNotIn("._", " | ".join(self.levels(card(self.tmp / "a", **files)).get("WARN", [])))
        files["switch__._x.nro"] = b"ad"
        self.assertIn("1 macOS ._", " | ".join(self.levels(card(self.tmp / "b", **files))["WARN"]))

    def test_a_broken_emummc_ini_is_a_finding_not_a_crash(self):
        files = dict(GOOD)
        files["emuMMC__emummc.ini"] = b"[emummc]\nenabled=1\nsector=banana\n"
        fails = " | ".join(self.levels(card(self.tmp, **files)).get("FAIL", []))
        self.assertIn("no usable sector", fails)

    def test_old_abi_programs_are_flagged_on_firmware_21_and_newer(self):
        files = dict(GOOD)
        files["switch__new__new.nro"] = code_image(1)
        files["switch__old__old.nro"] = code_image(None)
        files["switch__.overlays__old.ovl"] = code_image(None)
        files["atmosphere__contents__4200000000000001__exefs.nsp"] = sysmodule_nsp(code_image(None), True)
        files["switch__wine__drive_c__game__deep__setup.nro"] = code_image(None)  # too deep to be an app
        log = b"[fs]\nnocntchk=Patched (sys-patch)\n[stats]\nfw_version=%s\nams_version=1.12.2\nis_emummc=1\n"
        files["config__sys-patch__log.ini"] = log % b"22.5.0"
        found = self.levels(card(self.tmp / "a", **files))
        warns = " | ".join(found.get("WARN", []))
        # 7 = the three in GOOD (hbmenu, Daybreak, sys-patch) and the four added here that sit where apps live
        self.assertIn("3 of 7 programs are built for the homebrew ABI from before firmware 21", warns)
        for name in ("switch/old/old.nro", "switch/.overlays/old.ovl", "4200000000000001/exefs.nsp"):
            self.assertIn(name, warns)
        self.assertNotIn("setup.nro", warns)
        self.assertTrue(any("firmware 22.5.0, Atmosphère 1.12.2" in t for t in found["OK"]))

        files["config__sys-patch__log.ini"] = log % b"20.5.0"
        found = self.levels(card(self.tmp / "b", **files))
        self.assertNotIn("ABI", " | ".join(found.get("WARN", [])))
        self.assertTrue(any("fine on 20.5.0" in t for t in found["OK"]))

    def test_patches_sys_patch_could_not_apply_are_reported(self):
        files = dict(GOOD)
        files["config__sys-patch__log.ini"] = (b"[fs]\nnoncasigchk_17.0.0+=Unpatched\nnocntchk=Skipped\n"
                                               b"[stats]\nfw_version=24.0.0\nams_version=1.12.2\nis_emummc=1\n")
        warns = " | ".join(self.levels(card(self.tmp, **files)).get("WARN", []))
        self.assertIn("sys-patch could not apply fs/noncasigchk_17.0.0+", warns)

    def test_crash_reports_are_counted(self):
        root = card(self.tmp, **GOOD)
        self.assertIn("Atmosphère has left no crash reports on the card", self.levels(root)["OK"])
        for name in ("crash_reports/01700000000_0100000000001000.log", "fatal_errors/report_0000000012345678.bin",
                     "fatal_reports/01700000000_0100000000001000.log",
                     "crash_reports/dumps/01700000000_0100000000001000.bin"):  # the dump belongs to the first report
            path = root / "atmosphere" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"report")
        warned = self.levels(root)["WARN"]
        pattern = r"Atmosphère has left 3 crash reports on the card, the newest from 20\d\d-\d\d-\d\d\. One now"
        self.assertTrue([t for t in warned if re.match(pattern, t)], warned)
        mirrored = [text for _, text in check(root, on_card=False)]  # a copy made over Wi-Fi carries no dates
        self.assertTrue([t for t in mirrored if t.startswith("Atmosphère has left 3 crash reports on the card. One")])

    def test_the_battery_is_read_from_hekates_fuel_gauge_dump(self):
        gauge = battery.parse(fuel_gauge_dump(3600, 3315))
        self.assertEqual((gauge.designed, gauge.full, round(gauge.health, 1), gauge.cycles, round(gauge.charge, 1),
                          round(gauge.celsius, 1), gauge.millivolts), (3600, 3315, 92.1, 123.4, 81.5, 32.4, 4000))
        self.assertEqual(battery.parse(fuel_gauge_dump(3600, 3315, celsius=-5)).celsius, -5)
        for no_dump in (b"short", bytes(512), b"\xff" * 512, fuel_gauge_dump(1000, 900)):  # 1000 mAh: not configured
            self.assertIsNone(battery.parse(no_dump))

        root = card(self.tmp, **GOOD)
        self.assertFalse([t for texts in self.levels(root).values() for t in texts if "battery" in t])
        dump = root / "backup/0123abcd/dumps/fuel_gauge.bin"
        dump.parent.mkdir(parents=True)
        dump.write_bytes(fuel_gauge_dump(3600, 3315))
        line = next(t for t in self.levels(root)["OK"] if t.startswith("battery"))
        self.assertRegex(line, r"^battery, per hekate's fuel gauge dump of 20\d\d-\d\d-\d\d: holds 3315 of the "
                               r"3600 mAh the console is set for \(92 %\), after 123 charge cycles$")
        self.assertNotIn("0123abcd", line)
        mirrored = next(t for _, t in check(root, on_card=False) if t.startswith("battery"))
        self.assertIn("fuel gauge dump: holds", mirrored)

        def battery_line(level: str) -> str:
            return next(t for t in self.levels(root)[level] if "battery" in t)

        dump.write_bytes(fuel_gauge_dump(3600, 2700))
        self.assertIn("(75 %), after 123 charge cycles; it is worn", battery_line("WARN"))
        dump.write_bytes(fuel_gauge_dump(3600, 4100))
        self.assertIn("More than the console is set for", battery_line("OK"))
        dump.write_bytes(fuel_gauge_dump(3600, 3315, cycles=1.58))  # as a Switch Lite's gauge read after a reset
        self.assertIn("(92 %), after 1.6 charge cycles. So few cycles mean the gauge was reset not long ago",
                      battery_line("OK"))
        dump.write_bytes(fuel_gauge_dump(3600, 2700, cycles=1.58))  # no verdict on wear from a gauge that young
        self.assertIn("is still learning this one", battery_line("OK"))
        dump.write_bytes(bytes(512))
        self.assertIn("hekate's fuel gauge dump on the card is not one of a configured gauge; make it again in hekate",
                      self.levels(root)["WARN"])

    def test_check_exit_codes(self):
        root = card(self.tmp, **GOOD)
        self.assertEqual(run_tool("check", str(root)).returncode, 0)
        (root / "payload.bin").unlink()
        result = run_tool("check", str(root), "--json")
        self.assertEqual(result.returncode, 3)
        (report,) = json.loads(result.stdout)
        self.assertEqual(report["card"], str(root))
        self.assertTrue(any(f["level"] == "FAIL" for f in report["findings"]))


@unittest.skipIf(sys.platform == "win32", "there is no sudo on Windows")
class SudoTests(unittest.TestCase):
    def test_files_made_under_sudo_go_back_to_the_user_and_symlinks_are_not_followed(self):
        import pwd  # noqa: PLC0415 - not on Windows
        uid, gid = os.getuid(), os.getgid()
        chowned = []
        with mock.patch.dict(os.environ, {"SUDO_UID": str(uid), "SUDO_GID": str(gid)}), \
                mock.patch.object(os, "getuid", lambda: 0), \
                mock.patch.object(os, "chown", lambda *args, **kwargs: chowned.append((args, kwargs))):
            self.assertEqual(common.sudo_user(), (uid, gid))
            common.hand_back(Path("image.swimg"))
            try:
                home = Path(pwd.getpwuid(uid).pw_dir)
            except KeyError:  # a user id without an account, as in a container: the fallback is the answer
                home = Path.home()
            self.assertEqual(common.user_home(), home)
            if os.geteuid() != 0:
                with mock.patch.object(os, "seteuid", side_effect=AssertionError), common.as_invoking_user():
                    pass  # nothing to drop when the process is not root after all
        self.assertEqual(chowned, [((Path("image.swimg"), uid, gid), {"follow_symlinks": False})])
        with mock.patch.dict(os.environ, {"SUDO_UID": "0; rm", "SUDO_GID": "0"}), \
                mock.patch.object(os, "getuid", lambda: 0):
            self.assertIsNone(common.sudo_user())
        if uid:  # the variables alone make nobody root
            with mock.patch.dict(os.environ, {"SUDO_UID": str(uid), "SUDO_GID": str(gid)}):
                self.assertIsNone(common.sudo_user())

    def test_a_download_that_stalls_or_stops_short_is_retried_and_then_refused(self):
        class Short(io.BytesIO):
            headers = {"Content-Length": "100000"}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(components.time, "sleep", lambda s: None):
            dest = Path(tmp, "a.zip")
            calls = []
            with mock.patch.object(components.urllib.request, "urlopen",
                                   lambda *a, **k: calls.append(1) or Short(b"x" * 30000)), \
                    self.assertRaisesRegex(Failure, "30000 of 100000 bytes"):
                components.download("https://example.invalid/a.zip", dest, None)
            self.assertEqual((len(calls), list(Path(tmp).iterdir())), (3, []))
            import socket  # noqa: PLC0415
            with mock.patch.object(components.urllib.request, "urlopen", side_effect=socket.timeout("timed out")), \
                    self.assertRaisesRegex(Failure, "timed out"):
                components.github_api("repos/x/a")
        self.assertEqual((common.gb(250_139_901_952), common.gb(45_000_000), common.gb(0)),
                         ("250.1 GB", "45 MB", "0 MB"))

    def test_a_cache_that_cannot_be_written_is_an_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp, "file")
            blocker.write_bytes(b"x")
            with mock.patch.object(components.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"data")), \
                    self.assertRaisesRegex(Failure, "cannot write to the download cache"):
                components.download("https://example.invalid/a.zip", blocker / "sub" / "a.zip", None)

    def test_a_pinned_file_that_was_replaced_or_taken_down_is_a_changed_release(self):
        class Body(io.BytesIO):
            headers: dict[str, str] = {}

        calls = []

        def gone(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            raise components.urllib.error.HTTPError("https://example.invalid/a.nro", 404, "Not Found", {}, None)  # type: ignore[arg-type]

        pinned = hashlib.sha256(b"old").hexdigest()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(components.time, "sleep", lambda s: None):
            dest = Path(tmp, "a.nro")
            with mock.patch.object(components.urllib.request, "urlopen", lambda *a, **k: Body(b"new")), \
                    self.assertRaisesRegex(components.Changed, "a.nro is not the file that was pinned"):
                components.download("https://example.invalid/a.nro", dest, pinned)
            with mock.patch.object(components.urllib.request, "urlopen", gone), \
                    self.assertRaisesRegex(components.Changed, r"a.nro is no longer there \(HTTP 404\)"):
                components.download("https://example.invalid/a.nro", dest, pinned)
            self.assertEqual((len(calls), list(Path(tmp).iterdir())), (1, []))  # not tried again, nothing kept
            with mock.patch.object(components.urllib.request, "urlopen", gone), \
                    self.assertRaisesRegex(Failure, "download failed: .*HTTP 404") as raised:
                components.download("https://example.invalid/a.nro", dest, None)  # nothing pinned: a failure
            self.assertNotIsInstance(raised.exception, components.Changed)
            self.assertEqual(len(calls), 4)
        self.assertEqual(components.Changed("x").code, common.EXIT_MISMATCH)


class CliTests(unittest.TestCase):
    def test_a_file_system_error_is_a_message_unless_debugging(self):
        def fail(args):  # type: ignore[no-untyped-def]
            raise PermissionError(13, "Permission denied", "/card/payload.bin")

        from switchsd import cli  # noqa: PLC0415
        err = io.StringIO()
        with mock.patch.object(cli, "cmd_list", fail), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"SWITCHSD_DEBUG": ""}):
            self.assertEqual(cli.main(["list"]), 1)
        self.assertIn("error: Permission denied: /card/payload.bin", err.getvalue())
        with mock.patch.object(cli, "cmd_list", fail), mock.patch.dict(os.environ, {"SWITCHSD_DEBUG": "1"}), \
                self.assertRaises(PermissionError):
            cli.main(["list"])

    def test_version_help_and_usage_errors(self):
        version = run_tool("--version")
        self.assertEqual((version.returncode, version.stdout.strip()), (0, f"switchsd.py {__version__}"))
        for args in (["--help"], ["setup", "--help"], ["clone", "--help"]):
            self.assertEqual(run_tool(*args).returncode, 0, args)
        self.assertEqual(run_tool().returncode, 2)
        self.assertEqual(run_tool("clone", "old.img", "disk5").returncode, 2)  # --expect-bytes is required

    def test_a_command_line_that_cannot_be_right_exits_2_before_anything_is_looked_at(self):
        from switchsd import cli  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            for args, text in (
                    (["new", "no-such-disk", "--expect-bytes", "1", "--emummc", "99"], "not an emuMMC size"),
                    (["new", "no-such-disk", "--expect-bytes", "1", "--label", "far too long a label"], "volume label"),
                    (["format", "no-such-disk", "--expect-bytes", "1", "--label", "bad/label"], "volume label"),
                    (["setup", tmp, "--with", "no-such-component", "--dry-run"], "unknown component"),
                    (["setup", tmp, "--only", "fpslocker", "--without", "saltynx", "--dry-run"], "--without leaves"),
                    (["remote", "8.8.8.8", "info"], "not on a local network"),
                    (["remote", "switch.local", "info"], "not an IP address")):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(cli.main(args), 2, args)
                self.assertIn(text, err.getvalue(), args)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):  # the command line is fine; what it names is not there
                self.assertEqual(cli.main(["check", str(Path(tmp, "missing"))]), 1)

    def test_setup_without_a_card_takes_the_one_that_is_mounted(self):
        from switchsd import cli  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmp:
            one, two = card(Path(tmp, "a"), **GOOD), card(Path(tmp, "b"), **GOOD)
            for cards, code, text in (([], 1, "no mounted Switch card found; give the card's folder"),
                                      ([one, two], 1, "several Switch cards are mounted"),
                                      ([one], 0, f"setting up {one}")):
                err = io.StringIO()
                with mock.patch.object(doctor, "mounted_cards", lambda cards=cards: cards), \
                        mock.patch.object(cli, "resolve_plan", lambda *args: ([], {}, {}, {})), \
                        contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["setup", "--dry-run"]), code, cards)
                self.assertIn(text, err.getvalue())

    def test_setup_refuses_an_atmosphere_too_old_for_the_firmware_the_card_last_booted(self):
        from switchsd import cli  # noqa: PLC0415
        pins = {"atmosphere": {"tag": "1.12.2", "firmware": "23.5.0", "assets": {}}}
        for booted, code in (("24.0.0", 1), ("23.5.1", 1), ("23.5.0", 0), ("22.5.0", 0), ("who knows", 0), (None, 0)):
            with tempfile.TemporaryDirectory() as tmp:
                files = dict(GOOD)
                if booted:
                    files["config__sys-patch__log.ini"] = f"[stats]\nfw_version={booted}\nis_emummc=1\n".encode()
                root = card(Path(tmp), **files)
                before = sorted(p.as_posix() for p in root.rglob("*"))
                err = io.StringIO()
                with mock.patch.object(cli, "resolve_plan", lambda *args: ([], pins, {}, {})), \
                        contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["setup", str(root), "--dry-run"]), code, booted)
                if code:
                    self.assertIn(f"last booted firmware {booted}", err.getvalue())
                    self.assertIn("Atmosphère 1.12.2, which this would install, runs up to 23.5.0", err.getvalue())
                self.assertEqual(sorted(p.as_posix() for p in root.rglob("*")), before)
        with mock.patch.object(cli, "resolve_plan", lambda *args: ([], {}, {}, {})), \
                tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            root = card(Path(tmp), **{**GOOD, "config__sys-patch__log.ini": b"[stats]\nfw_version=99.0.0\n"})
            self.assertEqual(cli.main(["setup", str(root), "--dry-run"]), 0)  # Atmosphère is not being installed

    def test_errors_are_messages_not_tracebacks(self):
        for args in (["info", "not-a-disk"], ["setup", "/nonexistent/folder"], ["check", "/nonexistent"]):
            result = run_tool(*args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn("error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_setup_refuses_a_folder_that_is_not_a_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Documents").mkdir()
            result = run_tool("setup", tmp, "--dry-run")
            self.assertEqual(result.returncode, 1)
            self.assertIn("does not look like a Switch SD card", result.stderr)

    def test_setup_says_what_is_left_to_do_on_the_console(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steps = next_steps(root, ["switch/tico/tico.nro", "payload.bin"])
            self.assertEqual(len(steps), 4)
            self.assertIn("Backup eMMC", steps[0])  # a card without an emuMMC: the console's own storage comes first
            self.assertIn("Create emuMMC", steps[1])
            self.assertIn("Install Forwarder", steps[2])
            (root / "emuMMC").mkdir()
            (root / "emuMMC/emummc.ini").write_text("[emummc]\nenabled=1\n")
            self.assertEqual(next_steps(root, ["payload.bin"]), [])  # an update of a card in use: nothing to add

    def test_undo_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Documents").mkdir()
            result = run_tool("undo", tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("does not look like a Switch SD card", result.stderr)
            card = Path(tmp) / "card"
            (card / "bootloader").mkdir(parents=True)
            (card / "payload.bin").write_bytes(b"hekate")
            result = run_tool("undo", str(card))
            self.assertEqual(result.returncode, 1)
            self.assertIn("nothing to undo", result.stderr)
            (card / "switch").mkdir()
            (card / "switch/new.nro").write_bytes(b"new")
            journal = {"created": {"switch/new.nro": hashlib.sha256(b"new").hexdigest()}, "replaced": {},
                       "removed": {}}
            (card / install.BACKUPS).mkdir(parents=True)
            (card / install.BACKUPS / "20261121-220000.json").write_text(json.dumps(journal))
            (card / "switch/old.nro").write_bytes(b"old")
            earlier = {"created": {"switch/old.nro": hashlib.sha256(b"old").hexdigest()}, "replaced": {},
                       "removed": {}}
            (card / install.BACKUPS / "20261120-210000.json").write_text(json.dumps(earlier))
            result = run_tool("undo", str(card), "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("the setup of 2026-11-21 22:00: would remove the 1 file it added", result.stderr)
            self.assertTrue((card / "switch/new.nro").exists())
            result = run_tool("undo", str(card))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2026-11-21 22:00", result.stderr)
            self.assertIn("Running `undo` again takes back the setup before it.", result.stdout)
            self.assertFalse((card / "switch/new.nro").exists())
            result = run_tool("undo", str(card))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("the setup of 2026-11-20 21:00: removed the 1 file it added", result.stderr)
            self.assertIn("No earlier setup left a backup, so there is nothing more to undo.", result.stdout)
            self.assertNotIn("Running `undo` again", result.stdout)
            self.assertFalse((card / "switch/old.nro").exists())

    def test_list_shows_every_component(self):
        result = run_tool("list")
        self.assertEqual(result.returncode, 0)
        for comp in load_manifest():
            self.assertRegex(result.stdout, rf"(?m)^[* ] {re.escape(comp.id)} ")

    def test_lock_refuses_an_unknown_component_before_any_download(self):
        result = run_tool("lock", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown component", result.stderr)


if __name__ == "__main__":
    unittest.main()
