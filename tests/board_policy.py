"""A synthetic time loop for the board under `omni-board-policy/2`: cards staged and hand-built `PolicyView`s
published at set times, the board cued at each of its deadlines, and every cue committed at once, as a display
that drew it would."""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from omni.cards.attention import AttentionMode, AttentionPolicy
from omni.cards.base import (
    CardId,
    CardKind,
    CardPriority,
    DedupeKey,
    DisplayTiming,
    EndBasis,
    GameEnd,
    LayoutSupport,
    ScoreboardCard,
)
from omni.cards.baseball import BigPlayCardPayload, StatusCardPayload
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEventType
from omni.events.break_detection import ActionEvidence
from omni.policy.interrupts import InterruptClass, InterruptValue, LeverageBasis
from omni.policy.leverage import Leverage, ModelStatus
from omni.policy.promotion import PromotionCandidate, PromotionKey, PromotionKind
from omni.policy.settings import PolicySettings
from omni.policy.view import GameFacts, PolicyView
from omni.preferences.favorites import Favorites
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.scheduler import InterleavedCardQueue, SelectionReason
from omni.tower.board import BoardCue, BoardFrame, BoardHealth, BoardSession, SourceHealth
from omni.tower.decisions import BaseTurnNote, CardNote, PolicyDecision, PromotionNote
from omni.tower.director import DirectorPolicy
from omni.tower.lane import ChangeKind, LaneChange

T0 = datetime(2026, 9, 20, 23, 0, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
PROFILES = (PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64)
REGISTRY = MlbTeamRegistry.from_color_file()
# Every MLB club: ATL first, then PHI, so the first game of `games` is the Braves'.
TEAM_IDS = (144, 143, *range(108, 122), *range(133, 143), 145, 146, 147, 158)
ATL_ONLY = Favorites({League.MLB: ["ATL"]})
STATE = BaseballGameState(
    away_score=1,
    home_score=2,
    inning=5,
    phase=InningPhase.TOP,
    count=BaseballCount(balls=0, strikes=0, outs=0),
    bases=BaseballBaseState(),
    away_hits=4,
    home_hits=5,
)
_FACTORY = CardFactory()
_BURST = AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(12))


def at(seconds: float) -> ClockReading:
    """``seconds`` after T0, read both ways."""
    return ClockReading(T0 + timedelta(seconds=seconds), MonotonicTime(float(seconds)))


def game(raw: str, away: int, home: int, *, status: GameStatus = GameStatus.LIVE, start: float = -3600) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=status,
        scheduled_start=T0 + timedelta(seconds=start),
        away=REGISTRY.resolve(away),
        home=REGISTRY.resolve(home),
    )


def games(count: int) -> list[TeamGame]:
    """``count`` games of distinct teams: the first ATL's, against PHI."""
    return [game(f"g{index:02}", TEAM_IDS[2 * index], TEAM_IDS[2 * index + 1]) for index in range(count)]


def live(contest: TeamGame, *, available: float = 0) -> ScoreboardCard[Any]:
    return _FACTORY.live_baseball(contest, STATE, now=T0 + timedelta(seconds=available))


def final(
    contest: TeamGame, *, ended: float, available: float = 0, after: tuple[DedupeKey, ...] = ()
) -> ScoreboardCard[Any]:
    return _FACTORY.final(
        contest,
        STATE,
        now=T0 + timedelta(seconds=available),
        ended=GameEnd(T0 + timedelta(seconds=ended), EndBasis.FIRST_SEEN),
        after=after,
    )


def pregame(contest: TeamGame, *, available: float = 0) -> ScoreboardCard[Any]:
    return _FACTORY.pregame(contest, now=T0 + timedelta(seconds=available))


def status(contest: TeamGame, *, available: float = 0) -> ScoreboardCard[Any]:
    return _FACTORY.status(contest, status=GameStatus.DELAYED, now=T0 + timedelta(seconds=available))


