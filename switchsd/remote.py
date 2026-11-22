"""`remote`: drive the console over Wi-Fi through sys-botbase: buttons, sticks, touch, screenshots, files.

sys-botbase listens on TCP port 6000 with no password, so this only talks to addresses on the local
network, and the sysmodule is installed switched off. The file commands exist in masagrator's build.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path

from .common import EXIT_MISMATCH, EXIT_OK, Failure, Mismatch, Progress, Usage, log, plain
from .components import safe_relpath
from .doctor import check

PORT = 6000
BUTTONS = ("A", "B", "X", "Y", "L", "R", "ZL", "ZR", "PLUS", "MINUS", "LSTICK", "RSTICK",
           "DUP", "DDOWN", "DLEFT", "DRIGHT", "HOME", "CAPTURE")
ALIASES = {"UP": "DUP", "DOWN": "DDOWN", "LEFT": "DLEFT", "RIGHT": "DRIGHT", "+": "PLUS", "-": "MINUS",
           "L3": "LSTICK", "R3": "RSTICK"}
MAX_PICTURE = 4 << 20  # pixelPeek sends at most 0x7D000 bytes, twice that as hex
BLOCK = 1 << 16
# The console's own data: the file commands leave it alone unless told otherwise, as setup does.
PROTECTED = re.compile(r"^/(emummc|nintendo|jksv)(/|$)", re.IGNORECASE)
# For `check`: installed games and saves are not listed, these files are read, and with --programs
# the apps, overlays and sysmodules too. Everything else is only named.
UNLISTED = re.compile(r"^/(nintendo|emummc/[^/]+/nintendo|jksv)$", re.IGNORECASE)
READ = re.compile(r"^/(payload\.bin|exosphere\.ini|bootloader/hekate_ipl\.ini|emummc/emummc\.ini|"
                  r"emummc/[^/]+/raw_based|atmosphere/config/system_settings\.ini|"
                  r"atmosphere/hosts/(default|emummc)\.txt|config/sys-patch/log\.ini|"
                  r"config/switchsd/installed\.json|backup/[^/]+/dumps/fuel_gauge\.bin)$", re.IGNORECASE)
PROGRAM = re.compile(r"^/([^/]+\.nro|atmosphere/contents/[^/]+/exefs\.nsp|switch/([^/]+/){0,2}[^/]+\.(nro|ovl))$",
                     re.IGNORECASE)
MAX_READ = 4 << 20
MAX_ENTRIES = 100_000


def sd_path(path: str, writing: bool = False, force: bool = False) -> str:
    """An absolute path on the console's SD card, in the form sys-botbase takes."""
    path = "/" + path.removeprefix("sdmc:").strip("/")
    if re.search(r"\s", path) or any(ord(c) < 32 for c in path):
        raise Usage(f"{path!r}: sys-botbase cannot address paths with spaces; rename it or use the card reader")
    if any(part in (".", "..") for part in path.split("/")):
        raise Usage(f"{path!r} is not a plain path")
    if writing and PROTECTED.match(path) and not force:
        raise Usage(f"{path} belongs to the console's own data; pass --force if you really mean it")
    return path


def button(name: str) -> str:
    key = ALIASES.get(name.upper(), name.upper())
    if key not in BUTTONS:
        raise Usage(f"unknown button {name!r}; use one of {', '.join(BUTTONS)} (or UP/DOWN/LEFT/RIGHT, L3/R3)")
    return key


