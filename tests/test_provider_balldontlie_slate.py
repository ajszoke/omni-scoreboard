"""BALLDONTLIE's slate: each status and time form, scores by period through overtime, timeouts and bonus, what a
game not yet under way shows, and what each fetch comes to."""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from omni.core.clock import MonotonicTime
from omni.core.enum import GameStatus, HomeAway, SeasonStage
from omni.domain.basketball import BasketballGameState, BasketballPhase, BasketballSide, GameClock
from omni.domain.basketball_reads import SlateRead
from omni.providers.balldontlie import (
    BALLDONTLIE_SOURCE,
    BalldontlieHttp,
    BalldontlieProvider,
    BdlAnswer,
    CupStage,
    Exchange,
    FeedStatus,
    FetchOutcome,
    Poll,
    Pregame,
    RateLimit,
    RowProblem,
    SlateGame,
    SlatePoll,
    Unreadable,
)
from omni.providers.nba_teams import NbaTeamRegistry
from tests.balldontlie_feed import GAME, RATE, ScriptedFetch, final_row, ok, scheduled_row, slate_row, team

DAY = date(2026, 11, 18)
TIP = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)


def _provider(*answers: BdlAnswer | Exception) -> tuple[BalldontlieProvider, ScriptedFetch]:
    fetch = ScriptedFetch(list(answers))
    return BalldontlieProvider(NbaTeamRegistry.default(), fetch=fetch, clock=fetch.clock), fetch


def _poll(*rows: Any) -> SlatePoll:
    provider, _ = _provider(ok(*rows))
    return provider.poll_slate(DAY)


def _game(row: dict[str, Any]) -> SlateGame:
    poll = _poll(row)
    assert poll.unreadable == (), poll.unreadable
    (game,) = poll.games
    return game


def _state(row: dict[str, Any]) -> BasketballGameState:
    read = _game(row).read
    assert read is not None
    return read.state


def _unreadable(row: Any) -> Unreadable:
    poll = _poll(row)
    assert poll.games == ()
    (unreadable,) = poll.unreadable
    return unreadable


# --- the slate's rows ----------------------------------------------------------------------------------------------


def test_a_game_under_way_is_read_with_its_clubs_tip_and_state() -> None:
    game = _game(slate_row())
    contest = game.game
    assert contest.id.raw == str(GAME) and contest.id.source == BALLDONTLIE_SOURCE
    assert (contest.away.abbreviation, contest.home.abbreviation) == ("UTA", "LAL")
    assert contest.scheduled_start == TIP and contest.provider_date == DAY
    assert (contest.status, contest.stage) == (GameStatus.LIVE, SeasonStage.REGULAR)
    assert (game.status, game.phase, game.pregame, game.cup) == (
        FeedStatus.IN_PROGRESS,
        BasketballPhase.LIVE,
        None,
        None,
    )
    assert game.read is not None and game.read.state == BasketballGameState(
        phase=BasketballPhase.LIVE,
        period=3,
        away=BasketballSide(score=70, periods=(29, 27, 14), timeouts=4, in_bonus=False),
        home=BasketballSide(score=66, periods=(34, 20, 12), timeouts=5, in_bonus=True),
        clock=GameClock.of(6, 7),
    )


@pytest.mark.parametrize(
    ("time", "phase", "clock"),
    [
        ("Q3 6:07", BasketballPhase.LIVE, GameClock.of(6, 7)),
        ("Q3 12:00", BasketballPhase.LIVE, GameClock.of(12, 0)),
        ("Q3 :48.2", BasketballPhase.LIVE, GameClock.of(0, 48, 2)),
        ("Q3 :00.0", BasketballPhase.LIVE, GameClock(0)),
        ("END Q3", BasketballPhase.END_OF_PERIOD, None),
        ("Final", BasketballPhase.END_OF_PERIOD, None),  # the clock's out; the status hasn't turned final yet
        (None, BasketballPhase.LIVE, None),
        ("2026-11-19T03:00:00Z", BasketballPhase.LIVE, None),  # the tip's instant: no clock yet
    ],
)
def test_each_time_form_of_a_game_under_way(time: Any, phase: BasketballPhase, clock: GameClock | None) -> None:
    state = _state(slate_row(time=time))
    assert (state.phase, state.period, state.clock) == (phase, 3, clock)


