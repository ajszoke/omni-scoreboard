"""NBA's reads into TV-safe cards: the pregame card with its records and ranks, the live card from the newest state to
clear the delay with its panel's captions staged and confirmed, the slate alone without the players' lines, staleness
and a catch-up, the final with its best players and the night's result in its records, and what the policy reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from omni.app.nba_feeds import NbaReading, SeasonResults
from omni.app.nba_pipeline import LiveBasketballPipeline, NbaCardPolicy
from omni.app.pipeline import MeterReading, PipelineResult
from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.basketball import (
    BasketballGameCardPayload,
    BasketballPregameCardPayload,
    CaptionPanel,
    LineScorePanel,
    PlayerLinesPanel,
    TeamBoxPanel,
)
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, SeasonStage
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinLossRecord, WinProbability
from omni.domain.basketball import (
    FavoriteLine,
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    GameClock,
    PlayerLine,
    StandingRanks,
)
from omni.domain.basketball_reads import LinesRead, PlayerKey, RosterLine, SlateRead
from omni.domain.breaks import BreakKind
from omni.domain.contest import TeamGame
from omni.events.basketball import FieldGoalMade, ShotValue, TimeoutTaken
from omni.policy.leverage import ModelStatus
from omni.preferences.favorites import Favorites
from omni.providers.balldontlie import BALLDONTLIE_SOURCE, CupStage, FeedStatus, Pregame, SlateGame
from omni.providers.nba_standings import GameResult
from omni.providers.nba_teams import NbaTeamRegistry
from omni.queue.delay_policy import DelayPolicy
from omni.queue.scheduler import InterleavedCardQueue
from omni.tower.interruption import Interruption, InterruptionReason
from tests.basketball_fixtures import player

S = timedelta(seconds=1)
A, H = HomeAway.AWAY, HomeAway.HOME
T0 = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
REGISTRY = NbaTeamRegistry.default()
LAG = DurationSeconds(30)
LIVE, FINAL = BasketballPhase.LIVE, BasketballPhase.FINAL


def _game(game_id: int = 1, away: str = "UTA", home: str = "LAL", **changes: Any) -> TeamGame:
    fields: dict[str, Any] = {
        "id": LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(game_id)),
        "league": League.NBA,
        "status": GameStatus.LIVE,
        "scheduled_start": T0,
        "away": REGISTRY.resolve(away),
        "home": REGISTRY.resolve(home),
        "stage": SeasonStage.REGULAR,
    }
    fields.update(changes)
    return TeamGame(**fields)


def _state(
    phase: BasketballPhase = LIVE,
    period: int = 1,
    clock: int | None = 6000,
    scores: tuple[int, int] = (10, 8),
    timeouts: tuple[int, int] = (7, 7),
) -> BasketballGameState:
    periods = (scores[0],) if period == 1 else (scores[0] - 4 * (period - 1),) + (1,) * (period - 1)
    home_periods = (scores[1],) if period == 1 else (scores[1] - 4 * (period - 1),) + (1,) * (period - 1)
    return BasketballGameState(
        phase=phase,
        period=period,
        away=BasketballSide(score=scores[0], periods=periods, timeouts=timeouts[0]),
        home=BasketballSide(score=scores[1], periods=home_periods, timeouts=timeouts[1]),
        clock=GameClock(clock) if clock is not None else None,
    )


def _line(name: str, fgm: int, plus_minus: int = 0) -> PlayerLine:
    return player(name, plus_minus, fgm, fgm + 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0)


@dataclass(slots=True)
class _Feeds:
    """NBA's feeds as a fake: each game's slate row, its reads waiting, its newest, the season, and the lines' state."""

    clock: FakeClock
    lines: bool = True
    rows: dict[LeagueScopedId, SlateGame] = field(default_factory=dict)
    waiting: dict[LeagueScopedId, list[NbaReading]] = field(default_factory=dict)
    newest: dict[LeagueScopedId, NbaReading] = field(default_factory=dict)
    results: SeasonResults | None = None
    odds: FavoriteLine | None = None
    market: list[MeterReading] = field(default_factory=list)  # the market's readings, as they arrived

    def take(self, game: TeamGame) -> list[NbaReading]:
        return self.waiting.pop(game.id, [])

    def latest(self, game: TeamGame) -> NbaReading | None:
        return self.newest.get(game.id)

    def slate_game(self, game: TeamGame) -> SlateGame | None:
        return self.rows.get(game.id)

    def season(self) -> SeasonResults | None:
        return self.results

    def line(self, game: TeamGame) -> FavoriteLine | None:
        return self.odds

    def win_probability(self, game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        arrived = [reading for reading in self.market if reading.received.monotonic <= as_of.monotonic]
        return arrived[-1] if arrived else None


class _Board:
    """The pipeline over the fake feeds, a queue and a fake clock; reads pushed as they'd arrive."""

    def __init__(self, *, lines: bool = True, favorites: Favorites = Favorites(), lag: DurationSeconds = LAG) -> None:
        self.clock = FakeClock(T0)
        self.feeds = _Feeds(self.clock, lines=lines)
        self.queue = InterleavedCardQueue()
        self.pipeline = LiveBasketballPipeline(
            feeds=self.feeds, delay_policy=DelayPolicy(broadcast_lag=lag), queue=self.queue, favorites=favorites
        )
        self.lines: dict[PlayerKey, RosterLine] = {}
        self.current_for = timedelta(minutes=10)  # how long each read counts as current
        self.changes: list[tuple[float, str]] = []  # each change of the panel on the board, as `run` saw it

    def slate(self, state: BasketballGameState, game: TeamGame | None = None, **changes: Any) -> SlateRead:
        game = game if game is not None else _game()
        read = SlateRead(at=self.clock.monotonic(), state=state)
        status = FeedStatus.FINAL if state.phase is FINAL else FeedStatus.IN_PROGRESS
        fields: dict[str, Any] = {"game": game, "status": status, "phase": state.phase, "read": read}
        fields.update(changes)
        self.feeds.rows[game.id] = SlateGame(**fields)
        self._push(game, read)
        return read

    def players(self, *lines: tuple[int, HomeAway, PlayerLine], game: TeamGame | None = None, period: int = 1) -> None:
        game = game if game is not None else _game()
        for key, side, line in lines:
            self.lines[PlayerKey(key)] = RosterLine(key=PlayerKey(key), side=side, line=line)
        read = LinesRead(
            at=self.clock.monotonic(), period=period, clock=GameClock(5900), lines=tuple(self.lines.values())
        )
        self._push(game, read)

    def _push(self, game: TeamGame, read: SlateRead | LinesRead) -> None:
        reading = NbaReading(read, self.clock.read(), self.current_for)
        self.feeds.waiting.setdefault(game.id, []).append(reading)
        self.feeds.newest[game.id] = reading

    def refresh(self, by: timedelta = timedelta(0), *games: TeamGame) -> PipelineResult:
        self.clock.advance(by)
        return self.pipeline.refresh(games or (_game(),), at=self.clock.read())

    def card(self, kind: str = "live", game: TeamGame | None = None) -> ScoreboardCard[Any] | None:
        game = game if game is not None else _game()
        return self.queue.card(f"{game.id}:{kind}")

    def show(self, occurrence: int = 1, kind: str = "live", game: TeamGame | None = None) -> None:
        card = self.card(kind, game)
        assert card is not None
        self.pipeline.presented(card, self.clock.read(), occurrence=occurrence)

    def bottom(self) -> Any:
        card = self.card()
        assert card is not None
        return card.payload.bottom

    def run(self, seconds: int, *, occurrence: int = 1) -> list[tuple[float, str]]:
        """The card on the board for ``seconds``: a refresh and a confirmed frame each second; every change of its panel
        so far, as when (seconds since T0) and the panel's kind."""
        for _ in range(seconds):
            self.refresh(S)
            self.show(occurrence)
            word = type(self.bottom()).__name__
            if not self.changes or self.changes[-1][1] != word:
                self.changes.append(((self.clock.now() - T0).total_seconds(), word))
        return self.changes


def _season(*results: GameResult, at: ClockReading) -> SeasonResults:
    return SeasonResults(results=results, arrived=at)


def _result(game_id: int, away: str, home: str, winner: HomeAway) -> GameResult:
    return GameResult(
        game=LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(game_id)),
        away=REGISTRY.resolve(away),
        home=REGISTRY.resolve(home),
        winner=winner,
    )


