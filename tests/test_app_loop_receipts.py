"""The shared board on a display that confirms what it shows (a board's receiver), through `build_loop`'s `AppLoop`.

A frame is sent with an id, and a card's first frame books its turn only when the receiver confirms that id, at the
instant its word was heard; a frame replaced before it was confirmed is never credited; a lost panel clears what was
in flight. A first frame landing after the card's latest start is an accidental display, booked as nothing. A
receiver that never confirms (an older one, credited on its own say-so) doesn't count as confirmed visibility. A
display whose commit shows the frame (the emulator) keeps booking as the commit returns.
"""

from __future__ import annotations

from datetime import timedelta

from omni.app.contest_store import ContestStore
from omni.app.frame_stream import DisplayLink, StreamDisplaySink, StreamFrame, stream_display
from omni.app.loop import UNSHOWN_AFTER, AppLoop, Painted
from omni.app.pipeline import LiveBaseballPipeline
from omni.app.runner import build_loop
from omni.app.supervisor import ProviderSupervisor
from omni.cards.base import ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.time import DurationSeconds
from omni.policy.settings import PolicySettings
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue, SelectionReason
from omni.renderers.registry import default_registry
from omni.tower.board import BoardNote
from omni.tower.lane import ChangeKind, EndReason
from tests.test_app_loop_board import QUAD, T, _at, _card, _Slate
from tests.test_frame_writer import _Process, _Spawner, _until

ATL = "mlb:mlb_statsapi:atl-nym:live"
BOS = "mlb:mlb_statsapi:bos-nyy:live"


class _Receiver:
    """A board's receiver, over a link the test drives: the frames sent to it are kept, and the test says which it
    showed and when (on the same clock as the board's readings: seconds in). An older one never confirms."""

    def __init__(self, *, confirms: bool = True) -> None:
        self.now = 0.0
        self.link = DisplayLink(monotonic=lambda: self.now)
        self.link.started(1)
        if confirms:
            self.link.ready(1)
        self.sent: list[StreamFrame] = []
        self.taking = True  # its writer takes frames
        self.sink = StreamDisplaySink(QUAD, self._write, self.link, monotonic=lambda: self.now)

    def _write(self, frame: StreamFrame) -> bool:
        self.sent.append(frame)
        self.link.written(1, frame.id)
        return self.taking

    def show(self, frame: int, *, at: float) -> None:
        self.now = at
        self.link.shown(1, frame)


def _board(receiver: _Receiver, *games: str, settings: PolicySettings | None = None) -> AppLoop:
    slate = _Slate(*games)
    return build_loop(slate, slate.feed, receiver.sink, broadcast_lag=DurationSeconds(0), settings=settings)


def _paint(loop: AppLoop, receiver: _Receiver, seconds: float) -> Painted:
    receiver.now = seconds
    return loop.paint(_at(seconds))


def _ups(painted: Painted) -> list[str]:
    return [
        change.segment.frames[0].dedupe_key.raw
        for change in painted.changes
        if change.kind is ChangeKind.UP and isinstance(change.segment.frames[0], ScoreboardCard)
    ]


def test_a_card_counts_from_when_the_receiver_confirms_its_first_frame_not_from_when_it_was_sent() -> None:
    receiver = _Receiver()
    loop = _board(receiver, "atl-nym", "bos-nyy")
    loop.refresh(_at(0))
    sent = _paint(loop, receiver, 0)
    assert sent.shown is not None and sent.changes == () and sent.presented is None  # handed over is not up
    first = receiver.sent[-1].id
    assert _paint(loop, receiver, 1).changes == ()  # still waiting on the receiver's word
    receiver.show(first, at=2.5)
    up = _paint(loop, receiver, 3)
    [card] = _ups(up)
    assert up.confirmed[0] == first
    assert up.presented is not None and up.presented.at == T + timedelta(seconds=2.5)  # when its word was heard
    assert _paint(loop, receiver, 14.4).changes == ()  # its 12 s turn runs from 2.5, not from 0
    turned = _paint(loop, receiver, 14.5)
    [down] = turned.changes
    assert down.kind is ChangeKind.DOWN and down.aired == timedelta(seconds=12)
    assert _ups(turned) == []  # the next card is sent, and up only once confirmed
    receiver.show(receiver.sent[-1].id, at=15.0)
    assert _ups(_paint(loop, receiver, 15.1)) == [BOS if card == ATL else ATL]


