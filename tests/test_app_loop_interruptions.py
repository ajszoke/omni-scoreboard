"""Interruptions on the shared board, through the production builders (`build_planned_loop`, `build_loop`):
`BOARD_POLICY.md` §6, "Wake or output loss".

- **A host's sleep**, its monotonic clock frozen while the wall and suspend-inclusive clocks run 49 minutes: an ask
  begun before it and answered after it is rejected; the live field is drawn stale from the wake; a first fresh
  answer that fails changes nothing; a complete one is held its full delay, its plays a baseline (nothing
  historical flashes), and then the field is current again. The first frame after it is traced.
- **A stall** past the health tick is taken in the same way; the loop wakes at least every health tick.
- **A paint before the refresh** after a gap airs nothing from the input published before it.
- **Wall-clock corrections** either way are no interruption.
- **An output loss**, its pixels unchanged: the card's time ends at the receiver's last word, the frame goes out
  again in a new session, a late word from the old one renews nothing, and the time until the new word is unknown
  display time. A receiver that restarts with a frame in flight is credited only for the new session's frame.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from omni.app.display import RecordingDisplaySink
from omni.app.frame_stream import DisplayLink, LinkProtocol, StreamDisplaySink, StreamFrame
from omni.app.loop import AppLoop, Painted
from omni.app.planned_mlb import MLB_POLICIES, PlannedMlb
from omni.app.planned_nfl import NFL_POLICIES, PlannedNfl, nfl_accounts
from omni.app.poll_plan import PollPlan
from omni.app.poller import Poller
from omni.app.quota import HttpAnswer, Meter, MeteredHttp, NflMetaPlan, ProviderAccount, QuotaLedger, account_limits
from omni.app.quota import nflmeta_billed
from omni.app.runner import build_loop, build_planned_loop, warm_up
from omni.app.trace import DecisionTracer
from omni.cards.base import CardKind, ScoreboardCard
from omni.core.clock import FakeClock, MonotonicTime
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent, LiveBaseballFeed
from omni.tower.board import FrameKind
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from omni.tower.interruption import Interruption, InterruptionPolicy, InterruptionReason
from omni.tower.lane import ChangeKind, EndReason
from tests.test_app_loop_board import QUAD, T, _at, _Slate, _state
from tests.test_app_loop import _game
from tests.test_planned_mlb import AFTER_MIDNIGHT, TODAY, _FakeMlb
from tests import test_planned_nfl as nfl
from tests.test_renderer_big_play import _event

LAG = DurationSeconds(45)
WATCHED = InterruptionPolicy()


def _kinds(painted: Painted) -> list[tuple[ChangeKind, EndReason | None]]:
    return [
        (change.kind, change.reason)
        for change in painted.changes
        if isinstance(change.segment.frames[0], ScoreboardCard)
    ]


# --- a host's sleep, through the planned MLB board ---------------------------------------------------------------


class _Asleep(_FakeMlb):
    """StatsAPI that can be told to sleep mid-request: its next game feed ask begins before the host sleeps and
    returns after it wakes, by which time the board's refresh has noticed; its feed fails from then until told
    otherwise. Its answers carry ``events``."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.sleep_during: AppLoop | None = None
        self.events: tuple[BaseballGameEvent, ...] = ()

    def fetch_live_feed(self, game: TeamGame, *, now: datetime) -> LiveBaseballFeed:
        answer = super().fetch_live_feed(game, now=now)
        loop, self.sleep_during = self.sleep_during, None
        if loop is not None:
            self.clock.suspend(timedelta(minutes=49))
            self.clock.jump(timedelta(seconds=-20))  # NTP corrects the wall clock as the host wakes
            loop.refresh(self.clock.read())
            self.feed_failing = {game.id.raw}  # the first fresh answers fail
        return dataclasses.replace(answer, events=self.events)


