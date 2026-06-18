"""The renderer contract: a card + a profile + a canvas, drawn as a frame."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omni.cards.base import CardPayloadT, ScoreboardCard
from omni.core.enum import PanelProfile
from omni.renderers.canvas import Canvas

__all__ = ["Renderer"]


@runtime_checkable
class Renderer(Protocol[CardPayloadT]):
    """Draws one kind of card. Pure: given a card, profile, and canvas, it draws —
    it never fetches data or parses provider JSON."""

    @property
    def supported_profiles(self) -> frozenset[PanelProfile]: ...

    def render(self, card: ScoreboardCard[CardPayloadT], profile: PanelProfile, canvas: Canvas) -> None: ...