def test_halftime_is_read_from_the_time_or_the_status() -> None:
    for row in (slate_row(period=2, time="Half", status="2nd Qtr"), slate_row(period=2, time=None, status="Halftime")):
        state = _state(row)
        assert (state.phase, state.period, state.clock) == (BasketballPhase.HALFTIME, 2, None)
        assert state.away.periods == (29, 27)


@pytest.mark.parametrize(
    ("period", "time"), [(5, "OT1 2:00"), (5, "OT 2:00"), (6, "2OT 1:30"), (6, "Q6 :09.5"), (8, "END 4OT")]
)
def test_an_overtime_clock_reads_whatever_its_label(period: int, time: str) -> None:
    state = _state(slate_row(period=period, time=time))
    assert state.period == period and state.is_overtime


@pytest.mark.parametrize("time", ["Q2 garbage", "Q3 13:00", "Q3 6:75", "halfway", ""])
def test_a_time_in_no_known_form_is_unreadable(time: str) -> None:
    unreadable = _unreadable(slate_row(time=time))
    assert unreadable.problem is RowProblem.UNKNOWN_TIME and unreadable.game is not None
    assert repr(time) in unreadable.detail


def test_a_time_that_isnt_text_is_malformed() -> None:
    assert _unreadable(slate_row(time=367)).problem is RowProblem.MALFORMED


def test_before_the_tip_a_game_shows_its_tip() -> None:
    game = _game(scheduled_row())
    assert (game.phase, game.read, game.status) == (BasketballPhase.SCHEDULED, None, FeedStatus.SCHEDULED)
    assert game.pregame == Pregame(phase=BasketballPhase.SCHEDULED, tip=TIP)
    assert game.game.status is GameStatus.SCHEDULED


def test_a_postponed_game_shows_no_tip() -> None:
    for row in (scheduled_row(state="postponed", postponed=True), scheduled_row(postponed=True)):
        game = _game(row)
        assert game.pregame == Pregame(phase=BasketballPhase.POSTPONED, tip=None)
        assert game.read is None


def test_a_delayed_game_waits_for_its_tip_then_reads_as_under_way() -> None:
    waiting = _game(scheduled_row(state="delayed"))
    assert waiting.pregame == Pregame(phase=BasketballPhase.SCHEDULED, tip=TIP)
    assert waiting.game.status is GameStatus.DELAYED
    under_way = _game(slate_row(state="delayed", period=1, time="Q1 4:00"))
    assert under_way.phase is BasketballPhase.LIVE and under_way.game.status is GameStatus.DELAYED


@pytest.mark.parametrize(
    ("state", "status"),
    [("canceled", GameStatus.CANCELED), ("abandoned", GameStatus.CANCELED), ("suspended", GameStatus.SUSPENDED)],
)
def test_a_called_off_game_has_no_phase(state: str, status: GameStatus) -> None:
    game = _game(slate_row(state=state))
    assert (game.phase, game.read, game.pregame, game.game.status) == (None, None, None, status)


@pytest.mark.parametrize("state", ["unknown", "warming_up", None])
def test_a_status_the_provider_doesnt_read_is_unreadable(state: Any) -> None:
    unreadable = _unreadable(slate_row(state=state))
    assert unreadable.problem is RowProblem.UNKNOWN_STATUS
    assert unreadable.line.startswith(f"unknown_status: game {GAME}: ")


def test_a_final_is_read_with_its_winner() -> None:
    state = _state(final_row())
    assert (state.phase, state.period, state.clock, state.winner) == (BasketballPhase.FINAL, 4, None, HomeAway.AWAY)
    assert (state.away.periods, state.home.periods) == ((29, 27, 22, 31), (34, 20, 22, 21))
    assert (state.away.timeouts, state.home.timeouts, state.away.in_bonus, state.home.in_bonus) == (2, 1, True, False)