def local_address(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise Usage(f"{host!r} is not an IP address; the console shows its own under System Settings > "
                    "Internet") from None
    address = getattr(address, "ipv4_mapped", None) or address  # ::ffff:8.8.8.8 is 8.8.8.8
    if not (address.is_private or address.is_loopback or address.is_link_local):
        raise Usage(f"{host} is not on a local network; sys-botbase must never be reachable from the internet")
    return host


class Botbase:
    def __init__(self, host: str, port: int = PORT, timeout: float = 10) -> None:
        try:
            self.sock = socket.create_connection((local_address(host), port), timeout=timeout)
        except ConnectionRefusedError:
            raise Failure(f"nothing answers on {host}:{port}. In CFW, open the overlay menu (L + D-pad down + R3) "
                          "> Sysmodules and switch sys-botbase on.") from None
        except OSError as e:
            raise Failure(f"cannot reach {host}:{port} ({e.strerror or e}); is the console on the same Wi-Fi?") \
                from None
        self.reader = self.sock.makefile("rb")
        self.awake = False

    def __enter__(self) -> Botbase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.reader.close()
        self.sock.close()

    def send(self, command: str) -> None:
        try:
            self.sock.sendall(command.encode("utf-8") + b"\r\n")
        except OSError as e:
            raise Failure(f"the connection to the console broke ({e.strerror or e})") from None

    def line(self, what: str, limit: int = 4096) -> bytes:
        """One line from the console; empty when it hung up."""
        try:
            return self.reader.readline(limit)
        except socket.timeout:
            raise Failure(f"sys-botbase did not answer {what} in time") from None
        except OSError as e:
            raise Failure(f"the connection to the console broke ({e.strerror or e})") from None

    def ask(self, command: str, limit: int = 4096) -> str:
        self.send(command)
        line = self.line(repr(command), limit)
        if not line.endswith(b"\n"):
            raise Failure(f"sys-botbase sent an incomplete answer to {command!r}")
        return line.decode("ascii", "replace").strip()

    def screenshot(self) -> bytes:
        text = self.ask("pixelPeek", MAX_PICTURE)
        try:
            picture = bytes.fromhex(text)
        except ValueError:
            raise Failure("sys-botbase sent something other than a picture") from None
        if not picture.startswith(b"\xff\xd8"):
            raise Failure("sys-botbase sent no picture; the system refused the capture (some games block it)")
        return picture

    def charge(self) -> int | None:
        """The battery's charge in percent, or None from a sys-botbase that does not answer the question."""
        timeout = self.sock.gettimeout()
        self.sock.settimeout(3)
        try:
            answer = self.ask("charge")
        except Failure:
            return None
        finally:
            self.sock.settimeout(timeout)
        return int(answer) if answer.isdigit() and int(answer) <= 100 else None

    def require_files(self) -> None:
        version = self.ask("getVersion")
        if "files" not in version:
            raise Failure(f"sys-botbase {version} on the console has no file commands; they come with "
                          "masagrator's build, which `switchsd.py setup` installs")

    def size(self, path: str) -> int:
        """Size in bytes, or -1 when there is no such file or folder."""
        return number(self.ask(f"fileSize {sd_path(path)}"))

    def listdir(self, path: str) -> list[tuple[str, int | None]]:
        """Entries of a folder as (name, size), size None for a folder."""
        path = sd_path(path)
        if self.size(path) < 0:
            raise Failure(f"{path} does not exist on the console's card")
        self.send(f"fileList {path}")
        entries: list[tuple[str, int | None]] = []
        while True:
            raw = self.line(f"with the listing of {path}")
            if not raw:
                raise Failure(f"the connection closed in the middle of listing {path}")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                return sorted(entries)
            kind, _, rest = line.partition(" ")
            if kind == "D":
                entries.append((rest, None))
            elif kind == "F":
                size, _, name = rest.partition(" ")
                entries.append((name, number(size)))
            if len(entries) > MAX_ENTRIES:
                raise Failure(f"{path} lists more than {MAX_ENTRIES} entries; giving up")

    def pull(self, path: str, target: Path, show: bool = True) -> str:
        """Copy a file from the console to `target`; returns its SHA-256."""
        path = sd_path(path)
        self.send(f"fileRead {path}")
        total = number(self.line(f"with {path}", 64).decode("ascii", "replace").strip() or "-1")
        if total < 0:
            raise Failure(f"{path} cannot be read on the console's card")
        if total > shutil.disk_usage(target.resolve().parent).free:
            raise Failure(f"{path} is announced as {total} bytes, more than there is room for here")
        digest, left = hashlib.sha256(), total
        progress = Progress(f"pulling {path}", max(total, 1)) if show else None
        with open(target, "wb") as out:
            while left:
                try:
                    block = self.reader.read(min(BLOCK, left))
                except OSError as e:  # a timeout is one of these
                    raise Failure(f"the console stopped sending {path} ({e.strerror or e})") from None
                if not block:
                    raise Failure(f"the connection closed {left} bytes before the end of {path}")
                digest.update(block)
                out.write(block)
                left -= len(block)
                if progress:
                    progress.add(len(block))
        return digest.hexdigest()

    def snapshot(self, dest: Path, programs: bool) -> tuple[int, int, list[str]]:
        """Mirror the card into `dest` for `check`: (folders listed, files read, paths left out).

        Every file is created by name, empty unless `check` reads it. Names come from the console, so
        each one is checked before it becomes a local path.
        """
        folders = read = entries = 0
        left_out: list[str] = []
        stack = ["/"]
        while stack:
            folder = stack.pop()
            folders += 1
            for name, size in self.listdir(folder):
                path = f"{folder.rstrip('/')}/{name}"
                entries += 1
                if entries > MAX_ENTRIES:
                    raise Failure(f"the card lists more than {MAX_ENTRIES} files and folders; giving up")
                if not safe_relpath(path[1:]) or (size is None and UNLISTED.match(path)):
                    left_out.append(path)
                    continue
                local = dest / path[1:]
                reachable = not re.search(r"\s", path)
                wanted = READ.match(path) and (size or 0) <= MAX_READ or programs and PROGRAM.match(path)
                try:
                    if size is None:
                        local.mkdir(exist_ok=True)
                        if reachable:
                            stack.append(path)
                        else:
                            left_out.append(path)
                    elif wanted and reachable:
                        self.pull(path, local, show=False)
                        read += 1
                    else:
                        local.touch()
                except OSError:  # a name this computer's file system cannot hold
                    left_out.append(path)
        return folders, read, left_out

    def push(self, source: Path, path: str, force: bool = False) -> str:
        """Copy a local file to the console; returns its SHA-256."""
        path = sd_path(path, writing=True, force=force)
        total = source.stat().st_size
        digest, progress = hashlib.sha256(), Progress(f"pushing {path}", max(total, 1))
        self.send(f"fileWrite {path} {total}")
        with open(source, "rb") as src:
            for block in iter(lambda: src.read(BLOCK), b""):
                digest.update(block)
                try:
                    self.sock.sendall(block)
                except OSError as e:
                    raise Failure(f"the connection broke while sending {path} ({e.strerror or e})") from None
                progress.add(len(block))
        answer = self.line(f"after {path} was sent", 64).decode("ascii", "replace").strip()
        if answer != f"ok {total}":
            raise Failure(f"the console did not store {path}: it answered {answer!r} (is the folder there?)")
        return digest.hexdigest()

    def command(self, line: str, what: str) -> None:
        answer = self.ask(line)
        if answer != "ok":
            raise Failure(f"{what} failed on the console ({answer or 'no answer'})")

    def missing_dirs(self, path: str, force: bool = False) -> list[str]:
        """The folders of `path` that are not on the card yet, the outermost first."""
        parts = [p for p in sd_path(path, writing=True, force=force).split("/") if p]
        folders = ["/" + "/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]
        return [folder for folder in folders if self.size(folder) < 0]

    def makedirs(self, path: str, force: bool = False) -> None:
        for folder in self.missing_dirs(path, force):
            self.command(f"fileMkdir {folder}", f"creating {folder}")

    def wake(self) -> None:
        """Attach sys-botbase's controller with nothing held, before the first press goes out.

        sys-botbase attaches its controller with the first command that needs one. When that command
        is a press, the console never sees the button up, and it does not count HOME as pressed: on a
        Switch Lite a first `click HOME` was lost three times out of three, and none out of four once
        a centred stick went first.
        """
        if not self.awake:
            self.send("setStick LEFT 0 0")
            self.awake = True

    def click(self, key: str) -> None:
        key = button(key)
        self.wake()
        self.send(f"click {key}")

    def hold(self, key: str, seconds: float) -> None:
        key = button(key)
        if not 0 <= seconds <= 60:
            raise Usage("hold takes between 0 and 60 seconds")
        self.wake()
        self.send(f"press {key}")
        try:
            time.sleep(seconds)
        finally:  # also when interrupted: the console would keep the button down
            self.send(f"release {key}")

    def stick(self, side: str, x: float, y: float, seconds: float) -> None:
        side = side.upper()
        if side not in ("LEFT", "RIGHT") or not (-1 <= x <= 1 and -1 <= y <= 1) or not 0 <= seconds <= 60:
            raise Usage("stick takes LEFT or RIGHT, x and y between -1 and 1, and between 0 and 60 seconds")
        self.wake()
        self.send(f"setStick {side} {round(x * 0x7FFF)} {round(y * 0x7FFF)}")
        try:
            time.sleep(seconds)
        finally:
            self.send(f"setStick {side} 0 0")

    def touch(self, points: list[int]) -> None:
        if not points or len(points) % 2:
            raise Usage("touch takes pairs of x y")
        for x, y in zip(points[::2], points[1::2]):
            if not (0 <= x <= 1280 and 0 <= y <= 720):
                raise Usage(f"touch point {x},{y} is off the 1280x720 screen")
        self.send("touch " + " ".join(map(str, points)))


def cmd_remote(args: argparse.Namespace) -> int:
    with Botbase(args.host, args.port) as bot:
        if args.action == "info":
            title = bot.ask("getTitleID")  # an empty line when no game runs
            version, charge = bot.ask("getVersion"), bot.charge()
            running = f"running title {title}" if title.strip("0") else "no game running"
            battery = f", battery at {charge} %" if charge is not None else ""
            print(plain(f"sys-botbase {version}, {running}{battery}"))
        elif args.action == "screenshot":
            out = Path(args.file)
            out.write_bytes(bot.screenshot())
            print(f"saved {out} ({out.stat().st_size} bytes)")
        elif args.action == "click":
            for key in args.buttons:
                button(key)
            for i, key in enumerate(args.buttons):
                if i:
                    time.sleep(args.wait / 1000)
                bot.click(key)
            log(f"clicked {' '.join(args.buttons)}")
        elif args.action == "hold":
            bot.hold(args.button, args.seconds)
        elif args.action == "stick":
            bot.stick(args.side, args.x, args.y, args.seconds)
        elif args.action == "touch":
            bot.touch(args.points)
        else:
            bot.sock.settimeout(120)
            bot.require_files()
            return files(bot, args)
    return EXIT_OK


def number(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise Failure(f"sys-botbase answered {text[:40]!r} where a number belongs") from None


def files(bot: Botbase, args: argparse.Namespace) -> int:
    if args.action == "check":
        with tempfile.TemporaryDirectory() as tmp:
            folders, read, left_out = bot.snapshot(Path(tmp), args.programs)
            findings = check(Path(tmp), on_card=False, scan_abi=args.programs)
        charge = bot.charge()
        if charge is not None:
            findings.append(("OK", f"battery at {charge} % right now, says the console"))
        log(plain(f"over Wi-Fi: {folders} folders listed, {read} files read"
                  + (f"; not looked into: {', '.join(left_out[:8])}" if left_out else "")))
        for level, text in findings:
            print(plain(f"{level:<4} {text}"))
        return EXIT_MISMATCH if any(level == "FAIL" for level, _ in findings) else EXIT_OK
    if args.action == "ls":
        for name, size in bot.listdir(args.path):
            print(plain(f"{'folder' if size is None else size:>12}  {name}"))
    elif args.action == "pull":
        target = Path(args.target or Path(sd_path(args.path)).name)
        if target.is_dir():
            target = target / Path(sd_path(args.path)).name
        print(f"saved {target}  sha256 {bot.pull(args.path, target)}")
    elif args.action == "push":
        source = Path(args.source)
        if not source.is_file():
            raise Failure(f"{source} is not a file")
        remote = sd_path(args.path, writing=True, force=args.force)
        parent = remote.rsplit("/", 1)[0] or "/"
        if args.dry_run:
            for folder in bot.missing_dirs(parent, args.force):
                print(f"would create {folder}")
            old = bot.size(remote)
            print(f"would store {remote} ({source.stat().st_size} bytes)"
                  + (f", replacing the {old} bytes that are there" if old >= 0 else ""))
            return EXIT_OK
        bot.makedirs(parent, args.force)
        sent = bot.push(source, remote, args.force)
        if not args.no_verify:
            readback = Path(str(source) + ".switchsd-readback")
            try:
                if bot.pull(remote, readback) != sent:
                    raise Mismatch(f"{remote} reads back different from {source}; send it again")
            finally:
                readback.unlink(missing_ok=True)
        print(f"stored {remote}  sha256 {sent}" + ("" if args.no_verify else "  (read back and compared)"))
    elif args.action == "rm":
        remote = sd_path(args.path, writing=True, force=args.force)
        if args.dry_run:
            if bot.size(remote) < 0:
                raise Failure(f"{remote} does not exist on the console's card")
            print(f"would delete {remote}")
            return EXIT_OK
        bot.command(f"fileDelete {remote}", f"deleting {remote}")
        print(f"deleted {remote}")
    elif args.action == "mkdir":
        if args.dry_run:
            missing = bot.missing_dirs(args.path, args.force)
            print("\n".join(f"would create {folder}" for folder in missing) if missing
                  else f"{sd_path(args.path)} is there already")
            return EXIT_OK
        bot.makedirs(args.path, args.force)
        print(f"created {sd_path(args.path)}")
    return EXIT_OK


def add_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("remote", help="control the console over Wi-Fi through sys-botbase (switch it on first)")
    p.add_argument("host", help="the console's IP address (System Settings > Internet)")
    p.add_argument("--port", type=int, default=PORT, help=f"sys-botbase's port (default {PORT})")
    actions = p.add_subparsers(dest="action", required=True, metavar="action")
    actions.add_parser("info", help="sys-botbase's version and the running title")
    a = actions.add_parser("screenshot", help="save the screen as a JPEG")
    a.add_argument("file", help="where to save it, such as screen.jpg")
    a = actions.add_parser("click", help="tap buttons in turn: A B X Y L R ZL ZR PLUS MINUS UP DOWN LEFT RIGHT "
                                         "L3 R3 HOME CAPTURE")
    a.add_argument("buttons", nargs="+")
    a.add_argument("--wait", type=int, default=300, help="milliseconds between taps (default 300)")
    a = actions.add_parser("hold", help="hold a button down")
    a.add_argument("button")
    a.add_argument("seconds", type=float)
    a = actions.add_parser("stick", help="push a stick, then let it go")
    a.add_argument("side", help="LEFT or RIGHT")
    a.add_argument("x", type=float, help="-1 (left) to 1 (right)")
    a.add_argument("y", type=float, help="-1 (down) to 1 (up)")
    a.add_argument("--seconds", type=float, default=0.5, help="how long (default 0.5)")
    a = actions.add_parser("touch", help="tap the touch screen at x y (1280x720), more pairs tap in turn")
    a.add_argument("points", nargs="+", type=int)
    a = actions.add_parser("check", help="run `check` on the card while it sits in the console")
    a.add_argument("--programs", action="store_true",
                   help="also copy every app, overlay and sysmodule over to read its homebrew ABI (slow)")
    a = actions.add_parser("ls", help="list a folder on the console's SD card")
    a.add_argument("path", nargs="?", default="/")
    a = actions.add_parser("pull", help="copy a file from the console's SD card to this computer")
    a.add_argument("path", help="file on the card, such as /config/sys-patch/log.ini")
    a.add_argument("target", nargs="?", help="where to save it (default: the same name, here)")
    a = actions.add_parser("push", help="copy a file from this computer to the console's SD card")
    a.add_argument("source")
    a.add_argument("path", help="where on the card, such as /switch/app/app.nro")
    a.add_argument("--no-verify", action="store_true", help="skip reading the file back to compare it")
    a = actions.add_parser("rm", help="delete a file or an empty folder on the console's SD card")
    a.add_argument("path")
    a = actions.add_parser("mkdir", help="create a folder on the console's SD card")
    a.add_argument("path")
    for name in ("push", "rm", "mkdir"):
        actions.choices[name].add_argument("--force", action="store_true",
                                           help="allow emuMMC/, Nintendo/ and JKSV/, the console's own data")
        actions.choices[name].add_argument("--dry-run", action="store_true",
                                           help="ask the console what is there, show what would change, and stop")
    p.set_defaults(func=cmd_remote)
