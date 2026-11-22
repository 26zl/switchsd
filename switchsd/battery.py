"""The battery, as the console's fuel gauge sees it.

hekate's Console Info > Battery > Dump Fuel Regs saves the 256 registers of the MAX17050 fuel gauge
to backup/<id>/dumps/fuel_gauge.bin, two bytes each. The scaling below is hekate's own
(bdk/power/max17050.c).

The gauge sits on the console's board, not in the battery. Its designed capacity is what the
console is set for; its full capacity is what it has learned about the battery that is in it, which
takes a few full charges after a battery is replaced.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

DUMP = "backup/*/dumps/fuel_gauge.bin"
SIZE = 0x200
REP_SOC, TEMP, VCELL, FULL_CAP, CYCLES, DESIGN_CAP = 0x06, 0x08, 0x09, 0x10, 0x17, 0x18
UNSET = 1000  # mAh: the designed capacity a gauge reports that the console has not configured yet
WORN = 80  # percent of the designed capacity below which a battery counts as worn
LEARNING = 5  # charge cycles a gauge needs after a reset before its full capacity says much


@dataclass(frozen=True)
class Gauge:
    designed: float  # mAh
    full: float  # mAh
    charge: float  # percent
    cycles: float
    celsius: float
    millivolts: int

    @property
    def health(self) -> float:
        """The full capacity as a percentage of the designed one."""
        return 100 * self.full / self.designed


def parse(data: bytes) -> Gauge | None:
    """The gauge's readings, or None when these bytes are no dump of a configured gauge."""
    if len(data) != SIZE:
        return None
    reg = struct.unpack("<256H", data)
    designed, full = reg[DESIGN_CAP] / 2, reg[FULL_CAP] / 2  # one step is 0.5 mAh
    if designed in (0, UNSET) or not full or 0xFFFF in (reg[DESIGN_CAP], reg[FULL_CAP]):
        return None
    signed_temp = reg[TEMP] - 0x10000 if reg[TEMP] & 0x8000 else reg[TEMP]
    return Gauge(designed, full, reg[REP_SOC] / 256, reg[CYCLES] / 100, signed_temp / 256,
                 (reg[VCELL] >> 3) * 625 // 1000)


def newest_dump(root: Path) -> Path | None:
    """The latest fuel gauge dump on a card, if hekate has made one."""
    found = [path for path in root.glob(DUMP) if path.is_file()]
    return max(found, key=lambda path: path.stat().st_mtime) if found else None
