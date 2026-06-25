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
    "WinProbability",
    "BaseballCount",
    "BaseballBaseState",
    "BaseballScoringImpact",
    "BaseballGameState",
    "PitchingFeatKind",
    "PitchingFeatProgress",
    "pitching_feat_progress",
    "scoring_impact",
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
class WinProbability:
    """Live in-game win probability for the two sides, as percentages.

    Sourced from the feed (MLB StatsAPI `game_contextMetrics`); the two normally sum to
    ~100. The *absence* of this object means no probability is available yet (a game not
    started, or a feed without the block) — never a 50/50 placeholder. `favored` is the
    leading side, or None at an exact tie. General to any home/away contest, not baseball-
    specific, but lives here with the other live-game values the MLB feed produces.
    """

    home: float
    away: float

    def __post_init__(self) -> None:
        for pct in (self.home, self.away):
            if not 0.0 <= pct <= 100.0:
                raise ValueError("win probability percentages must be in 0..100")

    @property
    def favored(self) -> HomeAway | None:
        """The leading side, or None when the two are exactly even."""
        if self.home > self.away:
            return HomeAway.HOME
        if self.away > self.home:
            return HomeAway.AWAY
        return None

    def percent_for(self, side: HomeAway) -> float:
        """This side's win-probability percentage."""
        return self.home if side is HomeAway.HOME else self.away


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballScoringImpact:
    """How a play changed the score and the game's competitive state.

    Lets a play's drama be judged by what it *did* — drove in the go-ahead run —
    rather than its bare type (a single). `rbi` is the runs the batter is credited
    with: the free, reliable signal on a mapped play. A rare run without an RBI (a
    batter reaching on an error) is not counted as scoring here.
    """

    rbi: int
    tying: bool = False  # the play levelled the score
    go_ahead: bool = False  # the play put the batting side ahead
    walk_off: bool = False  # a go-ahead run that ended the game (home, bottom of the 9th or later)

    def __post_init__(self) -> None:
        if self.rbi < 0:
            raise ValueError("rbi cannot be negative")
        if self.walk_off and not self.go_ahead:
            raise ValueError("a walk-off is a kind of go-ahead")
        if (self.go_ahead or self.tying) and not self.scored:
            raise ValueError("tying/go-ahead require a run to have scored")
        if self.go_ahead and self.tying:
            raise ValueError("a play cannot both tie the game and take the lead")

    @property
    def scored(self) -> bool:
        """Whether the play drove in at least one run."""
        return self.rbi > 0


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
    # Whether each side has reached base at all this game: True (a baserunner is confirmed),
    # False (confirmed none — a perfect game in the making), or None (the feed does not say).
    # It only matters for a side being no-hit, where it is what separates a perfect game from
    # a plain no-hitter; None keeps the safe default of never claiming perfection unproven.
    away_reached_base: bool | None = None
    home_reached_base: bool | None = None

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")
        if self.away_hits < 0 or self.home_hits < 0:
            raise ValueError("hits cannot be negative")
        if self.inning < 1:
            raise ValueError("inning must be >= 1")


class PitchingFeatKind(StrEnumMixin, str, Enum):
    """A no-hit pitching feat in progress: a plain no-hitter, or a perfect game.

    A perfect game is a no-hitter with no baserunner allowed at all, so it strictly
    outranks one — the same card, a stronger headline.
    """

    NO_HITTER = "no_hitter"
    PERFECT_GAME = "perfect_game"


@dataclass(frozen=True, slots=True, kw_only=True)
class PitchingFeatProgress:
    """How far a no-hit pitching feat has carried, and which side is throwing it.

    `side` is the *pitching* (defending) side — the away team being hitless means the home
    staff is throwing it. `completed_innings` counts that side's *finished* defensive half-
    innings, so a bid in the top of the 6th is "through 5", not 6; it is what a card shows as
    "through N". `kind` distinguishes a perfect game from a plain no-hitter.
    """

    side: HomeAway
    kind: PitchingFeatKind
    completed_innings: int

    def __post_init__(self) -> None:
        if self.completed_innings < 1:
            raise ValueError("a pitching feat must have at least one completed inning")

    @property
    def perfect(self) -> bool:
        """True for a perfect game (no baserunner allowed), False for a plain no-hitter."""
        return self.kind is PitchingFeatKind.PERFECT_GAME


