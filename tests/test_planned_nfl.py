"""NFL on the shared board's plan: the day's games read once, a favorite followed with its plays, the others read
together while live, each other final read once, the answers handed over once each, and a month that runs out."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from omni.app.contest_store import ContestStore, ProviderStatus
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb, mlb_accounts
from omni.app.planned_nfl import NFL_POLICIES, NflSchedulePolicy, PlannedNfl, nfl_accounts
from omni.app.poll_plan import AccountMap, PollPlan, PollSource
from omni.app.poller import Poller
from omni.app.quota import (
    BillingMonth,
    HttpAnswer,
    MeteredHttp,
    Meter,
    MonthUsage,
    NflMetaPlan,
    ProviderAccount,
    QuotaLedger,
    account_limits,
    nflmeta_billed,
)
from omni.app.runner import build_loop
from omni.core.clock import FakeClock
from omni.core.enum import GameStatus, League, PanelProfile
from omni.domain.contest import TeamGame
from omni.core.ids import LeagueScopedId
from omni.events.baseball import LiveBaseballFeed
from omni.preferences.favorites import Favorites
from omni.providers.base import LocalDate, PollScope, ProviderError, ScopeOutcome, ScopeReport
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import HALFTIME_HOLD, NFLMETA_SOURCE, NflMetaLiveProvider
from omni.tower.board import SourceHealth
from tests.test_planned_mlb import AFTER_MIDNIGHT, TODAY, _FakeMlb
from tests.test_app_loop import _game as _mlb_game

EASTERN = ZoneInfo("America/New_York")
SUNDAY, SATURDAY, MONDAY = date(2026, 11, 15), date(2026, 11, 14), date(2026, 11, 16)
S = timedelta(seconds=1)
NFL = ProviderAccount.NFLMETA
SCORES = "/api/v1/live-scores"
CAR = Favorites({League.NFL: ["CAR"]})


def _et(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=EASTERN).astimezone(timezone.utc)


def _scope(day: date) -> PollScope:
    return PollScope(NFLMETA_SOURCE, League.NFL, LocalDate(day))


def _row(game_id: int, away: str, home: str, kickoff: datetime, phase: str = "pre", **changes: Any) -> dict[str, Any]:
    """A scoreboard row as NFLMeta writes one; a game in progress has its first quarter's clock."""
    row: dict[str, Any] = {
        "game_id": game_id,
        "kickoff_at": kickoff.isoformat().replace("+00:00", "Z"),
        "away_team": {"abbr": away, "score": 0},
        "home_team": {"abbr": home, "score": 0},
        "phase": phase,
        "period": 0 if phase == "pre" else 1,
        "clock": "15:00",
        "status_detail": "Final" if phase == "post" else None,
        "situation": {"possession_abbr": home, "down": 1, "distance": 10, "text": "1st & 10", "yardline_100": 75},
        "play_sync": {"status": "unknown", "last_play_id": None},
        "week": 11,
        "injuries": [],
    }
    row.update(changes)
    return row


def _change(play_id: str, number: int) -> dict[str, Any]:
    play = {"play_number": number, "play_type": "rush", "quarter": 1, "description": f"Run {number}."}
    return {"source_play_id": play_id, "revision": 1, "play": play}