def test_overtime_points_follow_the_quarters() -> None:
    state = _state(final_row(period=6, scores=(121, 119), away_overtime=(10, 12, None), home_overtime=(10, 10, None)))
    assert state.away.periods == (29, 27, 22, 31, 10, 12) and state.home.periods == (34, 20, 22, 21, 10, 10)
    assert state.period == 6 and state.winner is HomeAway.AWAY


def test_a_final_counts_the_overtimes_it_has_points_for() -> None:
    # Some old overtime finals read period 4; their overtime points say how many periods were played.
    state = _state(final_row(period=4, scores=(119, 107), away_overtime=(10, None, None), home_overtime=(10, 0, None)))
    assert state.period == 5 and len(state.away.periods) == 5


def test_a_fourth_overtime_has_no_column_so_the_line_score_stops_at_the_third() -> None:
    state = _state(
        final_row(
            period=8,
            scores=(150, 147),
            away_overtime=(10, 8, 6),
            home_overtime=(10, 8, 6),
        )
    )
    assert state.period == 8 and state.away.periods == (29, 27, 22, 31, 10, 8, 6)
    assert (state.away.score, state.home.score) == (150, 147)


def test_the_line_score_runs_to_the_period_under_way_and_stops_at_an_unfilled_column() -> None:
    assert _state(slate_row(period=2, time="Q2 1:00")).away.periods == (29, 27)
    state = _state(slate_row(period=5, time="OT1 4:00", away_periods=(29, 27, 22, 31), away_overtime=(None,) * 3))
    assert state.away.periods == (29, 27, 22, 31)


def test_timeouts_and_bonus_the_feed_leaves_null_are_unsaid() -> None:
    state = _state(slate_row(timeouts=(None, 0), bonus=(None, True)))
    assert (state.away.timeouts, state.home.timeouts) == (None, 0)
    assert (state.away.in_bonus, state.home.in_bonus) == (False, True)


def test_postseason_games_say_so() -> None:
    assert _game(slate_row(postseason=True)).game.stage is SeasonStage.POSTSEASON


@pytest.mark.parametrize(
    ("raw", "stage"),
    [
        (None, None),
        ("", None),
        ("Group Stage", CupStage.GROUP),
        ("East Quarterfinal", CupStage.QUARTERFINAL),
        ("Semifinals", CupStage.SEMIFINAL),
        ("Championship", CupStage.CHAMPIONSHIP),
        ("NBA Cup Final", CupStage.CHAMPIONSHIP),
        ("Knockout", CupStage.OTHER),
        (3, CupStage.OTHER),
    ],
)
def test_the_nba_cup_stage(raw: Any, stage: CupStage | None) -> None:
    assert CupStage.of(raw) is stage
    assert _game(slate_row(ist_stage=raw)).cup is stage


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        (slate_row(timeouts=(9, 5)), RowProblem.IMPOSSIBLE),
        (slate_row(period=3, time="Half"), RowProblem.IMPOSSIBLE),
        (slate_row(period=0, time=None), RowProblem.IMPOSSIBLE),
        (final_row(scores=(100, 100)), RowProblem.IMPOSSIBLE),
        (slate_row(home=team(29, "UTA")), RowProblem.IMPOSSIBLE),
        (slate_row(home=team(99, "SEA")), RowProblem.UNKNOWN_TEAM),
        (slate_row(home={"id": 14}), RowProblem.MALFORMED),
        (slate_row(home="LAL"), RowProblem.MALFORMED),
        (slate_row(datetime=None), RowProblem.MALFORMED),
        (slate_row(datetime="soon"), RowProblem.MALFORMED),
        (slate_row(datetime="2026-11-19T03:00:00"), RowProblem.MALFORMED),  # no offset
        (slate_row(date="tonight"), RowProblem.MALFORMED),
        (slate_row(period="3"), RowProblem.MALFORMED),
        (slate_row(period=True), RowProblem.MALFORMED),
        (slate_row(visitor_in_bonus="yes"), RowProblem.MALFORMED),
        (slate_row(home_q1="34"), RowProblem.MALFORMED),
    ],
)
def test_a_row_that_cant_be_read_says_why(row: dict[str, Any], problem: RowProblem) -> None:
    unreadable = _unreadable(row)
    assert unreadable.problem is problem and unreadable.game is not None and unreadable.game.raw == str(GAME)