class _Board:
    """The running MLB board on a fake Linux host that can sleep: the plan, its poller and the loop, stepped
    together, each paint kept with its time in seconds on the monotonic clock."""

    def __init__(self) -> None:
        self.clock = FakeClock(AFTER_MIDNIGHT, suspend_inclusive=MonotonicTime(0.0))
        self.provider = _Asleep(self.clock)
        self.provider.slates = {TODAY: (_game("g1"),)}
        plan = PollPlan(MLB_POLICIES)
        planned = PlannedMlb(plan, self.clock, self.provider)
        self.poller = Poller(plan, self.clock)
        self.loop = build_planned_loop(planned, RecordingDisplaySink(QUAD), broadcast_lag=LAG, interruptions=WATCHED)
        warm_up(self.loop, self.poller, self.clock)
        self.painted: list[tuple[float, Painted]] = []

    def seconds(self) -> float:
        return self.clock.monotonic().seconds

    def run(self, until: float) -> None:
        while self.seconds() < until:
            self.clock.advance(timedelta(seconds=1))
            self.poller.run_due()
            self.loop.refresh(self.clock.read())
            self.painted.append((self.seconds(), self.loop.paint(self.clock.read())))


def test_after_a_49_minute_sleep_what_was_asked_before_it_is_rejected_and_the_field_is_stale_until_a_complete_reading_clears_its_full_delay() -> (  # noqa: E501
    None
):
    board = _Board()
    asleep = board.provider
    board.run(60)
    assert board.painted[-1][1].shown is not None and not board.painted[-1][1].stale  # the field is current
    asleep.states["g1"] = _state(9, 9)  # what the feed says just before the sleep
    asleep.sleep_during = board.loop
    board.run(80)  # the feed's next ask begins, the host sleeps, and its answer returns after the wake
    [(woke, interrupted)] = [(at, paint) for at, paint in board.painted if paint.interrupted]
    [interruption] = interrupted.interrupted
    assert interruption.reason is InterruptionReason.HOST_SUSPEND and interruption.clock_trusted
    assert interruption.gap == timedelta(minutes=49, seconds=1)  # the sleep, and the second's step before it
    assert (ChangeKind.DOWN, EndReason.INTERRUPTED) in _kinds(interrupted)
    shown = [paint for at, paint in board.painted if at >= woke and paint.shown is not None]
    assert shown and all(paint.stale for paint in shown)  # the field from before the sleep, dimmed
    assert all(paint.stale for at, paint in board.painted if at >= woke)
    board.run(woke + 20)
    asleep.feed_failing = set()
    asleep.events = (_event(eid="g1:ab:9", away_score=9, home_score=9),)  # a play from the sleep: history now
    board.run(woke + 120)
    fresh = [at for at, paint in board.painted if at > woke and paint.shown is not None and not paint.stale]
    assert fresh and fresh[0] >= woke + 20 + 45  # a complete answer after the failure, released its full delay later
    drawn = [paint for at, paint in board.painted if at >= fresh[0]]
    assert all(not paint.stale for paint in drawn)
    cards = [change.segment.frames[0] for _, paint in board.painted for change in paint.changes]
    assert not [card for card in cards if isinstance(card, ScoreboardCard) and card.kind is CardKind.BIG_PLAY]
    resumed = [(at, paint.resumed) for at, paint in board.painted if paint.resumed is not None]
    assert resumed == [(woke, interruption)]  # the first frame up after the sleep, traced


# --- a stall, the health tick, the order of the passes -------------------------------------------------------


def _watched(lag: int = 45) -> tuple[AppLoop, _Slate]:
    slate = _Slate("atl-nym")
    loop = build_loop(
        slate, slate.feed, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(lag), interruptions=WATCHED
    )
    return loop, slate


def _step(loop: AppLoop, seconds: float) -> Painted:
    loop.refresh(_at(seconds))
    return loop.paint(_at(seconds))


