"""Tests for the live-baseball renderer: per-profile layouts + golden snapshots.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k golden
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from omni.cards.base import (
    CardId,
    CardKind,
    CardPriority,
    DedupeKey,
    DisplayTiming,
    LayoutSupport,
    ScoreboardCard,
)
from omni.cards.baseball import LiveBaseballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.base import LogoAsset
from omni.domain.contest import Contest, TeamGame
from omni.domain.teams import Team
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BatterGameLine,
    InningPhase,
    PitcherGameLine,
    TeamLinescore,
    WinProbability,
)
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.base import Renderer
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.live_baseball import LiveBaseballRenderer
from omni.renderers.pillow_canvas import PillowCanvas

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
LOGOS = LogoStore()  # resolves the committed COL/LAD tiles for the logo + golden renders
T = datetime(2026, 6, 17, 19, 5, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi")
ALL_PROFILES = (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32)
AWAY_COLOR = RGBColor(51, 0, 111)
HOME_COLOR = RGBColor(0, 90, 156)
WHITE = RGBColor(255, 255, 255)


def _team(team_id: str, name: str, abbr: str, color: RGBColor) -> Team:
    return Team(
        id=LeagueScopedId(League.MLB, SOURCE, team_id),
        league=League.MLB,
        display_name=name,
        short_name=name.split()[-1],
        abbreviation=abbr,
        primary_color=color,
        secondary_color=RGBColor(196, 206, 212),
        logo=LogoAsset(key=abbr.lower(), path=f"assets/logos/mlb/{abbr.lower()}.png"),
    )


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_team("115", "Colorado Rockies", "COL", AWAY_COLOR),
        home=_team("119", "Los Angeles Dodgers", "LAD", HOME_COLOR),
    )


def make_card(
    *,
    phase: InningPhase = InningPhase.TOP,
    away_score: int = 3,
    home_score: int = 5,
    away_hits: int = 7,
    home_hits: int = 9,
    away_errors: int = 0,
    home_errors: int = 1,
    inning: int = 7,
    bases: BaseballBaseState = BaseballBaseState(first=True),
    win_probability: WinProbability | None = None,
    batter: BatterGameLine | None = BatterGameLine(name="Betts", at_bats=4, hits=2, rbi=1, order=3),
    pitcher: PitcherGameLine | None = PitcherGameLine(name="Kershaw", innings_pitched="6.1", pitches=95, strikeouts=7),
) -> ScoreboardCard[LiveBaseballCardPayload]:
    payload = LiveBaseballCardPayload(
        away_line=TeamLinescore(runs=away_score, hits=away_hits, errors=away_errors),
        home_line=TeamLinescore(runs=home_score, hits=home_hits, errors=home_errors),
        inning=inning,
        phase=phase,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=bases,
        win_probability=win_probability,
        batter=batter,
        pitcher=pitcher,
    )
    return ScoreboardCard(
        id=CardId("g1:live"),
        kind=CardKind.LIVE_GAME,
        contest=_game(),
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
        layout_support=LayoutSupport(profiles=frozenset(ALL_PROFILES)),
        dedupe_key=DedupeKey("g1:live"),
        payload=payload,
    )


REGISTRY = MlbTeamRegistry.from_color_file()  # real teams carry the base/alt backgrounds the clash reads


def _clash_card() -> ScoreboardCard[LiveBaseballCardPayload]:
    # NYY @ DET: both render on an identical cap navy, so the home side must flip to its alt.
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g2"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=REGISTRY.resolve(147),  # Yankees
        home=REGISTRY.resolve(116),  # Tigers
    )
    return dataclasses.replace(make_card(away_score=4, home_score=2, inning=6), contest=game)


def _render(
    card: ScoreboardCard[LiveBaseballCardPayload], profile: PanelProfile, *, logos: LogoStore | None = None
) -> RecordingCanvas:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=logos), canvas)
    return canvas


def test_renderer_supports_all_three_profiles() -> None:
    renderer: Renderer[LiveBaseballCardPayload] = LiveBaseballRenderer()
    assert renderer.supported_profiles == frozenset(ALL_PROFILES)


def test_renderer_rejects_non_teamgame_contest() -> None:
    contest = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
    )
    card = dataclasses.replace(make_card(), contest=contest)
    with pytest.raises(TypeError):
        LiveBaseballRenderer().render(
            card, RenderContext(profile=PanelProfile.QUAD_128X64, now=T), RecordingCanvas(128, 64)
        )


def test_draw_op_quad_128x64() -> None:
    canvas = _render(make_card(), PanelProfile.QUAD_128X64)  # no logos -> colour bars + abbr fallback
    assert canvas.ops[0].op == "fill" and canvas.ops[0].color == RGBColor(0, 0, 0)
    rects = canvas.rects()
    assert any((r.x, r.y, r.w, r.h) == (0, 0, 4, 20) and r.color == AWAY_COLOR for r in rects)  # away bar
    assert any((r.x, r.y, r.w, r.h) == (0, 20, 4, 20) and r.color == HOME_COLOR for r in rects)  # home bar
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(8, 5, "COL"), (8, 25, "LAD")} <= texts  # abbr only as the colour-bar fallback
    assert {(30, 5, "3 7 0"), (30, 25, "5 9 1")} <= texts  # inline R H E (three equal numbers)
    assert {(64, 2, "▲7"), (64, 28, "2-1")} <= texts  # inning (filled triangle) + count, big font
    assert {(2, 41, "P: Kershaw"), (65, 44, "6.1IP 7K 95P")} <= texts  # strip: big name + smaller statline
    assert {(2, 52, "3. Betts"), (53, 55, "2-4")} <= texts  # batter strip line
    # 1st base is occupied -> a filled white diamond spanning its centre (108, 20)
    assert any(o.op == "fill_rect" and o.color == WHITE and o.y == 20 and o.x <= 108 <= o.x + o.w for o in canvas.ops)


def test_draw_op_stack_64x64_keeps_full_status() -> None:
    canvas = _render(make_card(), PanelProfile.STACK_64X64)
    rects = canvas.rects()
    assert any((r.x, r.y, r.w, r.h) == (0, 0, 3, 20) and r.color == AWAY_COLOR for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (0, 22, 3, 20) and r.color == HOME_COLOR for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (55, 52, 5, 5) and r.color == WHITE for r in rects)  # 1B filled
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(5, 6, "COL"), (5, 28, "LAD")} <= texts
    assert {(56, 6, "3"), (56, 28, "5")} <= texts  # right-aligned at 62 - 6
    assert {(50, 16, "7 0"), (50, 38, "9 1")} <= texts  # hits/errors as bare numbers beneath each run score
    assert {(3, 46, "↑7"), (20, 46, "2-1"), (3, 55, "2 OUT")} <= texts  # full status retained
    # Compromise: the pitcher/batter lines do not fit at 64px wide — omitted on stack.
    assert not any(t.text.startswith(("P: Kershaw", "3. Betts")) for t in canvas.texts())


def test_draw_op_single_64x32_is_an_explicit_compromise() -> None:
    canvas = _render(make_card(), PanelProfile.SINGLE_64X32)
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    # Essentials shown: abbreviations, scores, inning phase.
    assert {(4, 5, "COL"), (4, 21, "LAD")} <= texts
    assert {(36, 3, "3"), (36, 19, "5")} <= texts
    assert (46, 13, "↑7") in texts
    # Compromise: count, outs, the bases diamond, and the H/E detail are omitted at 64x32.
    joined = " ".join(t.text for t in canvas.texts())
    assert "OUT" not in joined and "-" not in joined
    assert "7 0" not in joined and "9 1" not in joined  # no hits/errors detail at 64x32
    assert "Kershaw" not in joined and "Betts" not in joined  # no pitcher/batter lines at 64x32
    # Only the two team stripes are drawn (no base markers).
    assert {(r.x, r.y, r.w, r.h) for r in canvas.rects()} == {(0, 0, 2, 16), (0, 16, 2, 16)}


def test_draw_op_bottom_inning_shows_down_triangle() -> None:
    # The InningPhase.BOTTOM arm picks the down glyph; assert the quad's filled triangle.
    canvas = _render(make_card(phase=InningPhase.BOTTOM, inning=9), PanelProfile.QUAD_128X64)
    assert (64, 2, "▼9") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_middle_break_shows_label_and_suppresses_at_bat() -> None:
    # Between halves there is no active at-bat: the larger panels show the phase
    # label alone, not a stale "2-1 / 2 OUT" or a bases diamond.
    for profile in (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64):
        canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7), profile)
        texts = [t.text for t in canvas.texts()]
        assert "MID7" in texts
        assert "2-1" not in texts and not any("OUT" in t for t in texts)  # at-bat suppressed
        assert not any(t.startswith(("P: Kershaw", "3. Betts")) for t in texts)  # no live at-bat -> no pitcher/batter
        assert not any(r.color == WHITE and r.w == r.h for r in canvas.rects())  # no bases drawn


def test_draw_op_end_break_shows_end_label() -> None:
    canvas = _render(make_card(phase=InningPhase.END, inning=8), PanelProfile.QUAD_128X64)
    assert "END8" in {t.text for t in canvas.texts()}


def test_draw_op_single_profile_shows_break_label() -> None:
    # The 64x32 compromise never showed an at-bat anyway — it just swaps the label.
    canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7), PanelProfile.SINGLE_64X32)
    assert (46, 13, "MID7") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_two_digit_run_in_the_line_score() -> None:
    # The run is the first of the three inline R/H/E numbers (after the abbr at x=30 without a logo).
    canvas = _render(make_card(away_score=10), PanelProfile.QUAD_128X64)
    assert (30, 5, "10 7 0") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_double_digit_line_score() -> None:
    # Double-digit hits widen the inline triplet without wrapping or clipping.
    canvas = _render(make_card(away_hits=12, away_errors=3), PanelProfile.QUAD_128X64)
    assert (30, 5, "3 12 3") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_empty_bases_draw_dim_diamond_outlines() -> None:
    canvas = _render(make_card(bases=BaseballBaseState()), PanelProfile.QUAD_128X64)  # all bases empty
    dim = RGBColor(60, 60, 60)
    assert any(o.op == "set_pixel" and o.color == dim for o in canvas.ops)  # empty bases -> dim diamond outlines
    # no base is filled: nothing white spans a base-diamond centre row (e.g. 1B at y=20)
    assert not any(
        o.op == "fill_rect" and o.color == WHITE and o.y == 20 and o.x <= 108 <= o.x + o.w for o in canvas.ops
    )


def test_logos_replace_the_team_bar_on_quad_and_stack() -> None:
    quad = _render(make_card(), PanelProfile.QUAD_128X64, logos=LOGOS)
    assert {i.key for i in quad.images()} == {"col", "lad"}  # both tiles blitted at (2,0)/(2,20)
    bars = {(r.x, r.y, r.w, r.h) for r in quad.rects()}
    assert (0, 0, 4, 20) not in bars and (0, 20, 4, 20) not in bars  # the tile replaces the colour bar
    quad_texts = {(t.x, t.y, t.text) for t in quad.texts()}
    assert "COL" not in {t.text for t in quad.texts()} and "LAD" not in {t.text for t in quad.texts()}  # abbr dropped
    assert {(26, 5, "3 7 0"), (26, 25, "5 9 1")} <= quad_texts  # R H E sits in the freed space, no abbr

    stack = _render(make_card(), PanelProfile.STACK_64X64, logos=LOGOS)
    assert {i.key for i in stack.images()} == {"col", "lad"}
    assert {(23, 6, "COL"), (23, 28, "LAD")} <= {(t.x, t.y, t.text) for t in stack.texts()}  # stack still shows abbr


def test_single_profile_drops_the_logo_even_with_a_store() -> None:
    # An explicit compromise: a 20px tile does not fit a 64x32 row, so the bar identifies the team.
    canvas = _render(make_card(), PanelProfile.SINGLE_64X32, logos=LOGOS)
    assert canvas.images() == []
    assert {(0, 0, 2, 16), (0, 16, 2, 16)} <= {(r.x, r.y, r.w, r.h) for r in canvas.rects()}


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
    LiveBaseballRenderer().render(make_card(), RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_{profile.to_json_value()}.png")


def test_clash_blits_the_home_alt_tile() -> None:
    # The renderer routes both marks through the clash resolver: NYY keeps its base tile,
    # DET flips to its alt so the two navy caps don't merge on the panel.
    canvas = _render(_clash_card(), PanelProfile.QUAD_128X64, logos=LOGOS)
    assert {i.key for i in canvas.images()} == {"nyy", "det-alt"}


def test_win_meter_blits_gauges_on_quad_and_stack() -> None:
    wp = WinProbability(home=26.0, away=74.0)
    for profile, home_top in ((PanelProfile.QUAD_128X64, 32), (PanelProfile.STACK_64X64, 22)):
        canvas = _render(make_card(win_probability=wp), profile, logos=LOGOS)
        gauges = [r for r in canvas.rects() if r.w == 2 and r.x in (21, 22)]  # the two meter slivers
        assert len(gauges) == 2, profile
        away, home = sorted(gauges, key=lambda r: r.y)
        assert away.h == 15 and home.h == 5  # round(74%/26% * 20px), filled from the bottom


@pytest.mark.parametrize("profile", (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64))
def test_win_meter_golden(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = make_card(win_probability=WinProbability(home=26.0, away=74.0))
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_win_meter_{profile.to_json_value()}.png")


def test_clash_golden_image_quad() -> None:
    width, height = geometry_for(PanelProfile.QUAD_128X64).size
    canvas = PillowCanvas(width, height)
    LiveBaseballRenderer().render(
        _clash_card(), RenderContext(profile=PanelProfile.QUAD_128X64, now=T, logos=LOGOS), canvas
    )
    _assert_matches_golden(canvas.image(), "live_baseball_clash_quad_128x64.png")


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_break_golden_image_per_profile(profile: PanelProfile) -> None:
    # The between-halves (MIDDLE) layout: matchup + scores + phase label, no at-bat.
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = make_card(phase=InningPhase.MIDDLE, inning=7)
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_break_{profile.to_json_value()}.png")
