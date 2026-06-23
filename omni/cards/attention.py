"""AttentionPolicy: how insistently a card competes for the screen.

Separate from `DisplayPriority` (which only *ranks* cards): a policy says how a card
should *behave* — `NORMAL` rotation, a brief `BURST` takeover after a scoring play, a
periodic `RECURRING` reminder for an active no-hitter, or a persistent `BADGE` shown
alongside. Crucially it is **bounded**: a BURST monopolizes only for `takeover_for`
then yields, and RECURRING is capped by `cooldown` / `max_repeats` — so high-priority
content is seen *without burying normal updates forever* (the verdict's High #3 fix
for "ALERT = indefinite full-screen monopoly"). The queue reads this; it never lets
display priority alone grant a permanent takeover.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import StrEnumMixin
from omni.core.time import DurationSeconds

__all__ = ["AttentionMode", "AttentionPolicy", "NORMAL_ATTENTION"]


class AttentionMode(StrEnumMixin, str, Enum):
    """How a card asks to be surfaced."""

    NORMAL = "normal"  # rotate fairly, like everything else
    BURST = "burst"  # a brief bounded takeover (a scoring play), then yield
    RECURRING = "recurring"  # resurface periodically (an active no-hitter), not constantly
    BADGE = "badge"  # a persistent marker shown alongside, never a takeover


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionPolicy:
    """A card's surfacing behavior — bounded so it can never monopolize forever."""

    mode: AttentionMode
    takeover_for: DurationSeconds = DurationSeconds(0)  # how long a BURST holds the screen
    cooldown: DurationSeconds = DurationSeconds(0)  # min gap before it may burst/recur again
    max_repeats: int | None = None  # cap on RECURRING resurfacings (None = unbounded count)

    def __post_init__(self) -> None:
        if self.max_repeats is not None and self.max_repeats < 0:
            raise ValueError("max_repeats cannot be negative")


# The default for a card that just wants fair rotation.
NORMAL_ATTENTION = AttentionPolicy(mode=AttentionMode.NORMAL)
