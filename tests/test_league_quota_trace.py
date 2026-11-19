"""Each league's quota in the decision trace on a line of its own: a spent NFLMeta month stays in view beside a
healthy MLB lane, and says so when the quota lets go."""

from __future__ import annotations

from datetime import timedelta

from omni.app.display import RecordingDisplaySink
from omni.app.planned_mlb import PlannedMlb, mlb_accounts
from omni.app.poller import Poller
from omni.app.quota import BillingMonth, MonthUsage, NflMetaPlan, QuotaLedger, account_limits
from omni.app.runner import NflSource, board_plan, build_planned_loop, planned_nfl_source
from omni.app.trace import DecisionTracer
from omni.core.clock import FakeClock
from omni.core.enum import League, PanelProfile
from omni.preferences.favorites import Favorites
from omni.tower.board import SourceHealth
from tests.test_app_loop import _game as _mlb_game
from tests.test_planned_mlb import AFTER_MIDNIGHT, TODAY, _FakeMlb
from tests.test_planned_nfl import CAR_ATL, NFL, S, SUNDAY, _board, _et, _NflMeta, _row


def _spent(requests: int) -> QuotaLedger:
    usage = {NFL: MonthUsage(BillingMonth(2026, 11), requests=requests)}
    return QuotaLedger(account_limits(NflMetaPlan.FREE), usage=usage)


def test_nfl_s_spent_month_is_traced_on_a_line_of_its_own_while_mlb_s_lane_is_healthy() -> None:
    clock = FakeClock(AFTER_MIDNIGHT)
    ledger = QuotaLedger(
        account_limits(NflMetaPlan.FREE), usage={NFL: MonthUsage(BillingMonth(2026, 6), requests=5_000)}
    )
    statsapi = _FakeMlb(clock)
    statsapi.slates = {TODAY: (_mlb_game("g1"),)}
    plan = board_plan(ledger=ledger, mlb=mlb_accounts(_FakeMlb.source), nfl=True)
    nfl = planned_nfl_source(plan, clock, _NflMeta(clock), ledger=ledger, favorites=Favorites({League.NFL: ["CAR"]}))
    planned = PlannedMlb(plan, clock, statsapi)
    loop = build_planned_loop(planned, RecordingDisplaySink(PanelProfile.QUAD_128X64), nfl=nfl)
    lines: list[str] = []
    tracer = DecisionTracer(lines.append)
    poller = Poller(plan, clock)
    for _ in range(3):
        loop.refresh(clock.read())
        poller.run_due()
        refresh = loop.refresh(clock.read())
        tracer.refreshed(refresh, took=timedelta(0), next_at=None)
        clock.advance(15 * S)
    assert refresh.health.source is SourceHealth.FRESH  # the board stands on MLB's healthy lane
    reason = "quota: NFLMeta Free spent for 2026-06: switch to Builder"
    assert [line for line in lines if "NFLMeta" in line] == [f"  nfl {reason}"]
    assert lines[0].startswith("refresh ") and "source fresh" in lines[0]


def test_nfl_s_quota_line_says_so_when_the_quota_lets_go() -> None:
    board = _board(_et(SUNDAY, 12), ledger=_spent(4_800), favorites=Favorites())
    board.nflmeta.rows[101] = _row(*CAR_ATL)
    nfl = NflSource(schedule=board.planned, feeds=board.planned, held_back=board.planned.held_back)
    loop = build_planned_loop(None, RecordingDisplaySink(PanelProfile.QUAD_128X64), nfl=nfl)
    lines: list[str] = []
    tracer = DecisionTracer(lines.append)

    def refresh() -> None:
        loop.refresh(board.clock.read())
        board.poller.run_due()
        tracer.refreshed(loop.refresh(board.clock.read()), took=timedelta(0), next_at=None)

    refresh()
    assert lines[-1] == "  nfl quota: NFLMeta requests 4% left, normal held back"
    board.clock.advance(timedelta(days=16))  # December: a new month, and nothing spent of it
    refresh()
    assert lines[-1] == "  nfl quota: clear"
