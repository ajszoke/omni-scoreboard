"""Renderer for the live-baseball card.

Supports quad_128x64 today; stack_64x64 and single_64x32 layouts follow (each an
explicit, tested compromise — never a crop of the 128x64 layout).
"""

from __future__ import annotations

from omni.cards.base import ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.font import char_size

__all__ = ["LiveBaseballRenderer"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)
_DIM = RGBColor(60, 60, 60)

_LABEL_FONT = "4x6"
_SCORE_FONT = "6x10"

# quad_128x64 layout: two stacked team rows on the left, a status panel on the right.
_STRIPE_W = 4
_ABBR_X = 8
_SCORE_RIGHT_X = 58
_AWAY_Y = 11
_HOME_Y = 43
_STATUS_X = 68


class LiveBaseballRenderer:
    """Draws a live MLB game card onto a `Canvas`."""

    supported_profiles = frozenset({PanelProfile.QUAD_128X64})

    def render(
        self,
        card: ScoreboardCard[LiveBaseballCardPayload],
        profile: PanelProfile,
        canvas: Canvas,
    ) -> None:
        if profile not in self.supported_profiles:
            raise NotImplementedError(f"{type(self).__name__} does not support {profile}")
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("live-baseball card requires a TeamGame contest")
        payload = card.payload

        canvas.fill(_BLACK)

        # Team color stripes: away on the top row, home on the bottom row.
        canvas.fill_rect(0, 0, _STRIPE_W, 32, game.away.primary_color)
        canvas.fill_rect(0, 32, _STRIPE_W, 32, game.home.primary_color)

        # Abbreviations and right-aligned scores.
        canvas.text(_ABBR_X, _AWAY_Y, game.away.abbreviation, _WHITE, font=_SCORE_FONT)
        canvas.text(_ABBR_X, _HOME_Y, game.home.abbreviation, _WHITE, font=_SCORE_FONT)
        self._right_text(canvas, _SCORE_RIGHT_X, _AWAY_Y, str(payload.away_score), _WHITE, _SCORE_FONT)
        self._right_text(canvas, _SCORE_RIGHT_X, _HOME_Y, str(payload.home_score), _WHITE, _SCORE_FONT)

        # Status panel: inning, count, outs.
        half = "T" if payload.half == "top" else "B"
        canvas.text(_STATUS_X, 6, f"{half}{payload.inning}", _YELLOW, font=_LABEL_FONT)
        canvas.text(_STATUS_X, 14, f"{payload.count.balls}-{payload.count.strikes}", _WHITE, font=_LABEL_FONT)
        canvas.text(_STATUS_X, 22, f"{payload.count.outs} OUT", _WHITE, font=_LABEL_FONT)

        # Bases diamond: 2B top, 3B left, 1B right.
        self._base(canvas, 100, 6, payload.bases.second)
        self._base(canvas, 92, 16, payload.bases.third)
        self._base(canvas, 108, 16, payload.bases.first)

    @staticmethod
    def _right_text(canvas: Canvas, right_x: int, y: int, s: str, color: RGBColor, font: str) -> None:
        char_w, _ = char_size(font)
        canvas.text(right_x - char_w * len(s), y, s, color, font=font)

    @staticmethod
    def _base(canvas: Canvas, x: int, y: int, occupied: bool) -> None:
        if occupied:
            canvas.fill_rect(x, y, 6, 6, _WHITE)
            return
        canvas.fill_rect(x, y, 6, 1, _DIM)  # top edge
        canvas.fill_rect(x, y + 5, 6, 1, _DIM)  # bottom edge
        canvas.fill_rect(x, y, 1, 6, _DIM)  # left edge
        canvas.fill_rect(x + 5, y, 1, 6, _DIM)  # right edge
