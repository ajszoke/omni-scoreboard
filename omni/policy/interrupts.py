"""Interrupts: what a big moment elsewhere is worth, and the bar it must clear to cut in (`BOARD_POLICY.md` §4).

An event's interrupt value is

    I = B * (0.5 + 0.5*z_pre) * (0.65 + 0.35*Q + 0.35*J + 0.25*C_season)

- ``B`` is its class's weight: a confirmed perfect game, a no-hitter, a decisive late winner, a win swing, another
  major competitive event, or routine (worth nothing).
- ``z_pre`` is the leverage of the released state just before it, never the terminal state's. A win swing takes
  ``z_pre = 1``: the swing it made is the evidence of what was at stake, so the time left doesn't discount it.
- ``Q`` is the matchup's quality (0 when either record is unknown); ``J`` and ``C_season`` its stakes.

The classifiers read our typed events and the released states on either side of them, and NFL's also reads how far
the market's win probability moved across the event (`win_swing`). Wherever the evidence a class needs isn't there,
they classify lower and say why (`ClassBasis`), so the trace can print it.

The bar an event must clear depends on what the healthy live favorites are doing: 1.10 while any is in action,
0.85 while none is and one is in a valid micro-break, 0.35 when all are in ordinary breaks or none is live.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import Enum

from omni.core.enum import HomeAway, StrEnumMixin
from omni.domain.baseball import (
    BaseballField,
    BaseballGameState,
    InningPhase,
    WinProbability,
    pitching_feat_progress,
)
from omni.domain.football import FootballField, FootballGameState, FootballInterval
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload
from omni.events.football import FootballEvent, FootballEventKind
from omni.policy.leverage import Leverage, ModelStatus

__all__ = [
    "BarContext",
    "ClassBasis",
    "Classification",
    "FavoriteActivity",
    "InterruptBars",
    "InterruptClass",
    "InterruptPolicy",
    "InterruptValue",
    "LeverageBasis",
    "SWING_KINDS",
    "Stakes",
    "bar_context",
    "classify_baseball_final",
    "classify_baseball_play",
    "classify_football_event",
    "interrupt_value",
    "win_swing",
]


class InterruptClass(StrEnumMixin, str, Enum):
    """What kind of moment an event is, which sets its weight ``B``."""

    PERFECT_GAME = "perfect_game"  # confirmed: a complete final and a complete reach ledger
    NO_HITTER = "no_hitter"  # confirmed: a complete final with verified hit totals
    DECISIVE_LATE_WINNER = "decisive_late_winner"  # an eligible walk-off, or a result-changing reversal
    WIN_SWING = "win_swing"  # a play that moved the market's win probability by the policy's points, whenever it came
    MAJOR = "major"  # another major competitive event: a close-game home run, a late one-score turnover
    ROUTINE = "routine"  # routine scoring, bookkeeping, an injury notice: the normal field, strip and status

    @property
    def feat(self) -> bool:
        """A rare feat: its value doesn't rest on leverage, so it uses ``z_pre = 0``."""
        return self in (InterruptClass.PERFECT_GAME, InterruptClass.NO_HITTER)


