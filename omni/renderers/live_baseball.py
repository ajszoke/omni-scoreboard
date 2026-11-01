"""Renderer for the live-baseball card across all three panel profiles.

Each profile gets its OWN layout, never a crop of another (AGENTS.md forbids
"cropping 128x64 cards down to 64x32"):

- quad_128x64 : a 40px top block — two team rows (logo tile + an inline ``R H E`` line score;
  the abbreviation is dropped when a tile resolves, since the logo names the team) beside a
  compact 3-row state module (inning + 2nd base / 1st + 3rd base / count + out-dots), with a
  standalone pitch line (the game pitch count + the live pitch) tucked in the band below them —
  over a 24px full-width pitcher/batter strip. On a break the inning moves under the line score and
  Due Up takes the right of the board, which only this profile has the room for.
- stack_64x64 : a compressed layout — two team rows (logo tile or bar, the win meter, the ABS
  lane, and the run score; the abbreviation is dropped when a tile resolves) over a status band
  of three columns centered on one row: the inning with its half-arrow above or below the number,
  the count over the outs, and the base cluster.
- single_64x32: an explicit COMPROMISE, but not a bare one — each 16px row carries the color
  bar, the win-meter stripe, the ABS lane, the abbreviation and the run total, with a game-state
  column on the right stacking inning, base cluster, and count + outs, all centered on one line.
  The H/E detail and the pitcher/batter strip are omitted (not legible at 64x32). The compromise
  is asserted by tests so it cannot silently crop.

During a between-halves break (`InningPhase.MIDDLE`/`END`) there is no active
at-bat, so the larger profiles show the phase label alone and suppress the count,
outs, and bases — a stale "1-2, 2 OUT" between innings would be misinformation.
"""

from __future__ import annotations

from datetime import datetime
from typing import assert_never

from omni.cards.base import ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.baseball import BaseballBaseState, BatterGameLine, InningPhase, TeamLinescore
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.line_score import (
    LINE_SCORE_RIGHT,
    RHE_BIG_FONT,
    LineScoreFace,
    draw_line_score_row,
    line_score_face,
)
from omni.renderers.player_lines import PANEL_X, PanelEntry, draw_name_with_stats, draw_panel_entry
from omni.renderers.team_row import draw_treated_mark
from omni.renderers.visual_treatment import resolve_matchup_treatment
from omni.renderers.win_meter import draw_win_meter
from omni.renderers.text import draw_centered, draw_right_aligned, text_width

__all__ = ["LiveBaseballRenderer"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)
_DIM = RGBColor(60, 60, 60)
_HE = RGBColor(160, 160, 160)  # hits/errors line — legible but secondary to the bright run score
_HE_DIM = RGBColor(204, 204, 204)  # the quad line score's hits + errors, held a step (0.8) under the full-white runs

_LABEL_FONT = "4x6"
_SCORE_FONT = "6x10"
_STATE_FONT = "8x13"  # the quad inning number + count (downsized; the triangle is drawn as a shape)
_SINGLE_ABBR_FONT = "5x7"
_DUE_UP_LABEL_FONT = "5x7"
_THIN = "\N{THIN SPACE}"  # U+2009, a 2px space in 4x6 — packs dense statlines tighter than a full cell

# The ABS challenge lane: a stack of bulbs per side, in the gap between that team's win-meter stripe
# and its score, held clear by `_ABS_MARGIN` either side so the lane reads as its own module rather
# than punctuation hung off the meter or the runs. Every profile puts it in the same relation — mark,
# meter, then bulbs — at the same size, so the lane reads identically from board to board.
_ABS_MARGIN = 2
_ABS_GAP = 2  # air between the two stacked bulbs
_ABS_DOT = 4  # bulb diameter, the same on every board — at 3 a circle has no corners left to round
_ABS_X = 24  # quad: a flush 20px tile, meter to x=21
_ABS_X_STACK = 25  # stack: the tile is inset a pixel, so the lane follows it right
_ABS_X_SINGLE = 6  # single: a 2px bar, then a 2px meter

