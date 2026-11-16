"""Replay harness: run the real AppLoop over a Timeline under a FakeClock.

This is the deterministic time-series replayer. It swaps only the *clock* and the
*data source*: a `ReplayProvider` serves the timeline's schedule as of ``now`` and a
feed fetcher serves each game's state, plays and decisions, then `build_loop` wires the
exact production spine (supervisor -> store -> pipeline -> queue -> registry -> sink).
During a timeline's outage both fail, as a network outage would, and a game's feed answer is
dated by the frame it came from. `replay()` ticks
a decision and a paint from the timeline's start to ``until`` and records what was shown
each tick, and `decisions` lists what changed. Identical timeline + parameters always
yield an identical trace, so a fixture timeline proves entry, fairness, delay, and
removal without real time or network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.app.display import RecordingDisplaySink
from omni.app.runner import build_loop
from omni.app.supervisor import BackoffPolicy, ProviderStatus
from omni.core.clock import FakeClock
from omni.core.enum import League, PanelProfile
from omni.core.ids import SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from omni.queue.scheduler import SelectionReason
from omni.replay.timeline import Timeline

__all__ = ["TraceEntry", "ReplayProvider", "decisions", "replay"]

_REPLAY_SOURCE = SourceRef("replay", "fixture://timeline")
# The schedule is asked every tick, as the running board's plan asks it on its own cadence, with no
# backoff on top: a source back from an outage answers the next ask.
_NO_BACKOFF = BackoffPolicy(base=DurationSeconds(0), cap=DurationSeconds(0))


@dataclass(frozen=True, slots=True, kw_only=True)
class TraceEntry:
    """What the loop showed at one tick (deterministic for a given timeline + params)."""

    at: datetime
    shown: str | None  # the committed card's id, or None when nothing was eligible
    provider_status: ProviderStatus
    reason: SelectionReason | None = None  # why the shown card was chosen


class ReplayProvider:
    """A `Provider` that serves a `Timeline`'s schedule as of ``now`` — no network."""

    def __init__(self, timeline: Timeline, *, source: SourceRef = _REPLAY_SOURCE, league: League = League.MLB) -> None:
        self._timeline = timeline
        self._source = source
        self._league = league

    @property
    def source(self) -> SourceRef:
        return self._source

    @property
    def league(self) -> League:
        return self._league

    def refresh(self, now: datetime) -> ProviderUpdate:
        if self._timeline.down_at(now):
            raise ProviderError("replay outage")
        return ProviderUpdate(source=self._source, observed_at=now, contests=self._timeline.schedule_at(now))


def replay(
    timeline: Timeline,
    *,
    profile: PanelProfile,
    tick: DurationSeconds,
    until: datetime | None = None,
    broadcast_lag: DurationSeconds = DurationSeconds(0),
    favorites: frozenset[str] = frozenset(),
) -> list[TraceEntry]:
    """Drive the real `AppLoop` across ``timeline`` and return the per-tick trace.

    Runs from ``timeline.start`` to ``until`` (default ``timeline.end``) in ``tick``
    steps. ``broadcast_lag`` exercises the TV-delay path end to end; ``favorites``
    feeds the scorer. Pure and deterministic — no real time, no I/O.
    """
    if tick.value <= 0:
        raise ValueError("tick must be a positive duration")
    stop = until if until is not None else timeline.end
    if stop < timeline.start:
        raise ValueError("until cannot be before the timeline start")

    clock = FakeClock(timeline.start)

    def fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        frame = timeline.frame_at(game.id, now)
        if timeline.down_at(now):
            raise ProviderError("replay outage")
        if frame is None or frame.state is None:
            raise ProviderError(f"no replay state for {game.id.raw}")
        return LiveBaseballFeed(state=frame.state, events=frame.events, decisions=frame.decisions, received_at=frame.at)

    sink = RecordingDisplaySink(profile)
    loop = build_loop(
        ReplayProvider(timeline),
        fetch_feed,
        sink,
        favorites=favorites,
        broadcast_lag=broadcast_lag,
        backoff=_NO_BACKOFF,
    )

    trace: list[TraceEntry] = []
    while clock.now() <= stop:
        now = clock.now()
        decision = loop.decide(now)
        painted = loop.paint(now)
        pick = decision.pick
        trace.append(
            TraceEntry(
                at=now,
                shown=painted.shown.raw if painted.shown is not None else None,
                provider_status=decision.provider_status,
                reason=pick.reason if pick is not None and painted.shown is not None else None,
            )
        )
        clock.advance(tick)
    return trace


def decisions(trace: list[TraceEntry], *, start: datetime) -> list[str]:
    """What changed across a trace, one line each, timed in seconds from ``start``.

    An ``up`` line names a card that went up and why it was chosen; a ``none`` line, a moment
    with nothing eligible; a ``source`` line, a change in the schedule provider's status.
    """
    lines: list[str] = []
    shown: tuple[str | None, SelectionReason | None] | None = None
    status: ProviderStatus | None = None
    for entry in trace:
        seconds = (entry.at - start).total_seconds()
        if entry.provider_status is not status:
            status = entry.provider_status
            lines.append(f"{seconds:8.1f}  source  {status.value}")
        if (entry.shown, entry.reason) == shown:
            continue
        shown = (entry.shown, entry.reason)
        if entry.shown is None:
            lines.append(f"{seconds:8.1f}  none    nothing eligible")
        else:
            reason = entry.reason.value if entry.reason is not None else "-"
            lines.append(f"{seconds:8.1f}  up      {entry.shown}  ({reason})")
    return lines
