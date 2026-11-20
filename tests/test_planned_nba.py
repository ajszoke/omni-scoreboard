"""NBA on the shared board's plan: the day's games at the live pace while one is on and once a minute otherwise,
yesterday's while one is still to finish, the players' lines on ALL-STAR (off for the run at a 401), the season a page
at a time, every read handed over once in arrival order, and the provider's word on its minute heeded."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.nba_feeds import NbaReading
from omni.app.planned_mlb import mlb_accounts
from omni.app.planned_nba import (
    EASTERN,
    MarketReader,
    NbaOddsReader,
    MeteredBdl,
    NbaSchedulePolicy,
    PlannedNba,
    nba_accounts,
    nba_policies,
    season_of,
)
from omni.app.poll_plan import AccountMap, Demand, PollKey, PollPlan, PollPriority, PollSource
from omni.app.poller import Poller
from omni.app.quota import BalldontliePlan, Meter, ProviderAccount, QuotaLedger, Refusal, account_limits
from omni.app.runner import board_plan, planned_nba_source
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import HomeAway, League
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import WinProbability
from omni.domain.basketball import BasketballPhase, FavoriteLine
from omni.domain.basketball_reads import LinesRead, SlateRead
from omni.domain.contest import TeamGame
from omni.providers.balldontlie import BALLDONTLIE_SOURCE, BalldontlieProvider, BdlAnswer, BdlQuery
from omni.providers.base import LocalDate, PollScope, ScopeOutcome, ScopeReport
from omni.core.ids import SourceRef
from omni.providers.nba_teams import NbaTeamRegistry
from omni.tower.interruption import Interruption, InterruptionReason
from tests.balldontlie_feed import RATE, final_row, scheduled_row, slate_row, stat_row, team

S = timedelta(seconds=1)
MLB_STATSAPI_SOURCE = SourceRef("mlb_statsapi")
WEDNESDAY, TUESDAY, THURSDAY = date(2026, 11, 18), date(2026, 11, 17), date(2026, 11, 19)
FREE, ALL_STAR = BalldontliePlan.FREE, BalldontliePlan.ALL_STAR
BDL = ProviderAccount.BALLDONTLIE


def _et(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN).astimezone(timezone.utc)


def _scope(day: date) -> PollScope:
    return PollScope(BALLDONTLIE_SOURCE, League.NBA, LocalDate(day))


def _id(game_id: int) -> LeagueScopedId:
    return LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(game_id))


def _tip(day: date, hour: int, minute: int = 0) -> str:
    return _et(day, hour, minute).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class _Bdl:
    """BALLDONTLIE as a fake: each date's rows, each game's stat rows, the season's rows (``season_page`` a page), and
    every request it was asked. A status in ``refuse`` answers the stats with it; ``down`` raises."""

    clock: FakeClock
    days: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stats: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    season: list[dict[str, Any]] = field(default_factory=list)
    season_page: int = 2
    asked: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    refuse: int | None = None
    down: bool = False
    headers: dict[str, str] = field(default_factory=lambda: dict(RATE))

    def __call__(self, path: str, query: BdlQuery) -> BdlAnswer:
        asked: dict[str, Any] = {}
        for name, value in query:
            asked.setdefault(name, []).append(value)
        self.asked.append((path, asked))
        if self.down:
            raise OSError("BALLDONTLIE is down")
        if path == "/v1/stats":
            if self.refuse is not None:
                return BdlAnswer(self.refuse, "{}", {})
            rows = [row for game in asked["game_ids[]"] for row in self.stats.get(int(game), [])]
            return self._page(rows, None)
        if "seasons[]" in asked:
            start = int(asked.get("cursor", ["0"])[0])
            page = self.season[start : start + self.season_page]
            following = start + self.season_page
            return self._page(page, str(following) if following < len(self.season) else None)
        return self._page(self.days.get(asked["dates[]"][0], []), None)

    def _page(self, rows: Sequence[dict[str, Any]], cursor: str | None) -> BdlAnswer:
        meta: dict[str, Any] = {"per_page": 100}
        if cursor is not None:
            meta["next_cursor"] = cursor
        return BdlAnswer(200, json.dumps({"data": list(rows), "meta": meta}), self.headers)

    def reads(self, kind: str) -> list[dict[str, Any]]:
        """What each request of one kind asked: ``slate``, ``lines`` or ``season``."""
        return [
            query
            for path, query in self.asked
            if (kind == "lines") == (path == "/v1/stats") and (kind == "season") == ("seasons[]" in query)
        ]


@dataclass(slots=True)
class _Board:
    clock: FakeClock
    bdl: _Bdl
    plan: PollPlan
    poller: Poller
    nba: PlannedNba
    ledger: QuotaLedger
    said: list[str]

    def step(self, by: timedelta = timedelta(0)) -> ScopeReport:
        """Move the clock, observe, ask what's due, and observe again: what the board then holds."""
        self.clock.advance(by)
        self.nba.observe(self.clock.read(), ())
        self.poller.run_due()
        return self.nba.observe(self.clock.read(), ())

    def run(self, span: timedelta, every: timedelta = S) -> None:
        """Step every ``every`` for ``span``."""
        for _ in range(int(span / every)):
            self.step(every)

    def game(self, game_id: int) -> TeamGame:
        report = self.nba.observe(self.clock.read(), ())
        for observation in report.observations:
            for contest in observation.contests:
                if contest.id == _id(game_id):
                    assert isinstance(contest, TeamGame)
                    return contest
        raise AssertionError(f"no game {game_id}")


