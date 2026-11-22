"""`check`: read an SD card and report what is missing, wrong or risky."""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import string
import struct
import sys
import time
from pathlib import Path

from . import abi, battery
from .common import GAME_EXTENSIONS, JUNK, Failure, is_junk, parse_ini
from .components import load_lock, safe_relpath
from .install import PROTECTED, RECORD, filesystem, looks_like_card

# Sysmodules that answer anyone on the network once they run: title id, name, what that lets others do.
LISTENERS = (
    ("430000000000000B", "sys-botbase", "anyone on the network can control the console"),
    ("420000000000000E", "sys-ftpd", "anyone on the network can read and change the card's files"),
    ("00FF0000A53BB665", "SysDVR", "in its network modes anyone on the network can watch the screen"),
)


def mounted_cards() -> list[Path]:
    """Mounted volumes that look like a hekate/Atmosphère SD card."""
    if sys.platform == "win32":
        candidates = [Path(f"{letter}:\\") for letter in string.ascii_uppercase]
    else:
        bases = [Path("/Volumes")]
        if sys.platform != "darwin":
            bases = [Path("/media"), Path("/mnt")]
            with contextlib.suppress(KeyError, OSError):  # a user id without an account, as in a container
                user = getpass.getuser()
                bases = [Path("/run/media") / user, Path("/media") / user, *bases]
        candidates = []
        for base in bases:
            with contextlib.suppress(OSError):
                candidates += sorted(p for p in base.iterdir() if p.is_dir())
    cards = []
    for path in candidates:
        with contextlib.suppress(OSError):
            if looks_like_card(path):
                cards.append(path)
    return cards


def one_mounted_card(otherwise: str) -> Path:
    """The Switch card that is mounted, when it is the only one; `otherwise` says how to name a card."""
    cards = mounted_cards()
    if not cards:
        raise Failure(f"no mounted Switch card found; {otherwise}")
    if len(cards) > 1:
        raise Failure("several Switch cards are mounted (" + ", ".join(map(str, cards)) + f"); {otherwise}")
    return cards[0]


def read_bytes(path: Path) -> bytes | None:
    """The file's content, or None when there is no such file. A file that cannot be read is an error."""
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


def read_text(path: Path) -> str | None:
    data = read_bytes(path)
    return None if data is None else data.decode("utf-8", errors="replace")


def last_boot(root: Path) -> tuple[dict[str, str], list[str]]:
    """What sys-patch logged at the last CFW boot: its [stats], and the patches it could not apply."""
    text = read_text(root / "config/sys-patch/log.ini")
    if text is None:
        return {}, []
    stats: dict[str, str] = {}
    failed = []
    for name, keys in parse_ini(text):
        if name.lower() == "stats":
            stats = keys
            continue
        failed += [f"{name}/{key}" for key, value in keys.items() if value.lower().startswith(("unpatched", "failed"))]
    return stats, failed


def version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


# Old-ABI programs that ship with a component but do not run unless asked: Breeze's spare homebrew
# menus, which only its optional menu-swapping profile starts. They are named, not warned about.
SPARE_MENUS = re.compile(r"^switch/breeze/(reeze/)?hbmenu\.nro$", re.IGNORECASE)


def programs(root: Path) -> list[Path]:
    """Homebrew apps, overlays and sysmodules: where they live, without walking game or Windows folders."""
    found = [p for p in root.glob("*.nro") if not is_junk(p.name)]
    found += [p for p in root.glob("atmosphere/contents/*/exefs.nsp")]
    for pattern in ("switch/*", "switch/*/*", "switch/*/*/*"):
        found += [p for p in root.glob(pattern)
                  if p.suffix.lower() in (".nro", ".ovl") and not is_junk(p.name) and p.is_file()]
    return sorted(set(found))


def hekate_version(path: Path) -> str | None:
    """hekate keeps "ICTC" and its version digits at 0x118; the Hekatos fork names itself too."""
    data = read_bytes(path)
    if data is None:
        return None
    digits = data[0x11C:0x11F]
    if data[0x118:0x11C] != b"ICTC" or not digits.isdigit():
        return None
    return f"{'Hekatos' if b'Hekatos' in data else 'hekate'} {'.'.join(chr(d) for d in digits)}"