def big_play(contest: TeamGame, play: str, *, available: float = 0, window: float = 120) -> ScoreboardCard[Any]:
    key = f"{contest.id}:bigplay:{play}"
    return ScoreboardCard(
        id=CardId(key),
        kind=CardKind.BIG_PLAY,
        contest=contest,
        timing=DisplayTiming(
            available_at=T0 + timedelta(seconds=available),
            min_display=DurationSeconds(6),
            max_display=DurationSeconds(15),
            expires_at=T0 + timedelta(seconds=available + window),
        ),
        priority=CardPriority(band=DisplayPriority.ALERT, score=1.0),
        layout_support=LayoutSupport(profiles=frozenset(PROFILES)),
        dedupe_key=DedupeKey(key),
        payload=BigPlayCardPayload(
            event_type=BaseballGameEventType.HOME_RUN, description="Homer", away_score=1, home_score=3
        ),
        attention=_BURST,
    )


def facts(
    contest: TeamGame,
    g: float,
    *,
    fresh: bool = True,
    breaks: tuple[BreakEvidence, ...] = (),
    action: ActionEvidence | None = None,
) -> GameFacts:
    return GameFacts(
        game=contest, leverage=Leverage(g=g, status=ModelStatus.PROXY), fresh=fresh, breaks=breaks, action=action
    )


def evidence(
    contest: TeamGame,
    kind: BreakKind,
    *,
    received: float,
    marker: str = "b1",
    onset: float | None = 0.0,
    stated: float | None = None,
    ended: bool = False,
) -> BreakEvidence:
    """Break evidence received at ``received``; ``onset`` is how long before that the break began (None: unknown)."""
    reading = at(received)
    return BreakEvidence(
        id=BreakId(contest.id, kind, marker),
        received=reading,
        confidence=BreakConfidence.EXPLICIT,
        onset=reading.utc - timedelta(seconds=onset) if onset is not None else None,
        stated=timedelta(seconds=stated) if stated is not None else None,
        ended=ended,
    )


def action(contest: TeamGame, marker: str, *, received: float) -> ActionEvidence:
    return ActionEvidence(contest=contest.id, marker=marker, received=at(received))


def major(
    card: ScoreboardCard[Any],
    worth: float,
    *,
    eligible: float = 0,
    model: bool = True,
    play: str | None = None,
    event: InterruptClass = InterruptClass.DECISIVE_LATE_WINNER,
) -> PromotionCandidate[DedupeKey]:
    """A scored offer naming ``card``; ``model`` says its leverage came from a validated model (it may cut into
    action)."""
    value = InterruptValue(
        event_class=event, value=worth, z_pre=1.0, basis=LeverageBasis.MODEL if model else LeverageBasis.PROXY
    )
    return PromotionCandidate(
        key=PromotionKey(card.contest.id, play or card.dedupe_key.raw),
        kind=PromotionKind.MAJOR,
        eligible=MonotonicTime(float(eligible)),
        card=card.dedupe_key,
        value=value,
    )


def reminder(card: ScoreboardCard[Any], *, eligible: float = 0) -> PromotionCandidate[DedupeKey]:
    return PromotionCandidate(
        key=PromotionKey(card.contest.id, card.dedupe_key.raw),
        kind=PromotionKind.REMINDER,
        eligible=MonotonicTime(float(eligible)),
        card=card.dedupe_key,
    )


@dataclass(frozen=True, slots=True)
class Up:
    """A card's first committed frame, and when it came down (None while up), with how long it aired."""

    at: float
    key: str
    reason: SelectionReason
    down: float | None = None


