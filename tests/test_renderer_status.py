"""Tests for the status (delay / suspension) card: factory, layouts, goldens.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k status
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from omni.cards.attention import AttentionMode
from omni.cards.baseball import StatusCardPayload
from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import Contest, TeamGame
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.pillow_canvas import PillowCanvas
from omni.renderers.registry import default_registry
from omni.renderers.status import StatusRenderer, status_banner

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
ALL_PROFILES = list(PanelProfile)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
_REG = MlbTeamRegistry.from_color_file()
AWAY = _REG.resolve(115)  # COL
HOME = _REG.resolve(119)  # LAD


def _game(status: GameStatus = GameStatus.DELAYED) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=status,
        scheduled_start=NOW,
        away=AWAY,
        home=HOME,
    )


def _card(status: GameStatus = GameStatus.DELAYED) -> ScoreboardCard[StatusCardPayload]:
    return CardFactory().status(_game(status), status=status, now=NOW)


def _render(card: ScoreboardCard[StatusCardPayload], profile: PanelProfile) -> RecordingCanvas:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    StatusRenderer().render(card, RenderContext(profile=profile, now=NOW), canvas)
    return canvas


# --- factory ----------------------------------------------------------------------


def test_factory_builds_a_status_card() -> None:
    card = _card()
    assert card.kind is CardKind.STATUS
    assert card.dedupe_key.raw == "g1:status"  # one per game, refreshed while paused
    assert card.timing.expires_at is None  # lives until the pipeline removes it (resume / final)
    assert card.attention.mode is AttentionMode.NORMAL  # a paused game sits in normal rotation
    assert card.layout_support.compromise_notes == ()  # matchup + banner fit every profile


# --- banner + layouts -------------------------------------------------------------


def test_banner_names_the_paused_state() -> None:
    assert status_banner(GameStatus.DELAYED) == "DELAYED"
    assert status_banner(GameStatus.SUSPENDED) == "SUSPENDED"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_banner_and_matchup_on_every_profile(profile: PanelProfile) -> None:
    texts = {t.text for t in _render(_card(GameStatus.DELAYED), profile).texts()}
    assert "DELAYED" in texts  # the banner shows on every profile — no crop
    assert AWAY.abbreviation in texts and HOME.abbreviation in texts  # both teams named


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_suspended_banner_on_every_profile(profile: PanelProfile) -> None:
    assert "SUSPENDED" in {t.text for t in _render(_card(GameStatus.SUSPENDED), profile).texts()}


def test_default_registry_routes_status_to_its_renderer() -> None:
    canvas = RecordingCanvas(128, 64)
    default_registry().render(_card(), RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)
    assert "DELAYED" in {t.text for t in canvas.texts()}


def test_renderer_rejects_non_teamgame_contest() -> None:
    import dataclasses

    bad = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.DELAYED,
        scheduled_start=NOW,
    )
    card = dataclasses.replace(_card(), contest=bad)
    with pytest.raises(TypeError):
        StatusRenderer().render(
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
def test_golden_delayed_per_profile(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    StatusRenderer().render(_card(GameStatus.DELAYED), RenderContext(profile=profile, now=NOW), canvas)
    _assert_matches_golden(canvas.image(), f"status_delayed_{profile.to_json_value()}.png")


def test_golden_suspended_quad() -> None:
    width, height = geometry_for(PanelProfile.QUAD_128X64).size
    canvas = PillowCanvas(width, height)
    StatusRenderer().render(
        _card(GameStatus.SUSPENDED), RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas
    )
    _assert_matches_golden(canvas.image(), "status_suspended_quad_128x64.png")