# Small-profile lanes. On the stack the tile names the club, so the abbreviation only returns as the
# color-bar fallback's label — drawn in the footprint the missing tile left, where it can't collide
# with the meter, the bulbs, or the run total whichever path drew. The single never fits a tile, so
# its abbreviation is always drawn, and its game-state column claims the right quarter of the board.
_STACK_ABBR_X = 3
_STACK_RUNS_LEFT = 31  # the run total centers in [this, 64): everything the ABS lane's margin leaves
_SINGLE_ABBR_X = 12
_SINGLE_RUNS_RIGHT = 39

# Every glyph cell ends in a blank column, so a string's lit pixels stop a pixel short of its width.
# The two boards' state areas center on those lit pixels rather than on the cells.
#
# The single's state column centers on the column midway between the run total's last lit pixel and
# the board's edge. The stack's status band centers on the row midway down the 22px its home row
# leaves, with its outer columns pinned so the count + outs can float evenly between them.
_SINGLE_STATE_LINE = (_SINGLE_RUNS_RIGHT - 1 + 64) // 2
_SINGLE_INNING_GAP = 3  # triangle to number
_SINGLE_OUTS_GAP = 2  # count to out squares
_SINGLE_BASES_CY = 13
_STACK_INNING_LEFT = 2
_STACK_BASES_CX = 52
_STACK_BASES_LEFT = 42  # the cluster's left tip: r=4 + spread=6 out from its center
_STACK_ARROW_TOP_Y = {InningPhase.TOP: 44, InningPhase.BOTTOM: 59}  # two free rows either side of the number
_STACK_INNING_Y = 49
_STACK_COUNT_Y = 45
_STACK_OUTS_Y = 56
_STACK_OUTS_WIDTH = 19  # three 5px squares on a 7px pitch; the count centers over them
_STACK_BASES_CY = 50

# The batting half reads as a filled triangle drawn as a shape (`_triangle`): apex up for the top of
# the inning (visitor batting), down for the bottom (home batting) — every profile draws it, each at
# its own size. A break has no batting direction, so it reads as a short word instead (MID/END).
_TRIANGLE_HALF = 3  # both small boards; 2 * half + 1 wide, half + 1 tall
_TRIANGLE_WIDTH = 2 * _TRIANGLE_HALF + 1

# Quad strip geometry: the pitcher/batter roster lines run flush to the left edge across the strip.
_STRIP_LEFT = 0  # the roster lines sit flush to the board's left edge (margin 0)
_STRIP_RIGHT = 126  # right inset for the strip's text lanes (a 2px margin off the 128 edge)

# The live pitch stacks in two rows on the right below the state module — the game pitch count on top,
# the current pitch (speed + 4-char type) below — each right-anchored to the board's edge. Stacking
# frees the two numbers from competing on one line and opens room on the left of the block.
_PITCH_COUNT_Y = 30
_PITCH_LINE_Y = 36
_PITCH_RIGHT = 128  # the pitch rows' last pixel sits on the board's right edge (margin 0)

# A quad break: the inning centers under the line score, and Due Up runs down the right of the board,
# each batter's stat line in the gap below their name. The last sits level with the inning.
_BREAK_LABEL_Y = 46
_DUE_UP_LABEL_Y = 3
_DUE_UP_NAMES_Y = 11
_DUE_UP_PITCH = 17


def _break_word(phase: InningPhase) -> str:
    """The short word a break shows in place of the batting half."""
    return "MID" if phase is InningPhase.MIDDLE else "END"


def _lit_width(s: str, font: str) -> int:
    """The width of ``s``'s lit pixels: its cell width less the blank column closing the last cell."""
    return text_width(s, font) - 1