def test_a_stall_past_the_health_tick_cuts_the_card_at_the_last_pass_and_dims_the_field_until_a_fresh_state_clears() -> (
    None
):
    loop, slate = _watched()
    painted = [_step(loop, seconds) for seconds in range(0, 61)]
    assert painted[-1].shown is not None and not painted[-1].stale
    slate.scores["atl-nym"] = (4, 2)
    stalled = _step(loop, 100)  # 40 s since the last pass: 35 past its health tick
    [interruption] = stalled.interrupted
    assert interruption.reason is InterruptionReason.PROCESS_STALL
    [down, up] = stalled.changes
    assert (down.kind, down.reason, down.at.monotonic) == (ChangeKind.DOWN, EndReason.INTERRUPTED, MonotonicTime(60))
    assert up.kind is ChangeKind.UP and stalled.stale and stalled.resumed is interruption
    for seconds in range(101, 145):
        assert _step(loop, seconds).stale  # the score answered at 100 waits its 45 s
    current = _step(loop, 145)
    assert not current.stale and current.interrupted == ()
    lines: list[str] = []
    DecisionTracer(lines.append).painted(stalled)
    assert lines[0].endswith(": process stall 1 (40.0s, clock trusted) | last swap - | 0 in flight")
    assert lines[-1].endswith(": the first frame since process stall 1 | unknown display 0.0s so far")


def test_without_a_suspend_inclusive_clock_a_wall_clock_far_ahead_is_taken_in_as_a_sleep_with_its_clock_untrusted() -> (
    None
):
    slate = _Slate("atl-nym")
    clock = FakeClock(T)  # a host with no suspend-inclusive clock
    loop = build_loop(
        slate, slate.feed, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(0), interruptions=WATCHED
    )
    loop.refresh(clock.read())
    loop.paint(clock.read())
    clock.suspend(timedelta(minutes=49))
    loop.refresh(clock.read())
    painted = loop.paint(clock.read())
    [interruption] = painted.interrupted
    assert interruption.reason is InterruptionReason.HOST_SUSPEND and not interruption.clock_trusted
    lines: list[str] = []
    DecisionTracer(lines.append).painted(painted)
    assert lines[0].endswith(": host suspend 1 (2940.0s, clock untrusted) | last swap - | 0 in flight")


def test_watching_for_interruptions_the_loop_wakes_at_least_every_health_tick() -> None:
    loop, _ = _watched()
    _step(loop, 0)
    assert loop.next_deadline(_at(0)) is not None and loop.next_deadline(_at(0)) <= MonotonicTime(5)  # type: ignore[operator]
    slate = _Slate()
    quiet = build_loop(slate, slate.feed, RecordingDisplaySink(QUAD))
    _step(quiet, 0)
    deadline = quiet.next_deadline(_at(0))
    assert deadline is None or deadline > MonotonicTime(5)  # a board not watching has no such tick


def test_a_paint_that_takes_in_a_gap_before_the_refresh_does_airs_nothing_published_before_it() -> None:
    loop, _ = _watched(lag=0)
    for seconds in range(0, 11):
        _step(loop, seconds)
    held = loop.paint(_at(60))  # the frame thread notices first
    assert held.frame is FrameKind.HELD and held.interrupted and held.resumed is held.interrupted[0]
    loop.refresh(_at(60.1))
    assert loop.paint(_at(60.2)).shown is not None  # once a refresh after the gap has published


def test_wall_clock_corrections_either_way_are_no_interruption() -> None:
    slate = _Slate("atl-nym")
    clock = FakeClock(T, suspend_inclusive=MonotonicTime(0.0))
    loop = build_loop(
        slate, slate.feed, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(0), interruptions=WATCHED
    )
    for jump in (timedelta(minutes=10), timedelta(minutes=-10), timedelta(hours=-2)):
        clock.jump(jump)
        clock.advance(timedelta(seconds=1))
        loop.refresh(clock.read())
        painted = loop.paint(clock.read())
        assert painted.interrupted == () and painted.shown is not None


