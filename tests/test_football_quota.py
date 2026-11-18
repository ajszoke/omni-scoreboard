"""The NFL runner on the quota ledger: each NFLMeta request and row counted, and a month that runs out."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from omni.app.__main__ import _parse_args as _parse_mlb_args
from omni.app.display import RecordingDisplaySink
from omni.app.football import _parse_args
from omni.app.football_board import FootballBoard
from omni.app.football_feed import GameFeed
from omni.app.quota import (
    AccountLimits,
    HttpAnswer,
    MeteredHttp,
    Meter,
    MonthUsage,
    BillingMonth,
    ProviderAccount,
    QuotaLedger,
    Refusal,
    account_limits,
    nflmeta_billed,
)
from omni.core.clock import FakeClock
from omni.core.enum import PanelProfile
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from tests.test_football_board import _full
from tests.test_nflmeta import GAME, T0, FakeFeed, _change, _page, _scores, _synced

S = timedelta(seconds=1)
NFL = ProviderAccount.NFLMETA


class _Billing:
    """NFLMeta as a fake: answers from a `FakeFeed`, with the rows header a real response carries (one a row
    returned, plus one an injury listed on the scoreboard)."""

    def __init__(self, feed: FakeFeed) -> None:
        self.feed = feed
        self.billed: list[int] = []

    def __call__(self, path: str, query: Mapping[str, str | int]) -> HttpAnswer:
        body = self.feed(path, query)
        rows = body["data"]
        billed = len(rows) + sum(len(row.get("injuries") or ()) for row in rows if isinstance(row, dict))
        self.billed.append(billed)
        return HttpAnswer(body, {"x-nflmeta-response-rows": str(billed)})


def _feed() -> FakeFeed:
    feed = FakeFeed()
    injury = {"player_name": "A. Player", "team_abbr": "PIT", "injury": "Knee", "state": "questionable_to_return"}
    feed.scores = [_scores(_synced("p1", injuries=[injury]))]
    feed.plays = [_page(_change("p1", 1, "C.Johnston punts 44 yards to CIN 18.", play_type="punt"))]
    return feed


def _board(ledger: QuotaLedger, nflmeta: _Billing, clock: FakeClock) -> tuple[FootballBoard, list[str]]:
    meter = Meter(ledger, NFL, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(nflmeta, meter))
    lines: list[str] = []
    feed = GameFeed(provider, GAME, ledger=ledger)
    return FootballBoard(feed, RecordingDisplaySink(PanelProfile.QUAD_128X64), log=_full(lines)), lines


def _run(board: FootballBoard, clock: FakeClock, seconds: int) -> None:
    for _ in range(seconds):
        board.step(clock.read())
        clock.advance(S)


def test_each_nflmeta_request_and_the_rows_its_headers_say_it_billed_are_counted() -> None:
    clock = FakeClock(T0)
    ledger = QuotaLedger(account_limits())
    nflmeta = _Billing(_feed())
    board, _ = _board(ledger, nflmeta, clock)
    _run(board, clock, 60)
    assert len(nflmeta.feed.requests) == 7  # the first look at the scores and the plays, then the scores each 10 s
    assert nflmeta.billed == [2, 1, 2, 2, 2, 2, 2]  # the game and its injury, the play
    assert ledger.usage(NFL, clock.read()) == MonthUsage(BillingMonth(2026, 11), requests=7, rows=13)


def test_once_the_month_is_spent_the_feed_stops_asking_and_the_card_goes_stale_naming_the_quota() -> None:
    clock = FakeClock(T0)
    spent = MonthUsage(BillingMonth(2026, 11), requests=4_995)  # carried over from earlier runs: five left
    ledger = QuotaLedger(account_limits(), usage={NFL: spent})
    nflmeta = _Billing(_feed())
    board, lines = _board(ledger, nflmeta, clock)
    _run(board, clock, 600)
    assert len(nflmeta.feed.requests) == 5  # the first look (two), then three more polls: none after
    assert ledger.exhausted(NFL, clock.read())
    quota = [line for line in lines if "quota" in line]
    assert quota == [
        "feed: quota: NFLMeta requests exhausted",  # once, however often it's held back
        "feed stale: no answer (quota: NFLMeta requests exhausted); dimming the card until it answers",
    ]
    assert lines.index(quota[1]) > lines.index(quota[0])


def test_a_minute_that_s_full_holds_the_ask_only_until_it_has_room() -> None:
    clock = FakeClock(T0)
    ledger = QuotaLedger({NFL: AccountLimits(per_minute=3)})
    nflmeta = _Billing(_feed())
    meter = Meter(ledger, NFL, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(nflmeta, meter))
    feed = GameFeed(provider, GAME, ledger=ledger)
    assert feed.ask(clock.read()).snapshot is not None  # the scores and the plays: two of three
    clock.advance(10 * S)
    assert feed.ask(clock.read()).snapshot is not None  # three of three
    clock.advance(10 * S)
    held = feed.ask(clock.read())
    assert held.snapshot is None and held.refused is not None and held.refused.refusal is Refusal.PER_MINUTE
    assert held.error == "quota: NFLMeta 3 of 3 a minute used, focus waiting"
    assert held.next_ask == clock.now() + 40 * S  # when the first look leaves the minute
    clock.advance(39 * S)
    assert not feed.due(clock.read())
    clock.advance(S)
    assert feed.due(clock.read()) and feed.ask(clock.read()).snapshot is not None
    assert len(nflmeta.feed.requests) == 4


def test_both_runners_take_the_nflmeta_plan_free_by_default_and_a_state_directory() -> None:
    football = _parse_args(["--profile", "quad_128x64", "--teams", "PIT,CIN", "--sink-command", "cat"])
    assert (football.nflmeta_plan, football.state_dir) == ("free", None)
    chosen = _parse_args(
        ["--profile", "quad_128x64", "--teams", "PIT,CIN", "--emulated"]
        + ["--nflmeta-plan", "builder", "--state-dir", "/srv/omni"]
    )
    assert (chosen.nflmeta_plan, chosen.state_dir) == ("builder", Path("/srv/omni"))
    mlb = _parse_mlb_args(["--profile", "quad_128x64", "--emulated"])
    assert (mlb.nflmeta_plan, mlb.state_dir) == ("free", None)
