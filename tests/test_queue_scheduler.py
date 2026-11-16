"""Tests for the InterleavedCardQueue: dedupe, eligibility, two-level fairness, burst."""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import datetime, timedelta, timezone

from omni.cards.attention import AttentionMode, AttentionPolicy
from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, LayoutSupport, ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.scheduler import InterleavedCardQueue, Pick, SelectionReason

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()
_STATE = BaseballGameState(
    away_score=0,
    home_score=0,
    inning=1,
    phase=InningPhase.TOP,
    count=BaseballCount(balls=0, strikes=0, outs=0),
    bases=BaseballBaseState(),
)


def _at(moment: datetime) -> ClockReading:
    """``moment`` read both ways, the monotonic clock running alongside UTC."""
    return ClockReading(moment, MonotonicTime((moment - NOW).total_seconds()))


def _card(
    gid: str,
    *,
    band: DisplayPriority = DisplayPriority.NORMAL,
    score: float = 0.0,
    profiles: list[PanelProfile] | None = None,
    available_at: datetime = NOW,
    expires_at: datetime | None = None,
    attention: AttentionPolicy | None = None,
) -> ScoreboardCard[LiveBaseballCardPayload]:
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, gid),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )
    card = CardFactory().live_baseball(game, _STATE, now=available_at, priority=CardPriority(band=band, score=score))
    changes: dict[str, object] = {}
    if expires_at is not None:
        changes["timing"] = dataclasses.replace(card.timing, expires_at=expires_at)
    if profiles is not None:
        changes["layout_support"] = LayoutSupport(profiles=frozenset(profiles))
    if attention is not None:
        changes["attention"] = attention
    return dataclasses.replace(card, **changes) if changes else card  # type: ignore[arg-type]


def _sibling(
    card: ScoreboardCard[LiveBaseballCardPayload], suffix: str, *, band: DisplayPriority
) -> ScoreboardCard[LiveBaseballCardPayload]:
    """A second card for the SAME contest (same group, different dedupe key)."""
    return dataclasses.replace(
        card,
        id=CardId(f"{card.contest.id.raw}:{suffix}"),
        kind=CardKind.BIG_PLAY,
        dedupe_key=DedupeKey(f"{card.contest.id.raw}:{suffix}"),
        priority=CardPriority(band=band, score=0.0),
    )


def _shown(card: ScoreboardCard[LiveBaseballCardPayload] | None) -> str:
    assert card is not None
    return card.contest.id.raw


def test_empty_queue_returns_none() -> None:
    assert InterleavedCardQueue().next_card(_at(NOW), QUAD) is None


def test_ingest_dedupes_by_key_latest_wins() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1", score=1.0))
    queue.ingest(_card("g1", score=99.0))  # same dedupe key "g1:live"
    assert len(queue) == 1
    chosen = queue.next_card(_at(NOW), QUAD)
    assert chosen is not None and chosen.priority.score == 99.0


def test_profile_support_filters_eligibility() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1", profiles=[PanelProfile.QUAD_128X64]))
    assert queue.next_card(_at(NOW), PanelProfile.SINGLE_64X32) is None
    assert queue.next_card(_at(NOW), QUAD) is not None


def test_card_is_not_shown_before_its_available_at() -> None:
    queue = InterleavedCardQueue()
    future = NOW + timedelta(seconds=60)
    queue.ingest(_card("g1", available_at=future))
    assert queue.next_card(_at(NOW), QUAD) is None
    assert queue.next_card(_at(future), QUAD) is not None


def test_expired_cards_are_pruned_and_not_shown() -> None:
    queue = InterleavedCardQueue()
    # Available a minute ago, expiring exactly at NOW: now >= expires_at -> gone.
    queue.ingest(_card("g1", available_at=NOW - timedelta(seconds=60), expires_at=NOW))
    assert len(queue) == 1
    assert queue.next_card(_at(NOW), QUAD) is None
    assert len(queue) == 0


def test_two_equal_cards_alternate_fairly() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    seq = [_shown(queue.next_card(_at(NOW), QUAD)) for _ in range(4)]
    assert seq[0] != seq[1]  # no monopoly
    assert seq == [seq[0], seq[1], seq[0], seq[1]]  # stable round-robin


