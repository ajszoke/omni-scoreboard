"""Tests for AttentionPolicy / AttentionMode and the card default."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.cards.attention import NORMAL_ATTENTION, AttentionMode, AttentionPolicy
from omni.cards.base import (
    CardId,
    CardKind,
    CardPriority,
    DedupeKey,
    DisplayTiming,
    LayoutSupport,
    ScoreboardCard,
)
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import Contest

T = datetime(2026, 6, 17, 19, 5, tzinfo=timezone.utc)


def test_policy_defaults_are_inert() -> None:
    policy = AttentionPolicy(mode=AttentionMode.NORMAL)
    assert policy.takeover_for == DurationSeconds(0)
    assert policy.cooldown == DurationSeconds(0)
    assert policy.max_repeats is None


def test_bounded_burst_policy() -> None:
    policy = AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(8))
    assert policy.mode is AttentionMode.BURST
    assert policy.takeover_for == DurationSeconds(8)


def test_bounded_recurring_policy() -> None:
    policy = AttentionPolicy(mode=AttentionMode.RECURRING, cooldown=DurationSeconds(60), max_repeats=3)
    assert (policy.cooldown, policy.max_repeats) == (DurationSeconds(60), 3)
    # A zero cap is allowed (capped at zero resurfacings); only negatives are rejected.
    assert AttentionPolicy(mode=AttentionMode.RECURRING, cooldown=DurationSeconds(60), max_repeats=0).max_repeats == 0


def test_burst_needs_a_positive_takeover() -> None:
    with pytest.raises(ValueError, match="must take over for a positive duration"):
        AttentionPolicy(mode=AttentionMode.BURST)  # takeover_for defaults to 0 — a no-op burst


def test_recurring_needs_a_positive_cooldown() -> None:
    with pytest.raises(ValueError, match="needs a positive cooldown"):
        AttentionPolicy(mode=AttentionMode.RECURRING)  # cooldown defaults to 0 — would resurface every tick


def test_recurring_max_repeats_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="max_repeats cannot be negative"):
        AttentionPolicy(mode=AttentionMode.RECURRING, cooldown=DurationSeconds(60), max_repeats=-1)


@pytest.mark.parametrize("mode", [AttentionMode.NORMAL, AttentionMode.RECURRING, AttentionMode.BADGE])
def test_takeover_for_rejected_off_burst(mode: AttentionMode) -> None:
    with pytest.raises(ValueError, match="takeover_for is only meaningful for a BURST"):
        AttentionPolicy(mode=mode, takeover_for=DurationSeconds(5))


@pytest.mark.parametrize("mode", [AttentionMode.NORMAL, AttentionMode.BURST, AttentionMode.BADGE])
def test_cooldown_rejected_off_recurring(mode: AttentionMode) -> None:
    # A BURST carries a real takeover so it clears its own invariant; the cooldown is the violation.
    takeover = DurationSeconds(8) if mode is AttentionMode.BURST else DurationSeconds(0)
    with pytest.raises(ValueError, match="cooldown is only meaningful for a RECURRING"):
        AttentionPolicy(mode=mode, takeover_for=takeover, cooldown=DurationSeconds(5))


@pytest.mark.parametrize("mode", [AttentionMode.NORMAL, AttentionMode.BURST, AttentionMode.BADGE])
def test_max_repeats_rejected_off_recurring(mode: AttentionMode) -> None:
    takeover = DurationSeconds(8) if mode is AttentionMode.BURST else DurationSeconds(0)
    with pytest.raises(ValueError, match="max_repeats is only meaningful for a RECURRING"):
        AttentionPolicy(mode=mode, takeover_for=takeover, max_repeats=1)


def test_normal_attention_constant() -> None:
    assert NORMAL_ATTENTION.mode is AttentionMode.NORMAL


def test_scoreboard_card_defaults_to_normal_attention() -> None:
    contest = Contest(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
    )
    card = ScoreboardCard(
        id=CardId("c1"),
        kind=CardKind.LIVE_GAME,
        contest=contest,
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.NORMAL, score=0.0),
        layout_support=LayoutSupport(profiles=frozenset({PanelProfile.QUAD_128X64})),
        dedupe_key=DedupeKey("g1:live"),
        payload={"x": 1},
    )
    assert card.attention is NORMAL_ATTENTION
