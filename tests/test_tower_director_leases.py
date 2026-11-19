"""A favorite's break lent to guests, through `BoardSession` under `omni-board-policy/2` (`BOARD_POLICY.md` §4-§5).

Each test reads COL@LAD (the Rockies a favorite) from synthetic StatsAPI answers, through the provider's parse and
the MLB break detectors, holds what each answer shows through a 45 s delay as the pipeline does (`ReleasedBreaks`),
and publishes the released facts to the board at the instants the test names. Times are seconds since `T0`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.policy_facts import ReleasedBreaks
from omni.app.trace import DecisionTracer
from omni.core.enum import League, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.breaks import BreakKind
from omni.events.mlb_breaks import mlb_break_evidence, mlb_latest_action
from omni.policy.leases import LeaseRefusal
from omni.policy.promotion import PromotionRefusal
from omni.policy.view import GameFacts
from omni.preferences.favorites import Favorites
from omni.queue.delay_policy import Arrival, DelayPolicy, ReleaseGuard
from omni.replay.policy_lines import policy_line
from omni.tower.decisions import (
    BaseReason,
    BaseTurnNote,
    GrantBoundary,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    PolicyDecision,
    UnspentNote,
    WaitNote,
    YieldNote,
)
from omni.tower.lane import ChangeKind, EndReason
from tests import mlb_break_feeds as statsapi
from tests.board_policy import PROFILES, T0, Run, at, big_play, evidence, facts, games, live, major, reminder

COL = statsapi.game()  # Rockies at Dodgers
OTHER = games(2)[1]
COL_ONLY = Favorites({League.MLB: ["COL"]})
LAG = DurationSeconds(45)


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class _Feed:
    """COL@LAD's answers, each read as the board reads one and held through the delay with what it shows."""

    def __init__(self) -> None:
        self.tracker = ReleasedBreaks(ReleaseGuard(DelayPolicy(broadcast_lag=LAG)))

    def answer(self, raw: dict[str, Any], *, received: float) -> None:
        feed = statsapi.read(raw)
        reading = at(received)
        arrival = Arrival(received=reading, as_of=feed.as_of)
        breaks, action = mlb_break_evidence(feed, COL, reading), mlb_latest_action(feed, COL, reading)
        self.tracker.push(breaks, action, arrival, reading)

    def facts(self, when: float) -> GameFacts:
        """The game's facts as a refresh at ``when`` publishes them."""
        self.tracker.release(at(when))
        return facts(COL, 0.4, breaks=self.tracker.breaks, action=self.tracker.action)


def _visit(*, began: float, as_of: float, pitches: tuple[float, ...] = (), after: tuple[float, ...] = ()) -> Any:
    """The top of the 5th, at-bat 41 under way: its ``pitches``, a mound visit at ``began``, then the pitches
    ``after`` it; the answer stood at ``as_of``. Each event keeps its place in the at-bat by its index: the pitches
    before the visit from 0, the visit 5, the pitches after it from 6."""
    events = [statsapi.pitch(at=_t(when), index=index) for index, when in enumerate(pitches)]
    events.append(statsapi.action("mound_visit", at=_t(began), index=5))
    events += [statsapi.pitch(at=_t(when), index=6 + n) for n, when in enumerate(after)]
    play = statsapi.play(41, complete=False, events=tuple(events))
    return statsapi.answer(plays=(play,), as_of=_t(as_of))


def _run(profile: PanelProfile = PanelProfile.QUAD_128X64) -> Run:
    run = Run(profile=profile, favorites=COL_ONLY)
    run.stage(live(COL), live(OTHER))
    return run


def _publish(run: Run, feed: _Feed, when: float) -> None:
    run.describe(feed.facts(when), facts(OTHER, 0.4))
    run.publish()


def _leases(run: Run) -> list[tuple[float, LeaseEvent, LeaseRefusal | None]]:
    return [(note.at.monotonic.seconds, note.event, note.refusal) for note in run.of(LeaseNote)]


