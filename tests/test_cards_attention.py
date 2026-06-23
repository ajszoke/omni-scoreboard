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
    policy = AttentionPolicy(
        mode=AttentionMode.BURST,
        takeover_for=DurationSeconds(8),
        cooldown=DurationSeconds(30),
        max_repeats=1,
    )
    assert policy.mode is AttentionMode.BURST
    assert policy.takeover_for == DurationSeconds(8)
    assert policy.max_repeats == 1


def test_max_repeats_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="max_repeats cannot be negative"):
        AttentionPolicy(mode=AttentionMode.RECURRING, max_repeats=-1)
    assert AttentionPolicy(mode=AttentionMode.RECURRING, max_repeats=0).max_repeats == 0  # zero is allowed


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
