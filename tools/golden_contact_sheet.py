"""Stitch the committed goldens into one contact sheet for review.

    python tools/golden_contact_sheet.py [--scale 6] [--columns 4] [--pattern 'live_baseball_*.png']

The committed goldens stay 1 LED = 1 px so they diff exactly; this is a *review* view only,
nearest-neighbor upscaled (default 6x) so the LEDs read as crisp squares instead of a 128x64
smudge you have to zoom into. The same sheet is written automatically after a golden regen
(``OMNI_REGEN_GOLDEN=1 pytest -k golden`` -> ``build/golden_contact_sheet.png``; see
tests/conftest.py). Pass ``--pattern`` to focus on one card family.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from omni.preview.contact_sheet import write_contact_sheet

_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Stitch the golden images into a 6x contact sheet.")
    parser.add_argument("--golden-dir", type=Path, default=_ROOT / "tests" / "golden")
    parser.add_argument("--out", type=Path, default=_ROOT / "build" / "golden_contact_sheet.png")
    parser.add_argument("--scale", type=int, default=6, help="LED -> pixel review scale (default 6)")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--pattern", default="*.png", help="filter, e.g. 'live_baseball_*.png'")
    args = parser.parse_args()
    count = write_contact_sheet(args.golden_dir, args.out, scale=args.scale, columns=args.columns, pattern=args.pattern)
    print(f"wrote {args.out} ({count} panels at {args.scale}x)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