# --- upcoming ----------------------------------------------------------------------------------------------------


def test_an_upcoming_game_has_a_pregame_card_with_its_records_and_ranks_once_the_season_is_read() -> None:
    board = _Board(favorites=Favorites({League.NBA: ["LAL"]}))
    game = _game(status=GameStatus.SCHEDULED)
    board.feeds.rows[game.id] = SlateGame(
        game=game,
        status=FeedStatus.SCHEDULED,
        phase=BasketballPhase.SCHEDULED,
        pregame=Pregame(phase=BasketballPhase.SCHEDULED, tip=T0),
    )
    result = board.refresh(timedelta(0), game)
    card = board.card("pregame", game)
    assert (
        card is not None
        and card.kind is CardKind.PREGAME
        and [each.raw for each in result.pregames] == ["nba:balldontlie:1:pregame"]
    )
    payload: BasketballPregameCardPayload = card.payload
    assert payload.tip == T0 and payload.records.away is None and payload.ranks.home is None
    assert card.priority.band is DisplayPriority.FAVORITE and card.priority.reasons == ("favorite #1 LAL",)
    assert board.refresh(S, game).pregames == ()  # nothing changed
    board.feeds.results = _season(_result(9, "UTA", "LAL", A), at=board.clock.read())
    board.refresh(S, game)
    payload = board.card("pregame", game).payload  # type: ignore[union-attr]
    assert payload.records.away == WinLossRecord(wins=1, losses=0)
    assert payload.ranks.home == StandingRanks(conference=15, division=5)
    board.feeds.results = _season(at=ClockReading(T0, MonotonicTime(0.0)))  # an older read changes nothing
    board.refresh(S, game)
    assert board.card("pregame", game).payload.records.away == WinLossRecord(wins=1, losses=0)  # type: ignore[union-attr]


