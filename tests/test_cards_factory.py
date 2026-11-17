"""Tests for the CardFactory: domain state -> renderable ScoreboardCard."""

from __future__ import annotations

import dataclasses

from datetime import datetime, timezone
from typing import Any

from omni.cards.base import CardKind, CardPriority, ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    BatterGameLine,
    DueUp,
    InningPhase,
    PitcherGameLine,
    PitchSnapshot,
    PitchType,
)
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nfl_teams import NflTeamRegistry
from omni.queue.scheduler import InterleavedCardQueue
from tests.test_renderer_big_play import _event

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
        last_pitch=PitchSnapshot(velocity_mph=88, pitch_type=PitchType.SWEEPER),
    )


def test_live_baseball_card_carries_state_and_metadata() -> None:
    card = CardFactory().live_baseball(_game(), _state(), now=NOW)

    assert card.kind is CardKind.LIVE_GAME
    assert card.league is League.MLB
    assert card.contest is not None and card.contest.id.raw == "700001"
    assert card.id.raw == "mlb:mlb_statsapi:700001:live"
    assert card.dedupe_key.raw == "mlb:mlb_statsapi:700001:live"

    p = card.payload
    assert (p.away_line.runs, p.home_line.runs) == (3, 5)  # runs flow from the state's scores
    assert (p.away_line.hits, p.away_line.errors) == (7, 1)  # and the H/E totals compose into the linescore
    assert (p.home_line.hits, p.home_line.errors) == (9, 0)
    assert p.batter is not None and p.batter.name == "Betts"  # current at-bat flows to the payload
    assert p.pitcher is not None and p.pitcher.name == "Webb"  # current pitcher flows too
    assert p.last_pitch is not None and p.last_pitch.token == "88 SWPR"  # the live pitch flows too
    assert p.inning == 7 and p.phase is InningPhase.TOP
    assert (p.count.balls, p.count.strikes, p.count.outs) == (2, 1, 2)
    assert p.bases.first and p.bases.third and not p.bases.second


def test_live_baseball_declares_its_per_profile_compromises() -> None:
    # Like every other card, the live card's small-profile omissions belong in the machine-readable
    # contract (LayoutSupport.compromise_notes), not only in the renderer docstring + tests.
    card = CardFactory().live_baseball(_game(), _state(), now=NOW)
    joined = " ".join(card.layout_support.compromise_notes)
    assert "single_64x32" in joined and "stack_64x64" in joined
    assert "bases" in joined and "pitcher/batter" in joined  # the single drops live detail; the stack the strip


def test_live_baseball_carries_due_up_and_declares_it_quad_only() -> None:
    due_up = DueUp(batters=(BatterGameLine(name="Ohtani", at_bats=3, hits=2),))
    state = dataclasses.replace(_state(), phase=InningPhase.MIDDLE, due_up=due_up)
    card = CardFactory().live_baseball(_game(), state, now=NOW)
    assert card.payload.due_up == due_up
    notes = card.layout_support.compromise_notes
    assert sum("Due Up" in note for note in notes) == 2  # both small boards drop it


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


def test_an_mlb_and_an_nfl_game_with_the_same_raw_id_never_share_a_card_or_a_key() -> None:
    mlb = _game()
    clubs = NflTeamRegistry.default()
    nfl = TeamGame(
        id=LeagueScopedId(League.NFL, SourceRef("nflmeta"), mlb.id.raw),  # the same raw id: 700001
        league=League.NFL,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=clubs.resolve("PIT"),
        home=clubs.resolve("CIN"),
    )
    play = _event(eid="700001:ab:1")
    factory = CardFactory()

    def cards(game: TeamGame) -> list[ScoreboardCard[Any]]:
        return [
            factory.live_baseball(game, _state(), now=NOW),
            factory.pregame(game, now=NOW),
            factory.final(game, _state(), now=NOW),
            factory.big_play(game, play, now=NOW),
            factory.no_hitter(game, pitching_side=HomeAway.HOME, through_inning=6, now=NOW),
            factory.status(game, status=GameStatus.DELAYED, now=NOW),
        ]

    for game, scope in ((mlb, "mlb:mlb_statsapi:700001"), (nfl, "nfl:nflmeta:700001")):
        built = cards(game)
        expected = [
            f"{scope}:{kind}" for kind in ("live", "pregame", "final", "bigplay:700001:ab:1", "nohitter", "status")
        ]
        assert [card.id.raw for card in built] == expected
        assert [card.dedupe_key.raw for card in built] == expected
        assert CardFactory.big_play_key(game, play).raw == f"{scope}:bigplay:700001:ab:1"
    queue = InterleavedCardQueue()
    queue.ingest_all([*cards(mlb), *cards(nfl)])
    assert len(queue) == 12  # no card of one game replaced the other's
