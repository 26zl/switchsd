"""Errors, logging and small helpers shared by every command."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_MISMATCH, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
CHUNK = 4 << 20

# Created by macOS or Windows; never part of a Switch SD card.
JUNK = frozenset({
    ".DS_Store", ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems", ".apdisk",
    ".VolumeIcon.icns", ".DocumentRevisions-V100", ".metadata_never_index",
    ".com.apple.timemachine.donotpresent",
})
ROOT_JUNK = frozenset({"System Volume Information", "$RECYCLE.BIN"})
GAME_EXTENSIONS = (".nsp", ".nsz", ".xci", ".xcz")


class Failure(Exception):
    """An error whose message is meant for the user."""

    code = EXIT_ERROR


class Mismatch(Failure):
    code = EXIT_MISMATCH


class Usage(Failure):
    """A command line that no card or console could make right: an unknown name, a value out of range."""

    code = EXIT_USAGE


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def gb(n: int) -> str:
    """A size for people: GB, or MB where GB would round to nothing."""
    return f"{n / 1e9:.1f} GB" if n >= 5e7 else f"{n / 1e6:.0f} MB"


def nfiles(n: int) -> str:
    """'1 file', '2 files'."""
    return f"{n} file" + ("" if n == 1 else "s")


def plain(text: str) -> str:
    """Text that came off a card or from the console, without the control characters a terminal would obey."""
    return "".join(c if c.isprintable() or c in "\n\t" else "?" for c in text)


def run(cmd: list[str], timeout: float = 120) -> str:
    """What a system tool prints. One that is missing, hangs or fails is a Failure."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout).stdout
    except FileNotFoundError:
        raise Failure(f"{cmd[0]} not found") from None
    except subprocess.TimeoutExpired:
        raise Failure(f"{' '.join(cmd)} timed out") from None
    except subprocess.CalledProcessError as e:
        raise Failure(f"{' '.join(cmd)} failed: {(e.stderr or e.stdout or '').strip()}") from None


def parse_ini(text: str) -> list[tuple[str, dict[str, str]]]:
    """Sections in file order, keys in lower case; hekate allows repeated keys, the last one wins here."""
    sections: list[tuple[str, dict[str, str]]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        if s.startswith("[") and s.endswith("]"):
            sections.append((s[1:-1].strip(), {}))
        elif "=" in s and sections:
            key, value = s.split("=", 1)
            sections[-1][1][key.strip().lower()] = value.strip()
    return sections


def linux_mounts(table: str = "/proc/self/mounts") -> list[tuple[str, str, str]]:
    """Every mount as (device, mount point, file system), in the kernel's order: a later one covers an earlier.

    The kernel writes a space, tab, newline or backslash in a name as a backslash and three octal digits.
    """
    def unescape(field: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)

    found = []
    with open(table, encoding="utf-8", errors="replace") as f:
        for line in f:
            fields = line.split()
            if len(fields) >= 3:
                found.append((unescape(fields[0]), unescape(fields[1]), fields[2]))
    return found


def is_junk(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return name in JUNK or name.startswith("._") or (len(parts) == 1 and name in ROOT_JUNK)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sudo_user() -> tuple[int, int] | None:
    """The uid and gid of whoever ran sudo, when this process is root because of sudo."""
    uid, gid = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
    if hasattr(os, "getuid") and os.getuid() == 0 and uid.isdigit() and gid.isdigit():
        return int(uid), int(gid)
    return None


def hand_back(*paths: Path) -> None:
    """Give files created under sudo to the user who ran sudo. A symlink put in their place stays a symlink."""
    user = sudo_user()
    if user:
        for path in paths:
            os.chown(path, *user, follow_symlinks=False)


@contextlib.contextmanager
def as_invoking_user() -> Iterator[None]:
    """Under sudo, do what needs no privileges as the user who ran sudo.

    Downloads land in a folder that user can write to; as root, a symlink planted there would point the
    write anywhere.
    """
    user = sudo_user()
    if not user or os.geteuid() != 0:
        yield
        return
    os.setegid(user[1])
    os.seteuid(user[0])
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(0)


def user_home() -> Path:
    """The home folder of the person at the keyboard: under sudo, of the one who ran it."""
    user = sudo_user()
    if user:
        import pwd  # noqa: PLC0415 - not on Windows
        with contextlib.suppress(KeyError):
            return Path(pwd.getpwuid(user[0]).pw_dir)
    return Path.home()


def cache_dir() -> Path:
    """Where downloads are kept between runs, per platform convention."""
    override = os.environ.get("SWITCHSD_CACHE")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "switchsd" / "cache"
    if sys.platform == "darwin":
        return user_home() / "Library" / "Caches" / "switchsd"
    return (Path(os.environ["XDG_CACHE_HOME"]) if os.environ.get("XDG_CACHE_HOME") and not sudo_user()
            else user_home() / ".cache") / "switchsd"


class Progress:
    def __init__(self, label: str, total: int) -> None:
        self.label, self.total, self.done = label, total, 0
        self.start = self.last = time.monotonic()
        self.tty = sys.stderr.isatty()

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        finished = self.done >= self.total
        if not finished and now - self.last < (0.5 if self.tty else 15):
            return
        self.last = now
        rate = self.done / max(now - self.start, 1e-3) / 1e6
        line = f"{self.label}: {self.done / 1e9:.2f}/{self.total / 1e9:.2f} GB ({rate:.0f} MB/s)"
        if self.tty:
            print("\r" + line, end="\n" if finished else "", file=sys.stderr, flush=True)
        else:
            log(line)
