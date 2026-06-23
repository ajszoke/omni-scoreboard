"""Baseball domain value objects and live game state.

These are foundation types — a half-inning, a balls/strikes/outs count, base
occupancy, and the live game-state snapshot a provider observes. Events and
cards build on them, so they live in `domain`; `omni.events.baseball` and
`omni.cards.baseball` re-export the value types for back-compat.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import StrEnumMixin

__all__ = ["InningPhase", "BaseballCount", "BaseballBaseState", "BaseballGameState"]


class InningPhase(StrEnumMixin, str, Enum):
    """The phase of an inning, including the breaks between halves.

    `TOP`/`BOTTOM` are active half-innings (a team is batting); `MIDDLE` (after the
    top) and `END` (after the bottom) are the breaks where there is no active at-bat.
    Modelling the breaks explicitly — instead of collapsing ``Middle``/``End`` into
    the adjacent half — is what lets lifecycle cards show a real "between innings"
    state rather than a stale count. A play/event only ever
    occurs in `TOP`/`BOTTOM`; the break values are reachable only via game state.
    """

    TOP = "top"
    MIDDLE = "middle"
    BOTTOM = "bottom"
    END = "end"

    @property
    def is_break(self) -> bool:
        """True for a between-halves break (`MIDDLE`/`END`) — no active at-bat."""
        return self in (InningPhase.MIDDLE, InningPhase.END)


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballCount:
    """Balls/strikes/outs at the moment of a play."""

    balls: int
    strikes: int
    outs: int

    def __post_init__(self) -> None:
        if self.balls < 0 or self.strikes < 0 or self.outs < 0:
            raise ValueError("balls, strikes, and outs must be non-negative")
        # Terminal maxima: 4th ball (walk), 3rd strike (K), 3rd out (inning end).
        if self.balls > 4 or self.strikes > 3 or self.outs > 3:
            raise ValueError("balls/strikes/outs exceed their terminal maximums (4/3/3)")


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballBaseState:
    """Base occupancy for rendering the diamond (a player model comes later)."""

    first: bool = False
    second: bool = False
    third: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballGameState:
    """A live baseball game's observed state: score/inning/phase/count/bases.

    This is the domain truth a provider produces from the game feed; a
    `CardFactory` maps it into a renderable `LiveBaseballCardPayload` (where
    presentation choices live). Keeping the two separate is the seam between
    "what the game is" and "how a card shows it". ``phase`` carries the full
    inning phase (including breaks); during a break the ``count``/``bases`` are
    between-innings and a renderer should not present them as a live at-bat.
    """

    away_score: int
    home_score: int
    inning: int
    phase: InningPhase
    count: BaseballCount
    bases: BaseballBaseState

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")
        if self.inning < 1:
            raise ValueError("inning must be >= 1")