def test_high_leverage_gets_more_airtime_among_many() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("hot", band=DisplayPriority.HIGH_LEVERAGE, score=50.0))
    for i in range(3):
        queue.ingest(_card(f"n{i}"))
    counts = Counter(_shown(queue.next_card(_at(NOW), QUAD)) for _ in range(24))
    # The high-leverage game is shown more often than any single normal game,
    # but the normal games are not buried (each still appears).
    assert all(counts["hot"] > counts[f"n{i}"] for i in range(3))
    assert all(counts[f"n{i}"] > 0 for i in range(3))


_BURST_8S = AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(8))


def test_a_burst_takes_over_once_then_rotates() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    queue.ingest(_card("walkoff", band=DisplayPriority.ALERT, score=100.0, attention=_BURST_8S))
    first = queue.pick(_at(NOW + timedelta(seconds=2)), QUAD)
    assert first is not None and _shown(first.card) == "walkoff" and first.reason is SelectionReason.TAKEOVER
    # Once it has aired it takes its turns in rotation, however soon after: the others aren't buried.
    after = [queue.pick(_at(NOW + timedelta(seconds=3)), QUAD) for _ in range(8)]
    assert {pick.reason for pick in after if pick is not None} == {SelectionReason.ROTATION}
    resumed = Counter(_shown(pick.card) for pick in after if pick is not None)
    assert resumed["g1"] > 0 and resumed["g2"] > 0  # no permanent takeover


def test_alert_band_alone_does_not_take_over() -> None:
    # ALERT band with default (NORMAL) attention earns more airtime but never monopolizes.
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    queue.ingest(_card("loud", band=DisplayPriority.ALERT, score=100.0))  # no AttentionPolicy
    seen = Counter(_shown(queue.next_card(_at(NOW), QUAD)) for _ in range(12))
    assert seen["loud"] > seen["g1"]  # weighted up...
    assert seen["g1"] > 0 and seen["g2"] > 0  # ...but the others still appear


def test_rotation_resumes_after_burst_card_removed() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    queue.ingest(_card("walkoff", band=DisplayPriority.ALERT, attention=_BURST_8S))
    assert _shown(queue.next_card(_at(NOW), QUAD)) == "walkoff"  # bursting
    queue.remove("walkoff:live")
    resumed = {_shown(queue.next_card(_at(NOW), QUAD)) for _ in range(4)}
    assert resumed == {"g1", "g2"}


def test_sibling_cards_for_one_contest_share_airtime() -> None:
    # Two cards for the SAME contest (one group) must both surface.
    queue = InterleavedCardQueue()
    live = _card("g1")
    queue.ingest(live)
    queue.ingest(_sibling(live, "bigplay", band=DisplayPriority.NORMAL))
    shown: Counter[str] = Counter()
    for _ in range(6):
        card = queue.next_card(_at(NOW), QUAD)
        assert card is not None
        shown[card.dedupe_key.raw] += 1
    assert shown["g1:live"] > 0 and shown["g1:bigplay"] > 0  # neither sibling starves


def test_removing_a_card_forgets_its_recency() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.next_card(_at(NOW), QUAD)  # records recency for g1
    queue.remove("g1:live")
    assert queue._card_last_shown == {} and queue._group_last_shown == {}  # no leak


def test_scored_and_delayed_cards_compose_through_the_queue() -> None:
    # A lightweight capstone: priced + delay-released cards rotate in the queue.
    from omni.core.time import DurationSeconds
    from omni.queue.delay_buffer import DelayBuffer

    buffer: DelayBuffer[ScoreboardCard[LiveBaseballCardPayload]] = DelayBuffer(DurationSeconds(10))
    buffer.push(_card("g1"), observed_at=NOW)
    buffer.push(_card("g2", band=DisplayPriority.FAVORITE, score=30.0), observed_at=NOW)

    queue = InterleavedCardQueue()
    assert buffer.release(NOW) == []  # still delayed
    later = NOW + timedelta(seconds=10)
    queue.ingest_all(buffer.release(later))
    assert len(queue) == 2
    # The favorite shows first (higher band breaks the equal-staleness tie).
    assert _shown(queue.next_card(_at(later), QUAD)) == "g2"


# --- RECURRING attention (the no-hitter cadence) ----------------------------------


def _recurring(
    gid: str, *, cooldown_s: int = 60, max_repeats: int | None = None
) -> ScoreboardCard[LiveBaseballCardPayload]:
    return _card(
        gid,
        band=DisplayPriority.ALERT,
        attention=AttentionPolicy(
            mode=AttentionMode.RECURRING, cooldown=DurationSeconds(cooldown_s), max_repeats=max_repeats
        ),
    )


