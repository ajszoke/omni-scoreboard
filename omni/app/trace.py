"""Basic decision traces: what the board put up, when, why, and what took it down.

One line per change, so a running board's log reads as a story rather than a heartbeat:

    versions: policy omni-board-policy/1 | config omni-config/1 ee88362b3fae | rules omni-rules/2026.1 | ...
    config ee88362b3fae: delay=45s;favorites=mlb:ATL,mlb:COL;meter=off;odds=on;profile=quad_128x64;...
    refresh 19:05:12 in 0.0s: source fresh | next 19:05:31
    up 19:05:12: mlb:mlb_statsapi:745123:live (rotation) | away COL base tile meter #C4CED4 | home LAD ...
    down 19:05:24: mlb:mlb_statsapi:745123:live (ran its time, 12.0s)
    frame 19:31:40: idle (nothing eligible)

- A **versions** line names what the run decides with (`omni.app.versions`): when the run starts, and again
  only if any of it changes. A **config** line under it spells out the settings its digest stands for,
  whenever that digest is new.
- A **refresh** line is written when the source's health, the number of games held in the TV delay, or
  the games the schedule calls final that their feed doesn't yet change, and whenever a refresh drops a
  game or a play, misses a card, reveals a final or corrects one. Each one names the next refresh due by
  itself. Under it, a card that left without airing is named with why (`unseen`), a final with when its
  game ended and what that rests on, which orders the finals, a final whose result a later answer changed
  (`corrected`), and each game whose result waits for its feed to say final (`unconfirmed`).
- An **up** line is written at a card's first committed frame, with why the queue chose it and how its
  teams were drawn: logo variant, tile or color bar, meter color, and the contrast policy that chose
  them. A **frame** line is written when an explicit frame goes up, with why.
- A **down** line says why a card came down and how long it was up; a **drop** line names a card that
  came down before it ever drew. A **stale** line marks a card going up, or turning, stale. A **note**
  line is something else the board decided, such as finding no base card for the turn it owes one.
- A paint failure is written once when it starts, and once when frames commit again.

Under `omni-board-policy/2` the board's decisions (`omni.tower.decisions`) are written too, one line each, with
the policy's factors as fields and the "why this card" sentence last:

    base 19:05:12 omni-board-policy/2: mlb:mlb_statsapi:746:live (weighted) | G 0.620 proxy F 4.00 T 1.000 Q 0.500 | ...
    promotion 19:05:30 omni-board-policy/2: mlb:mlb_statsapi:747:bigplay:9 (major) | I 1.200 late winner on model | ...
    lease 19:05:18: opened mlb:mlb_statsapi:746 mlb mound visit @ m1 | 36.0s left | 1 guest

- A **base** line is a base turn: its game's ``G`` (and what it rests on), ``F``, ``T`` and ``Q``; its target
  share of all base airtime, its league's, and its share within the league; its actual share so far; its debt
  and its league's; and any rescue, or the favorites' break it fills. A **clamp** line is a debt held at the
  limit.
- A **promotion** line is a promoted card's confirmed start: its ``I``, the bar and its context, the hold's
  deadline, and the cooldown it starts. A **wait** line is why a candidate didn't start, written when that
  changes; an **unspent** line is a cued promotion rejected at its first frame.
- A **lease** line is a favorite's break lease opened, revoked, ended, lapsed or refused; a **guest** line is a
  card admitted into a break or refused, and a **yield** line one giving the board back when the break closed.
- A **card** line is an ordinary card's turn, and a **cap** line the non-base cap binding.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, tzinfo

from omni.app.loop import Painted, Presentation, Refresh
from omni.app.versions import Versions
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.colors import RGBColor
from omni.policy.ledger import DebtClamp
from omni.renderers.visual_treatment import MarkTreatment
from omni.tower.board import BoardFrame, BoardPayload
from omni.tower.decisions import (
    BaseTurnNote,
    CapNote,
    CardNote,
    ClampNote,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    PolicyDecision,
    PromotionNote,
    UnspentNote,
    WaitNote,
    YieldNote,
    event_words,
)
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
        self._versions: Versions | None = None

    def versions(self, versions: Versions) -> None:
        """Write what the run decides with: the first time, and after that only when any of it changed."""
        if versions == self._versions:
            return
        last, self._versions = self._versions, versions
        self._log(versions.line())
        if last is None or last.config.digest != versions.config.digest:
            self._log(versions.config.line())

    def refreshed(self, refresh: Refresh, *, took: timedelta, next_at: datetime | None) -> None:
        health = refresh.health
        pipeline = refresh.pipeline
        key = (health.source, health.reason, health.held, health.unfed, pipeline.unconfirmed)
        dropped = pipeline.skipped + pipeline.warnings
        news = dropped or refresh.unseen or pipeline.ends or pipeline.corrections
        if key == self._last_refresh and not news:
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
        for missed in refresh.unseen:
            self._log(f"  unseen: {missed.card.id.raw} ({missed.reason.value})")
        for card, ended in pipeline.ends:
            self._log(f"  final: {card.raw} ended {self._clock(ended.at)} ({ended.basis.value})")
        for card in pipeline.corrections:
            self._log(f"  corrected: {card.raw}")
        for game in pipeline.unconfirmed:
            self._log(f"  unconfirmed: {game} (final on the schedule; its feed hasn't said so)")

    def painted(self, painted: Painted) -> None:
        for change in painted.changes:
            self._changed(change, painted)
        for note in painted.notes:
            self._log(f"note {self._clock(painted.at)}: {note.value}")
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

    def decided(self, decisions: Iterable[PolicyDecision]) -> None:
        """Write what the board's policy decided, one line each."""
        for decision in decisions:
            self._log(self._decision(decision))

    def _decision(self, note: PolicyDecision) -> str:
        if isinstance(note, BaseTurnNote):
            return self._base(note)
        if isinstance(note, PromotionNote):
            return self._promotion(note)
        when = self._clock(note.at.utc)
        if isinstance(note, CardNote):
            return f"card {when} {note.policy.value}: {note.card.raw} ({note.role.value}){_guest(note.hosts)} | why: {note.why}"
        if isinstance(note, WaitNote):
            bar = (
                f" | bar {note.bar:.2f} {note.context.value}"
                if note.bar is not None and note.context is not None
                else ""
            )
            return f"wait {when}: {note.key} r{note.revision} ({note.reason.value}){bar}"
        if isinstance(note, UnspentNote):
            return f"unspent {when}: {note.card.raw} ({note.reason.value})"
        if isinstance(note, LeaseNote):
            return self._lease(when, note)
        if isinstance(note, GuestNote):
            hosts = ", ".join(str(host) for host in note.hosts)
            if note.refusal is None:
                return f"guest {when}: admitted {note.card.raw} in {hosts}"
            return f"guest {when}: refused {note.card.raw} in {hosts} ({note.refusal.value})"
        if isinstance(note, YieldNote):
            return f"yield {when}: {note.card.raw} (its host's break closed {self._at(note.at, note.closed)})"
        if isinstance(note, CapNote):
            return f"cap {when}: the non-base cap is reached; refused {', '.join(rung.name.lower() for rung in note.refused)}"
        return f"clamp {when}: " + "; ".join(_clamp(clamp) for clamp in note.clamps)

    def _base(self, note: BaseTurnNote) -> str:
        weight = note.weight
        reason = note.reason.value + (", again" if note.continues else "")
        actual = f"{note.actual:.2%}" if note.actual is not None else "-"
        line = (
            f"base {self._clock(note.at.utc)} {note.policy.value}: {note.card.raw} ({reason})"
            f" | G {weight.g:.3f} {weight.status.value} F {weight.f:.2f} T {weight.t:.3f} Q {weight.q:.3f}"
            f" | target {note.target:.2%} (league {note.league_target:.2%}, {note.within_league:.2%} within)"
            f" | actual {actual} | debt {note.debt:+.2f}s (league {note.league_debt:+.2f}s)"
        )
        if note.rescue is not None:
            line += f" | rescue {note.rescue.misses}/{note.rescue.threshold} misses"
        return line + _guest(note.hosts) + f" | why: {note.why}"

    def _promotion(self, note: PromotionNote) -> str:
        value = note.value
        worth = (
            "I -" if value is None else f"I {value.value:.3f} {event_words(note.kind, value)} on {value.basis.value}"
        )
        hold = f" | hold until {self._at(note.at, note.hold)}" if note.hold is not None else ""
        return (
            f"promotion {self._clock(note.at.utc)} {note.policy.value}: {note.card.raw} ({note.kind.value})"
            f" | {worth} | bar {note.bar:.2f} {note.context.value}{hold}"
            f" | cooldown until {self._at(note.at, note.cooldown_until)}{_guest(note.hosts)} | why: {note.why}"
        )

    def _lease(self, when: str, note: LeaseNote) -> str:
        line = f"lease {when}: {note.event.value} {note.id}"
        if note.event is LeaseEvent.REFUSED and note.refusal is not None:
            return line + f" ({note.refusal.value})"
        if note.event is not LeaseEvent.OPENED:
            return line
        left = f"{note.remaining.total_seconds():.1f}s left" if note.remaining is not None else "open-ended"
        guests = (
            "any guests that fit" if note.turns is None else f"{note.turns} guest" + ("s" if note.turns > 1 else "")
        )
        return line + f" | {left} | {guests}" + (" | provisional" if note.provisional else "")

    def _at(self, reading: ClockReading, instant: MonotonicTime) -> str:
        return self._clock(reading.project(instant))

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


def _guest(hosts: tuple[object, ...]) -> str:
    return f" | guest of {', '.join(str(host) for host in hosts)}" if hosts else ""


def _clamp(clamp: DebtClamp) -> str:
    whose = str(clamp.contest) if clamp.contest is not None else f"{clamp.league.value} league"
    return f"{whose} debt {clamp.debt:+.2f}s held at {clamp.limit:+.2f}s"


def _mark(mark: MarkTreatment) -> str:
    drawn = "tile" if mark.is_tile else f"bar {_hex(mark.bar_color)}"
    return f"{mark.variant.value} {drawn} meter {_hex(mark.meter_color)}"


def _hex(color: RGBColor) -> str:
    return f"#{color.r:02X}{color.g:02X}{color.b:02X}"
