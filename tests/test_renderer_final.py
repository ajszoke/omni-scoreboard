"""Tests for the final card: winner-derived treatment, finite rotation, goldens.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k final
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from omni.cards.baseball import FinalCardPayload
from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.colors import RGBColor
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase, PitchingDecisions
from omni.domain.contest import Contest, TeamGame
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.final import FinalRenderer
from omni.renderers.image import LogoStore
from omni.renderers.pillow_canvas import PillowCanvas

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
LOGOS = LogoStore()  # resolves the committed COL/LAD tiles for the logo + golden renders
ALL_PROFILES = list(PanelProfile)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
_REG = MlbTeamRegistry.from_color_file()
AWAY = _REG.resolve(115)
HOME = _REG.resolve(119)
WHITE = RGBColor(255, 255, 255)  # winner
LOSER = RGBColor(110, 110, 110)  # loser (dimmed)
# Home (LAD) wins the sample, so its pitcher takes the win and the away (COL) pitcher the loss.
DECISIONS = PitchingDecisions(winner="Clayton Kershaw", loser="German Marquez", save="Tanner Scott")


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.FINAL,
        scheduled_start=NOW,
        away=AWAY,
        home=HOME,
    )


def _card(
    away: int = 3, home: int = 5, *, decisions: PitchingDecisions | None = DECISIONS
) -> ScoreboardCard[FinalCardPayload]:
    state = BaseballGameState(
        away_score=away,
        home_score=home,
        inning=9,
        phase=InningPhase.BOTTOM,
        count=BaseballCount(balls=0, strikes=0, outs=3),
        bases=BaseballBaseState(),
    )
    return CardFactory().final(_game(), state, decisions=decisions, now=NOW)


def _render(
    card: ScoreboardCard[FinalCardPayload], profile: PanelProfile, *, logos: LogoStore | None = None
) -> RecordingCanvas:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    FinalRenderer().render(card, RenderContext(profile=profile, now=NOW, logos=logos), canvas)
    return canvas


def _colors_by_text(canvas: RecordingCanvas) -> dict[str, RGBColor | None]:
    return {t.text: t.color for t in canvas.texts()}


# --- layout + winner treatment ----------------------------------------------------


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_matchup_scores_and_final_marker_on_every_profile(profile: PanelProfile) -> None:
    texts = {t.text for t in _render(_card(away=3, home=5), profile).texts()}
    assert AWAY.abbreviation in texts and HOME.abbreviation in texts
    assert "3" in texts and "5" in texts
    assert "FINAL" in texts or "FIN" in texts  # the status marker (full or compromised)


def test_home_winner_is_bright_and_away_loser_is_dimmed() -> None:
    colors = _colors_by_text(_render(_card(away=3, home=5), PanelProfile.QUAD_128X64))  # home wins
    assert colors[HOME.abbreviation] == WHITE and colors["5"] == WHITE  # winner bright
    assert colors[AWAY.abbreviation] == LOSER and colors["3"] == LOSER  # loser dimmed


def test_away_winner_treatment() -> None:
    colors = _colors_by_text(_render(_card(away=7, home=2), PanelProfile.QUAD_128X64))  # away wins
    assert colors[AWAY.abbreviation] == WHITE and colors[HOME.abbreviation] == LOSER


def test_tie_leaves_both_sides_bright() -> None:
    colors = _colors_by_text(_render(_card(away=4, home=4, decisions=None), PanelProfile.QUAD_128X64))
    assert colors[AWAY.abbreviation] == WHITE and colors[HOME.abbreviation] == WHITE  # no loser dim


def test_single_profile_compromise_shortens_to_fin() -> None:
    texts = {t.text for t in _render(_card(), PanelProfile.SINGLE_64X32).texts()}
    assert "FIN" in texts and "FINAL" not in texts


def test_compromise_is_declared_on_the_card() -> None:
    notes = _card().layout_support.compromise_notes
    assert any("single_64x32" in note for note in notes)
    assert any("stack_64x64" in note for note in notes)  # the dropped save line is declared too


# --- W/L/S pitching line ----------------------------------------------------------


def test_quad_shows_the_full_wls_line() -> None:
    texts = {t.text for t in _render(_card(), PanelProfile.QUAD_128X64).texts()}
    assert {"W Kershaw", "L Marquez", "S Scott"} <= texts  # last names only, with W/L/S labels


def test_wls_line_marks_the_winner_bright_and_the_loser_dim() -> None:
    colors = _colors_by_text(_render(_card(), PanelProfile.QUAD_128X64))
    assert colors["W Kershaw"] == WHITE and colors["L Marquez"] == LOSER  # mirrors the score treatment


def test_stack_shows_w_and_l_but_drops_the_save() -> None:
    texts = {t.text for t in _render(_card(), PanelProfile.STACK_64X64).texts()}
    assert "W Kershaw" in texts and "L Marquez" in texts
    assert not any(t.startswith("S ") for t in texts)  # the save line has no room on the 64x64


def test_single_drops_the_whole_pitching_line() -> None:
    texts = {t.text for t in _render(_card(), PanelProfile.SINGLE_64X32).texts()}
    assert not any(t.startswith(("W ", "L ", "S ")) for t in texts)  # no room at 64px wide


def test_a_save_less_game_shows_only_w_and_l() -> None:
    no_save = PitchingDecisions(winner="Tarik Skubal", loser="Kevin Gausman")
    texts = {t.text for t in _render(_card(decisions=no_save), PanelProfile.QUAD_128X64).texts()}
    assert "W Skubal" in texts and "L Gausman" in texts
    assert not any(t.startswith("S ") for t in texts)


@pytest.mark.parametrize("profile", [PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64])
def test_no_decisions_renders_no_pitching_line(profile: PanelProfile) -> None:
    texts = {t.text for t in _render(_card(decisions=None), profile).texts()}
    assert not any(t.startswith(("W ", "L ", "S ")) for t in texts)  # a tie / undecided feed


def test_renderer_rejects_non_teamgame_contest() -> None:
    import dataclasses

    bad = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.FINAL,
        scheduled_start=NOW,
    )
    card = dataclasses.replace(_card(), contest=bad)
    with pytest.raises(TypeError):
        FinalRenderer().render(card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), RecordingCanvas(128, 64))


# --- finite postgame rotation -----------------------------------------------------


def test_final_card_has_a_finite_postgame_window() -> None:
    card = _card()
    assert card.kind is CardKind.FINAL
    assert card.timing.is_available(NOW)  # shows immediately
    expires = card.timing.expires_at
    assert expires is not None and not card.timing.is_available(expires)  # then rotates out


# --- team logos -------------------------------------------------------------------


def test_logos_blit_full_colour_on_quad_and_stack_regardless_of_winner() -> None:
    # The loser's label/score dim, but its logo tile stays full-colour (the tile carries no dim).
    for profile in (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64):
        keys = {i.key for i in _render(_card(away=3, home=5), profile, logos=LOGOS).images()}
        assert keys == {AWAY.logo.key, HOME.logo.key}


def test_single_profile_drops_the_logo_even_with_a_store() -> None:
    assert _render(_card(), PanelProfile.SINGLE_64X32, logos=LOGOS).images() == []


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
    FinalRenderer().render(_card(away=3, home=5), RenderContext(profile=profile, now=NOW, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"final_{profile.to_json_value()}.png")
