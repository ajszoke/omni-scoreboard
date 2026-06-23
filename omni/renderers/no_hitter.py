"""Renderer for the no-hitter / perfect-game card across all three panel profiles.

A standing alert that a pitching team is carrying a no-hitter (or perfect game): a bold
headline, the defending team, and how deep the bid has carried. It is centred (not a
score grid) — a feat callout, surfaced periodically by a RECURRING attention rather than
flashed once like a big play.

- quad_128x64 : headline + team + "THROUGH N", each on its own centred line.
- stack_64x64 : the same three lines, compressed to 64px wide ("THRU N").
- single_64x32: an explicit COMPROMISE — headline + team only; the "through N" inning
  is dropped (no room at 64x32). Asserted by tests.
"""

from __future__ import annotations

from typing import assert_never

from omni.cards.baseball import NoHitterCardPayload
from omni.cards.base import ScoreboardCard
from omni.core.colors import RGBColor
from omni.core.enum import HomeAway, PanelProfile
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.text import draw_centered

__all__ = ["NoHitterRenderer", "headline_for"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_GOLD = RGBColor(255, 215, 0)

_HEADLINE_FONT = "6x10"
_LABEL_FONT = "4x6"


def headline_for(perfect: bool) -> str:
    """The feat headline: a perfect game vs a plain no-hitter."""
    return "PERFECT GAME" if perfect else "NO-HITTER"


def _pitching_abbr(game: TeamGame, side: HomeAway) -> str:
    team = game.home if side is HomeAway.HOME else game.away
    return team.abbreviation


class NoHitterRenderer:
    """Draws a no-hitter / perfect-game alert onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[NoHitterCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("no-hitter card requires a TeamGame contest")
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

    def _render_quad(self, canvas: Canvas, game: TeamGame, payload: NoHitterCardPayload) -> None:
        draw_centered(canvas, 0, 128, 8, headline_for(payload.perfect), _GOLD, _HEADLINE_FONT)
        draw_centered(canvas, 0, 128, 26, _pitching_abbr(game, payload.pitching_side), _WHITE, _HEADLINE_FONT)
        draw_centered(canvas, 0, 128, 44, f"THROUGH {payload.through_inning}", _WHITE, _LABEL_FONT)

    def _render_stack(self, canvas: Canvas, game: TeamGame, payload: NoHitterCardPayload) -> None:
        draw_centered(canvas, 0, 64, 8, headline_for(payload.perfect), _GOLD, _LABEL_FONT)
        draw_centered(canvas, 0, 64, 30, _pitching_abbr(game, payload.pitching_side), _WHITE, _HEADLINE_FONT)
        draw_centered(canvas, 0, 64, 50, f"THRU {payload.through_inning}", _WHITE, _LABEL_FONT)

    def _render_single(self, canvas: Canvas, game: TeamGame, payload: NoHitterCardPayload) -> None:
        # 64x32 compromise: headline + team only — no room for the "through N" inning.
        draw_centered(canvas, 0, 64, 4, headline_for(payload.perfect), _GOLD, _LABEL_FONT)
        draw_centered(canvas, 0, 64, 18, _pitching_abbr(game, payload.pitching_side), _WHITE, _LABEL_FONT)
