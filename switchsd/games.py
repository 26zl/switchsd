"""`games`: copy your own game files into the card's games/ folder, where the installers look.

FAT32 holds no file of 4 GiB or more. Horizon reads a folder that carries the archive attribute as
one file, made of the parts 00, 01, ... inside it, each 0xFFFF0000 bytes long except the last. A
bigger file is stored that way. Linux and Windows can set that attribute on a mounted card; macOS
cannot, and there hekate's Fix Archive Bit does it, which marks every folder that holds a file 00.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import shutil
import sys
from array import array
from pathlib import Path
from typing import BinaryIO

from .common import CHUNK, EXIT_OK, GAME_EXTENSIONS, Failure, Mismatch, Progress, Usage, gb, log, plain
from .doctor import one_mounted_card
from .fat32 import ATTR_ARCHIVE, BAD_LONG
from .install import Links, check_target

FOLDER = "games"
LIMIT = 0xFFFFFFFF  # the biggest file FAT32 holds
PART = 0xFFFF0000  # what Horizon makes each part of a file that is stored as a folder
TMP = ".switchsd-tmp"
MARGIN = 16 << 20  # room left on the card for the file system's own bookkeeping
HEKATE_FIX = "Tools > Arch bit (the second tab) > Fix Archive Bit"
FAT_IOCTL_GET_ATTRIBUTES, FAT_IOCTL_SET_ATTRIBUTES = 0x80047210, 0x40047211  # Linux, <linux/msdos_fs.h>


def card_root(given: str | None, force: bool) -> Path:
    if given:
        root = Path(given)
        check_target(root, force or (root / FOLDER).is_dir())  # a card that holds nothing but games yet is one too
        return root
    return one_mounted_card("name the card's folder with --card")


def card_name(source: Path) -> str:
    """The name the file gets on the card, which is its own."""
    name = source.name
    if source.suffix.lower() not in GAME_EXTENSIONS:
        raise Usage(f"{name} is not a game file ({', '.join(GAME_EXTENSIONS)})")
    if set(name) & BAD_LONG or any(ord(c) < 32 for c in name) or name != name.strip(" ") or name.endswith(".") \
            or len(name.encode("utf-16-le")) > 510:
        raise Usage(f"{name!r} cannot be a file name on a FAT32 card (none of \" * / : < > ? \\ |); rename the file")
    return name


def parts_of(size: int) -> list[int]:
    """How a file of this size is stored: whole, or in Horizon's parts when FAT32 cannot hold it."""
    if size <= LIMIT:
        return [size]
    full, rest = divmod(size, PART)
    return [PART] * full + ([rest] if rest else [])


def part_names(count: int) -> list[str]:
    return [f"{number:02d}" for number in range(count)]


def is_split(path: Path) -> bool:
    return path.is_dir() and path.suffix.lower() in GAME_EXTENSIONS and (path / "00").is_file()


def on_card(target: Path, sizes: list[int]) -> bool:
    """True if the file is on the card already, whole. Something else under its name is a Failure."""
    if not (target.exists() or target.is_symlink()):
        return False
    if len(sizes) == 1:
        same = target.is_file() and target.stat().st_size == sizes[0]
    else:
        found = {p.name: p.stat().st_size for p in target.iterdir() if not p.name.startswith("._")} \
            if target.is_dir() else {}
        same = found == dict(zip(part_names(len(sizes)), sizes))
    if not same:
        raise Failure(f"{FOLDER}/{target.name} is on the card already, and is not this file; delete it there first")
    return True


def uncached(fd: int) -> None:
    """Keep this file out of the system's cache, so that reading it back asks the card."""
    with contextlib.suppress(OSError, AttributeError):
        if sys.platform == "darwin":
            import fcntl  # noqa: PLC0415 - not on Windows
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        elif hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def write_part(src: BinaryIO, path: Path, size: int, digest: hashlib._Hash, progress: Progress) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o666), "wb") as out:
        uncached(out.fileno())
        left = size
        while left:
            block = src.read(min(CHUNK, left))
            if not block:
                raise Failure(f"{src.name} shrank while it was being copied; run the command again")
            digest.update(block)
            out.write(block)
            left -= len(block)
            progress.add(len(block))
        out.flush()
        os.fsync(out.fileno())
        uncached(out.fileno())


