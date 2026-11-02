"""Tests for the pregame renderer: countdown formatting, per-profile layouts,
the small-panel compromise, golden snapshots, and a time-series countdown.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k pregame
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from omni.cards.baseball import PregameCardPayload
from omni.cards.factory import CardFactory
from omni.core.colors import RGBColor
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BettingLine, PregameSide, StartingPitcher, WinLossRecord
from omni.domain.contest import TeamGame
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.pregame import PregameRenderer, first_pitch_label, start_time_label
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.pillow_canvas import PillowCanvas
from omni.renderers.text import text_width

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
LOGOS = LogoStore()  # resolves the committed COL/LAD tiles for the logo + golden renders
ALL_PROFILES = list(PanelProfile)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()
START = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 6, 17, 21, 25, tzinfo=timezone.utc)  # 2h05m before first pitch
AWAY = _REG.resolve(115)
HOME = _REG.resolve(119)
WHITE = RGBColor(255, 255, 255)
YELLOW = RGBColor(255, 215, 0)
ODDS_GREEN = RGBColor(50, 180, 50)
STATS_TEAL = RGBColor(50, 180, 180)


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


# --- the quad's detailed layout -----------------------------------------------------------

MOUNTAIN = ZoneInfo("America/Denver")
FULL_AWAY = PregameSide(
    record=WinLossRecord(wins=38, losses=44),
    starter=StartingPitcher(name="Marquez", record=WinLossRecord(wins=5, losses=9), era="4.68"),
    line=BettingLine(moneyline=165, runline=1.5, runline_price=-130),
)
FULL_HOME = PregameSide(
    record=WinLossRecord(wins=51, losses=31),
    starter=StartingPitcher(name="Kershaw", record=WinLossRecord(wins=12, losses=3), era="2.87"),
    line=BettingLine(moneyline=-195, runline=-1.5, runline_price=110),
)


def _detailed(*, away: PregameSide | None = FULL_AWAY, home: PregameSide | None = FULL_HOME) -> RecordingCanvas:
    card = CardFactory().pregame(_game(), now=NOW, away=away, home=home)
    canvas = RecordingCanvas(128, 64)
    context = RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW, logos=LOGOS, timezone=MOUNTAIN)
    PregameRenderer().render(card, context, canvas)
    return canvas


def test_quad_details_put_record_and_odds_over_starter_and_stats_beside_each_tile() -> None:
    by_text = {t.text: t for t in _detailed().texts()}
    assert (by_text["38-44"].x, by_text["38-44"].y, by_text["38-44"].color) == (22, 0, WHITE)
    assert (by_text["51-31"].x, by_text["51-31"].y, by_text["51-31"].color) == (22, 20, WHITE)
    assert by_text["+165 (+1.5 -130)"].color == ODDS_GREEN and by_text["+165 (+1.5 -130)"].y == 3
    assert by_text["-195 (-1.5 +110)"].color == ODDS_GREEN and by_text["-195 (-1.5 +110)"].y == 23
    assert (by_text["Marquez"].x, by_text["Marquez"].y, by_text["Marquez"].color) == (22, 10, YELLOW)
    assert (by_text["Kershaw"].x, by_text["Kershaw"].y, by_text["Kershaw"].color) == (22, 30, YELLOW)
    assert by_text["5-9 (4.68 ERA)"].color == STATS_TEAL and by_text["12-3 (2.87 ERA)"].y == 33


def test_quad_details_show_the_start_time_in_the_render_clocks_zone_with_the_countdown() -> None:
    texts = {t.text for t in _detailed().texts()}
    assert START.astimezone(MOUNTAIN).strftime("%-I:%M %p") in texts
    assert "FIRST PITCH 2h05m" in texts


def test_quad_details_put_tiles_flush_and_clash_resolved() -> None:
    assert {(i.x, i.y, i.key) for i in _detailed().images()} == {(0, 0, "col"), (0, 20, "lad")}


def test_quad_details_fall_back_gracefully_when_a_side_or_a_part_is_unknown() -> None:
    texts = {t.text for t in _detailed(away=None).texts()}
    assert "TBD" in texts and "51-31" in texts  # the known side still shows in full
    bare = {t.text for t in _detailed(home=PregameSide()).texts()}
    assert "TBD" in bare and not any("ERA" in t for t in bare if t.startswith("12-3"))


def test_quad_details_name_the_club_when_only_a_bar_drew() -> None:
    card = CardFactory().pregame(_game(), now=NOW, away=FULL_AWAY, home=FULL_HOME)
    canvas = RecordingCanvas(128, 64)
    PregameRenderer().render(card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)
    assert {(3, 5, "COL"), (3, 25, "LAD")} <= {(t.x, t.y, t.text) for t in canvas.texts()}


def test_a_long_starter_name_marquees_rather_than_running_off_the_board() -> None:
    long = PregameSide(starter=StartingPitcher(name="Featherstonhaughingtonshire", record=FULL_AWAY.starter.record, era="1.00"))  # type: ignore[union-attr]
    ops = _detailed(away=long).texts()
    assert not any("Featherstonhaughingtonshire" == t.text for t in ops)
    assert all(t.x + text_width(t.text, t.font) <= 128 for t in ops)


def test_a_side_without_details_keeps_the_plain_layout_on_every_board() -> None:
    assert "FIRST PITCH" in {t.text for t in _render(PanelProfile.QUAD_128X64).texts()}


@pytest.mark.parametrize("profile", (PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32))
def test_small_boards_drop_the_details_as_a_declared_compromise(profile: PanelProfile) -> None:
    card = CardFactory().pregame(_game(), now=NOW, away=FULL_AWAY, home=FULL_HOME)
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    PregameRenderer().render(card, RenderContext(profile=profile, now=NOW), canvas)
    joined = " ".join(t.text for t in canvas.texts())
    assert "Kershaw" not in joined and "51-31" not in joined and "+165" not in joined
    assert sum("odds" in note for note in card.layout_support.compromise_notes) == 2


@pytest.mark.parametrize(
    "hour, minute, expected",
    [(0, 5, "12:05 AM"), (9, 30, "9:30 AM"), (12, 0, "12:00 PM"), (19, 10, "7:10 PM")],
)
def test_start_time_label_reads_like_a_board_clock(hour: int, minute: int, expected: str) -> None:
    start = datetime(2026, 6, 17, hour, minute, tzinfo=timezone.utc)
    assert start_time_label(start, timezone.utc) == expected


def test_quad_details_show_only_the_parts_of_a_line_or_a_starter_that_are_known() -> None:
    sparse = PregameSide(
        line=BettingLine(moneyline=165),
        starter=StartingPitcher(name="Marquez", era="4.68"),
    )
    texts = {t.text for t in _detailed(away=sparse, home=PregameSide(starter=StartingPitcher(name="Kershaw"))).texts()}
    assert "+165" in texts and "(4.68 ERA)" in texts
    record_only = PregameSide(starter=StartingPitcher(name="Marquez", record=WinLossRecord(wins=5, losses=9)))
    assert "5-9" in {t.text for t in _detailed(away=record_only).texts()}


def test_the_render_clocks_zone_stands_in_when_no_display_zone_is_set() -> None:
    card = CardFactory().pregame(_game(), now=NOW, away=FULL_AWAY, home=FULL_HOME)
    canvas = RecordingCanvas(128, 64)
    PregameRenderer().render(card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)
    assert START.strftime("%-I:%M %p") in {t.text for t in canvas.texts()}


# --- golden snapshots -------------------------------------------------------------


def test_full_golden_image_quad() -> None:
    # The whole pregame on the big board: records, odds, starters, start time.
    card = CardFactory().pregame(_game(), now=NOW, away=FULL_AWAY, home=FULL_HOME)
    canvas = PillowCanvas(128, 64)
    context = RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW, logos=LOGOS, timezone=MOUNTAIN)
    PregameRenderer().render(card, context, canvas)
    _assert_matches_golden(canvas.image(), "pregame_full_quad_128x64.png")


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
