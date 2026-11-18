"""The board policies a run can decide with, by their published names.

Kept apart from the app (`omni.app.versions` re-exports it), so the policy's own modules and the board can name a
policy without importing the running app.
"""

from __future__ import annotations

from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["PolicyName"]


class PolicyName(StrEnumMixin, str, Enum):
    """A board policy a run can decide with, by its published name."""

    BOARD_V1 = "omni-board-policy/1"  # the shared board's scheduler bands (`omni.queue.scheduler`)
    BOARD_V2 = "omni-board-policy/2"  # `BOARD_POLICY.md`: leverage, ranked favorites, debt and interrupts
    NFL_SOLO_V1 = "omni-nfl-solo/1"  # the NFL single-game runner's own focus (`omni.app.football_board`)