def read_back(files: list[Path], total: int, label: str) -> bytes:
    digest, progress = hashlib.sha256(), Progress(label, max(total, 1))
    for path in files:
        with open(path, "rb") as f:
            uncached(f.fileno())
            for block in iter(lambda: f.read(CHUNK), b""):
                digest.update(block)
                progress.add(len(block))
    return digest.digest()


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def copy(source: Path, target: Path, sizes: list[int], verify: bool) -> None:
    """Copy under a temporary name, read the copy back, and only then give it its name."""
    tmp = target.with_name(target.name + TMP)
    remove(tmp)  # left by a run that was interrupted
    digest = hashlib.sha256()
    progress = Progress(f"copying {source.name}", max(sum(sizes), 1))
    try:
        with open(source, "rb") as src:
            if len(sizes) == 1:
                written = [tmp]
                write_part(src, tmp, sizes[0], digest, progress)
            else:
                tmp.mkdir()
                written = [tmp / name for name in part_names(len(sizes))]
                for path, size in zip(written, sizes):
                    write_part(src, path, size, digest, progress)
            if src.read(1):
                raise Failure(f"{source} grew while it was being copied; run the command again")
        if verify and read_back(written, sum(sizes), f"checking {source.name}") != digest.digest():
            raise Mismatch(f"{FOLDER}/{target.name} reads back different from {source}; the card may be faulty. "
                           "The copy was removed.")
        os.replace(tmp, target)
    except BaseException:
        remove(tmp)
        raise
    for junk in (target.with_name("._" + target.name), target.with_name("._" + tmp.name),
                 *(target.glob("._*") if target.is_dir() else ())):
        with contextlib.suppress(OSError):  # what macOS adds beside a file it has notes on
            junk.unlink()


def archive_state(folder: Path) -> bool | None:
    """Whether a folder carries the archive attribute; None where a mounted card does not say."""
    try:
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415 - Windows only
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(folder))  # type: ignore[attr-defined]
            return None if attrs == 0xFFFFFFFF else bool(attrs & ATTR_ARCHIVE)
        if sys.platform.startswith("linux"):
            import fcntl  # noqa: PLC0415 - not on Windows
            fd = os.open(folder, os.O_RDONLY)
            try:
                attrs = array("I", [0])
                fcntl.ioctl(fd, FAT_IOCTL_GET_ATTRIBUTES, attrs, True)
                return bool(attrs[0] & ATTR_ARCHIVE)
            finally:
                os.close(fd)
    except (OSError, AttributeError):
        pass  # not a FAT volume, or a system that does not answer
    return None


def set_archive(folder: Path) -> bool:
    """Make Horizon read the folder as one file. False where this system cannot do that on a mounted card."""
    try:
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415 - Windows only
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            attrs = kernel32.GetFileAttributesW(str(folder))
            return attrs != 0xFFFFFFFF and bool(kernel32.SetFileAttributesW(str(folder), attrs | ATTR_ARCHIVE))
        if sys.platform.startswith("linux"):
            import fcntl  # noqa: PLC0415 - not on Windows
            fd = os.open(folder, os.O_RDONLY)
            try:
                attrs = array("I", [0])
                fcntl.ioctl(fd, FAT_IOCTL_GET_ATTRIBUTES, attrs, True)
                attrs[0] |= ATTR_ARCHIVE
                fcntl.ioctl(fd, FAT_IOCTL_SET_ATTRIBUTES, attrs)
                return True
            finally:
                os.close(fd)
    except (OSError, AttributeError):
        pass
    return False