def _board(
    at: datetime,
    *,
    tier: BalldontliePlan = ALL_STAR,
    days: dict[date, list[dict[str, Any]]] | None = None,
    season: list[dict[str, Any]] | None = None,
    ledger: QuotaLedger | None = None,
    odds: NbaOddsReader | None = None,
    market: MarketReader | None = None,
) -> _Board:
    clock = FakeClock(at)
    bdl = _Bdl(clock, days={day.isoformat(): rows for day, rows in (days or {}).items()}, season=season or [])
    ledger = ledger if ledger is not None else QuotaLedger(account_limits(balldontlie=tier))
    plan = board_plan(ledger=ledger, mlb=None, nfl=False, nba=tier)
    said: list[str] = []
    nba = planned_nba_source(plan, clock, bdl, ledger=ledger, tier=tier, odds=odds, market=market, log=said.append)
    return _Board(clock, bdl, plan, Poller(plan, clock), nba, ledger, said)


def _scheduled(game_id: int, away: str, home: str, tip: str, day: date = WEDNESDAY) -> dict[str, Any]:
    clubs: dict[str, Any] = {"away": team(game_id * 2, away), "home": team(game_id * 2 + 1, home)}
    return scheduled_row(game_id=game_id, datetime=tip, date=day.isoformat(), **clubs)


def _live(game_id: int, away: str, home: str, tip: str, day: date = WEDNESDAY, **changes: Any) -> dict[str, Any]:
    clubs: dict[str, Any] = {"away": team(game_id * 2, away), "home": team(game_id * 2 + 1, home)}
    return slate_row(game_id=game_id, datetime=tip, date=day.isoformat(), **clubs, **changes)


def _final(game_id: int, away: str, home: str, tip: str, day: date = WEDNESDAY) -> dict[str, Any]:
    clubs: dict[str, Any] = {"away": team(game_id * 2, away), "home": team(game_id * 2 + 1, home)}
    return final_row(game_id=game_id, datetime=tip, date=day.isoformat(), **clubs)


def _line(row_id: int, player: int, game_id: int, club: int, points: int = 2) -> dict[str, Any]:
    row = stat_row(row_id, player, "Ann", f"Player{player}", game_id=game_id, pts=points, fgm=1, fga=1)
    row["team"] = {"id": club, "abbreviation": "X"}
    return row


# --- the plan --------------------------------------------------------------------------------------------------


def test_the_plan_must_have_nba_s_policies_and_spend_balldontlie_s_account() -> None:
    clock = FakeClock(_et(WEDNESDAY, 12))
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=_Bdl(clock), clock=clock)
    with pytest.raises(ValueError, match="no policy for NBA's slate_states, player_lines, season_results"):
        PlannedNba(PollPlan({}), clock, provider)
    ledger = QuotaLedger(account_limits())
    wrong = AccountMap(sources={BALLDONTLIE_SOURCE: ProviderAccount.MLB_STATSAPI})
    with pytest.raises(ValueError, match="spend BALLDONTLIE's account, not MLB StatsAPI's"):
        PlannedNba(PollPlan(nba_policies(FREE), ledger=ledger, accounts=wrong), clock, provider)
    assert PlannedNba(PollPlan(nba_policies(FREE)), clock, provider).lines is False  # Free reads no lines
    with pytest.raises(ValueError, match="positive"):
        NbaSchedulePolicy(tip_lead=timedelta(0))


