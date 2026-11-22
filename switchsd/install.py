"""Put components and config templates on a mounted SD card, or in any folder."""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .common import JUNK, Failure, gb, is_junk, linux_mounts, log, nfiles, sha256_file
from .components import DATA, Component, Rule, match_asset, safe_relpath, verified

TEMPLATES = DATA / "sdroot"
LEGACY = DATA / "legacy.json"
RECORD = "config/switchsd/installed.json"
BACKUPS = "config/switchsd/backup"
KEEP_BACKUPS = 3  # the updates `undo` can take back, newest first
STAMP = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Settings files the user may have changed: installed when missing, never overwritten.
KEEP = re.compile(r"^config/([^/]+/)?[^/]+\.(ini|conf|cfg)$|(^|/)dbi\.config$", re.IGNORECASE)
# The console's own data and the user's files: setup never writes or removes anything here.
PROTECTED = re.compile(r"^(emummc|nintendo|jksv)(/|$)", re.IGNORECASE)
# Where components write, at the top of a card. A release that puts a file anywhere else has changed
# its layout, such as a Switch 2 build in nx2/ or everything one folder down: it is looked at before
# it goes on a card, not after.
TOP_LEVEL = {"atmosphere", "bootloader", "config", "emuiibo", "games", "saltysd", "scripts", "switch", "themes",
             "exosphere.ini", "hbmenu.nro", "payload.bin"}
# Any of these names makes a folder one that setup takes for a card without --force.
MARKERS = ("atmosphere", "bootloader", "switch", "emummc", "nintendo", "payload.bin", "hbmenu.nro")


def looks_like_card(path: Path) -> bool:
    """A card that is set up, for finding cards among the mounted volumes: stricter than MARKERS."""
    return (path / "payload.bin").is_file() and ((path / "bootloader").is_dir() or (path / "atmosphere").is_dir())


@dataclass
class Item:
    dest: str
    component: str  # a component id, or "template"
    load: Callable[[], bytes]
    sha256: str
    size: int
    keep: bool


@dataclass
class Report:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    backed_up: list[str] = field(default_factory=list)  # settings files that --reset replaced
    saved: list[str] = field(default_factory=list)  # files copied before they were replaced or removed
    backup: str = ""  # the folder with this run's copies, for `undo`


def _item(dest: str, owner: str, data: bytes, keep: bool | None = None) -> Item:
    return Item(dest, owner, lambda: data, hashlib.sha256(data).hexdigest(), len(data),
                bool(KEEP.search(dest)) if keep is None else keep)


def rule_items(comp: Component, rule: Rule, asset: bytes | None, name: str = "") -> list[Item]:
    """What a rule installs from `asset`, the content of the release file called `name`."""
    items = _rule_items(comp, rule, asset, name)
    if rule.keep:
        for item in items:
            item.keep = item.keep or bool(re.search(rule.keep, item.dest))
    return items


def _rule_items(comp: Component, rule: Rule, asset: bytes | None, name: str) -> list[Item]:
    if rule.content is not None:
        return [_item(dest, comp.id, rule.content.encode()) for dest in rule.to]
    assert asset is not None
    if rule.unzip is None and not rule.member:
        return [_item(dest, comp.id, asset) for dest in rule.to]
    items = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(asset))
    except zipfile.BadZipFile:
        raise Failure(f"{comp.id}: {name} is not a zip archive; the release layout changed") from None
    with archive as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if rule.member:
            found = [i for i in infos if re.search(rule.member, i.filename)]
            if len(found) != 1:
                raise Failure(f"{comp.id}: {rule.member!r} matches {len(found)} files in {name}")
            data = z.read(found[0])
            return [_item(dest, comp.id, data) for dest in rule.to]
        for info in infos:
            member = info.filename
            if rule.unzip:
                match = re.match(rule.unzip, member)
                if not match:
                    continue
                member = member[match.end():]
            if not member or "__MACOSX" in member.split("/"):
                continue
            if (rule.include and not re.search(rule.include, member)) \
                    or (rule.exclude and re.search(rule.exclude, member)):
                continue
            dest = rule.into + member
            if not safe_relpath(dest):
                raise Failure(f"{comp.id}: {name} holds an unsafe path {info.filename!r}")
            if not is_junk(dest):
                items.append(_item(dest, comp.id, z.read(info)))
    if not items:
        raise Failure(f"{comp.id}: nothing to install from {name}; the release layout changed")
    return items


