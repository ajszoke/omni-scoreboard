"""RendererRegistry: pick and validate the renderer for a card.

Orchestration must not hard-code `LiveBaseballRenderer`. The
registry maps ``(Sport, CardKind) -> Renderer`` and, at dispatch, verifies the three
declarations that can otherwise drift apart all agree:

1. the card supports the target profile (``card.layout_support``),
2. the renderer supports it (``renderer.supported_profiles``), and
3. the canvas geometry matches the profile (``canvas.{width,height}``).

Keyed by sport *and* kind (not kind alone) so a future LIVE_GAME football card
routes to its own renderer rather than colliding with baseball.
"""

from __future__ import annotations

from typing import Any

from omni.cards.base import CardKind, ScoreboardCard
from omni.core.enum import Sport
from omni.panels.geometry import geometry_for
from omni.renderers.base import Renderer
from omni.renderers.big_play import BigPlayRenderer
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.final import FinalRenderer
from omni.renderers.live_baseball import LiveBaseballRenderer
from omni.renderers.pregame import PregameRenderer

__all__ = ["RenderDispatchError", "RendererRegistry", "default_registry"]


class RenderDispatchError(Exception):
    """No renderer is registered for a card, or card/renderer/canvas disagree on a profile."""


class RendererRegistry:
    """Routes a card to its renderer and validates the render contract."""

    def __init__(self) -> None:
        self._renderers: dict[tuple[Sport, CardKind], Renderer[Any]] = {}

    def register(self, sport: Sport, kind: CardKind, renderer: Renderer[Any]) -> None:
        """Register the renderer for a (sport, card-kind) pair (replacing any prior one)."""
        self._renderers[(sport, kind)] = renderer

    def renderer_for(self, card: ScoreboardCard[Any]) -> Renderer[Any]:
        """The renderer registered for this card, or raise `RenderDispatchError`."""
        key = (card.league.sport, card.kind)
        try:
            return self._renderers[key]
        except KeyError:
            raise RenderDispatchError(f"no renderer registered for {card.league.sport}/{card.kind}") from None

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        """Validate the card/renderer/canvas agree on ``context.profile``, then draw."""
        profile = context.profile
        renderer = self.renderer_for(card)
        if not card.layout_support.supports(profile):
            raise RenderDispatchError(f"card {card.kind} does not support {profile}")
        if profile not in renderer.supported_profiles:
            raise RenderDispatchError(f"renderer for {card.kind} does not support {profile}")
        geometry = geometry_for(profile)
        if (canvas.width, canvas.height) != (geometry.width, geometry.height):
            raise RenderDispatchError(
                f"canvas {canvas.width}x{canvas.height} does not match {profile} geometry "
                f"{geometry.width}x{geometry.height}"
            )
        renderer.render(card, context, canvas)


def default_registry() -> RendererRegistry:
    """A registry wired with the renderers shipped today (baseball live + pregame + final + big play)."""
    registry = RendererRegistry()
    registry.register(Sport.BASEBALL, CardKind.LIVE_GAME, LiveBaseballRenderer())
    registry.register(Sport.BASEBALL, CardKind.PREGAME, PregameRenderer())
    registry.register(Sport.BASEBALL, CardKind.FINAL, FinalRenderer())
    registry.register(Sport.BASEBALL, CardKind.BIG_PLAY, BigPlayRenderer())
    return registry