def test_the_board_s_plan_puts_nba_s_reads_on_balldontlie_s_account_beside_mlb_s() -> None:
    plan = board_plan(
        ledger=QuotaLedger(account_limits()), mlb=mlb_accounts(MLB_STATSAPI_SOURCE), nfl=False, nba=ALL_STAR
    )
    assert plan.accounts is not None
    assert plan.accounts.account(PollSource.SLATE_STATES, BALLDONTLIE_SOURCE) is BDL
    assert plan.accounts.account(PollSource.GAME_FEED, MLB_STATSAPI_SOURCE) is ProviderAccount.MLB_STATSAPI
    assert nba_accounts().sources == {BALLDONTLIE_SOURCE: BDL}
    with pytest.raises(ValueError, match="at least one league"):
        board_plan(ledger=None, mlb=None, nfl=False)


def test_each_tier_reads_the_slate_at_its_own_live_pace_and_once_a_minute_when_nothing_presses() -> None:
    for tier, live in ((FREE, 12), (ALL_STAR, 5)):
        slate = nba_policies(tier)[PollSource.SLATE_STATES]
        assert slate.cadence(PollPriority.NORMAL) == live * S
        assert slate.cadence(PollPriority.BACKGROUND) == 60 * S
    policies = nba_policies(ALL_STAR)
    assert policies[PollSource.PLAYER_LINES].every == 5 * S and policies[PollSource.SEASON_RESULTS].every == 10 * S


def test_a_season_is_named_for_the_year_it_tips_off() -> None:
    assert (season_of(date(2026, 11, 19)), season_of(date(2027, 4, 10)), season_of(date(2026, 7, 1))) == (
        2026,
        2026,
        2026,
    )


# --- the day's games ---------------------------------------------------------------------------------------------


def test_before_the_first_tip_the_slate_is_read_once_a_minute_and_its_games_are_the_scope_s() -> None:
    tip = _tip(WEDNESDAY, 19)
    board = _board(_et(WEDNESDAY, 12), days={WEDNESDAY: [_scheduled(1, "UTA", "LAL", tip)]})
    report = board.step()
    assert report.span.current == _scope(WEDNESDAY) and report.span.earlier == (_scope(TUESDAY),)
    assert report.span.max_age == 3 * 60 * S  # fresh for two minutes past the next read, a minute off
    (answer,) = report.observations
    assert answer.scope == _scope(WEDNESDAY) and answer.outcome is ScopeOutcome.COMPLETE
    (game,) = answer.contests
    assert game.id == _id(1) and game.league is League.NBA
    board.run(59 * S)
    assert len(board.bdl.reads("slate")) == 1
    board.step(S)
    assert len(board.bdl.reads("slate")) == 2
    row = board.nba.slate_game(board.game(1))
    assert row is not None and row.phase is BasketballPhase.SCHEDULED
    assert board.nba.take(board.game(1)) == [] and board.nba.latest(board.game(1)) is None  # nothing under way


def test_within_its_lead_of_a_tip_and_while_a_game_is_on_the_slate_is_read_at_the_live_pace() -> None:
    tip = _tip(WEDNESDAY, 19)
    board = _board(_et(WEDNESDAY, 18, 54), tier=FREE, days={WEDNESDAY: [_scheduled(1, "UTA", "LAL", tip)]})
    board.step()
    board.run(60 * S)
    assert len(board.bdl.reads("slate")) == 2  # 6:54 and 6:55: the tip is five minutes off
    board.run(60 * S)
    assert len(board.bdl.reads("slate")) == 7  # every 12 s on Free
    assert board.nba.observe(board.clock.read(), ()).span.max_age == 2 * 60 * S + 12 * S