def template_items() -> list[Item]:
    items = []
    for path in sorted(TEMPLATES.rglob("*")):
        if path.is_file() and not is_junk(path.name):
            items.append(_item(path.relative_to(TEMPLATES).as_posix(), "template", path.read_bytes(), keep=True))
    return items


def build_plan(selected: list[Component], pins: dict, fetched: dict[str, dict[str, Path]]) -> dict[str, Item]:
    """Every file to put on the card, keyed by lower-case path because FAT ignores case.

    Each release file is read once and compared with its pin; what goes on the card comes from those
    bytes, not from a second look at a cache folder that other programs can write to.
    """
    plan: dict[str, Item] = {}
    for comp in selected:
        pinned = pins[comp.id]["assets"]
        assets: dict[str, bytes] = {}
        for rule in comp.files:
            name = "" if rule.content is not None else match_asset(rule, list(pinned), comp.id)
            if name and name not in assets:
                assets[name] = verified(fetched[comp.id][name], pinned[name]["sha256"], f"{comp.id}: {name}")
            for item in rule_items(comp, rule, assets.get(name), name):
                key = item.dest.lower()
                if PROTECTED.match(key):
                    raise Failure(f"{comp.id} would write {item.dest}, which belongs to the console")
                if key.split("/")[0] not in TOP_LEVEL:
                    raise Failure(f"{comp.id} {pins[comp.id]['tag']} would write {item.dest}, outside the folders "
                                  "components use: its release layout changed, so it needs a look before it goes "
                                  "on a card")
                prev = plan.get(key)
                if prev and prev.component != comp.id and prev.component not in comp.overrides:
                    raise Failure(f"{comp.id} and {prev.component} both install {item.dest}")
                plan[key] = item
    for item in template_items():
        if item.dest.lower() in plan:
            raise Failure(f"template {item.dest} collides with {plan[item.dest.lower()].component}")
        plan[item.dest.lower()] = item
    return plan


def check_target(root: Path, force: bool) -> None:
    if not root.is_dir():
        raise Failure(f"{root} is not a folder; give the SD card's mount point, such as /Volumes/SWITCH SD or E:\\")
    resolved, home = root.resolve(), Path.home().resolve()
    if (resolved == Path(resolved.anchor) and not is_removable_root(resolved)) or home.is_relative_to(resolved):
        raise Failure(f"{root} is a system or home folder; give the SD card's own folder")
    names = {p.name.lower() for p in root.iterdir() if not is_junk(p.name)}
    if names and not names & set(MARKERS) and not force:
        raise Failure(f"{root} is not empty and does not look like a Switch SD card; pass --force if it is one")


def is_removable_root(path: Path) -> bool:
    """A drive root such as E:\\ is fine on Windows unless it is the system drive."""
    if sys.platform != "win32":
        return False
    return path.anchor.rstrip("\\").upper() != os.environ.get("SYSTEMDRIVE", "C:").upper()