def _no_hit_pitching_side(state: BaseballGameState) -> HomeAway | None:
    """The side throwing a no-hit bid (the hitless side's opponent), or None if both have hit.

    A hitless away team means the home staff is throwing it, and vice versa. The bid is about
    hits *allowed*, so it persists across both halves of an inning until a hit breaks it. If
    both sides are hitless — a rare double no-hitter — the home side is reported: it pitches the
    top of each inning, so it has always finished at least as many innings as the away side,
    making it the deeper (or equal) of the two bids.
    """
    if state.away_hits == 0:
        return HomeAway.HOME
    if state.home_hits == 0:
        return HomeAway.AWAY
    return None


def _completed_defensive_innings(*, pitching: HomeAway, inning: int, phase: InningPhase) -> int:
    """How many defensive half-innings `pitching` has *finished* at `inning`/`phase`.

    The home side pitches the top of each inning, the away side the bottom; a half-inning
    counts as finished only once play has moved past it. So in the top of the 6th the home
    pitcher has finished 5 (not 6), and the away pitcher finishes its 6th only at the End break.
    """
    if pitching is HomeAway.HOME:
        # The top is still in progress during TOP (one fewer finished); past it by Middle/Bottom/End.
        return inning - 1 if phase is InningPhase.TOP else inning
    # The bottom is finished only at the End break; before that the away pitcher is mid- or pre-inning.
    return inning if phase is InningPhase.END else inning - 1


def pitching_feat_progress(state: BaseballGameState, *, min_completed_innings: int) -> PitchingFeatProgress | None:
    """The no-hit feat `state` shows once it is deep enough to be news, or None.

    A bid is surfaced only once the pitching side has *finished* `min_completed_innings`
    hitless innings — counted per side, so the bid can't surface a half-inning early. It is a
    perfect game only when the batting side is *confirmed* not to have reached base
    (`reached_base is False`); an unknown reached-base state (None) stays a plain no-hitter, so
    the rarer claim is never made without evidence.
    """
    pitching = _no_hit_pitching_side(state)
    if pitching is None:
        return None
    completed = _completed_defensive_innings(pitching=pitching, inning=state.inning, phase=state.phase)
    if completed < min_completed_innings:
        return None
    reached = state.away_reached_base if pitching is HomeAway.HOME else state.home_reached_base
    kind = PitchingFeatKind.PERFECT_GAME if reached is False else PitchingFeatKind.NO_HITTER
    return PitchingFeatProgress(side=pitching, kind=kind, completed_innings=completed)


_WALK_OFF_MIN_INNING = 9  # a game cannot end on a home run before the bottom of the 9th


def scoring_impact(
    *, phase: InningPhase, inning: int, rbi: int, away_score: int | None, home_score: int | None
) -> BaseballScoringImpact:
    """Classify a play's scoring impact from its RBI and the resulting score.

    The impact is empty unless the play drove in a run (`rbi > 0`) and the resulting
    score is known. The batting side is read from the half (`BOTTOM` -> home bats);
    `tying`/`go_ahead`/`walk_off` follow from the resulting lead and the runs the play is
    credited with. A walk-off is the home side taking the lead in the bottom of the 9th
    or later — the run that ends the game.
    """
    if rbi <= 0 or away_score is None or home_score is None:
        return BaseballScoringImpact(rbi=max(rbi, 0))
    batting_home = phase is InningPhase.BOTTOM
    lead = (home_score - away_score) if batting_home else (away_score - home_score)
    go_ahead = 0 < lead <= rbi  # the play moved the batting side from not-ahead to ahead
    return BaseballScoringImpact(
        rbi=rbi,
        tying=away_score == home_score,
        go_ahead=go_ahead,
        walk_off=go_ahead and batting_home and inning >= _WALK_OFF_MIN_INNING,
    )
