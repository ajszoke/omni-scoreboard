"""Tests for LiveBaseballPipeline: delay-safe live cards into the queue."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from omni.app.pipeline import LiveBaseballPipeline, MeterReading, PipelineResult
from omni.cards.base import CardId, DedupeKey, EndBasis, GameEnd
from omni.cards.baseball import PregameCardPayload
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballScoringImpact,
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    BettingLine,
    GameLines,
    InningPhase,
    PitchingDecisions,
    PregameDetails,
    PregameSide,
    WinLossRecord,
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


def _at(moment: datetime) -> ClockReading:
    """``moment`` read both ways, the monotonic clock running alongside UTC."""
    return ClockReading(moment, MonotonicTime((moment - T).total_seconds()))


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
    """A configurable game-feed fetcher that can also fail on command.

    Its feed gives the game's status as the schedule does, unless told otherwise (``statuses``).
    """

    def __init__(self) -> None:
        self.states: dict[str, BaseballGameState] = {}
        self.events: dict[str, tuple[BaseballGameEvent, ...]] = {}
        self.decisions: dict[str, PitchingDecisions] = {}
        self.warnings: dict[str, tuple[str, ...]] = {}
        self.statuses: dict[str, GameStatus] = {}
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
            status=self.statuses.get(game.id.raw, game.status),
        )


class _WinProb:
    """A win-probability source, read as of an instant; can be made to fail.

    Set ``value`` for a source that has answered all along with that reading (as of any instant, it
    arrived then), or `arrive` readings at moments to give it a timeline: a read as of an instant gets
    the last one to arrive by then. A reading of None is the source with nothing to say.
    """

    def __init__(self) -> None:
        self.value: WinProbability | None = None
        self.arrivals: list[tuple[datetime, WinProbability | None]] = []
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def arrive(self, value: WinProbability | None, at: datetime) -> None:
        self.arrivals.append((at, value))

    def __call__(self, game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        self.calls.append(game.id.raw)
        if game.id.raw in self.fail:
            raise ProviderError("win-probability feed down")
        if not self.arrivals:
            return MeterReading(self.value, as_of) if self.value is not None else None
        arrived = [(at, value) for at, value in self.arrivals if _at(at).monotonic <= as_of.monotonic]
        if not arrived:
            return None
        at, value = arrived[-1]
        return MeterReading(value, _at(at)) if value is not None else None


def _setup(lag: int = 30, *, factory: CardFactory = CardFactory()) -> tuple[LiveBaseballPipeline, InterleavedCardQueue]:
    queue = InterleavedCardQueue()
    pipe = LiveBaseballPipeline(
        scorer=PriorityScorer(),
        delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(lag)),
        factory=factory,
        queue=queue,
    )
    return pipe, queue


def test_holds_live_state_within_the_tv_delay() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert res.held == (_id("g1"),)
    assert res.ingested == () and len(queue) == 0


def test_surfaces_a_card_once_the_delay_elapses() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # buffered
    res = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=30)), fetch_feed=fetch)
    assert len(res.ingested) == 1 and len(queue) == 1


def test_shows_lag_old_state_not_the_current_spoiler() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(home=0))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # observes 0-0
    fetch.set("g1", _state(home=1))  # a run scores...
    pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=30)), fetch_feed=fetch)  # ...but it's fresh
    card = queue.next_card(_at(T + timedelta(seconds=30)), QUAD)
    assert card is not None
    assert card.payload.home_line.runs == 0  # the delayed 0-0, never the un-aired 0-1


def _meter(queue: InterleavedCardQueue, now: datetime) -> WinProbability | None:
    card = queue.next_card(_at(now), QUAD)
    assert card is not None
    reading: WinProbability | None = card.payload.win_probability
    return reading


def test_win_probability_never_knows_more_than_the_score_beside_it() -> None:
    # The meter shows the newest reading that arrived no later than the feed answer of the state on the card.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    wp = _WinProb()
    early, later, leading = (WinProbability(home=h, away=100.0 - h) for h in (60.0, 75.0, 99.0))
    wp.arrive(early, T)
    wp.arrive(later, T + timedelta(seconds=5))
    wp.arrive(leading, T + timedelta(seconds=11))

    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch, fetch_win_probability=wp)  # its answer arrives at T
    assert wp.calls == []  # nothing read while the state is still inside the delay
    pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch, fetch_win_probability=wp)
    at30 = T + timedelta(seconds=30)
    pipe.refresh([_game("g1")], at=_at(at30), fetch_feed=fetch, fetch_win_probability=wp)
    assert _meter(queue, at30) == early  # beside the state that arrived at T, not `later` (T + 5)
    at40 = T + timedelta(seconds=40)
    pipe.refresh([_game("g1")], at=_at(at40), fetch_feed=fetch, fetch_win_probability=wp)
    assert _meter(queue, at40) == later  # beside the state from T + 10; `leading` (T + 11) never shows with it


def test_a_reading_dated_after_the_state_it_would_stand_beside_is_turned_away() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())

    def ahead(game: TeamGame, as_of: ClockReading) -> MeterReading:
        return MeterReading(WinProbability(home=99.0, away=1.0), _at(as_of.utc + timedelta(seconds=1)))

    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch, fetch_win_probability=ahead)
    assert _meter(queue, T) is None


def test_an_older_reading_never_replaces_a_newer_one_held() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    wp = _WinProb()
    newer, older = WinProbability(home=70.0, away=30.0), WinProbability(home=50.0, away=50.0)
    wp.arrive(newer, T)
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch, fetch_win_probability=wp)
    wp.arrive(older, T - timedelta(seconds=5))  # an answer that arrives out of order
    at1 = T + timedelta(seconds=1)
    pipe.refresh([_game("g1")], at=_at(at1), fetch_feed=fetch, fetch_win_probability=wp)
    assert _meter(queue, at1) == newer


def test_win_probability_surfaces_once_clear_of_a_zero_lag() -> None:
    # With no delay the happy path attaches the current reading immediately.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    wp = _WinProb()
    wp.value = WinProbability(home=70.0, away=30.0)
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch, fetch_win_probability=wp)
    card = queue.next_card(_at(T), QUAD)
    assert card is not None and card.payload.win_probability == WinProbability(home=70.0, away=30.0)


def test_no_win_probability_fetcher_means_no_meter() -> None:
    # Omitting the fetcher (the meter disabled) leaves the live card's win_probability None.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    card = queue.next_card(_at(T), QUAD)
    assert card is not None and card.payload.win_probability is None


def test_win_probability_fetch_failure_is_a_nonfatal_warning() -> None:
    # A win-prob fetch failure must not drop the live card — it shows without a meter, plus a warning.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    wp = _WinProb()
    wp.fail.add("g1")
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch, fetch_win_probability=wp)
    assert len(res.ingested) == 1 and res.skipped == ()  # the live card still shows; not a game drop
    assert any("win-probability" in warning for warning in res.warnings)
    card = queue.next_card(_at(T), QUAD)
    assert card is not None and card.payload.win_probability is None  # no meter on a failed fetch


def test_win_probability_buffer_is_released_when_the_game_leaves() -> None:
    # The per-game win-prob buffer rides the state feed's lifecycle — dropped with the game, no leak.
    pipe, _queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    wp = _WinProb()
    wp.value = WinProbability(home=70.0, away=30.0)
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch, fetch_win_probability=wp)
    assert _id("g1") in pipe._meter_tracks  # a buffer exists while the game is live
    pipe.refresh([], at=_at(T + timedelta(seconds=1)), fetch_feed=fetch, fetch_win_probability=wp)
    assert _id("g1") not in pipe._meter_tracks  # gone with the game


MARKET_READ = WinProbability(home=70.0, away=30.0)
MODEL_READ = WinProbability(home=55.0, away=45.0)


class _MeterRig:
    """A live game with a market and a model behind its meter, stepped by seconds since `T`."""

    def __init__(self, *, lag: int = 0) -> None:
        self.pipe, self.queue = _setup(lag=lag)
        self.fetch = _Fetch()
        self.fetch.set("g1", _state(away=1, home=2))
        self.market, self.model = _WinProb(), _WinProb()
        self.warnings: tuple[str, ...] = ()

    def meter_at(self, seconds: int, *, jumped: timedelta = timedelta(0)) -> WinProbability | None:
        """The meter on the live card ``seconds`` in, the wall clock moved by ``jumped``."""
        at = ClockReading(T + timedelta(seconds=seconds) + jumped, MonotonicTime(seconds))
        result = self.pipe.refresh(
            [_game("g1")],
            at=at,
            fetch_feed=self.fetch,
            fetch_win_probability=self.market,
            fetch_fallback_win_probability=self.model,
        )
        self.warnings = result.warnings
        card = self.queue.next_card(at, QUAD)
        if card is None:  # the live card itself is still inside the TV delay
            return None
        reading: WinProbability | None = card.payload.win_probability
        return reading


def test_the_market_leads_and_the_model_is_not_even_asked_while_it_answers() -> None:
    rig = _MeterRig()
    rig.market.value, rig.model.value = MARKET_READ, MODEL_READ
    assert rig.meter_at(0) == MARKET_READ
    assert rig.meter_at(30) == MARKET_READ
    assert rig.model.calls == []


def test_a_reading_holds_through_a_gap_then_expires_at_three_minutes_to_the_model() -> None:
    rig = _MeterRig()
    rig.market.value, rig.model.value = MARKET_READ, MODEL_READ
    assert rig.meter_at(0) == MARKET_READ
    rig.market.value = None  # the market goes quiet
    assert rig.meter_at(60) == MARKET_READ
    assert rig.meter_at(179) == MARKET_READ  # still inside the three minutes
    assert rig.meter_at(180) == MODEL_READ  # expired: the model takes over


def test_the_market_takes_back_over_as_soon_as_it_returns() -> None:
    rig = _MeterRig()
    rig.market.value, rig.model.value = MARKET_READ, MODEL_READ
    rig.meter_at(0)
    rig.market.value = None
    assert rig.meter_at(180) == MODEL_READ
    rig.market.value = WinProbability(home=80.0, away=20.0)
    assert rig.meter_at(192) == WinProbability(home=80.0, away=20.0)


def test_the_handover_stays_delay_safe_and_never_leaves_the_meter_blank() -> None:
    # The model starts buffering when the market goes quiet, not when the reading expires, so it has
    # already cleared the TV delay by then; and a reading newer than the lag never reaches the meter.
    rig = _MeterRig(lag=30)
    rig.market.value, rig.model.value = MARKET_READ, MODEL_READ
    warmup = [rig.meter_at(seconds) for seconds in range(0, 108, 12)]
    assert warmup[0] is None and warmup[-1] == MARKET_READ  # nothing shows until a reading clears the delay
    assert rig.model.calls == []
    rig.market.value = None
    after = [rig.meter_at(seconds) for seconds in range(108, 420, 12)]
    assert after[0] == MARKET_READ and after[-1] == MODEL_READ
    assert None not in after  # the meter is never blank across the handover


def test_a_game_the_market_never_prices_uses_the_model_after_the_delay() -> None:
    rig = _MeterRig()
    rig.model.value = MODEL_READ
    assert rig.meter_at(0) == MODEL_READ


def test_the_model_holds_through_its_own_gaps_too() -> None:
    rig = _MeterRig()
    rig.model.value = MODEL_READ
    rig.meter_at(0)
    rig.model.value = None
    assert rig.meter_at(179) == MODEL_READ
    assert rig.meter_at(180) is None  # nothing left to show: better no meter than a stale one


def test_with_both_sources_quiet_the_meter_goes_away_at_three_minutes() -> None:
    rig = _MeterRig()
    rig.market.value = MARKET_READ
    rig.meter_at(0)
    rig.market.value = None
    assert rig.meter_at(179) == MARKET_READ
    assert rig.meter_at(180) is None


def test_a_reading_ages_on_the_monotonic_clock_so_a_wall_jump_back_keeps_it_no_longer() -> None:
    rig = _MeterRig()
    rig.market.value = MARKET_READ
    rig.meter_at(0)
    rig.market.value = None
    back = -timedelta(hours=1)  # an hour back on the wall clock: by UTC the reading would look an hour younger
    assert rig.meter_at(179, jumped=back) == MARKET_READ
    assert rig.meter_at(180, jumped=back) is None


def test_a_market_outage_warns_and_the_model_still_answers() -> None:
    rig = _MeterRig()
    rig.model.value = MODEL_READ
    rig.market.fail.add("g1")
    assert rig.meter_at(0) == MODEL_READ
    assert any("win-probability fetch failed" in warning for warning in rig.warnings)


def test_both_sources_failing_only_warns() -> None:
    rig = _MeterRig()
    rig.market.fail.add("g1")
    rig.model.fail.add("g1")
    assert rig.meter_at(0) is None  # the live card still shows, just without a meter
    assert sum("fetch failed" in warning for warning in rig.warnings) == 2


# --- pregame details and odds ------------------------------------------------------


COL_SIDE = PregameSide(record=WinLossRecord(wins=38, losses=44))
LAD_SIDE = PregameSide(record=WinLossRecord(wins=51, losses=31))
DETAILS = PregameDetails(away=COL_SIDE, home=LAD_SIDE)
LINES = GameLines(away=BettingLine(moneyline=165), home=BettingLine(moneyline=-195))


def _pregame_payload(queue: InterleavedCardQueue, now: datetime) -> PregameCardPayload:
    card = queue.next_card(_at(now), QUAD)
    assert card is not None and isinstance(card.payload, PregameCardPayload)
    return card.payload


def test_pregame_card_carries_the_details_and_odds_when_given_sources() -> None:
    pipe, queue = _setup()
    pipe.refresh(
        [_game("g1", GameStatus.SCHEDULED)],
        at=_at(T),
        fetch_feed=_Fetch(),
        fetch_pregame_details=lambda _game: DETAILS,
        fetch_game_lines=lambda _game, _now: LINES,
    )
    payload = _pregame_payload(queue, T)
    assert payload.away == PregameSide(record=COL_SIDE.record, line=LINES.away)
    assert payload.home == PregameSide(record=LAD_SIDE.record, line=LINES.home)


def test_pregame_odds_alone_still_fill_out_the_card() -> None:
    pipe, queue = _setup()
    pipe.refresh(
        [_game("g1", GameStatus.SCHEDULED)], at=_at(T), fetch_feed=_Fetch(), fetch_game_lines=lambda _game, _now: LINES
    )
    assert _pregame_payload(queue, T).home == PregameSide(line=LINES.home)


def test_pregame_stays_plain_without_sources_or_when_a_game_is_off_the_board() -> None:
    pipe, queue = _setup()
    pipe.refresh(
        [_game("g1", GameStatus.SCHEDULED)], at=_at(T), fetch_feed=_Fetch(), fetch_game_lines=lambda _game, _now: None
    )
    payload = _pregame_payload(queue, T)
    assert payload.away is None and payload.home is None


def test_pregame_details_are_read_every_pass_from_what_was_last_heard() -> None:
    pipe, queue = _setup()
    calls: list[str] = []
    known: dict[str, PregameDetails] = {}

    def details(game: TeamGame) -> PregameDetails | None:
        calls.append(game.id.raw)
        return known.get(game.id.raw)

    games = [_game("g1", GameStatus.SCHEDULED)]
    pipe.refresh(games, at=_at(T), fetch_feed=_Fetch(), fetch_pregame_details=details)
    assert _pregame_payload(queue, T).home is None  # nothing heard yet: the card stays plain
    known["g1"] = DETAILS
    later = T + timedelta(seconds=5)
    pipe.refresh(games, at=_at(later), fetch_feed=_Fetch(), fetch_pregame_details=details)
    assert calls == ["g1", "g1"] and _pregame_payload(queue, later).home == LAD_SIDE


def test_a_pregame_details_failure_warns_and_the_card_goes_without() -> None:
    pipe, queue = _setup()

    def down(game: TeamGame) -> PregameDetails | None:
        raise ProviderError("feed down")

    res = pipe.refresh([_game("g1", GameStatus.SCHEDULED)], at=_at(T), fetch_feed=_Fetch(), fetch_pregame_details=down)
    assert any("pregame details fetch failed" in warning for warning in res.warnings)
    assert _pregame_payload(queue, T).home is None


def test_an_odds_failure_warns_and_the_card_just_goes_without() -> None:
    pipe, queue = _setup()

    def down(_game: TeamGame, _now: datetime) -> GameLines | None:
        raise ProviderError("coupon down")

    res = pipe.refresh([_game("g1", GameStatus.SCHEDULED)], at=_at(T), fetch_feed=_Fetch(), fetch_game_lines=down)
    assert any("odds fetch failed" in warning for warning in res.warnings)
    assert _pregame_payload(queue, T).home is None


def test_a_non_carding_status_surfaces_nothing() -> None:
    # A postponed game (not upcoming, live, paused, nor final) produces no card on any path —
    # exercising the full PipelineResult shape in one assertion.
    pipe, queue = _setup()
    res = pipe.refresh([_game("g2", GameStatus.POSTPONED)], at=_at(T), fetch_feed=_Fetch())
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
    res = pipe.refresh([_game("g1"), _game("g2")], at=_at(T), fetch_feed=fetch)
    assert any("g1" in warning for warning in res.skipped)
    assert len(res.ingested) == 1 and len(queue) == 1  # g2 still made it through


def test_surfaces_per_play_warnings_without_dropping_the_game() -> None:
    # A malformed play is isolated in the provider; the pipeline surfaces it as a warning, but
    # the live card still ingests — a per-play drop is not a per-game failure.
    pipe, queue = _setup(lag=0)  # eligible immediately
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=2))
    fetch.warnings["g1"] = ("play 4: ValueError: bad rbi",)
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert res.warnings == ("g1: play 4: ValueError: bad rbi",)
    assert len(res.ingested) == 1 and len(queue) == 1  # the game still shows
    assert res.skipped == ()  # not a dropped-game failure


def test_surfaces_per_play_warnings_for_a_final_game() -> None:
    # The final path collects feed warnings too (off the same per-game fetch).
    pipe, _queue = _setup(lag=0)  # final reveals as soon as the game is seen final
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=1))
    fetch.warnings["g1"] = ("play 7: AttributeError: null about",)
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)
    assert res.warnings == ("g1: play 7: AttributeError: null about",)


def test_live_card_is_swapped_for_the_final_when_the_game_ends() -> None:
    pipe, queue = _setup(lag=0)  # no delay, so the final reveals as soon as the game is seen final
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert len(queue) == 1  # the live card
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert res.removed == (_id("g1"),)  # the live card is gone...
    assert [c.raw for c in res.finals] == ["g1:final"]  # ...replaced by the final card
    assert len(queue) == 1


def test_processes_multiple_live_games() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    fetch.set("g2", _state(away=1, home=1))
    res = pipe.refresh([_game("g1"), _game("g2")], at=_at(T), fetch_feed=fetch)
    assert len(res.ingested) == 2 and len(queue) == 2


def test_drops_feed_when_a_game_leaves_before_ever_surfacing() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    res1 = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # buffered, no card yet
    assert res1.held == (_id("g1"),) and len(queue) == 0
    res2 = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert res2.removed == ()  # nothing was ever carded, so nothing to "remove"
    assert len(queue) == 0
    # The feed was dropped: going live again starts buffering afresh.
    res3 = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=20)), fetch_feed=fetch)
    assert res3.held == (_id("g1"),)


# --- pregame path -----------------------------------------------------------------


def test_upcoming_game_surfaces_a_pregame_card() -> None:
    pipe, queue = _setup()
    res = pipe.refresh([_game("g1", GameStatus.SCHEDULED)], at=_at(T), fetch_feed=_Fetch())
    assert [c.raw for c in res.pregames] == ["g1:pregame"]
    assert res.ingested == () and len(queue) == 1


def test_both_scheduled_and_pregame_statuses_card() -> None:
    pipe, queue = _setup()
    res = pipe.refresh(
        [_game("g1", GameStatus.SCHEDULED), _game("g2", GameStatus.PREGAME)], at=_at(T), fetch_feed=_Fetch()
    )
    assert sorted(c.raw for c in res.pregames) == ["g1:pregame", "g2:pregame"]
    assert len(queue) == 2


def test_pregame_needs_no_feed_fetch() -> None:
    # A pregame card is built from the schedule alone, so even a failing feed never blocks it.
    pipe, queue = _setup()
    fetch = _Fetch()
    fetch.fail.add("g1")  # would raise if the pipeline tried to fetch
    res = pipe.refresh([_game("g1", GameStatus.PREGAME)], at=_at(T), fetch_feed=fetch)
    assert [c.raw for c in res.pregames] == ["g1:pregame"]
    assert res.skipped == ()  # never attempted a fetch


def test_pregame_card_yields_when_the_game_goes_live() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    pipe.refresh([_game("g1", GameStatus.PREGAME)], at=_at(T), fetch_feed=fetch)
    assert _id("g1") in pipe._pregame_keys and len(queue) == 1
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1", GameStatus.LIVE)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert _id("g1") not in pipe._pregame_keys  # pregame card dropped...
    assert [c.raw for c in res.ingested] == ["g1:live"]  # ...replaced by the live card
    assert len(queue) == 1  # one card for the game, not two


def test_pregame_card_removed_when_the_game_leaves_the_slate() -> None:
    pipe, queue = _setup()
    pipe.refresh([_game("g1", GameStatus.PREGAME)], at=_at(T), fetch_feed=_Fetch())
    assert len(queue) == 1
    pipe.refresh([], at=_at(T + timedelta(seconds=10)), fetch_feed=_Fetch())  # g1 gone from the slate
    assert _id("g1") not in pipe._pregame_keys and len(queue) == 0


# --- big-play event path ----------------------------------------------------------


def test_notable_event_surfaces_a_big_play_after_the_delay() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # first sight: nothing to flash yet
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T + timedelta(seconds=5)),))
    held = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)
    assert held.big_plays == ()  # observed, but still inside the TV delay
    fired = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=35)), fetch_feed=fetch)  # source_time + lag
    assert len(fired.big_plays) == 1 and "bigplay" in fired.big_plays[0].raw


def test_a_big_play_fires_exactly_once_across_polls() -> None:
    pipe, queue = _setup(lag=0)  # eligible as soon as it is observed
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T),))
    r2 = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    r3 = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=20)), fetch_feed=fetch)  # same event still in feed
    assert len(r2.big_plays) == 1
    assert r3.big_plays == ()  # deduped by lineage — not re-flashed every tick


def test_a_routine_event_does_not_flash() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    single = _event("g1:ab:3", event_type=BaseballGameEventType.SINGLE, band=DisplayPriority.NORMAL, source_time=T)
    fetch.set_events("g1", (single,))
    res = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert res.big_plays == ()  # below the big-play band: informs the live card, never a takeover


def test_backlog_is_suppressed_on_first_sight() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T),))  # already in the feed when we tune in
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # primed -> suppressed
    res = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=120)), fetch_feed=fetch)  # long past any delay
    assert res.big_plays == ()  # joining mid-game never dumps the earlier plays onto the screen


def test_event_stream_drains_through_the_post_final_window() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T + timedelta(seconds=5)),))
    pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)  # a play is held in the stream
    # The game ends before that play clears the delay — the stream is kept alive to drain it.
    fin = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert fin.big_plays == () and _id("g1") in pipe._event_streams  # still draining, not torn down
    # Once the play clears the delay it flashes, even though the game is already final.
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=35)), fetch_feed=fetch)
    assert len(res.big_plays) == 1 and "bigplay" in res.big_plays[0].raw


def test_event_stream_dropped_when_the_game_leaves_the_slate() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    fetch.set_events("g1", (_event("g1:ab:9", source_time=T + timedelta(seconds=5)),))
    pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)
    assert _id("g1") in pipe._event_streams
    pipe.refresh([], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)  # g1 gone from the slate entirely
    assert _id("g1") not in pipe._event_streams  # dropped with the game — no leak


# --- no-hitter path ---------------------------------------------------------------


def test_no_hitter_card_surfaces_when_a_side_is_hitless() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=7, away_hits=0, home_hits=4))  # away hitless -> home's no-no
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert [c.raw for c in res.no_hitters] == ["g1:nohitter"]
    assert len(queue) == 2  # the live card + the no-hitter card


def test_no_hitter_card_is_removed_when_the_bid_breaks() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=7, away_hits=0))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert len(queue) == 2
    fetch.set("g1", _state(inning=7, away_hits=1))  # a hit — the bid is broken
    res = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert res.no_hitters == () and len(queue) == 1  # just the live card now


def test_no_hitter_card_not_surfaced_before_the_min_inning() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=3, away_hits=0))  # hitless, but only the 3rd — routine
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert res.no_hitters == () and len(queue) == 1


def test_no_hitter_card_not_surfaced_at_the_top_of_the_sixth() -> None:
    # In the top of the 6th the pitching side has finished only five innings — not yet news.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=6, phase=InningPhase.TOP, away_hits=0))
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert res.no_hitters == () and len(queue) == 1  # just the live card — the bid is "through 5"


def test_perfect_game_surfaces_with_the_perfect_flag_and_finished_innings() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    # Away hitless in the top of the 7th (home has finished six) with a confirmed clean sheet.
    fetch.set("g1", _state(inning=7, phase=InningPhase.TOP, away_hits=0, away_reached_base=False))
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert [c.raw for c in res.no_hitters] == ["g1:nohitter"]
    card = queue.next_card(_at(T), QUAD)
    assert card is not None and card.dedupe_key.raw == "g1:nohitter"
    assert card.payload.perfect is True and card.payload.through_inning == 6  # finished innings, not current


def test_no_hitter_card_dropped_when_its_game_leaves() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=8, away_hits=0))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert _id("g1") in pipe._no_hitter_keys
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert _id("g1") not in pipe._no_hitter_keys  # the no-hitter card is cleaned up with the live set...
    assert [c.raw for c in res.finals] == ["g1:final"] and len(queue) == 1  # ...and replaced by the final


# --- status (delay / suspension) path ---------------------------------------------


def test_status_card_surfaces_for_a_delayed_game() -> None:
    pipe, queue = _setup(lag=0)
    res = pipe.refresh([_game("g1", GameStatus.DELAYED)], at=_at(T), fetch_feed=_Fetch())
    assert [c.raw for c in res.statuses] == ["g1:status"]
    assert len(queue) == 1


def test_status_card_surfaces_for_a_suspended_game() -> None:
    pipe, queue = _setup(lag=0)
    res = pipe.refresh([_game("g1", GameStatus.SUSPENDED)], at=_at(T), fetch_feed=_Fetch())
    assert [c.raw for c in res.statuses] == ["g1:status"]


def test_status_card_needs_no_feed_fetch() -> None:
    # A paused game reveals no score, so the pipeline never fetches its feed — a dead feed
    # during a long rain delay must not cost the card.
    pipe, _ = _setup(lag=0)
    fetch = _Fetch()
    fetch.fail.add("g1")  # any fetch would raise -> a skip; there must be none
    res = pipe.refresh([_game("g1", GameStatus.DELAYED)], at=_at(T), fetch_feed=fetch)
    assert [c.raw for c in res.statuses] == ["g1:status"] and res.skipped == ()


def test_live_game_pausing_swaps_the_live_card_for_a_status_card() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(inning=4))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # live
    assert _id("g1") in pipe._card_keys and len(queue) == 1
    res = pipe.refresh([_game("g1", GameStatus.DELAYED)], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)
    assert [c.raw for c in res.statuses] == ["g1:status"]
    assert _id("g1") in res.removed  # the live card was pulled...
    assert _id("g1") not in pipe._card_keys and _id("g1") not in pipe._feeds  # ...and its live state torn down
    assert len(queue) == 1  # just the status card now


def test_paused_game_resuming_swaps_back_to_the_live_card() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    pipe.refresh([_game("g1", GameStatus.DELAYED)], at=_at(T), fetch_feed=fetch)  # paused
    assert _id("g1") in pipe._status_keys and len(queue) == 1
    fetch.set("g1", _state(inning=4))
    res = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)  # resumes live
    assert res.statuses == () and _id("g1") not in pipe._status_keys  # status card gone
    assert [c.raw for c in res.ingested] == ["g1:live"] and len(queue) == 1  # live card back


def test_status_card_dropped_when_the_game_ends() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    pipe.refresh([_game("g1", GameStatus.SUSPENDED)], at=_at(T), fetch_feed=fetch)
    assert _id("g1") in pipe._status_keys
    fetch.set("g1", _state(inning=9))
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)
    assert _id("g1") not in pipe._status_keys  # status card cleaned up...
    assert [c.raw for c in res.finals] == ["g1:final"] and len(queue) == 1  # ...replaced by the final


# --- final path -------------------------------------------------------------------


def test_final_is_held_inside_the_post_game_delay() -> None:
    # The final score is the ultimate spoiler, so a just-ended game shows nothing until the
    # broadcast lag elapses from when we first saw it final.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)
    assert res.finals == () and res.held == (_id("g1"),)  # ended, but the result is still embargoed
    assert len(queue) == 0


def test_final_reveals_once_the_delay_elapses() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)  # first seen final here
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=30)), fetch_feed=fetch)
    assert [c.raw for c in res.finals] == ["g1:final"] and len(queue) == 1
    card = queue.next_card(_at(T + timedelta(seconds=30)), QUAD)
    assert card is not None and card.payload.away_score == 2 and card.payload.home_score == 5


def test_final_is_anchored_to_first_sight_not_to_a_late_poll() -> None:
    # A slow poll that first sees the game final well after it ended only ever delays the
    # reveal further (anchor = first sight), so the result can never surface early.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1, home=0))
    late = T + timedelta(seconds=600)  # we did not poll until ten minutes after the game ended
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(late), fetch_feed=fetch)
    assert res.finals == () and res.held == (_id("g1"),)  # still embargoed: the clock starts at first sight
    res2 = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(late + timedelta(seconds=30)), fetch_feed=fetch)
    assert [c.raw for c in res2.finals] == ["g1:final"]


def test_final_card_is_built_exactly_once() -> None:
    pipe, queue = _setup(lag=0)  # reveals on first sight
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    r1 = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)
    r2 = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert [c.raw for c in r1.finals] == ["g1:final"]
    assert r2.finals == ()  # already revealed — not rebuilt every tick
    assert len(queue) == 1


def test_final_not_refetched_once_revealed() -> None:
    # Once the final is shown, a lingering final game is not fetched again (it would otherwise
    # poll a finished game for its whole post-game window).
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)
    fetch.fail.add("g1")  # any later fetch would raise
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert res.skipped == ()  # never refetched, so the now-failing feed is never touched


def test_final_isolates_a_fetch_failure() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.fail.add("g1")
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)
    assert any("g1" in warning for warning in res.skipped)
    assert res.finals == () and len(queue) == 0  # no card, but no crash


def test_final_card_carries_the_pitching_decisions() -> None:
    # The feed's W/L/S decisions ride through the pipeline onto the revealed final card.
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    fetch.decisions["g1"] = PitchingDecisions(winner="Clayton Kershaw", loser="German Marquez", save="Tanner Scott")
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)
    assert [c.raw for c in res.finals] == ["g1:final"]
    card = queue.next_card(_at(T), QUAD)
    assert card is not None and card.payload.decisions == fetch.decisions["g1"]


def test_final_card_dropped_when_the_game_leaves_the_slate() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)
    assert _id("g1") in pipe._finals and len(queue) == 1
    pipe.refresh([], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)  # g1 gone from the slate
    assert _id("g1") not in pipe._finals and len(queue) == 0


def test_last_live_frame_is_held_across_the_final_embargo() -> None:
    # H1-adjacent: a just-ended game keeps its last delay-safe live frame on screen — never
    # blank — through the post-game embargo, then the final card replaces it.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5, inning=9))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # live tick 1: observed, still inside the delay
    live = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=30)), fetch_feed=fetch)  # delay elapsed
    assert [c.raw for c in live.ingested] == ["g1:live"]  # the last delay-safe frame is on screen

    # The game ends; the final is embargoed (first sight + 30s), but the live frame is held.
    held = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=35)), fetch_feed=fetch)
    assert held.finals == () and _id("g1") in held.held
    assert _id("g1") in pipe._card_keys and _id("g1") not in held.removed  # live card not torn down
    card = queue.next_card(_at(T + timedelta(seconds=35)), QUAD)
    assert card is not None and card.dedupe_key.raw == "g1:live"  # the last live frame, not blank

    # Once the embargo elapses, the final reveals and the held frame is dropped in the same pass.
    reveal = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=65)), fetch_feed=fetch)
    assert [c.raw for c in reveal.finals] == ["g1:final"] and _id("g1") in reveal.removed
    assert _id("g1") not in pipe._card_keys and len(queue) == 1  # the final replaced the held frame


def test_pending_final_forgotten_if_the_game_resumes() -> None:
    # A game that flickers final -> live (a resumed suspension) drops its pending reveal, so a
    # stale captured score can never surface later.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)  # pending reveal armed
    assert _id("g1") in pipe._finals
    pipe.refresh([_game("g1", GameStatus.LIVE)], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)
    assert _id("g1") not in pipe._finals  # disarmed — it is live again


def test_walk_off_flashes_then_the_final_reveals() -> None:
    # A walk-off homer ends the game; the flash and the final ride the same delay, so the
    # walk-off surfaces (no later than the final) and the final confirms it — none of it early.
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=3))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # live; first sight primes the backlog
    walk_off = _event("g1:ab:99", source_time=T + timedelta(seconds=1), away=2, home=3)
    fetch.set_events("g1", (walk_off,))
    pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=2)), fetch_feed=fetch)  # walk-off observed, held
    # The game flips to final right after the walk-off; both are still inside the delay.
    mid = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=3)), fetch_feed=fetch)
    assert mid.big_plays == () and mid.finals == ()  # nothing leaks early
    # First-final-sight (T+3) + 30 = T+33; the walk-off (play at T+1) cleared at T+31.
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=33)), fetch_feed=fetch)
    assert len(res.big_plays) == 1  # the walk-off flashes...
    assert [c.raw for c in res.finals] == ["g1:final"]  # ...and the final confirms it
    assert _id("g1") not in pipe._event_streams  # stream drained and dropped at the reveal


def test_a_state_is_dated_by_when_its_answer_arrived() -> None:
    pipe, queue = _setup(lag=30)
    arrived = T - timedelta(seconds=30)  # read from the plan now, but it arrived 30 s ago

    def fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        return LiveBaseballFeed(state=_state(), received=_at(arrived))

    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch_feed)
    assert len(res.ingested) == 1 and len(queue) == 1  # already clear of the delay


def _walk_off(eid: str, *, source_time: datetime) -> BaseballGameEvent:
    event = _event(eid, source_time=source_time, away=2, home=3)
    impact = BaseballScoringImpact(rbi=1, go_ahead=True, walk_off=True)
    return dataclasses.replace(event, payload=dataclasses.replace(event.payload, scoring_impact=impact))


def test_a_final_waits_for_its_plays_and_airs_after_its_walk_off() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=3))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # live; first sight primes the backlog
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=3)), fetch_feed=fetch)
    # The play-by-play lags the score: the walk-off is listed only in the next answer, which gives no time of its
    # own, so it waits the full delay from that receipt. The feed's clock runs a little ahead of ours: the walk-off
    # is stamped after we first saw the final.
    walk_off = _walk_off("g1:ab:99", source_time=T + timedelta(seconds=5))
    fetch.set_events("g1", (walk_off,))
    first_final = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert first_final.releases == ((_id("g1"), T + timedelta(seconds=33)),)  # its result's reveal
    held = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=33)), fetch_feed=fetch)
    assert held.finals == () and _id("g1") in held.held  # the result could show, but its walk-off can't yet
    assert held.releases == ((_id("g1"), T + timedelta(seconds=40)),)  # then the walk-off clears
    assert pipe.next_deadline(_at(T + timedelta(seconds=33))) == _at(T + timedelta(seconds=40)).monotonic
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=40)), fetch_feed=fetch)
    assert len(res.big_plays) == 1 and [card.raw for card in res.finals] == ["g1:final"]
    final = queue.card("g1:final")
    assert final is not None and final.after == (CardFactory.big_play_key(_game("g1"), walk_off),)
    assert final.timing.available_at == T + timedelta(seconds=33)  # available from when its result could show
    # A walk-off stamped after the game was first seen final isn't a credible end: first sight orders it.
    ended = GameEnd(T + timedelta(seconds=3), EndBasis.FIRST_SEEN)
    assert final.ended == ended and res.ends == ((CardId("g1:final"), ended),)


def test_a_final_is_ordered_by_its_walk_off_and_replaces_its_game_s_live_card() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=3))
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)  # live; first sight primes the backlog
    walk_off = _walk_off("g1:ab:99", source_time=T + timedelta(seconds=2))
    fetch.set_events("g1", (walk_off,))
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch)
    final = queue.card("g1:final")
    ended = GameEnd(T + timedelta(seconds=2), EndBasis.DECISIVE_PLAY)  # the play that ended it
    assert final is not None and final.ended == ended and res.ends == ((CardId("g1:final"), ended),)
    assert final.replaces == DedupeKey("g1:live") and queue.card("g1:live") is None  # it takes the live card's place


def test_a_final_with_no_live_card_up_replaces_nothing() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=3))
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_at(T), fetch_feed=fetch)  # first seen already final
    final = queue.card("g1:final")
    assert final is not None and final.replaces is None
    assert res.ends == ((CardId("g1:final"), GameEnd(T, EndBasis.FIRST_SEEN)),)


def test_a_game_s_end_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        GameEnd(datetime(2026, 6, 17, 23, 0), EndBasis.FIRST_SEEN)


def test_a_game_whose_feed_stops_answering_goes_stale_then_comes_down() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away_hits=0, inning=8, phase=InningPhase.BOTTOM))  # a no-hit bid: two cards
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert {card.dedupe_key.raw for card in queue.eligible(T, QUAD)} == {"g1:live", "g1:nohitter"}
    fetch.fail.add("g1")
    res = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=10)), fetch_feed=fetch)
    assert res.stale == (_id("g1"),)
    marked = {card.dedupe_key.raw: card.stale_since for card in queue.eligible(T, QUAD)}
    assert marked == {"g1:live": T + timedelta(seconds=10), "g1:nohitter": T + timedelta(seconds=10)}
    pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=60)), fetch_feed=fetch)  # still stale: marked once
    assert pipe.next_deadline(_at(T + timedelta(seconds=60))) == _at(T + timedelta(seconds=130)).monotonic
    pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=130)), fetch_feed=fetch)
    assert len(queue) == 0  # two minutes on, the last good comes down
    fetch.fail.clear()
    back = pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=140)), fetch_feed=fetch)
    live = queue.card("g1:live")
    assert back.stale == () and live is not None and live.stale_since is None


def test_a_stale_game_that_leaves_the_live_set_is_forgotten() -> None:
    pipe, _ = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    fetch.fail.add("g1")
    assert pipe.refresh([_game("g1")], at=_at(T + timedelta(seconds=5)), fetch_feed=fetch).stale == (_id("g1"),)
    assert pipe.refresh([], at=_at(T + timedelta(seconds=6)), fetch_feed=fetch).stale == ()


def test_the_next_deadline_is_the_soonest_release() -> None:
    pipe, _ = _setup(lag=30)
    assert pipe.next_deadline(_at(T)) is None
    fetch = _Fetch()
    fetch.set("g1", _state())
    res = pipe.refresh([_game("g1")], at=_at(T), fetch_feed=fetch)
    assert pipe.next_deadline(_at(T)) == _at(T + timedelta(seconds=30)).monotonic  # its state clears the delay
    assert res.releases == ((_id("g1"), T + timedelta(seconds=30)),)  # and the held game says when


def _seconds(seconds: float) -> ClockReading:
    return _at(T + timedelta(seconds=seconds))


def _final_score(queue: InterleavedCardQueue) -> tuple[int, int] | None:
    card = queue.card("g1:final")
    return (card.payload.away_score, card.payload.home_score) if card is not None else None


# --- a result only from the feed's own final, and corrections after it ---------------------------------


def test_a_schedule_final_whose_feed_still_answers_live_shows_no_result_and_keeps_its_live_card() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=2))
    pipe.refresh([_game("g1")], at=_seconds(0), fetch_feed=fetch)
    fetch.statuses["g1"] = GameStatus.LIVE  # the schedule moves first
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(5), fetch_feed=fetch)
    assert res.finals == () and _final_score(queue) is None and res.unconfirmed == (_id("g1"),)
    fetch.set("g1", _state(away=2, home=3))  # the feed's live answers still move the live card
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(10), fetch_feed=fetch)
    live = queue.card("g1:live")
    assert live is not None and live.payload.home_line.runs == 3 and res.ingested == (CardId("g1:live"),)
    assert pipe.next_deadline(_seconds(10)) == _seconds(35).monotonic  # when it goes stale, unconfirmed
    del fetch.statuses["g1"]  # now the feed says final
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(20), fetch_feed=fetch)
    assert _final_score(queue) == (2, 3) and res.unconfirmed == () and res.stale == ()


def test_a_game_whose_feed_says_final_before_its_schedule_does_gets_its_result_from_the_feed() -> None:
    pipe, queue = _setup(lag=30, factory=CardFactory(final_postgame_window=DurationSeconds(60)))
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=3))
    pipe.refresh([_game("g1")], at=_seconds(0), fetch_feed=fetch)
    pipe.refresh([_game("g1")], at=_seconds(30), fetch_feed=fetch)  # the live card is up
    fetch.statuses["g1"] = GameStatus.FINAL  # the feed says final; the schedule (a stale answer) still says live
    held = pipe.refresh([_game("g1")], at=_seconds(40), fetch_feed=fetch)
    assert held.held == (_id("g1"),) and held.removed == () and queue.card("g1:live") is not None
    res = pipe.refresh([_game("g1")], at=_seconds(70), fetch_feed=fetch)
    assert [card.raw for card in res.finals] == ["g1:final"] and res.removed == (_id("g1"),)
    assert _final_score(queue) == (2, 3)
    queue.prune(T + timedelta(seconds=130))  # its post-game window ends
    fetch.set("g1", _state(away=2, home=4))
    gone = pipe.refresh([_game("g1")], at=_seconds(131), fetch_feed=fetch)
    assert gone.corrections == () and gone.ingested == () and queue.card("g1:final") is None


def test_a_final_the_feed_confirmed_reveals_on_time_though_its_reads_then_fail() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(0), fetch_feed=fetch)
    fetch.fail.add("g1")
    held = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(10), fetch_feed=fetch)
    assert held.held == (_id("g1"),) and held.stale == ()  # its feed has already said final
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(30), fetch_feed=fetch)
    assert [card.raw for card in res.finals] == ["g1:final"] and _final_score(queue) == (2, 5)


def test_a_live_answer_after_the_feed_said_final_changes_nothing() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(0), fetch_feed=fetch)
    fetch.statuses["g1"] = GameStatus.LIVE  # an answer from a lagging server
    fetch.set("g1", _state(away=2, home=4))
    held = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(10), fetch_feed=fetch)
    assert held.unconfirmed == () and held.ingested == ()
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(30), fetch_feed=fetch)
    assert _final_score(queue) == (2, 5)


def test_a_later_final_answer_with_a_new_score_corrects_the_final_once_it_clears_the_delay() -> None:
    pipe, queue = _setup(lag=30)
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(0), fetch_feed=fetch)
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(30), fetch_feed=fetch)
    revealed = queue.card("g1:final")
    assert revealed is not None and _final_score(queue) == (2, 5)
    same = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(35), fetch_feed=fetch)
    assert same.corrections == ()  # the same result read again changes nothing
    fetch.set("g1", _state(away=2, home=6))  # a scoring change, received at 40
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(40), fetch_feed=fetch)
    early = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(69), fetch_feed=fetch)
    assert early.corrections == () and _final_score(queue) == (2, 5)  # a new observation: it waits the delay
    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(70), fetch_feed=fetch)
    corrected = queue.card("g1:final")
    assert res.corrections == (CardId("g1:final"),) and _final_score(queue) == (2, 6)
    assert corrected is not None and corrected.timing == revealed.timing  # in place, within its own window
    fetch.fail.add("g1")
    assert pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(80), fetch_feed=fetch).skipped == ()


def test_a_correction_to_a_final_whose_window_is_over_doesnt_bring_it_back() -> None:
    pipe, queue = _setup(lag=0, factory=CardFactory(final_postgame_window=DurationSeconds(60)))
    fetch = _Fetch()
    fetch.set("g1", _state(away=2, home=5))
    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(0), fetch_feed=fetch)
    queue.prune(T + timedelta(seconds=60))  # its post-game window ends
    fetch.set("g1", _state(away=2, home=6))
    asked: list[datetime] = []

    def counted(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        asked.append(now)
        return fetch(game, now)

    res = pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(61), fetch_feed=counted)
    assert res.corrections == () and queue.card("g1:final") is None and asked == []  # its feed isn't even read


def test_a_game_that_leaves_the_slate_unconfirmed_or_current_is_forgotten() -> None:
    pipe, _ = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state())
    fetch.statuses["g1"] = GameStatus.LIVE

    def current(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        return dataclasses.replace(fetch(game, now), current_for=timedelta(seconds=30))

    pipe.refresh([_game("g1", GameStatus.FINAL)], at=_seconds(0), fetch_feed=current)
    assert pipe._unconfirmed and pipe._current_until
    pipe.refresh([], at=_seconds(1), fetch_feed=current)
    assert not pipe._unconfirmed and not pipe._current_until and pipe.next_deadline(_seconds(1)) is None


def test_a_state_that_clears_the_delay_after_its_cards_came_down_for_staleness_stays_down() -> None:
    pipe, queue = _setup(lag=0)
    fetch = _Fetch()
    fetch.set("g1", _state(away=1))
    pipe.refresh([_game("g1")], at=_seconds(0), fetch_feed=fetch)
    fetch.fail.add("g1")
    pipe.refresh([_game("g1")], at=_seconds(1), fetch_feed=fetch)  # stale from here
    pipe.refresh([_game("g1")], at=_seconds(121), fetch_feed=fetch)
    assert queue.card("g1:live") is None  # two minutes on, it came down

    def stale_answer(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        # An answer read now that arrived long ago and is no longer current, with a state the board hasn't shown.
        return LiveBaseballFeed(state=_state(away=9), received=_seconds(0.5), current_for=timedelta(seconds=1))

    res = pipe.refresh([_game("g1")], at=_seconds(122), fetch_feed=stale_answer)
    assert res.ingested == () and queue.card("g1:live") is None and res.stale == (_id("g1"),)
