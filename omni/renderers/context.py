"""RenderContext: the ambient render-time inputs a renderer draws against.

A renderer is a pure function of *what* to draw (the `ScoreboardCard`) and *where*
(the `Canvas`). Everything else it needs — the panel profile, and later the render
clock, theme, and contrast policy — is ambient context that the orchestrator owns,
not the card. Bundling it here keeps the renderer contract stable: B1's lifecycle
cards can grow what they need (a `now` for a pregame countdown, a contrast policy for
logo legibility) by adding a field here, with **no change to any renderer signature
or call site that doesn't use the new field**.

Today it carries only the profile; that single field is enough to lock the seam.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.enum import PanelProfile

__all__ = ["RenderContext"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderContext:
    """Immutable render-time context handed to a renderer alongside the card and canvas."""

    profile: PanelProfile