class ClassBasis(StrEnumMixin, str, Enum):
    """Why an event got its class, or why it was classified lower than it might be."""

    PERFECT_GAME = "perfect_game"  # a complete final: no hit allowed, and the reach ledger shows no one reached
    NO_HITTER = "no_hitter"  # a complete final with verified hits, none allowed; someone reached
    REACH_UNVERIFIED = "reach_unverified"  # no hit allowed, but the reach ledger can't vouch for a perfect game
    NOT_FINAL = "not_final"  # a feat needs the feed's own final
    HITS_UNVERIFIED = "hits_unverified"  # the final's hit totals weren't reported
    NO_FEAT = "no_feat"  # hits allowed, or under nine complete innings, or the last half's end isn't clear
    WALK_OFF = "walk_off"  # the home side took the lead in the 9th or later, ending the game
    GAME_ENDING_LEAD_CHANGE = "game_ending_lead_change"  # late, it put a side ahead, and the game ended on it
    REVERSAL = "reversal"  # late, a ruling changed who leads
    LATE_TIE_OR_LEAD = "late_tie_or_lead"  # a tying or go-ahead play in the 9th or later
    CLOSE_HOME_RUN = "close_home_run"  # a home run that tied, took the lead, or left a one-run game
    LATE_ONE_SCORE = (
        "late_one_score"  # a score, turnover or ruling in the fourth quarter or overtime of a one-score game
    )
    WIN_SWING = "win_swing"  # the market's win probability moved by the policy's points or more across it
    NOT_SCORING = "not_scoring"  # the play drove in no run, or its scoring wasn't worked out
    NOT_MAJOR = "not_major"  # the strip's routine: a play, a stoppage, a flag
    NOT_CLOSE = "not_close"  # neither late and close nor a lead change
    INCOHERENT = "incoherent"  # the states on either side are missing, unreported, or don't line up with it


@dataclass(frozen=True, slots=True)
class Classification:
    """An event's class, and the evidence it rests on (or the gap that kept it lower)."""

    event_class: InterruptClass
    basis: ClassBasis


@dataclass(frozen=True, slots=True, kw_only=True)
class Stakes:
    """What the matchup brings to an interrupt.

    ``quality`` is ``Q`` (None when either team's record is unknown, which counts as 0 for an interrupt);
    ``postseason`` is ``J`` (1 for a confirmed postseason game, 0.5 for an NBA Cup knockout); ``season`` is
    ``C_season`` (1 only for a verified clinch or elimination game).
    """

    quality: float | None = None
    postseason: float = 0.0
    season: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (("quality", self.quality), ("postseason", self.postseason), ("season", self.season)):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"a stake's {name} must be within 0..1, not {value}")


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptPolicy:
    """The interrupt value's weights and the classifiers' thresholds, as `BOARD_POLICY.md` §4 sets them."""

    perfect_game: float = 3.5
    no_hitter: float = 2.8
    decisive_late_winner: float = 1.2
    win_swing: float = 1.2
    major: float = 0.8
    routine: float = 0.0
    leverage_floor: float = 0.5  # the leverage factor is floor + span * z_pre
    leverage_span: float = 0.5
    stakes_floor: float = 0.65  # the stakes factor is floor + q*Q + j*J + season*C_season
    stakes_quality: float = 0.35
    stakes_postseason: float = 0.35
    stakes_season: float = 0.25
    feat_innings: int = 9  # a no-hitter or perfect game needs this many complete innings pitched
    mlb_late_inning: int = 9  # MLB's late: the 9th or extras
    mlb_close_margin: int = 1  # a home run that leaves the game within this many runs is a close-game home run
    nfl_regulation_periods: int = 4
    nfl_late_clock: timedelta = timedelta(minutes=5)  # NFL's late: the fourth quarter at or under this, or overtime
    nfl_one_score: int = 8  # NFL's one-score margin
    win_swing_points: float = 10.0  # a play that moves the win probability this many points is a win swing

    def __post_init__(self) -> None:
        weights = (
            self.perfect_game,
            self.no_hitter,
            self.decisive_late_winner,
            self.win_swing,
            self.major,
            self.routine,
        )
        if any(weight < 0 for weight in weights):
            raise ValueError("an interrupt class's weight can't be negative")
        if self.feat_innings < 1 or self.mlb_late_inning < 1 or self.nfl_regulation_periods < 1:
            raise ValueError("innings and periods count from one")
        if self.win_swing_points <= 0:
            raise ValueError("a win swing's threshold must be positive")

    def weight(self, event_class: InterruptClass) -> float:
        """``B`` for a class."""
        return {
            InterruptClass.PERFECT_GAME: self.perfect_game,
            InterruptClass.NO_HITTER: self.no_hitter,
            InterruptClass.DECISIVE_LATE_WINNER: self.decisive_late_winner,
            InterruptClass.WIN_SWING: self.win_swing,
            InterruptClass.MAJOR: self.major,
            InterruptClass.ROUTINE: self.routine,
        }[event_class]