def filesystem(root: Path) -> str | None:
    """Best-effort file system name of the volume holding `root`: msdos/vfat/fat32, exfat, apfs, ntfs..."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/diskutil", "info", str(root)], capture_output=True, text=True,
                                 timeout=30, check=False).stdout
            match = re.search(r"File System Personality:\s*(.+)", out)
            return match.group(1).strip().lower() if match else None
        if sys.platform == "win32":
            name = ctypes.create_unicode_buffer(64)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
                str(Path(root.anchor)), None, 0, None, None, None, name, 64)
            return name.value.lower() if ok else None
        best, fstype, target = "", None, str(root.resolve())
        for _, point, kind in linux_mounts():
            inside = target == point or target.startswith(point.rstrip("/") + "/")
            if inside and len(point) >= len(best):
                best, fstype = point, kind
        return fstype
    except (OSError, subprocess.SubprocessError):
        return None


def write_file(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".switchsd-tmp")
    tmp.unlink(missing_ok=True)  # left by an interrupted run, or a link put in its place
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(tmp, flags, 0o666), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


class Links:
    """Refuses paths that leave the card through a symbolic link, which a staging folder may hold and
    macOS shows on FAT as well."""

    def __init__(self, root: Path) -> None:
        self.base = root.resolve()
        self.vetted: set[Path] = set()

    def vet(self, target: Path, dest: str) -> None:
        if target.parent not in self.vetted:
            if not target.parent.resolve().is_relative_to(self.base):
                raise Failure(f"{dest}: a link on the card leads its folder somewhere else; remove the link first")
            self.vetted.add(target.parent)
        if target.is_symlink():
            raise Failure(f"{dest} is a link on the card, not a file; remove it first")


def read_record(root: Path) -> dict:
    path = root / RECORD
    if not path.is_file():
        return {"components": {}}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not all(isinstance(info.get("files", {}), dict) for info in record.get("components", {}).values()):
            raise ValueError("a component's files are not a list of paths and checksums")
    except (OSError, ValueError, AttributeError) as e:
        raise Failure(f"{RECORD} is unreadable ({e}); move it away to start a fresh record") from None
    return record


def make_record(selected: list[Component], pins: dict, plan: dict[str, Item], others: dict | None = None) -> bytes:
    """config/switchsd/installed.json: every component with its version and the files it owns.

    `others` are the entries of components an earlier setup installed and this one leaves alone.
    """
    comps = dict(others or {})
    for comp in selected:
        comps[comp.id] = {"version": pins[comp.id]["tag"],
                          "files": {i.dest: i.sha256 for i in plan.values() if i.component == comp.id}}
    record = {"tool": f"switchsd {__version__}", "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "components": dict(sorted(comps.items()))}
    return (json.dumps(record, indent=1, ensure_ascii=False) + "\n").encode()


class Journal:
    """What one setup changes on a card, kept so that `undo` can take it back.

    Before the first change, every file the run replaces or removes is copied to
    config/switchsd/backup/<stamp>/, and the list of changes, with each file's checksum before and
    after, is saved as config/switchsd/backup/<stamp>.json. A run that stops halfway can be undone too.
    """

    def __init__(self, root: Path, links: Links) -> None:
        self.root, self.links = root, links
        now, number = time.strftime("%Y%m%d-%H%M%S"), 1
        self.stamp = now
        while any((root / BACKUPS / f"{self.stamp}{end}").exists() for end in ("", ".json", ".undone.json")):
            number += 1
            self.stamp = f"{now}-{number}"
        self.created: dict[str, str] = {}  # path: checksum written
        self.replaced: dict[str, list[str]] = {}  # path: [checksum before, checksum written]
        self.removed: dict[str, str] = {}  # path: checksum before
        self.copied: set[str] = set()

    @property
    def folder(self) -> str:
        return f"{BACKUPS}/{self.stamp}"

    def __bool__(self) -> bool:
        return bool(self.created or self.replaced or self.removed)

    def backup_size(self) -> int:
        paths = (self.root / dest for dest in [*self.replaced, *self.removed] if dest not in self.copied)
        return sum(path.stat().st_size for path in paths if path.is_file())

    def save(self) -> None:
        """Copy what is not copied yet, then write the list: the backup is complete before it is named."""
        for dest in [*self.replaced, *self.removed]:
            source = self.root / dest
            if dest in self.copied or not source.is_file():
                continue
            backup = self.root / self.folder / dest
            self.links.vet(backup, f"{self.folder}/{dest}")
            write_file(backup, source.read_bytes())
            self.copied.add(dest)
        journal = self.root / BACKUPS / f"{self.stamp}.json"
        self.links.vet(journal, f"{BACKUPS}/{self.stamp}.json")
        data = {"tool": f"switchsd {__version__}", "created": self.created, "replaced": self.replaced,
                "removed": self.removed}
        write_file(journal, (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode())


def backup_sets(root: Path) -> list[str]:
    """The stamps of the backups setup left on a card, oldest first, undone ones included."""
    folder = root / BACKUPS
    stamps = set()
    for path in folder.iterdir() if folder.is_dir() else []:
        for end in (".undone.json", ".json"):
            if path.name.endswith(end) and STAMP.match(path.name[:-len(end)]):
                stamps.add(path.name[:-len(end)])
                break
    return sorted(stamps)


def is_setting(dest: str) -> bool:
    """A settings file: one the user may change, which setup never overwrites unasked."""
    return bool(KEEP.search(dest)) or (TEMPLATES / dest).is_file()


def prune_backups(root: Path, keep: int, links: Links, current: str = "") -> None:
    """Drop all but the newest `keep` backups besides `current`. Copies of settings files stay: they
    are small, and `--reset` promises them."""
    links.vet(root / BACKUPS / "x", BACKUPS)  # the backup folder itself is no way off the card
    stamps = [stamp for stamp in backup_sets(root) if stamp != current]
    for stamp in stamps[:max(len(stamps) - keep, 0)]:
        folder = root / BACKUPS / stamp
        for end in (".json", ".undone.json"):
            links.vet(root / BACKUPS / f"{stamp}{end}", f"{BACKUPS}/{stamp}{end}")
            (root / BACKUPS / f"{stamp}{end}").unlink(missing_ok=True)
        if folder.is_symlink():
            folder.unlink()  # the link only, never what it leads to
            continue
        if not folder.is_dir():
            continue
        links.vet(folder / "x", f"{BACKUPS}/{stamp}")
        for path in sorted(folder.rglob("*"), reverse=True):  # files before their folders
            dest = path.relative_to(folder).as_posix()
            if path.is_symlink() or (path.is_file() and not is_setting(dest)):
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if not any(folder.iterdir()):
            folder.rmdir()


def check_space(root: Path, writes: list[Item], journal: Journal) -> None:
    """Stop before the first change when the card cannot hold the new files and the backup."""
    need = sum(item.size for item in writes) + journal.backup_size() + (16 << 20) + len(writes) * (128 << 10)
    free = shutil.disk_usage(root).free
    if need > free:
        raise Failure(f"needs up to {gb(need)} for the new files and the copies of the ones they replace, but only "
                      f"{gb(free)} is free on {root}; nothing was changed")


def same_record(old: bytes, new: bytes) -> bool:
    """Whether two records name the same components and files, whenever they were written."""
    try:
        return json.loads(old).get("components") == json.loads(new).get("components")
    except (ValueError, AttributeError):
        return False


def apply(root: Path, plan: dict[str, Item], selected: list[Component], pins: dict, *,
          reset: set[str] | None, prune: bool, dry_run: bool, journal: Journal | None = None,
          leave: set[str] | None = None) -> Report:
    """Write the plan. `reset` lists template paths to restore (lower case); None restores none.
    `leave` names components this run left out: their files stay as they are, even with `prune`, and
    their entries in the record carry over.

    Nothing is changed before every file the run replaces or removes has been copied and the list of
    changes saved, so `undo` can put the card back as it was.
    """
    leave = leave or set()
    report = Report()
    record = read_record(root)
    links = journal.links if journal else Links(root)
    links.vet(root / RECORD, RECORD)
    for item in plan.values():  # all of them before the first write
        links.vet(root / item.dest, item.dest)
    journal = journal or Journal(root, links)
    writes: list[Item] = []
    for key in sorted(plan):
        item = plan[key]
        target = root / item.dest
        if target.exists() and not target.is_file():
            raise Failure(f"{item.dest} is a folder on the card, but {item.component} installs a file there")
        if target.is_file():
            if item.keep and not (reset and item.component == "template" and item.dest.lower() in reset):
                report.kept.append(item.dest)
                continue
            before = sha256_file(target)
            if before == item.sha256:
                report.unchanged.append(item.dest)
                continue
            journal.replaced[item.dest] = [before, item.sha256]
            if item.keep:
                report.backed_up.append(f"{journal.folder}/{item.dest}")
        else:
            journal.created[item.dest] = item.sha256
        writes.append(item)
        report.written.append(item.dest)

    removals: list[Path] = []
    wanted = {c.id for c in selected}
    for cid, info in record.get("components", {}).items():
        if cid in leave or (cid not in wanted and not prune):
            continue
        for dest, digest in info.get("files", {}).items():
            # The record is read off the card: it removes nothing this run installs and nothing off the card.
            if dest.lower() in plan or PROTECTED.match(dest) or not safe_relpath(dest) or dest in journal.removed:
                continue
            target = root / dest
            try:
                links.vet(target, dest)
            except Failure:
                report.left.append(dest)
                continue
            if target.is_file() and sha256_file(target) == digest:
                removals.append(target)
                journal.removed[dest] = digest
                report.removed.append(dest)
            elif target.exists():
                report.left.append(dest)

    others = {k: v for k, v in record.get("components", {}).items() if k not in wanted and (k in leave or not prune)}
    new_record = make_record(selected, pins, plan, others)
    record_path = root / RECORD
    old_record = record_path.read_bytes() if record_path.is_file() else None
    if not journal and old_record is not None and same_record(old_record, new_record):
        return report  # nothing to change, and the record says so already
    written = hashlib.sha256(new_record).hexdigest()
    report.saved = [dest for dest in [*journal.replaced, *journal.removed] if dest != RECORD]
    if old_record is None:
        journal.created[RECORD] = written
    else:
        journal.replaced[RECORD] = [hashlib.sha256(old_record).hexdigest(), written]
    report.backup = journal.folder
    if dry_run:
        return report
    check_space(root, writes, journal)
    prune_backups(root, KEEP_BACKUPS - 1, links, journal.stamp)
    journal.save()
    for item in writes:
        write_file(root / item.dest, item.load())
    for target in removals:
        target.unlink()
        remove_empty_parents(root, target)
    write_file(record_path, new_record)
    return report


@dataclass
class Undone:
    stamp: str
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)


def read_journal(root: Path, stamp: str) -> dict:
    """A backup's list of changes, read off the card and checked before undo acts on it."""
    name = f"{BACKUPS}/{stamp}.json"
    try:
        data = json.loads((root / name).read_text(encoding="utf-8"))
        created, replaced, removed = data["created"], data["replaced"], data["removed"]
        sums = [*created.values(), *removed.values(), *(s for pair in replaced.values() for s in pair)]
        if not all(isinstance(pair, list) and len(pair) == 2 for pair in replaced.values()) \
                or not all(isinstance(s, str) and SHA256.match(s) for s in sums):
            raise ValueError("a checksum is not one")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise Failure(f"{name} is unreadable ({e}); nothing was changed") from None
    for dest in [*created, *replaced, *removed]:
        if not safe_relpath(dest) or PROTECTED.match(dest) or dest.lower().startswith(BACKUPS + "/"):
            raise Failure(f"{name} names {dest!r}, which undo does not touch; nothing was changed")
    return data