# --- The anchor: a lease runs from its pinned release, less its age ---------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_a_mound_visit_read_at_its_release_opens_with_its_ceiling_less_its_12_s_age(profile: PanelProfile) -> None:
    """The visit began 12 s before the answer stood, which arrived at -45 and releases at 0: 24 s are left."""
    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-57, as_of=-45), received=-45)
    run = _run(profile)
    _publish(run, feed, 0)
    run.step(0)
    [opened] = run.of(LeaseNote)
    assert opened.event is LeaseEvent.OPENED and opened.remaining == timedelta(seconds=24)
    assert opened.age is not None and opened.age.age == timedelta(seconds=12)


def test_a_mound_visit_first_read_16_s_after_its_release_has_used_more_than_half_its_ceiling_and_lends_nothing() -> (
    None
):
    """A decision late after the release can't restore the time the break has already used: at 16 the visit has
    run 28 of its 36 s, so it gets no lease, and the other game's turn isn't a guest of it."""
    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-57, as_of=-45), received=-45)
    run = _run()
    _publish(run, feed, 16)
    run.step(16)
    assert _leases(run) == [(16.0, LeaseEvent.REFUSED, LeaseRefusal.BACKFILL)]
    [refused] = run.of(LeaseNote)
    assert refused.elapsed == timedelta(seconds=28) and refused.age is not None
    assert refused.age.age == timedelta(seconds=12) and refused.age.uncertainty == timedelta(0)
    assert not any(note.refusal is None for note in run.of(GuestNote))


# --- Admission at the physical start ----------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_a_guest_cued_with_room_whose_first_frame_lands_at_16_with_8_s_left_is_discarded_unseen(
    profile: PanelProfile,
) -> None:
    """Cued at 0 with 24 s left, the other game's 12 s turn and its 6 s margin fit; its first frame is confirmed at
    16, when 8 s are left and it needs 18: it comes down unseen, spends none of the visit's one guest, and the
    Rockies get their turn back."""
    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-57, as_of=-45), received=-45)
    run = _run(profile)
    _publish(run, feed, 0)
    cue = run.board.cue(at(0))
    assert cue.payload == live(OTHER)
    run.board.committed(cue, at(16))
    [dropped] = run.board.changes()
    assert (dropped.kind, dropped.reason) == (ChangeKind.DROPPED, EndReason.INVALIDATED)
    refused = [note for note in run.board.decisions() if isinstance(note, GuestNote) and note.refusal is not None]
    assert [(note.refusal, note.boundary, note.at.monotonic.seconds) for note in refused] == [
        (LeaseRefusal.NO_ROOM, GrantBoundary.SWAP, 16.0)
    ]
    run.step(16)
    assert run.ups[-1].key == live(COL).dedupe_key.raw
    assert [note.reason for note in run.of(BaseTurnNote)] == [BaseReason.RETURN]


def test_a_guest_whose_first_frame_lands_late_but_still_fits_keeps_its_whole_turn_from_that_frame() -> None:
    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-57, as_of=-45), received=-45)
    run = _run()
    _publish(run, feed, 0)
    cue = run.board.cue(at(0))
    run.board.committed(cue, at(5))  # 19 s left: the turn and its margin still fit
    [up] = run.board.changes()
    assert up.kind is ChangeKind.UP and up.at.monotonic.seconds == 5.0
    assert run.board.next_deadline(at(5)) is not None


# --- Causal closure: only action after the break's onset ends it ------------------------------------------------


def test_a_late_arriving_pitch_from_before_the_visit_never_revokes_its_lease() -> None:
    """The feed fills in a pitch thrown before the visit after the visit was shown: a new action marker, but older
    than the break, so the visit's lease stays open and its guest runs its whole turn."""
    feed = _Feed()
    feed.answer(_visit(pitches=(-62,), began=-50, as_of=-45), received=-45)
    feed.answer(_visit(pitches=(-62, -55), began=-50, as_of=-40), received=-40)  # pitch 1, thrown at -55, late
    run = _run()
    _publish(run, feed, 0)
    run.on(5, lambda: _publish(run, feed, 5))
    run.run(0, 12)
    assert [event for _, event, _ in _leases(run)] == [LeaseEvent.OPENED]
    assert run.of(YieldNote) == []
    assert run.shown(live(OTHER).dedupe_key.raw)[0] == (0.0, 12.0)


