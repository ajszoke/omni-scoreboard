"""RenderContext: the ambient render-time inputs a renderer draws against.

A renderer is a pure function of *what* to draw (the `ScoreboardCard`) and *where*
(the `Canvas`). Everything else it needs — the panel profile, the render clock, and
later a theme/contrast policy — is ambient context that the orchestrator owns, not
the card. Bundling it here keeps the renderer contract stable: B1's lifecycle cards
grow what they need by adding a field here, with **no change to any renderer
signature or call site that doesn't use the new field**.

`now` is the render clock. It lets a renderer derive *live* values — a pregame
first-pitch countdown, an "ago" stamp — from an otherwise stable card snapshot, so
the displayed value advances every tick without the card being rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.core.enum import PanelProfile

__all__ = ["RenderContext"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderContext:
    """Immutable render-time context handed to a renderer alongside the card and canvas."""

    profile: PanelProfile
    now: datetime  # render time; renderers derive live values (e.g. countdowns) from this

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