def undoable(root: Path) -> list[str]:
    """The setups on this card that `undo` can still take back, oldest first."""
    return [s for s in backup_sets(root) if (root / BACKUPS / f"{s}.json").is_file()]


def undo(root: Path, dry_run: bool) -> Undone:
    """Put back what the newest setup on this card changed: replaced and removed files come back from
    its backup, and the files it added go. A file changed since then is left as it is."""
    links = Links(root)
    links.vet(root / BACKUPS / "x", BACKUPS)  # the backup folder itself is no way off the card
    pending = undoable(root)
    if not pending:
        raise Failure("nothing to undo: no setup has left a backup on this card")
    stamp = pending[-1]
    links.vet(root / BACKUPS / f"{stamp}.json", f"{BACKUPS}/{stamp}.json")
    data = read_journal(root, stamp)
    folder = root / BACKUPS / stamp
    back: dict[str, tuple[str, str | None]] = {d: (p[0], p[1]) for d, p in data["replaced"].items()}
    back.update({d: (digest, None) for d, digest in data["removed"].items()})
    for dest, (before, _) in back.items():  # every copy is checked before anything changes
        copy = folder / dest
        links.vet(copy, f"{BACKUPS}/{stamp}/{dest}")
        links.vet(root / dest, dest)
        if not copy.is_file() or sha256_file(copy) != before:
            raise Failure(f"the copy of {dest} in {BACKUPS}/{stamp} is missing or damaged; nothing was changed")
    for dest in data["created"]:
        links.vet(root / dest, dest)
    done = Undone(stamp)
    last = sorted(back, key=lambda d: d.lower() == RECORD)  # the record goes back last
    for dest in last:
        before, after = back[dest]
        target = root / dest
        current = sha256_file(target) if target.is_file() else None
        if current == before:
            continue
        if target.exists() and (current is None or current != after):
            done.left.append(dest)  # changed since: left as it is
            continue
        if not dry_run:
            write_file(target, (folder / dest).read_bytes())
        done.restored.append(dest)
    for dest, after in sorted(data["created"].items(), key=lambda kv: kv[0].lower() == RECORD):
        target = root / dest
        if not target.exists():
            continue
        if not target.is_file() or sha256_file(target) != after:
            done.left.append(dest)
            continue
        if not dry_run:
            target.unlink()
            remove_empty_parents(root, target)
        done.deleted.append(dest)
    if not dry_run:
        journal = root / BACKUPS / f"{stamp}.json"
        os.replace(journal, journal.with_name(f"{stamp}.undone.json"))
    return done


