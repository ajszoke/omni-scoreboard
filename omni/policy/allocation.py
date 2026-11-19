"""The base allocation: each game's weight, league and game shares, and the favorite floor (`BOARD_POLICY.md`
§3.2-§3.4).

`targets` turns the live pool (`omni.policy.pool`) into the share of base airtime each league and each game
should get. It's pure: the same pool and settings always give the same targets. The airtime ledger
(`omni.policy.ledger`) then hands out turns against them.

A game's weight is ``b = (1 + 1.5*G) * T * F``: its leverage ``G``, its stakes ``T`` and, for a favorite in
action, its rank ``F``. Each league's mass is its games' weights, boosted a little for a short season and
scaled by the user's league weight. A league's share blends its mass with an even split (80/20); a game takes
its league's share in proportion to its weight. When favorites act, their combined share has a floor that
rises with the leverage of the most important one, from 30% in a blowout to 70% close and late.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from omni.core.enum import League, StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.policy.leverage import LeveragePolicy, ModelStatus
from omni.policy.pool import MatchupRecords, PoolEntry, Stakes, TeamRecord

__all__ = [
    "AllocationPolicy",
    "AllocationSettings",
    "FavoriteFloor",
    "GameTarget",
    "GameWeight",
    "LeagueTarget",
    "SetAside",
    "SetAsideReason",
    "Targets",
    "interrupt_quality",
    "rotation_quality",
    "stakes_factor",
    "targets",
    "weigh",
]

_LEAGUE_ORDER = {league: index for index, league in enumerate(League)}


@dataclass(frozen=True, slots=True, kw_only=True)
class AllocationPolicy:
    """Every constant of the base allocation, with `BOARD_POLICY.md` §3's values. A change bumps the policy's
    version; none is a user setting."""

    leverage: LeveragePolicy = LeveragePolicy()  # §3.1
    # §3.2: F = 1 + rank_boost/rank for a favorite in action.
    rank_boost: float = 3.0
    # A record's shrunk win fraction is (W + 0.5*T + prior_wins) / (games + prior_games).
    prior_wins: float = 4.0
    prior_games: float = 8.0
    # Q = clamp((mean - quality_low) / quality_span, 0, 1); unknown records give their own Q.
    quality_low: float = 0.25
    quality_span: float = 0.50
    unknown_quality_rotation: float = 0.5
    unknown_quality_interrupt: float = 0.0
    # T = 1 + quality_weight*(Q - quality_pivot) + stage_weight*J + decider_weight*C_season + national_weight*N.
    quality_weight: float = 0.20
    quality_pivot: float = 0.5
    stage_weight: float = 0.15
    postseason_j: float = 1.0
    cup_knockout_j: float = 0.5
    decider_weight: float = 0.15
    national_weight: float = 0.03
    # b = (1 + leverage_gain*G) * T * F.
    leverage_gain: float = 1.50
    # §3.3: s_l = (reference_season / N_l) ** season_exponent; q_l = blend*mass share + (1 - blend)/L.
    season_games: tuple[tuple[League, int], ...] = ((League.MLB, 162), (League.NFL, 17), (League.NBA, 82))
    reference_season: int = 162
    season_exponent: float = 0.25
    blend: float = 0.80
    league_weight_low: float = 0.5
    league_weight_high: float = 2.0
    # §3.4: floor_F = floor_base + floor_slope * max(G of the acting favorites).
    floor_base: float = 0.30
    floor_slope: float = 0.40
    # §3.5: debts clamped to +-debt_limit; overdue at max(rescue_misses, L, ceil(rescue_turns/Q_l)) misses for
    # a league, max(n_l, ceil(rescue_turns/p_g)) for a game; debts compared to microseconds.
    debt_limit: timedelta = timedelta(seconds=24)
    rescue_misses: int = 6
    rescue_turns: float = 2.0
    debt_digits: int = 6

    def __post_init__(self) -> None:
        if not 0.0 <= self.blend <= 1.0 or not 0.0 <= self.floor_base <= self.floor_base + self.floor_slope < 1.0:
            raise ValueError("the league blend and the favorite floor are shares within 0..1, the floor below 1")
        if not 0.0 < self.league_weight_low <= 1.0 <= self.league_weight_high:
            raise ValueError("league weights must allow the default weight of 1")
        if any(games < 1 for _, games in self.season_games) or self.reference_season < 1:
            raise ValueError("a season has at least one game")
        if self.debt_limit <= timedelta(0) or self.rescue_turns <= 0:
            raise ValueError("the debt limit and the rescue's turns must be positive")

    def season_boost(self, league: League) -> float:
        """A short season's small boost, ``(162/N_l) ** 0.25``; `ValueError` for a league without a length."""
        for each, games in self.season_games:
            if each is league:
                return float((self.reference_season / games) ** self.season_exponent)
        raise ValueError(f"the allocation has no season length for {league.value.upper()}")

    def check(self, settings: AllocationSettings) -> None:
        """Raise `ValueError` if ``settings`` are outside what this policy allows."""
        for league, weight in settings.league_weights:
            if not self.league_weight_low <= weight <= self.league_weight_high:
                raise ValueError(
                    f"a league weight is {self.league_weight_low}..{self.league_weight_high}, "
                    f"not {weight} for {league.value.upper()}"
                )