def test_a_row_with_no_id_names_no_game() -> None:
    for row in (slate_row(id=None), "not a row"):
        unreadable = _unreadable(row)
        assert (unreadable.problem, unreadable.game) == (RowProblem.MALFORMED, None)
        assert unreadable.line.startswith("malformed: ") and "game" not in unreadable.line


def test_one_bad_row_leaves_the_rest_of_the_slate() -> None:
    poll = _poll(slate_row(), slate_row(game_id=7, state="unknown"), final_row(game_id=8))
    assert [game.game.id.raw for game in poll.games] == [str(GAME), "8"]
    assert poll.answered and not poll.complete and len(poll.unreadable) == 1


def test_the_date_may_come_with_a_time() -> None:
    assert _game(slate_row(date="2026-11-18T00:00:00.000Z")).game.provider_date == DAY


def test_a_read_is_stamped_on_arrival() -> None:
    provider, fetch = _provider(ok(slate_row()), ok(slate_row(time="Q3 5:59")))
    first, second = provider.poll_slate(DAY), provider.poll_slate(DAY)
    assert (first.at, second.at) == (MonotonicTime(1.0), MonotonicTime(2.0))
    reads = [poll.games[0].read for poll in (first, second)]
    assert [read.at for read in reads if read is not None] == [first.at, second.at]
    assert fetch.asked[0] == ("/v1/games", (("dates[]", "2026-11-18"), ("per_page", "100")))


# --- the value types -----------------------------------------------------------------------------------------------


def test_a_pregame_is_for_a_game_not_yet_under_way() -> None:
    with pytest.raises(ValueError, match="not yet under way"):
        Pregame(phase=BasketballPhase.LIVE, tip=TIP)
    with pytest.raises(ValueError, match="its tip"):
        Pregame(phase=BasketballPhase.SCHEDULED, tip=None)
    with pytest.raises(ValueError, match="its tip"):
        Pregame(phase=BasketballPhase.POSTPONED, tip=TIP)


def test_a_slate_game_reads_or_waits_by_its_phase() -> None:
    live, scheduled = _game(slate_row()), _game(scheduled_row())
    assert live.read is not None and scheduled.pregame is not None
    with pytest.raises(ValueError, match="its read"):
        SlateGame(game=live.game, status=live.status, phase=BasketballPhase.LIVE)
    with pytest.raises(ValueError, match="its pregame"):
        SlateGame(game=live.game, status=live.status, phase=None, pregame=scheduled.pregame)
    with pytest.raises(ValueError, match="read's phase"):
        SlateGame(game=live.game, status=live.status, phase=BasketballPhase.FINAL, read=live.read)
    postponed = Pregame(phase=BasketballPhase.POSTPONED, tip=None)
    with pytest.raises(ValueError, match="pregame's phase"):
        SlateGame(game=live.game, status=live.status, phase=BasketballPhase.SCHEDULED, pregame=postponed)
    halftime = SlateRead(at=live.read.at, state=_state(slate_row(period=2, time="Half")))
    assert SlateGame(game=live.game, status=live.status, phase=BasketballPhase.HALFTIME, read=halftime).read


# --- fetching ------------------------------------------------------------------------------------------------------


def test_the_rate_limit_window_comes_from_the_headers() -> None:
    assert RateLimit.of(RATE) == RateLimit(
        limit=60, remaining=41, resets_at=datetime.fromtimestamp(1795058002, timezone.utc)
    )
    assert RateLimit.of({}) is None
    assert RateLimit.of({**RATE, "x-ratelimit-remaining": "lots"}) is None
    assert RateLimit.of({**RATE, "x-ratelimit-reset": "9" * 30}) is None