def test_a_pitch_after_the_visit_revokes_its_lease_and_its_guest_gives_the_board_back_after_its_minimum() -> None:
    feed = _Feed()
    feed.answer(_visit(pitches=(-62,), began=-50, as_of=-45), received=-45)
    feed.answer(_visit(pitches=(-62,), began=-50, after=(-42,), as_of=-40), received=-40)
    run = _run()
    _publish(run, feed, 0)
    run.on(5, lambda: _publish(run, feed, 5))
    run.run(0, 12)
    assert [(when, event) for when, event, _ in _leases(run)] == [(0.0, LeaseEvent.OPENED), (5.0, LeaseEvent.REVOKED)]
    [given] = run.of(YieldNote)
    assert given.at.monotonic.seconds == 8.0 and given.aired == timedelta(seconds=8)


# --- Nested intervals: one supersedes, the rest add nothing ----------------------------------------------------


def test_a_rain_delay_during_a_mound_visit_supersedes_its_lease_and_lends_from_its_own_onset() -> None:
    """The visit's lease opens at 0; the rain delay released at 5 closes it (its budget discarded), and an
    open-ended lease opens in its place. The guest up keeps its minimum, then the delay hosts the next guest; long
    after the visit's 36 s would have run out, the Rockies are still resting in their break."""
    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-48, as_of=-45), received=-45)
    delayed = statsapi.answer(plays=(statsapi.play(41, complete=False),), status="Delayed: Rain", as_of=_t(-40))
    feed.answer(delayed, received=-40)
    run = _run()
    _publish(run, feed, 0)
    run.on(5, lambda: _publish(run, feed, 5))
    run.run(0, 120)
    events = [(when, event) for when, event, _ in _leases(run)]
    assert events[:3] == [(0.0, LeaseEvent.OPENED), (5.0, LeaseEvent.SUPERSEDED), (5.0, LeaseEvent.OPENED)]
    opened = [note for note in run.of(LeaseNote) if note.event is LeaseEvent.OPENED]
    assert opened[1].id.kind is BreakKind.MLB_WEATHER and opened[1].remaining is None
    [given] = run.of(YieldNote)
    assert given.at.monotonic.seconds == 8.0
    assert not any(note.reason is BaseReason.RETURN for note in run.of(BaseTurnNote))  # never back to the Rockies
    assert all(up.key == live(OTHER).dedupe_key.raw for up in run.ups)


# --- An unscored promotion plays only in an ordinary break or open rotation -------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_an_unscored_reminder_waits_out_a_favorite_s_micro_break(profile: PanelProfile) -> None:
    """The Braves are in a mound visit with room for an 8 s card: a bid reminder, which carries no value, still
    waits, as a snapshot would; it may play in an ordinary break or open rotation."""
    atl, other = games(2)
    run = Run(profile=profile)
    bid = live(other)
    run.stage(live(atl), bid)
    run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=-45),)), facts(other, 0.4))
    run.offers.append(reminder(bid))
    run.publish()
    run.run(0, 30)
    assert run.promotions() == []
    assert [note.reason for note in run.of(WaitNote)][:1] == [PromotionRefusal.ORDINARY_BREAK_ONLY]


# --- What the trace says -----------------------------------------------------------------------------------------


def _lines(notes: Sequence[PolicyDecision]) -> list[str]:
    lines: list[str] = []
    DecisionTracer(lines.append, timezone=timezone.utc).decided(list(notes))
    return lines


