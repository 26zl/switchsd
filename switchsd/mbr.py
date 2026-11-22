"""MBR partition tables and hekate's card layout."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .blockdev import SECTOR
from .common import Failure, Usage

MIB_SECTORS = (1 << 20) // SECTOR
ALIGN = 16 * MIB_SECTORS  # hekate starts the FAT32 partition and aligns partitions on 16 MiB
FAT32_TYPES = (0x0B, 0x0C)
FAT32_LBA = 0x0C
EMUMMC_TYPE = 0xE0


@dataclass(frozen=True)
class Part:
    slot: int  # 1-4, the entry in the MBR
    type: int
    start: int  # sectors
    size: int  # sectors

    @property
    def end(self) -> int:
        return self.start + self.size


def parse_mbr(sector0: bytes) -> list[Part]:
    if len(sector0) < SECTOR or sector0[510:512] != b"\x55\xaa":
        raise Failure("sector 0 holds no MBR")
    parts = []
    for slot in range(1, 5):
        entry = sector0[446 + 16 * (slot - 1):446 + 16 * slot]
        start, size = struct.unpack_from("<II", entry, 8)
        if entry[4] and size:
            parts.append(Part(slot, entry[4], start, size))
    if any(p.type == 0xEE for p in parts):
        raise Failure("the card is GPT-partitioned; only hekate's MBR layout is supported")
    by_start = sorted(parts, key=lambda p: p.start)
    for a, b in zip(by_start, by_start[1:]):
        if a.end > b.start:
            raise Failure(f"MBR partitions {a.slot} and {b.slot} overlap")
    return parts


def build_mbr(template: bytes, parts: list[Part], disk_id: int) -> bytes:
    """Keep the boot code area of `template`, replace the partition table."""
    table = bytearray(64)
    for p in parts:
        entry = 16 * (p.slot - 1)
        table[entry + 4] = p.type
        struct.pack_into("<II", table, entry + 8, p.start, p.size)
    return template[:440].ljust(440, b"\0") + struct.pack("<IH", disk_id, 0) + bytes(table) + b"\x55\xaa"


@dataclass(frozen=True)
class Plan:
    src_fat: Part
    src_emummc: tuple[Part, ...]
    fat: Part
    emummc: tuple[Part, ...]

    @property
    def span(self) -> tuple[int, int]:
        """Sectors [start, end) of the old card that hold the emuMMC partitions; empty without any."""
        if not self.src_emummc:
            return 0, 0
        return self.src_emummc[0].start, self.src_emummc[-1].end

    @property
    def shift(self) -> int:
        return self.emummc[0].start - self.src_emummc[0].start if self.emummc else 0

    @property
    def parts(self) -> list[Part]:
        return [self.fat, *self.emummc]


def card_parts(parts: list[Part]) -> tuple[Part, list[Part]]:
    """The FAT32 partition and the emuMMC partitions behind it; any other layout is refused."""
    fats = [p for p in parts if p.type in FAT32_TYPES]
    emus = sorted((p for p in parts if p.type == EMUMMC_TYPE), key=lambda p: p.start)
    other = [p for p in parts if p not in fats and p not in emus]
    if len(fats) != 1 or fats[0].slot != 1:
        raise Failure("expected exactly one FAT32 partition, in MBR slot 1")
    if other:
        types = ", ".join(f"{p.type:#04x}" for p in other)
        raise Failure(f"unsupported partition types {types}: Linux and Android partitions are not handled")
    if emus and emus[0].start < fats[0].end:
        raise Failure("the emuMMC partition must come after the FAT32 partition, as hekate places it")
    return fats[0], emus


def fat_in_front(dst_sectors: int, tail: int) -> Part:
    """hekate's FAT32 partition on a card whose end holds `tail` sectors of emuMMC partitions.

    Its partition manager rounds the card down to 16 MiB, starts the FAT32 partition at 16 MiB and sets
    aside one MiB more than the emuMMC partitions span.
    """
    card_mib = dst_sectors // MIB_SECTORS // 16 * 16
    fat_mib = (card_mib - ALIGN // MIB_SECTORS - (-(-tail // MIB_SECTORS) + 1)) // 16 * 16
    if fat_mib < 64:
        raise Failure("the card is too small for this emuMMC")
    return Part(1, FAT32_LBA, ALIGN, fat_mib * MIB_SECTORS)


def plan_layout(parts: list[Part], src_sectors: int, dst_sectors: int, restore: bool = False) -> Plan:
    """Lay the new card out the way hekate's partition manager would, emuMMC sizes unchanged.

    Moving to a new card (`restore` False) insists on a bigger card with an emuMMC partition to move.
    Restoring a backup takes any card the layout fits on, with or without an emuMMC partition; whether
    the files fit is for the caller to check.
    """
    src_fat, emus = card_parts(parts)
    if not emus:
        if not restore:
            raise Failure("no emuMMC partition (type 0xE0): copying the files is all this card needs")
        return Plan(src_fat, (), format_layout(dst_sectors), ())
    if dst_sectors <= src_sectors and not restore:
        raise Failure("the new card must be bigger than the old one")
    fat = fat_in_front(dst_sectors, emus[-1].end - emus[0].start)
    shift = fat.end - emus[0].start
    emummc = tuple(Part(p.slot, p.type, p.start + shift, p.size) for p in emus)
    if emummc[-1].end > dst_sectors or (fat.size < src_fat.size and not restore):
        raise Failure("the new layout does not fit on the card")
    return Plan(src_fat, tuple(emus), fat, emummc)


# hekate's emuMMC sizes in MiB, as its partition manager counts them: a whole 32 GB or 64 GB eMMC, or a
# resized one of 4 to 24 GiB plus 32 MiB for BOOT0, BOOT1 and alignment.
EMUMMC_FULL = {"full": 29856, "full64": 59680}
EMUMMC_RESIZED = range(4, 25)
HEKATE_MIN_FAT_MIB = 2048


def emummc_mib(size: str) -> int:
    """The partition manager's size for `full`, `full64` or a number of GiB; 0 for no emuMMC partition."""
    text = size.strip().lower()
    if text in ("", "0", "none"):
        return 0
    if text in EMUMMC_FULL:
        return EMUMMC_FULL[text]
    if text.isdigit() and int(text) in EMUMMC_RESIZED:
        return int(text) * 1024 + 32
    raise Usage(f"{size!r} is not an emuMMC size: use full (32 GB consoles), full64 (the OLED model) or "
                f"{EMUMMC_RESIZED[0]} to {EMUMMC_RESIZED[-1]} for that many GiB")


def new_layout(dst_sectors: int, emummc: int) -> list[Part]:
    """An empty card as hekate's partition manager lays it out, with an emuMMC partition of `emummc` MiB.

    hekate's Create emuMMC takes that partition as it is. 0 gives one FAT32 partition and no emuMMC.
    """
    if not emummc:
        return [format_layout(dst_sectors)]
    size = (emummc - 1) * MIB_SECTORS  # hekate leaves the last MiB of the size out of the partition
    fat = fat_in_front(dst_sectors, size)
    if fat.size < HEKATE_MIN_FAT_MIB * MIB_SECTORS:
        raise Failure("that emuMMC leaves under 2 GB for files, which hekate does not allow either; pick a "
                      "smaller emuMMC or a bigger card")
    return [fat, Part(2, EMUMMC_TYPE, fat.end, size)]


def format_layout(dst_sectors: int) -> Part:
    """One FAT32 partition from 16 MiB to the last whole MiB. hekate starts there too and runs to the card's end."""
    size = (dst_sectors - ALIGN) // MIB_SECTORS * MIB_SECTORS
    if size < 64 * MIB_SECTORS:
        raise Failure("the card is too small")
    return Part(1, FAT32_LBA, ALIGN, size)