class _NflMeta:
    """NFLMeta as a fake: each game's scoreboard row as the test sets it and its play changes, answering
    live-scores by its filters (unfiltered, ``game_id``, or ``phase`` with ``game_ids``) and billing one row a game
    listed, one an injury entry and one a play change, in the header a real response carries. Given what's left of
    the month (``requests_left``), each response says so too, as NFLMeta's does. Every request is kept, with when
    it was made."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.rows: dict[int, dict[str, Any]] = {}
        self.plays: dict[int, list[dict[str, Any]]] = {}
        self.requests: list[tuple[datetime, str, dict[str, Any]]] = []
        self.hidden: set[int] = set()  # games a phase=in read leaves out, as if their rows went missing
        self.down = False
        self.requests_left: int | None = None

    def set(self, game_id: int, **changes: Any) -> None:
        self.rows[game_id] = {**self.rows[game_id], **changes}

    def __call__(self, path: str, query: Mapping[str, str | int]) -> HttpAnswer:
        self.requests.append((self.clock.now(), path, dict(query)))
        if self.down:
            raise OSError("NFLMeta is down")
        if path.endswith("/play-changes"):
            changes = self.plays.get(int(path.split("/")[4]), [])
            start = int(query.get("cursor", 0))
            page = changes[start : start + int(query["limit"])]
            meta = {"has_more": start + len(page) < len(changes), "next_cursor": str(start + len(page))}
            return self._answer({"data": page, "meta": meta}, len(page))
        rows = list(self.rows.values())
        if "game_id" in query:
            rows = [row for row in rows if row["game_id"] == query["game_id"]]
        if "game_ids" in query:
            named = {int(each) for each in str(query["game_ids"]).split(",")} - self.hidden
            rows = [row for row in rows if row["game_id"] in named and row["phase"] == query["phase"]]
        billed = len(rows) + sum(len(row["injuries"]) for row in rows)
        return self._answer({"data": rows, "meta": {"recommended_poll_seconds": 10}}, billed)

    def _answer(self, body: Any, billed: int) -> HttpAnswer:
        headers = {"x-nflmeta-response-rows": str(billed)}
        if self.requests_left is not None:
            self.requests_left = max(self.requests_left - 1, 0)
            headers["x-monthly-quota-remaining"] = str(self.requests_left)
        return HttpAnswer(body, headers)

    def asked(self, **query: Any) -> list[datetime]:
        """When live-scores was asked with exactly ``query``."""
        return [at for at, path, asked in self.requests if path == SCORES and asked == query]

    def play_reads(self, game_id: int) -> list[datetime]:
        return [at for at, path, _ in self.requests if path == f"/api/v1/games/{game_id}/play-changes"]


@dataclass
class _Board:
    planned: PlannedNfl
    plan: PollPlan
    clock: FakeClock
    nflmeta: _NflMeta
    ledger: QuotaLedger
    poller: Poller
    store: ContestStore = field(default_factory=ContestStore)
    reports: list[ScopeReport] = field(default_factory=list)

    def observe(self) -> ScopeReport:
        report = self.planned.observe(self.clock.read(), self.store.scopes)
        self.store.take(report)
        self.store.reconcile(self.clock.monotonic())
        self.reports.append(report)
        return report

    def step(self) -> ScopeReport:
        """One pass: what's wanted, the poller's asks, then the pass that reads their answers."""
        self.observe()
        self.poller.run_due()
        return self.observe()

    def run(self, until: datetime, every: timedelta = 5 * S) -> None:
        """A pass every ``every`` from now until just before ``until``."""
        while self.clock.now() < until:
            self.step()
            self.clock.advance(every)

    def ids(self, day: date) -> list[str]:
        return [contest.id.raw for contest in self.store.membership(_scope(day))]

    def status(self, day: date, game_id: int) -> GameStatus:
        return next(contest.status for contest in self.store.membership(_scope(day)) if contest.id.raw == str(game_id))

    def health(self) -> ProviderStatus:
        health = self.store.league_health(League.NFL, self.clock.monotonic())
        assert health is not None
        return health.status


def _board(
    start: datetime,
    *,
    favorites: Favorites = CAR,
    ledger: QuotaLedger | None = None,
    policy: NflSchedulePolicy = NflSchedulePolicy(),
) -> _Board:
    clock = FakeClock(start)
    nflmeta = _NflMeta(clock)
    ledger = ledger if ledger is not None else QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = PollPlan(NFL_POLICIES, ledger=ledger, accounts=nfl_accounts())
    meter = Meter(ledger, NFL, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(nflmeta, meter))
    planned = PlannedNfl(plan, clock, provider, favorites=favorites, policy=policy)
    return _Board(planned, plan, clock, nflmeta, ledger, Poller(plan, clock))


def _sunday(board: _Board, *games: tuple[int, str, str, datetime]) -> None:
    for game_id, away, home, kickoff in games:
        board.nflmeta.rows[game_id] = _row(game_id, away, home, kickoff)


EARLY = _et(SUNDAY, 13)
LATE = _et(SUNDAY, 16, 25)
CAR_ATL = (101, "CAR", "ATL", EARLY)
PIT_CIN = (102, "PIT", "CIN", EARLY)
MIN_GB = (103, "MIN", "GB", EARLY)
LAC_BAL = (104, "LAC", "BAL", LATE)


def _start(board: _Board, *game_ids: int) -> None:
    for game_id in game_ids:
        board.nflmeta.set(game_id, phase="in")


def _end(board: _Board, *game_ids: int) -> None:
    for game_id in game_ids:
        board.nflmeta.set(game_id, phase="post", status_detail="Final")


# --- the day's games --------------------------------------------------------------------------------------


def test_the_day_list_is_read_once_before_kickoff_and_its_games_are_the_date_s_members() -> None:
    board = _board(_et(SUNDAY, 12))
    _sunday(board, CAR_ATL, PIT_CIN, LAC_BAL)
    board.run(EARLY)
    assert board.nflmeta.requests == [(_et(SUNDAY, 12), SCORES, {})]  # one unfiltered read, an hour early
    assert board.ids(SUNDAY) == ["101", "102", "104"]
    assert {contest.status for contest in board.store.membership(_scope(SUNDAY))} == {GameStatus.PREGAME}
    report = board.reports[-1]
    assert (report.span.current, report.span.earlier) == (_scope(SUNDAY), (_scope(SATURDAY),))
    assert board.health() is ProviderStatus.FRESH  # nothing is due before kickoff, so the list stays fresh
    _start(board, 101, 102)
    board.run(_et(SUNDAY, 13, 30))
    assert board.nflmeta.asked() == [_et(SUNDAY, 12)]  # still the one read, with the games being played
    assert board.status(SUNDAY, 101) is GameStatus.LIVE and board.health() is ProviderStatus.FRESH


def test_the_date_rolls_over_at_midnight_in_the_league_s_time_zone_with_a_list_of_its_own() -> None:
    board = _board(_et(SUNDAY, 23))
    _sunday(board, CAR_ATL, PIT_CIN)
    _end(board, 101, 102)
    board.run(_et(MONDAY, 0))
    assert board.nflmeta.asked() == [_et(SUNDAY, 23)] and len(board.nflmeta.requests) == 1  # finals: nothing to read
    board.nflmeta.rows[105] = _row(105, "DAL", "PHI", _et(MONDAY, 20, 15))  # NFLMeta has turned to Monday
    board.run(_et(MONDAY, 0, 5))
    assert board.nflmeta.asked() == [_et(SUNDAY, 23), _et(MONDAY, 0)]
    assert board.ids(MONDAY) == ["105"] and board.ids(SUNDAY) == ["101", "102"]  # each game under its own date
    report = board.reports[-1]
    assert (report.span.current, report.span.earlier) == (_scope(MONDAY), (_scope(SUNDAY),))
    assert board.status(SUNDAY, 101) is GameStatus.FINAL


def test_a_date_that_lists_no_game_of_its_own_is_listed_again_after_two_hours_and_stays_fresh_between() -> None:
    board = _board(_et(SATURDAY, 8))
    board.nflmeta.rows[101] = _row(*CAR_ATL)  # tomorrow's game: not Saturday's to list
    board.run(_et(SATURDAY, 10))
    assert board.nflmeta.asked() == [_et(SATURDAY, 8)]
    assert board.ids(SATURDAY) == [] and board.health() is ProviderStatus.FRESH
    [answer] = board.reports[-1].observations
    assert (answer.scope, answer.outcome, answer.contests) == (_scope(SATURDAY), ScopeOutcome.COMPLETE, ())
    board.run(_et(SATURDAY, 10, 0, 5))
    assert board.nflmeta.asked() == [_et(SATURDAY, 8), _et(SATURDAY, 10)]


def test_a_day_list_that_skips_a_row_is_partial() -> None:
    board = _board(_et(SUNDAY, 12))
    _sunday(board, CAR_ATL, PIT_CIN)
    board.nflmeta.set(102, home_team={"abbr": "XXX", "score": 0})
    board.step()
    [answer] = board.reports[-1].observations
    assert answer.outcome is ScopeOutcome.PARTIAL and answer.warnings == ("game 102: unreadable (KeyError: 'XXX')",)
    assert board.ids(SUNDAY) == ["101"]


# --- a favorite's game ------------------------------------------------------------------------------------


def test_a_favorite_is_read_every_ten_seconds_and_its_plays_when_the_scoreboard_points_at_a_new_one() -> None:
    board = _board(_et(SUNDAY, 12, 59, 50))
    _sunday(board, CAR_ATL)
    board.run(EARLY)
    assert board.nflmeta.asked(game_id=101) == []  # nothing before kickoff
    _start(board, 101)
    board.run(_et(SUNDAY, 13, 0, 15))
    board.nflmeta.plays[101] = [_change("p1", 1)]
    board.nflmeta.set(101, play_sync={"status": "matched", "last_play_id": "p1"}, clock="14:21")
    board.run(_et(SUNDAY, 13, 0, 45))
    polled = [_et(SUNDAY, 13, 0, second) for second in (0, 10, 20, 30, 40)]
    assert board.nflmeta.asked(game_id=101) == polled
    assert board.nflmeta.play_reads(101) == [polled[0], polled[2]]  # the first look, then the new play
    assert board.nflmeta.asked(phase="in", game_ids="101") == []  # never in the others' read
    readings = board.planned.take(_game(board, 101))
    assert [reading.arrived.utc for reading in readings] == [_et(SUNDAY, 12, 59, 50), *polled]
    assert [len(reading.snapshot.events) for reading in readings] == [0, 0, 0, 1, 0, 0]
    assert readings[3].snapshot.events[0].play == 1


def test_a_favorite_at_halftime_is_not_asked_through_its_hold_and_its_date_stays_fresh() -> None:
    kicked_off = _row(*CAR_ATL, phase="in", play_sync={"status": "matched", "last_play_id": "p1"})
    board = _board(_et(SUNDAY, 14, 20))
    board.nflmeta.rows[101] = kicked_off
    board.nflmeta.plays[101] = [_change("p1", 1)]
    board.run(_et(SUNDAY, 14, 20, 10))  # the day list, then the game's first read
    board.nflmeta.plays[101].append(_change("p2", 2))
    board.nflmeta.set(
        101, period=2, clock="0:00", status_detail="Halftime", play_sync={"status": "matched", "last_play_id": "p2"}
    )
    board.run(_et(SUNDAY, 14, 33, 15))
    seen = _et(SUNDAY, 14, 20, 15)
    assert board.nflmeta.requests[-1][0] == seen  # the poll that saw halftime, and none since
    latest = board.planned.latest(_game(board, 101))
    assert latest is not None and latest.snapshot.break_ends_at == seen + HALFTIME_HOLD
    assert board.health() is ProviderStatus.FRESH  # nothing was due: the hold's end is when one is
    board.nflmeta.set(101, period=3, clock="15:00", status_detail="15:00 - 3rd")
    board.run(_et(SUNDAY, 14, 33, 35))
    assert board.nflmeta.asked(game_id=101)[-2:] == [seen + HALFTIME_HOLD, seen + HALFTIME_HOLD + 10 * S]


def test_a_favorite_s_final_is_read_through_its_drain_then_never_again() -> None:
    board = _board(_et(SUNDAY, 16, 8, 55))
    board.nflmeta.rows[101] = _row(*CAR_ATL, phase="in", period=4)
    board.run(_et(SUNDAY, 16, 10))
    _end(board, 101)
    board.run(_et(SUNDAY, 16, 20))
    asked = board.nflmeta.asked(game_id=101)
    assert asked[-1] == _et(SUNDAY, 16, 12)  # the drain's last read, two minutes after the final was seen
    assert asked[-13:] == [_et(SUNDAY, 16, 10) + n * 10 * S for n in range(13)]
    assert board.status(SUNDAY, 101) is GameStatus.FINAL and board.health() is ProviderStatus.FRESH


def test_an_equal_abbreviation_in_another_league_gets_no_favorite_cadence() -> None:
    board = _board(_et(SUNDAY, 12, 59, 55), favorites=Favorites({League.MLB: ["MIN"]}))  # the Twins, not the Vikings
    _sunday(board, MIN_GB)
    board.run(EARLY)
    _start(board, 103)
    board.run(_et(SUNDAY, 13, 1))
    assert board.nflmeta.asked(game_id=103) == [] and board.nflmeta.play_reads(103) == []
    assert board.nflmeta.asked(phase="in", game_ids="103") == [EARLY, EARLY + 45 * S]


# --- the other games --------------------------------------------------------------------------------------


def test_the_others_are_read_together_every_45_seconds_while_live_and_each_final_is_read_once() -> None:
    board = _board(_et(SUNDAY, 12, 59))
    _sunday(board, CAR_ATL, PIT_CIN, MIN_GB, LAC_BAL)
    board.run(EARLY)
    assert board.nflmeta.asked(phase="in", game_ids="102,103") == []  # nothing before kickoff
    _start(board, 101, 102, 103)
    board.run(_et(SUNDAY, 13, 2, 20))
    _end(board, 102, 103)
    board.run(_et(SUNDAY, 13, 10))
    live = board.nflmeta.asked(phase="in", game_ids="102,103")
    assert live == [EARLY + n * 45 * S for n in range(5)]  # the favorite left out; the late game not started
    assert board.nflmeta.asked(game_id=102) == board.nflmeta.asked(game_id=103) == [live[-1] + 5 * S]  # once each
    assert [board.status(SUNDAY, game_id) for game_id in (102, 103)] == [GameStatus.FINAL] * 2
    assert [path for _, path, query in board.nflmeta.requests if "game_ids" in query] == [SCORES] * 5  # none since
    _start(board, 104)
    board.run(_et(SUNDAY, 16, 26))
    assert board.nflmeta.asked(phase="in", game_ids="104") == [LATE, LATE + 45 * S]


def test_a_game_the_live_read_misses_but_its_own_read_finds_in_progress_is_read_with_the_others_again() -> None:
    board = _board(_et(SUNDAY, 12, 59, 55))
    _sunday(board, PIT_CIN, MIN_GB)
    _start(board, 102, 103)
    board.run(_et(SUNDAY, 13, 0, 5))
    board.nflmeta.hidden = {103}  # the next read of the others leaves it out
    board.run(_et(SUNDAY, 13, 1, 35))
    assert board.nflmeta.asked(game_id=103) == [EARLY + 50 * S]  # read on its own: still in progress
    assert board.nflmeta.asked(phase="in", game_ids="102,103") == [EARLY, EARLY + 45 * S, EARLY + 90 * S]
    assert board.nflmeta.asked(phase="in", game_ids="102") == []  # it was never dropped from the others'
    assert board.status(SUNDAY, 103) is GameStatus.LIVE


def test_a_board_started_mid_afternoon_reads_what_the_day_list_found_in_progress_and_not_its_finals() -> None:
    board = _board(_et(SUNDAY, 15))
    _sunday(board, CAR_ATL, PIT_CIN, MIN_GB, LAC_BAL)
    _start(board, 101, 103)
    _end(board, 102)
    board.run(_et(SUNDAY, 15, 1))
    assert board.nflmeta.asked(phase="in", game_ids="103") == [_et(SUNDAY, 15, 0, 5), _et(SUNDAY, 15, 0, 50)]
    assert board.nflmeta.asked(game_id=102) == []  # its final came with the day list
    assert board.nflmeta.asked(game_id=101)[0] == _et(SUNDAY, 15, 0, 5)
    assert board.status(SUNDAY, 102) is GameStatus.FINAL


def test_a_game_still_being_played_past_midnight_is_read_under_its_own_date() -> None:
    board = _board(_et(SUNDAY, 23, 58, 55))
    board.nflmeta.rows[106] = _row(106, "KC", "LV", _et(SUNDAY, 20, 20), phase="in", period=5)
    board.run(_et(MONDAY, 0, 2, 5))
    assert board.nflmeta.asked() == [_et(SUNDAY, 23, 58, 55), _et(MONDAY, 0)]  # each date's own list
    live = [_et(SUNDAY, 23, 59) + n * 45 * S for n in range(5)]
    assert board.nflmeta.asked(phase="in", game_ids="106") == live  # read on, across midnight
    _end(board, 106)
    board.run(_et(MONDAY, 0, 5))
    assert board.nflmeta.asked(game_id=106) == [live[-1] + 50 * S]
    assert board.ids(SUNDAY) == ["106"] and board.status(SUNDAY, 106) is GameStatus.FINAL
    assert sorted(board.store.scopes, key=str) == [_scope(SUNDAY), _scope(MONDAY)]  # Saturday's has gone


# --- the feeds ----------------------------------------------------------------------------------------------


def _game(board: _Board, game_id: int) -> TeamGame:
    contest = next(each for each in board.store.contests if each.id.raw == str(game_id))
    assert isinstance(contest, TeamGame)
    return contest


def test_take_hands_over_each_answer_once_in_arrival_order_and_latest_the_newest() -> None:
    board = _board(_et(SUNDAY, 13))
    _sunday(board, CAR_ATL, PIT_CIN)
    _start(board, 101, 102)
    board.run(_et(SUNDAY, 13, 0, 55))
    pit = _game(board, 102)
    taken = board.planned.take(pit)
    assert [reading.arrived.utc for reading in taken] == [EARLY, EARLY + 5 * S, EARLY + 50 * S]  # list, then live
    assert board.planned.take(pit) == []  # each once
    latest = board.planned.latest(pit)
    assert latest is taken[-1] and latest.snapshot.state is not None
    board.run(_et(SUNDAY, 13, 1, 40))
    [newer] = board.planned.take(pit)
    assert board.planned.latest(pit) is newer and newer.arrived.utc == EARLY + 95 * S
    stranger = dataclasses.replace(pit, id=LeagueScopedId(League.NFL, NFLMETA_SOURCE, "999"))
    assert board.planned.take(stranger) == [] and board.planned.latest(stranger) is None


# --- failures and the quota ----------------------------------------------------------------------------------


def test_a_failing_read_is_the_date_s_failure_until_an_answer_follows_it() -> None:
    board = _board(_et(SUNDAY, 13))
    _sunday(board, PIT_CIN)
    board.nflmeta.down = True
    board.step()
    health = board.store.league_health(League.NFL, board.clock.monotonic())
    assert health is not None and health.status is ProviderStatus.NEVER_LOADED
    assert health.error == "ProviderError: NFLMeta fetch failed for /api/v1/live-scores: NFLMeta is down"
    board.nflmeta.down = False
    board.run(_et(SUNDAY, 13, 0, 10))
    _start(board, 102)
    board.run(_et(SUNDAY, 13, 0, 20))
    board.nflmeta.down = True
    board.run(_et(SUNDAY, 13, 3))
    [answer, failure] = board.reports[-1].observations
    assert failure.outcome is ScopeOutcome.FAILED and failure.received.monotonic > answer.received.monotonic
    assert board.health() is ProviderStatus.STALE  # two minutes past its last answer, with a read due
    board.nflmeta.down = False
    board.run(_et(SUNDAY, 13, 4))
    assert [seen.outcome for seen in board.reports[-1].observations] == [ScopeOutcome.COMPLETE]
    assert board.health() is ProviderStatus.FRESH


def test_once_free_is_spent_mid_afternoon_nfl_s_reads_are_refused_with_the_reason_and_its_health_goes_stale() -> None:
    board = _board(_et(SUNDAY, 14, 59), ledger=QuotaLedger(account_limits(NflMetaPlan.FREE)))
    _sunday(board, CAR_ATL, PIT_CIN)
    _start(board, 101, 102)
    loop = build_loop(
        board.planned, _no_feed, RecordingDisplaySink(PanelProfile.QUAD_128X64), quota=board.planned.held_back
    )
    _run_loop(loop, board, _et(SUNDAY, 15))
    board.nflmeta.requests_left = 10  # NFLMeta says the month is nearly spent (another board, an earlier run)
    _run_loop(loop, board, _et(SUNDAY, 15, 0, 10))
    live_reads = len(board.nflmeta.asked(phase="in", game_ids="102"))
    [league] = _run_loop(loop, board, _et(SUNDAY, 15, 0, 40)).leagues  # past the others' next read
    assert (league.source, league.reason) == (SourceHealth.FRESH, "quota: NFLMeta requests 0% left, normal held back")
    assert len(board.nflmeta.asked(phase="in", game_ids="102")) == live_reads  # the others wait; the favorite doesn't
    _run_loop(loop, board, _et(SUNDAY, 15, 3))
    assert board.ledger.exhausted(NFL, board.clock.read())
    asked = len(board.nflmeta.requests)
    refresh = _run_loop(loop, board, _et(SUNDAY, 15, 10))
    assert len(board.nflmeta.requests) == asked  # nothing is fetched once it's spent
    [league] = refresh.leagues
    assert league.source is SourceHealth.STALE
    assert league.reason == "no schedule for over 2m: quota: NFLMeta Free spent for 2026-11: switch to Builder"
    assert board.planned.held_back(_scope(SUNDAY)) == "quota: NFLMeta Free spent for 2026-11: switch to Builder"


def test_a_spent_nflmeta_month_leaves_mlb_s_reads_on_the_same_plan_untouched() -> None:
    clock = FakeClock(AFTER_MIDNIGHT)
    spent = MonthUsage(BillingMonth(2026, 6), requests=5_000)
    ledger = QuotaLedger(account_limits(), usage={NFL: spent})
    mlb = mlb_accounts(_FakeMlb.source)
    accounts = AccountMap(sources={**mlb.sources, **nfl_accounts().sources}, kinds=mlb.kinds)
    plan = PollPlan({**MLB_POLICIES, **NFL_POLICIES}, ledger=ledger, accounts=accounts)
    statsapi = _FakeMlb(clock)
    statsapi.slates = {TODAY: (_mlb_game("g1"),)}
    nflmeta = _NflMeta(clock)
    meter = Meter(ledger, NFL, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(nflmeta, meter))
    planned_nfl = PlannedNfl(plan, clock, provider)
    planned_mlb = PlannedMlb(plan, clock, statsapi)
    for _ in range(3):
        planned_mlb.observe(clock.read(), ())
        planned_nfl.observe(clock.read(), ())
        Poller(plan, clock).run_due()
        clock.advance(15 * S)
    assert nflmeta.requests == []  # NFL asks nothing
    assert [asked for day, asked in statsapi.asked if day == TODAY] == [AFTER_MIDNIGHT + n * 15 * S for n in range(3)]
    assert (
        planned_nfl.held_back(_scope(date(2026, 6, 18))) == "quota: NFLMeta Free spent for 2026-06: switch to Builder"
    )
    assert planned_mlb.held_back(PollScope(_FakeMlb.source, League.MLB, LocalDate(TODAY))) is None


def _no_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
    raise ProviderError("no baseball here")


def _run_loop(loop: AppLoop, board: _Board, until: datetime) -> Any:
    """The running board's passes every five seconds until just before ``until``: a refresh, the poller's asks,
    then a refresh that reads their answers. Returns the last refresh."""
    refresh = None
    while board.clock.now() < until:
        loop.refresh(board.clock.read())
        board.poller.run_due()
        refresh = loop.refresh(board.clock.read())
        board.clock.advance(5 * S)
    return refresh


# --- how the board is built -----------------------------------------------------------------------------------


def test_a_plan_without_nfl_s_reads_or_their_account_doesn_t_build_the_nfl_board() -> None:
    clock = FakeClock(EARLY)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=_NflMeta(clock))
    partial = PollPlan({kind: NFL_POLICIES[kind] for kind in (PollSource.LIVE_SCORES, PollSource.FOLLOWED_GAME)})
    with pytest.raises(ValueError, match="no policy for NFL's day_list, final_score asks"):
        PlannedNfl(partial, clock, provider)
    ledger = QuotaLedger(account_limits())
    elsewhere = AccountMap(sources={NFLMETA_SOURCE: NFL}, kinds={PollSource.FINAL_SCORE: ProviderAccount.BALLDONTLIE})
    with pytest.raises(ValueError, match="NFL's final_score asks spend NFLMeta's account, not BALLDONTLIE's"):
        PlannedNfl(PollPlan(NFL_POLICIES, ledger=ledger, accounts=elsewhere), clock, provider)
    plain = PlannedNfl(PollPlan(NFL_POLICIES), clock, provider)  # a plan with no ledger counts nothing
    assert plain.source == NFLMETA_SOURCE and plain.league is League.NFL
    for wrong in ({"fresh_for": timedelta(0)}, {"recheck_empty": -S}):
        with pytest.raises(ValueError, match="must be positive"):
            NflSchedulePolicy(**wrong)
