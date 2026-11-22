"""emuMMC/emummc.ini and emuMMC/RAW*/raw_based hold the emuMMC's absolute sector; moving it means editing both."""

from __future__ import annotations

import re
import struct
from collections.abc import Callable

from .common import Failure
from .mbr import Plan


def remap_sector(value: int, plan: Plan, what: str) -> int:
    start, end = plan.span
    if not start <= value < end:
        raise Failure(f"{what} points at sector {value:#x}, outside the emuMMC partition "
                      f"({start:#x}-{end - 1:#x}); refusing to guess")
    return value + plan.shift


# hekate reads the value with strtol(..., 16): hexadecimal, with or without the 0x.
_SECTOR_LINE = re.compile(r"(?i)^(\s*sector\s*=\s*)((?:0x)?[0-9a-f]+)(\s*)$")


def patch_emummc_ini(data: bytes, plan: Plan) -> bytes:
    """Move `sector=` in the [emummc] section; everything else, line endings included, stays."""
    out, section = [], ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Failure("emummc.ini is not UTF-8 text") from None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
        match = _SECTOR_LINE.match(body)
        if section == "emummc" and match and int(match.group(2), 16):
            new = remap_sector(int(match.group(2), 16), plan, "emummc.ini")
            body = f"{match.group(1)}{new:#x}{match.group(3)}"
        out.append(body + ending)
    return "".join(out).encode("utf-8")


def patch_raw_based(data: bytes, plan: Plan) -> bytes:
    if len(data) != 4:
        raise Failure(f"raw_based holds {len(data)} bytes, expected a 4-byte sector number")
    return struct.pack("<I", remap_sector(struct.unpack("<I", data)[0], plan, "raw_based"))


def config_patcher(path: str) -> Callable[[bytes, Plan], bytes] | None:
    parts = path.lower().split("/")
    if parts == ["emummc", "emummc.ini"]:
        return patch_emummc_ini
    if len(parts) == 3 and parts[0] == "emummc" and parts[2] == "raw_based":
        return patch_raw_based
    return None