# --- an output loss --------------------------------------------------------------------------------------------


class _Receiver:
    """A board's receiver with sessions, over a link the test drives."""

    def __init__(self) -> None:
        self.now = 0.0
        self.link = DisplayLink(monotonic=lambda: self.now)
        self.link.started(1)
        self.link.ready(1)
        self.link.announced(1, "boot-a")
        self.sent: list[tuple[int, StreamFrame]] = []  # each frame, with the session it went to
        self.sink = StreamDisplaySink(QUAD, self._write, self.link, monotonic=lambda: self.now)

    @property
    def session(self) -> int:
        protocol, session = self.link.protocol(1)
        assert protocol is LinkProtocol.SESSIONS
        return session

    def _write(self, frame: StreamFrame) -> bool:
        self.sent.append((self.session, frame))
        return True

    def show(self, sent: tuple[int, StreamFrame], *, at: float) -> None:
        self.now = at
        self.link.shown(1, sent[1].id, session=sent[0])

    def alive(self, *, at: float) -> None:
        self.now = at
        self.link.alive(1, self.session)


def _on(receiver: _Receiver) -> AppLoop:
    slate = _Slate("atl-nym")
    loop = build_loop(slate, slate.feed, receiver.sink, broadcast_lag=DurationSeconds(0), interruptions=WATCHED)
    loop.refresh(_at(0))
    return loop


def _paint(loop: AppLoop, receiver: _Receiver, seconds: float) -> Painted:
    """A frame at ``seconds``. The cards were staged once (a refresh restages a live card as a new version, and a
    frame drawn from an older one books nothing, as the board's input rules say)."""
    receiver.now = seconds
    return loop.paint(_at(seconds))


def test_an_output_loss_with_unchanged_pixels_ends_the_card_at_the_last_word_and_counts_the_gap_as_unknown() -> None:
    receiver = _Receiver()
    loop = _on(receiver)
    _paint(loop, receiver, 0)
    receiver.show(receiver.sent[-1], at=0.5)
    up = _paint(loop, receiver, 1)
    assert [kind for kind, _ in _kinds(up)] == [ChangeKind.UP]
    for seconds in (2, 4):
        receiver.alive(at=seconds)  # the receiver answers the board's heartbeats
        _paint(loop, receiver, seconds)
    old = receiver.sent[-1]
    receiver.now = 6
    receiver.link.blanked(1, receiver.session)  # its watchdog blanked the panel
    lost = _paint(loop, receiver, 6)
    [interruption] = lost.interrupted
    assert interruption.reason is InterruptionReason.OUTPUT_LOSS and interruption.before.monotonic == MonotonicTime(4)
    assert (interruption.old_session, interruption.new_session) == (old[0], old[0] + 1)
    [down] = [change for change in lost.changes if change.kind is ChangeKind.DOWN]
    assert (down.reason, down.aired) == (EndReason.INTERRUPTED, timedelta(seconds=3.5))  # from 0.5 to the last word
    again = receiver.sent[-1]
    assert again[0] == old[0] + 1 and again[1].pixels == old[1].pixels  # the same pixels go out, to a new session
    receiver.show(old, at=6.5)  # the old session's word, late
    assert _paint(loop, receiver, 7).confirmed == ()  # renews nothing
    receiver.show(again, at=7.5)
    back = _paint(loop, receiver, 8)
    assert back.confirmed[0] == again[1].id and back.resumed is interruption
    assert back.unknown_display == timedelta(seconds=3.5)  # from the last word, at 4, to the new one, at 7.5
    lines: list[str] = []
    tracer = DecisionTracer(lines.append)
    tracer.painted(lost)
    tracer.painted(back)
    assert lines[0].startswith("interrupted ") and "output loss 1 (2.0s since its last word)" in lines[0]
    assert lines[-1].endswith(": the first frame since output loss 1 | unknown display 3.5s so far")


