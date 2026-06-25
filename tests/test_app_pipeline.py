"""Tests for LiveBaseballPipeline: delay-safe live cards into the queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.pipeline import LiveBaseballPipeline, PipelineResult
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    InningPhase,
    PitchingDecisions,
    WinProbability,
)
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


def _state(
    away: int = 0,
    home: int = 0,
    inning: int = 3,
    away_hits: int = 5,
    home_hits: int = 5,
    *,
    phase: InningPhase = InningPhase.TOP,
    away_reached_base: bool | None = None,
    home_reached_base: bool | None = None,
) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=inning,
        phase=phase,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
        away_hits=away_hits,
        home_hits=home_hits,
        away_reached_base=away_reached_base,
        home_reached_base=home_reached_base,
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
        self.decisions: dict[str, PitchingDecisions] = {}
        self.warnings: dict[str, tuple[str, ...]] = {}
        self.fail: set[str] = set()

    def set(self, raw: str, state: BaseballGameState) -> None:
        self.states[raw] = state

    def set_events(self, raw: str, events: tuple[BaseballGameEvent, ...]) -> None:
        self.events[raw] = events

    def __call__(self, game: TeamGame, now: datetime) -> LiveBaseballFeed:
        if game.id.raw in self.fail:
            raise ProviderError("game feed down")
        return LiveBaseballFeed(
            state=self.states[game.id.raw],
            events=self.events.get(game.id.raw, ()),
            decisions=self.decisions.get(game.id.raw),
            warnings=self.warnings.get(game.id.raw, ()),
        )


class _WinProb:
    """A configurable win-probability fetcher; can be set per refresh and made to fail."""

    def __init__(self) -> None:
        self.value: WinProbability | None = None
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def __call__(self, game: TeamGame) -> WinProbability | None:
        self.calls.append(game.id.raw)
        if game.id.raw in self.fail:
            raise ProviderError("win-probability feed down")
        return self.value


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
    assert card.payload.home_line.runs == 0  # the delayed 0-0, never the un-aired 0-1


def test_win_probability_is_delay_safe_never_leads_the_score() -> None:
    # The meter must show the reading from the same lag-old moment as the score, never a fresher one.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    wp = _WinProb()
    early = WinProbability(home=60.0, away=40.0)
    later = WinProbability(home=95.0, away=5.0)

    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch, fetch_win_probability=wp)  # state held; no win-prob yet
    assert wp.calls == []  # nothing fetched while the state is still buffering

    wp.value = early
    at30 = T + timedelta(seconds=30)
    pipe.refresh([_game("g1")], now=at30, fetch_feed=fetch, fetch_win_probability=wp)  # state surfaces; early pushed
    c1 = queue.next_card(at30, QUAD)
    assert c1 is not None and c1.payload.win_probability is None  # warm-up: early has not cleared the lag yet

    wp.value = later  # a fresher reading arrives...
    at60 = T + timedelta(seconds=60)
    pipe.refresh([_game("g1")], now=at60, fetch_feed=fetch, fetch_win_probability=wp)
    c2 = queue.next_card(at60, QUAD)
    assert c2 is not None and c2.payload.win_probability == early  # ...but the meter shows the delayed `early`


def test_win_probability_surfaces_once_clear_of_a_zero_lag() -> None:
    # With no delay the happy path attaches the current reading immediately.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    wp = _WinProb()
    wp.value = WinProbability(home=70.0, away=30.0)
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch, fetch_win_probability=wp)
    card = queue.next_card(T, QUAD)
    assert card is not None and card.payload.win_probability == WinProbability(home=70.0, away=30.0)


def test_no_win_probability_fetcher_means_no_meter() -> None:
    # Omitting the fetcher (the meter disabled) leaves the live card's win_probability None.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    card = queue.next_card(T, QUAD)
    assert card is not None and card.payload.win_probability is None


def test_win_probability_fetch_failure_is_a_nonfatal_warning() -> None:
    # A win-prob fetch failure must not drop the live card — it shows without a meter, plus a warning.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    wp = _WinProb()
    wp.fail.add("g1")
    res = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch, fetch_win_probability=wp)
    assert len(res.ingested) == 1 and res.skipped == ()  # the live card still shows; not a game drop
    assert any("win-probability" in warning for warning in res.warnings)
    card = queue.next_card(T, QUAD)
    assert card is not None and card.payload.win_probability is None  # no meter on a failed fetch


def test_win_probability_buffer_is_released_when_the_game_leaves() -> None:
    # The per-game win-prob buffer rides the state feed's lifecycle — dropped with the game, no leak.
    pipe, _queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    wp = _WinProb()
    wp.value = WinProbability(home=70.0, away=30.0)
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch, fetch_win_probability=wp)
    assert _id("g1") in pipe._win_prob_feeds  # a buffer exists while the game is live
    pipe.refresh([], now=T + timedelta(seconds=1), fetch_feed=fetch, fetch_win_probability=wp)
    assert _id("g1") not in pipe._win_prob_feeds  # gone with the game


def test_a_non_carding_status_surfaces_nothing() -> None:
    # A postponed game (not upcoming, live, paused, nor final) produces no card on any path —
    # exercising the full PipelineResult shape in one assertion.
    pipe, queue = _setup()
    res = pipe.refresh([_game("g2", GameStatus.POSTPONED)], now=T, fetch_feed=_Fetch())
    assert res == PipelineResult(
        pregames=(),
        statuses=(),
        ingested=(),
        big_plays=(),
        no_hitters=(),
        finals=(),
        held=(),
        removed=(),
        skipped=(),
        warnings=(),
    )
    assert len(queue) == 0


def test_isolates_per_game_fetch_failures() -> None:
    pipe, queue = _setup(lag=0)  # eligible immediately
    fetch = _Fetch()
    fetch.set("g2", _state(away=1, home=2))
    fetch.fail.add("g1")
    res = pipe.refresh([_game("g1"), _game("g2")], now=T, fetch_feed=fetch)
    assert any("g1" in warning for warning in res.skipped)
    assert len(res.ingested) == 1 and len(queue) == 1  # g2 still made it through


def test_surfaces_per_play_warnings_without_dropping_the_game() -> None:
    # A malformed play is isolated in the provider; the pipeline surfaces it as a warning, but
    # the live card still ingests — a per-play drop is not a per-game failure.
    pipe, queue = _setup(lag=0)  # eligible immediately
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    fetch.warnings["g1"] = ("play 4: ValueError: bad rbi",)
    res = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert res.warnings == ("g1: play 4: ValueError: bad rbi",)
    assert len(res.ingested) == 1 and len(queue) == 1  # the game still shows
    assert res.skipped == ()  # not a dropped-game failure


def test_surfaces_per_play_warnings_for_a_final_game() -> None:
    # The final path collects feed warnings too (off the same per-game fetch).
    pipe, _queue = _setup(lag=0)  # final reveals as soon as the game is seen final
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=1))
    fetch.warnings["g1"] = ("play 7: AttributeError: null about",)
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    assert res.warnings == ("g1: play 7: AttributeError: null about",)


def test_live_card_is_swapped_for_the_final_when_the_game_ends() -> None:
    pipe, queue = _setup(lag=0)  # no delay, so the final reveals as soon as the game is seen final
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert len(queue) == 1  # the live card
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert res.removed == (_id("g1"),)  # the live card is gone...
    assert [c.raw for c in res.finals] == ["g1:final"]  # ...replaced by the final card
    assert len(queue) == 1


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


# --- pregame path -----------------------------------------------------------------


def test_upcoming_game_surfaces_a_pregame_card() -> None:
    pipe, queue = _setup()
    res = pipe.refresh([_game("g1", GameStatus.SCHEDULED)], now=T, fetch_feed=_Fetch())
    assert [c.raw for c in res.pregames] == ["g1:pregame"]
    assert res.ingested == () and len(queue) == 1


def test_both_scheduled_and_pregame_statuses_card() -> None:
    pipe, queue = _setup()
    res = pipe.refresh([_game("g1", GameStatus.SCHEDULED), _game("g2", GameStatus.PREGAME)], now=T, fetch_feed=_Fetch())
    assert sorted(c.raw for c in res.pregames) == ["g1:pregame", "g2:pregame"]
    assert len(queue) == 2


def test_pregame_needs_no_feed_fetch() -> None:
    # A pregame card is built from the schedule alone, so even a failing feed never blocks it.
    pipe, queue = _setup()
    fetch = _Fetch()
    fetch.fail.add("g1")  # would raise if the pipeline tried to fetch
    res = pipe.refresh([_game("g1", GameStatus.PREGAME)], now=T, fetch_feed=fetch)
    assert [c.raw for c in res.pregames] == ["g1:pregame"]
    assert res.skipped == ()  # never attempted a fetch


def test_pregame_card_yields_when_the_game_goes_live() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    pipe.refresh([_game("g1", GameStatus.PREGAME)], now=T, fetch_feed=fetch)
    assert _id("g1") in pipe._pregame_keys and len(queue) == 1
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1", GameStatus.LIVE)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert _id("g1") not in pipe._pregame_keys  # pregame card dropped...
    assert [c.raw for c in res.ingested] == ["g1:live"]  # ...replaced by the live card
    assert len(queue) == 1  # one card for the game, not two


def test_pregame_card_removed_when_the_game_leaves_the_slate() -> None:
    pipe, queue = _setup()
    pipe.refresh([_game("g1", GameStatus.PREGAME)], now=T, fetch_feed=_Fetch())
    assert len(queue) == 1
    pipe.refresh([], now=T + timedelta(seconds=10), fetch_feed=_Fetch())  # g1 gone from the slate
    assert _id("g1") not in pipe._pregame_keys and len(queue) == 0


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


def test_event_stream_drains_through_the_post_final_window() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T + timedelta(seconds=5)),))
    pipe.refresh([_game("g1")], now=T + timedelta(seconds=5), fetch_feed=fetch)  # a play is held in the stream
    # The game ends before that play clears the delay — the stream is kept alive to drain it.
    fin = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert fin.big_plays == () and _id("g1") in pipe._event_streams  # still draining, not torn down
    # Once the play clears the delay it flashes, even though the game is already final.
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=35), fetch_feed=fetch)
    assert len(res.big_plays) == 1 and "bigplay" in res.big_plays[0].raw


def test_event_stream_dropped_when_the_game_leaves_the_slate() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T + timedelta(seconds=5)),))
    pipe.refresh([_game("g1")], now=T + timedelta(seconds=5), fetch_feed=fetch)
    assert _id("g1") in pipe._event_streams
    pipe.refresh([], now=T + timedelta(seconds=10), fetch_feed=fetch)  # g1 gone from the slate entirely
    assert _id("g1") not in pipe._event_streams  # dropped with the game — no leak


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


def test_no_hitter_card_not_surfaced_at_the_top_of_the_sixth() -> None:
    # In the top of the 6th the pitching side has finished only five innings — not yet news.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=6, phase=InningPhase.TOP, away_hits=0))
    res = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert res.no_hitters == () and len(queue) == 1  # just the live card — the bid is "through 5"


def test_perfect_game_surfaces_with_the_perfect_flag_and_finished_innings() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    # Away hitless in the top of the 7th (home has finished six) with a confirmed clean sheet.
    fetch.set("g1", _state(inning=7, phase=InningPhase.TOP, away_hits=0, away_reached_base=False))
    res = pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert [c.raw for c in res.no_hitters] == ["g1:nohitter"]
    card = queue.next_card(T, QUAD)
    assert card is not None and card.dedupe_key.raw == "g1:nohitter"
    assert card.payload.perfect is True and card.payload.through_inning == 6  # finished innings, not current


def test_no_hitter_card_dropped_when_its_game_leaves() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=8, away_hits=0))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)
    assert _id("g1") in pipe._no_hitter_keys
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert _id("g1") not in pipe._no_hitter_keys  # the no-hitter card is cleaned up with the live set...
    assert [c.raw for c in res.finals] == ["g1:final"] and len(queue) == 1  # ...and replaced by the final


# --- status (delay / suspension) path ---------------------------------------------


def test_status_card_surfaces_for_a_delayed_game() -> None:
    pipe, queue = _setup(lag=0)
    res = pipe.refresh([_game("g1", GameStatus.DELAYED)], now=T, fetch_feed=_Fetch())
    assert [c.raw for c in res.statuses] == ["g1:status"]
    assert len(queue) == 1


def test_status_card_surfaces_for_a_suspended_game() -> None:
    pipe, queue = _setup(lag=0)
    res = pipe.refresh([_game("g1", GameStatus.SUSPENDED)], now=T, fetch_feed=_Fetch())
    assert [c.raw for c in res.statuses] == ["g1:status"]


def test_status_card_needs_no_feed_fetch() -> None:
    # A paused game reveals no score, so the pipeline never fetches its feed — a dead feed
    # during a long rain delay must not cost the card.
    pipe, _ = _setup(lag=0)
    fetch = _Fetch()
    fetch.fail.add("g1")  # any fetch would raise -> a skip; there must be none
    res = pipe.refresh([_game("g1", GameStatus.DELAYED)], now=T, fetch_feed=fetch)
    assert [c.raw for c in res.statuses] == ["g1:status"] and res.skipped == ()


def test_live_game_pausing_swaps_the_live_card_for_a_status_card() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=4))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # live
    assert _id("g1") in pipe._card_keys and len(queue) == 1
    res = pipe.refresh([_game("g1", GameStatus.DELAYED)], now=T + timedelta(seconds=5), fetch_feed=fetch)
    assert [c.raw for c in res.statuses] == ["g1:status"]
    assert _id("g1") in res.removed  # the live card was pulled...
    assert _id("g1") not in pipe._card_keys and _id("g1") not in pipe._feeds  # ...and its live state torn down
    assert len(queue) == 1  # just the status card now


def test_paused_game_resuming_swaps_back_to_the_live_card() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    pipe.refresh([_game("g1", GameStatus.DELAYED)], now=T, fetch_feed=fetch)  # paused
    assert _id("g1") in pipe._status_keys and len(queue) == 1
    fetch.set("g1", _state(inning=4))
    res = pipe.refresh([_game("g1")], now=T + timedelta(seconds=5), fetch_feed=fetch)  # resumes live
    assert res.statuses == () and _id("g1") not in pipe._status_keys  # status card gone
    assert [c.raw for c in res.ingested] == ["g1:live"] and len(queue) == 1  # live card back


def test_status_card_dropped_when_the_game_ends() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    pipe.refresh([_game("g1", GameStatus.SUSPENDED)], now=T, fetch_feed=fetch)
    assert _id("g1") in pipe._status_keys
    fetch.set("g1", _state(inning=9))
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=5), fetch_feed=fetch)
    assert _id("g1") not in pipe._status_keys  # status card cleaned up...
    assert [c.raw for c in res.finals] == ["g1:final"] and len(queue) == 1  # ...replaced by the final


# --- final path -------------------------------------------------------------------


def test_final_is_held_inside_the_post_game_delay() -> None:
    # The final score is the ultimate spoiler, so a just-ended game shows nothing until the
    # broadcast lag elapses from when we first saw it final.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    assert res.finals == () and res.held == (_id("g1"),)  # ended, but the result is still embargoed
    assert len(queue) == 0


def test_final_reveals_once_the_delay_elapses() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)  # first seen final here
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=30), fetch_feed=fetch)
    assert [c.raw for c in res.finals] == ["g1:final"] and len(queue) == 1
    card = queue.next_card(T + timedelta(seconds=30), QUAD)
    assert card is not None and card.payload.away_score == 2 and card.payload.home_score == 5


def test_final_is_anchored_to_first_sight_not_to_a_late_poll() -> None:
    # A slow poll that first sees the game final well after it ended only ever delays the
    # reveal further (anchor = first sight), so the result can never surface early.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=0))
    late = T + timedelta(seconds=600)  # we did not poll until ten minutes after the game ended
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=late, fetch_feed=fetch)
    assert res.finals == () and res.held == (_id("g1"),)  # still embargoed: the clock starts at first sight
    res2 = pipe.refresh([_game("g1", GameStatus.FINAL)], now=late + timedelta(seconds=30), fetch_feed=fetch)
    assert [c.raw for c in res2.finals] == ["g1:final"]


def test_final_card_is_built_exactly_once() -> None:
    pipe, queue = _setup(lag=0)  # reveals on first sight
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    r1 = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    r2 = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert [c.raw for c in r1.finals] == ["g1:final"]
    assert r2.finals == ()  # already revealed — not rebuilt every tick
    assert len(queue) == 1


def test_final_not_refetched_once_revealed() -> None:
    # Once the final is shown, a lingering final game is not fetched again (it would otherwise
    # poll a finished game for its whole post-game window).
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    fetch.fail.add("g1")  # any later fetch would raise
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=10), fetch_feed=fetch)
    assert res.skipped == ()  # never refetched, so the now-failing feed is never touched


def test_final_isolates_a_fetch_failure() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.fail.add("g1")
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    assert any("g1" in warning for warning in res.skipped)
    assert res.finals == () and len(queue) == 0  # no card, but no crash


def test_final_card_carries_the_pitching_decisions() -> None:
    # The feed's W/L/S decisions ride through the pipeline onto the revealed final card.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    fetch.decisions["g1"] = PitchingDecisions(winner="Clayton Kershaw", loser="German Marquez", save="Tanner Scott")
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    assert [c.raw for c in res.finals] == ["g1:final"]
    card = queue.next_card(T, QUAD)
    assert card is not None and card.payload.decisions == fetch.decisions["g1"]


def test_final_card_dropped_when_the_game_leaves_the_slate() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)
    assert _id("g1") in pipe._final_keys and len(queue) == 1
    pipe.refresh([], now=T + timedelta(seconds=10), fetch_feed=fetch)  # g1 gone from the slate
    assert _id("g1") not in pipe._final_keys and len(queue) == 0


def test_last_live_frame_is_held_across_the_final_embargo() -> None:
    # H1-adjacent: a just-ended game keeps its last delay-safe live frame on screen — never
    # blank — through the post-game embargo, then the final card replaces it.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5, inning=9))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # live tick 1: observed, still inside the delay
    live = pipe.refresh([_game("g1")], now=T + timedelta(seconds=30), fetch_feed=fetch)  # delay elapsed
    assert [c.raw for c in live.ingested] == ["g1:live"]  # the last delay-safe frame is on screen

    # The game ends; the final is embargoed (first sight + 30s), but the live frame is held.
    held = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=35), fetch_feed=fetch)
    assert held.finals == () and _id("g1") in held.held
    assert _id("g1") in pipe._card_keys and _id("g1") not in held.removed  # live card not torn down
    card = queue.next_card(T + timedelta(seconds=35), QUAD)
    assert card is not None and card.dedupe_key.raw == "g1:live"  # the last live frame, not blank

    # Once the embargo elapses, the final reveals and the held frame is dropped in the same pass.
    reveal = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=65), fetch_feed=fetch)
    assert [c.raw for c in reveal.finals] == ["g1:final"] and _id("g1") in reveal.removed
    assert _id("g1") not in pipe._card_keys and len(queue) == 1  # the final replaced the held frame


def test_pending_final_forgotten_if_the_game_resumes() -> None:
    # A game that flickers final -> live (a resumed suspension) drops its pending reveal, so a
    # stale captured score can never surface later.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], now=T, fetch_feed=fetch)  # pending reveal armed
    assert _id("g1") in pipe._final_pending
    pipe.refresh([_game("g1", GameStatus.LIVE)], now=T + timedelta(seconds=5), fetch_feed=fetch)
    assert _id("g1") not in pipe._final_pending  # disarmed — it is live again


def test_walk_off_flashes_then_the_final_reveals() -> None:
    # A walk-off homer ends the game; the flash and the final ride the same delay, so the
    # walk-off surfaces (no later than the final) and the final confirms it — none of it early.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=3))
    pipe.refresh([_game("g1")], now=T, fetch_feed=fetch)  # live; first sight primes the backlog
    walk_off = _event("g1:ab:99", source_time=T + timedelta(seconds=1), away=2, home=3)
    fetch.set_events("g1", (walk_off,))
    pipe.refresh([_game("g1")], now=T + timedelta(seconds=2), fetch_feed=fetch)  # walk-off observed, held
    # The game flips to final right after the walk-off; both are still inside the delay.
    mid = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=3), fetch_feed=fetch)
    assert mid.big_plays == () and mid.finals == ()  # nothing leaks early
    # First-final-sight (T+3) + 30 = T+33; the walk-off (play at T+1) cleared at T+31.
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], now=T + timedelta(seconds=33), fetch_feed=fetch)
    assert len(res.big_plays) == 1  # the walk-off flashes...
    assert [c.raw for c in res.finals] == ["g1:final"]  # ...and the final confirms it
    assert _id("g1") not in pipe._event_streams  # stream drained and dropped at the reveal
