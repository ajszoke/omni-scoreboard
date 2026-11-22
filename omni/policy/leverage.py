"""A game's leverage, as the policy reads it: a weight between 0 and 1, and how far it can be trusted.

Leverage is the chance for the next outcome to change who wins. Until a league's model passes validation,
the policy reads a labeled proxy from the score, the time left and the progress, which is never called a
leverage index or a win probability. Each reading says which it is, so a decision can refuse what a proxy
mustn't grant: a proxy never qualifies an interrupt during a favorite's action.

Two ways to a weight ``G`` (`BOARD_POLICY.md` §3.1):
- **the proxy** (`baseball_proxy`, `football_proxy`, `basketball_proxy`), from a game's state. A score,
  period, clock or outs count the feed didn't give is unknown, never 0; without one the weight is a flat
  0.2, labeled UNKNOWN;
- **a model's reading**, through a per-contest `LeverageTracker`: the instantaneous ``z`` from its leverage
  index, blended with ``R``, a moving average of ``z`` over the game's recent action.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from omni.core.clock import MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.domain.baseball import BaseballField, BaseballGameState, InningPhase
from omni.domain.football import FootballField, FootballGameState, FootballInterval

__all__ = [
    "Leverage",
    "LeveragePolicy",
    "LeverageTracker",
    "MarginScale",
    "ModelReading",
    "ModelStatus",
    "ReadingKind",
    "baseball_proxy",
    "basketball_proxy",
    "football_proxy",
    "model_z",
    "proxy_leverage",
]


class ModelStatus(StrEnumMixin, str, Enum):
    """How far a leverage reading can be trusted."""

    VALIDATED = "validated"  # a registered model that passed its gates, on complete inputs
    REDUCED = "reduced"  # that model on partial inputs
    PROXY = "proxy"  # the labeled proxy: score, time and progress only
    UNKNOWN = "unknown"  # not enough to say: no score, time or progress


@dataclass(frozen=True, slots=True, kw_only=True)
class Leverage:
    """A game's leverage weight ``g`` (0..1), the instantaneous ``z`` behind it (0..1) when a model gives
    one, its status, and the model it came from (None for the proxy and unknown)."""

    g: float
    status: ModelStatus
    z: float | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("g", self.g), ("z", self.z)):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"leverage {name} must be within 0..1, not {value}")
        if (self.model_id is None) != (self.status in (ModelStatus.PROXY, ModelStatus.UNKNOWN)):
            raise ValueError("a validated or reduced reading names its model; a proxy or unknown one doesn't")

    @property
    def qualifies_action(self) -> bool:
        """Whether it may qualify an interrupt during a favorite's action: only a validated model's."""
        return self.status is ModelStatus.VALIDATED


@dataclass(frozen=True, slots=True, kw_only=True)
class MarginScale:
    """The margin that still counts as close, by the share of the game left: ``base + slope * r**exponent``.

    A margin ``d`` scores ``1 / (1 + (d/M)**2)``, so a deficit of one scale ``M`` halves a game's weight.
    """

    base: float
    slope: float
    exponent: float = 1.0

    def __post_init__(self) -> None:
        if self.base <= 0 or self.slope < 0 or self.exponent <= 0:
            raise ValueError("a margin scale needs a positive base, and a slope and exponent that don't shrink it")

    def at(self, remaining: float) -> float:
        """The scale ``M`` with ``remaining`` (0..1) of regulation left."""
        return self.base + self.slope * float(remaining**self.exponent)


@dataclass(frozen=True, slots=True, kw_only=True)
class LeveragePolicy:
    """Every constant behind a game's leverage weight, with `BOARD_POLICY.md` §3.1's values."""

    # The proxy: G = clamp(C * (proxy_floor + proxy_progress * P**2), 0, 1), with C from the margin scale.
    proxy_floor: float = 0.30
    proxy_progress: float = 0.70
    unknown_g: float = 0.2  # without score, time or progress
    mlb_scheduled_innings: int = 9  # 54 scheduled outs
    nfl_regulation: timedelta = timedelta(seconds=3600)
    nba_regulation: timedelta = timedelta(seconds=2880)
    # The first half's last minutes get the game's own last minutes' boost: a drive or a possession against the half's
    # clock is worth watching the way one against the game's is. The NFL's two-minute warning; the NBA's last two.
    half_end: timedelta = timedelta(seconds=120)
    mlb_margin: MarginScale = MarginScale(base=1.0, slope=2.0)  # 1 + 2r
    nfl_margin: MarginScale = MarginScale(base=3.0, slope=11.0)  # 3 + 11r
    nba_margin: MarginScale = MarginScale(base=3.0, slope=15.0, exponent=0.5)  # 3 + 15*sqrt(r)
    # A model's reading: z = clamp((LI - li_floor) / li_span, 0, 1); G = current_weight*z + memory_weight*R.
    li_floor: float = 0.5
    li_span: float = 2.5
    current_weight: float = 0.75
    memory_weight: float = 0.25
    half_life: timedelta = timedelta(seconds=90)  # R's half-life, in active time
    uncertain_cap: timedelta = timedelta(seconds=30)  # the most one interval between readings counts for
    reduced_cap: float = 0.2  # the most a reduced model's reading can weigh (LI = 1)

    def __post_init__(self) -> None:
        if self.mlb_scheduled_innings < 1:
            raise ValueError("a game is scheduled for at least one inning")
        if self.nfl_regulation <= timedelta(0) or self.nba_regulation <= timedelta(0):
            raise ValueError("regulation must be positive")
        if self.half_end < timedelta(0):
            raise ValueError("the half's last minutes can't be negative")
        if self.li_span <= 0 or self.half_life <= timedelta(0) or self.uncertain_cap < timedelta(0):
            raise ValueError("the leverage index's span and the average's half-life must be positive")


_DEFAULT = LeveragePolicy()
_PERIODS = 4  # regulation quarters, in football and basketball
_OUTS_PER_INNING = 6  # three a half


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def proxy_leverage(
    *, margin: int, progress: float, remaining: float, scale: MarginScale, policy: LeveragePolicy = _DEFAULT
) -> Leverage:
    """The labeled proxy for a game ``progress`` (0..1) through regulation, with ``remaining`` (0..1) of it
    left and a score difference of ``margin``. In overtime the progress is 1 and ``remaining`` is the
    overtime left over regulation."""
    p, r = _clamp(progress), _clamp(remaining)
    closeness = 1.0 / (1.0 + (abs(margin) / scale.at(r)) ** 2)
    return Leverage(
        g=_clamp(closeness * (policy.proxy_floor + policy.proxy_progress * p * p)), status=ModelStatus.PROXY
    )


def _unknown(policy: LeveragePolicy) -> Leverage:
    return Leverage(g=policy.unknown_g, status=ModelStatus.UNKNOWN)


def baseball_proxy(state: BaseballGameState, *, policy: LeveragePolicy = _DEFAULT) -> Leverage:
    """An MLB game's proxy weight, by the scheduled outs it has completed.

    Each half's third out and the break after it are the same out, so it's counted once. Extra innings have
    no regulation left. Without a reported score, inning or half, or outs while a half is under way, it's
    unknown.
    """
    if not all(state.known(each) for each in (BaseballField.SCORE, BaseballField.INNING, BaseballField.HALF)):
        return _unknown(policy)
    if not state.phase.is_break and not state.known(BaseballField.OUTS):
        return _unknown(policy)
    in_inning = {
        InningPhase.TOP: state.count.outs,
        InningPhase.MIDDLE: 3,
        InningPhase.BOTTOM: 3 + state.count.outs,
        InningPhase.END: _OUTS_PER_INNING,
    }[state.phase]
    scheduled = policy.mlb_scheduled_innings * _OUTS_PER_INNING
    if state.inning > policy.mlb_scheduled_innings:  # extra innings: no regulation left
        progress = 1.0
    else:
        progress = ((state.inning - 1) * _OUTS_PER_INNING + in_inning) / scheduled
    return proxy_leverage(
        margin=state.home_score - state.away_score,
        progress=progress,
        remaining=1.0 - progress,
        scale=policy.mlb_margin,
        policy=policy,
    )


def _timed_proxy(
    *,
    period: int,
    clock_seconds: float,
    margin: int,
    regulation: timedelta,
    scale: MarginScale,
    policy: LeveragePolicy,
) -> Leverage:
    total = regulation.total_seconds()
    quarter = total / _PERIODS
    left = _clamp(clock_seconds, 0.0, quarter)
    if period > _PERIODS:  # overtime: regulation is complete, and the scale reads the overtime left
        return proxy_leverage(margin=margin, progress=1.0, remaining=left / total, scale=scale, policy=policy)
    progress = ((period - 1) * quarter + quarter - left) / total
    reading = proxy_leverage(margin=margin, progress=progress, remaining=1.0 - progress, scale=scale, policy=policy)
    if period == _PERIODS // 2 and 0 < left <= policy.half_end.total_seconds():
        # The half's last minutes get the boost the game's own last minutes would at the same score (the same clock
        # read in the last quarter), and never less than the half's own weight: a lead that would all but settle the
        # game that late doesn't settle it at the half. The half's own end (0:00, and the break after it) is as before.
        late = (total - left) / total
        boosted = proxy_leverage(margin=margin, progress=late, remaining=1.0 - late, scale=scale, policy=policy)
        return boosted if boosted.g > reading.g else reading
    return reading


def football_proxy(state: FootballGameState, *, policy: LeveragePolicy = _DEFAULT) -> Leverage:
    """An NFL game's proxy weight, by the regulation seconds played.

    At the end of a quarter or at halftime the period has run out, so an unreported clock reads 0:00 there.
    Elsewhere an unreported score, period or clock makes it unknown. The final has no next outcome, so no
    weight.
    """
    if state.interval is FootballInterval.FINAL:
        return Leverage(g=0.0, status=ModelStatus.PROXY)
    if not (state.known(FootballField.SCORE) and state.known(FootballField.PERIOD)):
        return _unknown(policy)
    between = state.known(FootballField.INTERVAL) and state.interval is not None
    if not state.known(FootballField.CLOCK) and not between:
        return _unknown(policy)
    return _timed_proxy(
        period=state.period,
        clock_seconds=state.clock_seconds if state.known(FootballField.CLOCK) else 0,
        margin=state.home_score - state.away_score,
        regulation=policy.nfl_regulation,
        scale=policy.nfl_margin,
        policy=policy,
    )


def basketball_proxy(
    *,
    period: int | None,
    clock_seconds: float | None,
    away_score: int | None,
    home_score: int | None,
    policy: LeveragePolicy = _DEFAULT,
) -> Leverage:
    """An NBA game's proxy weight, by the regulation seconds played; None for anything the feed didn't give.

    There's no NBA game state on the shared board yet, so this takes the few values it reads directly.
    """
    if period is None or clock_seconds is None or away_score is None or home_score is None or period < 1:
        return _unknown(policy)
    return _timed_proxy(
        period=period,
        clock_seconds=clock_seconds,
        margin=home_score - away_score,
        regulation=policy.nba_regulation,
        scale=policy.nba_margin,
        policy=policy,
    )


def model_z(li: float, *, policy: LeveragePolicy = _DEFAULT) -> float:
    """A leverage index on the policy's 0..1 scale: 0 at or below 0.5, 1 at 3 or more."""
    return _clamp((li - policy.li_floor) / policy.li_span)


