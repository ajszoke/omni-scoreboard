"""Basic decision traces: what the board chose, when, why, and what reached the panel.

One line per change, so a running board's log reads as a story rather than a heartbeat:

    decide 19:05:12 in 0.4s: mlb-745123:live (rotation) | provider fresh | next 19:05:24
    present 19:05:12: mlb-745123:live (rotation) | away COL base tile meter #C4CED4 | home LAD base tile ...

- A **decision** line is written when the pick, its reason or the provider's status changes, and whenever
  a pass drops a game or a play. Each one names the next decision, the planner's next deadline.
- A **presentation** line is written at the first committed frame of a newly picked card, with how its
  teams were drawn: logo variant, tile or color bar, meter color, and the contrast policy that chose
  them.
- A paint failure is written once when it starts, and once when frames commit again.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo

from omni.app.loop import Decision, Painted, Presentation
from omni.core.colors import RGBColor
from omni.renderers.visual_treatment import MarkTreatment

__all__ = ["DecisionTracer"]


class DecisionTracer:
    """Writes the app's decisions and presentations as one-line traces, each when something changes."""

    def __init__(self, log: Callable[[str], None], *, timezone: tzinfo | None = None) -> None:
        self._log = log
        self._timezone = timezone
        self._last_decision: tuple[object, ...] | None = None
        self._last_presented: str | None = None
        self._paint_error: str | None = None

    def decided(self, decision: Decision, *, took: timedelta, next_at: datetime) -> None:
        pick = decision.pick
        key = (
            pick.card.id.raw if pick is not None else None,
            pick.reason if pick is not None else None,
            decision.provider_status,
        )
        dropped = decision.pipeline.skipped + decision.pipeline.warnings
        if key == self._last_decision and not dropped:
            return
        self._last_decision = key
        chose = f"{pick.card.id.raw} ({pick.reason.value})" if pick is not None else "nothing eligible"
        self._log(
            f"decide {self._clock(decision.at)} in {took.total_seconds():.1f}s: {chose}"
            f" | provider {decision.provider_status.value} | next {self._clock(next_at)}"
        )
        for note in dropped:
            self._log(f"  dropped: {note}")

    def painted(self, painted: Painted) -> None:
        if painted.render_error is not None:
            if painted.render_error != self._paint_error:
                self._paint_error = painted.render_error
                self._log(f"paint failed: {painted.render_error}")
            return
        if self._paint_error is not None and painted.shown is not None:
            self._paint_error = None
            self._log("paint recovered")
        if painted.presented is not None:
            self._presented(painted.presented)

    def failed(self, exc: Exception) -> None:
        self._log(f"decide failed: {type(exc).__name__}: {exc}")

    def _presented(self, presentation: Presentation) -> None:
        if presentation.card.raw == self._last_presented:
            return  # the same card picked again: still up, nothing new to say
        self._last_presented = presentation.card.raw
        line = f"present {self._clock(presentation.at)}: {presentation.card.raw} ({presentation.reason.value})"
        treatment = presentation.treatment
        if treatment is not None:
            line += (
                f" | away {_mark(treatment.away)} | home {_mark(treatment.home)}"
                f" | contrast {presentation.contrast.min_contrast:g}/{presentation.contrast.clash_delta_e:g}"
            )
        self._log(line)

    def _clock(self, instant: datetime) -> str:
        return f"{instant.astimezone(self._timezone):%H:%M:%S}"


def _mark(mark: MarkTreatment) -> str:
    drawn = "tile" if mark.is_tile else f"bar {_hex(mark.bar_color)}"
    return f"{mark.variant.value} {drawn} meter {_hex(mark.meter_color)}"


def _hex(color: RGBColor) -> str:
    return f"#{color.r:02X}{color.g:02X}{color.b:02X}"