def test_recurring_surfaces_on_its_beat_then_holds_back() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_recurring("nono"))  # a no-hitter-like RECURRING card
    queue.ingest(_card("live"))  # a normal live card, different game
    assert _shown(queue.next_card(_at(NOW), QUAD)) == "nono"  # due on first sight
    # Within the cooldown it is held back; the normal card rotates instead.
    assert _shown(queue.next_card(_at(NOW + timedelta(seconds=5)), QUAD)) == "live"
    assert _shown(queue.next_card(_at(NOW + timedelta(seconds=30)), QUAD)) == "live"
    # Once the cooldown elapses it is due again and resurfaces.
    assert _shown(queue.next_card(_at(NOW + timedelta(seconds=65)), QUAD)) == "nono"


def test_recurring_is_capped_by_max_repeats() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_recurring("nono", cooldown_s=10, max_repeats=2))
    queue.ingest(_card("live"))
    shown = [_shown(queue.next_card(_at(NOW + timedelta(seconds=s)), QUAD)) for s in range(0, 60, 10)]
    assert shown[0] == "nono"  # due on first sight
    assert shown.count("nono") == 2  # then never again past its cap — yields to rotation


def test_burst_preempts_a_due_recurring() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_recurring("nono"))
    queue.ingest(
        _card(
            "bang",
            band=DisplayPriority.ALERT,
            attention=AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(8)),
        )
    )
    # Both want the screen now, but a BURST (a scoring play) outranks a periodic reminder.
    assert _shown(queue.next_card(_at(NOW), QUAD)) == "bang"


def test_due_recurring_preempts_even_a_stale_normal_card() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("live"))
    for s in (0, 10, 20):  # let the live card accumulate staleness
        queue.next_card(_at(NOW + timedelta(seconds=s)), QUAD)
    queue.ingest(_recurring("nono"))
    assert _shown(queue.next_card(_at(NOW + timedelta(seconds=30)), QUAD)) == "nono"  # the reminder still preempts


def test_recurring_alone_and_not_due_shows_nothing() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_recurring("nono", cooldown_s=60))
    assert _shown(queue.next_card(_at(NOW), QUAD)) == "nono"  # due on first sight
    # The only card, now within its cooldown: a periodic reminder is not a constant one.
    assert queue.next_card(_at(NOW + timedelta(seconds=10)), QUAD) is None


def test_each_pick_names_why_it_was_chosen() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("live"))
    queue.ingest(_recurring("nono"))
    queue.ingest(_card("walkoff", band=DisplayPriority.ALERT, score=100.0, attention=_BURST_8S))
    picks = [queue.pick(_at(NOW + timedelta(seconds=s)), QUAD) for s in (0, 10, 11)]
    assert [(_shown(p.card), p.reason) for p in picks if p is not None][:2] == [
        ("walkoff", SelectionReason.TAKEOVER),  # inside its burst window
        ("nono", SelectionReason.REMINDER),  # due on first sight, once the burst is over
    ]
    assert picks[2] is not None and picks[2].reason is SelectionReason.ROTATION  # then the usual turns
    assert queue.pick(_at(NOW), PanelProfile.SINGLE_64X32) is not None
    assert InterleavedCardQueue().pick(_at(NOW), QUAD) is None


# --- choosing apart from booking ------------------------------------------------------


def test_peeking_records_nothing_and_booking_records_the_turn() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    first = queue.peek(_at(NOW), QUAD)
    assert first is not None and queue.peek(_at(NOW), QUAD) == first  # nothing was booked
    assert not queue.aired(first.card.dedupe_key.raw)
    queue.book(first, _at(NOW))
    assert queue.aired(first.card.dedupe_key.raw)
    second = queue.peek(_at(NOW), QUAD)
    assert second is not None and second.card.contest.id != first.card.contest.id  # the other game's turn


def test_booking_a_card_that_left_the_queue_is_ignored() -> None:
    queue = InterleavedCardQueue()
    card = _card("g1")
    queue.ingest(card)
    pick = queue.peek(_at(NOW), QUAD)
    assert pick is not None
    queue.remove(card.dedupe_key.raw)
    queue.book(pick, _at(NOW))
    assert queue.card(card.dedupe_key.raw) is None and queue._card_last_shown == {}


def test_a_card_reads_back_as_its_newest_version() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1", score=1.0))
    queue.ingest(_card("g1", score=2.0))
    newest = queue.card("g1:live")
    assert newest is not None and newest.priority.score == 2.0
    assert queue.card("g2:live") is None