def test_a_postponed_game_reads_ppd_and_a_game_not_yet_read_or_called_off_has_no_card() -> None:
    board = _Board()
    postponed, unread, off = _game(1, status=GameStatus.POSTPONED), _game(2, "BOS", "NYK"), _game(3, "DEN", "PHX")
    board.feeds.rows[postponed.id] = SlateGame(
        game=postponed,
        status=FeedStatus.POSTPONED,
        phase=BasketballPhase.POSTPONED,
        pregame=Pregame(phase=BasketballPhase.POSTPONED, tip=None),
    )
    board.feeds.rows[off.id] = SlateGame(game=off, status=FeedStatus.CANCELED, phase=None)
    board.refresh(timedelta(0), postponed, unread, off)
    card = board.card("pregame", postponed)
    assert card is not None and card.payload.phase is BasketballPhase.POSTPONED and card.payload.tip is None
    assert (
        board.card("pregame", unread) is None and board.card("pregame", off) is None and board.card("live", off) is None
    )


def test_the_pregame_card_goes_when_the_game_is_under_way_and_the_live_card_follows_the_delay() -> None:
    board = _Board()
    game = _game(status=GameStatus.SCHEDULED)
    board.feeds.rows[game.id] = SlateGame(
        game=game,
        status=FeedStatus.SCHEDULED,
        phase=BasketballPhase.SCHEDULED,
        pregame=Pregame(phase=BasketballPhase.SCHEDULED, tip=T0),
    )
    board.refresh(timedelta(0), game)
    live = _game()
    board.slate(_state())
    held = board.refresh(S, live)
    assert board.card("pregame") is None and board.card() is None
    assert [str(each) for each in held.held] == ["nba:balldontlie:1"] and held.releases[0][1] == T0 + 30 * S
    assert board.pipeline.next_deadline(board.clock.read()) == MonotonicTime(30.0)
    shown = board.refresh(30 * S, live)
    card = board.card()
    assert (
        card is not None
        and card.kind is CardKind.LIVE_GAME
        and [each.raw for each in shown.ingested] == ["nba:balldontlie:1:live"]
    )
    assert isinstance(card.payload.bottom, LineScorePanel) and card.payload.records.away is None
    assert card.priority.band is DisplayPriority.NORMAL


# --- live: the panel ----------------------------------------------------------------------------------------------


def _under_way(board: _Board) -> None:
    """A game under way, its first slate read and baseline lines through the delay, the card up on the board."""
    board.slate(_state())
    board.players((1, A, _line("A.Shooter", 0)), (2, H, _line("H.Home", 0)))
    board.refresh(31 * S)
    board.show()