def test_a_game_under_way_hands_over_each_read_once_in_arrival_order_and_its_lines_on_all_star() -> None:
    tip = _tip(WEDNESDAY, 19)
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", tip)]})
    board.bdl.stats[1] = [_line(10, 100, 1, 2)]
    board.step()  # the slate finds the game under way: its lines are wanted from here
    board.step(5 * S)
    game = board.game(1)
    taken = board.nba.take(game)
    assert [type(reading.read) for reading in taken] == [SlateRead, LinesRead, SlateRead]
    assert [reading.read.at for reading in taken] == sorted(reading.read.at for reading in taken)
    assert taken[0].current_for == 120 * S  # asked before any game was known: two of the minute's cadence
    assert all(reading.current_for == 10 * S for reading in taken[1:])  # two of the 5 s cadence
    assert board.nba.take(game) == []  # each once
    latest = board.nba.latest(game)
    assert isinstance(latest, NbaReading) and latest.read.at == taken[-1].read.at
    assert board.bdl.reads("lines")[0]["game_ids[]"] == ["1"]
    assert board.nba.take(TeamGame(**{**_fields(game), "id": _id(9)})) == []  # a game it doesn't know


def _fields(game: TeamGame) -> dict[str, Any]:
    return {name: getattr(game, name) for name in game.__dataclass_fields__}


def test_a_game_s_lines_are_read_for_a_few_minutes_after_its_final_then_nothing_more() -> None:
    tip = _tip(WEDNESDAY, 19)
    board = _board(_et(WEDNESDAY, 21, 30), days={WEDNESDAY: [_final(1, "UTA", "LAL", tip)]})
    board.bdl.stats[1] = [_line(10, 100, 1, 2)]
    board.step()
    board.run(3 * 60 * S)
    lines = len(board.bdl.reads("lines"))
    assert 30 <= lines <= 37
    board.run(60 * S)
    assert len(board.bdl.reads("lines")) == lines  # past three minutes: no more
    assert len(board.bdl.reads("slate")) <= 6  # the slate, nothing pressing, once a minute


def test_free_reads_no_lines() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), tier=FREE, days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.run(60 * S)
    assert board.bdl.reads("lines") == [] and not board.nba.lines
    assert (len(board.bdl.reads("slate")), len(board.bdl.reads("season"))) == (4, 1)  # Free's 5 a minute


def test_a_401_from_the_lines_turns_them_off_for_the_run_said_once_and_never_asked_again() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.bdl.refuse = 401
    board.step()
    board.run(30 * S)
    assert len(board.bdl.reads("lines")) == 1 and not board.nba.lines
    assert board.said == [
        "omni.app: NBA runs on the slate alone: BALLDONTLIE refused the players' lines (HTTP 401), which its key's"
        " tier doesn't read, so NBA's cards have no captions or stat panels"
    ]


def test_a_failed_lines_read_is_asked_again_and_files_nothing() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.bdl.refuse = 500
    board.step()
    board.step()
    failure = board.plan.failure(PollKey(PollSource.PLAYER_LINES, _scope(WEDNESDAY)))
    assert failure is not None and failure.error == "ProviderError: BALLDONTLIE the players' lines: HTTP 500"
    assert board.nba.lines and all(isinstance(each.read, SlateRead) for each in board.nba.take(board.game(1)))


def test_lines_for_a_game_the_plan_no_longer_holds_are_set_aside() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.bdl.stats[1] = [_line(10, 100, 1, 2)]
    board.bdl.stats[2] = [_line(11, 101, 2, 4)]
    board.step()
    board.bdl.days["2026-11-19"] = [_live(2, "BOS", "NYK", _tip(THURSDAY, 19), day=THURSDAY)]
    board.nba._provider.poll_slate(THURSDAY)  # the provider has read game 2, the plan never filed it
    lines = board.nba._lines_read(PollKey(PollSource.PLAYER_LINES, _scope(WEDNESDAY)), (_id(1), _id(2)), 10 * S)
    lines(board.clock.now())
    taken = board.nba.take(board.game(1))
    assert len([each for each in taken if isinstance(each.read, LinesRead)]) == 1


def test_yesterday_is_read_early_in_the_day_and_while_one_of_its_games_is_still_on() -> None:
    late = _live(5, "POR", "SAC", _tip(TUESDAY, 22), day=TUESDAY)
    board = _board(_et(WEDNESDAY, 0, 30), days={TUESDAY: [late], WEDNESDAY: []})
    report = board.step()
    assert {observation.scope for observation in report.observations} == {_scope(WEDNESDAY), _scope(TUESDAY)}
    board.run(30 * S)
    assert len([each for each in board.bdl.reads("slate") if each["dates[]"] == ["2026-11-17"]]) == 7
    board.bdl.days["2026-11-17"] = [_final(5, "POR", "SAC", _tip(TUESDAY, 22), day=TUESDAY)]
    board.run(60 * S)
    tuesday = len([each for each in board.bdl.reads("slate") if each["dates[]"] == ["2026-11-17"]])
    board.run(60 * S)
    assert len([each for each in board.bdl.reads("slate") if each["dates[]"] == ["2026-11-17"]]) == tuesday
    late_morning = _board(_et(WEDNESDAY, 9), days={TUESDAY: [late]})
    late_morning.step()
    assert [each["dates[]"] for each in late_morning.bdl.reads("slate")] == [["2026-11-18"]]


