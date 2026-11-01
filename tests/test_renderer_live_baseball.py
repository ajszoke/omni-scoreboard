"""Tests for the live-baseball renderer: per-profile layouts + golden snapshots.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k golden
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timedelta, timezone
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
from omni.domain.contest import Contest, TeamGame
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BatterGameLine,
    ChallengeState,
    DueUp,
    InningPhase,
    PitcherGameLine,
    PitchSnapshot,
    PitchType,
    TeamLinescore,
    WinProbability,
)
from omni.panels.geometry import geometry_for
from omni.providers.mlb_palette import LOGO_ALT_COLOR, LOGO_BASE_COLOR
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.base import Renderer
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.live_baseball import (
    LiveBaseballRenderer,
    _PITCH_COUNT_Y,
    _PITCH_LINE_Y,
    _PITCH_RIGHT,
    _STRIP_RIGHT,
)
from omni.renderers.pillow_canvas import PillowCanvas
from omni.renderers.text import text_width

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
LOGOS = LogoStore()  # resolves the committed COL/LAD tiles for the logo + golden renders
T = datetime(2026, 6, 17, 19, 5, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi")
ALL_PROFILES = (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32)
REGISTRY = MlbTeamRegistry.from_color_file()  # the real palette, so a render shows what the board would draw
AWAY_COLOR = LOGO_ALT_COLOR[115].value_lifted()  # COL's bar: its base is black, so its purple names the club
HOME_COLOR = LOGO_BASE_COLOR[119].value_lifted()  # LAD's bar: Dodger blue
WHITE = RGBColor(255, 255, 255)
YELLOW = RGBColor(255, 215, 0)  # game-state color: count, outs, bases, stat lines
DIM = RGBColor(60, 60, 60)  # spent / empty markers
THIN = "\N{THIN SPACE}"  # U+2009, the 2px statline separator


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=REGISTRY.resolve(115),  # Rockies
        home=REGISTRY.resolve(119),  # Dodgers
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
    last_pitch: PitchSnapshot | None = PitchSnapshot(velocity_mph=84, pitch_type=PitchType.SWEEPER),
    challenges: ChallengeState | None = None,
    due_up: DueUp | None = None,
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
        last_pitch=last_pitch,
        challenges=challenges,
        due_up=due_up,
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


def _clash_card() -> ScoreboardCard[LiveBaseballCardPayload]:
    # NYY @ DET: both render on an identical cap navy, so the visiting side must switch to its alt.
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
    canvas = _render(make_card(), PanelProfile.QUAD_128X64)  # no logos -> color bars + abbr fallback
    assert canvas.ops[0].op == "fill" and canvas.ops[0].color == RGBColor(0, 0, 0)
    rects = canvas.rects()
    assert any((r.x, r.y, r.w, r.h) == (0, 0, 4, 20) and r.color == AWAY_COLOR for r in rects)  # away bar
    assert any((r.x, r.y, r.w, r.h) == (0, 20, 4, 20) and r.color == HOME_COLOR for r in rects)  # home bar
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert {(3, 5, "COL"), (3, 25, "LAD")} <= texts  # abbr names the team when only the 4px bar drew (flush left)
    # Big R H E in right-aligned columns (9x18B): single digits land at col_right - 9, the runs column
    # starting two clear pixels past the ABS lane.
    assert {(30, 1, "3"), (44, 1, "7"), (58, 1, "0")} <= texts  # away runs / hits / errors
    assert {(30, 21, "5"), (44, 21, "9"), (58, 21, "1")} <= texts  # home
    assert (75, 15, "2-1") in texts  # count (8x13), nudged up and right into the middle gap
    assert (86, 1, "7") in texts  # inning number, right of the batting-half triangle
    # the inning triangle: a 1:1 up-apex shape (visitor bats in the top), its base row 9px wide at y=9
    assert any(o.op == "fill_rect" and o.color == YELLOW and o.y == 9 and o.x == 75 and o.w == 9 for o in canvas.ops)
    # Pitcher/batter rows sit flush to the left edge; the game pitch count lives in the standalone
    # pitch line above, so it is no longer repeated within these stats.
    assert {(0, 43, "P:"), (18, 43, "Kershaw")} <= texts
    stat_op = next(t for t in canvas.texts() if t.y == 46)
    assert stat_op.text == f"6.1IP{THIN}7K"  # IP + K only, drawn whole beside the name
    assert {(0, 54, "3."), (18, 54, "Betts"), (51, 57, f"2-4{THIN}RBI")} <= texts  # batter: name + a fitting statline
    # Pitch stacked in two right-anchored rows below the state module: the game pitch count on top,
    # the current pitch below, both ending on the board's right edge.
    assert (_PITCH_RIGHT - text_width("P:95", "4x6"), _PITCH_COUNT_Y, "P:95") in texts
    pitch_token = f"84mph{THIN}SWPR"
    assert (_PITCH_RIGHT - text_width(pitch_token, "4x6"), _PITCH_LINE_Y, pitch_token) in texts  # ends on the edge
    # 1st base occupied -> a filled yellow diamond at its center (121, 13), a px in from the right edge
    assert any(o.op == "fill_rect" and o.color == YELLOW and o.y == 13 and o.x <= 121 <= o.x + o.w for o in canvas.ops)


@pytest.mark.parametrize(
    "home_runs, rbi, expected",
    [
        (0, 0, "2-4"),  # a quiet day is just the line — no flags
        (0, 1, f"2-4{THIN}RBI"),  # a lone RBI shows the label, not "1 RBI"
        (1, 2, f"2-4{THIN}HR{THIN}2RBI"),  # a lone HR is a flag; the count rides multiples — HR before RBI
        (2, 5, f"2-4{THIN}2HR{THIN}5RBI"),  # multiples carry their count
    ],
)
def test_batter_line_cribs_the_legacy_order_and_singular_convention(home_runs: int, rbi: int, expected: str) -> None:
    batter = BatterGameLine(name="Slugger", at_bats=4, hits=2, home_runs=home_runs, rbi=rbi, order=3)
    assert LiveBaseballRenderer._batter_line(batter) == expected


def test_quad_strip_shows_the_pitch_token_only_with_a_snapshot() -> None:
    card = make_card(last_pitch=PitchSnapshot(velocity_mph=88, pitch_type=PitchType.SINKER))
    shown = _render(card, PanelProfile.QUAD_128X64)
    assert any(t.text == f"88mph{THIN}SNKR" for t in shown.texts())  # speed + mph + the 4-char type
    hidden = _render(make_card(last_pitch=None), PanelProfile.QUAD_128X64)
    assert not any("SNKR" in t.text for t in hidden.texts())  # no snapshot -> no token


def test_quad_overflowing_pitcher_name_claims_the_row_and_drops_stats() -> None:
    # A name wide enough to crowd out its own stats takes the whole line as a marquee lane, so it
    # stays readable and never overruns the strip's right margin — and there is no room for a stat line.
    long_name = "Featherstonhaughington"  # wider than the full strip leaves for a name beside stats
    card = make_card(pitcher=PitcherGameLine(name=long_name, innings_pitched="6.1", pitches=95, strikeouts=7))
    texts = _render(card, PanelProfile.QUAD_128X64).texts()
    name_ops = [t for t in texts if t.y == 43]  # the name claims the pitcher row's own font baseline
    assert (name_ops[0].x, name_ops[0].text) == (0, "P:")  # the leader stays put...
    assert name_ops[1].text in long_name  # ...beside a clipped run of the name (it marquees)
    assert all(t.x + text_width(t.text, "6x10") <= _STRIP_RIGHT for t in name_ops)  # never past the strip margin
    assert not any(t.y == 46 and "IP" in t.text for t in texts)  # the stat line is dropped — no space for it


def test_draw_op_stack_64x64_keeps_full_status() -> None:
    canvas = _render(make_card(), PanelProfile.STACK_64X64)
    rects = canvas.rects()
    assert any((r.x, r.y, r.w, r.h) == (0, 0, 3, 20) and r.color == AWAY_COLOR for r in rects)
    assert any((r.x, r.y, r.w, r.h) == (0, 22, 3, 20) and r.color == HOME_COLOR for r in rects)
    # 1B occupied: the cluster's lower-right diamond, filled — its widest row spans 2r+1 at the center.
    assert any((r.x, r.y, r.w, r.h) == (54, 56, 9, 1) and r.color == YELLOW for r in rects)
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    # The bar fallback names the club in the footprint the missing tile left, clear of every lane.
    assert {(3, 5, "COL"), (3, 27, "LAD")} <= texts
    assert {(43, 1, "3"), (43, 23, "5")} <= texts  # run only, the headline face centered past the ABS lane
    assert not any(t.text in {"7 0", "9 1"} for t in canvas.texts())  # H/E dropped — the stack is run-only
    assert {(3, 49, "7"), (17, 45, "2-1")} <= texts  # inning and count, both stepped up to the score face
    # the inning triangle: a 1:1 up-apex shape (visitor bats in the top), its base row 7px wide at y=47
    assert any((r.x, r.y, r.w, r.h) == (2, 47, 7, 1) and r.color == YELLOW for r in rects)
    # outs as the quad's own 5px squares: two recorded (yellow), the third still to go (a dim outline)
    assert {(16, 56, 5, 5), (23, 56, 5, 5)} <= {(r.x, r.y, r.w, r.h) for r in rects if r.color == YELLOW}
    assert (30, 56, 5, 1) in {(r.x, r.y, r.w, r.h) for r in rects if r.color == DIM}
    # Compromise: the pitcher/batter lines do not fit at 64px wide — omitted on stack.
    assert not {"Kershaw", "Betts"} & {t.text for t in canvas.texts()}


def test_stack_bases_are_the_rotated_cluster_not_upright_squares() -> None:
    # The stack draws the same rotated-diamond infield the quad does, shrunk to r=4: 2nd on top, 3rd
    # and 1st a spread below it. Rotating lets them nest, so the cluster fits where two rows of
    # upright 5x5 base squares used to sit — the only 5x5 squares left in the band are the out row.
    bases_loaded = make_card(bases=BaseballBaseState(first=True, second=True, third=True), phase=InningPhase.BOTTOM)
    canvas = _render(bases_loaded, PanelProfile.STACK_64X64)
    rects = {(r.x, r.y, r.w, r.h) for r in canvas.rects() if r.color == YELLOW}
    assert {(48, 50, 9, 1), (42, 56, 9, 1), (54, 56, 9, 1)} <= rects  # 2nd / 3rd / 1st center rows
    squares = {(r.x, r.y) for r in canvas.rects() if r.w == 5 and r.h == 5}
    assert squares == {(16, 56), (23, 56)}  # only the two recorded outs — no upright base markers


@pytest.mark.parametrize("phase, top_y, base_row_y", [(InningPhase.TOP, 44, 47), (InningPhase.BOTTOM, 59, 59)])
def test_stack_inning_number_holds_still_as_the_arrow_changes_sides(
    phase: InningPhase, top_y: int, base_row_y: int
) -> None:
    canvas = _render(make_card(phase=phase), PanelProfile.STACK_64X64)
    assert (3, 49, "7") in {(t.x, t.y, t.text) for t in canvas.texts()}  # the same cell in either half
    arrow = [r for r in canvas.rects() if r.color == YELLOW and r.x <= 5 < r.x + r.w and r.h == 1 and r.y > 42]
    assert {r.y for r in arrow} == set(range(top_y, top_y + 4))  # above the number, or below it
    assert any((r.x, r.y, r.w) == (2, base_row_y, 7) for r in arrow)  # its wide edge faces the number


@pytest.mark.parametrize(
    "inning, inning_right, outs_x, gutter", [(7, 9, 16, 7), (10, 13, 18, 5)]  # extras take their room from the gutters
)
def test_stack_count_and_outs_float_evenly_between_the_inning_and_the_bases(
    inning: int, inning_right: int, outs_x: int, gutter: int
) -> None:
    canvas = _render(make_card(inning=inning), PanelProfile.STACK_64X64)
    assert (outs_x, 56, 5, 5) in {(r.x, r.y, r.w, r.h) for r in canvas.rects()}
    outs_right = outs_x + 19
    assert outs_x - inning_right == gutter  # the left gutter, out from the inning's right edge
    assert 42 - outs_right == gutter  # the right gutter, in to the cluster's left tip
    number_x = 3 if inning < 10 else 2
    assert (number_x, 49, str(inning)) in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_stack_status_columns_center_on_one_row() -> None:
    # Row 53 is the middle of what the home row leaves: the number's lit rows, the count-over-outs
    # block, and the cluster all balance on it, and the two-sided arrow slots keep the inning there.
    canvas = _render(make_card(bases=BaseballBaseState(first=True, second=True, third=True)), PanelProfile.STACK_64X64)
    assert (3, 49, "7") in {(t.x, t.y, t.text) for t in canvas.texts()}  # lit rows 50..56
    assert (17, 45, "2-1") in {(t.x, t.y, t.text) for t in canvas.texts()}  # the block's top row: 46
    squares = [r for r in canvas.rects() if r.w == 5 and r.h == 5]
    assert {r.y + r.h - 1 for r in squares} == {60}  # the block's bottom row: 60
    cluster = [r for r in canvas.rects() if r.color == YELLOW and r.x >= 42 and r.h == 1]
    assert (min(r.y for r in cluster), max(r.y for r in cluster)) == (46, 60)


def test_draw_op_single_64x32_is_an_explicit_compromise() -> None:
    canvas = _render(make_card(), PanelProfile.SINGLE_64X32)
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    # Essentials shown: abbreviations, scores, inning phase — and now the bases too.
    assert {(12, 5, "COL"), (12, 21, "LAD")} <= texts
    assert {(33, 3, "3"), (33, 19, "5")} <= texts
    assert (54, 0, "7") in texts  # inning number, stepped up to the score face above the bases
    assert {t.font for t in canvas.texts() if t.text in {"COL", "LAD"}} == {"5x7"}
    # Compromise: the H/E detail and the roster strip stay off at 64x32.
    joined = " ".join(t.text for t in canvas.texts())
    assert "7 0" not in joined and "9 1" not in joined  # no hits/errors detail at 64x32
    assert "Kershaw" not in joined and "Betts" not in joined  # no pitcher/batter lines at 64x32
    # What draws: the two team stripes, the inning triangle, the base cluster (1B occupied), and outs.
    rects = {(r.x, r.y, r.w, r.h) for r in canvas.rects()}
    stripes = {(0, 0, 2, 16), (0, 16, 2, 16)}
    triangle = {(47, 3, 1, 1), (46, 4, 3, 1), (45, 5, 5, 1), (44, 6, 7, 1)}  # the 1:1 up-triangle rows
    assert stripes | triangle <= rects
    assert (53, 18, 7, 1) in rects  # 1B filled: the cluster's lower-right diamond at r=3


def test_single_abbreviations_step_down_once_a_score_reaches_two_digits() -> None:
    # In 5x7 a two-digit total would sit a single pixel off the abbreviation, reading as "COL10".
    canvas = _render(make_card(away_score=3, home_score=12), PanelProfile.SINGLE_64X32)
    assert {t.font for t in canvas.texts() if t.text in {"COL", "LAD"}} == {"4x6"}  # both rows, to stay matched
    assert (27, 19, "12") in {(t.x, t.y, t.text) for t in canvas.texts()}
    assert 12 + text_width("LAD", "4x6") + 2 <= 27  # at least 2px of air before the total


def test_single_squeezes_in_the_count_and_outs_under_its_state_column() -> None:
    # Stacking the inning over the bases is what pays for the B-S-O row: the count leads, with the
    # out squares beside it, shrunk to 3px so the trio fits the width.
    canvas = _render(make_card(), PanelProfile.SINGLE_64X32)
    assert (40, 25, "2-1") in {(t.x, t.y, t.text) for t in canvas.texts()}
    rects = {(r.x, r.y, r.w, r.h): r.color for r in canvas.rects()}
    assert rects.get((53, 26, 3, 3)) == YELLOW and rects.get((57, 26, 3, 3)) == YELLOW  # two outs recorded
    assert rects.get((61, 26, 3, 1)) == DIM  # the third still to go — a dim outline, not a fill


@pytest.mark.parametrize("inning, triangle_cx, number_x", [(7, 47, 54), (10, 44, 51)])
def test_single_state_column_centers_on_one_line(inning: int, triangle_cx: int, number_x: int) -> None:
    # Column 51 is midway between the run total's last lit pixel (x=37) and the board's edge. Extras
    # re-center the whole inning group on it, so a two-digit inning still lands on the board.
    card = make_card(inning=inning, bases=BaseballBaseState(first=True, second=True, third=True))
    canvas = _render(card, PanelProfile.SINGLE_64X32)
    assert (number_x, 0, str(inning)) in {(t.x, t.y, t.text) for t in canvas.texts()}
    lit_right = number_x + 6 * len(str(inning)) - 2
    assert lit_right < 64
    assert (triangle_cx - 3 + lit_right) / 2 == 51  # triangle's left corner to the number's last lit pixel
    # 2nd sits dead on the line, and 3rd + 1st a full 5px out either side of it.
    rects = {(r.x, r.y, r.w, r.h) for r in canvas.rects() if r.color == YELLOW}
    assert {(48, 13, 7, 1), (43, 18, 7, 1), (53, 18, 7, 1)} <= rects
    # The count + outs row spans 24 lit pixels, an even width: it can only land a half-pixel off.
    assert (40, 25, "2-1") in {(t.x, t.y, t.text) for t in canvas.texts()}
    assert (53 + 10 + 40) / 2 == 51.5


def test_single_carries_the_meter_and_abs_lane_it_once_dropped() -> None:
    # The 64x32 row is thin, not empty: the color bar is followed by the win-meter stripe and the ABS
    # bulbs, each in the same order the bigger boards use, before the abbreviation starts.
    card = make_card(win_probability=WinProbability(home=74.0, away=26.0), challenges=ChallengeState(away=2, home=1))
    ops = _render(card, PanelProfile.SINGLE_64X32, logos=LOGOS).ops
    meter = [o for o in ops if o.op == "fill_rect" and o.x == 2 and o.w == 2 and o.h == 1]
    assert len(meter) == 32  # both 16px rows striped
    assert len({o.color for o in meter if o.y >= 16}) > 1  # the favored home side brightens up its rows
    bulbs = {(o.y, o.color) for o in ops if o.op == "fill_rect" and o.x == 6 and o.w == 4 and o.h == 1}
    assert {(4, YELLOW), (5, YELLOW), (10, YELLOW), (11, YELLOW)} <= bulbs  # away: both challenges left
    assert {(20, DIM), (21, DIM), (26, YELLOW), (27, YELLOW)} <= bulbs  # home: top spent, bottom still lit


def test_draw_op_bottom_inning_shows_down_triangle() -> None:
    # The InningPhase.BOTTOM arm points the triangle down (home batting); its number sits beside it.
    canvas = _render(make_card(phase=InningPhase.BOTTOM, inning=9), PanelProfile.QUAD_128X64)
    texts = {(t.x, t.y, t.text) for t in canvas.texts()}
    assert (86, 1, "9") in texts  # inning number
    # a down-apex 1:1 triangle: its base row (9px wide) is at the top of the shape, y=5
    assert any(o.op == "fill_rect" and o.color == YELLOW and o.y == 5 and o.x == 75 and o.w == 9 for o in canvas.ops)


def test_draw_op_middle_break_shows_label_and_suppresses_at_bat() -> None:
    # Between halves there is no active at-bat: the larger panels show the phase
    # label alone, not a stale "2-1 / 2 OUT" or a bases diamond.
    for profile in (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64):
        canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7), profile)
        texts = [t.text for t in canvas.texts()]
        assert "MID 7" in texts
        assert "2-1" not in texts and not any("OUT" in t for t in texts)  # at-bat suppressed
        assert not {"Kershaw", "Betts"} & set(texts)  # no live at-bat -> no pitcher/batter
        assert not any(r.color == WHITE and r.w == r.h for r in canvas.rects())  # no bases drawn


def test_draw_op_end_break_shows_end_label() -> None:
    canvas = _render(make_card(phase=InningPhase.END, inning=8), PanelProfile.QUAD_128X64)
    assert "END 8" in {t.text for t in canvas.texts()}


DUE_UP = DueUp(
    batters=(
        BatterGameLine(name="Ohtani", order=1, at_bats=3, hits=2, home_runs=1, rbi=2),
        BatterGameLine(name="Betts", order=2, at_bats=3, hits=1),
        BatterGameLine(name="Freeman", order=3, at_bats=3, hits=0),
    )
)


def test_quad_break_centers_the_inning_under_the_line_score_and_runs_due_up_down_the_right() -> None:
    canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7, due_up=DUE_UP), PanelProfile.QUAD_128X64)
    by_text = {t.text: t for t in canvas.texts()}
    label = by_text["MID 7"]
    assert (label.x, label.y) == ((67 - 40) // 2, 46)  # 8x13, centered across the line score's width
    assert (by_text["Due Up:"].x, by_text["Due Up:"].y, by_text["Due Up:"].font) == (72, 3, "5x7")
    # Lineup spot + name in yellow, with the day's line in white in the gap below.
    expected = (
        ("1.", "Ohtani", 11, f"2-3{THIN}HR{THIN}2RBI"),
        ("2.", "Betts", 28, "1-3"),
        ("3.", "Freeman", 45, "0-3"),
    )
    for spot, name, y, stats in expected:
        assert (by_text[spot].x, by_text[spot].y, by_text[spot].font, by_text[spot].color) == (72, y, "5x8", YELLOW)
        assert (by_text[name].x, by_text[name].y, by_text[name].font, by_text[name].color) == (87, y, "5x8", YELLOW)
        assert (by_text[stats].x, by_text[stats].y, by_text[stats].font, by_text[stats].color) == (
            72,
            y + 8,
            "4x6",
            WHITE,
        )


def test_due_up_lines_up_a_batter_with_no_known_lineup_spot() -> None:
    card = make_card(
        phase=InningPhase.END, inning=8, due_up=DueUp(batters=(BatterGameLine(name="Sub", at_bats=0, hits=0),))
    )
    assert {"#.", "Sub"} <= {t.text for t in _render(card, PanelProfile.QUAD_128X64).texts()}


def test_due_up_draws_only_as_many_names_as_the_feed_gives() -> None:
    card = make_card(phase=InningPhase.END, inning=8, due_up=DueUp(batters=DUE_UP.batters[:1]))
    texts = {t.text for t in _render(card, PanelProfile.QUAD_128X64).texts()}
    assert {"Due Up:", "1.", "Ohtani"} <= texts
    assert "Betts" not in texts


def test_quad_break_without_a_due_up_shows_no_label() -> None:
    canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7), PanelProfile.QUAD_128X64)
    assert "Due Up:" not in {t.text for t in canvas.texts()}


def test_a_long_due_up_name_marquees_inside_the_column() -> None:
    long_name = BatterGameLine(name="Featherstonhaugh", order=9, at_bats=0, hits=0)
    card = make_card(phase=InningPhase.MIDDLE, inning=7, due_up=DueUp(batters=(long_name,)))
    for seconds in range(0, 30, 2):  # across a whole trip, there and back
        width, height = geometry_for(PanelProfile.QUAD_128X64).size
        canvas = RecordingCanvas(width, height)
        context = RenderContext(profile=PanelProfile.QUAD_128X64, now=T + timedelta(seconds=seconds))
        LiveBaseballRenderer().render(card, context, canvas)
        name_ops = [t for t in canvas.texts() if t.y == 11]
        assert (name_ops[0].x, name_ops[0].text) == (72, "9.")  # the batting order number never moves
        assert all(t.x >= 87 and t.x + text_width(t.text, "5x8") <= 128 for t in name_ops[1:])


def test_due_up_never_draws_on_a_live_half() -> None:
    # A stale break value in the payload must not leak onto an at-bat.
    canvas = _render(make_card(phase=InningPhase.TOP, due_up=DUE_UP), PanelProfile.QUAD_128X64)
    assert "Due Up:" not in {t.text for t in canvas.texts()}


@pytest.mark.parametrize("profile", (PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32))
def test_due_up_is_an_asserted_quad_only_compromise(profile: PanelProfile) -> None:
    # Neither small board has the room for three names: each keeps just the inning on a break.
    canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7, due_up=DUE_UP), profile)
    joined = " ".join(t.text for t in canvas.texts())
    assert "Due" not in joined and "Ohtani" not in joined and "2-3" not in joined


def test_draw_op_single_profile_shows_break_label() -> None:
    # The break word replaces the whole game-state block — no batting direction, and no stale bases.
    # It splits into a tight pair around the board's middle, in the run totals' face, on the state
    # column's center line: lit rows 8..14 and 17..23, balanced around the middle of a 32px board.
    canvas = _render(make_card(phase=InningPhase.MIDDLE, inning=7), PanelProfile.SINGLE_64X32)
    assert {(43, 7, "MID"), (49, 16, "7")} <= {(t.x, t.y, t.text) for t in canvas.texts()}
    assert not any(r.color == YELLOW for r in canvas.rects())  # no triangle, no base cluster
    extras = _render(make_card(phase=InningPhase.END, inning=10), PanelProfile.SINGLE_64X32)
    assert {(43, 7, "END"), (46, 16, "10")} <= {(t.x, t.y, t.text) for t in extras.texts()}


def test_draw_op_two_digit_run_steps_down_to_the_tighter_font() -> None:
    # A two-digit value forces both rows down to 6x13B, which carries its own slightly wider columns;
    # "10" right-aligns in the runs column (right edge 42) at x = 42 - 12 — still clear of the ABS lane.
    canvas = _render(make_card(away_score=10), PanelProfile.QUAD_128X64)
    assert (30, 3, "10") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_double_digit_line_score() -> None:
    # Double-digit hits also trip the step-down; "12" right-aligns in the hits column (right edge 56).
    canvas = _render(make_card(away_hits=12, away_errors=3), PanelProfile.QUAD_128X64)
    assert (44, 3, "12") in {(t.x, t.y, t.text) for t in canvas.texts()}


def test_draw_op_empty_bases_draw_dim_diamond_outlines() -> None:
    canvas = _render(make_card(bases=BaseballBaseState()), PanelProfile.QUAD_128X64)  # all bases empty
    dim = RGBColor(60, 60, 60)
    assert any(o.op == "set_pixel" and o.color == dim for o in canvas.ops)  # empty bases -> dim diamond outlines
    # no base is filled: nothing white spans a base-diamond center row (e.g. 1B at y=20)
    assert not any(
        o.op == "fill_rect" and o.color == WHITE and o.y == 20 and o.x <= 108 <= o.x + o.w for o in canvas.ops
    )


def test_logos_replace_the_team_bar_on_quad_and_stack() -> None:
    quad = _render(make_card(), PanelProfile.QUAD_128X64, logos=LOGOS)
    assert {(i.x, i.y, i.key) for i in quad.images()} == {(0, 0, "col"), (0, 20, "lad")}  # tiles flush to the left edge
    bars = {(r.x, r.y, r.w, r.h) for r in quad.rects()}
    assert (0, 0, 4, 20) not in bars and (0, 20, 4, 20) not in bars  # the tile replaces the color bar
    quad_texts = {(t.x, t.y, t.text) for t in quad.texts()}
    assert "COL" not in {t.text for t in quad.texts()} and "LAD" not in {t.text for t in quad.texts()}  # abbr dropped
    assert {(30, 1, "3"), (44, 1, "7"), (58, 1, "0")} <= quad_texts  # big R H E columns, no abbr (logo names it)
    assert {(30, 21, "5"), (44, 21, "9"), (58, 21, "1")} <= quad_texts

    stack = _render(make_card(), PanelProfile.STACK_64X64, logos=LOGOS)
    assert {i.key for i in stack.images()} == {"col", "lad"}
    # The stack dedupes too now: the tile names the club, freeing the lane the meter and ABS bulbs use.
    assert "COL" not in {t.text for t in stack.texts()} and "LAD" not in {t.text for t in stack.texts()}


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


def test_clash_blits_the_away_alt_tile() -> None:
    # The renderer routes both marks through the clash resolver: DET at home keeps its primary,
    # the visiting NYY shows its alt so the two navy caps don't merge on the panel.
    canvas = _render(_clash_card(), PanelProfile.QUAD_128X64, logos=LOGOS)
    assert {i.key for i in canvas.images()} == {"nyy-alt", "det"}


def test_win_meter_stripes_fill_the_favored_side_and_stay_faint_on_the_other() -> None:
    # LAD lead 5-3, so LAD is the favored side: the home stripe fills, the away one holds the faint floor.
    wp = WinProbability(home=74.0, away=26.0)
    # Every profile carries the meter now, each hugging its own mark: 20px rows on quad and stack,
    # 16px on the single, where the stripe follows the 2px color bar.
    for profile, x, mark_h in (
        (PanelProfile.QUAD_128X64, 20, 20),
        (PanelProfile.STACK_64X64, 21, 20),
        (PanelProfile.SINGLE_64X32, 2, 16),
    ):
        rows = [
            r
            for r in _render(make_card(win_probability=wp), profile, logos=LOGOS).rects()
            if r.w == 2 and r.x == x and r.h == 1
        ]
        assert len(rows) == 2 * mark_h, profile  # both marks striped: faint floor, brighter where won
        away = {r.color for r in rows if r.y < mark_h}  # the trailing (away) side holds one faint level
        home = {r.color for r in rows if r.y >= mark_h}  # the favored (home) side brightens -> several levels
        assert len(home) > 1 and len(away) == 1, profile


@pytest.mark.parametrize("profile", (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64))
def test_win_meter_golden(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = make_card(win_probability=WinProbability(home=74.0, away=26.0))
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
def test_abs_golden_image_per_profile(profile: PanelProfile) -> None:
    # The fully-loaded live card on each board: win meter + ABS challenge bulbs (2 left for the
    # visitor, 1 for the home side). This is the density snapshot the small profiles are judged on.
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = make_card(win_probability=WinProbability(home=74.0, away=26.0), challenges=ChallengeState(away=2, home=1))
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_abs_{profile.to_json_value()}.png")


def test_quad_challenge_dots_fill_by_remaining() -> None:
    # ABS dots in the x=24 lane between the meter and the line score: away 2 left (both yellow), home
    # 1 left. The pool burns from the top, so the home side's spent dot is the upper one and its
    # remaining challenge stays lit at the bottom. Each circle's two full-width rows identify it.
    ops = _render(make_card(challenges=ChallengeState(away=2, home=1)), PanelProfile.QUAD_128X64).ops
    dots = {(o.y, o.color) for o in ops if o.op == "fill_rect" and o.x == 24 and o.w == 4 and o.h == 1}
    assert {(6, YELLOW), (7, YELLOW), (12, YELLOW), (13, YELLOW)} <= dots  # away: both challenges left
    assert {(26, DIM), (27, DIM), (32, YELLOW), (33, YELLOW)} <= dots  # home: top spent, bottom left


def test_quad_challenge_dots_are_circles_not_squares() -> None:
    # The lane's dots round off: a circle's first and last rows inset a pixel each side, so the box's
    # corners stay dark and the dot reads round rather than square.
    ops = _render(make_card(challenges=ChallengeState(away=2, home=2)), PanelProfile.QUAD_128X64).ops
    rows = {(o.x, o.y, o.w) for o in ops if o.op == "fill_rect" and o.h == 1 and 24 <= o.x <= 25 and o.y < 20}
    assert {(25, 5, 2), (24, 6, 4), (24, 7, 4), (25, 8, 2)} <= rows  # the away side's top circle


@pytest.mark.parametrize("profile", (PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32))
def test_extras_golden_image_small_profiles(profile: PanelProfile) -> None:
    # A two-digit inning in the bottom half: the widest the small boards' inning ever gets, with the
    # stack's arrow on its lower side.
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = make_card(
        phase=InningPhase.BOTTOM,
        inning=11,
        win_probability=WinProbability(home=74.0, away=26.0),
        challenges=ChallengeState(away=2, home=1),
    )
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_extras_{profile.to_json_value()}.png")


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_break_golden_image_per_profile(profile: PanelProfile) -> None:
    # The between-halves (MIDDLE) layout: matchup + scores + phase label, no at-bat, and Due Up on
    # the one board with room for it. Mid-7th, so the home Dodgers are the ones due up.
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = make_card(phase=InningPhase.MIDDLE, inning=7, due_up=DUE_UP)
    LiveBaseballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_baseball_break_{profile.to_json_value()}.png")