def test_a_receiver_that_restarts_with_a_frame_in_flight_is_credited_only_for_its_new_session_s_frame() -> None:
    receiver = _Receiver()
    loop = _on(receiver)
    _paint(loop, receiver, 0)
    in_flight = receiver.sent[-1]
    receiver.now = 0.2
    receiver.link.announced(1, "boot-b")  # it restarted before it said so: a new session, nothing yet shown
    _paint(loop, receiver, 0.3)
    receiver.show(in_flight, at=0.4)  # its word on the old session's frame comes late
    assert _paint(loop, receiver, 0.5).changes == ()  # no invented credit
    fresh = receiver.sent[-1]
    assert fresh[0] == in_flight[0] + 1
    receiver.show(fresh, at=0.6)
    up = _paint(loop, receiver, 0.7)
    assert up.confirmed[0] == fresh[1].id and [kind for kind, _ in _kinds(up)] == [ChangeKind.UP]
    assert up.presented is not None and up.presented.at == T + timedelta(seconds=0.6)
    receiver.alive(at=1.0)
    receiver.now = 1.5
    receiver.link.announced(1, "boot-c")  # and again, now while it shows the card
    restarted = _paint(loop, receiver, 1.5)
    assert [i.reason for i in restarted.interrupted] == [InterruptionReason.OUTPUT_LOSS]
    trace: list[str] = []
    DecisionTracer(trace.append).painted(restarted)
    assert "| 0 in flight" in trace[0] and "| last swap " in trace[0]


# --- NFL's reads across a host's gap ---------------------------------------------------------------------------