class LeverageBasis(StrEnumMixin, str, Enum):
    """What ``z_pre`` came from, which decides whether the event may cut into a favorite's action."""

    FEAT = "feat"  # a verified rare feat: z_pre = 0, and its value needs no leverage, so it may cut into action
    MODEL = "model"  # a validated model's reading: it may cut into action
    PROXY = "proxy"  # the labeled proxy's g in place of z_pre: break-only
    REDUCED = "reduced"  # a model on partial inputs, its g in place of z_pre: break-only
    UNKNOWN = "unknown"  # nothing model-derived: z_pre = 0, break-only
    NONE = "none"  # no released predecessor: z_pre = 0, break-only

    @property
    def qualifies_action(self) -> bool:
        return self in (LeverageBasis.FEAT, LeverageBasis.MODEL)


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptValue:
    """An event's interrupt value ``I``, with what it rests on."""

    event_class: InterruptClass
    value: float
    z_pre: float
    basis: LeverageBasis

    def __post_init__(self) -> None:
        if self.value < 0 or not 0.0 <= self.z_pre <= 1.0:
            raise ValueError("an interrupt value is non-negative, and z_pre is within 0..1")

    @property
    def action_evidence(self) -> bool:
        """Whether its leverage evidence may qualify it to cut into a favorite's action (its value still has to
        clear the action bar)."""
        return self.basis.qualifies_action


_BREAK_ONLY_G = {ModelStatus.PROXY: LeverageBasis.PROXY, ModelStatus.REDUCED: LeverageBasis.REDUCED}


def _z_pre(event_class: InterruptClass, predecessor: Leverage | None) -> tuple[float, LeverageBasis]:
    if event_class.feat:
        return 0.0, LeverageBasis.FEAT
    z_pre, basis = _predecessor_z(predecessor)
    if event_class is InterruptClass.WIN_SWING:
        # The swing it made is the evidence of what was at stake. Its predecessor's basis still decides whether it
        # may cut into a favorite's action.
        return 1.0, basis
    return z_pre, basis


def _predecessor_z(predecessor: Leverage | None) -> tuple[float, LeverageBasis]:
    if predecessor is None:
        return 0.0, LeverageBasis.NONE
    if predecessor.status is ModelStatus.VALIDATED:
        # A model's instantaneous z is the swing at stake; a reading that carries only its weight stands in with it.
        return (predecessor.z if predecessor.z is not None else predecessor.g), LeverageBasis.MODEL
    if predecessor.status in _BREAK_ONLY_G:
        # For break-only scoring, a coherent proxy's (or partial model's) weight stands in for z_pre.
        return predecessor.g, _BREAK_ONLY_G[predecessor.status]
    return 0.0, LeverageBasis.UNKNOWN  # without score, time or progress, nothing model-derived counts


def interrupt_value(
    event_class: InterruptClass,
    *,
    predecessor: Leverage | None,
    stakes: Stakes = Stakes(),
    policy: InterruptPolicy = InterruptPolicy(),
) -> InterruptValue:
    """``I`` for an event of ``event_class`` whose released predecessor read ``predecessor``.

    A rare feat uses ``z_pre = 0``. Otherwise a validated model's ``z`` counts and may qualify a cut into action;
    a proxy's or a partial model's ``g`` stands in for break-only scoring; an unknown reading counts as 0. A win
    swing uses ``z_pre = 1`` on its predecessor's basis.
    """
    z_pre, basis = _z_pre(event_class, predecessor)
    quality = stakes.quality if stakes.quality is not None else 0.0  # unknown records count as 0 for an interrupt
    leverage = policy.leverage_floor + policy.leverage_span * z_pre
    stakes_factor = (
        policy.stakes_floor
        + policy.stakes_quality * quality
        + policy.stakes_postseason * stakes.postseason
        + policy.stakes_season * stakes.season
    )
    return InterruptValue(
        event_class=event_class, value=policy.weight(event_class) * leverage * stakes_factor, z_pre=z_pre, basis=basis
    )


