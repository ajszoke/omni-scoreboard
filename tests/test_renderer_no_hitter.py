"""Tests for the no-hitter card: RECURRING attention, layouts, goldens.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k no_hitter
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from omni.cards.attention import AttentionMode
from omni.cards.baseball import NoHitterCardPayload
from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import Contest, TeamGame
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.no_hitter import NoHitterRenderer, headline_for
from omni.renderers.pillow_canvas import PillowCanvas
from omni.renderers.registry import default_registry

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
ALL_PROFILES = list(PanelProfile)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
_REG = MlbTeamRegistry.from_color_file()
AWAY = _REG.resolve(115)  # COL
HOME = _REG.resolve(119)  # LAD


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
        away=AWAY,
        home=HOME,
    )


def _card(
    *,
    perfect: bool = False,
    through: int = 7,
    side: HomeAway = HomeAway.HOME,
) -> ScoreboardCard[NoHitterCardPayload]:
    return CardFactory().no_hitter(_game(), pitching_side=side, through_inning=through, perfect=perfect, now=NOW)


def _render(card: ScoreboardCard[NoHitterCardPayload], profile: PanelProfile) -> RecordingCanvas:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    NoHitterRenderer().render(card, RenderContext(profile=profile, now=NOW), canvas)
    return canvas


# --- factory + RECURRING attention ------------------------------------------------


def test_factory_builds_a_no_hitter_card() -> None:
    card = _card()
    assert card.kind is CardKind.NO_HITTER
    assert card.dedupe_key.raw == "g1:nohitter"  # one per game, refreshed as it carries
    assert card.source_event_ids == ()  # a standing condition, not a one-shot event
    assert card.timing.expires_at is None  # lives until the pipeline removes it


def test_no_hitter_uses_bounded_recurring_attention() -> None:
    card = _card()
    assert card.attention.mode is AttentionMode.RECURRING  # resurfaces periodically, not constantly
    assert card.attention.cooldown.value > 0  # paced by a cooldown
    assert card.attention.max_repeats is None  # unbounded while the bid is alive
    assert card.priority.band is DisplayPriority.ALERT


def test_perfect_game_outranks_a_plain_no_hitter() -> None:
    assert _card(perfect=True).priority.score > _card(perfect=False).priority.score
    assert _card(perfect=True).priority.reasons == ("perfect game",)
    assert _card(perfect=False).priority.reasons == ("no-hitter",)


# --- headline + layouts -----------------------------------------------------------


def test_headline_distinguishes_perfect_from_no_hitter() -> None:
    assert headline_for(True) == "PERFECT GAME"
    assert headline_for(False) == "NO-HITTER"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_headline_and_team_on_every_profile(profile: PanelProfile) -> None:
    texts = {t.text for t in _render(_card(side=HomeAway.HOME), profile).texts()}
    assert "NO-HITTER" in texts
    assert HOME.abbreviation in texts  # the pitching (defending) team is named on every profile


def test_team_follows_the_pitching_side() -> None:
    home_texts = {t.text for t in _render(_card(side=HomeAway.HOME), PanelProfile.QUAD_128X64).texts()}
    away_texts = {t.text for t in _render(_card(side=HomeAway.AWAY), PanelProfile.QUAD_128X64).texts()}
    assert HOME.abbreviation in home_texts and AWAY.abbreviation not in home_texts
    assert AWAY.abbreviation in away_texts and HOME.abbreviation not in away_texts


def test_through_inning_shown_on_large_panels() -> None:
    quad = {t.text for t in _render(_card(through=7), PanelProfile.QUAD_128X64).texts()}
    stack = {t.text for t in _render(_card(through=7), PanelProfile.STACK_64X64).texts()}
    assert "THROUGH 7" in quad
    assert "THRU 7" in stack


def test_single_profile_compromise_drops_the_inning() -> None:
    texts = {t.text for t in _render(_card(through=7), PanelProfile.SINGLE_64X32).texts()}
    assert not any("THROUGH" in t or "THRU" in t for t in texts)  # inning dropped at 64x32
    assert "NO-HITTER" in texts and HOME.abbreviation in texts  # headline + team remain


def test_compromise_is_declared_on_the_card() -> None:
    assert any("single_64x32" in note for note in _card().layout_support.compromise_notes)


def test_default_registry_routes_no_hitter_to_its_renderer() -> None:
    canvas = RecordingCanvas(128, 64)
    default_registry().render(_card(), RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)
    assert "NO-HITTER" in {t.text for t in canvas.texts()}


def test_renderer_rejects_non_teamgame_contest() -> None:
    import dataclasses

    bad = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
    )
    card = dataclasses.replace(_card(), contest=bad)
    with pytest.raises(TypeError):
        NoHitterRenderer().render(
            card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), RecordingCanvas(128, 64)
        )


# --- goldens ----------------------------------------------------------------------


def _assert_matches_golden(image: Image.Image, name: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("OMNI_REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return
    assert path.exists(), f"missing golden {name}; regenerate with OMNI_REGEN_GOLDEN=1"
    expected = Image.open(path).convert("RGB")
    assert image.convert("RGB").tobytes() == expected.tobytes(), f"render differs from golden {name}"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_golden_image_per_profile(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    NoHitterRenderer().render(_card(perfect=True), RenderContext(profile=profile, now=NOW), canvas)
    _assert_matches_golden(canvas.image(), f"no_hitter_{profile.to_json_value()}.png")