def waiting(folder: Path) -> list[tuple[str, int, int]]:
    """The game files in the folder as (name, bytes, parts): 1 part for a plain file."""
    found = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()) if folder.is_dir() else []:
        if path.name.startswith("._") or path.name.endswith(TMP):
            continue
        if is_split(path):
            sizes = [p.stat().st_size for p in path.iterdir() if p.name.isdigit()]
            found.append((path.name, sum(sizes), len(sizes)))
        elif path.is_file() and path.suffix.lower() in GAME_EXTENSIONS:
            found.append((path.name, path.stat().st_size, 1))
    return found


def mark_split(folder: Path, dry_run: bool) -> list[str]:
    """Set the archive attribute on every split file; returns the ones this system could not mark."""
    left = []
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        if is_split(path) and not archive_state(path) and (dry_run or not set_archive(path)):
            left.append(path.name)
    return left


def cmd_games(args: argparse.Namespace) -> int:
    sources = {card_name(Path(given)).lower(): Path(given) for given in args.files}  # before any look at a card
    if len(sources) < len(args.files):
        raise Usage("two of the files have the same name, and the card's folder holds each name once")
    root = card_root(args.card, args.force)
    folder = root / FOLDER
    links = Links(root)
    links.vet(folder / "x", f"{FOLDER}/")
    if folder.exists() and not folder.is_dir():
        raise Failure(f"{folder} is a file; the card's {FOLDER} folder cannot be made")

    plan = []
    for source in sources.values():
        if not source.is_file():
            raise Failure(f"{source} is not a file")
        sizes = parts_of(source.stat().st_size)
        links.vet(folder / source.name, f"{FOLDER}/{source.name}")
        plan.append((source, folder / source.name, sizes, on_card(folder / source.name, sizes)))
    todo = [(source, target, sizes) for source, target, sizes, present in plan if not present]
    need, free = sum(sum(sizes) for _, _, sizes in todo), shutil.disk_usage(root).free
    if todo and need + MARGIN > free:
        raise Failure(f"the files take {gb(need)}, but only {gb(free)} is free on {root}")

    for source, target, sizes, present in plan:
        how = f"in {len(sizes)} parts, because FAT32 holds no file of 4 GB or more" if len(sizes) > 1 else "as it is"
        if present:
            log(f"{target.name}: already on the card")
        elif args.dry_run:
            log(f"would copy {target.name} ({gb(sum(sizes))}) {how}")
        else:
            folder.mkdir(exist_ok=True)
            log(f"{target.name}: {gb(sum(sizes))}, {how}")
            copy(source, target, sizes, not args.no_verify)

    unmarked = mark_split(folder, args.dry_run)
    for name, size, parts in waiting(folder):
        print(plain(f"{gb(size):>9}  {name}" + (f"  ({parts} parts)" if parts > 1 else "")))
    if args.dry_run:
        return EXIT_OK
    if not waiting(folder):
        print(f"no game files in {folder}")
        return EXIT_OK
    steps = []
    if unmarked:
        steps.append(f"In hekate: {HEKATE_FIX}. That marks every file that lies in parts, so that the console reads "
                     "it as one; this computer cannot do it on a mounted card.")
    steps.append("In CFW: DBI > Browse SD card > games, and install to the SD card. Delete a file from the card's "
                 "games folder once it is installed.")
    print("Eject the card safely and put it in the console. Then:")
    for number, step in enumerate(steps, 1):
        print(f"  {number}. {step}")
    return EXIT_OK


def add_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("games", help="copy your own game files to the card's games folder, splitting those of "
                                     "4 GB and more so that FAT32 holds them; without files, list what waits there")
    p.add_argument("files", nargs="*", metavar="FILE", help=", ".join(GAME_EXTENSIONS) + " files")
    p.add_argument("--card", help="the card's mount point; without it, the one mounted Switch card")
    p.add_argument("--force", action="store_true", help="use a folder that does not look like a Switch card")
    p.add_argument("--dry-run", action="store_true", help="show what would be copied and stop")
    p.add_argument("--no-verify", action="store_true", help="skip reading each copy back to compare it")
    p.set_defaults(func=cmd_games)