def check(root: Path, on_card: bool = True, scan_abi: bool = True) -> list[tuple[str, str]]:
    """Findings as (level, text). A card that cannot be read is a Failure, not a list of false alarms.

    `on_card` False is for a copy of a card's files, whose file system says nothing about the card;
    `scan_abi` False is for a copy that holds the programs' names but not their content.
    """
    try:
        return _check(root, on_card, scan_abi)
    except PermissionError as e:
        if sys.platform == "darwin":
            raise Failure(f"macOS does not let this program read the card ({e.filename}). Allow it under System "
                          "Settings > Privacy & Security > Files and Folders > Removable Volumes, then try "
                          "again.") from None
        raise Failure(f"no permission to read {e.filename}") from None
    except OSError as e:
        raise Failure(f"cannot read {e.filename or root}: {e.strerror}. The card may be failing.") from None


def _check(root: Path, on_card: bool, scan_abi: bool) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(level: str, text: str) -> None:
        out.append((level, text))

    def has(rel: str) -> bool:
        return (root / rel).is_file()

    fs = filesystem(root) if on_card else None
    if fs and ("fat32" in fs or fs in ("vfat", "msdos")):
        add("OK", f"file system: {fs}")
    elif fs:
        add("WARN", f"file system is {fs}; hekate and Horizon are most reliable on FAT32")

    if has("payload.bin"):
        add("OK", f"payload.bin for the modchip: {hekate_version(root / 'payload.bin') or 'not hekate'}")
    else:
        add("FAIL", "payload.bin is missing: a modchip then has nothing to start, and the console boots OFW")
    for rel in ("bootloader/update.bin", "bootloader/sys/nyx.bin"):
        if not has(rel):
            add("FAIL", f"{rel} is missing: hekate is incomplete")
    for rel in ("atmosphere/package3", "atmosphere/stratosphere.romfs"):
        if not has(rel):
            add("FAIL", f"{rel} is missing: Atmosphère is incomplete")
    if not has("hbmenu.nro"):
        add("WARN", "hbmenu.nro is missing: no homebrew menu")

    ini = read_text(root / "bootloader/hekate_ipl.ini")
    if ini is None:
        add("FAIL", "bootloader/hekate_ipl.ini is missing: hekate has no boot entries")
        sections = []
    else:
        sections = parse_ini(ini)
        names = [name for name, _ in sections if name.lower() != "config"]
        add("OK", "hekate boot entries: " + (", ".join(names) or "none"))
        for name, keys in sections:
            cfw = "pkg3" in keys or "fss0" in keys
            if cfw and keys.get("emummc_force_disable") == "1" and keys.get("stock") != "1":
                add("WARN", f"[{name}] runs CFW on the console's own NAND; use CFW (emuMMC) and stock OFW instead")
            if keys.get("ofw") == "1":
                add("WARN", f"[{name}] uses ofw=1, which upstream hekate does not know; use Reboot > OFW")
            if cfw and "kernel" in keys:
                add("WARN", f"[{name}] boots its own kernel ({keys['kernel']}), built for one Atmosphère version. "
                            "If it stops booting after an Atmosphère update, redo the setup that added it "
                            "(Autorun: Settings > System > Quick setup); the other entries are not affected.")

    emu = read_text(root / "emuMMC/emummc.ini")
    if emu is None:
        add("WARN", "no emuMMC/emummc.ini: create the emuMMC in hekate (emuMMC > Create emuMMC)")
    else:
        settings = next((keys for name, keys in parse_ini(emu) if name.lower() == "emummc"), {})
        try:
            sector: int | None = int(settings.get("sector", "0"), 16)  # as hekate reads it
        except ValueError:
            sector = None
        if not any(k.get("emummcforce") == "1" for _, k in sections):
            add("FAIL", "an emuMMC exists but no hekate entry boots it with emummcforce=1")
        folder = settings.get("path", "emuMMC/RAW1").strip("/")
        if sector is None:
            add("FAIL", f"emuMMC: emummc.ini has no usable sector ({settings.get('sector')!r})")
        elif sector and not safe_relpath(folder):
            add("FAIL", f"emuMMC: emummc.ini names the folder {settings.get('path')!r}, which is not on the card")
        elif sector:
            raw = root / folder / "raw_based"
            data = read_bytes(raw)
            based = struct.unpack("<I", data)[0] if data is not None and len(data) == 4 else None
            if based == sector:
                add("OK", f"emuMMC: partition-based at sector {sector:#x}, raw_based agrees")
            else:
                add("FAIL", f"emuMMC: emummc.ini says sector {sector:#x} but {raw.relative_to(root).as_posix()} says "
                            f"{'nothing' if based is None else hex(based)}")
        else:
            add("OK", f"emuMMC: file-based in {settings.get('path', '?')}")

    exo = read_text(root / "exosphere.ini") or ""
    if re.search(r"(?m)^\s*blank_prodinfo_emummc\s*=\s*1", exo):
        add("OK", "exosphere.ini blanks PRODINFO in the emuMMC")
    else:
        add("WARN", "exosphere.ini does not blank PRODINFO in the emuMMC (blank_prodinfo_emummc=1)")
    settings_ini = read_text(root / "atmosphere/config/system_settings.ini") or ""
    hosts = "".join(read_text(root / f"atmosphere/hosts/{n}") or "" for n in ("default.txt", "emummc.txt"))
    if re.search(r"(?m)^\s*enable_dns_mitm\s*=\s*u8!0x1", settings_ini) and "nintendo" in hosts.lower():
        add("OK", "Nintendo's servers are blocked under CFW (DNS redirection and hosts file)")
    else:
        add("WARN", "Nintendo's servers are not blocked under CFW; do not go online there")

    if has("atmosphere/contents/420000000000000B/exefs.nsp"):
        add("OK", "sys-patch is installed")
    else:
        add("WARN", "sys-patch is missing: unsigned titles will not install or start")
    if not has("switch/daybreak.nro"):
        add("WARN", "Daybreak is missing: needed to update the emuMMC's firmware")

    try:
        lock = load_lock()
    except (ValueError, OSError, Failure):
        lock = {}
        add("WARN", "switchsd's own component lock is unreadable, so versions are not compared")
    stats, unpatched = last_boot(root)
    firmware = version_tuple(stats.get("fw_version", ""))
    if firmware:
        where = "emuMMC" if stats.get("is_emummc") == "1" else "sysMMC"
        running = stats.get("ams_version", "?")
        add("OK", f"last CFW boot ({where}): firmware {stats['fw_version']}, Atmosphère {running}, per "
                  "sys-patch's log")
        pinned = lock.get("atmosphere", {})
        newest = version_tuple(str(pinned.get("firmware", "")))
        if newest and running == pinned.get("tag"):
            if firmware < newest:
                add("OK", f"firmware: Atmosphère {running} runs up to {pinned['firmware']}; updating from "
                          f"{stats['fw_version']} is optional")
            elif firmware > newest:
                add("WARN", f"firmware {stats['fw_version']} is newer than Atmosphère {running} supports "
                            f"({pinned['firmware']}); CFW may not boot until Atmosphère has a release for it")
    if unpatched:
        add("WARN", "sys-patch could not apply " + ", ".join(unpatched) + " at the last boot; it may need an update "
                    "for this firmware")

    found = programs(root)
    old = [p.relative_to(root).as_posix() for p in found if scan_abi and abi.revision(p) == 0]
    spare = [p for p in old if SPARE_MENUS.match(p)]
    old = [p for p in old if p not in spare]
    if not scan_abi:
        add("OK", f"{len(found)} apps, overlays and sysmodules found; their homebrew ABI was not read")
    elif old:
        shown = ", ".join(old[:8]) + (f" and {len(old) - 8} more" if len(old) > 8 else "")
        if firmware and firmware < (21, 0, 0):
            add("OK", f"{len(old)} of {len(found)} programs use the homebrew ABI from before firmware 21; fine on "
                      f"{stats['fw_version']}")
        else:
            add("WARN", f"{len(old)} of {len(found)} programs are built for the homebrew ABI from before firmware 21 "
                        f"and may crash or misbehave on 21 and newer: {shown}")
    elif found:
        add("OK", f"all {len(found) - len(spare)} apps, overlays and sysmodules checked are built for the current "
                  "homebrew ABI" + (", apart from Breeze's spare homebrew menus, which only its optional "
                                    "menu-swapping profile starts" if spare else ""))

    themes = root / "themes"
    if themes.is_dir() and any(p.suffix.lower() == ".ips" for p in themes.iterdir()):
        add("WARN", "theme patches lie in themes/; NXThemes Installer reads them from themes/systemPatches/")

    for title, name, risk in LISTENERS:
        if has(f"atmosphere/contents/{title}/flags/boot2.flag"):
            add("WARN", f"{name} starts at boot, so {risk}; switch it on in the Sysmodules overlay only while "
                        "you use it")
    flags = sorted(p.parent.parent.name for p in (root / "atmosphere/contents").glob("*/flags/boot2.flag"))
    level = "WARN" if len(flags) > 8 else "OK"
    add(level, f"{len(flags)} sysmodules start at boot" + (f": {', '.join(flags)}" if flags else ""))

    for rel in ("config/prodinforge/donor_prodinfo.bin", "config/prodinforge/donor.keys"):
        if has(rel):
            add("WARN", f"{rel} holds another console's identity; nothing needs it unless ProdinForge is run")
    if list((root / "atmosphere/automatic_backups").glob("*PRODINFO*")):
        add("OK", "Atmosphère's backup of the console's PRODINFO is on the card; keep it, never share it")

    # What Atmosphère writes down when a program or the system itself crashes.
    crashes = [p for pattern in ("atmosphere/crash_reports/*.log", "atmosphere/fatal_reports/*.log",
                                 "atmosphere/fatal_errors/report_*.bin") for p in root.glob(pattern) if p.is_file()]
    if crashes:
        latest = time.strftime(", the newest from %Y-%m-%d", time.localtime(max(p.stat().st_mtime for p in crashes)))
        add("WARN", f"Atmosphère has left {len(crashes)} crash reports on the card{latest if on_card else ''}. One "
                    "now and then is normal; many point at a sysmodule, a cheat or an overclock.")
    else:
        add("OK", "Atmosphère has left no crash reports on the card")

    dump = battery.newest_dump(root)
    gauge = battery.parse(dump.read_bytes()) if dump else None
    if dump and gauge is None:
        add("WARN", "hekate's fuel gauge dump on the card is not one of a configured gauge; make it again in hekate")
    elif gauge:
        made = time.strftime(" of %Y-%m-%d", time.localtime(dump.stat().st_mtime)) if dump and on_card else ""
        young = gauge.cycles < battery.LEARNING
        cycles = f"{gauge.cycles:.1f}" if young else f"{gauge.cycles:.0f}"
        text = (f"battery, per hekate's fuel gauge dump{made}: holds {gauge.full:.0f} of the {gauge.designed:.0f} mAh "
                f"the console is set for ({gauge.health:.0f} %), after {cycles} charge cycles")
        if young:  # the count restarts when the battery is unplugged; the gauge then starts from the designed value
            add("OK", text + ". So few cycles mean the gauge was reset not long ago, as when a battery is unplugged, "
                             "and is still learning this one.")
        elif gauge.health < battery.WORN:
            add("WARN", text + "; it is worn, and play time is short")
        elif gauge.health > 105:
            add("OK", text + ". More than the console is set for: a bigger battery, or a gauge that is still learning.")
        else:
            add("OK", text)

    junk = 0
    for dirpath, dirnames, filenames in os.walk(root):
        top = os.path.samefile(dirpath, root)
        dirnames[:] = [d for d in dirnames if d not in JUNK and not (top and PROTECTED.match(d))]
        junk += sum(1 for f in filenames if f.startswith("._") or f == ".DS_Store")
    if junk:
        add("WARN", f"{junk} macOS ._ or .DS_Store files; `switchsd.py setup` removes them")

    games = root / "games"
    if games.is_dir():
        count = sum(1 for p in games.rglob("*") if p.suffix.lower() in GAME_EXTENSIONS and not is_junk(p.name))
        add("OK", f"games/: {count} installable files")

    if (root / "switch/tico/tico.nro").is_file():
        modules = root / "tico" / "modules"
        count = sum(1 for p in modules.iterdir() if p.is_dir() and not is_junk(p.name)) if modules.is_dir() else 0
        if count:
            add("OK", f"tico: {count} emulators, installed from its Store; games go in tico/roms/<console>/")
        else:
            add("WARN", "tico has no emulators yet: start it from its icon on the home menu (in sphaira, Options > "
                        "Install Forwarder makes one) and install them under Store > Apps")

    record = read_text(root / RECORD)
    if record:
        try:
            installed = json.loads(record).get("components", {})
            stale = [f"{cid} {info.get('version')} -> {lock[cid]['tag']}" for cid, info in installed.items()
                     if cid in lock and lock[cid]["tag"] != info.get("version")]
            add("OK", f"set up by switchsd: {len(installed)} components")
            if stale:
                add("WARN", "newer versions pinned: " + ", ".join(stale) + "; run setup again")
        except (ValueError, AttributeError, KeyError):
            add("WARN", f"{RECORD} is unreadable")
    return out
