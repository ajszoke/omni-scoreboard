"""RenderContext: the ambient render-time inputs a renderer draws against.

A renderer is a pure function of *what* to draw (the `ScoreboardCard`) and *where*
(the `Canvas`). Everything else it needs — the panel profile, the render clock, and
a logo store — is ambient context that the orchestrator owns, not the card. Bundling
it here keeps the renderer contract stable: lifecycle cards grow what they need by
adding a field here, with **no change to any renderer signature or call site that
doesn't use the new field**.

`now` is the render clock. It lets a renderer derive *live* values — a pregame
first-pitch countdown, an "ago" stamp — from an otherwise stable card snapshot, so
the displayed value advances every tick without the card being rebuilt.

`logos` is the optional tile store. When present a renderer blits the team logo;
when absent (a unit test that doesn't care, a profile too small to fit one) it falls
back to a plain color bar — so adding it broke no existing call site.

`contrast` is the hardware-tunable legibility policy (the WCAG floor a dim brand color
is value-lifted to, and the clash distance two tiles must keep). It defaults to the
pre-policy behavior, so it too is a transparent addition; a device tunes it per sink.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.core.enum import PanelProfile
from omni.renderers.image import LogoStore
from omni.renderers.visual_treatment import DEFAULT_CONTRAST_POLICY, VisualContrastPolicy

__all__ = ["RenderContext"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RenderContext:
    """Immutable render-time context handed to a renderer alongside the card and canvas."""

    profile: PanelProfile
    now: datetime  # render time; renderers derive live values (e.g. countdowns) from this
    logos: LogoStore | None = None  # ambient tile store; None -> renderers fall back to a color bar
    contrast: VisualContrastPolicy = DEFAULT_CONTRAST_POLICY  # hw-tunable legibility floor + clash threshold

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
