"""Command line: one entry point for setting up, checking and migrating Switch SD cards."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .common import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_MISMATCH, EXIT_OK, Failure, Usage, gb, log, nfiles, plain
from .blockdev import SECTOR
from .components import (LOCK, Changed, Component, fetch, github_token, load_lock, load_manifest, newest_tag,
                         resolve_latest, select, verify_pin)
from .disks import check_disk
from .doctor import check, last_boot, mounted_cards, one_mounted_card, version_tuple
from .fat32 import check_fits, label_bytes
from .games import add_commands as games_commands
from .install import (RECORD, Item, Journal, Links, apply, build_plan, check_target, clean_legacy, filesystem,
                      make_record, read_record, remove_appledouble, summarize, undo, undoable)
from .autocheck import add_commands as autocheck_commands
from .mbr import EMUMMC_TYPE, emummc_mib, new_layout
from .migrate import add_commands as raw_commands
from .migrate import build_card, confirm, disk_args, row, tree_from_files, write_args
from .remote import add_commands as remote_commands

HEKATE_KEEPS = 1 << 30  # hekate's partition manager carries a card's files over up to about 1.2 GB

EXAMPLES = """\
set up or update a card (any OS; the card is mounted as a folder):
  python3 switchsd.py setup                               the one mounted Switch card
  python3 switchsd.py setup "/Volumes/SWITCH SD"          macOS
  python3 switchsd.py setup /run/media/$USER/SWITCH       Linux
  py switchsd.py setup E:\\                                 Windows
  python3 switchsd.py setup E: --with sys-con,checkpoint --without moonlight
  python3 switchsd.py undo                                put back what the last setup changed
check a card, and see what has a newer release:
  python3 switchsd.py check                               every mounted Switch card
  python3 switchsd.py updates
put your own game files on the card, for DBI to install:
  python3 switchsd.py games Game.nsp Update.nsp
build a new card, back one up, bring it back (raw: sudo, or an administrator terminal on Windows):
  python3 switchsd.py disks                               the cards, with the size the next commands ask for
  sudo python3 switchsd.py new disk6 --expect-bytes 250139901952 --emummc full
  sudo python3 switchsd.py image disk6 card.swimg
  sudo python3 switchsd.py restore card.swimg disk6 --expect-bytes 250139901952
control the console over Wi-Fi (switch sys-botbase on in the Sysmodules overlay first):
  python3 switchsd.py remote 192.168.1.50 screenshot screen.jpg
  python3 switchsd.py remote 192.168.1.50 click HOME A
see every component:
  python3 switchsd.py list