class LiveBaseballRenderer:
    """Draws a live MLB game card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[LiveBaseballCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("live-baseball card requires a TeamGame contest")
        payload = card.payload
        profile = context.profile

        canvas.fill(_BLACK)
        if profile is PanelProfile.QUAD_128X64:
            self._render_quad(canvas, context, game, payload)
        elif profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, context, game, payload)
        elif profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, context, game, payload)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(profile)

    def _render_quad(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # Top 40px: two team rows (logo + inline R/H/E) on the left, the game-state module on the
        # right. Bottom 24px: the pitcher/batter strip. The logo identifies the team, so the
        # abbreviation is dropped whenever a tile resolves (kept only as the color-bar fallback).
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.QUAD_128X64,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=20,
        )
        draw_treated_mark(canvas, context, game.away, treatment.away)
        draw_treated_mark(canvas, context, game.home, treatment.home)
        if payload.win_probability is not None:
            draw_win_meter(canvas, treatment, payload.win_probability)
        if payload.challenges is not None:  # ABS challenges left, in the lane between the meter and the line score
            for remaining, row_top in ((payload.challenges.away, 0), (payload.challenges.home, 20)):
                self._challenge_dots(canvas, _ABS_X, row_top, remaining, diameter=_ABS_DOT, row_height=20)
        self._line_scores(canvas, game, payload, away_logo=treatment.away.is_tile, home_logo=treatment.home.is_tile)
        if payload.phase.is_break:  # between halves: no at-bat, no bases — just the inning
            label = f"{_break_word(payload.phase)} {payload.inning}"
            draw_centered(canvas, 0, LINE_SCORE_RIGHT, _BREAK_LABEL_Y, label, _YELLOW, _STATE_FONT)
            self._due_up(canvas, payload, now=context.now)
            return
        self._state_module(canvas, payload)
        self._pitch_line(canvas, payload)
        self._batter_pitcher_strip(canvas, payload, now=context.now)

    def _due_up(self, canvas: Canvas, payload: LiveBaseballCardPayload, *, now: datetime) -> None:
        """Who bats next, down the right of the board: each batter's lineup spot and name in yellow,
        with the day's line in white in the gap below."""
        if payload.due_up is None:
            return
        canvas.text(PANEL_X, _DUE_UP_LABEL_Y, "Due Up:", _YELLOW, font=_DUE_UP_LABEL_FONT)
        for i, batter in enumerate(payload.due_up.batters):
            entry = PanelEntry(
                leader=self._lineup_leader(batter),
                name=batter.name,
                name_color=_YELLOW,
                stats=self._batter_line(batter),
                stats_color=_WHITE,
            )
            draw_panel_entry(canvas, entry, y=_DUE_UP_NAMES_Y + i * _DUE_UP_PITCH, now=now)

    def _line_scores(
        self, canvas: Canvas, game: TeamGame, payload: LiveBaseballCardPayload, *, away_logo: bool, home_logo: bool
    ) -> None:
        """Draw both rows' ``R H E`` as a big right-aligned line score; abbr prepended only without a logo.

        Both rows share one font so their three columns line up: the headline 9x18B, stepping down to
        the bold 13 the moment any run or hit reaches two digits, so the columns still clear the
        state module on the right.
        """
        away, home = payload.away_line, payload.home_line
        # Both rows share a face so their columns line up; it steps down the moment any value would spill.
        face = line_score_face((away.runs, away.hits, away.errors, home.runs, home.hits, home.errors))
        self._rhe_row(canvas, game.away, away, has_logo=away_logo, face=face, row_top=0)
        self._rhe_row(canvas, game.home, home, has_logo=home_logo, face=face, row_top=20)

    def _rhe_row(
        self,
        canvas: Canvas,
        team: Team,
        line: TeamLinescore,
        *,
        has_logo: bool,
        face: LineScoreFace,
        row_top: int,
    ) -> None:
        """One team's run/hit/error columns, with the abbreviation only when no logo names the team."""
        if not has_logo:  # only the color bar drew, so the abbreviation has to name the team
            canvas.text(3, row_top + 5, team.abbreviation, _WHITE, font=_SCORE_FONT)
        # Runs at full white; hits and errors a step down (0.8) so the run total reads as the headline.
        draw_line_score_row(
            canvas, (line.runs, line.hits, line.errors), face, row_top=row_top, colors=(_WHITE, _HE_DIM, _HE_DIM)
        )

    def _state_module(self, canvas: Canvas, payload: LiveBaseballCardPayload) -> None:
        """The game-state cluster: inning over count on the left, the base diamonds + out squares right.

        The inning is a filled triangle (pointing toward the batting half) beside its number; the
        count sits below, both in the state font, nudged up with breathing room. Inning and count sit
        off the line score by more than the gutter between R/H/E columns, so the middle of the board
        reads as a break between two modules rather than a fourth column. The diamonds cluster in the
        top-right — 2nd on top, 3rd and 1st a tight row below — a pixel in from the top and right
        edges, over the out squares. Inning, count, bases, and outs all draw yellow.
        """
        up = payload.phase is InningPhase.TOP  # apex toward the batting half (the visitor bats in the top)
        self._triangle(canvas, 79, 5, 4, up=up, color=_YELLOW)  # inning-half triangle, left of the number
        canvas.text(86, 1, str(payload.inning), _YELLOW, font=_STATE_FONT)  # inning number
        self._bases_cluster(canvas, 114, 6, 5, 7, payload.bases)  # a px in from the top and right edges
        canvas.text(75, 15, f"{payload.count.balls}-{payload.count.strikes}", _YELLOW, font=_STATE_FONT)  # count
        self._out_dots(canvas, 105, 21, payload.count.outs)  # outs, nudged up below the diamond cluster

    def _pitch_line(self, canvas: Canvas, payload: LiveBaseballCardPayload) -> None:
        """The live pitch, stacked in two right-anchored rows below the state module.

        ``P:<n>`` (the pitcher's pitches so far this game) sits on top, the current pitch — its speed,
        ``mph``, and the 4-char type, e.g. ``94mph SNKR`` — below it, both ending on the board's right
        edge. Stacking keeps the two numbers from crowding one line. Either is drawn only when present,
        and the pair only on a live at-bat (the caller skips it on a break).
        """
        pitcher = payload.pitcher
        if pitcher is not None:
            draw_right_aligned(canvas, _PITCH_RIGHT, _PITCH_COUNT_Y, f"P:{pitcher.pitches}", _WHITE, _LABEL_FONT)
        pitch = payload.last_pitch
        if pitch is not None:
            token = f"{pitch.velocity_mph}mph{_THIN}{pitch.pitch_type.short}"
            draw_right_aligned(canvas, _PITCH_RIGHT, _PITCH_LINE_Y, token, _WHITE, _LABEL_FONT)

    def _batter_pitcher_strip(self, canvas: Canvas, payload: LiveBaseballCardPayload, *, now: datetime) -> None:
        """The bottom strip: the pitcher's line, then the batter's, each flush to the left edge.

        ``P:`` leads the pitcher and the lineup spot (``4.``) the batter; the name is white and the
        stat line beside it a size smaller and yellow. The pitcher sits just under the pitch lane, the
        batter flush to the board bottom. The live pitch has its own lane above the strip, so neither
        row reserves room for it. Drawn only on a live at-bat (skipped on a break).
        """
        pitcher = payload.pitcher
        if pitcher is not None:
            stats = f"{pitcher.innings_pitched}IP{_THIN}{pitcher.strikeouts}K"
            self._roster_line(canvas, "P:", pitcher.name, stats, y=43, right=_STRIP_RIGHT, now=now)
        batter = payload.batter
        if batter is not None:
            self._roster_line(
                canvas,
                self._lineup_leader(batter),
                batter.name,
                self._batter_line(batter),
                y=54,
                right=_STRIP_RIGHT,
                now=now,
            )

    @staticmethod
    def _lineup_leader(batter: BatterGameLine) -> str:
        """The ``4.`` that leads a batter's name, so it reads as the batter rather than initials."""
        return f"{batter.order}." if batter.order is not None else "#."

    @staticmethod
    def _batter_line(batter: BatterGameLine) -> str:
        """The batter's day in the legacy stat order — hits-for-AB, then HR, then RBI.

        Cribbed from the legacy board: a lone HR/RBI shows just the label (a flag that it
        happened), two or more carry the count. Tokens are thin-spaced to stay compact.
        (The legacy order slots 3B/2B between HR and RBI — restored here once modelled.)
        """
        parts = [f"{batter.hits}-{batter.at_bats}"]
        if batter.home_runs > 0:
            parts.append(f"{batter.home_runs}HR" if batter.home_runs > 1 else "HR")
        if batter.rbi > 0:
            parts.append(f"{batter.rbi}RBI" if batter.rbi > 1 else "RBI")
        return _THIN.join(parts)

    def _roster_line(
        self, canvas: Canvas, leader: str, name: str, stats: str, *, y: int, right: int, now: datetime
    ) -> None:
        """A strip line: ``leader`` and ``name`` in white, then ``stats`` in yellow a size smaller beside it."""
        draw_name_with_stats(
            canvas,
            name,
            stats,
            leader=leader,
            x=_STRIP_LEFT,
            y=y,
            right=right,
            name_color=_WHITE,
            stats_color=_YELLOW,
            now=now,
        )

    def _render_stack(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # 64x64: the full layout compressed — two team rows up top, the status band below. The tile
        # names the club, so the abbreviation beside it is dropped (it only returns as the color-bar
        # fallback's label); the room that frees is what the win meter and the ABS lane now occupy.
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.STACK_64X64,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=22,
        )
        for team, side, row_top in ((game.away, treatment.away, 0), (game.home, treatment.home, 22)):
            draw_treated_mark(canvas, context, team, side)
            if not side.is_tile:  # a bare color bar can't name the club, so the abbreviation stays
                canvas.text(_STACK_ABBR_X, row_top + 5, team.abbreviation, _WHITE, font=_SCORE_FONT)
        if payload.win_probability is not None:
            draw_win_meter(canvas, treatment, payload.win_probability)
        if payload.challenges is not None:
            for remaining, row_top in ((payload.challenges.away, 0), (payload.challenges.home, 22)):
                self._challenge_dots(canvas, _ABS_X_STACK, row_top, remaining, diameter=_ABS_DOT, row_height=20)
        # The run total is the row's headline: the largest face we carry, centered in what the ABS
        # lane leaves — the row holds nothing else, so centering beats hanging it off the right edge.
        for runs, row_top in ((payload.away_line.runs, 0), (payload.home_line.runs, 22)):
            draw_centered(canvas, _STACK_RUNS_LEFT, 64, row_top + 1, str(runs), _WHITE, RHE_BIG_FONT)
        if payload.phase.is_break:  # between halves: no at-bat, no bases — just the inning
            label = f"{_break_word(payload.phase)} {payload.inning}"
            draw_centered(canvas, 0, 64, _STACK_INNING_Y, label, _YELLOW, _SCORE_FONT)
            return
        self._stack_status_band(canvas, payload)

    def _render_single(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # 64x32: the tightest profile, but the padding it used to carry buys real state. Each 16px row
        # runs color bar, win-meter stripe, ABS bulbs, abbreviation, run total; the right quarter is a
        # game-state column shared by both rows, stacked rather than spread — inning over bases over
        # count + outs. Stacking is what pays for the whole column: the inning no longer sits beside
        # the cluster, so both scale up and the count and outs fit under them. Only the pitcher/batter
        # strip and the H/E detail stay off at this size — an asserted compromise.
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.SINGLE_64X32,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=16,
        )
        # A two-digit run total reaches back to the abbreviation, so both step down to keep them apart.
        two_digits = max(payload.away_line.runs, payload.home_line.runs) >= 10
        abbr_font = _LABEL_FONT if two_digits else _SINGLE_ABBR_FONT
        for team, side, row_top in ((game.away, treatment.away, 0), (game.home, treatment.home, 16)):
            draw_treated_mark(canvas, context, team, side)  # never a tile here — a 20px tile can't fit a 16px row
            canvas.text(_SINGLE_ABBR_X, row_top + 5, team.abbreviation, _WHITE, font=abbr_font)
        if payload.win_probability is not None:
            draw_win_meter(canvas, treatment, payload.win_probability)
        if payload.challenges is not None:
            for remaining, row_top in ((payload.challenges.away, 0), (payload.challenges.home, 16)):
                self._challenge_dots(canvas, _ABS_X_SINGLE, row_top, remaining, diameter=_ABS_DOT, row_height=16)
        draw_right_aligned(canvas, _SINGLE_RUNS_RIGHT, 3, str(payload.away_line.runs), _WHITE, _SCORE_FONT)
        draw_right_aligned(canvas, _SINGLE_RUNS_RIGHT, 19, str(payload.home_line.runs), _WHITE, _SCORE_FONT)
        if payload.phase.is_break:  # a break has no batting direction, no bases and no live count
            # Split into a tight pair around the board's middle, in the run totals' face: the M of
            # MID doesn't resolve in 4x6.
            for text, y in ((_break_word(payload.phase), 7), (str(payload.inning), 16)):
                x = _SINGLE_STATE_LINE - _lit_width(text, _SCORE_FONT) // 2
                canvas.text(x, y, text, _YELLOW, font=_SCORE_FONT)
            return
        self._single_state_column(canvas, payload)

    def _stack_status_band(self, canvas: Canvas, payload: LiveBaseballCardPayload) -> None:
        """The stack's status band: inning, count over outs, and bases, each centered on one row.

        The number holds still and the half-arrow changes sides around it, so which side it sits on
        carries the half along with which way it points. The count + outs float midway between the
        inning and the bases, re-solving once extras widen the number so the two gutters stay equal.
        """
        inning = str(payload.inning)
        inning_width = _lit_width(inning, _SCORE_FONT)
        column_width = max(_TRIANGLE_WIDTH, inning_width)
        center = _STACK_INNING_LEFT + column_width // 2
        top_y = _STACK_ARROW_TOP_Y[payload.phase]
        self._triangle(canvas, center, top_y, _TRIANGLE_HALF, up=payload.phase is InningPhase.TOP, color=_YELLOW)
        canvas.text(center - inning_width // 2, _STACK_INNING_Y, inning, _YELLOW, font=_SCORE_FONT)

        inning_right = _STACK_INNING_LEFT + column_width
        outs_x = inning_right + (_STACK_BASES_LEFT - inning_right - _STACK_OUTS_WIDTH) // 2
        count = f"{payload.count.balls}-{payload.count.strikes}"
        count_x = outs_x + (_STACK_OUTS_WIDTH - _lit_width(count, _SCORE_FONT)) // 2
        canvas.text(count_x, _STACK_COUNT_Y, count, _YELLOW, font=_SCORE_FONT)
        self._out_dots(canvas, outs_x, _STACK_OUTS_Y, payload.count.outs)

        self._bases_cluster(canvas, _STACK_BASES_CX, _STACK_BASES_CY, 4, 6, payload.bases)

    def _single_state_column(self, canvas: Canvas, payload: LiveBaseballCardPayload) -> None:
        """The single's state column: inning over bases over count + outs, centered on one line.

        The inning group re-centers as a whole when extras widen its number, so the triangle gives
        ground rather than the number running off the board.
        """
        line = _SINGLE_STATE_LINE
        inning = str(payload.inning)
        inning_group = _TRIANGLE_WIDTH + _SINGLE_INNING_GAP + _lit_width(inning, _SCORE_FONT)
        left = line - inning_group // 2
        self._triangle(
            canvas, left + _TRIANGLE_HALF, 3, _TRIANGLE_HALF, up=payload.phase is InningPhase.TOP, color=_YELLOW
        )
        canvas.text(left + _TRIANGLE_WIDTH + _SINGLE_INNING_GAP, 0, inning, _YELLOW, font=_SCORE_FONT)

        self._bases_cluster(canvas, line, _SINGLE_BASES_CY, 3, 5, payload.bases)

        count = f"{payload.count.balls}-{payload.count.strikes}"
        count_width = _lit_width(count, _LABEL_FONT)
        row = count_width + _SINGLE_OUTS_GAP + 11  # three 3px squares on a 4px pitch
        left = line - (row - 1) // 2
        canvas.text(left, 25, count, _YELLOW, font=_LABEL_FONT)
        self._out_dots(canvas, left + count_width + _SINGLE_OUTS_GAP, 26, payload.count.outs, size=3, pitch=4)

    @staticmethod
    def _triangle(canvas: Canvas, cx: int, top_y: int, half: int, *, up: bool, color: RGBColor) -> None:
        """A filled triangle with a 1:1 slope (each row insets 1px per side), pointing up or down.

        ``cx`` is the horizontal center and ``top_y`` the top row; the base is ``2 * half + 1`` wide
        and the shape ``half + 1`` rows tall. ``up`` puts the apex at the top (the visitor's half),
        otherwise at the bottom (the home half).
        """
        for i in range(half + 1):
            reach = i if up else half - i
            canvas.fill_rect(cx - reach, top_y + i, 2 * reach + 1, 1, color)

    def _bases_cluster(self, canvas: Canvas, cx: int, cy: int, r: int, spread: int, bases: BaseballBaseState) -> None:
        """The three bases as rotated diamonds in the infield's own arrangement: 2nd on top, 3rd and
        1st a row below, each ``spread`` px out from the center on both axes.

        Rotating the markers is what lets them nest — the diagonal faces run parallel, so at a spread
        near ``r`` they come within a pixel or two of touching — and the cluster reads as an infield
        in less room than two separated rows of upright squares would take.
        """
        self._diamond(canvas, cx, cy, r, bases.second)
        self._diamond(canvas, cx - spread, cy + spread, r, bases.third)
        self._diamond(canvas, cx + spread, cy + spread, r, bases.first)

    @staticmethod
    def _diamond(canvas: Canvas, cx: int, cy: int, r: int, occupied: bool) -> None:
        """A base marker rotated 45° (a diamond): yellow-filled when occupied, dim outline when empty."""
        for dy in range(-r, r + 1):
            half = r - abs(dy)
            if occupied:
                canvas.fill_rect(cx - half, cy + dy, 2 * half + 1, 1, _YELLOW)
            else:
                canvas.set_pixel(cx - half, cy + dy, _DIM)
                if half > 0:
                    canvas.set_pixel(cx + half, cy + dy, _DIM)

    @staticmethod
    def _out_dots(canvas: Canvas, x: int, y: int, outs: int, *, size: int = 5, pitch: int = 7) -> None:
        """Three out squares, filled once recorded — the empty ones show the outs still to go.

        ``size`` and ``pitch`` shrink the row for the boards that can't spare the quad's 5px squares;
        below that the outline and the fill differ by a pixel or two, so it is the yellow-vs-dim
        contrast, not the shape, that carries the reading at every size.
        """
        for i in range(3):
            left = x + i * pitch
            if i < outs:
                canvas.fill_rect(left, y, size, size, _YELLOW)
            else:
                canvas.fill_rect(left, y, size, 1, _DIM)  # top
                canvas.fill_rect(left, y + size - 1, size, 1, _DIM)  # bottom
                canvas.fill_rect(left, y, 1, size, _DIM)  # left
                canvas.fill_rect(left + size - 1, y, 1, size, _DIM)  # right

    def _challenge_dots(
        self,
        canvas: Canvas,
        x: int,
        row_top: int,
        remaining: int,
        *,
        diameter: int,
        row_height: int,
        total: int = 2,
    ) -> None:
        """A team's ABS challenges as ``total`` circles stacked in its lane: the bottom ``remaining``
        fill yellow (challenges left), the rest dim (spent).

        The pool burns down from the top, so the lit bulbs always sit at the bottom of the stack and a
        spend reads as the column shortening rather than a gap opening in its middle. The stack is
        centered in the ``row_height`` row so it sits level with the score beside it.
        """
        span = total * diameter + (total - 1) * _ABS_GAP
        top = row_top + (row_height - span) // 2
        spent = total - remaining
        for i in range(total):
            self._circle(canvas, x, top + i * (diameter + _ABS_GAP), diameter, _DIM if i < spent else _YELLOW)

    @staticmethod
    def _circle(canvas: Canvas, x: int, y: int, diameter: int, color: RGBColor) -> None:
        """A filled circle in the ``diameter``-square box at ``(x, y)``.

        Every row spans the full width but the first and last, which inset a pixel each side — at
        this scale that is the whole difference between reading as a dot and reading as a square.
        """
        for row in range(diameter):
            inset = 1 if row in (0, diameter - 1) else 0
            canvas.fill_rect(x + inset, y + row, diameter - 2 * inset, 1, color)
