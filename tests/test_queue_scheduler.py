"""Tests for the InterleavedCardQueue: dedupe, eligibility, two-level fairness, burst."""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import datetime, timedelta, timezone

from omni.cards.attention import AttentionMode, AttentionPolicy
from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, LayoutSupport, ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, HalfInning
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.scheduler import InterleavedCardQueue

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()
_STATE = BaseballGameState(
    away_score=0,
    home_score=0,
    inning=1,
    half=HalfInning.TOP,
    count=BaseballCount(balls=0, strikes=0, outs=0),
    bases=BaseballBaseState(),
)


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
    assert InterleavedCardQueue().next_card(NOW, QUAD) is None


def test_ingest_dedupes_by_key_latest_wins() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1", score=1.0))
    queue.ingest(_card("g1", score=99.0))  # same dedupe key "g1:live"
    assert len(queue) == 1
    chosen = queue.next_card(NOW, QUAD)
    assert chosen is not None and chosen.priority.score == 99.0


def test_profile_support_filters_eligibility() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1", profiles=[PanelProfile.QUAD_128X64]))
    assert queue.next_card(NOW, PanelProfile.SINGLE_64X32) is None
    assert queue.next_card(NOW, QUAD) is not None


def test_card_is_not_shown_before_its_available_at() -> None:
    queue = InterleavedCardQueue()
    future = NOW + timedelta(seconds=60)
    queue.ingest(_card("g1", available_at=future))
    assert queue.next_card(NOW, QUAD) is None
    assert queue.next_card(future, QUAD) is not None


def test_expired_cards_are_pruned_and_not_shown() -> None:
    queue = InterleavedCardQueue()
    # Available a minute ago, expiring exactly at NOW: now >= expires_at -> gone.
    queue.ingest(_card("g1", available_at=NOW - timedelta(seconds=60), expires_at=NOW))
    assert len(queue) == 1
    assert queue.next_card(NOW, QUAD) is None
    assert len(queue) == 0


def test_two_equal_cards_alternate_fairly() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    seq = [_shown(queue.next_card(NOW, QUAD)) for _ in range(4)]
    assert seq[0] != seq[1]  # no monopoly
    assert seq == [seq[0], seq[1], seq[0], seq[1]]  # stable round-robin


def test_high_leverage_gets_more_airtime_among_many() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("hot", band=DisplayPriority.HIGH_LEVERAGE, score=50.0))
    for i in range(3):
        queue.ingest(_card(f"n{i}"))
    counts = Counter(_shown(queue.next_card(NOW, QUAD)) for _ in range(24))
    # The high-leverage game is shown more often than any single normal game,
    # but the normal games are not buried (each still appears).
    assert all(counts["hot"] > counts[f"n{i}"] for i in range(3))
    assert all(counts[f"n{i}"] > 0 for i in range(3))


_BURST_8S = AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(8))


def test_burst_card_takes_over_for_its_window_then_yields() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    queue.ingest(_card("walkoff", band=DisplayPriority.ALERT, score=100.0, attention=_BURST_8S))
    # Inside the 8s window the walk-off monopolizes the screen.
    for _ in range(4):
        assert _shown(queue.next_card(NOW + timedelta(seconds=2), QUAD)) == "walkoff"
    # After the window it rejoins normal rotation — the others are no longer buried.
    after = NOW + timedelta(seconds=10)
    resumed = Counter(_shown(queue.next_card(after, QUAD)) for _ in range(8))
    assert resumed["g1"] > 0 and resumed["g2"] > 0  # no permanent takeover


def test_alert_band_alone_does_not_take_over() -> None:
    # ALERT band with default (NORMAL) attention earns more airtime but never monopolizes.
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    queue.ingest(_card("loud", band=DisplayPriority.ALERT, score=100.0))  # no AttentionPolicy
    seen = Counter(_shown(queue.next_card(NOW, QUAD)) for _ in range(12))
    assert seen["loud"] > seen["g1"]  # weighted up...
    assert seen["g1"] > 0 and seen["g2"] > 0  # ...but the others still appear


def test_rotation_resumes_after_burst_card_removed() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.ingest(_card("g2"))
    queue.ingest(_card("walkoff", band=DisplayPriority.ALERT, attention=_BURST_8S))
    assert _shown(queue.next_card(NOW, QUAD)) == "walkoff"  # bursting
    queue.remove("walkoff:live")
    resumed = {_shown(queue.next_card(NOW, QUAD)) for _ in range(4)}
    assert resumed == {"g1", "g2"}


def test_sibling_cards_for_one_contest_share_airtime() -> None:
    # Two cards for the SAME contest (one group) must both surface — High #4.
    queue = InterleavedCardQueue()
    live = _card("g1")
    queue.ingest(live)
    queue.ingest(_sibling(live, "bigplay", band=DisplayPriority.NORMAL))
    shown: Counter[str] = Counter()
    for _ in range(6):
        card = queue.next_card(NOW, QUAD)
        assert card is not None
        shown[card.dedupe_key.raw] += 1
    assert shown["g1:live"] > 0 and shown["g1:bigplay"] > 0  # neither sibling starves


def test_removing_a_card_forgets_its_recency() -> None:
    queue = InterleavedCardQueue()
    queue.ingest(_card("g1"))
    queue.next_card(NOW, QUAD)  # records recency for g1
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
    assert _shown(queue.next_card(later, QUAD)) == "g2"