# --- MLB -----------------------------------------------------------------------------------------------------------

_HALF_ORDER = {InningPhase.TOP: 0, InningPhase.MIDDLE: 1, InningPhase.BOTTOM: 2, InningPhase.END: 3}


def classify_baseball_final(
    state: BaseballGameState, *, final: bool, policy: InterruptPolicy = InterruptPolicy()
) -> Classification:
    """Whether a completed game is a confirmed perfect game or no-hitter.

    ``final`` says the feed itself called the game final. A feat needs that, reported hit totals, and nine
    complete innings pitched by the side throwing it. StatsAPI leaves a game the home side won without batting in
    the ninth reading as the top of the ninth; with the home side ahead that top half is over, so the home staff's
    ninth counts. A final in its top with no home lead can't vouch for that inning, so the game classifies lower
    rather than guess. A perfect game needs the reach ledger's confirmed "no batter reached"
    (`BaseballGameState.away_reached_base` and its twin are False only over a complete play log); an unknown
    reach leaves it a no-hitter.
    """
    if not final:
        return Classification(InterruptClass.ROUTINE, ClassBasis.NOT_FINAL)
    if not state.known(BaseballField.HITS):
        return Classification(InterruptClass.ROUTINE, ClassBasis.HITS_UNVERIFIED)
    if state.phase is InningPhase.TOP and _home_ahead(state):
        state = replace(state, phase=InningPhase.MIDDLE)  # the home side didn't need its half: the top is over
    progress = pitching_feat_progress(state, min_completed_innings=policy.feat_innings)
    if progress is None:
        return Classification(InterruptClass.ROUTINE, ClassBasis.NO_FEAT)
    if progress.perfect:
        return Classification(InterruptClass.PERFECT_GAME, ClassBasis.PERFECT_GAME)
    reached = state.away_reached_base if progress.side is HomeAway.HOME else state.home_reached_base
    return Classification(
        InterruptClass.NO_HITTER, ClassBasis.REACH_UNVERIFIED if reached is None else ClassBasis.NO_HITTER
    )


def _home_ahead(state: BaseballGameState) -> bool:
    """Whether the home side leads, by known scores."""
    return state.home_score is not None and state.away_score is not None and state.home_score > state.away_score


def _baseball_states_known(*states: BaseballGameState) -> bool:
    fields = (BaseballField.SCORE, BaseballField.INNING, BaseballField.HALF)
    return all(state.known(field) for state in states for field in fields)


def _coherent_baseball_play(
    play: BaseballPlayPayload, before: BaseballGameState, after: BaseballGameState, *, ends_game: bool
) -> bool:
    """Whether the play sits between the states on either side of it: in order, with the score only growing,
    and, for a play that ends the game, the successor showing exactly the score it left."""
    if play.away_score is None or play.home_score is None or not _baseball_states_known(before, after):
        return False
    order = [(s.inning, _HALF_ORDER[s.phase]) for s in (before, after)]
    when = (play.inning, _HALF_ORDER[play.phase])
    if not order[0] <= when <= order[1]:
        return False
    scores = [(before.away_score, before.home_score), (play.away_score, play.home_score)]
    scores.append((after.away_score, after.home_score))
    grew = all(earlier <= later for a, b in zip(scores, scores[1:]) for earlier, later in zip(a, b))
    added = sum(scores[1]) - sum(scores[0])
    return grew and added > 0 and (not ends_game or scores[2] == scores[1])


