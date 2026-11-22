"""Raw card commands: back up a card and restore it, inspect it, clone it onto a bigger card, format or
build a new one.

hekate's partition manager puts the emuMMC in a raw partition (MBR type 0xE0) behind the FAT32
partition, so copying the files misses it. `image` saves both, leaving out what the card does not use.
`clone` and `restore` copy the emuMMC partition sector for sector, build the FAT32 partition
themselves from the image's files, and point the emuMMC config at the new sector when it moves.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

from . import __version__
from .blockdev import SECTOR, BlockDev
from .common import CHUNK, EXIT_OK, Failure, Mismatch, Progress, gb, hand_back, is_junk, log, parse_ini, plain
from .disks import Disk, check_disk, exclusive, get_disk, list_disks, reread, same_disk, wait_for
from .emummc import config_patcher, patch_raw_based
from .fat32 import ATTR_ARCHIVE, ATTR_DIR, Fat32, Node, check_fits, geometry, label_bytes, now_times, write_fat32
from .imagefile import CompactImage, is_compact, merge, write_image
from .install import looks_like_card
from .mbr import (ALIGN, EMUMMC_TYPE, FAT32_TYPES, MIB_SECTORS, Part, Plan, build_mbr, card_parts, format_layout,
                  parse_mbr, plan_layout)

MIB = 1 << 20
MAX_CONFIG = MIB  # emummc.ini and raw_based are a few bytes; nothing bigger is read whole
OWN_DATA = re.compile(r"^(emummc|nintendo|jksv)/", re.IGNORECASE)
APPLEDOUBLE = bytes.fromhex("00051607")


def open_image(path: str) -> tuple[BlockDev, int]:
    """An image file as a device, and the size in sectors of the card it holds."""
    file = Path(path)
    if not file.is_file():
        raise Failure(f"{path} is not an image file")
    if is_compact(file):
        image = CompactImage(str(file))
        return image, image.size // SECTOR
    size = file.stat().st_size
    if not size or size % SECTOR:
        raise Failure(f"{path} is not a whole number of sectors")
    return BlockDev(str(file)), size // SECTOR


def create_private(path: Path) -> BinaryIO:
    """A new file that only its owner can read: a card image holds the console's key and PRODINFO backups."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600), "wb")


def sidecar_digest(image: Path) -> str | None:
    """The checksum `image --full` wrote next to a raw image, when it is there and readable."""
    try:
        first = Path(str(image) + ".sha256").read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    return first if re.fullmatch(r"[0-9a-f]{64}", first) else None


def check_image(image: BlockDev, path: str) -> str:
    """Compare an image with its checksum: says what it matched, or "" when there is nothing to compare."""
    if isinstance(image, CompactImage):
        image.verify()
        return "matches the checksum stored in it"
    want = sidecar_digest(Path(path))
    if want is None:
        return ""
    if hash_span(image, 0, Path(path).stat().st_size, "checking the image") != want:
        raise Mismatch(f"{path} does not match {Path(path).name}.sha256: the image is damaged or was changed")
    return f"matches {Path(path).name}.sha256"


def used_regions(dev: BlockDev, sectors: int) -> list[tuple[int, int]]:
    """The byte ranges of a card that hold something, for a layout `restore` can rebuild.

    Everything in front of the FAT32 partition, that volume's own structures and the clusters in use,
    and every emuMMC partition whole. Of what lies in front, `restore` brings back only the boot code
    area of sector 0: like hekate's partition manager, it leaves the rest of those 16 MiB zeroed.
    """
    parts = parse_mbr(dev.read(0, SECTOR))
    fat_part, emus = card_parts(parts)
    for part in parts:
        if part.end > sectors:
            raise Failure(f"partition {part.slot} ends beyond the end of the card")
    fat = Fat32(dev, fat_part.start * SECTOR)
    base = fat_part.start * SECTOR
    if fat.data_offset + fat.clusters * fat.cluster_bytes > fat_part.end * SECTOR:
        raise Failure("the FAT32 volume claims more room than its partition has")
    check_files(fat)
    regions = [(0, base), (base, fat.data_offset - base), *fat.used_runs()]
    regions += [(p.start * SECTOR, p.size * SECTOR) for p in emus]
    return merge(regions, MIB)