def test_an_answer_carries_its_rate_limit_window() -> None:
    provider, _ = _provider(BdlAnswer(200, ok().body, {"X-RateLimit-Limit": "60", **RATE}))
    poll = provider.poll_slate(DAY)
    assert poll.answered and poll.complete and poll.games == ()
    assert poll.exchanges == (
        Exchange(path="/v1/games", outcome=FetchOutcome.ANSWERED, status=200, rate_limit=RateLimit.of(RATE)),
    )
    assert poll.rate_limit == RateLimit.of(RATE)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, FetchOutcome.UNAUTHORIZED),
        (403, FetchOutcome.UNAUTHORIZED),
        (429, FetchOutcome.RATE_LIMITED),
        (500, FetchOutcome.HTTP_ERROR),
        (404, FetchOutcome.HTTP_ERROR),
    ],
)
def test_an_error_status_is_an_outcome(status: int, outcome: FetchOutcome) -> None:
    provider, _ = _provider(BdlAnswer(status, '{"error": "Unauthorized"}', {}))
    poll = provider.poll_slate(DAY)
    assert (poll.outcome, poll.games, poll.detail, poll.rate_limit) == (outcome, (), f"HTTP {status}", None)
    assert not poll.answered and not poll.complete
    (exchange,) = poll.exchanges
    assert (exchange.status, exchange.retry_after) == (status, None)


def test_a_429_says_how_long_to_wait_when_it_says() -> None:
    for retry, expected in (("30", timedelta(seconds=30)), ("soon", None), (None, None)):
        headers = {**RATE, "x-ratelimit-remaining": "0"} | ({"retry-after": retry} if retry else {})
        provider, _ = _provider(BdlAnswer(429, "", headers))
        poll = provider.poll_slate(DAY)
        assert poll.outcome is FetchOutcome.RATE_LIMITED and poll.exchanges[0].retry_after == expected
        assert poll.rate_limit is not None and poll.rate_limit.remaining == 0


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (TimeoutError("timed out"), FetchOutcome.TIMEOUT),
        (URLError(TimeoutError("timed out")), FetchOutcome.TIMEOUT),
        (URLError("Name or service not known"), FetchOutcome.NO_ANSWER),
        (ConnectionRefusedError("refused"), FetchOutcome.NO_ANSWER),
        (RuntimeError("anything"), FetchOutcome.NO_ANSWER),
    ],
)
def test_no_answer_is_an_outcome_never_an_escape(failure: Exception, outcome: FetchOutcome) -> None:
    provider, _ = _provider(failure)
    poll = provider.poll_slate(DAY)
    assert (poll.outcome, poll.games) == (outcome, ())
    assert poll.detail == f"no answer ({type(failure).__name__})"  # the provider's words, never the failure's
    assert poll.exchanges[0].status is None


@pytest.mark.parametrize("text", ["<html>busy</html>", "[]", '{"data": {}}', '{"meta": {}}', ""])
def test_a_body_that_isnt_the_endpoints_json_is_malformed(text: str) -> None:
    provider, _ = _provider(BdlAnswer(200, text, RATE))
    poll = provider.poll_slate(DAY)
    assert (poll.outcome, poll.games) == (FetchOutcome.MALFORMED, ())
    assert poll.exchanges[0].rate_limit is not None and poll.rate_limit is not None


def test_pages_are_followed_by_their_cursor_and_joined_in_order() -> None:
    provider, fetch = _provider(ok(slate_row(game_id=1), cursor=48697916), ok(slate_row(game_id=2), cursor="abc"), ok())
    poll = provider.poll_slate(DAY)
    assert [game.game.id.raw for game in poll.games] == ["1", "2"]
    assert [query[-1] for _, query in fetch.asked] == [("per_page", "100"), ("cursor", "48697916"), ("cursor", "abc")]
    assert poll.at == MonotonicTime(3.0)


@pytest.mark.parametrize("meta", [{"next_cursor": ""}, {"next_cursor": True}, {"next_cursor": 1.5}, [], None])
def test_a_page_with_no_usable_cursor_is_the_last(meta: Any) -> None:
    provider, fetch = _provider(BdlAnswer(200, f'{{"data": [], "meta": {json.dumps(meta)}}}', RATE))
    assert provider.poll_slate(DAY).answered and len(fetch.asked) == 1


