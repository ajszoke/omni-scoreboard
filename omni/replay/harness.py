"""Replay harness: run the real AppLoop over a Timeline under a FakeClock.

This is the deterministic time-series replayer. It swaps only the *clock* and the
*data source*: a `ReplayProvider` serves the timeline's schedule as of ``now`` and a
feed fetcher serves each game's state, plays and decisions, then `build_loop` wires the
exact production spine (supervisor -> store -> pipeline -> queue -> board -> registry ->
sink). During a timeline's outage both fail, as a network outage would.

The loop runs a refresh and a paint at every instant something can change: a frame of the
timeline landing, an outage starting or ending, or a deadline the loop itself names (a card's
minimum or turn, a play clearing the delay, the source going stale). Stepped only there, it
decides as it would at any finer pace. `replay()` samples what was on the panel every
``tick``; `replay_decisions()` lists every change, with its reason. Identical timeline +
parameters always yield an identical trace, so a fixture timeline proves entry, fairness,
delay, and removal without real time or network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from typing import Any

from omni.app.display import RecordingDisplaySink
from omni.app.loop import Painted, Refresh
from omni.app.runner import build_loop
from omni.app.supervisor import BackoffPolicy, ProviderStatus
from omni.core.clock import FakeClock
from omni.core.enum import League, PanelProfile
from omni.core.ids import SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.cards.base import ScoreboardCard
from omni.events.baseball import LiveBaseballFeed
from omni.panels.geometry import geometry_for
from omni.providers.base import ProviderError, ProviderUpdate
from omni.queue.scheduler import SelectionReason
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.replay.timeline import Timeline
from omni.tower.board import BoardFrame, BoardPayload, BoardPolicy, FrameKind
from omni.tower.lane import ChangeKind, LaneChange

__all__ = ["TraceEntry", "ReplayProvider", "replay", "replay_decisions"]

_REPLAY_SOURCE = SourceRef("replay", "fixture://timeline")
_NUDGE = timedelta(milliseconds=1)  # past a deadline, so spans measured in floating point have surely run
# The schedule is asked at every step, as the running board's plan asks it on its own cadence, with no
# backoff on top: a source back from an outage answers the next ask.
_NO_BACKOFF = BackoffPolicy(base=DurationSeconds(0), cap=DurationSeconds(0))

# What a replay hears at each step: when, what the refresh found, and what the paint did.
_Observer = Callable[[datetime, Refresh, Painted], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class TraceEntry:
    """What the loop showed at one sample (deterministic for a given timeline + params)."""

    at: datetime
    shown: str | None  # the committed card's id; None while an explicit frame is up
    provider_status: ProviderStatus
    frame: FrameKind | None = None  # the explicit frame up, when no card is
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
    policy: BoardPolicy = BoardPolicy(),
) -> list[TraceEntry]:
    """Drive the real `AppLoop` across ``timeline`` and sample what was on the panel every ``tick``.

    Runs from ``timeline.start`` to ``until`` (default ``timeline.end``). ``broadcast_lag``
    exercises the TV-delay path end to end; ``favorites`` feeds the scorer. Pure and
    deterministic — no real time, no I/O.
    """
    if tick.value <= 0:
        raise ValueError("tick must be a positive duration")
    every = tick.as_timedelta()
    trace: list[TraceEntry] = []

    def sample(at: datetime, refresh: Refresh, painted: Painted) -> None:
        if (at - timeline.start) % every:
            return  # a step between samples
        trace.append(
            TraceEntry(
                at=at,
                shown=painted.shown.raw if painted.shown is not None else None,
                provider_status=refresh.provider_status,
                frame=painted.frame,
                reason=painted.reason,
            )
        )

    _drive(timeline, profile, until, broadcast_lag, favorites, policy, sample, every=every, draw=True)
    return trace


def replay_decisions(
    timeline: Timeline,
    *,
    profile: PanelProfile,
    until: datetime | None = None,
    broadcast_lag: DurationSeconds = DurationSeconds(0),
    favorites: frozenset[str] = frozenset(),
    policy: BoardPolicy = BoardPolicy(),
) -> list[str]:
    """Every change on the board across ``timeline``, one line each, timed in seconds from its start.

    ``up`` names a card that went up and why the queue chose it; ``down`` one that came down, why,
    and how long it was up; ``drop`` one that came down before it drew; ``frame`` an explicit frame
    and why; ``stale`` a card on air whose data passed its age; ``unseen`` one that expired before it
    ever aired; ``source`` a change in how far the board trusts its source.
    """
    lines: list[str] = []
    seen: dict[str, object] = {"source": None, "stale": None}

    def observe(at: datetime, refresh: Refresh, painted: Painted) -> None:
        seconds = f"{(at - timeline.start).total_seconds():8.1f}"
        source = (refresh.health.source, refresh.health.reason)
        if source != seen["source"]:
            seen["source"] = source
            reason = f"  ({refresh.health.reason})" if refresh.health.reason else ""
            lines.append(f"{seconds}  source  {refresh.health.source.value}{reason}")
        lines.extend(f"{seconds}  unseen  {card.raw}  (expired before it aired)" for card in refresh.unseen)
        chosen = painted.presented.reason if painted.presented is not None else None
        lines.extend(
            f"{seconds}  {line}" for line in map(lambda change: _describe(change, chosen), painted.changes) if line
        )
        stale = painted.shown.raw if painted.shown is not None and painted.stale else None
        if stale is not None and stale != seen["stale"]:
            lines.append(f"{seconds}  stale   {stale}")
        seen["stale"] = stale

    _drive(timeline, profile, until, broadcast_lag, favorites, policy, observe, every=None, draw=False)
    return lines


def _describe(change: LaneChange[BoardPayload], chosen: SelectionReason | None) -> str | None:
    """One change on the board as a decision line (without its time); None for a frame coming down."""
    payload = change.segment.frames[0]
    why = change.reason.value if change.reason is not None else "-"
    if isinstance(payload, BoardFrame):
        return f"frame   {payload.kind.value}  ({payload.reason})" if change.kind is ChangeKind.UP else None
    if change.kind is ChangeKind.UP:
        return f"up      {payload.id.raw}  ({chosen.value if chosen is not None else '-'})"
    if change.kind is ChangeKind.DOWN:
        aired = change.aired.total_seconds() if change.aired is not None else 0.0
        return f"down    {payload.id.raw}  ({why}, {aired:.1f}s)"
    return f"drop    {payload.id.raw}  ({why})"


def _drive(
    timeline: Timeline,
    profile: PanelProfile,
    until: datetime | None,
    broadcast_lag: DurationSeconds,
    favorites: frozenset[str],
    policy: BoardPolicy,
    observe: _Observer,
    *,
    every: timedelta | None,
    draw: bool,
) -> None:
    """Step the loop through ``timeline`` at every instant something can change, and every ``every``."""
    stop = until if until is not None else timeline.end
    if stop < timeline.start:
        raise ValueError("until cannot be before the timeline start")

    def fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        frame = timeline.frame_at(game.id, now)
        if timeline.down_at(now):
            raise ProviderError("replay outage")
        if frame is None or frame.state is None:
            raise ProviderError(f"no replay state for {game.id.raw}")
        # A feed's answer is dated by the frame it carries, or by the end of an outage since: the first answer
        # after one is new, however old the state in it.
        recovered = max((outage.end for outage in timeline.outages if outage.end <= now), default=frame.at)
        return LiveBaseballFeed(
            state=frame.state, events=frame.events, decisions=frame.decisions, received_at=max(frame.at, recovered)
        )

    loop = build_loop(
        ReplayProvider(timeline),
        fetch_feed,
        _Discarding(profile) if not draw else RecordingDisplaySink(profile),
        favorites=favorites,
        broadcast_lag=broadcast_lag,
        backoff=_NO_BACKOFF,
        policy=policy,
        registry=None if draw else _Undrawn(),
    )
    clock = FakeClock(timeline.start)
    changes = [instant for instant in timeline.changes if instant > timeline.start]
    sample_at = timeline.start
    while clock.now() <= stop:
        at = clock.read()
        refresh = loop.refresh(at.utc)
        observe(at.utc, refresh, loop.paint(at))

        while changes and changes[0] <= at.utc:
            changes.pop(0)
        candidates = changes[:1]
        deadline = loop.next_deadline(clock.read())
        if deadline is not None:
            candidates.append(at.utc + deadline.since(at.monotonic) + _NUDGE)
        if every is not None:
            while sample_at <= at.utc:
                sample_at += every
            candidates.append(sample_at)
        # With nothing left that could change, the replay is done: step past its end.
        clock.advance(max(min(candidates, default=stop + _NUDGE) - at.utc, _NUDGE))


class _Undrawn(RendererRegistry):
    """Today's renderers, chosen and checked as for drawing, but not drawn: a decision list needs no pixels."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self._real.renderer_for(card)


class _Discarding:
    """A display that takes every frame and keeps none."""

    def __init__(self, profile: PanelProfile) -> None:
        self._profile = profile
        geometry = geometry_for(profile)
        self._canvas = RecordingCanvas(geometry.width, geometry.height)

    @property
    def profile(self) -> PanelProfile:
        return self._profile

    def new_frame(self) -> Canvas:
        return self._canvas

    def commit(self, frame: Canvas) -> None:
        self._canvas.ops.clear()
