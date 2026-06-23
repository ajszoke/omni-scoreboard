"""Renderer for the pregame baseball card across all three panel profiles.

A pregame card shows the matchup and a live **first-pitch countdown**. The countdown
is derived from the render clock (`RenderContext.now`) against the card's snapshotted
`scheduled_start`, so one card's display advances every tick — "2h05m" -> "45m" ->
"SOON" — without the card being rebuilt.

Each profile gets its OWN layout, never a crop of another:

- quad_128x64 : team rows on the left (colour bar + abbreviation, no scores yet),
  a "FIRST PITCH" label and the countdown on the right.
- stack_64x64 : the two team rows up top, label + countdown below.
- single_64x32: an explicit COMPROMISE — matchup abbreviations + countdown only.
  The "FIRST PITCH" label is dropped (no room). Asserted by tests so it cannot
  silently regress into a crop.
"""

from __future__ import annotations

from datetime import datetime
from typing import assert_never

from omni.cards.baseball import PregameCardPayload
from omni.cards.base import ScoreboardCard
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.text import draw_centered, draw_right_aligned

__all__ = ["PregameRenderer", "first_pitch_label"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)

_LABEL_FONT = "4x6"
_VALUE_FONT = "6x10"
_FIRST_PITCH = "FIRST PITCH"


def first_pitch_label(now: datetime, scheduled_start: datetime) -> str:
    """A compact "time until first pitch" string for a small panel.

    ``2h05m`` an hour or more out, ``45m`` within the hour, ``SOON`` under a minute
    out or once first pitch has passed (warmups / a delayed start that is still
    pregame). Both datetimes are timezone-aware (validated upstream).
    """
    seconds = (scheduled_start - now).total_seconds()
    if seconds < 60:
        return "SOON"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class PregameRenderer:
    """Draws a pregame MLB card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[PregameCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("pregame card requires a TeamGame contest")
        countdown = first_pitch_label(context.now, card.payload.scheduled_start)
        profile = context.profile

        canvas.fill(_BLACK)
        if profile is PanelProfile.QUAD_128X64:
            self._render_quad(canvas, game, countdown)
        elif profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, game, countdown)
        elif profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, game, countdown)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(profile)

    def _render_quad(self, canvas: Canvas, game: TeamGame, countdown: str) -> None:
        # Team rows on the left (no scores pregame); label + countdown on the right.
        canvas.fill_rect(0, 0, 4, 32, game.away.primary_color)
        canvas.fill_rect(0, 32, 4, 32, game.home.primary_color)
        canvas.text(8, 11, game.away.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(8, 43, game.home.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(66, 16, _FIRST_PITCH, _YELLOW, font=_LABEL_FONT)
        draw_centered(canvas, 64, 128, 34, countdown, _WHITE, _VALUE_FONT)

    def _render_stack(self, canvas: Canvas, game: TeamGame, countdown: str) -> None:
        # 64x64: two team rows up top, label + countdown below.
        canvas.fill_rect(0, 0, 3, 20, game.away.primary_color)
        canvas.fill_rect(0, 22, 3, 20, game.home.primary_color)
        canvas.text(5, 6, game.away.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(5, 28, game.home.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(3, 46, _FIRST_PITCH, _YELLOW, font=_LABEL_FONT)
        draw_centered(canvas, 0, 64, 54, countdown, _WHITE, _VALUE_FONT)

    def _render_single(self, canvas: Canvas, game: TeamGame, countdown: str) -> None:
        # 64x32 compromise: abbreviations + countdown only, no "first pitch" label.
        canvas.fill_rect(0, 0, 2, 16, game.away.primary_color)
        canvas.fill_rect(0, 16, 2, 16, game.home.primary_color)
        canvas.text(4, 5, game.away.abbreviation, _WHITE, font=_LABEL_FONT)
        canvas.text(4, 21, game.home.abbreviation, _WHITE, font=_LABEL_FONT)
        draw_right_aligned(canvas, 62, 11, countdown, _WHITE, _VALUE_FONT)