def test_a_frame_replaced_before_it_was_confirmed_is_never_credited_and_each_confirmed_frame_is_matched_once() -> None:
    receiver = _Receiver()
    loop = _board(receiver, "atl-nym")
    loop.refresh(_at(0))
    for seconds in (0.0, 0.1, 0.2):
        _paint(loop, receiver, seconds)
    first, second, third = (frame.id for frame in receiver.sent[:3])
    receiver.show(third, at=0.25)
    painted = _paint(loop, receiver, 0.3)
    assert painted.confirmed[0] == third and _ups(painted) == [ATL]  # then the same pixels, vouched for at once
    receiver.show(first, at=0.35)  # the receiver's word on a frame it was sent before the one it confirmed
    receiver.show(third, at=0.36)  # and the same frame's word again
    later = [_paint(loop, receiver, seconds) for seconds in (0.4, 0.5)]
    confirmed = [frame for paint in (painted, *later) for frame in paint.confirmed]
    assert first not in confirmed and second not in confirmed  # replaced before their word: never credited
    assert len(confirmed) == len(set(confirmed))  # each frame's word matched once
    assert [key for paint in later for key in _ups(paint)] == []  # one card up, from one first frame


def test_a_frame_the_writer_won_t_take_is_a_paint_failure_that_spends_nothing() -> None:
    receiver = _Receiver()
    loop = _board(receiver, "atl-nym")
    loop.refresh(_at(0))
    receiver.taking = False
    failed = _paint(loop, receiver, 0)
    assert failed.render_error == "DisplayUnavailable: the display isn't taking frames" and failed.changes == ()
    receiver.taking = True
    _paint(loop, receiver, 1)
    receiver.show(receiver.sent[-1].id, at=1.5)
    up = _paint(loop, receiver, 2)
    assert _ups(up) == [ATL] and up.presented is not None and up.presented.at == T + timedelta(seconds=1.5)


def test_a_lost_panel_clears_what_was_in_flight_and_a_frame_unconfirmed_for_30_s_is_given_up_on() -> None:
    receiver = _Receiver()
    loop = _board(receiver, "atl-nym")
    loop.refresh(_at(0))
    _paint(loop, receiver, 0)
    receiver.show(receiver.sent[-1].id, at=0.1)
    _paint(loop, receiver, 0.2)
    in_flight = receiver.sent[-1].id
    receiver.link.ended(1)  # its process ended: what it showed is gone
    receiver.link.started(2)
    receiver.link.ready(2)
    receiver.link.shown(2, in_flight)  # word on a frame sent before the loss
    assert _paint(loop, receiver, 0.3).confirmed == ()
    stalled = receiver.sent[-1].id
    assert UNSHOWN_AFTER == timedelta(seconds=30)
    for seconds in range(1, 31):
        _paint(loop, receiver, 0.3 + seconds)  # the receiver says nothing for 30 s
    receiver.link.shown(2, stalled)
    assert _paint(loop, receiver, 31.0).confirmed == ()  # given up on: its word came too late to count