def legacy_targets(root: Path) -> list[tuple[Path, str]]:
    """Files and folders from older packs that `setup --clean` removes, with the reason for each."""
    found = []
    for entry in json.loads(LEGACY.read_text(encoding="utf-8"))["remove"]:
        pattern = entry["path"]
        folder, _, name = pattern.rstrip("/").rpartition("/")
        if not safe_relpath(pattern.rstrip("/").replace("*", "x")) or PROTECTED.match(pattern):
            raise Failure(f"legacy.json: unsafe path {pattern!r}")
        if not (root / folder).resolve().is_relative_to(root.resolve()):
            continue  # a link leads this folder off the card
        for path in sorted((root / folder).glob(name)) if (root / folder).is_dir() else []:
            if pattern.endswith("/") == path.is_dir():
                found.append((path, entry["reason"]))
    return found


def clean_legacy(root: Path, dry_run: bool, journal: Journal | None = None) -> list[str]:
    """Remove leftovers of older packs; with a journal, they are copied first, for `undo`."""
    removed = []
    for path, reason in legacy_targets(root):
        rel = path.relative_to(root).as_posix()
        if not dry_run:
            if journal is not None:
                files = [path] if path.is_file() else sorted(p for p in path.rglob("*")
                                                             if p.is_file() and not p.is_symlink())
                for file in files:
                    dest = file.relative_to(root).as_posix()
                    if safe_relpath(dest):
                        journal.removed[dest] = sha256_file(file)
                journal.save()
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        removed.append(f"{rel} ({reason})")
    return removed


