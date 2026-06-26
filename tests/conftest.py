"""Shared pytest hooks.

When the goldens are (re)generated (``OMNI_REGEN_GOLDEN=1``), stitch them into one 6x review
sheet so the whole batch can be eyeballed at once instead of zooming an image viewer into each
128x64 PNG. The sheet lands at ``build/golden_contact_sheet.png`` (git-ignored); it is a review
artifact only — the committed goldens stay 1 LED = 1 px so they diff exactly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omni.preview.contact_sheet import write_contact_sheet

_GOLDEN_DIR = Path(__file__).parent / "golden"
_SHEET = Path(__file__).parent.parent / "build" / "golden_contact_sheet.png"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not os.environ.get("OMNI_REGEN_GOLDEN"):
        return  # only after an intentional golden regen, never on an ordinary run
    count = write_contact_sheet(_GOLDEN_DIR, _SHEET, scale=6)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"[golden] contact sheet: {_SHEET} ({count} panels at 6x)")