def test_a_first_frame_that_lands_after_the_card_s_latest_start_is_an_accidental_display_booked_as_nothing() -> None:
    receiver = _Receiver()
    queue = InterleavedCardQueue()
    slate = _Slate()
    loop = AppLoop(
        schedules=(ProviderSupervisor(slate, max_age=DurationSeconds(120)),),
        store=ContestStore(),
        pipeline=LiveBaseballPipeline(
            scorer=PriorityScorer(),
            delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)),
            factory=CardFactory(),
            queue=queue,
        ),
        queue=queue,
        registry=default_registry(),
        sink=receiver.sink,
        fetch_feed=slate.feed,
    )
    late = _card("col-lad", expires=20)  # its 8 s minimum must start by 12 s
    queue.ingest_all((late,), _at(0))
    loop.refresh(_at(0))
    _paint(loop, receiver, 0)
    receiver.show(receiver.sent[-1].id, at=12.5)  # the receiver's word comes after that
    landed = _paint(loop, receiver, 12.6)
    [down] = landed.changes
    assert (down.kind, down.reason, down.aired) == (ChangeKind.DOWN, EndReason.LANDED_LATE, timedelta(0))
    assert landed.presented is None and not queue.aired(late.dedupe_key.raw)  # its pixels went up; nothing counts
    on_time = _card("chc-stl", expires=40)
    queue.ingest_all((on_time,), _at(13))
    loop.refresh(_at(13))
    _paint(loop, receiver, 13)
    receiver.show(receiver.sent[-1].id, at=31.9)  # its latest start is 32 s
    assert _ups(_paint(loop, receiver, 32.5)) == [on_time.dedupe_key.raw] and queue.aired(on_time.dedupe_key.raw)


def test_an_older_receiver_s_frames_are_credited_unconfirmed_and_the_board_says_so_once() -> None:
    receiver = _Receiver(confirms=False)
    loop = _board(receiver, "atl-nym", "bos-nyy")
    loop.refresh(_at(0))
    painted = [_paint(loop, receiver, seconds / 10) for seconds in range(0, 61)]
    assert [paint.notes for paint in painted if paint.notes] == [(BoardNote.UNCONFIRMED_DISPLAY,)]
    [up] = [paint for paint in painted if _ups(paint)]
    assert up.at == T + timedelta(seconds=5) and up.reason is SelectionReason.ROTATION  # credited 5 s in


def test_under_policy_2_an_older_receiver_counts_for_nothing_and_the_board_rotates_by_policy_1() -> None:
    receiver = _Receiver(confirms=False)
    loop = _board(receiver, "atl-nym", "bos-nyy", settings=PolicySettings())
    painted: list[Painted] = []
    for tenth in range(0, 400):
        loop.refresh(_at(tenth / 10))
        painted.append(_paint(loop, receiver, tenth / 10))
    changes = [change for paint in painted for change in paint.changes if change.segment.key.surface.value == "board"]
    void = [change for change in changes if change.reason is EndReason.UNCONFIRMED]
    assert len(void) == 1 and void[0].kind is ChangeKind.DROPPED  # the director's cue: void at its first word
    assert [note for paint in painted for note in paint.notes] == [BoardNote.UNCONFIRMED_DISPLAY]
    assert [decision for paint in painted for decision in paint.decisions] == []  # the director credits nothing
    ups = [(paint.at, key) for paint in painted for key in _ups(paint)]
    first, second = ups[0][1], ups[1][1]
    assert {first, second} == {ATL, BOS} and ups[2][1] == first  # policy 1's rotation, on the unconfirmed word
    assert [at - T for at, _ in ups[:3]] == [timedelta(seconds=seconds) for seconds in (5.1, 17.1, 29.1)]


def test_a_board_s_receiver_is_started_written_to_and_closed_as_one() -> None:
    process = _Process()
    logs: list[str] = []
    board = stream_display(["show"], QUAD, log=logs.append, spawn=_Spawner(process))
    board.start()
    frame = board.sink.new_frame()
    board.sink.send(frame)
    assert _until(lambda: len(process.sent) > 0)
    sent = process.sent
    board.stop()
    assert logs == ["display: starting show"] and sent[:4] == b"OMNF" and process.waited == 10
    process.end()


def test_a_display_that_shows_a_frame_as_it_commits_it_keeps_booking_at_the_commit() -> None:
    from omni.app.display import RecordingDisplaySink

    slate = _Slate("atl-nym")
    loop = build_loop(slate, slate.feed, RecordingDisplaySink(QUAD), broadcast_lag=DurationSeconds(0))
    loop.refresh(_at(0))
    painted = loop.paint(_at(0))
    assert _ups(painted) == [ATL] and painted.confirmed == () and painted.presented is not None