def test_a_basket_from_the_lines_clears_the_delay_and_holds_the_panel_for_a_turn_once_a_frame_carries_it() -> None:
    board = _Board()
    _under_way(board)  # up at 31 s
    board.players((1, A, _line("A.Shooter", 1)))  # a basket, read at 31 s, waiting a read for its assist
    board.run(5)
    board.players()  # no assist: told by this read, at 36 s, which clears the delay at 66 s
    board.slate(_state(scores=(12, 8)))  # the slate beside it, read at 36 s too
    board.run(31)
    caption = board.bottom()
    assert board.run(19) == [
        (32, "LineScorePanel"),
        (43, "TeamBoxPanel"),
        (55, "PlayerLinesPanel"),
        (67, "CaptionPanel"),  # seen waiting at 66 s, in the players' second half: at the next
        (79, "LineScorePanel"),
    ]
    assert isinstance(caption, CaptionPanel) and caption.place is None  # alone: no marker
    assert caption.caption == FieldGoalMade(side=A, shooter="A.Shooter", value=ShotValue.TWO)


def test_on_the_slate_alone_the_panel_has_no_captions_and_shows_the_line_score() -> None:
    board = _Board(lines=False)
    _under_way(board)
    board.players((1, A, _line("A.Shooter", 1)))
    board.slate(_state(clock=5800))
    board.run(2)
    board.slate(_state(clock=5800, scores=(12, 8), timeouts=(6, 7)))  # a timeout, told from the slate alone
    assert board.run(90) == [(32, "LineScorePanel")]  # no caption, no stat panel


def test_a_caption_no_frame_carried_in_time_is_dropped_and_said_so() -> None:
    board = _Board()
    _under_way(board)
    board.pipeline.presented(_other_card(), board.clock.read(), occurrence=2)  # another card is up
    board.players((1, A, _line("A.Shooter", 1)))
    board.refresh(S)
    board.players()  # the basket is told
    board.slate(_state(scores=(12, 8)))
    board.refresh(30 * S)
    skipped = board.refresh(60 * S).skipped
    assert skipped == ("nba:balldontlie:1: caption 1 (FieldGoalMade) unseen (too late to start)",)


def test_a_timeout_s_caption_holds_its_turn_and_the_stat_panels_rotate_while_it_lasts() -> None:
    board = _Board()
    _under_way(board)
    board.slate(_state(clock=5800))
    board.run(2)
    board.slate(_state(clock=5700))  # the clock ran
    board.run(2)
    board.slate(_state(clock=5700, timeouts=(7, 6)))  # stopped, and LAL's count dropped: read at 35 s
    board.run(32)
    timeout = board.bottom()
    assert board.run(58) == [
        (32, "LineScorePanel"),
        (43, "TeamBoxPanel"),
        (55, "PlayerLinesPanel"),
        (67, "CaptionPanel"),  # seen waiting at 65 s: at the next half boundary
        (79, "TeamBoxPanel"),  # the timeout lasts: the stat panels rotate, no line score
        (91, "PlayerLinesPanel"),
        (103, "TeamBoxPanel"),
        (115, "PlayerLinesPanel"),
    ]
    assert isinstance(timeout, CaptionPanel) and timeout.caption == TimeoutTaken(side=H, left=6)
    assert board.card().payload.state.timeout is H  # type: ignore[union-attr]  # the single board's TO
    board.slate(_state(clock=5600, timeouts=(7, 6)))  # the clock runs again: the timeout is over
    board.run(31)
    assert board.pipeline._live[_game().id].open == set()
    assert board.card().payload.state.timeout is None  # type: ignore[union-attr]


def _other_card() -> ScoreboardCard[Any]:
    """Another league's card, as a frame of it would be confirmed."""
    from tests.basketball_fixtures import game_card, live, side

    return game_card(
        live(side(50, 4, False, (25, 25)), side(48, 4, False, (24, 24)), 3, 3000, home_win=None), away="POR"
    )


# --- staleness and a catch-up ------------------------------------------------------------------------------------