class ReadingKind(StrEnumMixin, str, Enum):
    """What a model's released reading says about the game's action since the last one."""

    FRESH = "fresh"  # new action: the moving average takes it in
    DUPLICATE = "duplicate"  # a re-read of the same state: the average holds
    IN_BREAK = "in_break"  # a confirmed break: the average holds, and the break isn't active time
    AFTER_OUTAGE = "after_outage"  # the first reading after an outage: the average restarts from it


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelReading:
    """One released reading from a registered model: its leverage index, at a monotonic time."""

    li: float
    status: ModelStatus
    model_id: str
    at: MonotonicTime
    kind: ReadingKind = ReadingKind.FRESH

    def __post_init__(self) -> None:
        if self.status not in (ModelStatus.VALIDATED, ModelStatus.REDUCED):
            raise ValueError("a model's reading is validated or reduced; the proxy has its own functions")
        if self.li < 0 or math.isnan(self.li):
            raise ValueError("a leverage index can't be negative")


class LeverageTracker:
    """One contest's weight from its model's readings: ``G = 0.75*z + 0.25*R``.

    ``R`` is an average of ``z`` that halves an old reading's pull every 90 s of the game's action. Only
    fresh readings move it, by the active time since the last one, and no single interval counts for more
    than 30 s, since how much of it was action is uncertain. A duplicate or a break holds it; time in a
    break isn't active time. The first reading, and the first after an outage, restart it at their own
    ``z``. A reduced model's reading weighs at most 0.2.
    """

    def __init__(self, *, policy: LeveragePolicy = _DEFAULT) -> None:
        self._policy = policy
        self._average: float | None = None
        self._since = MonotonicTime(0.0)  # where the active interval the average hasn't taken in began

    def read(self, reading: ModelReading) -> Leverage:
        """Take in ``reading`` and return the weight it gives."""
        policy = self._policy
        z = model_z(reading.li, policy=policy)
        if self._average is None or reading.kind is ReadingKind.AFTER_OUTAGE:
            self._average, self._since = z, reading.at
        elif reading.kind is ReadingKind.FRESH:
            active = min(max(reading.at.since(self._since), timedelta(0)), policy.uncertain_cap)
            kept = 2.0 ** -(active / policy.half_life)
            self._average, self._since = kept * self._average + (1.0 - kept) * z, reading.at
        elif reading.kind is ReadingKind.IN_BREAK:
            self._since = reading.at
        g = _clamp(policy.current_weight * z + policy.memory_weight * self._average)
        if reading.status is ModelStatus.REDUCED:
            g = min(g, policy.reduced_cap)
        return Leverage(g=g, z=z, status=reading.status, model_id=reading.model_id)
