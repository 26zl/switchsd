"""`autocheck`: run `check` by itself whenever a Switch card is mounted, and say how it went.

macOS starts it from a launchd agent with StartOnMount; Linux from a systemd user path unit that
watches the removable-media folder.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import plistlib
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .common import EXIT_OK, Failure, Usage, log, plain, run
from .doctor import check, mounted_cards

LABEL = "switchsd.autocheck"
SCRIPT = Path(__file__).resolve().parent.parent / "switchsd.py"


def python() -> str:
    """A stable interpreter path: macOS's own when there, as Homebrew's moves between versions."""
    if sys.platform == "darwin" and Path("/usr/bin/python3").exists():
        return "/usr/bin/python3"
    return sys.executable


def notify(title: str, text: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if sys.platform == "darwin":
            script = f"display notification {applescript(text)} with title {applescript(title)}"
            subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=30, check=False)
        else:
            subprocess.run(["notify-send", title, text], capture_output=True, timeout=30, check=False)


def applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_word(text: str) -> str:
    """One word of a systemd ExecStart line: quoted, with % and $ taken literally."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def run_once(cards: list[Path] | None = None, tell: Callable[[str, str], None] = notify) -> int:
    """Check every mounted Switch card; one notification per card."""
    found = 0
    for card in mounted_cards() if cards is None else cards:
        found += 1
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            findings = check(card)
        except Failure as e:
            print(plain(f"{stamp} {card}: not checked: {e}"))
            tell("Switch card could not be checked", plain(str(e)))
            continue
        except Exception as e:  # noqa: BLE001 - a card can hold anything; the user still gets an answer
            print(plain(f"{stamp} {card}: the check itself failed: {type(e).__name__}: {e}"))
            tell("Switch card could not be checked", "The check itself failed; the log has the details.")
            continue
        fails = [text for level, text in findings if level == "FAIL"]
        warns = [text for level, text in findings if level == "WARN"]
        print(plain(f"{stamp} {card}: {len(fails)} FAIL, {len(warns)} WARN"))
        for level, text in findings:
            if level != "OK":
                print(plain(f"  {level} {text}"))
        if fails:
            tell(f"Switch card: {len(fails)} problem(s)", plain(fails[0]))
        elif warns:
            tell(f"Switch card: {len(warns)} warning(s)", plain(warns[0]))
        else:
            tell("Switch card is fine", plain(f"{card.name}: everything checks out"))
    return found


def mac_plist(home: Path) -> tuple[Path, bytes]:
    logfile = home / "Library" / "Logs" / "switchsd-autocheck.log"
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python(), "-I", str(SCRIPT), "autocheck", "run"],
        "StartOnMount": True,
        "StandardOutPath": str(logfile),
        "StandardErrorPath": str(logfile),
    }
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist", plistlib.dumps(plist)


def linux_units(home: Path) -> dict[Path, str]:
    folder = home / ".config" / "systemd" / "user"
    return {
        folder / "switchsd-autocheck.path": "[Unit]\nDescription=Check Switch SD cards when they are mounted\n\n"
                                            "[Path]\nPathChanged=/run/media/%u\nPathChanged=/media/%u\n\n"
                                            "[Install]\nWantedBy=default.target\n",
        folder / "switchsd-autocheck.service": "[Unit]\nDescription=Check mounted Switch SD cards\n\n"
                                               f"[Service]\nType=oneshot\nExecStart={unit_word(python())} -I "
                                               f"{unit_word(str(SCRIPT))} autocheck run\n",
    }


def job(home: Path) -> tuple[dict[Path, bytes], list[list[str]]]:
    """What `install` writes on this system, and the commands that then start the job."""
    if sys.platform == "darwin":
        path, data = mac_plist(home)
        return {path: data}, [["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)]]
    if sys.platform.startswith("linux"):
        return ({path: text.encode() for path, text in linux_units(home).items()},
                [["systemctl", "--user", "daemon-reload"],
                 ["systemctl", "--user", "enable", "--now", "switchsd-autocheck.path"]])
    raise Failure("autocheck is not available on Windows yet; run `py switchsd.py check E:\\` by hand")


def install(home: Path, load: bool = True) -> list[Path]:
    files, commands = job(home)
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if sys.platform == "darwin":
        (home / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
    if load:
        if sys.platform == "darwin":  # an earlier install's job, if it is loaded
            subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True,
                           check=False)
        for command in commands:
            run(command)
    return list(files)


def installed(home: Path) -> list[Path]:
    """The files of an earlier install that are there."""
    if sys.platform == "win32":
        return []
    paths = [mac_plist(home)[0]] if sys.platform == "darwin" else list(linux_units(home))
    return [path for path in paths if path.exists()]


def uninstall(home: Path) -> list[Path]:
    if sys.platform == "darwin":
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, check=False)
    elif sys.platform != "win32":
        with contextlib.suppress(OSError):  # no systemd here: the unit files still go
            subprocess.run(["systemctl", "--user", "disable", "--now", "switchsd-autocheck.path"],
                           capture_output=True, check=False)
    removed = installed(home)
    for path in removed:
        path.unlink()
    return removed


def cmd_autocheck(args: argparse.Namespace) -> int:
    home = Path.home()
    if args.dry_run:
        if args.action == "run":
            raise Usage("--dry-run goes with install or uninstall; `autocheck run` changes nothing")
        if args.action == "install":
            files, commands = job(home)
            for path in files:
                print(f"would write {path}")
            for command in commands:
                print("would run " + " ".join(command))
        else:
            found = installed(home)
            print("\n".join(f"would stop the job and remove {path}" for path in found) if found
                  else "autocheck is not installed")
        return EXIT_OK
    if args.action == "install":
        for path in install(home):
            print(f"installed {path}")
        print("A notification now reports on every Switch card that gets mounted.")
        if sys.platform == "darwin":
            print("On macOS this may stay silent: macOS 27 was seen to refuse the background job access to the "
                  f"card without asking. Insert a card and look at {home / 'Library/Logs/switchsd-autocheck.log'}; "
                  "if it says the card could not be read, check under System Settings > Privacy & Security > "
                  "Files and Folders, or run `switchsd.py check` by hand.")
    elif args.action == "uninstall":
        removed = uninstall(home)
        print("removed " + ", ".join(map(str, removed)) if removed else "autocheck was not installed")
    elif run_once() == 0:
        log("no Switch card is mounted")
    return EXIT_OK


def add_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("autocheck", help="check Switch cards by themselves when they are mounted (Linux; on "
                                         "macOS the system may refuse the background job access to the card)")
    p.add_argument("action", choices=("install", "uninstall", "run"),
                   help="install or uninstall the automatic check, or run it once now")
    p.add_argument("--dry-run", action="store_true", help="show what install or uninstall would change and stop")
    p.set_defaults(func=cmd_autocheck)
