"""Replay harness: run the real AppLoop over a Timeline under a FakeClock.

This is the deterministic time-series replayer. It swaps only the *clock* and the
*data source*: a
`ReplayProvider` serves the timeline's schedule as of ``now`` and a state-fetcher
serves each live game's state, then `build_loop` wires the exact production spine
(supervisor -> store -> pipeline -> queue -> registry -> sink). `replay()` ticks
`run_once(now)` from the timeline's start to ``until`` and records what was shown
each tick. Identical timeline + parameters always yield an identical trace, so a
fixture timeline proves entry, fairness, delay, and removal without real time or
network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.app.clock import FakeClock
from omni.app.display import RecordingDisplaySink
from omni.app.runner import build_loop
from omni.app.supervisor import ProviderStatus
from omni.core.enum import League, PanelProfile
from omni.core.ids import SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from omni.replay.timeline import Timeline

__all__ = ["TraceEntry", "ReplayProvider", "replay"]

_REPLAY_SOURCE = SourceRef("replay", "fixture://timeline")


@dataclass(frozen=True, slots=True, kw_only=True)
class TraceEntry:
    """What the loop showed at one tick (deterministic for a given timeline + params)."""

    at: datetime
    shown: str | None  # the committed card's id, or None when nothing was eligible
    provider_status: ProviderStatus


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
        state = timeline.state_at(game.id, now)
        if state is None:  # pragma: no cover - Timeline guarantees LIVE frames carry state
            raise ProviderError(f"no replay state for live game {game.id.raw}")
        return LiveBaseballFeed(state=state)

    sink = RecordingDisplaySink(profile)
    loop = build_loop(ReplayProvider(timeline), fetch_feed, sink, favorites=favorites, broadcast_lag=broadcast_lag)

    trace: list[TraceEntry] = []
    while clock.now() <= stop:
        now = clock.now()
        result = loop.run_once(now)
        trace.append(
            TraceEntry(
                at=now,
                shown=result.shown.raw if result.shown is not None else None,
                provider_status=result.provider_status,
            )
        )
        clock.advance(tick)
    return trace
