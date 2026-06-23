"""Tests for LiveBaseballPipeline: delay-safe live cards into the queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.pipeline import LiveBaseballPipeline, PipelineResult
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.events.base import EventImportance
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload, LiveBaseballFeed
from omni.providers.base import ProviderError
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64


def _id(raw: str) -> LeagueScopedId:
    return LeagueScopedId(League.MLB, SOURCE, raw)


def _game(raw: str, status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=_id(raw),
        league=League.MLB,
        status=status,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def _state(away: int = 0, home: int = 0, inning: int = 3, away_hits: int = 5, home_hits: int = 5) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=inning,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
        away_hits=away_hits,
        home_hits=home_hits,
    )


def _event(
    eid: str,
    *,
    event_type: BaseballGameEventType = BaseballGameEventType.HOME_RUN,
    band: DisplayPriority = DisplayPriority.ALERT,
    source_time: datetime = T,
    away: int = 1,
    home: int = 0,
) -> BaseballGameEvent:
    return BaseballGameEvent(
        id=_id(eid),
        contest=_game("g1"),
        event_type=event_type,
        source=SOURCE,
        source_time=source_time,
        observed_at=source_time,
        importance=EventImportance(
            priority=band, urgency=UpdateUrgency.HIGH, leverage=0.0, rarity=0.5, favorite_relevance=0.0
        ),
        payload=BaseballPlayPayload(
            inning=7, phase=InningPhase.BOTTOM, description="homer", rbi=1, away_score=away, home_score=home
        ),
    )


class _Fetch:
    """A configurable game-feed fetcher that can also fail on command."""

    def __init__(self) -> None:
        self.states: dict[str, BaseballGameState] = {}
        self.events: dict[str, tuple[BaseballGameEvent, ...]] = {}
        self.fail: set[str] = set()

    def set(self, raw: str, state: BaseballGameState) -> None:
        self.states[raw] = state

    def set_events(self, raw: str, events: tuple[BaseballGameEvent, ...]) -> None:
        self.events[raw] = events

    def __call__(self, game: TeamGame, now: datetime) -> LiveBaseballFeed:
        if game.id.raw in self.fail:
            raise ProviderError("game feed down")
        return LiveBaseballFeed(state=self.states[game.id.raw], events=self.events.get(game.id.raw, ()))


def _setup(lag: int = 30) -> tuple[LiveBaseballPipeline, InterleavedCardQueue]:
    queue = InterleavedCardQueue()
    pipe = LiveBaseballPipeline(
        scorer=PriorityScorer(),
        delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(lag)),
        factory=CardFactory(),
        queue=queue,
    )
    return pipe, queue


def test_holds_live_state_within_the_tv_delay() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert res.held == (_id("g1"),)
    assert res.ingested == () and len(queue) == 0


def test_surfaces_a_card_once_the_delay_elapses() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # buffered
    res = pipe.refresh([_game("g1")], now=T + timedelta(seconds=30), fetch_feed=fetch)
    assert len(res.ingested) == 1 and len(queue) == 1


def test_shows_lag_old_state_not_the_current_spoiler() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(home=0))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # observes 0-0
    fetch.set("g1", _state(home=1))  # a run scores...
    pipe.refresh([_game("g1")], now=T + timedelta(seconds=30), fetch_feed=fetch)  # ...but it's fresh
    card = queue.next_card(T + timedelta(seconds=30), QUAD)
    assert card is not None
    assert card.payload.home_score == 0  # the delayed 0-0, never the un-aired 0-1


def test_ignores_non_live_games() -> None:
    pipe, queue = _setup()
    fetch = _Fetch()
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1", GameStatus.SCHEDULED), _game("g2", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    assert res == PipelineResult(ingested=(), big_plays=(), no_hitters=(), held=(), removed=(), skipped=())
    assert len(queue) == 0


def test_isolates_per_game_fetch_failures() -> None:
    pipe, queue = _setup(lag=0)  # eligible immediately
    fetch = _Fetch()
    fetch.set("g2", _state(away=1, home=2))
    fetch.fail.add("g1")
    res = pipe.refresh([_game("g1"), _game("g2")], now=T, fetch_feed=fetch)
    assert any("g1" in warning for warning in res.skipped)
    assert len(res.ingested) == 1 and len(queue) == 1  # g2 still made it through


def test_removes_card_when_a_game_leaves_the_live_set() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert len(queue) == 1
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert res.removed == (_id("g1"),)
    assert len(queue) == 0


def test_processes_multiple_live_games() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    fetch.set("g2", _state(away=1, home=1))
    res = pipe.refresh([_game("g1"), _game("g2")], now=T, fetch_feed=fetch)
    assert len(res.ingested) == 2 and len(queue) == 2


def test_drops_feed_when_a_game_leaves_before_ever_surfacing() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    res1 = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # buffered, no card yet
    assert res1.held == (_id("g1"),) and len(queue) == 0
    res2 = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert res2.removed == ()  # nothing was ever carded, so nothing to "remove"
    assert len(queue) == 0
    # The feed was dropped: going live again starts buffering afresh.
    res3 = pipe.refresh([_game("g1")], now=T + timedelta(seconds=20), fetch_feed=fetch)
    assert res3.held == (_id("g1"),)


# --- big-play event path ----------------------------------------------------------


def test_notable_event_surfaces_a_big_play_after_the_delay() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # first sight: nothing to flash yet
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T + timedelta(seconds=5)),))
    held = pipe.refresh([_game("g1")], now=T + timedelta(seconds=5), fetch_feed=fetch)
    assert held.big_plays == ()  # observed, but still inside the TV delay
    fired = pipe.refresh([_game("g1")], now=T + timedelta(seconds=35), fetch_feed=fetch)  # source_time + lag
    assert len(fired.big_plays) == 1 and "bigplay" in fired.big_plays[0].raw


def test_a_big_play_fires_exactly_once_across_polls() -> None:
    pipe, queue = _setup(lag=0)  # eligible as soon as it is observed
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T),))
    r2 = pipe.refresh([_game("g1")], now=T + timedelta(seconds=10), fetch_feed=fetch)
    r3 = pipe.refresh([_game("g1")], now=T + timedelta(seconds=20), fetch_feed=fetch)  # same event still in feed
    assert len(r2.big_plays) == 1
    assert r3.big_plays == ()  # deduped by lineage — not re-flashed every tick


def test_a_routine_event_does_not_flash() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    single = _event("g1:ab:3", event_type=BaseballGameEventType.SINGLE, band=DisplayPriority.NORMAL, source_time=T)
    fetch.set_events("g1", (single,))
    res = pipe.refresh([_game("g1")], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert res.big_plays == ()  # below the big-play band: informs the live card, never a takeover


def test_backlog_is_suppressed_on_first_sight() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T),))  # already in the feed when we tune in
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # primed -> suppressed
    res = pipe.refresh([_game("g1")], now=T + timedelta(seconds=120), fetch_feed=fetch)  # long past any delay
    assert res.big_plays == ()  # joining mid-game never dumps the earlier plays onto the screen


def test_event_stream_is_dropped_when_a_game_leaves() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T + timedelta(seconds=5)),))
    pipe.refresh([_game("g1")], now=T + timedelta(seconds=5), fetch_feed=fetch)  # an event is held in the stream
    assert _id("g1") in pipe._event_streams and pipe._event_streams[_id("g1")].pending() == 1
    pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert _id("g1") not in pipe._event_streams  # cleaned up with the game — no leak


# --- no-hitter path ---------------------------------------------------------------


def test_no_hitter_card_surfaces_when_a_side_is_hitless() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=7, away_hits=0, home_hits=4))  # away hitless -> home's no-no
    res = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert [c.raw for c in res.no_hitters] == ["g1:nohitter"]
    assert len(queue) == 2  # the live card + the no-hitter card


def test_no_hitter_card_is_removed_when_the_bid_breaks() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=7, away_hits=0))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert len(queue) == 2
    fetch.set("g1", _state(inning=7, away_hits=1))  # a hit — the bid is broken
    res = pipe.refresh([_game("g1")], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert res.no_hitters == () and len(queue) == 1  # just the live card now


def test_no_hitter_card_not_surfaced_before_the_min_inning() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=3, away_hits=0))  # hitless, but only the 3rd — routine
    res = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert res.no_hitters == () and len(queue) == 1


def test_no_hitter_card_dropped_when_its_game_leaves() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=8, away_hits=0))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert _id("g1") in pipe._no_hitter_keys
    pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert _id("g1") not in pipe._no_hitter_keys and len(queue) == 0  # cleaned up with the game