def classify_baseball_play(
    event: BaseballGameEvent,
    *,
    before: BaseballGameState | None,
    after: BaseballGameState | None,
    policy: InterruptPolicy = InterruptPolicy(),
) -> Classification:
    """A play's class, from its scoring impact and the released states just before and after it.

    - A walk-off (the home side taking the lead in the 9th or later) is a decisive late winner.
    - A tying or go-ahead play in the 9th or later, or a home run that ties, takes the lead or leaves a
      one-run game, is another major event.
    - Everything else is routine, and so is any play whose surrounding states are missing or don't line up.

    The scoring impact counts runs batted in, so a run without an RBI (a walk-off on an error) reads as no score
    and stays routine. A result-changing reversal isn't classified: MLB's typed events carry a challenge, but not
    its verdict.
    """
    play = event.payload
    impact = play.scoring_impact
    if impact is None or not impact.scored:
        return Classification(InterruptClass.ROUTINE, ClassBasis.NOT_SCORING)
    if before is None or after is None or not _coherent_baseball_play(play, before, after, ends_game=impact.walk_off):
        return Classification(InterruptClass.ROUTINE, ClassBasis.INCOHERENT)
    if impact.walk_off:
        return Classification(InterruptClass.DECISIVE_LATE_WINNER, ClassBasis.WALK_OFF)
    lead_change = impact.tying or impact.go_ahead
    if lead_change and play.inning >= policy.mlb_late_inning:
        return Classification(InterruptClass.MAJOR, ClassBasis.LATE_TIE_OR_LEAD)
    assert play.away_score is not None and play.home_score is not None  # a coherent play has its score
    close = abs(play.away_score - play.home_score) <= policy.mlb_close_margin
    if event.event_type is BaseballGameEventType.HOME_RUN and (lead_change or close):
        return Classification(InterruptClass.MAJOR, ClassBasis.CLOSE_HOME_RUN)
    return Classification(InterruptClass.ROUTINE, ClassBasis.NOT_CLOSE)


# --- NFL -----------------------------------------------------------------------------------------------------------

# What a win swing can be put down to: the game's plays (a play, a score the feed reported without one, a try, a
# ruling), never a stoppage, a timeout or an injury. A period's end is no strip entry at all.
SWING_KINDS = frozenset(
    {FootballEventKind.PLAY, FootballEventKind.SCORE, FootballEventKind.CONVERSION, FootballEventKind.REVIEW}
)


def win_swing(before: WinProbability | None, after: WinProbability | None) -> float | None:
    """How many points the away side's win probability moved from ``before`` to ``after``, either way; None without
    both readings. It's read to a millionth of a point, so a price's float noise can't move it across a threshold."""
    if before is None or after is None:
        return None
    return round(abs(after.away - before.away), 6)


def _leader(state: FootballGameState) -> HomeAway | None:
    if state.home_score == state.away_score:
        return None
    return HomeAway.HOME if state.home_score > state.away_score else HomeAway.AWAY


def _coherent_football(event: FootballEvent, before: FootballGameState, after: FootballGameState) -> bool:
    """Whether the states on either side report a score and period and run forward, with no side's score going
    down unless the event is a ruling (a reversal can take points off)."""
    needed = (FootballField.SCORE, FootballField.PERIOD)
    if not all(state.known(field) for state in (before, after) for field in needed):
        return False
    if after.period < before.period:
        return False
    clocks_known = before.known(FootballField.CLOCK) and after.known(FootballField.CLOCK)
    if after.period == before.period and clocks_known and after.clock_seconds > before.clock_seconds:
        return False
    if event.kind is FootballEventKind.REVIEW:
        return True
    return after.away_score >= before.away_score and after.home_score >= before.home_score