def check_files(fat: Fat32, skip: Callable[[str], bool] = lambda path: False) -> None:
    """Follow every file and folder to its end, so that damage is named before it costs a card."""
    for entry in fat.walk(skip):
        try:
            fat.covers(entry)
        except Failure as problem:
            raise Failure(f"{entry.path} is damaged on the source: {problem}") from None


def confirm(args: argparse.Namespace, disk: Disk, action: str) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise Failure("refusing to continue without confirmation; pass --yes to skip it")
    log(f"{action} on {disk.id} ({disk.name or 'unknown media'}, {gb(disk.size)}).")
    print(f"Type {disk.id} to continue: ", end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip()
    except EOFError:
        answer = ""
    if answer != disk.id:
        raise Failure("not confirmed; nothing was changed")


@contextlib.contextmanager
def opened_for_writing(disk: Disk) -> Iterator[BlockDev]:
    """The card, to ourselves and writable, once it is certain that it is still the disk that was checked.

    Minutes can pass between the first check and the first write: downloads, a checksum pass, the prompt.
    """
    same_disk(disk)
    with exclusive(disk), BlockDev(disk.raw, writable=True) as dst:
        actual = dst.device_size()
        if actual is not None and actual != disk.size:
            raise Failure(f"{disk.id} now holds {actual} bytes, not the {disk.size} that were checked: another "
                          "disk took that name. Nothing was written.")
        yield dst


def forget_old_layout(dst: BlockDev) -> None:
    """Wipe the partition table before the first other write.

    From here on the card's old volumes get overwritten piece by piece; a run that is interrupted must
    leave a card that is plainly empty, not one that still mounts with damaged files.
    """
    dst.zero(0, SECTOR)
    dst.sync()


def copy_span(src: BlockDev, src_off: int, dst: BlockDev, dst_off: int, length: int, label: str) -> str:
    digest, progress = hashlib.sha256(), Progress(label, length)
    for done in range(0, length, CHUNK):
        buf = src.read(src_off + done, min(CHUNK, length - done))
        digest.update(buf)
        dst.write(dst_off + done, buf)
        progress.add(len(buf))
    dst.sync()
    return digest.hexdigest()


def hash_span(dev: BlockDev, offset: int, length: int, label: str) -> str:
    digest, progress = hashlib.sha256(), Progress(label, length)
    for done in range(0, length, CHUNK):
        buf = dev.read(offset + done, min(CHUNK, length - done))
        digest.update(buf)
        progress.add(len(buf))
    return digest.hexdigest()


def probe(dev: BlockDev, sectors: int, count: int = 64) -> None:
    """Write a unique 1 MiB block at `count` places across the card, then read them all back.

    A fake card that wraps around or drops writes past its real size fails this.
    """
    first, last = ALIGN // MIB_SECTORS, sectors // MIB_SECTORS - 1
    places = sorted({first + (last - first) * i // (count - 1) for i in range(count)})
    nonce = secrets.token_bytes(16)

    def pattern(mib: int) -> bytes:
        return hashlib.sha256(nonce + mib.to_bytes(8, "little")).digest() * (MIB // 32)

    for mib in places:
        dev.write(mib * MIB, pattern(mib))
    dev.sync()
    bad = [mib for mib in places if dev.read(mib * MIB, MIB) != pattern(mib)]
    if bad:
        raise Failure(f"the card returned wrong data at {len(bad)} of {len(places)} test places (first at "
                      f"{bad[0]} MiB). It is fake or faulty; do not use it.")
    log(f"probe: {len(places)} test blocks across the whole card read back correctly")


def fat_label(fat: Fat32) -> str:
    label = fat.volume_label().upper()
    return label if re.fullmatch(r"[A-Z0-9 _-]{1,11}", label) else "SWITCH SD"


def constant(data: bytes) -> Callable[[], Iterable[bytes]]:
    return lambda: [data]


def reader(fat: Fat32, cluster: int, size: int) -> Callable[[], Iterable[bytes]]:
    return lambda: fat.read_chain(cluster, size)


def tree_from_fat(fat: Fat32, plan: Plan, lenient: bool = False) -> tuple[Node, list[str], list[str], list[str]]:
    """The image's FAT32 as a tree for the writer: (root, skipped, patched, notes).

    When the emuMMC moves, the files that hold its sector are rewritten. A reference that cannot be
    moved stops a clone; `lenient`, for restoring a backup, keeps that file as it is and notes it.
    """
    root = Node("", True, ATTR_DIR, now_times())
    folders = {"": root}
    skipped: list[str] = []
    patched: list[str] = []
    notes: list[str] = []
    moving = bool(plan.emummc) and plan.shift != 0

    def metadata(path: str) -> bool:
        # In the console's own folders a name alone decides nothing: a save may hold a file called ._x.
        return is_junk(path) and not (OWN_DATA.match(path) and path.rpartition("/")[2].startswith("._"))

    check_files(fat, metadata)
    for e in fat.walk(metadata, skipped):
        parent, _, name = e.path.rpartition("/")
        if is_junk(e.path) and not e.is_dir \
                and next(iter(fat.read_chain(e.cluster, min(e.size, 4))), b"") == APPLEDOUBLE:
            skipped.append(e.path)
            continue
        patcher = config_patcher(e.path) if moving and not e.is_dir else None
        if patcher and e.size > MAX_CONFIG:
            if not lenient:
                raise Failure(f"{e.path} holds {e.size} bytes, which no emuMMC setting does")
            notes.append(f"{e.path} is kept as it is: at {e.size} bytes it is no emuMMC setting")
            patcher = None
        if e.is_dir:
            node = folders[e.path] = Node(name, True, e.attr, e.times)
        elif patcher:
            data = fat.read_file(e)
            try:
                moved = patcher(data, plan)
            except Failure as problem:
                if not lenient:
                    raise
                moved = data
                notes.append(f"{e.path} is kept as it is: {problem}")
            if moved != data:
                patched.append(e.path)
            node = Node(name, False, e.attr, e.times, len(moved), constant(moved))
        else:
            node = Node(name, False, e.attr, e.times, e.size, reader(fat, e.cluster, e.size))
        folders[parent].children.append(node)
    return root, skipped, patched, notes


def tree_from_files(files: dict[str, bytes]) -> Node:
    """Card paths and their content as a tree for the writer; folders are made as needed."""
    times = now_times()
    root = Node("", True, ATTR_DIR, times)
    folders = {"": root}
    for path in sorted(files):
        *above, name = path.split("/")
        folder, key = root, ""
        for part in above:
            key = f"{key}/{part.lower()}" if key else part.lower()
            if key not in folders:
                folders[key] = Node(part, True, ATTR_DIR, times)
                folder.children.append(folders[key])
            folder = folders[key]
        folder.children.append(Node(name, False, ATTR_ARCHIVE, times, len(files[path]), constant(files[path])))
    return root


def flatten(node: Node, prefix: str = "") -> dict[str, Node]:
    out = {}
    for child in node.children:
        path = f"{prefix}/{child.name}" if prefix else child.name
        out[path.lower()] = child
        if child.is_dir:
            out.update(flatten(child, path))
    return out


def verify_fat32(dev: BlockDev, start: int, root: Node) -> int:
    """Read the new volume back and compare every file and folder with what was written."""
    expected = flatten(root)
    fat = Fat32(dev, start * SECTOR)
    checked = 0
    for e in fat.walk():
        node = expected.pop(e.path.lower(), None)
        if node is None:
            raise Mismatch(f"{e.path} is on the new card but was never written")
        if e.is_dir != node.is_dir or e.attr != node.attr:
            raise Mismatch(f"{e.path}: type or attributes differ from what was written")
        if not e.is_dir:
            digest = hashlib.sha256()
            for chunk in fat.read_chain(e.cluster, e.size):
                digest.update(chunk)
            if digest.hexdigest() != node.sha256:
                raise Mismatch(f"{e.path} reads back different from what was written; the card may be faulty")
        checked += 1
    if expected:
        raise Mismatch("missing on the new card: " + ", ".join(sorted(expected)[:10]))
    return checked


def row(name: str, part: Part) -> str:
    return f"  {name:<7}{part.type:#04x}  sectors {part.start:>10}-{part.end - 1:<10} {gb(part.size * SECTOR):>9}"


def describe(plan: Plan, label: str, restore: bool = False) -> str:
    old, new = ("image:", "card:") if restore else ("old card:", "new card:")
    lines = [old, row("FAT32", plan.src_fat)] + [row("emuMMC", p) for p in plan.src_emummc]
    lines += [new, row("FAT32", plan.fat) + f"  rebuilt, label {label!r}"]
    lines += [row("emuMMC", p) + "  cloned" for p in plan.emummc]
    if plan.emummc and plan.shift:
        lines.append(f"emuMMC sectors move by {plan.shift:#x}; emummc.ini and raw_based are updated to match")
    elif plan.emummc:
        lines.append("the emuMMC keeps its sectors, so emummc.ini and raw_based stay as they are")
    return "\n".join(lines)


def cmd_disks(args: argparse.Namespace) -> int:
    disks = list_disks()
    if args.json:
        print(json.dumps([{"id": d.id, "bytes": d.size, "name": d.name, "virtual": d.virtual,
                           "volumes": [{"bytes": v.size, "mount": v.mount} for v in d.volumes]} for d in disks],
                         indent=1))
        return EXIT_OK
    if not disks:
        print("no removable disk found; is the card in the reader?")
        return EXIT_OK
    for d in disks:
        print(f"{d.id}  {gb(d.size)}  {d.name.strip() or 'unnamed'}{'  (disk image)' if d.virtual else ''}")
        for v in d.volumes:
            where = "not mounted"
            if v.mount:
                where = f"mounted at {v.mount}" + ("  <- Switch card" if looks_like_card(Path(v.mount)) else "")
            print(f"    partition of {gb(v.size)}, {where}")
        print(f"    for the raw commands: {d.id} --expect-bytes {d.size}")
    return EXIT_OK


def cmd_info(args: argparse.Namespace) -> int:
    is_image = Path(args.target).is_file()
    if is_image:
        dev, sectors = open_image(args.target)
    elif args.verify:
        raise Failure("--verify compares an image file with its checksum; a card has none")
    else:
        try:
            disk = get_disk(args.target)
        except Failure as e:
            raise Failure(f"{args.target} is no image file here, and {e}") from None
        dev, sectors = BlockDev(disk.raw), disk.size // SECTOR
    with dev:
        parts = parse_mbr(dev.read(0, SECTOR))
        report: dict = {"bytes": sectors * SECTOR, "partitions": [
            {"slot": p.slot, "type": f"{p.type:#04x}", "start": p.start, "sectors": p.size} for p in parts]}
        if isinstance(dev, CompactImage):
            report["image"] = {"format": "compact", "stored_bytes": dev.stored, "created": dev.meta.get("created")}
        elif is_image:
            report["image"] = {"format": "raw", "stored_bytes": sectors * SECTOR}
        fat_part = next((p for p in parts if p.type in FAT32_TYPES), None)
        if fat_part:
            fat = Fat32(dev, fat_part.start * SECTOR)
            emummc: dict[str, object] = {}
            folder = next((e for e in fat.entries(fat.root, "") if e.is_dir and e.path.lower() == "emummc"), None)
            inside = list(fat.entries(folder.cluster, folder.path)) if folder else []
            for sub in [e for e in inside if e.is_dir]:
                inside += [e for e in fat.entries(sub.cluster, sub.path) if not e.is_dir]
            for entry in inside:
                if entry.is_dir:
                    continue
                if entry.path.lower() == "emummc/emummc.ini" and entry.size <= MAX_CONFIG:
                    text = fat.read_file(entry).decode("utf-8", "replace")
                    emummc[entry.path] = next((keys for name, keys in parse_ini(text) if name.lower() == "emummc"),
                                              {})
                elif config_patcher(entry.path) is patch_raw_based and entry.size == 4:
                    emummc[entry.path] = hex(struct.unpack("<I", fat.read_file(entry))[0])
            report["fat32"] = {"label": fat.label, "cluster_bytes": fat.cluster_bytes, "emummc": emummc}
        if args.verify:
            report["image"]["verified"] = check_image(dev, args.target)
            if not report["image"]["verified"]:
                raise Failure(f"{args.target} has no checksum to compare with: no .sha256 file lies next to it")
    if args.json:
        print(json.dumps(report, indent=2))
        return EXIT_OK
    report = json.loads(plain(json.dumps(report)))  # labels and settings come off the card
    print(f"size: {gb(report['bytes'])} ({report['bytes']} bytes)")
    image = report.get("image", {})
    if image.get("format") == "compact":
        print(f"compact image: holds the {gb(image['stored_bytes'])} that were in use"
              + (f", made {image['created']}" if image.get("created") else ""))
    if image.get("verified"):
        print(f"checksum: the image {image['verified']}")
    for p in parts:
        kind = "FAT32" if p.type in FAT32_TYPES else "emuMMC" if p.type == EMUMMC_TYPE else "other"
        print(f"  slot {p.slot}: {kind:<6} {p.type:#04x}  sectors {p.start}-{p.end - 1}  {gb(p.size * SECTOR)}")
    if "fat32" in report:
        print(f"FAT32 label {report['fat32']['label']!r}, {report['fat32']['cluster_bytes'] // 1024} KiB clusters")
        for path, value in report["fat32"]["emummc"].items():
            print(f"  {path}: {value}")
    return EXIT_OK


def copy_out(src: BlockDev, size: int, dst: BinaryIO, label: str) -> str:
    """Every sector of `src` into the new file `dst`; returns the SHA-256."""
    digest, progress = hashlib.sha256(), Progress(label, size)
    for done in range(0, size, CHUNK):
        buf = src.read(done, min(CHUNK, size - done))
        digest.update(buf)
        dst.write(buf)
        progress.add(len(buf))
    dst.flush()
    os.fsync(dst.fileno())
    return digest.hexdigest()


def cmd_image(args: argparse.Namespace) -> int:
    disk = check_disk(args.disk, args.expect_bytes)
    out = Path(args.output)
    folder = out.resolve().parent
    if out.exists():
        raise Failure(f"{out} already exists; pick a new name")
    if not folder.is_dir():
        raise Failure(f"{folder} is not a folder")
    with exclusive(disk), BlockDev(disk.raw) as src:
        if args.full:
            regions = [(0, disk.size)]
        else:
            try:
                regions = used_regions(src, disk.size // SECTOR)
            except Failure as e:
                raise Failure(f"{e}. `image --full` copies every sector, whatever the card holds.") from None
        total = sum(length for _, length in regions)
        if not args.full:
            log(f"{gb(total)} of {gb(disk.size)} is allocated; the rest is left out of the image")
        if shutil.disk_usage(folder).free < total + 64 * MIB:
            raise Failure(f"not enough free space for {gb(total)} in {folder}")
        try:
            with create_private(out) as dst:
                if args.full:
                    digest = copy_out(src, disk.size, dst, f"reading {disk.id}")
                else:
                    about = {"tool": f"switchsd {__version__}", "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             "source": disk.name.strip()}
                    digest, kept = write_image(src, disk.size, regions, dst, about, f"reading {disk.id}")
                    log(f"kept {gb(kept)}: the other {gb(total - kept)} is allocated but blank, and left out too")
        except FileExistsError:
            raise Failure(f"{out} already exists; pick a new name") from None
        except OSError as e:
            out.unlink(missing_ok=True)
            hint = " (a FAT32 drive holds no file over 4 GB)" if e.errno == errno.EFBIG else ""
            raise Failure(f"cannot write {out}: {e.strerror}{hint}") from None
        except BaseException:
            out.unlink(missing_ok=True)
            raise
    try:
        if args.full:
            with BlockDev(str(out)) as copy:
                if hash_span(copy, 0, disk.size, "checking the image") != digest:
                    raise Mismatch(f"{out} read back different from what was written")
        else:
            with CompactImage(str(out)) as image:
                image.verify()
    except BaseException as e:
        out.unlink(missing_ok=True)
        if isinstance(e, Mismatch):
            raise Mismatch(f"{e}; the disk it was saved to may be faulty. The image was removed.") from None
        raise
    checksum = out.with_name(out.name + ".sha256")
    try:
        with open(checksum, "x", encoding="utf-8") as sidecar:
            sidecar.write(f"{digest}  {out.name}\n")
    except OSError as e:
        raise Failure(f"the image is complete, but {checksum} could not be written: {e.strerror}") from None
    hand_back(out, checksum)
    log(f"{out}: {gb(out.stat().st_size)}, read back and verified"
        + ("" if args.full else "; `switchsd.py restore` writes it to a card"))
    print(digest)
    return EXIT_OK


def cmd_clone(args: argparse.Namespace) -> int:
    restore = args.restore
    disk = check_disk(args.disk, args.expect_bytes, writing=True)
    sectors = disk.size // SECTOR
    img, img_sectors = open_image(args.image)
    with img:
        mbr = img.read(0, SECTOR)
        parts = parse_mbr(mbr)
        for part in parts:
            if part.end > img_sectors:
                raise Failure(f"{args.image} ends before its partition {part.slot} does; the image is incomplete")
        plan = plan_layout(parts, img_sectors, sectors, restore)
        src = Fat32(img, plan.src_fat.start * SECTOR)
        label = fat_label(src)
        root, skipped, patched, notes = tree_from_fat(src, plan, lenient=restore)
        check_fits(plan.fat.start, plan.fat.size, root)
        log(describe(plan, label, restore))
        for note in notes:
            log(f"note: {note}")
        if skipped:
            log("left out (macOS/Windows metadata): " + ", ".join(skipped))
        if args.dry_run:
            for path in patched:
                log(f"would point {path} at the new emuMMC sector")
            return EXIT_OK
        confirm(args, disk, "This erases everything")
        if not args.skip_check:
            checked = check_image(img, args.image)
            log(f"image verified: it {checked}" if checked
                else "note: the image has no checksum, so it cannot be checked for damage first")

        def clone_emummc(dst: BlockDev) -> None:
            src_off, dst_off = plan.span[0] * SECTOR, plan.emummc[0].start * SECTOR
            length = (plan.span[1] - plan.span[0]) * SECTOR
            want = copy_span(img, src_off, dst, dst_off, length, "cloning emuMMC")
            if hash_span(dst, dst_off, length, "verifying emuMMC") != want:
                raise Mismatch("the emuMMC on the card differs from the image; the card may be faulty")
            log("emuMMC verified: identical to the image")

        files = build_card(disk, plan.parts, label, root, args.skip_probe, mbr, clone_emummc if plan.emummc else None)
        for path in patched:
            log(f"{path}: now points at the new emuMMC sector")
    print(f"{disk.id} is {'restored' if restore else 'ready'}: {'emuMMC cloned, ' if plan.emummc else ''}"
          f"{files} files copied. Eject it and put it in the Switch.")
    return EXIT_OK


def build_card(disk: Disk, parts: list[Part], label: str, root: Node, skip_probe: bool,
               boot_code: bytes = bytes(440), before_files: Callable[[BlockDev], None] | None = None) -> int:
    """Erase the card and write it anew; returns the number of files written.

    The order is the safety: the old partition table goes first, so that an interrupted run leaves a
    card that is plainly empty. Then whatever `before_files` puts down, such as a cloned emuMMC, the
    FAT32 volume holding `root` in parts[0], each read back, and the new partition table last. What lay
    in front of the first partition is zeroed, as hekate's partition manager leaves it.
    """
    label_bytes(label)
    sectors = disk.size // SECTOR
    with opened_for_writing(disk) as dst:
        forget_old_layout(dst)
        if not skip_probe:
            probe(dst, sectors)
        if before_files:
            before_files(dst)
        dst.zero(SECTOR, ALIGN * SECTOR - SECTOR)
        total = sum(n.size for n in flatten(root).values())
        files, _ = write_fat32(dst, parts[0].start, parts[0].size, label, root,
                               Progress("writing files", max(total, 1)).add)
        checked = verify_fat32(dst, parts[0].start, root)
        if checked:
            log(f"FAT32 verified: {checked} files and folders read back identical")
        dst.write(0, build_mbr(boot_code, parts, secrets.randbits(32)))
        dst.sync()
    reread(disk)
    wait_for(disk, [(p.start * SECTOR, p.size * SECTOR) for p in parts])
    return files


def cmd_format(args: argparse.Namespace) -> int:
    label_bytes(args.label)
    disk = check_disk(args.disk, args.expect_bytes, writing=True)
    part = format_layout(disk.size // SECTOR)
    log(f"new card: FAT32 {args.label.upper()!r}, sectors {part.start}-{part.end - 1}, {gb(part.size * SECTOR)}, "
        f"{geometry(part.start, part.size)[0] * SECTOR // 1024} KiB clusters; it starts at 16 MiB, as hekate's does")
    if args.dry_run:
        return EXIT_OK
    confirm(args, disk, "This erases everything")
    build_card(disk, [part], args.label, Node("", True, ATTR_DIR, now_times()), args.skip_probe)
    print(f"{disk.id} is formatted. Next: `switchsd.py setup` on its mount point, then create the emuMMC in hekate.")
    return EXIT_OK


def disk_args(p: argparse.ArgumentParser, required: bool) -> None:
    p.add_argument("disk", help="the card: disk5 (macOS), /dev/sdb (Linux), 2 (Windows disk number)")
    p.add_argument("--expect-bytes", type=int, required=required,
                   help="the card's exact size in bytes, as `disks` prints it; guards against the wrong disk")


def write_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="show the plan and stop")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--skip-probe", action="store_true", help="skip the fake-capacity test")


def add_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("disks", help="list the cards and other removable disks, with the exact size the raw "
                                     "commands ask for")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_disks)

    p = sub.add_parser("info", help="partitions and emuMMC settings of a card image, or of a card (raw, admin)")
    p.add_argument("target", help="image file, or a card such as disk6, /dev/sdb or 2")
    p.add_argument("--verify", action="store_true", help="also compare an image file with its checksum")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("image", help="back up a card to one file: the emuMMC and everything in use (raw, admin)")
    disk_args(p, False)
    p.add_argument("output", help="image file to create")
    p.add_argument("--full", action="store_true",
                   help="copy every sector into a plain raw image, as big as the card, instead of only what is in use")
    p.set_defaults(func=cmd_image)

    for name, text in (("clone", "move a card with an emuMMC partition onto a bigger card (raw, admin; erases)"),
                       ("restore", "write a backup image to a card of any size that holds it (raw, admin; erases)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("image", help="image of the card, from `image`")
        disk_args(p, True)
        write_args(p)
        p.add_argument("--skip-check", action="store_true",
                       help="do not compare the image with its checksum before writing")
        p.set_defaults(func=cmd_clone, restore=name == "restore")

    p = sub.add_parser("format", help="format a new card FAT32 where hekate puts it, on any OS (raw, admin; erases)")
    disk_args(p, True)
    p.add_argument("--label", default="SWITCH SD", help="volume label (default: SWITCH SD)")
    write_args(p)
    p.set_defaults(func=cmd_format)