def test_a_quiet_feed_s_card_is_drawn_stale_then_comes_down_and_a_catch_up_tells_nothing() -> None:
    board = _Board()
    board.current_for = 40 * S
    _under_way(board)
    result = board.refresh(10 * S)  # the newest read was current for 40 s
    assert [str(each) for each in result.stale] == ["nba:balldontlie:1"]
    assert result.warnings == ("nba:balldontlie:1: stale: its feed's newest read is out of date",)
    card = board.card()
    assert card is not None and card.stale_since is not None
    board.refresh(60 * S)
    board.players((1, A, _line("A.Shooter", 3)))  # a catch-up: two baskets while it was out
    caught = board.refresh(S)
    assert caught.skipped == ("nba:balldontlie:1: 3 from an outage's catch-up set aside",)
    board.refresh(60 * S)
    assert board.card() is None  # stale for two minutes: down
    board.slate(_state(clock=5000))
    board.refresh(S)
    board.refresh(30 * S)
    card = board.card()
    assert card is not None and card.stale_since is None  # a state read since cleared the delay


def test_a_game_with_no_read_at_all_is_stale_at_once() -> None:
    board = _Board()
    game = _game()
    board.feeds.rows[game.id] = SlateGame(
        game=game, status=FeedStatus.IN_PROGRESS, phase=LIVE, read=SlateRead(at=MonotonicTime(0.0), state=_state())
    )
    result = board.refresh(S)
    assert [str(each) for each in result.stale] == ["nba:balldontlie:1"] and result.skipped == (
        "nba:balldontlie:1: no read yet",
    )


# --- the final --------------------------------------------------------------------------------------------------


def test_a_final_reveals_after_the_delay_with_the_night_s_result_and_each_side_s_best_player() -> None:
    board = _Board()
    board.feeds.results = _season(_result(9, "UTA", "LAL", H), at=board.clock.read())
    _under_way(board)
    board.players((1, A, _line("A.Shooter", 5, plus_minus=8)), (3, A, _line("A.Bench", 1, plus_minus=-2)))
    final = _state(FINAL, period=4, clock=None, scores=(101, 99))
    board.slate(final)
    row = board.feeds.rows[_game().id]
    held = board.refresh(S)
    assert [str(each) for each in held.held] == ["nba:balldontlie:1"]
    revealed = board.refresh(30 * S)
    card = board.card("final")
    assert card is not None and [each.raw for each in revealed.finals] == ["nba:balldontlie:1:final"]
    assert board.card() is None and card.replaces is not None and card.replaces.raw == "nba:balldontlie:1:live"
    payload: BasketballGameCardPayload = card.payload
    assert payload.records.away == WinLossRecord(wins=1, losses=1)  # the night's win counted at the reveal
    assert payload.best is not None and payload.best.players.away is not None
    assert payload.best.players.away.name == "A.Shooter" and isinstance(payload.bottom, LineScorePanel)
    assert row.result is not None and revealed.ends[0][1].at == T0 + 31 * S
    board.players((4, H, _line("H.Star", 9, plus_minus=12)))  # the drain: later lines name the home side's best
    board.slate(_state(FINAL, period=4, clock=None, scores=(101, 100)))  # and a corrected score
    board.refresh(S)
    corrected = board.refresh(30 * S)
    payload = board.card("final").payload  # type: ignore[union-attr]
    assert [each.raw for each in corrected.corrections] == ["nba:balldontlie:1:final"]
    best = payload.best.players.home if payload.best is not None else None
    assert payload.state.home.score == 100 and best is not None and best.name == "H.Star"
    assert board.refresh(S).corrections == ()
    board.queue.remove("nba:balldontlie:1:final")  # its window is over
    board.slate(_state(FINAL, period=4, clock=None, scores=(102, 100)))
    assert board.refresh(31 * S).corrections == ()


def test_a_final_without_a_slate_row_or_lines_folds_nothing_and_names_no_best_players() -> None:
    board = _Board()
    board.slate(_state(FINAL, period=4, clock=None, scores=(101, 99)))
    board.refresh(S)
    board.feeds.rows.clear()
    board.refresh(30 * S, _game())
    assert board.card("final") is None  # a game the slate no longer lists isn't carded


def test_a_final_s_game_leaving_the_slate_takes_its_card_down() -> None:
    board = _Board()
    board.slate(_state(FINAL, period=4, clock=None, scores=(101, 99)))
    board.refresh(31 * S)
    assert board.card("final") is not None and board.card("final").payload.best is None  # type: ignore[union-attr]
    board.pipeline.refresh((), at=board.clock.read())
    assert board.card("final") is None