def test_a_poll_reads_every_page_or_none() -> None:
    provider, _ = _provider(ok(slate_row(), cursor=5), BdlAnswer(429, "", RATE))
    poll = provider.poll_slate(DAY)
    assert (poll.outcome, poll.games, len(poll.exchanges)) == (FetchOutcome.RATE_LIMITED, (), 2)
    assert poll.rate_limit == RateLimit.of(RATE)


def test_pages_that_never_end_are_malformed() -> None:
    provider, fetch = _provider(*[ok(cursor=n) for n in range(1, 40)])
    poll = provider.poll_slate(DAY)
    assert (poll.outcome, poll.games, len(fetch.asked)) == (FetchOutcome.MALFORMED, (), 30)
    assert poll.detail == "the pages didn't end in 30 requests"


def test_a_poll_with_no_window_has_no_rate_limit() -> None:
    assert Poll(at=MonotonicTime(0.0), outcome=FetchOutcome.TIMEOUT).rate_limit is None


# --- the fetch over HTTPS ------------------------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, text: str, headers: dict[str, str]) -> None:
        self.status, self._text, self.headers = status, text, headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._text.encode()


class _Opener:
    def __init__(self, outcome: _Response | Exception) -> None:
        self._outcome = outcome
        self.requests: list[tuple[Request, float]] = []

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        self.requests.append((request, timeout))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


KEY = "sk-not-a-real-key-0123"


def test_the_fetch_sends_the_key_in_its_header_and_nothing_personal() -> None:
    opener = _Opener(_Response(200, '{"data": []}', {"X-RateLimit-Remaining": "59"}))
    fetch = BalldontlieHttp(KEY, opener=opener, timeout=4.0)
    answer = fetch("/v1/stats", (("game_ids[]", "1"), ("game_ids[]", "2"), ("per_page", "100")))
    assert answer == BdlAnswer(200, '{"data": []}', {"x-ratelimit-remaining": "59"})
    ((request, timeout),) = opener.requests
    assert request.full_url == "https://api.balldontlie.io/v1/stats?game_ids[]=1&game_ids[]=2&per_page=100"
    assert dict(request.header_items()) == {"Authorization": KEY, "User-agent": "omni-scoreboard"}
    assert timeout == 4.0 and KEY not in request.full_url and KEY not in repr(fetch)
    fetch("/v1/teams", ())
    assert opener.requests[-1][0].full_url == "https://api.balldontlie.io/v1/teams"


def test_the_fetch_answers_an_error_status() -> None:
    headers = Message()
    headers["X-RateLimit-Remaining"] = "0"
    error = HTTPError("https://api.balldontlie.io/v1/games", 429, "Too Many", headers, io.BytesIO(b"slow down"))
    answer = BalldontlieHttp(KEY, opener=_Opener(error))("/v1/games", ())
    assert answer == BdlAnswer(429, "slow down", {"x-ratelimit-remaining": "0"})


def test_the_fetch_raises_only_with_no_answer_and_never_with_the_key() -> None:
    with pytest.raises(TimeoutError, match="timed out"):
        BalldontlieHttp(KEY, opener=_Opener(TimeoutError("timed out")))("/v1/games", ())
    with pytest.raises(ConnectionError) as raised:
        BalldontlieHttp(KEY, opener=_Opener(OSError(f"bad header {KEY}")))("/v1/games", ())
    assert KEY not in str(raised.value) and raised.value.__cause__ is None and raised.value.__suppress_context__


def test_the_fetch_needs_a_key() -> None:
    with pytest.raises(ValueError, match="key"):
        BalldontlieHttp("  ")


def test_a_provider_over_the_https_fetch_turns_its_failures_into_outcomes() -> None:
    fetch = BalldontlieHttp(KEY, opener=_Opener(URLError(TimeoutError())))
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=fetch, clock=ScriptedFetch([]).clock)
    assert provider.poll_slate(DAY).outcome is FetchOutcome.TIMEOUT
