#!/usr/bin/env python3
"""Run switchsd from a checkout: python3 switchsd.py --help"""

import sys
from pathlib import Path

# `python3 -I` leaves the script's folder off sys.path, so add it explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from switchsd.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