# --- what the policy reads, and a host's gap ---------------------------------------------------------------------


def test_the_policy_reads_each_live_game_s_proxy_leverage_freshness_records_and_cup_knockout() -> None:
    board = _Board()
    board.feeds.results = _season(_result(9, "UTA", "LAL", H), at=board.clock.read())
    board.slate(_state(), cup=CupStage.SEMIFINAL)
    result = board.refresh(31 * S)
    facts = result.policy.facts(_game().id)
    assert facts is not None and facts.fresh and facts.cup_knockout
    assert facts.leverage.status is ModelStatus.PROXY and facts.records is not None
    assert facts.records.away.wins == 0 and facts.records.home.wins == 1


def test_the_leverage_reads_breaks_and_a_period_not_yet_ticking() -> None:
    from omni.app.nba_pipeline import _leverage

    end = _leverage(_state(BasketballPhase.END_OF_PERIOD, period=1, clock=None))
    tip = _leverage(_state(clock=None))
    overtime = _leverage(_state(period=5, clock=None, scores=(110, 110)))
    final = _leverage(_state(FINAL, period=4, clock=None, scores=(100, 90)))
    assert end.status is tip.status is overtime.status is ModelStatus.PROXY
    assert final.status is ModelStatus.UNKNOWN


def test_a_host_s_gap_draws_the_card_stale_until_a_state_read_after_it_clears() -> None:
    board = _Board()
    _under_way(board)
    gap = Interruption(
        serial=1, reason=InterruptionReason.HOST_SUSPEND, before=board.clock.read(), after=board.clock.read()
    )
    board.pipeline.interrupted(gap)
    board.slate(_state(clock=5800))
    board.refresh(S)
    card = board.card()
    assert card is not None and card.stale_since is not None
    board.refresh(30 * S)
    card = board.card()
    assert card is not None and card.stale_since is None


def test_halftime_counts_down_fifteen_minutes_from_its_first_read_on_the_viewer_s_clock() -> None:
    board = _Board()
    board.slate(_state(BasketballPhase.HALFTIME, period=2, clock=None, scores=(50, 48)))
    board.refresh(31 * S)
    payload = board.card().payload  # type: ignore[union-attr]
    assert payload.break_ends_at == T0 + timedelta(minutes=15) + 30 * S
    board.slate(_state(period=3, clock=7000, scores=(50, 48)))
    board.refresh(31 * S)
    assert board.card().payload.break_ends_at is None  # type: ignore[union-attr]


def test_the_pipeline_wakes_for_whatever_changes_next_and_never_for_nothing() -> None:
    board = _Board()
    assert board.pipeline.next_deadline(board.clock.read()) is None
    _under_way(board)
    assert board.pipeline.next_deadline(board.clock.read()) is not None
    assert NbaCardPolicy().halftime == timedelta(minutes=15)


def test_a_season_stage_postseason_final_counts_nothing() -> None:
    board = _Board()
    board.feeds.results = _season(at=board.clock.read())
    game = _game(stage=SeasonStage.POSTSEASON)
    board.slate(_state(FINAL, period=4, clock=None, scores=(101, 99)), game=game)
    board.refresh(31 * S, game)
    card = board.card("final", game)
    assert card is not None and card.payload.records.away == WinLossRecord(wins=0, losses=0)


def test_reads_around_a_final_hold_its_reveal_and_a_live_read_after_it_changes_nothing() -> None:
    board = _Board()
    _under_way(board)
    board.slate(_state(FINAL, period=4, clock=None, scores=(101, 99)))
    board.slate(_state(period=4, clock=10, scores=(99, 99)))  # a live read behind the final's
    board.refresh(S)
    board.slate(_state(FINAL, period=4, clock=None, scores=(101, 98)))  # a second final read, before the reveal
    assert [str(each) for each in board.refresh(S).held] == ["nba:balldontlie:1"]
    assert board.pipeline.next_deadline(board.clock.read()) is not None  # its reveal, its panel's next half
    board.refresh(29 * S)
    card = board.card("final")
    assert card is not None and card.payload.state.home.score == 98  # the newest final read to have cleared
    board.players((5, H, _line("H.Late", 2)))
    board.refresh(timedelta(0))
    assert board.pipeline.next_deadline(board.clock.read()) == board.clock.monotonic() + 30 * S  # those lines
    board.slate(_state(period=4, clock=5, scores=(99, 99)))  # the drain: a live read changes nothing
    board.refresh(31 * S)
    assert board.card("final").payload.state.home.score == 98  # type: ignore[union-attr]


