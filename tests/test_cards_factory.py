"""Tests for the CardFactory: domain state -> renderable ScoreboardCard."""

from __future__ import annotations

from datetime import datetime, timezone

from omni.cards.base import CardKind, CardPriority
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    BatterGameLine,
    InningPhase,
    PitcherGameLine,
)
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
T = datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")


def _game() -> TeamGame:
    reg = MlbTeamRegistry.from_color_file()
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "700001"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=reg.resolve(115, full_name="Colorado Rockies"),
        home=reg.resolve(119, full_name="Los Angeles Dodgers"),
    )


def _state() -> BaseballGameState:
    return BaseballGameState(
        away_score=3,
        home_score=5,
        away_hits=7,
        home_hits=9,
        away_errors=1,
        home_errors=0,
        inning=7,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=BaseballBaseState(first=True, third=True),
        batter=BatterGameLine(name="Betts", at_bats=4, hits=2),
        pitcher=PitcherGameLine(name="Webb", innings_pitched="6.1", pitches=95, strikeouts=7),
    )


def test_live_baseball_card_carries_state_and_metadata() -> None:
    card = CardFactory().live_baseball(_game(), _state(), now=NOW)

    assert card.kind is CardKind.LIVE_GAME
    assert card.league is League.MLB
    assert card.contest is not None and card.contest.id.raw == "700001"
    assert card.id.raw == "700001:live"
    assert card.dedupe_key.raw == "700001:live"

    p = card.payload
    assert (p.away_line.runs, p.home_line.runs) == (3, 5)  # runs flow from the state's scores
    assert (p.away_line.hits, p.away_line.errors) == (7, 1)  # and the H/E totals compose into the linescore
    assert (p.home_line.hits, p.home_line.errors) == (9, 0)
    assert p.batter is not None and p.batter.name == "Betts"  # current at-bat flows to the payload
    assert p.pitcher is not None and p.pitcher.name == "Webb"  # current pitcher flows too
    assert p.inning == 7 and p.phase is InningPhase.TOP
    assert (p.count.balls, p.count.strikes, p.count.outs) == (2, 1, 2)
    assert p.bases.first and p.bases.third and not p.bases.second


def test_default_timing_and_priority() -> None:
    card = CardFactory().live_baseball(_game(), _state(), now=NOW)
    assert card.timing.available_at == NOW
    assert card.timing.min_display == DurationSeconds(8)
    assert card.timing.max_display == DurationSeconds(30)
    assert card.timing.is_available(NOW)
    assert card.priority.band is DisplayPriority.NORMAL
    assert card.priority.score == 0.0


def test_supports_all_three_profiles() -> None:
    card = CardFactory().live_baseball(_game(), _state(), now=NOW)
    for profile in PanelProfile:
        assert card.layout_support.supports(profile)


def test_priority_override_is_respected() -> None:
    high = CardPriority(band=DisplayPriority.HIGH_LEVERAGE, score=88.0, reasons=("close late game",))
    card = CardFactory().live_baseball(_game(), _state(), now=NOW, priority=high)
    assert card.priority is high


def test_display_durations_are_configurable() -> None:
    factory = CardFactory(live_min_display=DurationSeconds(3), live_max_display=DurationSeconds(12))
    card = factory.live_baseball(_game(), _state(), now=NOW)
    assert card.timing.min_display == DurationSeconds(3)
    assert card.timing.max_display == DurationSeconds(12)
