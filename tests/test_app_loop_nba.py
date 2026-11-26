"""The board with NBA on it: `build_planned_loop` with NBA alone and beside MLB, its cards through the shared queue,
every confirmed frame handed to NBA's pipeline, its deadlines in the loop's, and a host's sleep taken in by it too."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop
from omni.app.nba_pipeline import LiveBasketballPipeline
from omni.app.planned_mlb import PlannedMlb, mlb_accounts
from omni.app.poller import Poller
from omni.app.quota import BalldontliePlan, QuotaLedger, account_limits
from omni.app.runner import NbaSource, board_plan, build_planned_loop, planned_nba_source, warm_up
from omni.cards.base import CardKind
from omni.core.clock import FakeClock, MonotonicTime
from omni.core.enum import PanelProfile
from omni.core.time import DurationSeconds
from omni.tower.interruption import InterruptionPolicy
from tests.test_planned_mlb import _FakeMlb
from tests.test_planned_nba import WEDNESDAY, _Bdl, _et, _line, _live, _tip

ALL_STAR = BalldontliePlan.ALL_STAR
S = timedelta(seconds=1)
LIVE_KEY = "nba:balldontlie:1:live"


@dataclass(slots=True)
class _Board:
    clock: FakeClock
    loop: AppLoop
    poller: Poller

    def up(self) -> None:
        """Warm up, then step past NBA's 10 s delay: the game's live card is staged."""
        warm_up(self.loop, self.poller, self.clock)
        self.clock.advance(11 * S)
        self.poller.run_due()
        self.loop.refresh(self.clock.read())

    @property
    def basketball(self) -> LiveBasketballPipeline:
        pipeline = self.loop._basketball
        assert pipeline is not None
        return pipeline


def _board(clock: FakeClock, *, mlb: bool = False, interruptions: InterruptionPolicy | None = None) -> _Board:
    """A board with NBA on it, UTA at LAL under way, behind a 10 s delay; with ``mlb``, MLB's source beside it."""
    bdl = _Bdl(clock, days={WEDNESDAY.isoformat(): [_live(1, "UTA", "LAL", _tip(WEDNESDAY, 19))]})
    bdl.stats[1] = [_line(10, 100, 1, 2)]
    ledger = QuotaLedger(account_limits(balldontlie=ALL_STAR))
    fake = _FakeMlb(clock) if mlb else None
    plan = board_plan(
        ledger=ledger, mlb=mlb_accounts(fake.source) if fake is not None else None, nfl=False, nba=ALL_STAR
    )
    planned = PlannedMlb(plan, clock, fake) if fake is not None else None
    nba = NbaSource.of(planned_nba_source(plan, clock, bdl, ledger=ledger, tier=ALL_STAR, log=lambda line: None))
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_planned_loop(planned, sink, nba=nba, nba_delay=DurationSeconds(10), interruptions=interruptions)
    return _Board(clock, loop, Poller(plan, clock))


def test_a_board_with_nba_alone_cards_its_game_after_nba_s_own_delay() -> None:
    clock = FakeClock(_et(WEDNESDAY, 19, 30))
    board = _board(clock)
    warm_up(board.loop, board.poller, clock)
    assert board.loop.refresh(clock.read()).pipeline.held
    deadline = board.loop.refresh_deadline(clock.read())
    assert deadline is not None and deadline <= clock.monotonic() + 10 * S
    clock.advance(11 * S)
    board.poller.run_due()
    refresh = board.loop.refresh(clock.read())
    assert [card.raw for card in refresh.pipeline.ingested] == [LIVE_KEY]
    painted = board.loop.paint(clock.read())
    assert painted.shown is not None and painted.shown.raw == LIVE_KEY


def test_beside_mlb_a_frame_of_nba_s_card_reaches_its_panel() -> None:
    clock = FakeClock(_et(WEDNESDAY, 19, 30))
    board = _board(clock, mlb=True)
    board.up()
    painted = board.loop.paint(clock.read())
    assert painted.shown is not None and painted.shown.raw == LIVE_KEY
    shown = clock.monotonic()
    clock.advance(S)
    board.loop.refresh(clock.read())
    (live,) = board.basketball._live.values()
    assert live.panel._on_air and live.panel._since == shown


def test_a_host_s_sleep_is_taken_in_by_nba_s_pipeline_too() -> None:
    clock = FakeClock(_et(WEDNESDAY, 19, 30), suspend_inclusive=MonotonicTime(0.0))
    board = _board(clock, interruptions=InterruptionPolicy())
    board.up()
    board.loop.paint(clock.read())
    clock.suspend(timedelta(minutes=30))
    clock.advance(S)
    board.loop.refresh(clock.read())
    (live,) = board.basketball._live.values()
    assert live.panel._occurrence is None  # its panel's turn is history


def test_a_frame_of_any_other_card_ends_the_live_card_s_turn_on_its_panel() -> None:
    clock = FakeClock(_et(WEDNESDAY, 19, 30))
    board = _board(clock)
    board.up()
    card = board.loop._queue.card(LIVE_KEY)
    assert card is not None
    board.basketball.presented(card, clock.read(), occurrence=7)
    board.basketball.presented(replace(card, kind=CardKind.FINAL), clock.read(), occurrence=8)
    board.loop.refresh(clock.read())
    (live,) = board.basketball._live.values()
    assert not live.panel._on_air