@dataclass(frozen=True, slots=True, init=False)
class AllocationSettings:
    """The user's settings the allocation reads: a weight per league (1 unless set) and the turn length."""

    league_weights: tuple[tuple[League, float], ...]
    slot: timedelta

    def __init__(
        self, league_weights: Mapping[League, float] | None = None, *, slot: timedelta = timedelta(seconds=12)
    ) -> None:
        if slot <= timedelta(0):
            raise ValueError("a turn must be positive")
        chosen = league_weights or {}
        ordered = tuple((league, float(chosen[league])) for league in League if league in chosen)
        object.__setattr__(self, "league_weights", ordered)
        object.__setattr__(self, "slot", slot)

    def weight(self, league: League) -> float:
        return next((weight for each, weight in self.league_weights if each is league), 1.0)


_POLICY = AllocationPolicy()
_SETTINGS = AllocationSettings()


def _shrunk(record: TeamRecord, policy: AllocationPolicy) -> float:
    return (record.wins + 0.5 * record.ties + policy.prior_wins) / (record.games + policy.prior_games)


def _quality(records: MatchupRecords | None, unknown: float, policy: AllocationPolicy) -> float:
    if records is None:
        return unknown
    mean = (_shrunk(records.away, policy) + _shrunk(records.home, policy)) / 2.0
    return min(max((mean - policy.quality_low) / policy.quality_span, 0.0), 1.0)


def rotation_quality(records: MatchupRecords | None, *, policy: AllocationPolicy = _POLICY) -> float:
    """The matchup's quality ``Q`` for rotation: 0.5 when the records are unknown."""
    return _quality(records, policy.unknown_quality_rotation, policy)


def interrupt_quality(records: MatchupRecords | None, *, policy: AllocationPolicy = _POLICY) -> float:
    """The matchup's quality ``Q`` for an interrupt's value: 0 when the records are unknown."""
    return _quality(records, policy.unknown_quality_interrupt, policy)


