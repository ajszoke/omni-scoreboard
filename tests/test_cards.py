"""Tests for omni.cards scaffolding: timing, layout support, and ScoreboardCard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def make_contest() -> Contest:
    return Contest(
        id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
    )


def test_display_timing_availability_window() -> None:
    timing = DisplayTiming(
        available_at=T,
        expires_at=T + timedelta(minutes=5),
        min_display=DurationSeconds(5),
        max_display=DurationSeconds(30),
    )
    assert not timing.is_available(T - timedelta(seconds=1))
    assert timing.is_available(T)
    assert timing.is_available(T + timedelta(minutes=4))
    assert not timing.is_available(T + timedelta(minutes=5))  # expired at the boundary


def test_layout_support_membership() -> None:
    support = LayoutSupport(profiles=frozenset({PanelProfile.QUAD_128X64}))
    assert support.supports(PanelProfile.QUAD_128X64)
    assert not support.supports(PanelProfile.SINGLE_64X32)


def test_scoreboard_card_exposes_league_from_contest() -> None:
    card = ScoreboardCard(
        id=CardId("c1"),
        kind=CardKind.LIVE_GAME,
        contest=make_contest(),
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.FAVORITE, score=42.0),
        layout_support=LayoutSupport(profiles=frozenset({PanelProfile.QUAD_128X64})),
        dedupe_key=DedupeKey("g1:live"),
        payload={"placeholder": "payload"},
    )
    assert card.league is League.MLB
    assert card.kind is CardKind.LIVE_GAME
    assert card.priority.band is DisplayPriority.FAVORITE
    assert card.source_event_ids == ()  # default


def test_display_timing_without_expiry_is_available_indefinitely() -> None:
    timing = DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30))
    assert not timing.is_available(T - timedelta(seconds=1))
    assert timing.is_available(T)
    assert timing.is_available(T + timedelta(days=3650))  # no expires_at -> available far in the future
