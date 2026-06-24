"""Baseball domain value objects and live game state.

These are foundation types — a half-inning, a balls/strikes/outs count, base
occupancy, and the live game-state snapshot a provider observes. Events and
cards build on them, so they live in `domain` and import from here directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import HomeAway, StrEnumMixin

__all__ = [
    "InningPhase",
    "PitchType",
    "PitchingDecisions",
    "BaseballCount",
    "BaseballBaseState",
    "BaseballGameState",
    "no_hitter_side",
]


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


class PitchType(StrEnumMixin, str, Enum):
    """A pitch type from the StatsAPI `pitchTypes` taxonomy; the value is the StatsAPI code.

    Replaces a raw pitch-code string on a play so a play's decisive pitch is comparable and
    renderable without stringly-typed checks. The taxonomy is closed and slow-moving — a new
    pitch like the sweeper (`ST`) is a rare addition — so an enum is the right shape. The enum
    value doubles as the short display code (`"FF"`, `"SL"`); :attr:`label` is the long name.
    Coerce a raw code with :func:`omni.core.enum.try_coerce_enum` (None on an unknown code).
    """

    AUTOMATIC_BALL = "AB"
    AUTOMATIC_STRIKE = "AS"
    CHANGEUP = "CH"
    CURVEBALL = "CU"
    SLOW_CURVE = "CS"
    EEPHUS = "EP"
    CUTTER = "FC"
    FASTBALL = "FA"
    FOUR_SEAM_FASTBALL = "FF"
    FORKBALL = "FO"
    SPLITTER = "FS"
    TWO_SEAM_FASTBALL = "FT"
    GYROBALL = "GY"
    INTENTIONAL_BALL = "IN"
    KNUCKLE_CURVE = "KC"
    KNUCKLEBALL = "KN"
    NO_PITCH = "NP"
    PITCHOUT = "PO"
    SCREWBALL = "SC"
    SINKER = "SI"
    SLIDER = "SL"
    SWEEPER = "ST"
    SLURVE = "SV"
    UNKNOWN = "UN"

    @property
    def label(self) -> str:
        """The long human name (e.g. ``Four-Seam Fastball``) for a larger panel or a log."""
        return _PITCH_LABELS[self]


_PITCH_LABELS: dict[PitchType, str] = {
    PitchType.AUTOMATIC_BALL: "Automatic Ball",
    PitchType.AUTOMATIC_STRIKE: "Automatic Strike",
    PitchType.CHANGEUP: "Changeup",
    PitchType.CURVEBALL: "Curveball",
    PitchType.SLOW_CURVE: "Slow Curve",
    PitchType.EEPHUS: "Eephus",
    PitchType.CUTTER: "Cutter",
    PitchType.FASTBALL: "Fastball",
    PitchType.FOUR_SEAM_FASTBALL: "Four-Seam Fastball",
    PitchType.FORKBALL: "Forkball",
    PitchType.SPLITTER: "Splitter",
    PitchType.TWO_SEAM_FASTBALL: "Two-Seam Fastball",
    PitchType.GYROBALL: "Gyroball",
    PitchType.INTENTIONAL_BALL: "Intentional Ball",
    PitchType.KNUCKLE_CURVE: "Knuckle Curve",
    PitchType.KNUCKLEBALL: "Knuckleball",
    PitchType.NO_PITCH: "No Pitch",
    PitchType.PITCHOUT: "Pitchout",
    PitchType.SCREWBALL: "Screwball",
    PitchType.SINKER: "Sinker",
    PitchType.SLIDER: "Slider",
    PitchType.SWEEPER: "Sweeper",
    PitchType.SLURVE: "Slurve",
    PitchType.UNKNOWN: "Unknown",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class PitchingDecisions:
    """The winning and losing pitchers of a completed game, and the save if one was earned.

    Names are full names as the feed gives them (a renderer shortens to a last name for a
    small panel). A game with no decision — one still in progress, a tie, or a feed missing
    the block — is the *absence* of this object, not a `PitchingDecisions` with blank fields;
    so `winner` and `loser` are always present and only `save` is optional.
    """

    winner: str
    loser: str
    save: str | None = None

    def __post_init__(self) -> None:
        if not self.winner or not self.loser:
            raise ValueError("a pitching decision needs both a winner and a loser")


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
    away_hits: int = 0
    home_hits: int = 0

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")
        if self.away_hits < 0 or self.home_hits < 0:
            raise ValueError("hits cannot be negative")
        if self.inning < 1:
            raise ValueError("inning must be >= 1")


def no_hitter_side(state: BaseballGameState, *, min_inning: int) -> HomeAway | None:
    """The side throwing an active no-hitter bid in `state`, or None.

    A bid only counts once the game has reached `min_inning` — early hitless innings
    are routine, not news — and the batting side still has zero hits: a hitless away
    team means the home pitching staff is throwing the no-hitter, and vice versa. The
    bid persists across both halves of an inning (it is about hits allowed, not who is
    batting now) until a hit breaks it. If both sides are hitless (a rare double
    no-hitter) the away team's drought is the one reported.
    """
    if state.inning < min_inning:
        return None
    if state.away_hits == 0:
        return HomeAway.HOME
    if state.home_hits == 0:
        return HomeAway.AWAY
    return None
