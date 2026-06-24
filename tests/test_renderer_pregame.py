"""Tests for the pregame renderer: countdown formatting, per-profile layouts,
the small-panel compromise, golden snapshots, and a time-series countdown.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k pregame
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from omni.cards.baseball import PregameCardPayload
from omni.cards.factory import CardFactory
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import TeamGame
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.pregame import PregameRenderer, first_pitch_label
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.pillow_canvas import PillowCanvas

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
LOGOS = LogoStore()  # resolves the committed COL/LAD tiles for the logo + golden renders
ALL_PROFILES = list(PanelProfile)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()
START = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 6, 17, 21, 25, tzinfo=timezone.utc)  # 2h05m before first pitch
AWAY = _REG.resolve(115)
HOME = _REG.resolve(119)


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.PREGAME,
        scheduled_start=START,
        away=AWAY,
        home=HOME,
    )


def _render(profile: PanelProfile, *, now: datetime = NOW, logos: LogoStore | None = None) -> RecordingCanvas:
    card = CardFactory().pregame(_game(), now=now)
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    PregameRenderer().render(card, RenderContext(profile=profile, now=now, logos=logos), canvas)
    return canvas


# --- first_pitch_label boundaries -------------------------------------------------


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(hours=2, minutes=5), "2h05m"),
        (timedelta(hours=1), "1h00m"),
        (timedelta(minutes=90), "1h30m"),
        (timedelta(minutes=45), "45m"),
        (timedelta(minutes=1), "1m"),
        (timedelta(seconds=59), "SOON"),
        (timedelta(0), "SOON"),
        (timedelta(seconds=-120), "SOON"),  # past first pitch, still pregame
    ],
)
def test_first_pitch_label(delta: timedelta, expected: str) -> None:
    assert first_pitch_label(NOW, NOW + delta) == expected


# --- per-profile layouts ----------------------------------------------------------


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_matchup_and_countdown_on_every_profile(profile: PanelProfile) -> None:
    texts = {t.text for t in _render(profile).texts()}
    assert AWAY.abbreviation in texts and HOME.abbreviation in texts  # the matchup
    assert "2h05m" in texts  # the countdown


def test_first_pitch_label_shown_on_larger_panels() -> None:
    for profile in (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64):
        texts = {t.text for t in _render(profile).texts()}
        assert "FIRST PITCH" in texts


def test_single_panel_compromise_drops_the_label() -> None:
    # The explicit 64x32 compromise: matchup + countdown only, no "first pitch" label.
    texts = {t.text for t in _render(PanelProfile.SINGLE_64X32).texts()}
    assert "FIRST PITCH" not in texts
    assert AWAY.abbreviation in texts and "2h05m" in texts  # but the essentials remain


def test_compromise_is_declared_on_the_card() -> None:
    card = CardFactory().pregame(_game(), now=NOW)
    assert card.layout_support.supports(PanelProfile.SINGLE_64X32)
    assert any("single_64x32" in note for note in card.layout_support.compromise_notes)


def test_renderer_rejects_non_teamgame_contest() -> None:
    from omni.domain.contest import Contest

    card = CardFactory().pregame(_game(), now=NOW)
    bad = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.PREGAME,
        scheduled_start=START,
    )
    import dataclasses

    card = dataclasses.replace(card, contest=bad)
    with pytest.raises(TypeError):
        PregameRenderer().render(
            card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), RecordingCanvas(128, 64)
        )


# --- time-series: one card, a live countdown that advances with the render clock ---


def test_countdown_advances_with_render_clock_without_rebuilding() -> None:
    card = CardFactory().pregame(_game(), now=NOW)  # built ONCE
    renderer = PregameRenderer()

    def countdown_at(now: datetime) -> str:
        canvas = RecordingCanvas(128, 64)
        renderer.render(card, RenderContext(profile=PanelProfile.QUAD_128X64, now=now), canvas)
        # The countdown is the 6x10 value text (the label is 4x6).
        values = [t.text for t in canvas.texts() if t.font == "6x10"]
        return values[-1]

    assert countdown_at(NOW) == "2h05m"
    assert countdown_at(START - timedelta(minutes=45)) == "45m"
    assert countdown_at(START - timedelta(seconds=30)) == "SOON"


# --- team logos -------------------------------------------------------------------


def test_logos_blit_on_quad_and_stack_but_not_single() -> None:
    for profile in (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64):
        keys = {i.key for i in _render(profile, logos=LOGOS).images()}
        assert keys == {AWAY.logo.key, HOME.logo.key}  # both tiles blitted
    # The 64x32 compromise: no room for a tile, so the matchup keeps the colour bar.
    assert _render(PanelProfile.SINGLE_64X32, logos=LOGOS).images() == []


# --- golden snapshots -------------------------------------------------------------


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
    card = CardFactory().pregame(_game(), now=NOW)
    PregameRenderer().render(card, RenderContext(profile=profile, now=NOW, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"pregame_{profile.to_json_value()}.png")