def test_a_game_the_slate_leaves_out_before_its_tip_has_left_the_date_and_an_unreadable_one_stays() -> None:
    tip = _tip(WEDNESDAY, 19)
    rows = [_scheduled(1, "UTA", "LAL", tip), _scheduled(2, "BOS", "NYK", tip), _scheduled(3, "DEN", "PHX", tip)]
    board = _board(_et(WEDNESDAY, 12), days={WEDNESDAY: rows})
    board.step()
    board.bdl.days["2026-11-18"] = [rows[0], {**rows[2], "status_state": "unknown"}, _scheduled(4, "MIA", "ORL", tip)]
    board.bdl.days["2026-11-18"].append({**_scheduled(6, "ATL", "CHA", tip), "date": "2026-11-19"})
    report = board.step(60 * S)
    (answer,) = [each for each in report.observations if each.scope == _scope(WEDNESDAY)]
    assert answer.outcome is ScopeOutcome.PARTIAL and answer.warnings == (
        "unknown_status: game 3: the feed doesn't know the game's status",
    )
    assert {contest.id for contest in answer.contests} == {_id(1), _id(3), _id(4)}


def test_a_failing_slate_reports_its_failure_while_nothing_has_answered_since() -> None:
    board = _board(_et(WEDNESDAY, 12), days={WEDNESDAY: []})
    board.bdl.down = True
    report = board.step()
    (failure,) = report.observations
    assert failure.outcome is ScopeOutcome.FAILED and "BALLDONTLIE the slate" in (failure.error or "")
    board.bdl.down = False
    board.step(2 * S)
    report = board.step(S)
    assert [observation.outcome for observation in report.observations] == [ScopeOutcome.COMPLETE]


# --- the season --------------------------------------------------------------------------------------------------


def test_the_season_is_read_a_page_at_a_time_once_a_day_without_the_day_s_own_finals() -> None:
    season = [
        _final(21, "UTA", "LAL", _tip(TUESDAY, 22), day=TUESDAY),
        _final(22, "BOS", "NYK", _tip(TUESDAY, 19), day=TUESDAY),
        _final(23, "DEN", "PHX", _tip(WEDNESDAY, 19)),  # tonight's own: the pipeline folds it in when it shows it
    ]
    board = _board(_et(WEDNESDAY, 12), days={WEDNESDAY: []}, season=season)
    board.step()
    assert board.nba.season() is None  # a page of two read, one to go
    board.step(10 * S)
    results = board.nba.season()
    assert results is not None and [each.game for each in results.results] == [_id(21), _id(22)]
    assert [each["cursor"] if "cursor" in each else None for each in board.bdl.reads("season")] == [None, ["2"]]
    assert board.bdl.reads("season")[0]["seasons[]"] == ["2026"]
    board.run(60 * S)
    assert len(board.bdl.reads("season")) == 2  # once a day
    board.clock.set(_et(THURSDAY, 12))
    board.step()
    assert len(board.bdl.reads("season")) == 3  # a new day reads it again, from its first page


def test_a_failed_season_page_is_asked_again_from_where_the_read_stood() -> None:
    board = _board(_et(WEDNESDAY, 12), days={WEDNESDAY: []}, season=[_final(21, "UTA", "LAL", _tip(TUESDAY, 22))])
    board.bdl.down = True
    board.step()
    assert board.nba.season() is None
    board.bdl.down = False
    board.step(2 * S)
    assert board.nba.season() is not None


def test_a_season_page_from_another_day_s_read_files_nothing() -> None:
    board = _board(_et(WEDNESDAY, 12), days={WEDNESDAY: []}, season=[_final(21, "UTA", "LAL", _tip(TUESDAY, 22))])
    board.nba.observe(board.clock.read(), ())
    (ask,) = [ask for ask in board.plan.take_due(board.clock.read()) if ask.key.source is PollSource.SEASON_RESULTS]
    board.clock.set(_et(THURSDAY, 12))
    board.nba.observe(board.clock.read(), ())  # Thursday's read starts over
    ask.fetch(board.clock.now())
    assert board.nba.season() is None


