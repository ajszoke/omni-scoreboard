"""Print the NFLMeta budget replay's report: a synthetic November read at each strategy, on each plan.

    .venv/bin/python tools/nflmeta_budget.py > tests/fixtures/replay/nflmeta-budget.md

Everything is synthetic and declared in `omni.replay.nfl_budget`: no key, no network and no recording. The
committed copy of the report is checked against the replay by the tests, so a change to the ledger's rules
or the billing model shows up as a diff in it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.replay.nfl_budget import budget_report  # noqa: E402


def main() -> int:
    sys.stdout.write(budget_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