class _SleepyNflMeta(nfl._NflMeta):
    """NFLMeta whose next request is begun before the host sleeps and answered after the board took the sleep in."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.during: Callable[[], None] | None = None

    def __call__(self, path: str, query: Mapping[str, str | int]) -> HttpAnswer:
        answer = super().__call__(path, query)
        during, self.during = self.during, None
        if during is not None:
            during()
        return answer


def test_after_a_sleep_nfl_s_read_begun_before_it_files_nothing_and_each_read_is_wanted_again_at_once() -> None:
    clock = FakeClock(nfl.EARLY, suspend_inclusive=MonotonicTime(0.0))
    nflmeta = _SleepyNflMeta(clock)
    ledger = QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = PollPlan(NFL_POLICIES, ledger=ledger, accounts=nfl_accounts())
    meter = Meter(ledger, ProviderAccount.NFLMETA, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(nflmeta, meter))
    planned = PlannedNfl(plan, clock, provider, favorites=nfl.CAR)
    board = nfl._Board(planned, plan, clock, nflmeta, ledger, Poller(plan, clock))
    nfl._sunday(board, nfl.CAR_ATL, nfl.PIT_CIN)
    nfl._start(board, 101, 102)
    board.run(nfl._et(nfl.SUNDAY, 13, 1))
    car, pit = nfl._game(board, 101), nfl._game(board, 102)
    planned.take(car), planned.take(pit)
    clock.advance(timedelta(seconds=5))  # the favorite's next read is due
    before = clock.read()
    taken: list[Interruption] = []

    def sleep() -> None:  # mid-request: the host sleeps 49 minutes, and the board takes it in as it wakes
        clock.suspend(timedelta(minutes=49))
        taken.append(Interruption(serial=1, reason=InterruptionReason.HOST_SUSPEND, before=before, after=clock.read()))
        contests = [contest.id for contest in board.store.contests]
        planned.interrupted(taken[0], held=board.store.scopes, contests=contests)

    nflmeta.during = sleep
    nflmeta.set(101, home_score=7)  # what the read in flight brings back: from before the sleep
    board.poller.run_due()
    assert nflmeta.during is None and taken  # the read was out across the sleep
    assert planned.take(car) == [] and planned.take(pit) == []  # it files nothing, and nothing untaken stays
    asked = len(nflmeta.requests)
    board.step()  # the next pass wants every read afresh, due at once, within the account's budget
    again = {(path, tuple(query.items())) for _, path, query in nflmeta.requests[asked:]}
    assert (nfl.SCORES, (("game_id", 101),)) in again  # the favorite's game, at once rather than 10 s on
    fresh = planned.take(car)
    assert fresh and fresh[-1].arrived == clock.read() and fresh[-1].snapshot.state is not None


def _nfl_board(start: datetime) -> tuple[nfl._Board, _SleepyNflMeta]:
    clock = FakeClock(start, suspend_inclusive=MonotonicTime(0.0))
    nflmeta = _SleepyNflMeta(clock)
    ledger = QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = PollPlan(NFL_POLICIES, ledger=ledger, accounts=nfl_accounts())
    meter = Meter(ledger, ProviderAccount.NFLMETA, clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(nflmeta, meter))
    planned = PlannedNfl(plan, clock, provider, favorites=nfl.CAR)
    return nfl._Board(planned, plan, clock, nflmeta, ledger, Poller(plan, clock)), nflmeta


def _sleep_during_next_read(board: nfl._Board, nflmeta: _SleepyNflMeta) -> None:
    """The next request is out when the host sleeps 49 minutes; the board takes the sleep in as it wakes."""
    before = board.clock.read()

    def sleep() -> None:
        board.clock.suspend(timedelta(minutes=49))
        interruption = Interruption(
            serial=1, reason=InterruptionReason.HOST_SUSPEND, before=before, after=board.clock.read()
        )
        contests = [contest.id for contest in board.store.contests]
        board.planned.interrupted(interruption, held=board.store.scopes, contests=contests)

    nflmeta.during = sleep


def test_a_day_list_out_across_a_sleep_lists_nothing() -> None:
    board, nflmeta = _nfl_board(nfl._et(nfl.SUNDAY, 12))
    nfl._sunday(board, nfl.CAR_ATL, nfl.PIT_CIN)
    board.observe()  # the day list is wanted
    _sleep_during_next_read(board, nflmeta)
    board.poller.run_due()
    assert nflmeta.during is None and board.observe().observations == ()  # nothing listed from before the sleep


def test_the_others_live_read_out_across_a_sleep_files_nothing() -> None:
    board, nflmeta = _nfl_board(nfl._et(nfl.SUNDAY, 13))
    nfl._sunday(board, nfl.PIT_CIN, nfl.MIN_GB)  # no favorite: both read together
    nfl._start(board, 102, 103)
    board.run(nfl._et(nfl.SUNDAY, 13, 0, 40))
    pit = nfl._game(board, 102)
    board.planned.take(pit)
    board.clock.advance(timedelta(seconds=10))  # their next read together is due
    _sleep_during_next_read(board, nflmeta)
    board.poller.run_due()
    assert nflmeta.during is None and board.planned.take(pit) == []


def test_a_final_read_out_across_a_sleep_files_nothing() -> None:
    board, nflmeta = _nfl_board(nfl._et(nfl.SUNDAY, 13))
    nfl._sunday(board, nfl.PIT_CIN, nfl.MIN_GB)
    nfl._start(board, 102, 103)
    board.run(nfl._et(nfl.SUNDAY, 13, 0, 50))
    nflmeta.hidden = {102}  # the live read stops listing it: it has ended, and its final is read next
    board.clock.advance(timedelta(seconds=45))
    board.step()
    pit = nfl._game(board, 102)
    board.planned.take(pit)
    nfl._end(board, 102)
    board.observe()  # its final read is wanted, due at once
    _sleep_during_next_read(board, nflmeta)
    board.poller.run_due()
    assert nflmeta.during is None and board.planned.take(pit) == []
