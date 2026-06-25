"""AttentionPolicy: how insistently a card competes for the screen.

Separate from `DisplayPriority` (which only *ranks* cards): a policy says how a card
should *behave* — `NORMAL` rotation, a brief `BURST` takeover after a scoring play, a
periodic `RECURRING` reminder for an active no-hitter, or a persistent `BADGE` shown
alongside. Crucially it is **bounded**: a BURST monopolizes only for `takeover_for`
then yields, and RECURRING is capped by `cooldown` / `max_repeats` — so high-priority
content is seen *without burying normal updates forever*. The queue reads this; it never
lets display priority alone grant a permanent takeover.
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
    """A card's surfacing behavior — bounded so it can never monopolize forever.

    Each knob belongs to exactly one mode (`takeover_for` to BURST, `cooldown`/`max_repeats`
    to RECURRING); a knob set on any other mode would be silently ignored by the queue, so it
    is rejected here rather than read as a working setting. A BURST must take over for a
    positive duration (a zero-length burst never bursts) and a RECURRING needs a positive
    cooldown (or it would resurface every tick, defeating the point) — both invariants the
    queue then trusts instead of re-checking.
    """

    mode: AttentionMode
    takeover_for: DurationSeconds = DurationSeconds(0)  # how long a BURST holds the screen (BURST only)
    cooldown: DurationSeconds = DurationSeconds(0)  # min gap before a RECURRING may resurface (RECURRING only)
    max_repeats: int | None = None  # cap on RECURRING resurfacings (RECURRING only; None = unbounded count)

    def __post_init__(self) -> None:
        if self.mode is AttentionMode.BURST:
            if self.takeover_for.value <= 0:
                raise ValueError("a BURST attention must take over for a positive duration")
        elif self.takeover_for.value != 0:
            raise ValueError("takeover_for is only meaningful for a BURST attention")

        if self.mode is AttentionMode.RECURRING:
            if self.cooldown.value <= 0:
                raise ValueError("a RECURRING attention needs a positive cooldown")
            if self.max_repeats is not None and self.max_repeats < 0:
                raise ValueError("max_repeats cannot be negative")
        else:
            if self.cooldown.value != 0:
                raise ValueError("cooldown is only meaningful for a RECURRING attention")
            if self.max_repeats is not None:
                raise ValueError("max_repeats is only meaningful for a RECURRING attention")


# The default for a card that just wants fair rotation.
NORMAL_ATTENTION = AttentionPolicy(mode=AttentionMode.NORMAL)
