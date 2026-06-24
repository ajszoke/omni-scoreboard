"""Renderer for the live-baseball card across all three panel profiles.

Each profile gets its OWN layout, never a crop of another (AGENTS.md forbids
"cropping 128x64 cards down to 64x32"):

- quad_128x64 : full layout — team rows, scores, inning/count/outs, bases diamond.
- stack_64x64 : the full layout compressed to 64px wide (same fields, smaller).
- single_64x32: an explicit COMPROMISE — team abbreviations, scores, and the
  inning-phase label only. Count, outs, and the bases diamond are omitted (not
  legible at 64x32). The compromise is asserted by tests so it cannot silently
  regress into a crop.

During a between-halves break (`InningPhase.MIDDLE`/`END`) there is no active
at-bat, so the larger profiles show the phase label alone and suppress the count,
outs, and bases — a stale "1-2, 2 OUT" between innings would be misinformation.
"""

from __future__ import annotations

from typing import assert_never

from omni.cards.base import ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.baseball import InningPhase
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.team_row import draw_team_mark
from omni.renderers.text import draw_centered, draw_right_aligned

__all__ = ["LiveBaseballRenderer"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)
_DIM = RGBColor(60, 60, 60)

_LABEL_FONT = "4x6"
_SCORE_FONT = "6x10"

_PHASE_ABBR = {
    InningPhase.TOP: "T",
    InningPhase.MIDDLE: "MID",
    InningPhase.BOTTOM: "B",
    InningPhase.END: "END",
}


def _phase_label(phase: InningPhase, inning: int) -> str:
    """Compact inning-phase label: ``T7``/``B7`` when active, ``MID7``/``END7`` on a break."""
    return f"{_PHASE_ABBR[phase]}{inning}"


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
            self._render_single(canvas, game, payload)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(profile)

    def _render_quad(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # Two stacked team rows on the left (logo or colour bar), a status panel + bases on the right.
        away_x = draw_team_mark(canvas, context, game.away, row_top=0)
        home_x = draw_team_mark(canvas, context, game.home, row_top=32)
        canvas.text(away_x, 11, game.away.abbreviation, _WHITE, font=_SCORE_FONT)
        canvas.text(home_x, 43, game.home.abbreviation, _WHITE, font=_SCORE_FONT)
        draw_right_aligned(canvas, 58, 11, str(payload.away_score), _WHITE, _SCORE_FONT)
        draw_right_aligned(canvas, 58, 43, str(payload.home_score), _WHITE, _SCORE_FONT)
        label = _phase_label(payload.phase, payload.inning)
        if payload.phase.is_break:
            draw_centered(canvas, 64, 128, 28, label, _YELLOW, _LABEL_FONT)  # break: no live at-bat
            return
        canvas.text(68, 6, label, _YELLOW, font=_LABEL_FONT)
        canvas.text(68, 14, f"{payload.count.balls}-{payload.count.strikes}", _WHITE, font=_LABEL_FONT)
        canvas.text(68, 22, f"{payload.count.outs} OUT", _WHITE, font=_LABEL_FONT)
        self._base(canvas, 100, 6, 6, payload.bases.second)
        self._base(canvas, 92, 16, 6, payload.bases.third)
        self._base(canvas, 108, 16, 6, payload.bases.first)

    def _render_stack(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # 64x64: the full layout compressed — two team rows up top (logo or bar), status + bases below.
        away_x = draw_team_mark(canvas, context, game.away, row_top=0)
        home_x = draw_team_mark(canvas, context, game.home, row_top=22)
        canvas.text(away_x, 6, game.away.abbreviation, _WHITE, font=_SCORE_FONT)
        canvas.text(home_x, 28, game.home.abbreviation, _WHITE, font=_SCORE_FONT)
        draw_right_aligned(canvas, 62, 6, str(payload.away_score), _WHITE, _SCORE_FONT)
        draw_right_aligned(canvas, 62, 28, str(payload.home_score), _WHITE, _SCORE_FONT)
        label = _phase_label(payload.phase, payload.inning)
        if payload.phase.is_break:
            draw_centered(canvas, 0, 64, 50, label, _YELLOW, _LABEL_FONT)  # break: no live at-bat
            return
        canvas.text(3, 46, label, _YELLOW, font=_LABEL_FONT)
        canvas.text(20, 46, f"{payload.count.balls}-{payload.count.strikes}", _WHITE, font=_LABEL_FONT)
        canvas.text(3, 55, f"{payload.count.outs} OUT", _WHITE, font=_LABEL_FONT)
        self._base(canvas, 49, 45, 5, payload.bases.second)
        self._base(canvas, 43, 52, 5, payload.bases.third)
        self._base(canvas, 55, 52, 5, payload.bases.first)

    def _render_single(self, canvas: Canvas, game: TeamGame, payload: LiveBaseballCardPayload) -> None:
        # 64x32 compromise: abbreviations + scores + the inning-phase label only.
        canvas.fill_rect(0, 0, 2, 16, game.away.primary_color)
        canvas.fill_rect(0, 16, 2, 16, game.home.primary_color)
        canvas.text(4, 5, game.away.abbreviation, _WHITE, font=_LABEL_FONT)
        canvas.text(4, 21, game.home.abbreviation, _WHITE, font=_LABEL_FONT)
        draw_right_aligned(canvas, 42, 3, str(payload.away_score), _WHITE, _SCORE_FONT)
        draw_right_aligned(canvas, 42, 19, str(payload.home_score), _WHITE, _SCORE_FONT)
        # The phase label (T7/MID7/B7/END7) already conveys the break; no count/bases here anyway.
        canvas.text(46, 13, _phase_label(payload.phase, payload.inning), _YELLOW, font=_LABEL_FONT)

    @staticmethod
    def _base(canvas: Canvas, x: int, y: int, size: int, occupied: bool) -> None:
        if occupied:
            canvas.fill_rect(x, y, size, size, _WHITE)
            return
        canvas.fill_rect(x, y, size, 1, _DIM)  # top edge
        canvas.fill_rect(x, y + size - 1, size, 1, _DIM)  # bottom edge
        canvas.fill_rect(x, y, 1, size, _DIM)  # left edge
        canvas.fill_rect(x + size - 1, y, 1, size, _DIM)  # right edge
