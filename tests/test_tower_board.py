"""The board session: one card at a time from the queue, held by its dwell from its first committed frame."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from omni.cards.attention import AttentionMode, AttentionPolicy
from omni.cards.base import CardPriority, ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.scheduler import InterleavedCardQueue, SelectionReason
from omni.tower.board import (
    BoardCue,
    BoardFrame,
    BoardHealth,
    BoardPolicy,
    BoardSession,
    FrameKind,
    SourceHealth,
)
from omni.tower.lane import ChangeKind, EndReason

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()
_STATE = BaseballGameState(
    away_score=1,
    home_score=2,
    inning=5,
    phase=InningPhase.TOP,
    count=BaseballCount(balls=0, strikes=0, outs=0),
    bases=BaseballBaseState(),
    away_hits=4,
    home_hits=5,
)
_BURST = AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(12))
_REMINDER = AttentionPolicy(mode=AttentionMode.RECURRING, cooldown=DurationSeconds(60))


def _at(seconds: float) -> ClockReading:
    """``seconds`` after T0, read both ways."""
    return ClockReading(T0 + timedelta(seconds=seconds), MonotonicTime(seconds))


def _card(
    gid: str,
    *,
    attention: AttentionPolicy | None = None,
    band: DisplayPriority = DisplayPriority.NORMAL,
    minimum: int = 8,
    maximum: int = 30,
    expires: float | None = None,
) -> ScoreboardCard[LiveBaseballCardPayload]:
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, gid),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T0,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )
    factory = CardFactory(live_min_display=DurationSeconds(minimum), live_max_display=DurationSeconds(maximum))
    card = factory.live_baseball(game, _STATE, now=T0, priority=CardPriority(band=band, score=0.0))
    if expires is not None:
        card = dataclasses.replace(
            card, timing=dataclasses.replace(card.timing, expires_at=T0 + timedelta(seconds=expires))
        )
    if attention is not None:
        card = dataclasses.replace(card, attention=attention)
    return card


def _board(*cards: ScoreboardCard[LiveBaseballCardPayload], policy: BoardPolicy = BoardPolicy()) -> BoardSession:
    queue = InterleavedCardQueue()
    queue.ingest_all(cards)
    return BoardSession(queue, QUAD, policy=policy)


def _air(board: BoardSession, seconds: float) -> BoardCue:
    """Cue the board at ``seconds`` and commit what it cued, as a display that drew it would."""
    cue = board.cue(_at(seconds))
    board.committed(cue, _at(seconds))
    return cue


def _name(cue: BoardCue) -> str:
    payload = cue.payload
    return payload.kind.value if isinstance(payload, BoardFrame) else payload.contest.id.raw


def test_a_lone_card_stays_up_past_its_turn_and_maximum() -> None:
    board = _board(_card("g1"))
    first = _air(board, 0)
    assert _name(first) == "g1" and first.reason is SelectionReason.ROTATION and not first.stale
    for seconds in (12, 30, 120):
        assert _air(board, seconds).occurrence == first.occurrence  # nothing else wants the board
    assert [change.kind for change in board.changes()] == [ChangeKind.UP]


def test_cards_take_turns_of_the_rotation_slot() -> None:
    board = _board(_card("g1"), _card("g2"))
    first = _air(board, 0)
    assert _name(_air(board, 11.9)) == _name(first)  # short of its turn
    second = _air(board, 12)
    assert _name(second) != _name(first)
    up, down, up_again = board.changes()
    assert (up.kind, down.kind, up_again.kind) == (ChangeKind.UP, ChangeKind.DOWN, ChangeKind.UP)
    assert down.reason is EndReason.RAN_ITS_TIME and down.aired == timedelta(seconds=12)


def test_a_turn_is_held_within_the_card_s_own_minimum_and_maximum() -> None:
    long_minimum = _board(_card("g1", minimum=15, maximum=30), _card("g2", minimum=15, maximum=30))
    first = _air(long_minimum, 0)
    assert (
        _air(long_minimum, 12).occurrence == first.occurrence and _air(long_minimum, 15).occurrence != first.occurrence
    )
    short_maximum = _board(_card("g1", minimum=4, maximum=10), _card("g2", minimum=4, maximum=10))
    first = _air(short_maximum, 0)
    assert _air(short_maximum, 10).occurrence != first.occurrence


def test_dwell_runs_from_the_first_committed_frame() -> None:
    board = _board(_card("g1"), _card("g2"))
    cue = board.cue(_at(0))  # cued, but the display never drew it
    assert board.next_deadline(_at(0)) is None  # its dwell waits on a commit
    assert board.cue(_at(20)).occurrence == cue.occurrence  # still its turn: none of it has run
    board.committed(cue, _at(20))
    board.committed(cue, _at(25))  # a later frame of it changes nothing
    assert board.cue(_at(31.9)).occurrence == cue.occurrence and board.cue(_at(32)).occurrence != cue.occurrence


def test_a_takeover_waits_for_the_minimum_then_takes_the_board() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    board = BoardSession(queue, QUAD)
    _air(board, 0)
    queue.ingest(_card("bang", attention=_BURST, band=DisplayPriority.ALERT))
    assert _name(_air(board, 5)) == "g1"  # ordinary priority never cuts a visible minimum
    takeover = _air(board, 8)
    assert _name(takeover) == "bang" and takeover.reason is SelectionReason.TAKEOVER
    down = [change for change in board.changes() if change.kind is ChangeKind.DOWN]
    assert down[0].reason is EndReason.NEXT_WAITING and down[0].aired == timedelta(seconds=8)
    assert _name(_air(board, 19.9)) == "bang" and _name(_air(board, 20)) == "g1"  # its 12 s, then rotation


def test_a_reminder_takes_the_board_from_rotation_and_yields_to_nothing_once_its_turn_is_over() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    board = BoardSession(queue, QUAD)
    _air(board, 0)
    queue.ingest(_card("nono", attention=_REMINDER, band=DisplayPriority.ALERT))
    assert _name(_air(board, 8)) == "nono"  # due on first sight: after the live card's minimum
    queue.remove("g1:live")
    assert _name(_air(board, 19.9)) == "nono"
    idle = _air(board, 20)  # its beat has passed, and nothing else is eligible
    assert isinstance(idle.payload, BoardFrame) and idle.payload == BoardFrame(FrameKind.IDLE, "nothing eligible")


def test_a_card_that_leaves_the_queue_or_expires_comes_down_at_once() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    board = BoardSession(queue, QUAD)
    _air(board, 0)
    queue.ingest(_card("g2", expires=40))
    queue.remove("g1:live")
    assert _name(_air(board, 1)) == "g2"  # factual invalidation doesn't wait for a minimum
    assert _name(_air(board, 40)) == "idle"  # expired, and nothing else
    reasons = [change.reason for change in board.changes() if change.kind is ChangeKind.DOWN]
    assert reasons == [EndReason.INVALIDATED, EndReason.EXPIRED]


def test_a_card_cued_but_gone_before_it_drew_is_dropped_unbooked() -> None:
    queue = InterleavedCardQueue()
    queue.ingest_all((_card("g1"), _card("g2")))
    board = BoardSession(queue, QUAD)
    card = board.cue(_at(0)).payload
    assert not isinstance(card, BoardFrame)
    queue.remove(card.dedupe_key.raw)
    board.cue(_at(1))
    dropped = board.changes()
    assert [change.kind for change in dropped] == [ChangeKind.DROPPED] and dropped[0].aired is None


def test_a_newer_version_of_the_card_up_swaps_in_and_keeps_its_dwell() -> None:
    queue = InterleavedCardQueue()
    queue.ingest_all((_card("g1"), _card("g2")))
    board = BoardSession(queue, QUAD)
    first = _air(board, 0)
    card = first.payload
    assert not isinstance(card, BoardFrame)
    newer = dataclasses.replace(card, priority=CardPriority(band=DisplayPriority.NORMAL, score=9.0))
    queue.ingest(newer)
    edited = _air(board, 5)
    assert edited.occurrence == first.occurrence and edited.payload is newer and edited.segment.revision == 1
    assert _air(board, 12).occurrence != first.occurrence  # its turn still ends 12 s after it first drew


def test_takeovers_back_to_back_stop_at_the_cap_for_a_full_rotation_turn() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    board = BoardSession(queue, QUAD, policy=BoardPolicy(takeover_cap=timedelta(seconds=20)))
    _air(board, 0)
    queue.ingest_all(_card(f"b{n}", attention=_BURST, band=DisplayPriority.ALERT) for n in range(3))
    assert _air(board, 8).reason is SelectionReason.TAKEOVER  # 12 s
    assert _air(board, 20).reason is SelectionReason.TAKEOVER  # 24 s: past the cap
    base = _air(board, 32)
    assert _name(base) == "g1" and base.reason is SelectionReason.ROTATION
    assert _air(board, 40).occurrence == base.occurrence  # no takeover cuts the base turn short
    resumed = _air(board, 44)
    assert resumed.reason is SelectionReason.TAKEOVER  # the run starts over once that turn has run


def test_with_nothing_to_show_the_board_says_why() -> None:
    board = _board()
    assert board.cue(_at(0)).payload == BoardFrame(FrameKind.IDLE, "nothing eligible")
    board.health = BoardHealth(held=2)
    assert board.cue(_at(1)).payload == BoardFrame(FrameKind.HELD, "2 games in the TV delay")
    board.health = BoardHealth(held=1)
    assert board.cue(_at(2)).payload == BoardFrame(FrameKind.HELD, "a game in the TV delay")
    board.health = BoardHealth(source=SourceHealth.OFFLINE, reason="no schedule yet: down")
    assert board.cue(_at(3)).payload == BoardFrame(FrameKind.OFFLINE, "no schedule yet: down")
    assert board.health.source is SourceHealth.OFFLINE


def test_a_frame_with_a_new_reason_is_revised_in_place() -> None:
    board = _board()
    board.health = BoardHealth(held=1)
    first = _air(board, 0)
    board.health = BoardHealth(held=2)
    revised = board.cue(_at(1))
    assert revised.occurrence == first.occurrence and revised.segment.revision == 1
    assert [change.kind for change in board.changes()] == [ChangeKind.UP]


def test_a_frame_gives_way_to_a_card_at_once() -> None:
    queue = InterleavedCardQueue()
    board = BoardSession(queue, QUAD)
    _air(board, 0)
    queue.ingest(_card("g1"))
    assert _name(_air(board, 0.1)) == "g1"
    down = [change for change in board.changes() if change.kind is ChangeKind.DOWN]
    assert down[0].reason is EndReason.NEXT_WAITING


def test_offline_clears_the_card_and_the_frame_before_it() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    board = BoardSession(queue, QUAD)
    _air(board, 0)
    board.health = BoardHealth(source=SourceHealth.OFFLINE, reason="no schedule for over 10m: down")
    offline = _air(board, 1)
    assert isinstance(offline.payload, BoardFrame) and offline.payload.kind is FrameKind.OFFLINE
    board.health = BoardHealth()
    queue.remove("g1:live")
    _air(board, 2)  # idle: the offline frame comes down for it
    downs = [change.reason for change in board.changes() if change.kind is ChangeKind.DOWN]
    assert downs == [EndReason.CLEARED, EndReason.NEXT_WAITING]
    board.health = BoardHealth(source=SourceHealth.OFFLINE, reason="again")
    _air(board, 3)
    assert [change.reason for change in board.changes() if change.kind is ChangeKind.DOWN] == [EndReason.CLEARED]


def test_a_card_is_marked_stale_when_its_source_or_its_own_data_is() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    board = BoardSession(queue, QUAD)
    assert not _air(board, 0).stale
    board.health = BoardHealth(source=SourceHealth.STALE, reason="no schedule for over 2m: down")
    assert _air(board, 1).stale
    board.health = BoardHealth()
    queue.ingest(dataclasses.replace(_card("g1"), stale_since=T0))
    assert _air(board, 2).stale


def test_the_next_deadline_is_the_soonest_of_minimum_turn_expiry_and_the_queue() -> None:
    queue = InterleavedCardQueue()
    queue.ingest_all((_card("g1", expires=20), _card("g2", expires=20)))
    board = BoardSession(queue, QUAD)
    assert board.next_deadline(_at(0)) == MonotonicTime(0.0)  # nothing cued yet
    _air(board, 0)
    assert board.next_deadline(_at(0)) == MonotonicTime(8.0)  # its minimum
    assert board.next_deadline(_at(9)) == MonotonicTime(12.0)  # its turn (the queue: 12 s too, as 8 s remain)
    lone = _board()
    _air(lone, 0)
    assert lone.next_deadline(_at(0)) is None  # a frame, and a queue with nothing coming


def test_a_board_s_slot_and_cap_are_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        BoardPolicy(slot=timedelta(0))
    with pytest.raises(ValueError, match="positive"):
        BoardPolicy(takeover_cap=timedelta(seconds=-1))


def test_a_stale_cue_commits_nothing() -> None:
    board = _board(_card("g1"), _card("g2"))
    old = board.cue(_at(0))
    board.committed(old, _at(0))
    _air(board, 12)
    board.changes()
    board.committed(old, _at(13))  # a frame of what was up before: too late to start anything
    assert board.changes() == []
