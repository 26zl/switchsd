"""`remote` against a fake sys-botbase that records every command it gets."""

from __future__ import annotations

import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import GOOD, fuel_gauge_dump, run_tool

from switchsd import remote
from switchsd.common import Failure
from switchsd.remote import Botbase, button, local_address

JPEG = b"\xff\xd8\xff\xe0" + b"picture" * 50 + b"\xff\xd9"


class FakeBotbase(socketserver.ThreadingTCPServer):
    """Speaks sys-botbase's protocol, masagrator's file commands included, over an in-memory SD card."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, picture: bytes = JPEG) -> None:
        self.commands: list[str] = []
        self.picture = picture
        self.version = "2.6-files-kill"
        self.title = "0100000000001000"  # sys-botbase answers an empty line when no game runs
        self.charge = "81"
        self.files: dict[str, bytes] = {"/config/sys-patch/log.ini": b"[stats]\nfw_version=22.5.0\n"}
        self.folders = {"/", "/config", "/config/sys-patch", "/switch"}
        self.corrupt_writes = False
        self.sizes: str | None = None  # an answer to give instead of a file's size
        self.cut_listing = False  # hang up in the middle of a listing
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(socketserver.StreamRequestHandler):
    hung_up = False

    def handle(self) -> None:
        server: FakeBotbase = self.server  # type: ignore[assignment]
        for raw in self.rfile:
            command = raw.decode().strip()
            server.commands.append(command)
            replies = {"getVersion": server.version, "getTitleID": server.title, "charge": server.charge,
                       "pixelPeek": server.picture.hex()}
            if command in replies:
                self.wfile.write(replies[command].encode() + b"\n")
            elif command.startswith("file"):
                self.file_command(server, command.split(" "))
            if self.hung_up:  # Windows answers a read after the hang-up with an error, not the end of the stream
                return

    def file_command(self, server: FakeBotbase, argv: list[str]) -> None:
        name, path = argv[0], argv[1]
        out = b"err unknown\n"
        if name == "fileSize" and server.sizes:
            out = server.sizes.encode() + b"\n"
        elif name == "fileSize":
            size = len(server.files[path]) if path in server.files else 0 if path in server.folders else -1
            out = b"%d\n" % size
        elif name == "fileList":
            prefix = path.rstrip("/") + "/"
            lines = [b"D " + f[len(prefix):].encode() for f in server.folders
                     if f.startswith(prefix) and "/" not in f[len(prefix):] and f != "/"]
            lines += [b"F %d %s" % (len(d), f[len(prefix):].encode()) for f, d in server.files.items()
                      if f.startswith(prefix) and "/" not in f[len(prefix):]]
            out = b"".join(line + b"\n" for line in lines) + b"\n"
            if server.cut_listing:
                self.wfile.write(out[:-1].rstrip(b"\n") + b"\n")
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.hung_up = True
                return
        elif name == "fileRead":
            data = server.files.get(path)
            out = b"-1\n" if data is None else b"%d\n" % len(data) + data
        elif name == "fileWrite":
            data = self.rfile.read(int(argv[2]))
            parent = path.rsplit("/", 1)[0] or "/"
            if parent in server.folders:
                server.files[path] = data[:-1] + b"?" if server.corrupt_writes and data else data
                out = b"ok %d\n" % len(data)
            else:
                out = b"err 0\n"
        elif name == "fileDelete":
            out = b"ok\n" if server.files.pop(path, None) is not None else b"err\n"
        elif name == "fileMkdir":
            ok = path not in server.folders and (path.rsplit("/", 1)[0] or "/") in server.folders
            if ok:
                server.folders.add(path)
            out = b"ok\n" if ok else b"err\n"
        self.wfile.write(out)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeBotbase()
        self.port = str(self.server.server_address[1])
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def remote(self, *args: str):  # type: ignore[no-untyped-def]
        return run_tool("remote", "127.0.0.1", "--port", self.port, *args, timeout=60)

    def test_info_and_screenshot(self):
        result = self.remote("info")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sys-botbase 2.6-files-kill, running title 0100000000001000, battery at 81 %", result.stdout)
        self.server.title = ""
        self.assertIn("sys-botbase 2.6-files-kill, no game running, battery at 81 %", self.remote("info").stdout)
        self.server.charge = "what?"  # an answer that is no percentage is left out, not passed on
        self.assertEqual(self.remote("info").stdout.strip(), "sys-botbase 2.6-files-kill, no game running")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "screen.jpg")
            result = self.remote("screenshot", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.read_bytes(), JPEG)

    def test_buttons_sticks_and_touch_become_sys_botbase_commands(self):
        self.assertEqual(self.remote("click", "HOME", "down", "a", "--wait", "0").returncode, 0)
        self.assertEqual(self.remote("hold", "ZR", "0").returncode, 0)
        self.assertEqual(self.remote("stick", "left", "1", "-0.5", "--seconds", "0").returncode, 0)
        self.assertEqual(self.remote("touch", "640", "360").returncode, 0)
        centred = "setStick LEFT 0 0"  # goes first in every run that presses or pushes something
        self.assertEqual(self.server.commands, [
            centred, "click HOME", "click DDOWN", "click A", centred, "press ZR", "release ZR",
            centred, "setStick LEFT 32767 -16384", "setStick LEFT 0 0", "touch 640 360"])

    def test_bad_input_is_refused_before_anything_is_sent(self):
        for args in (["click", "A", "Q"], ["touch", "2000", "10"], ["stick", "UP", "0", "0"]):
            result = self.remote(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("error:", result.stderr)
        self.assertNotIn("click A", self.server.commands)

    def test_files_travel_both_ways_and_are_read_back(self):
        listing = self.remote("ls", "/config/sys-patch")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("log.ini", listing.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            got = Path(tmp, "log.ini")
            self.assertEqual(self.remote("pull", "/config/sys-patch/log.ini", str(got)).returncode, 0)
            self.assertEqual(got.read_bytes(), b"[stats]\nfw_version=22.5.0\n")
            payload = bytes(range(256)) * 700  # binary, larger than one 64 KiB block
            source = Path(tmp, "app.nro")
            source.write_bytes(payload)
            pushed = self.remote("push", str(source), "/switch/new-app/app.nro")
            self.assertEqual(pushed.returncode, 0, pushed.stderr)
            self.assertIn("read back and compared", pushed.stdout)
            self.assertEqual(self.server.files["/switch/new-app/app.nro"], payload)
            self.assertIn("/switch/new-app", self.server.folders)
            self.assertEqual(list(Path(tmp).glob("*.switchsd-readback")), [])
            self.assertEqual(self.remote("rm", "/switch/new-app/app.nro").returncode, 0)
            self.assertNotIn("/switch/new-app/app.nro", self.server.files)

            self.server.corrupt_writes = True
            bad = self.remote("push", str(source), "/switch/bad.nro")
            self.assertEqual(bad.returncode, 3)
            self.assertIn("reads back different", bad.stderr)

    def test_a_dry_run_asks_the_console_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "app.nro")
            source.write_bytes(b"x" * 1234)
            new = self.remote("push", str(source), "/switch/new-app/deep/app.nro", "--dry-run")
            self.assertEqual(new.returncode, 0, new.stderr)
            self.assertEqual(new.stdout.splitlines(),
                             ["would create /switch/new-app", "would create /switch/new-app/deep",
                              "would store /switch/new-app/deep/app.nro (1234 bytes)"])
            over = self.remote("push", str(source), "/config/sys-patch/log.ini", "--dry-run")
            self.assertIn("would store /config/sys-patch/log.ini (1234 bytes), replacing the 26 bytes that are there",
                          over.stdout)
            self.assertEqual(self.remote("rm", "/config/sys-patch/log.ini", "--dry-run").stdout.strip(),
                             "would delete /config/sys-patch/log.ini")
            gone = self.remote("rm", "/config/nothing.ini", "--dry-run")
            self.assertEqual(gone.returncode, 1)
            self.assertIn("does not exist on the console's card", gone.stderr)
            self.assertEqual(self.remote("mkdir", "/switch/a/b", "--dry-run").stdout.splitlines(),
                             ["would create /switch/a", "would create /switch/a/b"])
            self.assertIn("/config/sys-patch is there already",
                          self.remote("mkdir", "/config/sys-patch", "--dry-run").stdout)
            kept = self.remote("rm", "/Nintendo/save/x", "--dry-run")  # a dry run opens no door either
            self.assertEqual(kept.returncode, 2)
        self.assertFalse([c for c in self.server.commands if c.startswith(("fileWrite", "fileDelete", "fileMkdir"))])
        self.assertEqual(self.server.files, {"/config/sys-patch/log.ini": b"[stats]\nfw_version=22.5.0\n"})
        self.assertEqual(self.server.folders, {"/", "/config", "/config/sys-patch", "/switch"})

    def test_file_commands_refuse_what_they_cannot_or_should_not_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "x.bin")
            source.write_bytes(b"x")
            for args, text, code in (
                    (["pull", "/switch/no such file.nro"], "spaces", 2),
                    (["pull", "/switch/missing.nro", str(Path(tmp, "m"))], "cannot be read", 1),
                    (["ls", "/nope"], "does not exist", 1),
                    (["push", str(source), "/emuMMC/RAW1/raw_based"], "console's own data", 2),
                    (["rm", "/Nintendo/save/x"], "console's own data", 2),
                    (["rm", "/JKSV/Game/save"], "console's own data", 2),
                    (["push", str(source), "/switch/../payload.bin"], "not a plain path", 2)):
                result = self.remote(*args)
                self.assertEqual(result.returncode, code, args)
                self.assertIn(text, result.stderr, args)
        self.assertFalse([c for c in self.server.commands if c.startswith(("fileWrite", "fileDelete"))])
        self.server.version = "2.5"
        old = self.remote("ls", "/")
        self.assertEqual(old.returncode, 1)
        self.assertIn("no file commands", old.stderr)

    def put_card(self, files: dict[str, bytes]) -> None:
        for path, data in files.items():
            self.server.files[path] = data
            parts = path.split("/")[1:-1]
            for depth in range(1, len(parts) + 1):
                self.server.folders.add("/" + "/".join(parts[:depth]))

    def test_check_runs_on_the_card_while_it_sits_in_the_console(self):
        card = {"/" + name.replace("__", "/"): data for name, data in GOOD.items()}
        card["/atmosphere/automatic_backups/X_PRODINFO.bin"] = b"the console's identity"
        card["/emuMMC/RAW1/Nintendo/Contents/registered/a.nca"] = b"an installed game"
        card["/switch/app/app.nro"] = b"not a program image"
        card["/../outside.txt"] = b"a name no card can hold"
        card["/backup/0123abcd/dumps/fuel_gauge.bin"] = fuel_gauge_dump(3600, 3315)
        card["/backup/0123abcd/rawnand.bin.00"] = b"a NAND backup, which is only named"
        self.put_card(card)
        result = self.remote("check")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Atmosphère has left no crash reports on the card", result.stdout)
        self.assertIn("battery, per hekate's fuel gauge dump: holds 3315 of the 3600 mAh the console is set for (92 %)",
                      result.stdout)
        self.assertIn("battery at 81 % right now, says the console", result.stdout)
        self.assertNotIn("0123abcd", result.stdout)
        self.assertIn("payload.bin for the modchip: hekate 6.5.5", result.stdout)
        self.assertIn("emuMMC: partition-based at sector 0x1b9e0000, raw_based agrees", result.stdout)
        self.assertIn("Atmosphère's backup of the console's PRODINFO is on the card", result.stdout)
        self.assertIn("their homebrew ABI was not read", result.stdout)
        self.assertNotIn("file system", result.stdout)
        self.assertNotIn("WARN", result.stdout)
        read = [c.split(" ", 1)[1] for c in self.server.commands if c.startswith("fileRead")]
        self.assertIn("/bootloader/hekate_ipl.ini", read)
        self.assertIn("/backup/0123abcd/dumps/fuel_gauge.bin", read)
        self.assertFalse([p for p in read
                          if "PRODINFO" in p or "Nintendo" in p or p.endswith(".nro") or "rawnand" in p])
        self.assertNotIn("fileList /emuMMC/RAW1/Nintendo", self.server.commands)
        self.assertFalse([c for c in self.server.commands if c.startswith(("fileWrite", "fileDelete", "fileMkdir"))])

        deep = self.remote("check", "--programs")
        self.assertEqual(deep.returncode, 0, deep.stderr)
        self.assertIn("fileRead /switch/app/app.nro", self.server.commands)
        self.assertNotIn("was not read", deep.stdout)

        del self.server.files["/payload.bin"]
        broken = self.remote("check")
        self.assertEqual(broken.returncode, 3)
        self.assertIn("FAIL payload.bin is missing", broken.stdout)

    def test_answers_that_are_not_numbers_are_errors_not_tracebacks(self):
        self.server.sizes = "lots"
        result = self.remote("ls", "/config")
        self.assertEqual(result.returncode, 1)
        self.assertIn("where a number belongs", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_button_is_let_go_even_when_the_wait_is_interrupted(self):
        with Botbase("127.0.0.1", int(self.port)) as bot:
            with mock.patch.object(remote.time, "sleep", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                bot.hold("A", 1)
            with mock.patch.object(remote.time, "sleep", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                bot.stick("LEFT", 1, 0, 1)
            for bad in (-1, 3600):
                with self.assertRaisesRegex(Failure, "seconds"):
                    bot.hold("A", bad)
            bot.ask("getVersion")  # by now the server has seen everything sent before
        self.assertEqual(self.server.commands[:5],
                         ["setStick LEFT 0 0", "press A", "release A", "setStick LEFT 32767 0", "setStick LEFT 0 0"])

    def test_a_listing_cut_short_is_an_error(self):
        self.server.cut_listing = True
        result = self.remote("ls", "/config")
        self.assertEqual(result.returncode, 1)
        self.assertIn("closed in the middle of listing /config", result.stderr)

    def test_a_refused_capture_is_reported(self):
        self.server.picture = b""
        with Botbase("127.0.0.1", int(self.port)) as bot, self.assertRaisesRegex(Failure, "no picture"):
            bot.screenshot()

    def test_addresses_and_buttons(self):
        for ok in ("192.168.1.50", "10.0.0.2", "172.16.5.4", "127.0.0.1", "169.254.1.1"):
            self.assertEqual(local_address(ok), ok)
        for bad in ("8.8.8.8", "switch.local", "1.1.1.1", "::ffff:8.8.8.8"):
            with self.assertRaises(Failure):
                local_address(bad)
        self.assertEqual(button("r3"), "RSTICK")
        with self.assertRaises(Failure):
            button("TURBO")


if __name__ == "__main__":
    unittest.main()