@dataclass
class Run:
    """A board under policy v2 driven through time: events at set instants, a cue at every deadline."""

    profile: PanelProfile = PanelProfile.QUAD_128X64
    settings: PolicySettings = field(default_factory=PolicySettings)
    favorites: Favorites = ATL_ONLY
    rules: DirectorPolicy | None = None
    queue: InterleavedCardQueue = field(default_factory=InterleavedCardQueue)
    board: BoardSession = field(init=False)
    facts: dict[LeagueScopedId, GameFacts] = field(default_factory=dict)
    offers: list[PromotionCandidate[DedupeKey]] = field(default_factory=list)
    health: BoardHealth = field(default_factory=BoardHealth)
    ups: list[Up] = field(default_factory=list)
    changes: list[LaneChange[Any]] = field(default_factory=list)
    notes: list[PolicyDecision] = field(default_factory=list)
    cues: list[tuple[float, BoardCue]] = field(default_factory=list)
    events: dict[float, list[Callable[[], None]]] = field(default_factory=dict)
    fail: Callable[[float, BoardCue], bool] = lambda t, cue: False  # whether the display fails to draw this cue
    now: float = 0.0

    def __post_init__(self) -> None:
        self.board = BoardSession(
            self.queue, self.profile, settings=self.settings, favorites=self.favorites, rules=self.rules
        )

    # --- arranging --------------------------------------------------------------------------------------------

    def stage(self, *cards: ScoreboardCard[Any], when: float | None = None) -> None:
        self.queue.ingest_all(cards, at(self.now if when is None else when))

    def describe(self, *described: GameFacts) -> None:
        for each in described:
            self.facts[each.game.id] = each

    def publish(self) -> None:
        view = PolicyView(games=MappingProxyType(dict(self.facts)), offers=tuple(self.offers))
        self.board.publish(self.health, view)

    def on(self, when: float, event: Callable[[], None]) -> None:
        self.events.setdefault(float(when), []).append(event)

    # --- running ----------------------------------------------------------------------------------------------

    def step(self, seconds: float) -> BoardCue:
        """Cue the board at ``seconds`` and commit what it cued at once (unless the display fails it)."""
        self.now = seconds
        reading = at(seconds)
        cue = self.board.cue(reading)
        self.cues.append((seconds, cue))
        if self.fail(seconds, cue):
            self.board.failed(cue, reading)
        else:
            self.board.committed(cue, reading)
        self._collect(cue)
        return cue

    def run(self, start: float, until: float) -> None:
        """From ``start``, run each event's instant and each of the board's deadlines up to ``until``."""
        t = float(start)
        while t <= until:
            for event in self.events.pop(t, []):
                self.now = t
                event()
            self.step(t)
            deadline = self.board.next_deadline(at(t))
            upcoming = [instant for instant in self.events if instant > t]
            if deadline is not None:
                upcoming.append(deadline.seconds)
            if not upcoming:
                break
            t = min(upcoming)

    def _collect(self, cue: BoardCue) -> None:
        for change in self.board.changes():
            self.changes.append(change)
            payload = change.segment.frames[0]
            if isinstance(payload, BoardFrame):
                continue
            key = payload.dedupe_key.raw
            if change.kind is ChangeKind.UP:
                assert cue.reason is not None
                self.ups.append(Up(change.at.monotonic.seconds, key, cue.reason))
            elif change.kind is ChangeKind.DOWN:
                index = max(i for i, up in enumerate(self.ups) if up.key == key and up.down is None)
                self.ups[index] = dataclasses.replace(self.ups[index], down=change.at.monotonic.seconds)
        self.notes.extend(self.board.decisions())

    # --- reading ----------------------------------------------------------------------------------------------

    def shown(self, key: str) -> list[tuple[float, float | None]]:
        return [(up.at, up.down) for up in self.ups if up.key == key]

    def keys(self, since: float = float("-inf"), before: float = float("inf")) -> list[str]:
        return [up.key for up in self.ups if since <= up.at < before]

    def base_turns(self, before: float = float("inf")) -> Counter[str]:
        """Base turns granted per game, by its raw id, from the board's decisions."""
        return Counter(
            note.contest.raw
            for note in self.notes
            if isinstance(note, BaseTurnNote) and note.at.monotonic.seconds < before
        )

    def airtime(self, until: float) -> dict[str, float]:
        """Confirmed seconds each card was up, by key, to ``until``."""
        total: dict[str, float] = {}
        for up in self.ups:
            end = up.down if up.down is not None else until
            total[up.key] = total.get(up.key, 0.0) + min(end, until) - up.at
        return total

    def of(self, kind: type[Any]) -> list[Any]:
        return [note for note in self.notes if isinstance(note, kind)]

    def promotions(self) -> list[tuple[float, str]]:
        return [(note.at.monotonic.seconds, note.card.raw) for note in self.of(PromotionNote)]

    def cards(self) -> list[tuple[float, str, str]]:
        return [(note.at.monotonic.seconds, note.role.value, note.card.raw) for note in self.of(CardNote)]


def offline() -> BoardHealth:
    return BoardHealth(source=SourceHealth.OFFLINE, reason="no schedule for over 10m: down")


def keys(cards: Iterable[ScoreboardCard[Any]]) -> list[str]:
    return [card.dedupe_key.raw for card in cards]