exit codes: 0 success, 1 error, 2 a command line that cannot be right (unknown option, component or button,
value out of range), 3 check or verification found a problem, 130 interrupted
"""


def ids(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_list(args: argparse.Namespace) -> int:
    lock = load_lock() if LOCK.exists() else {}
    for comp in load_manifest():
        pin = lock.get(comp.id, {}).get("tag", "-")
        flag = "*" if comp.default else " "
        print(f"{flag} {comp.id:<20} {comp.category:<11} {str(pin)[:16]:<16} {comp.about}")
    print("\n* = installed by default; add others with --with, leave defaults out with --without")
    return EXIT_OK


def already_installed(root: Path, manifest: list, args: argparse.Namespace) -> list[str]:
    """Optional components an earlier setup put on this card: they stay selected, so they keep getting updates."""
    if args.only_ids:
        return []
    known = {c.id: c for c in manifest}
    recorded = read_record(root).get("components", {})
    return sorted(cid for cid in recorded
                  if cid in known and not known[cid].default and cid not in args.with_ids
                  and cid not in args.without_ids)


def resolve_plan(manifest: list[Component], args: argparse.Namespace,
                 present: list[str]) -> tuple[list[Component], dict, dict[str, Item], dict[str, str]]:
    """The selected components, their pinned (or newest) releases downloaded, every file they install,
    and the components left out, with the reason.

    A project can replace a file under a release after it was pinned. Such a component is left out and
    the rest goes ahead, unless it is one a card needs to start CFW (category core): then nothing does.
    """
    comps = select(manifest, args.with_ids + present, args.without_ids, args.only_ids)
    if args.latest:
        log("--latest: resolving the newest releases instead of the pinned ones")
        pins = {c.id: resolve_latest(c) for c in comps}
    else:
        lock = load_lock()
        missing = [c.id for c in comps if c.id not in lock]
        if missing:
            raise Failure(f"no pinned release for {', '.join(missing)}; run `switchsd.py lock` or use --latest")
        pins = {c.id: lock[c.id] for c in comps}
    fetched, left_out = {}, {}
    for comp in comps:
        tag = pins[comp.id]["tag"]
        log(f"  {comp.id} {tag}")
        try:
            fetched[comp.id] = fetch(comp, pins[comp.id])
        except Changed as e:
            if comp.category == "core":
                raise Changed(f"{comp.id} {tag}: {e}. Its project changed the release after it was pinned, and a card "
                              f"needs {comp.id} to start CFW, so nothing was changed. Once the pin is updated, `git "
                              f"pull` and run this again (maintainers: look at the release, then `switchsd.py lock "
                              f"{comp.id}`)") from None
            log(f"    left out: {e}")
            left_out[comp.id] = "its project changed the release on GitHub after it was pinned"
    added = True
    while added:  # what needs a component that is left out is left out with it
        added = False
        for comp in comps:
            need = next((n for n in comp.requires if n in left_out), None)
            if need and comp.id not in left_out:
                left_out[comp.id] = f"it needs {need}, which is left out"
                added = True
    comps = [c for c in comps if c.id not in left_out]
    return comps, pins, build_plan(comps, pins, fetched), left_out


def left_out_note(left_out: dict[str, str], recorded: dict) -> str:
    """What setup or new could not take this time, and what brings it later."""
    lines = [f"  {cid} {'stays as it is on the card' if cid in recorded else 'is not on the card'}: {why}"
             for cid, why in left_out.items()]
    return ("Left out, because nothing unchecked goes on a card:\n" + "\n".join(lines)
            + "\nThe pins are checked every week; once they are updated, `git pull` and run setup again.")


def in_place(target: Path, item: Item) -> bool:
    """True if the file is there and will not grow: kept as it is, or already of the size to be written."""
    return target.is_file() and (item.keep or target.stat().st_size == item.size)


def supports_the_cards_firmware(root: Path, pins: dict) -> None:
    """Stop before an Atmosphère goes on the card that cannot start the firmware the card last booted."""
    pin = pins.get("atmosphere", {})
    supported = version_tuple(str(pin.get("firmware", "")))
    booted = last_boot(root)[0].get("fw_version", "")
    if supported and (version_tuple(booted) or supported) > supported:
        raise Failure(f"the console last booted firmware {booted} from this card, and Atmosphère {pin['tag']}, which "
                      f"this would install, runs up to {pin['firmware']}: CFW would stop starting. `switchsd.py "
                      "updates` shows whether a newer Atmosphère is out, and `setup --latest` takes it.")


EMMC_BACKUP = ("Tools > Backup eMMC > eMMC BOOT0 & BOOT1, then eMMC RAW GPP. Copy the card's backup/ folder to the "
               "computer before going on")


def next_steps(root: Path, written: list[str]) -> list[str]:
    """What only the console can do, once this setup is on the card: printed so nobody has to guess."""
    steps = []
    if not (root / "emuMMC" / "emummc.ini").is_file():
        steps.append(f"start hekate from this card and back up the console's own storage, if you have no backup "
                     f"yet: {EMMC_BACKUP}")
        steps.append("create the emuMMC: emuMMC > Create emuMMC, then Launch > CFW (emuMMC)")
    if "switch/tico/tico.nro" in written:
        steps.append("put tico on the home menu: in the homebrew menu (Album), start sphaira, highlight tico and "
                     "choose Options (X) > Install Forwarder > Create Forwarder")
        steps.append("start tico from the home menu and install the emulators you want under Store > Apps")
    return steps


def cmd_setup(args: argparse.Namespace) -> int:
    if args.target:
        root = Path(args.target)
    else:
        root = one_mounted_card("give the card's folder, such as /Volumes/SWITCH SD, /run/media/you/SWITCH or E:\\")
        log(f"setting up {root}")
    check_target(root, args.force)
    fs = filesystem(root)
    if fs and not ("fat32" in fs or fs in ("vfat", "msdos")):
        log(f"note: {root} is {fs}, not FAT32. Fine for a staging folder; on a card, FAT32 is what hekate "
            "and Horizon handle best (`switchsd.py format` makes one, on any OS).")
    manifest = load_manifest()
    present = already_installed(root, manifest, args)
    if present:
        log("also updating what is already on the card: " + ", ".join(present))
    comps, pins, plan, left_out = resolve_plan(manifest, args, present)
    recorded = read_record(root).get("components", {})
    supports_the_cards_firmware(root, pins)
    need = sum(item.size for item in plan.values() if not in_place(root / item.dest, item))
    free = shutil.disk_usage(root).free
    if need > free:
        raise Failure(f"needs up to {gb(need)}, but only {gb(free)} is free on {root}")
    templates = {item.dest.lower() for item in plan.values() if item.component == "template"}
    reset = templates if args.reset_config else {p.lower() for p in args.reset}
    unknown = sorted(reset - templates)
    if unknown:
        raise Usage(f"--reset: {', '.join(unknown)} is not a settings file switchsd provides; it provides "
                    + ", ".join(sorted(templates)))
    journal = Journal(root, Links(root))
    if args.clean:
        for line in clean_legacy(root, args.dry_run, journal):
            log(f"{'would remove' if args.dry_run else 'removed'} {line}")
    report = apply(root, plan, comps, pins, reset=reset, prune=args.prune, dry_run=args.dry_run, journal=journal,
                   leave=set(left_out))
    if not args.dry_run:
        removed = remove_appledouble(root)
        if removed:
            log(f"removed {removed} ._ files macOS created for its own file attributes")
    summarize(report, plan, args.dry_run)
    if not args.dry_run:
        print(f"{root} is set up with {len(comps)} components" + (": `switchsd.py undo` takes this run back"
                                                                   if report.backup else "")
              + ". Eject it safely before taking it out.")
    if left_out:
        print(left_out_note(left_out, recorded))
    if not args.dry_run:
        steps = next_steps(root, report.written)
        if steps:
            print("Then, on the console:")
            for number, step in enumerate(steps, 1):
                print(f"  {number}. {step}")
    return EXIT_MISMATCH if left_out else EXIT_OK


def cmd_undo(args: argparse.Namespace) -> int:
    if args.target:
        root = Path(args.target)
    else:
        root = one_mounted_card("give the card's folder, such as /Volumes/SWITCH SD, /run/media/you/SWITCH or E:\\")
        log(f"undoing on {root}")
    check_target(root, False)
    done = undo(root, args.dry_run)
    if not args.dry_run:
        remove_appledouble(root)
    stamp = done.stamp
    when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
    verb = ("would put back", "would remove") if args.dry_run else ("put back", "removed")
    changes = ([f"{verb[0]} the {nfiles(len(done.restored))} it replaced or removed"] if done.restored else []) \
        + ([f"{verb[1]} the {nfiles(len(done.deleted))} it added"] if done.deleted else [])
    log(f"the setup of {when}: " + (" and ".join(changes) if changes else "nothing left to change"))
    if done.left:
        log("left as they are, because they changed after that setup: " + ", ".join(done.left[:8])
            + (f" and {len(done.left) - 8} more" if len(done.left) > 8 else ""))
    if not args.dry_run:
        print(f"{root} is back as it was before that setup" + (", apart from the files above" if done.left else "")
              + (". Running `undo` again takes back the setup before it." if undoable(root) else
                 ". No earlier setup left a backup, so there is nothing more to undo."))
    return EXIT_OK


def cmd_new(args: argparse.Namespace) -> int:
    emummc = emummc_mib(args.emummc)
    label_bytes(args.label)
    disk = check_disk(args.disk, args.expect_bytes, writing=True)
    parts = new_layout(disk.size // SECTOR, emummc)
    comps, pins, plan, left_out = resolve_plan(load_manifest(), args, [])
    files = {item.dest: item.load() for item in plan.values()}
    files[RECORD] = make_record(comps, pins, plan)
    root = tree_from_files(files)
    check_fits(parts[0].start, parts[0].size, root)
    need = sum(len(data) for data in files.values())
    log("new card:\n" + row("FAT32", parts[0]) + f"  label {args.label.upper()!r}, {len(files)} files, {gb(need)}")
    for part in parts[1:]:
        log(row("emuMMC", part) + "  empty, for hekate's Create emuMMC")
    if left_out:
        print(left_out_note(left_out, {}))
    done = EXIT_MISMATCH if left_out else EXIT_OK
    if args.dry_run:
        return done
    confirm(args, disk, "This erases everything")
    count = build_card(disk, parts, args.label, root, args.skip_probe)
    print(f"{disk.id} is ready: {count} files from {len(comps)} components. Eject it, put it in the Switch and "
          "let hekate start. Then, in hekate:")
    steps = [f"back up the console's own storage, if you have no backup yet: {EMMC_BACKUP}"]
    if not any(p.type == EMUMMC_TYPE for p in parts):
        kept = ("hekate carries the card's files over" if need < HEKATE_KEEPS
                else "hekate erases the card's files, so run `switchsd.py setup` on the card afterwards")
        steps.append(f"Tools > Partition SD Card: set the emuMMC size, Next Step, Start ({kept})")
    steps += ["emuMMC > Create emuMMC > SD Partition > Part 1, which copies the console's own system to the card",
              "Launch > CFW (emuMMC)"]
    for number, step in enumerate(steps, 1):
        print(f"  {number}. {step}")
    return done


def cmd_check(args: argparse.Namespace) -> int:
    if args.target:
        roots = [Path(args.target)]
        if not roots[0].is_dir():
            raise Failure(f"{roots[0]} is not a folder")
    else:
        roots = mounted_cards()
        if not roots:
            raise Failure("no mounted Switch card found; give its folder, such as /Volumes/SWITCH SD, "
                          "/run/media/you/SWITCH or E:\\")
    results = [(root, check(root)) for root in roots]
    if args.json:
        print(json.dumps([{"card": str(root), "findings": [{"level": level, "text": text}
                                                            for level, text in findings]}
                          for root, findings in results], indent=1))
    else:
        for root, findings in results:
            if not args.target:
                print(f"{root}:")
            for level, text in findings:
                print(plain(f"{level:<4} {text}"))
    failed = any(level == "FAIL" for _, findings in results for level, _ in findings)
    return EXIT_MISMATCH if failed else EXIT_OK


def cmd_updates(args: argparse.Namespace) -> int:
    comps = load_manifest()
    lock = load_lock()
    wanted = select(comps, [], [], args.component or [c.id for c in comps])
    if args.verify and not github_token():
        raise Failure("--verify reads checksums from GitHub's API, which allows 60 requests an hour without a "
                      "token: set GITHUB_TOKEN (for example GITHUB_TOKEN=$(gh auth token))")
    annotate = bool(os.environ.get("GITHUB_ACTIONS"))
    behind = broken = unchecked = 0
    for comp in wanted:
        pin = lock.get(comp.id)
        if pin is None:
            print(f"{comp.id}: not pinned yet")
            behind += 1
            continue
        newest = newest_tag(comp)
        if newest != pin["tag"]:
            behind += 1
            print(f"::warning::{comp.id}: pinned {pin['tag']}, newest is {newest}" if annotate
                  else f"{comp.id}: {pin['tag']} -> {newest}")
        problems, without = verify_pin(comp, pin) if args.verify else ([], 0)
        unchecked += without
        for problem in problems:
            broken += 1
            print(f"::error::{comp.id}: {problem}" if annotate else f"FAIL {comp.id}: {problem}")
    if broken:
        print(f"{broken} pinned files no longer match what GitHub publishes; look at those releases before trusting "
              "them, then run `switchsd.py lock`")
        return EXIT_MISMATCH
    print(f"{behind} of {len(wanted)} components have a newer release: `switchsd.py lock` pins them, then "
          "`setup` installs them" if behind else f"all {len(wanted)} components are pinned to their newest release")
    if args.verify:
        print("every pinned file that GitHub publishes a checksum for matches it"
              + (f"; {unchecked} more could not be compared, because GitHub publishes none" if unchecked else ""))
    return EXIT_OK


def cmd_lock(args: argparse.Namespace) -> int:
    comps = load_manifest()
    wanted = args.component or [c.id for c in comps]
    lock = load_lock() if LOCK.exists() else {}
    for comp in select(comps, [], [], wanted):
        old = lock.get(comp.id, {}).get("tag")
        lock[comp.id] = resolve_latest(comp)
        new = lock[comp.id]["tag"]
        log(f"  {comp.id}: {new}" + (f" (was {old})" if old and old != new else ""))
    known = {c.id for c in comps}
    lock = {k: lock[k] for k in sorted(lock) if k in known}
    LOCK.write_text(json.dumps(lock, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {LOCK.name}: {len(lock)} components pinned")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchsd.py", description=sys.modules["switchsd"].__doc__,
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("list", help="show every component, its pinned version and whether it is a default")
    p.set_defaults(func=cmd_list)

    def component_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--with", dest="with_ids", type=ids, default=[], metavar="IDS",
                       help="add components (comma list)")
        p.add_argument("--without", dest="without_ids", type=ids, default=[], metavar="IDS",
                       help="leave components out")
        p.add_argument("--only", dest="only_ids", type=ids, default=[], metavar="IDS", help="exactly these components")
        p.add_argument("--latest", action="store_true", help="newest releases instead of the pinned, verified ones")

    p = sub.add_parser("setup", help="install or update hekate, Atmosphère and the homebrew on a mounted card")
    p.add_argument("target", nargs="?", help="the card's mount point, or any folder to stage the files in; "
                                             "without it, the one mounted Switch card")
    component_args(p)
    p.add_argument("--reset-config", action="store_true",
                   help="replace hekate_ipl.ini, exosphere.ini and the other settings with the defaults "
                        "(the old ones are backed up to config/switchsd/backup)")
    p.add_argument("--reset", type=ids, default=[], metavar="FILES",
                   help="restore these settings files to the defaults, such as bootloader/hekate_ipl.ini "
                        "(the old ones are backed up to config/switchsd/backup)")
    p.add_argument("--clean", action="store_true",
                   help="remove leftovers of older packs listed in data/legacy.json, such as HATS-Tools and "
                        "misplaced theme patches")
    p.add_argument("--prune", action="store_true", help="remove files of components no longer selected")
    p.add_argument("--force", action="store_true", help="set up a folder that does not look like a Switch card")
    p.add_argument("--dry-run", action="store_true", help="show what would change and stop")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("undo", help="put back what the last setup on a card replaced or removed, from its backup")
    p.add_argument("target", nargs="?", help="the card's mount point; without it, the one mounted Switch card")
    p.add_argument("--dry-run", action="store_true", help="show what would change and stop")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("check", aliases=["doctor"],
                       help="report what is missing, wrong or risky on a mounted card")
    p.add_argument("target", nargs="?", help="the card's mount point; without it, every mounted Switch card")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("lock", help="pin every component to its newest release (needs network; for maintainers)")
    p.add_argument("component", nargs="*", help="only these components")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("updates", help="list components with a newer release upstream than the pinned one")
    p.add_argument("component", nargs="*", help="only these components")
    p.add_argument("--verify", action="store_true",
                   help="also compare every pinned file with the checksum GitHub publishes (needs GITHUB_TOKEN)")
    p.set_defaults(func=cmd_updates)

    p = sub.add_parser("new", help="build a new card in one go: formatted like hekate does, with hekate, "
                                   "Atmosphère and the homebrew on it (raw, admin; erases)")
    disk_args(p, True)
    p.add_argument("--emummc", default="none", metavar="SIZE",
                   help="also make the partition hekate's Create emuMMC needs: full (32 GB consoles: Lite, V1, V2), "
                        "full64 (OLED), or 4 to 24 for a resized emuMMC of that many GiB (default: none)")
    p.add_argument("--label", default="SWITCH SD", help="volume label (default: SWITCH SD)")
    component_args(p)
    write_args(p)
    p.set_defaults(func=cmd_new)

    games_commands(sub)
    raw_commands(sub)
    remote_commands(sub)
    autocheck_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Failure as e:
        log(f"error: {e}")
        return e.code
    except BrokenPipeError:  # the reader of our output went away, as `| head` does
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_ERROR
    except OSError as e:
        if os.environ.get("SWITCHSD_DEBUG"):
            raise
        log(f"error: {e.strerror or e}" + (f": {e.filename}" if e.filename else "")
            + ". Set SWITCHSD_DEBUG=1 to see where it happened.")
        return EXIT_ERROR
    except KeyboardInterrupt:
        log("interrupted: run the same command again to continue")
        return EXIT_INTERRUPTED