def test_halftime_s_countdown_runs_from_its_first_read_however_many_follow() -> None:
    board = _Board()
    board.slate(_state(BasketballPhase.HALFTIME, period=2, clock=None, scores=(50, 48)))
    board.refresh(10 * S)
    board.slate(_state(BasketballPhase.HALFTIME, period=2, clock=None, scores=(50, 48)))
    board.refresh(21 * S)
    board.refresh(10 * S)
    assert board.card().payload.break_ends_at == T0 + timedelta(minutes=15) + 30 * S  # type: ignore[union-attr]


def test_a_stale_game_that_leaves_the_board_is_forgotten() -> None:
    board = _Board()
    board.current_for = 5 * S
    _under_way(board)
    assert board.refresh(S).stale
    board.feeds.rows.clear()
    result = board.pipeline.refresh((), at=board.clock.read())
    assert result.stale == () and [str(each) for each in result.removed] == ["nba:balldontlie:1"]


def test_the_policy_reads_a_timeout_s_break_once_its_read_clears_the_delay_and_a_catch_up_s_lends_nothing() -> None:
    board = _Board()
    _under_way(board)
    board.slate(_state(clock=5800))
    board.run(2)
    board.slate(_state(clock=5700))
    board.run(2)
    board.slate(_state(clock=5700, timeouts=(7, 6)))  # LAL's first timeout of the quarter, read at 35 s
    assert board.refresh(29 * S).policy.facts(_game().id).breaks == ()  # type: ignore[union-attr]
    facts = board.refresh(S).policy.facts(_game().id)
    assert facts is not None
    (timeout,) = facts.breaks
    assert timeout.kind is BreakKind.NBA_TV_TIMEOUT and timeout.released == MonotonicTime(65.0)
    caught = _Board()
    caught.current_for = 5 * S
    _under_way(caught)
    caught.refresh(10 * S)  # stale: what comes next is a catch-up
    caught.slate(_state(clock=5700, timeouts=(7, 6)))
    caught.refresh(31 * S)
    facts = caught.refresh(S).policy.facts(_game().id)
    assert facts is None or facts.breaks == ()


def test_the_pregame_card_carries_the_favorite_s_line_when_the_sportsbook_has_one() -> None:
    board = _Board()
    board.feeds.odds = FavoriteLine(favorite=H, moneyline=-260, spread=-6.5)
    game = _game(status=GameStatus.SCHEDULED)
    board.feeds.rows[game.id] = SlateGame(
        game=game,
        status=FeedStatus.SCHEDULED,
        phase=BasketballPhase.SCHEDULED,
        pregame=Pregame(phase=BasketballPhase.SCHEDULED, tip=T0),
    )
    board.refresh(timedelta(0), game)
    card = board.card("pregame", game)
    assert card is not None and card.payload.line == board.feeds.odds


def test_the_meter_stands_beside_a_state_only_with_a_reading_no_newer_than_it_and_ages_out() -> None:
    board = _Board()
    early = MeterReading(WinProbability(home=60.0, away=40.0), board.clock.read())
    board.feeds.market.append(early)
    board.slate(_state())
    board.clock.advance(5 * S)
    board.feeds.market.append(MeterReading(WinProbability(home=70.0, away=30.0), board.clock.read()))  # after it
    board.refresh(26 * S)
    card = board.card()
    assert card is not None and card.payload.win_probability == early.value  # not the later one
    older = ClockReading(T0 - S, MonotonicTime(-1.0))
    board.feeds.market.append(MeterReading(WinProbability(home=55.0, away=45.0), older))  # handed on late
    board.refresh(S)
    assert board.card().payload.win_probability == early.value  # type: ignore[union-attr]  # an older one doesn't replace it
    board.refresh(4 * 60 * S)  # past the delay and three minutes since it arrived
    assert board.card().payload.win_probability is None  # type: ignore[union-attr]