# --- the quota and the provider's word ---------------------------------------------------------------------------


def test_the_quota_holding_a_read_back_says_why_for_its_scope() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), tier=FREE, days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.step()
    board.ledger.pause(BDL, board.clock.monotonic() + 30 * S)
    board.step(12 * S)
    assert board.nba.held_back(_scope(WEDNESDAY)) == "quota: BALLDONTLIE said to wait, normal waiting"
    assert board.nba.held_back(_scope(TUESDAY)) is None
    board.step(30 * S)
    assert board.nba.held_back(_scope(WEDNESDAY)) is None


@pytest.mark.parametrize(
    ("status", "retry_after", "window", "wait"),
    [
        (429, "7", None, 7),  # the 429 says how long
        (429, None, (0, 20), 20),  # it doesn't: until the window resets
        (429, None, None, 60),  # nor does anything else: the provider's minute
        (200, None, (0, 12), 12),  # nothing left in the window
        (200, None, (0, 600), 60),  # a reset past a minute off is a clock that's off
        (200, None, (0, -5), 0),  # one already past is no wait
        (200, None, (3, 20), None),  # room left
    ],
)
def test_an_answer_that_says_the_minute_is_spent_makes_the_account_wait(
    status: int, retry_after: str | None, window: tuple[int, int] | None, wait: int | None
) -> None:
    clock = FakeClock(_et(WEDNESDAY, 12))
    headers: dict[str, str] = {"Retry-After": retry_after} if retry_after is not None else {}
    if window is not None:
        remaining, reset = window
        headers.update(
            {
                "x-ratelimit-limit": "60",
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(int(clock.now().timestamp()) + reset),
            }
        )
    ledger = QuotaLedger(account_limits())
    fetch = MeteredBdl(lambda path, query: BdlAnswer(status, "{}", headers), Meter(ledger, BDL, clock), clock)
    assert fetch("/v1/games", ()).status == status
    admission = ledger.admit(BDL, Demand(PollPriority.NORMAL), clock.read())
    if wait is None or wait == 0:
        assert admission.admitted
    else:
        assert admission.refusal is Refusal.PROVIDER_WAIT and admission.retry_at == clock.monotonic() + wait * S
    assert ledger.usage(BDL, clock.read()).requests == 1


def test_a_request_that_got_no_answer_is_counted_and_one_the_ledger_refuses_is_never_made() -> None:
    clock = FakeClock(_et(WEDNESDAY, 12))
    ledger = QuotaLedger(account_limits())
    made: list[str] = []

    def down(path: str, query: BdlQuery) -> BdlAnswer:
        made.append(path)
        raise TimeoutError("no answer")

    fetch = MeteredBdl(down, Meter(ledger, BDL, clock), clock)
    with pytest.raises(TimeoutError):
        fetch("/v1/games", ())
    assert ledger.usage(BDL, clock.read()).requests == 1
    ledger.pause(BDL, clock.monotonic() + 10 * S)
    with pytest.raises(Exception, match="said to wait"):
        fetch("/v1/games", ())
    assert made == ["/v1/games"]


# --- a host's gap ------------------------------------------------------------------------------------------------


def test_after_a_host_s_gap_its_reads_are_wanted_afresh_and_an_ask_from_before_files_nothing() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.bdl.stats[1] = [_line(10, 100, 1, 2)]
    board.step()
    game = board.game(1)
    board.nba.observe(board.clock.read(), ())
    board.clock.advance(5 * S)
    board.nba.observe(board.clock.read(), ())
    asks = board.plan.take_due(board.clock.read())
    assert {ask.key.source for ask in asks} == {PollSource.SLATE_STATES, PollSource.PLAYER_LINES}
    gap = Interruption(
        serial=1, reason=InterruptionReason.PROCESS_STALL, before=board.clock.read(), after=board.clock.read()
    )
    board.nba.interrupted(gap, held=(), contests=())
    assert len(board.plan) == 0 and board.nba.take(game) == []
    for ask in asks:
        ask.fetch(board.clock.now())  # each from before the gap: nothing filed
    assert board.nba.take(game) == []
    board.step()
    assert len(board.nba.take(game)) == 2  # wanted afresh, asked at once


