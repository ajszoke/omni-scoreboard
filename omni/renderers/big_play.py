"""Renderer for the big-play card across all three panel profiles.

A momentary flash of a notable play: a headline derived from the event type, the
play's description, and the resulting score. It is centred (not a score grid) because
it briefly takes over the screen via a BURST attention before yielding.

- quad_128x64 : headline + description + score, each on its own centred line.
- stack_64x64 : the same three lines, compressed to 64px wide.
- single_64x32: an explicit COMPROMISE — headline + score only; the play
  description is dropped (no room at 64x32). Asserted by tests.
"""

from __future__ import annotations

from typing import assert_never

from omni.cards.baseball import BigPlayCardPayload
from omni.cards.base import ScoreboardCard
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEventType
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.text import draw_centered

__all__ = ["BigPlayRenderer", "headline_for"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)

_HEADLINE_FONT = "6x10"
_LABEL_FONT = "4x6"


def headline_for(event_type: BaseballGameEventType) -> str:
    """A short upper-case headline for the play, e.g. HOME_RUN -> ``HOME RUN``."""
    return str(event_type.value).replace("_", " ").upper()


def _fit(s: str, max_chars: int) -> str:
    """Truncate ``s`` to ``max_chars`` glyphs so it fits a panel's width."""
    return s if len(s) <= max_chars else s[:max_chars]


def _score_line(game: TeamGame, payload: BigPlayCardPayload) -> str:
    return f"{game.away.abbreviation} {payload.away_score}  {game.home.abbreviation} {payload.home_score}"


class BigPlayRenderer:
    """Draws a big-play flash card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[BigPlayCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("big-play card requires a TeamGame contest")
        payload = card.payload

        canvas.fill(_BLACK)
        if context.profile is PanelProfile.QUAD_128X64:
            self._render_quad(canvas, game, payload)
        elif context.profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, game, payload)
        elif context.profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, game, payload)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(context.profile)

    def _render_quad(self, canvas: Canvas, game: TeamGame, payload: BigPlayCardPayload) -> None:
        draw_centered(canvas, 0, 128, 8, headline_for(payload.event_type), _YELLOW, _HEADLINE_FONT)
        draw_centered(canvas, 0, 128, 26, _fit(payload.description, 31), _WHITE, _LABEL_FONT)
        draw_centered(canvas, 0, 128, 44, _score_line(game, payload), _WHITE, _HEADLINE_FONT)

    def _render_stack(self, canvas: Canvas, game: TeamGame, payload: BigPlayCardPayload) -> None:
        draw_centered(canvas, 0, 64, 8, headline_for(payload.event_type), _YELLOW, _LABEL_FONT)
        draw_centered(canvas, 0, 64, 30, _fit(payload.description, 15), _WHITE, _LABEL_FONT)
        draw_centered(canvas, 0, 64, 48, _score_line(game, payload), _WHITE, _LABEL_FONT)

    def _render_single(self, canvas: Canvas, game: TeamGame, payload: BigPlayCardPayload) -> None:
        # 64x32 compromise: headline + score only — no room for the play description.
        draw_centered(canvas, 0, 64, 4, headline_for(payload.event_type), _YELLOW, _LABEL_FONT)
        draw_centered(canvas, 0, 64, 18, _score_line(game, payload), _WHITE, _LABEL_FONT)
