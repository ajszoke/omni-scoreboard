"""When a cue's first frame lands, through `BoardSession.committed` under `omni-board-policy/2`: a landing judged at the
receipt's instant. A guest cued while its host's break had room for it, whose first frame lands once the break no
longer does, is an accidental display: its pixels went up, but no guest turn is granted and nothing is credited."""

from __future__ import annotations

from datetime import timedelta

from omni.cards.base import ScoreboardCard
from omni.domain.breaks import BreakKind
from omni.tower.board import BoardCue
from omni.policy.leases import LeaseRefusal
from omni.tower.decisions import GrantBoundary, GuestNote
from omni.tower.lane import ChangeKind, EndReason
from tests.board_policy import Run, at, evidence, facts, games, live


def _guest_cued() -> tuple[Run, BoardCue, str]:
    """The Braves in a mound visit from 0 (its lease 36 s), another game's card cued at 0 as its guest."""
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other), when=0)
    run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=-45),)), facts(other, 0.4))
    run.publish()
    cue = run.board.cue(at(0))
    assert isinstance(cue.payload, ScoreboardCard) and cue.payload.dedupe_key == live(other).dedupe_key
    return run, cue, cue.payload.dedupe_key.raw


def test_a_guest_whose_first_frame_lands_while_its_host_s_break_has_room_is_granted_its_turn() -> None:
    run, cue, _ = _guest_cued()
    assert run.board.committed(cue, at(1)) is True
    [up] = run.board.changes()
    assert up.kind is ChangeKind.UP
    assert [note for note in run.board.decisions() if isinstance(note, GuestNote) and note.refusal is None]


def test_a_guest_whose_first_frame_lands_after_its_host_s_break_ran_short_is_an_accidental_display() -> None:
    run, cue, key = _guest_cued()
    assert run.board.committed(cue, at(30)) is False  # 6 s of the visit left
    [down] = run.board.changes()
    assert (down.kind, down.reason, down.aired) == (ChangeKind.DOWN, EndReason.GRANT_LAPSED, timedelta(0))
    guests = [note for note in run.board.decisions() if isinstance(note, GuestNote)]
    assert [(note.refusal, note.boundary) for note in guests] == [(LeaseRefusal.NO_ROOM, GrantBoundary.SWAP)]
    assert not run.queue.aired(key)
