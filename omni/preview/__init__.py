"""On-screen preview: assemble a card from a fixture and draw it to the emulator.

`omni.preview.scenario` builds a card from a self-contained scenario fixture
(schedule row + game feed) through the real provider + factory; `omni.preview.cli`
renders it onto an `RGBMatrixEmulator` matrix via `MatrixCanvas`.
"""

from __future__ import annotations

from omni.preview.scenario import build_card_from_scenario

__all__ = ["build_card_from_scenario"]
