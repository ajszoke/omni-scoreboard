"""Renderer for the status (delay / suspension) card across all three panel profiles.

A status card keeps a paused game on the board — the matchup plus a bold banner naming the
state — instead of letting a `DELAYED`/`SUSPENDED` game fall out of every lifecycle phase. It
shows no score (the result is not known, and a paused live score is a spoiler the delay
machinery owns), so the banner *is* the message and every profile shows the full card:

- quad_128x64 : team rows on the left (logo or colour bar + abbreviation), banner on the right.
- stack_64x64 : the two team rows up top, banner below.
- single_64x32: matchup abbreviations on the left, banner on the right — full info, no crop.
"""

from __future__ import annotations

from typing import assert_never

from omni.cards.baseball import StatusCardPayload
from omni.cards.base import ScoreboardCard
from omni.core.colors import RGBColor
from omni.core.enum import GameStatus, PanelProfile
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.team_row import draw_matchup_marks
from omni.renderers.text import draw_centered, draw_right_aligned

__all__ = ["StatusRenderer", "status_banner"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_AMBER = RGBColor(255, 176, 0)  # a caution colour — the game is paused, not over

_LABEL_FONT = "4x6"
_VALUE_FONT = "6x10"

# A paused state -> its on-screen banner. Keyed by exactly the statuses a status card accepts
# (`STATUS_CARD_STATUSES`), so the payload's validation guarantees a banner always exists.
_BANNERS: dict[GameStatus, str] = {
    GameStatus.DELAYED: "DELAYED",
    GameStatus.SUSPENDED: "SUSPENDED",
}


def status_banner(status: GameStatus) -> str:
    """The banner text for a paused-game status (a delay or a suspension)."""
    return _BANNERS[status]


class StatusRenderer:
    """Draws a delay / suspension status card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[StatusCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("status card requires a TeamGame contest")
        banner = status_banner(card.payload.status)
        profile = context.profile

        canvas.fill(_BLACK)
        if profile is PanelProfile.QUAD_128X64:
            self._render_quad(canvas, context, game, banner)
        elif profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, context, game, banner)
        elif profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, game, banner)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(profile)

    def _render_quad(self, canvas: Canvas, context: RenderContext, game: TeamGame, banner: str) -> None:
        # Team rows on the left (logo or colour bar, no scores); banner centred on the right.
        away_x, home_x = draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=32)
        canvas.text(away_x, 11, game.away.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(home_x, 43, game.home.abbreviation, _WHITE, font=_VALUE_FONT)
        draw_centered(canvas, 64, 128, 27, banner, _AMBER, _VALUE_FONT)

    def _render_stack(self, canvas: Canvas, context: RenderContext, game: TeamGame, banner: str) -> None:
        # 64x64: two team rows up top (logo or bar), banner below.
        away_x, home_x = draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=22)
        canvas.text(away_x, 6, game.away.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(home_x, 28, game.home.abbreviation, _WHITE, font=_VALUE_FONT)
        draw_centered(canvas, 0, 64, 50, banner, _AMBER, _LABEL_FONT)

    def _render_single(self, canvas: Canvas, game: TeamGame, banner: str) -> None:
        # 64x32: abbreviations on the left, banner on the right — the full card still fits.
        canvas.fill_rect(0, 0, 2, 16, game.away.primary_color)
        canvas.fill_rect(0, 16, 2, 16, game.home.primary_color)
        canvas.text(4, 5, game.away.abbreviation, _WHITE, font=_LABEL_FONT)
        canvas.text(4, 21, game.home.abbreviation, _WHITE, font=_LABEL_FONT)
        draw_right_aligned(canvas, 62, 13, banner, _AMBER, _LABEL_FONT)