def classify_football_event(
    event: FootballEvent,
    *,
    before: FootballGameState | None,
    after: FootballGameState | None,
    swing: float | None = None,
    policy: InterruptPolicy = InterruptPolicy(),
) -> Classification:
    """A strip event's class, from the released states just before and after it, and the market's ``swing`` across
    it (`win_swing`; None when it wasn't read).

    - Late (the fourth quarter at 5:00 or under, or overtime), a major event that puts a side ahead and ends the
      game is a decisive late winner; so is a ruling there that changes who leads.
    - Otherwise a play (`SWING_KINDS`, major or not: a long completion can swing a game) that moved the win
      probability by `InterruptPolicy.win_swing_points` or more is a win swing, whatever the period or the clock.
    - In the fourth quarter or overtime of a one-score game, any other major event (a score, a turnover, a
      ruling) is another major event.
    - Everything else is routine, and so is any event whose surrounding states are missing or don't line up.

    A late go-ahead score that doesn't end the game is only a major event (or a win swing): whether it decided the
    game isn't known until the final, long after the alert's lifetime.
    """
    swung = event.kind in SWING_KINDS and swing is not None and swing >= policy.win_swing_points
    if not event.major and not swung:
        return Classification(InterruptClass.ROUTINE, ClassBasis.NOT_MAJOR)
    if before is None or after is None or not _coherent_football(event, before, after):
        return Classification(InterruptClass.ROUTINE, ClassBasis.INCOHERENT)
    regulation = policy.nfl_regulation_periods
    late_clock = before.known(FootballField.CLOCK) and before.clock_seconds <= policy.nfl_late_clock.total_seconds()
    late = before.period > regulation or (before.period == regulation and late_clock)
    took_lead = _leader(after) is not None and _leader(after) is not _leader(before)
    if event.major and late and took_lead:
        ended = after.known(FootballField.INTERVAL) and after.interval is FootballInterval.FINAL
        if ended:
            return Classification(InterruptClass.DECISIVE_LATE_WINNER, ClassBasis.GAME_ENDING_LEAD_CHANGE)
        if event.kind is FootballEventKind.REVIEW:
            return Classification(InterruptClass.DECISIVE_LATE_WINNER, ClassBasis.REVERSAL)
    if swung:
        return Classification(InterruptClass.WIN_SWING, ClassBasis.WIN_SWING)
    one_score = abs(before.home_score - before.away_score) <= policy.nfl_one_score
    if before.period >= regulation and one_score:
        return Classification(InterruptClass.MAJOR, ClassBasis.LATE_ONE_SCORE)
    return Classification(InterruptClass.ROUTINE, ClassBasis.NOT_CLOSE)


# --- The bars ------------------------------------------------------------------------------------------------------


class FavoriteActivity(StrEnumMixin, str, Enum):
    """What one healthy live favorite is doing at a decision."""

    IN_ACTION = "in_action"  # playing, or back to weighted eligibility after a lease ran out without resumption
    MICRO_BREAK = "micro_break"  # a short in-game stoppage with a valid lease: a mound visit, a timeout, a review
    ORDINARY_BREAK = "ordinary_break"  # an inning or quarter break, halftime, a TV timeout, a weather stoppage


class BarContext(StrEnumMixin, str, Enum):
    """Which bar an interrupt must clear, from what the live favorites are doing."""

    ACTION = "action"  # any favorite in action: another's timeout doesn't lower the bar over its live drive
    MICRO_BREAK = "micro_break"  # none in action, one in a valid micro-break, the rest in breaks
    OPEN = "open"  # all in ordinary breaks, or no live favorite


def bar_context(activities: Iterable[FavoriteActivity]) -> BarContext:
    """The context the healthy live favorites set; with none live, the board is open."""
    seen = set(activities)
    if FavoriteActivity.IN_ACTION in seen:
        return BarContext.ACTION
    if FavoriteActivity.MICRO_BREAK in seen:
        return BarContext.MICRO_BREAK
    return BarContext.OPEN


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptBars:
    """The value ``I`` an interrupt must reach in each context."""

    action: float = 1.10
    micro_break: float = 0.85
    open: float = 0.35

    def __post_init__(self) -> None:
        if not 0.0 <= self.open <= self.micro_break <= self.action:
            raise ValueError("the bars rise from open, to a micro-break, to action")

    def for_context(self, context: BarContext) -> float:
        return {BarContext.ACTION: self.action, BarContext.MICRO_BREAK: self.micro_break, BarContext.OPEN: self.open}[
            context
        ]

    def qualifies_action(self, value: InterruptValue) -> bool:
        """Whether an event may cut into a favorite's action: its leverage evidence allows it, and it clears the
        action bar."""
        return value.action_evidence and value.value >= self.action