def test_the_trace_says_each_refusal_s_age_and_guard_where_a_guest_was_refused_and_what_a_yield_aired() -> None:
    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-57, as_of=-45), received=-45)
    late = _run()
    _publish(late, feed, 16)
    late.step(16)
    refused = [note for note in late.of(LeaseNote) if note.event is LeaseEvent.REFUSED]
    written = "lease 23:00:16: refused mlb:mlb_statsapi:700001 mlb mound visit @ at-bat 41 event 5 (backfill)"
    assert _lines(refused) == [written + " | age 12.0s same_source | 28.0s of its ceiling gone"]
    assert [policy_line(note) for note in refused] == [
        "lease   refused mlb:mlb_statsapi:700001 mlb mound visit @ at-bat 41 event 5  (backfill)"
        " | age 12.0s same_source | 28.0s of its ceiling gone"
    ]

    unknown = _run()
    untimed = statsapi.play(41, complete=False, events=(statsapi.action("mound_visit", index=5),))
    later = _Feed()
    later.answer(statsapi.answer(plays=(untimed,), as_of=_t(-45)), received=-45)
    _publish(unknown, later, 25)  # the onset unknown: 15 s allowed for it, and 25 s since its release
    unknown.step(25)
    [provisional] = [note for note in unknown.of(LeaseNote) if note.event is LeaseEvent.REFUSED]
    assert policy_line(provisional).endswith(
        "(spent) | age 15.0s provisional, 15.0s of it an allowance | 40.0s of its ceiling gone"
    )

    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-57, as_of=-45), received=-45)
    swap = _run()
    _publish(swap, feed, 0)
    cue = swap.board.cue(at(0))
    swap.board.committed(cue, at(16))
    guests = [note for note in swap.board.decisions() if isinstance(note, GuestNote) and note.refusal is not None]
    assert _lines(guests) == [
        "guest 23:00:16: refused mlb:mlb_statsapi:g01:live in mlb:mlb_statsapi:700001 (no_room at its first frame)"
    ]
    assert policy_line(guests[0]).endswith("(no_room at its first frame)")

    feed = _Feed()
    feed.answer(_visit(pitches=(-62,), began=-50, as_of=-45), received=-45)
    feed.answer(_visit(pitches=(-62,), began=-50, after=(-42,), as_of=-40), received=-40)
    resumed = _run()
    _publish(resumed, feed, 0)
    resumed.on(5, lambda: _publish(resumed, feed, 5))
    resumed.run(0, 12)
    assert policy_line(resumed.of(YieldNote)[0]).endswith("(its host's break closed 5.0; aired 8.0s)")


def test_a_promotion_cued_into_a_break_that_ran_short_by_its_first_frame_spends_nothing() -> None:
    """A late winner elsewhere, cued into the Rockies' mound visit at 0 with 24 s left: its first frame confirmed at
    16, with 8 s left, it comes down unseen, its cooldown unspent, and it waits for a better moment."""
    feed = _Feed()
    feed.answer(_visit(pitches=(-60,), began=-57, as_of=-45), received=-45)
    run = _run()
    play = big_play(OTHER, "p1")
    run.stage(play)
    run.offers.append(major(play, 0.9, model=False))
    _publish(run, feed, 0)
    cue = run.board.cue(at(0))
    assert cue.payload == play
    run.board.committed(cue, at(16))
    [unspent] = [note for note in run.board.decisions() if isinstance(note, UnspentNote)]
    assert unspent.reason is PromotionRefusal.NO_ROOM
    assert run.promotions() == []


def test_a_promotion_with_no_break_to_check_is_still_judged_by_the_broker_at_its_first_frame() -> None:
    """In open rotation a promotion has no lease to hold: a newer revision of its moment, offered before its first
    frame, still rejects the one cued, which spends nothing."""
    first, second = games(3)[1:]
    play = big_play(first, "p1")
    run = Run()
    run.stage(live(first), live(second), play)
    run.describe(facts(first, 0.4), facts(second, 0.4))
    offer = major(play, 0.9, model=False)
    run.offers.append(offer)
    run.publish()
    cue = run.board.cue(at(0))
    assert cue.payload == play
    run.offers[:] = [dataclasses.replace(offer, revision=1)]
    run.publish()
    run.board.committed(cue, at(0.5))
    [unspent] = [note for note in run.board.decisions() if isinstance(note, UnspentNote)]
    assert unspent.reason is PromotionRefusal.SUPERSEDED
