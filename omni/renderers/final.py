"""Renderer for the final (postgame) baseball card across all three panel profiles.

The completed game with a **winner-derived treatment**: the winning side is drawn
bright and the loser dimmed (a tie leaves both bright), with a FINAL marker. Which
side won is the payload's derivation (`FinalCardPayload.winner`) — never a
position-based fade of "the home row". When the payload carries pitching decisions,
a W/L/S line (last names) accompanies the marker — degrading by profile:

- quad_128x64 : team rows + final scores on the left; "FINAL" and the full W/L/S
  (winner, loser, and save when there is one) stacked on the right.
- stack_64x64 : team rows + scores up top; "FINAL" then W/L below — the save line
  is dropped (no vertical room), an explicit, asserted COMPROMISE.
- single_64x32: the status reads "FIN" ("FINAL" does not fit at 64px wide) and the
  pitching line is dropped entirely — an explicit, asserted COMPROMISE.
"""

from __future__ import annotations

from typing import assert_never

from omni.cards.baseball import FinalCardPayload
from omni.cards.base import ScoreboardCard
from omni.core.colors import RGBColor
from omni.core.enum import HomeAway, PanelProfile
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.text import draw_centered, draw_right_aligned

__all__ = ["FinalRenderer"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)  # the winner (or both sides on a tie)
_LOSER = RGBColor(110, 110, 110)  # the loser, dimmed but still legible
_YELLOW = RGBColor(255, 215, 0)

_LABEL_FONT = "4x6"
_SCORE_FONT = "6x10"


def _side_color(side: HomeAway, winner: HomeAway | None) -> RGBColor:
    """Bright for the winner (or both, on a tie); dimmed for the loser."""
    return _WHITE if winner is None or side is winner else _LOSER


def _last_name(full_name: str) -> str:
    """The last whitespace-delimited token of a pitcher's name — what fits on a panel."""
    parts = full_name.split()
    return parts[-1] if parts else full_name


class FinalRenderer:
    """Draws a final MLB game card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[FinalCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("final card requires a TeamGame contest")
        payload = card.payload
        away = _side_color(HomeAway.AWAY, payload.winner)
        home = _side_color(HomeAway.HOME, payload.winner)

        canvas.fill(_BLACK)
        if context.profile is PanelProfile.QUAD_128X64:
            self._render_quad(canvas, game, payload, away, home)
        elif context.profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, game, payload, away, home)
        elif context.profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, game, payload, away, home)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(context.profile)

    def _render_quad(
        self, canvas: Canvas, game: TeamGame, payload: FinalCardPayload, away: RGBColor, home: RGBColor
    ) -> None:
        canvas.fill_rect(0, 0, 4, 32, game.away.primary_color)
        canvas.fill_rect(0, 32, 4, 32, game.home.primary_color)
        canvas.text(8, 11, game.away.abbreviation, away, font=_SCORE_FONT)
        canvas.text(8, 43, game.home.abbreviation, home, font=_SCORE_FONT)
        draw_right_aligned(canvas, 58, 11, str(payload.away_score), away, _SCORE_FONT)
        draw_right_aligned(canvas, 58, 43, str(payload.home_score), home, _SCORE_FONT)
        draw_centered(canvas, 64, 128, 11, "FINAL", _YELLOW, _LABEL_FONT)
        decisions = payload.decisions
        if decisions is not None:
            canvas.text(70, 27, f"W {_last_name(decisions.winner)}", _WHITE, font=_LABEL_FONT)
            canvas.text(70, 41, f"L {_last_name(decisions.loser)}", _LOSER, font=_LABEL_FONT)
            if decisions.save is not None:
                canvas.text(70, 55, f"S {_last_name(decisions.save)}", _WHITE, font=_LABEL_FONT)

    def _render_stack(
        self, canvas: Canvas, game: TeamGame, payload: FinalCardPayload, away: RGBColor, home: RGBColor
    ) -> None:
        canvas.fill_rect(0, 0, 3, 20, game.away.primary_color)
        canvas.fill_rect(0, 22, 3, 20, game.home.primary_color)
        canvas.text(5, 6, game.away.abbreviation, away, font=_SCORE_FONT)
        canvas.text(5, 28, game.home.abbreviation, home, font=_SCORE_FONT)
        draw_right_aligned(canvas, 62, 6, str(payload.away_score), away, _SCORE_FONT)
        draw_right_aligned(canvas, 62, 28, str(payload.home_score), home, _SCORE_FONT)
        draw_centered(canvas, 0, 64, 43, "FINAL", _YELLOW, _LABEL_FONT)
        decisions = payload.decisions
        if decisions is not None:
            # 64x64 compromise: W and L only — there is no vertical room for the save line.
            canvas.text(4, 51, f"W {_last_name(decisions.winner)}", _WHITE, font=_LABEL_FONT)
            canvas.text(4, 58, f"L {_last_name(decisions.loser)}", _LOSER, font=_LABEL_FONT)

    def _render_single(
        self, canvas: Canvas, game: TeamGame, payload: FinalCardPayload, away: RGBColor, home: RGBColor
    ) -> None:
        # 64x32 compromise: abbreviations + scores + a shortened "FIN" status.
        canvas.fill_rect(0, 0, 2, 16, game.away.primary_color)
        canvas.fill_rect(0, 16, 2, 16, game.home.primary_color)
        canvas.text(4, 5, game.away.abbreviation, away, font=_LABEL_FONT)
        canvas.text(4, 21, game.home.abbreviation, home, font=_LABEL_FONT)
        draw_right_aligned(canvas, 42, 3, str(payload.away_score), away, _SCORE_FONT)
        draw_right_aligned(canvas, 42, 19, str(payload.home_score), home, _SCORE_FONT)
        canvas.text(46, 13, "FIN", _YELLOW, font=_LABEL_FONT)
