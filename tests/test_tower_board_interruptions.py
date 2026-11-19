"""Interruptions on the board under `omni-board-policy/2`, through `BoardSession.interrupted` (`BOARD_POLICY.md` §6,
"Wake or output loss"): the turn on air ends at the last moment its display is known to have shown it.

- After an **output loss** what the policy decided stands: the favorite's break still lends its airtime, and the time
  from the display's last word to its next confirmed frame is unknown display time, not airtime.
- After a **host's gap** the lease from before it is void, a promotion waiting is withdrawn, and nothing published
  before the gap airs: the board holds the explicit frame until a refresh after it publishes.
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType

from omni.domain.breaks import BreakKind
from omni.policy.view import PolicyView
from omni.queue.scheduler import SelectionReason
from omni.tower.board import BoardFrame, FrameKind
from omni.tower.decisions import GuestNote
from omni.tower.interruption import Interruption, InterruptionReason
from omni.tower.lane import ChangeKind, EndReason
from tests.board_policy import Run, action, at, big_play, evidence, facts, games, live, major


def _interruption(reason: InterruptionReason, before: float, after: float) -> Interruption:
    return Interruption(serial=1, reason=reason, before=at(before), after=at(after))


def _publish(run: Run, *, after: int) -> None:
    run.board.publish(
        run.health, PolicyView(games=MappingProxyType(dict(run.facts)), offers=tuple(run.offers)), after=after
    )


def _guest_up(kind: BreakKind = BreakKind.MLB_MOUND_VISIT, stated: float | None = None) -> Run:
    """The Braves in a break from 0 (a mound visit's lends one guest), the other game's card up at 0 as its guest."""
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other), when=0)
    run.describe(facts(atl, 0.4, breaks=(evidence(atl, kind, received=-45, stated=stated),)), facts(other, 0.4))
    run.publish()
    run.step(0)
    assert [note.card for note in run.of(GuestNote) if note.refusal is None] == [live(other).id]
    return run


def test_an_output_loss_ends_a_guest_s_turn_at_the_last_word_and_the_host_s_break_still_lends_its_airtime() -> None:
    run = _guest_up(BreakKind.MLB_INNING_BREAK, stated=130)  # an inning break: guests for its whole length
    run.board.interrupted(_interruption(InterruptionReason.OUTPUT_LOSS, before=4, after=6))
    [down] = run.board.changes()
    assert (down.kind, down.reason, down.aired, down.at) == (
        ChangeKind.DOWN,
        EndReason.INTERRUPTED,
        timedelta(seconds=4),
        at(4),
    )
    run.step(6)  # the board decides again, on what it knew
    guests = [note for note in run.of(GuestNote) if note.refusal is None]
    assert len(guests) == 2 and guests[-1].at == at(6)  # the visit's lease still lends the break
    assert run.board.unknown_display == timedelta(seconds=2)  # from the last word at 4 to the frame at 6


def test_an_output_loss_whose_last_word_came_before_the_card_went_up_credits_it_nothing() -> None:
    run = _guest_up()
    run.board.interrupted(_interruption(InterruptionReason.OUTPUT_LOSS, before=-3, after=6))
    [down] = run.board.changes()
    assert (down.reason, down.aired) == (EndReason.INTERRUPTED, timedelta(0))  # never before it went up


def test_a_lone_card_s_renewed_turn_cut_before_it_began_is_credited_nothing_of_it() -> None:
    [atl] = games(1)
    run = Run()
    run.stage(live(atl), when=0)
    run.describe(facts(atl, 0.4))
    run.publish()
    run.run(0, 13)  # its turn renews at 12, its frame staying up
    run.board.interrupted(_interruption(InterruptionReason.OUTPUT_LOSS, before=10, after=13))
    [down] = run.board.changes()
    assert (down.reason, down.aired) == (EndReason.INTERRUPTED, timedelta(seconds=10))


def test_after_a_host_s_gap_nothing_published_before_it_airs_and_its_lease_is_void() -> None:
    run = _guest_up()
    atl, other = games(2)
    run.board.interrupted(_interruption(InterruptionReason.HOST_SUSPEND, before=4, after=4.5))
    held = run.step(5)
    frame = held.payload
    assert isinstance(frame, BoardFrame) and frame.kind is FrameKind.HELD  # the input is from before the gap
    run.describe(facts(atl, 0.4), facts(other, 0.4))  # what a refresh after it publishes: no break evidence
    _publish(run, after=1)
    up = run.step(5.5)
    assert up.reason is SelectionReason.ROTATION
    assert [note.at for note in run.of(GuestNote) if note.refusal is None] == [at(0)]  # no guest since the gap


def _waiting(*, interrupted: bool) -> Run:
    """A break-only promotion of the other game, waiting while the Braves act; their mound visit begins at 10. With
    ``interrupted``, the host sleeps at 5, and the refresh after it no longer offers what it held from before."""
    atl, other = games(2)
    play = big_play(other, "p1")
    run = Run()
    run.stage(live(atl), live(other), play)
    run.describe(facts(atl, 0.4, action=action(atl, "ab 41 p1", received=-45)), facts(other, 0.4))
    run.offers.append(major(play, 0.9, model=False))
    run.publish()

    def gap() -> None:
        run.board.interrupted(_interruption(InterruptionReason.HOST_SUSPEND, before=4.9, after=5))
        run.offers.clear()
        _publish(run, after=1)

    def visit() -> None:
        breaks = (evidence(atl, BreakKind.MLB_MOUND_VISIT, received=-35, marker="b2"),)
        run.describe(facts(atl, 0.4, breaks=breaks))
        _publish(run, after=1 if interrupted else 0)

    if interrupted:
        run.on(5, gap)
    run.on(10, visit)
    run.run(0, 30)
    return run


def test_after_a_host_s_gap_a_promotion_waiting_from_before_it_never_starts() -> None:
    play = big_play(games(2)[1], "p1").dedupe_key.raw
    assert _waiting(interrupted=False).promotions() == [(10.0, play)]  # without the gap, the visit lets it in
    assert _waiting(interrupted=True).promotions() == []


def test_an_interruption_before_anything_went_up_and_a_second_loss_before_the_display_s_next_word_count_once() -> None:
    [atl] = games(1)
    run = Run()
    run.stage(live(atl), when=0)
    run.describe(facts(atl, 0.4))
    run.publish()
    run.board.interrupted(_interruption(InterruptionReason.OUTPUT_LOSS, before=0, after=1))  # nothing up yet
    assert run.board.changes() == []
    run.board.interrupted(_interruption(InterruptionReason.OUTPUT_LOSS, before=2, after=3))  # a second, still lost
    run.step(5)
    assert run.board.unknown_display == timedelta(seconds=5)  # from the first loss's bound to the next frame