def test_a_reason_ranks_a_takeover_over_a_reminder_over_rotation() -> None:
    assert SelectionReason.TAKEOVER.rank > SelectionReason.REMINDER.rank > SelectionReason.ROTATION.rank


def test_with_takeovers_off_a_burst_waits_for_another_card_unless_it_is_alone() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("bang", band=DisplayPriority.ALERT, attention=_BURST_8S))
    alone = queue.peek(_at(NOW), QUAD, takeovers=False)
    assert alone is not None and _shown(alone.card) == "bang" and alone.reason is SelectionReason.ROTATION
    queue.ingest(_card("g1"))
    other = queue.peek(_at(NOW), QUAD, takeovers=False)
    assert other is not None and _shown(other.card) == "g1"


# --- required ordering and a usable window --------------------------------------------


def _final(gid: str, *, ended: datetime, after: tuple[DedupeKey, ...] = ()) -> ScoreboardCard[LiveBaseballCardPayload]:
    card = _card(gid, available_at=ended)
    key = f"{gid}:final"
    return dataclasses.replace(card, id=CardId(key), kind=CardKind.FINAL, dedupe_key=DedupeKey(key), after=after)


def test_finals_that_have_not_aired_go_in_the_order_their_games_ended() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_final("a-late", ended=NOW - timedelta(seconds=10)))
    queue.ingest(_final("z-early", ended=NOW - timedelta(seconds=20)))
    order = [_shown(queue.next_card(_at(NOW), QUAD)) for _ in range(3)]
    assert order == ["z-early", "a-late", "z-early"]  # once both have aired, they rotate as usual


def test_a_card_waits_for_an_eligible_predecessor_to_air() -> None:
    queue = InterleavedCardQueue()
    walk_off = _sibling(_card("g1"), "bigplay", band=DisplayPriority.NORMAL)
    queue.ingest(walk_off)
    queue.ingest(_final("g1", ended=NOW, after=(walk_off.dedupe_key,)))
    queue.ingest(_card("g2"))
    order = [card.dedupe_key.raw for card in (queue.next_card(_at(NOW), QUAD) for _ in range(4)) if card is not None]
    assert order.index("g1:bigplay") < order.index("g1:final")  # the walk-off airs before its final


def test_a_predecessor_that_cannot_air_holds_nothing_up() -> None:
    queue = InterleavedCardQueue()
    gone = DedupeKey("g1:bigplay:gone")
    unsupported = dataclasses.replace(_card("g9", profiles=[PanelProfile.SINGLE_64X32]), dedupe_key=DedupeKey("g9:x"))
    queue.ingest(unsupported)
    queue.ingest(_final("g1", ended=NOW, after=(gone, unsupported.dedupe_key)))
    assert _shown(queue.next_card(_at(NOW), QUAD)) == "g1"


def test_a_card_with_less_time_left_than_its_minimum_is_passed_over() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1", available_at=NOW - timedelta(seconds=60), expires_at=NOW + timedelta(seconds=5)))
    assert queue.peek(_at(NOW), QUAD) is None  # 5 s left, 8 s minimum
    assert queue.peek(_at(NOW - timedelta(seconds=10)), QUAD) is not None


def test_the_next_deadline_is_the_soonest_change_in_eligibility() -> None:
    queue = InterleavedCardQueue()
    assert queue.next_deadline(_at(NOW), QUAD) is None
    queue.ingest(_card("later", available_at=NOW + timedelta(seconds=30)))
    queue.ingest(_card("brief", available_at=NOW - timedelta(seconds=60), expires_at=NOW + timedelta(seconds=20)))
    queue.ingest(_card("elsewhere", available_at=NOW + timedelta(seconds=1), profiles=[PanelProfile.SINGLE_64X32]))
    # "brief" stops being usable when 8 s are left: 12 s from now.
    assert queue.next_deadline(_at(NOW), QUAD) == MonotonicTime(12.0)
    reminder = _recurring("nono", cooldown_s=5)
    queue.ingest(reminder)
    queue.book(Pick(reminder, SelectionReason.REMINDER), _at(NOW))
    assert queue.next_deadline(_at(NOW), QUAD) == MonotonicTime(5.0)  # due again
    assert queue.next_deadline(_at(NOW + timedelta(seconds=6)), QUAD) == MonotonicTime(12.0)  # due already