def stakes_factor(stakes: Stakes, quality: float, *, policy: AllocationPolicy = _POLICY) -> float:
    """``T``, 0.90..1.43: the matchup's quality, a postseason or Cup knockout, a verified decider, national TV."""
    stage = policy.postseason_j if stakes.postseason else policy.cup_knockout_j if stakes.cup_knockout else 0.0
    return (
        1.0
        + policy.quality_weight * (quality - policy.quality_pivot)
        + policy.stage_weight * stage
        + policy.decider_weight * float(stakes.season_decider)
        + policy.national_weight * float(stakes.national_tv)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class GameWeight:
    """A game's weight and the factors behind it, for the trace: ``b = (1 + 1.5*g) * t * f``."""

    g: float
    status: ModelStatus  # what ``g`` rests on
    f: float
    q: float
    t: float
    b: float
    favorite_rank: int | None
    acting_favorite: bool


def weigh(entry: PoolEntry, *, policy: AllocationPolicy = _POLICY) -> GameWeight:
    """A pool entry's base weight."""
    acting = entry.acting_favorite
    f = 1.0 + policy.rank_boost / entry.favorite_rank if acting and entry.favorite_rank is not None else 1.0
    q = rotation_quality(entry.records, policy=policy)
    t = stakes_factor(entry.stakes, q, policy=policy)
    g = entry.leverage.g
    return GameWeight(
        g=g,
        status=entry.leverage.status,
        f=f,
        q=q,
        t=t,
        b=(1.0 + policy.leverage_gain * g) * t * f,
        favorite_rank=entry.favorite_rank,
        acting_favorite=acting,
    )


class SetAsideReason(StrEnumMixin, str, Enum):
    """Why a pool entry gets no share."""

    MUTED = "muted"
    NOT_DRAWABLE = "not drawable"
    FAVORITE_IN_BREAK = "favorite in break"  # while anything else can be shown


@dataclass(frozen=True, slots=True)
class SetAside:
    """A pool entry the allocation left out, and why: the "why this card" line can say so."""

    contest: LeagueScopedId
    reason: SetAsideReason


@dataclass(frozen=True, slots=True, kw_only=True)
class GameTarget:
    """One game's targets: its share of all base airtime before and after the floor, and within its league."""

    contest: LeagueScopedId
    weight: GameWeight
    blended: float  # q_g: its league's share in proportion to its weight
    share: float  # q'_g: after the favorite floor
    within_league: float  # p_g = q'_g / Q_l

    @property
    def league(self) -> League:
        return self.contest.league


@dataclass(frozen=True, slots=True, kw_only=True)
class LeagueTarget:
    """One league's targets, and the mass behind them."""

    league: League
    games: int  # n_l
    season_boost: float  # s_l
    user_weight: float  # u_l
    mass: float
    blended: float  # q_l: the mass share blended with an even split
    share: float  # Q_l: after the favorite floor


@dataclass(frozen=True, slots=True, kw_only=True)
class FavoriteFloor:
    """The acting favorites' floor, their combined share before it, and whether it bound."""

    level: float  # floor_F
    before: float  # Q_F
    projected: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Targets:
    """The shares each league and game should get, from one pool and one set of settings.

    Leagues are in their declared order, games by league and then id; ``floor`` is None when no favorite
    acts.
    """

    settings: AllocationSettings
    leagues: tuple[LeagueTarget, ...]
    games: tuple[GameTarget, ...]
    floor: FavoriteFloor | None
    set_aside: tuple[SetAside, ...]

    def league(self, league: League) -> LeagueTarget | None:
        return next((each for each in self.leagues if each.league is league), None)

    def game(self, contest: LeagueScopedId) -> GameTarget | None:
        return next((each for each in self.games if each.contest == contest), None)

    def in_league(self, league: League) -> tuple[GameTarget, ...]:
        return tuple(each for each in self.games if each.league is league)


def _sorted(entries: Iterable[PoolEntry]) -> list[PoolEntry]:
    return sorted(entries, key=lambda entry: (_LEAGUE_ORDER[entry.league], str(entry.contest)))


def targets(
    pool: Iterable[PoolEntry], settings: AllocationSettings = _SETTINGS, *, policy: AllocationPolicy = _POLICY
) -> Targets:
    """The base allocation over ``pool``, each contest once (`ValueError` otherwise)."""
    policy.check(settings)
    entries = _sorted(pool)
    if len({entry.contest for entry in entries}) != len(entries):
        raise ValueError("the pool holds each contest once")
    set_aside: list[SetAside] = []
    candidates: list[PoolEntry] = []
    for entry in entries:
        if entry.muted:
            set_aside.append(SetAside(entry.contest, SetAsideReason.MUTED))
        elif not entry.drawable:
            set_aside.append(SetAside(entry.contest, SetAsideReason.NOT_DRAWABLE))
        else:
            candidates.append(entry)
    resting = [entry for entry in candidates if entry.favorite_rank is not None and entry.in_break]
    if len(resting) < len(candidates):  # something else can be shown: a favorite's break is its guests' time
        set_aside.extend(SetAside(entry.contest, SetAsideReason.FAVORITE_IN_BREAK) for entry in resting)
        candidates = [entry for entry in candidates if entry not in resting]
    weights = [(entry, weigh(entry, policy=policy)) for entry in candidates]
    leagues = sorted({entry.league for entry in candidates}, key=_LEAGUE_ORDER.__getitem__)
    league_b = {league: sum(weight.b for entry, weight in weights if entry.league is league) for league in leagues}
    masses = {league: settings.weight(league) * policy.season_boost(league) * league_b[league] for league in leagues}
    total = sum(masses.values())
    blended = {
        league: policy.blend * masses[league] / total + (1.0 - policy.blend) / len(leagues) for league in leagues
    }
    raw = [blended[entry.league] * weight.b / league_b[entry.league] for entry, weight in weights]
    shares, floor = _floor(weights, raw, policy)
    league_shares = {
        league: sum(share for (entry, _), share in zip(weights, shares) if entry.league is league) for league in leagues
    }
    return Targets(
        settings=settings,
        leagues=tuple(
            LeagueTarget(
                league=league,
                games=sum(1 for entry in candidates if entry.league is league),
                season_boost=policy.season_boost(league),
                user_weight=settings.weight(league),
                mass=masses[league],
                blended=blended[league],
                share=league_shares[league],
            )
            for league in leagues
        ),
        games=tuple(
            GameTarget(
                contest=entry.contest,
                weight=weight,
                blended=before,
                share=after,
                within_league=after / league_shares[entry.league],
            )
            for (entry, weight), before, after in zip(weights, raw, shares)
        ),
        floor=floor,
        set_aside=tuple(set_aside),
    )


def _floor(
    weights: list[tuple[PoolEntry, GameWeight]], raw: list[float], policy: AllocationPolicy
) -> tuple[list[float], FavoriteFloor | None]:
    """The shares after the acting favorites' floor, and the floor itself (None when no favorite acts).

    When every candidate is an acting favorite there's nothing to project from, so nothing moves.
    """
    acting = [weight.acting_favorite for _, weight in weights]
    if not any(acting):
        return raw, None
    level = policy.floor_base + policy.floor_slope * max(weight.g for _, weight in weights if weight.acting_favorite)
    before = sum(share for share, favorite in zip(raw, acting) if favorite)
    if all(acting) or before >= level:
        return raw, FavoriteFloor(level=level, before=before, projected=False)
    shares = [
        level * share / before if favorite else (1.0 - level) * share / (1.0 - before)
        for share, favorite in zip(raw, acting)
    ]
    return shares, FavoriteFloor(level=level, before=before, projected=True)