def remove_empty_parents(root: Path, removed: Path) -> None:
    """Drop the folders a removed file leaves empty, such as a sysmodule's contents folder."""
    folder = removed.parent
    while folder != root and root in folder.parents:
        leftovers = [p for p in folder.iterdir() if not p.name.startswith("._")]
        if leftovers:
            return
        for junk in folder.iterdir():
            junk.unlink()
        folder.rmdir()
        folder = folder.parent


def remove_appledouble(root: Path) -> int:
    """Delete the ._ files macOS writes on FAT to hold extended attributes such as com.apple.provenance.

    Not under the console's own folders: a save may hold a file of such a name.
    """
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        top = os.path.samefile(dirpath, root)
        dirnames[:] = [d for d in dirnames if d not in JUNK and not (top and PROTECTED.match(d))]
        for name in filenames:
            if name.startswith("._"):
                os.unlink(os.path.join(dirpath, name))
                removed += 1
    return removed


def summarize(report: Report, plan: dict[str, Item], dry_run: bool) -> None:
    verb = "would write" if dry_run else "wrote"
    size = sum(plan[p.lower()].size for p in report.written)
    log(f"{verb} {nfiles(len(report.written))} ({gb(size)}); {len(report.unchanged)} already up to date")
    if report.kept:
        shown = ", ".join(report.kept[:6]) + (f" and {len(report.kept) - 6} more" if len(report.kept) > 6 else "")
        log(f"kept {len(report.kept)} files that were already there (settings, self-updating apps): {shown}")
    if report.backed_up:
        log("backed up before replacing: " + ", ".join(report.backed_up))
    if report.saved:
        n = nfiles(len(report.saved))
        them = "it" if len(report.saved) == 1 else "them"
        log(f"would first copy the {n} it replaces or removes, for `switchsd.py undo`" if dry_run else
            f"first copied the {n} it replaced or removed to {report.backup}: `switchsd.py undo` puts {them} back")
    if report.removed:
        log(f"{'would remove' if dry_run else 'removed'} {len(report.removed)} files that newer versions no longer "
            "ship: " + ", ".join(report.removed))
    if report.left:
        log("left in place because they were changed after installing: " + ", ".join(report.left))