def test_a_season_page_asked_before_a_host_s_gap_files_nothing() -> None:
    board = _board(_et(WEDNESDAY, 12), days={WEDNESDAY: []}, season=[_final(21, "UTA", "LAL", _tip(TUESDAY, 22))])
    board.nba.observe(board.clock.read(), ())
    (ask,) = [ask for ask in board.plan.take_due(board.clock.read()) if ask.key.source is PollSource.SEASON_RESULTS]
    gap = Interruption(
        serial=1, reason=InterruptionReason.PROCESS_STALL, before=board.clock.read(), after=board.clock.read()
    )
    board.nba.interrupted(gap, held=(), contests=())
    ask.fetch(board.clock.now())
    assert board.nba.season() is None


def test_an_older_date_the_board_still_holds_is_kept_and_another_source_s_is_not() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.step()
    game = board.game(1)
    board.clock.set(_et(date(2026, 11, 20), 12))
    other = PollScope(MLB_STATSAPI_SOURCE, League.MLB, LocalDate(WEDNESDAY))
    board.nba.observe(board.clock.read(), (_scope(WEDNESDAY), other))
    assert board.nba.slate_game(game) is not None
    board.nba.observe(board.clock.read(), (other,))
    assert board.nba.slate_game(game) is None


def test_a_read_filed_after_a_newer_one_is_taken_in_its_place_but_never_the_newest() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.step()
    game = board.game(1)
    (first,) = board.nba.take(game)
    board.step(5 * S)
    newest = board.nba.latest(game)
    board.nba._keep(board.nba._games[game.id], first)  # as a worker that raced another files it late
    assert board.nba.latest(game) == newest
    assert board.nba.take(game)[0] == first  # handed over in its place by when it arrived


# --- the odds and the meter ----------------------------------------------------------------------------------------


def test_an_upcoming_game_s_line_and_a_game_s_meter_are_read_on_their_own_accounts_as_the_card_asks() -> None:
    tip = _tip(WEDNESDAY, 19)
    line = FavoriteLine(favorite=HomeAway.HOME, moneyline=-150, spread=-3.5)
    quotes: list[WinProbability | None] = [WinProbability(home=58.0, away=42.0), None]

    def odds(game: TeamGame, now: datetime) -> FavoriteLine | None:
        return line

    def market(game: TeamGame, now: datetime) -> WinProbability | None:
        return quotes.pop(0)

    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", tip)]}, odds=odds, market=market)
    board.step()
    game = board.game(1)
    assert board.nba.line(game) is None and board.nba.win_probability(game, board.clock.read()) is None  # asked
    board.poller.run_due()
    assert board.nba.line(game) == line
    reading = board.nba.win_probability(game, board.clock.read())
    assert reading is not None and reading.value.home == 58.0
    assert board.nba.win_probability(game, ClockReading(_et(WEDNESDAY, 19), MonotonicTime(-1.0))) is None  # not yet
    accounts = board.plan.accounts
    assert accounts is not None
    assert accounts.account(PollSource.GAME_LINES, BALLDONTLIE_SOURCE) is ProviderAccount.BOVADA
    assert accounts.account(PollSource.PUBLIC_MARKET, BALLDONTLIE_SOURCE) is ProviderAccount.POLYMARKET_US
    board.clock.advance(15 * S)
    board.nba.win_probability(game, board.clock.read())
    board.poller.run_due()
    assert board.nba.win_probability(game, board.clock.read()) is None  # the market has nothing now
    gap = Interruption(
        serial=1, reason=InterruptionReason.PROCESS_STALL, before=board.clock.read(), after=board.clock.read()
    )
    board.nba.interrupted(gap, held=(), contests=(game.id, LeagueScopedId(League.MLB, MLB_STATSAPI_SOURCE, "1")))
    assert board.plan.latest(PollKey(PollSource.PUBLIC_MARKET, game.id), board.clock.read()) is None
    assert board.nba.line(game) == line  # the odds stand


def test_without_a_sportsbook_or_a_market_nothing_is_asked() -> None:
    board = _board(_et(WEDNESDAY, 19, 30), days={WEDNESDAY: [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    board.step()
    game = board.game(1)
    assert board.nba.line(game) is None and board.nba.win_probability(game, board.clock.read()) is None
    assert PollKey(PollSource.GAME_LINES, game.id) not in board.plan._entries
