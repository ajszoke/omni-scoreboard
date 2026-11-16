"""Basic decision traces: what the board put up, when, why, and what took it down.

One line per change, so a running board's log reads as a story rather than a heartbeat:

    refresh 19:05:12 in 0.0s: source fresh | next 19:05:31
    up 19:05:12: mlb-745123:live (rotation) | away COL base tile meter #C4CED4 | home LAD base tile ...
    down 19:05:24: mlb-745123:live (ran its time, 12.0s)
    frame 19:31:40: idle (nothing eligible)

- A **refresh** line is written when the source's health or the number of games held in the TV delay
  changes, and whenever a refresh drops a game or a play. Each one names the next refresh due by
  itself.
- An **up** line is written at a card's first committed frame, with why the queue chose it and how its
  teams were drawn: logo variant, tile or color bar, meter color, and the contrast policy that chose
  them. A **frame** line is written when an explicit frame goes up, with why.
- A **down** line says why a card came down and how long it was up; a **drop** line names a card that
  came down before it ever drew. A **stale** line marks a card going up, or turning, stale.
- A paint failure is written once when it starts, and once when frames commit again.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo

from omni.app.loop import Painted, Presentation, Refresh
from omni.core.colors import RGBColor
from omni.renderers.visual_treatment import MarkTreatment
from omni.tower.board import BoardFrame, BoardPayload
from omni.tower.lane import ChangeKind, LaneChange

__all__ = ["DecisionTracer"]


class DecisionTracer:
    """Writes the app's refreshes and the board's changes as one-line traces, each when something changes."""

    def __init__(self, log: Callable[[str], None], *, timezone: tzinfo | None = None) -> None:
        self._log = log
        self._timezone = timezone
        self._last_refresh: tuple[object, ...] | None = None
        self._stale: bool = False
        self._paint_error: str | None = None

    def refreshed(self, refresh: Refresh, *, took: timedelta, next_at: datetime | None) -> None:
        health = refresh.health
        key = (health.source, health.reason, health.held, health.unfed)
        dropped = refresh.pipeline.skipped + refresh.pipeline.warnings
        if key == self._last_refresh and not dropped and not refresh.unseen:
            return
        self._last_refresh = key
        line = f"refresh {self._clock(refresh.at)} in {took.total_seconds():.1f}s: source {health.source.value}"
        if health.reason:
            line += f" ({health.reason})"
        if health.held:
            line += f" | held {health.held}"
        if health.unfed:
            line += f" | unfed {health.unfed}"
        self._log(line + f" | next {self._clock(next_at) if next_at is not None else '-'}")
        for note in dropped:
            self._log(f"  dropped: {note}")
        for card in refresh.unseen:
            self._log(f"  unseen: {card.raw} (expired before it aired)")

    def painted(self, painted: Painted) -> None:
        for change in painted.changes:
            self._changed(change, painted)
        if painted.render_error is not None:
            if painted.render_error != self._paint_error:
                self._paint_error = painted.render_error
                self._log(f"paint failed: {painted.render_error}")
            return
        if self._paint_error is not None:
            self._paint_error = None
            self._log("paint recovered")
        if painted.shown is not None and painted.stale and not self._stale:
            self._log(f"stale {self._clock(painted.at)}: {painted.shown.raw}")
        self._stale = painted.stale

    def failed(self, exc: Exception) -> None:
        self._log(f"refresh failed: {type(exc).__name__}: {exc}")

    def _changed(self, change: LaneChange[BoardPayload], painted: Painted) -> None:
        payload = change.segment.frames[0]
        at = self._clock(change.at.utc)
        name = f"{payload.kind.value}" if isinstance(payload, BoardFrame) else payload.id.raw
        if change.kind is ChangeKind.UP:
            if isinstance(payload, BoardFrame):
                self._log(f"frame {at}: {payload.kind.value} ({payload.reason})")
            elif painted.presented is not None:
                self._log(self._presented(painted.presented))
            else:
                self._log(f"up {at}: {name}")
            return
        reason = change.reason.value if change.reason is not None else "-"
        if change.kind is ChangeKind.DOWN:
            aired = f", {change.aired.total_seconds():.1f}s" if change.aired is not None else ""
            self._log(f"down {at}: {name} ({reason}{aired})")
        else:
            self._log(f"drop {at}: {name} ({reason})")

    def _presented(self, presentation: Presentation) -> str:
        line = f"up {self._clock(presentation.at)}: {presentation.card.raw} ({presentation.reason.value})"
        treatment = presentation.treatment
        if treatment is not None:
            line += (
                f" | away {_mark(treatment.away)} | home {_mark(treatment.home)}"
                f" | contrast {presentation.contrast.min_contrast:g}/{presentation.contrast.clash_delta_e:g}"
            )
        return line

    def _clock(self, instant: datetime) -> str:
        return f"{instant.astimezone(self._timezone):%H:%M:%S}"


def _mark(mark: MarkTreatment) -> str:
    drawn = "tile" if mark.is_tile else f"bar {_hex(mark.bar_color)}"
    return f"{mark.variant.value} {drawn} meter {_hex(mark.meter_color)}"


def _hex(color: RGBColor) -> str:
    return f"#{color.r:02X}{color.g:02X}{color.b:02X}"
