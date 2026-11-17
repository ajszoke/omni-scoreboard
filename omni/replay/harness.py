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
``tick``; `replay_decisions()` lists every change, with its reason, under a header naming what the
replay decided with (`replay_versions`). Identical timeline + parameters always yield an identical
trace, so a fixture timeline proves entry, fairness, delay, and removal without real time or network.

A feed's answer arrives when the replay first reads its frame, which is the frame's own instant
(the replay steps there), or the end of an outage since: the first answer after one is new,
however old the state in it. The loop gets the whole clock reading, so its delays run on the
replay's monotonic clock. A `Seek` jumps the replay's wall clock to another point in the
timeline, as a clock correction would: monotonic time runs on, what was held keeps its release,
and each frame read after the seek arrives then.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from typing import Any

from omni.app.display import RecordingDisplaySink
from omni.app.loop import Painted, Refresh
from omni.app.runner import build_loop
from omni.app.contest_store import ProviderStatus
from omni.app.supervisor import BackoffPolicy
from omni.app.versions import DecisionConfig, PolicyName, Versions
from omni.core.clock import ClockReading, FakeClock
from omni.core.enum import League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.rules import RULES
from omni.cards.base import ScoreboardCard
from omni.events.baseball import LiveBaseballFeed
from omni.panels.geometry import geometry_for
from omni.preferences.favorites import Favorites
from omni.providers.base import ProviderError, ProviderUpdate
from omni.queue.scheduler import SelectionReason
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.renderers.text_lane import ScrollSpeed
from omni.replay.timeline import Timeline
from omni.tower.board import BoardFrame, BoardPayload, BoardPolicy, FrameKind
from omni.tower.lane import ChangeKind, LaneChange

__all__ = ["TraceEntry", "ReplayProvider", "Seek", "replay", "replay_decisions", "replay_versions"]

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


@dataclass(frozen=True, slots=True)
class Seek:
    """At ``at`` on the replay's wall clock, jump it to ``to``; monotonic time doesn't move."""

    at: datetime
    to: datetime


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
    favorites: Favorites = Favorites(),
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
    favorites: Favorites = Favorites(),
    policy: BoardPolicy = BoardPolicy(),
    seek: Seek | None = None,
) -> list[str]:
    """Every change on the board across ``timeline``, one line each, timed in seconds from its start.

    The first line is the replay's header: its policy, config digest, rules and models
    (`replay_versions`). Then ``up`` names a card that went up and why the queue chose it; ``down`` one that came down, why,
    and how long it was up; ``drop`` one that came down before it drew; ``frame`` an explicit frame
    and why; ``stale`` a card on air whose data passed its age; ``unseen`` one that left before it
    ever aired, and why; ``final`` a final revealed, with when its game ended and what that rests on;
    ``note`` something else the board decided; ``source`` a change in how far the board trusts its
    source. With ``seek``, the replay's wall clock jumps once, and the times follow it.
    """
    versions = replay_versions(profile, broadcast_lag=broadcast_lag, favorites=favorites, policy=policy)
    lines: list[str] = [versions.header()]
    seen: dict[str, object] = {"source": None, "stale": None}

    def observe(at: datetime, refresh: Refresh, painted: Painted) -> None:
        seconds = f"{(at - timeline.start).total_seconds():8.1f}"
        source = (refresh.health.source, refresh.health.reason)
        if source != seen["source"]:
            seen["source"] = source
            reason = f"  ({refresh.health.reason})" if refresh.health.reason else ""
            lines.append(f"{seconds}  source  {refresh.health.source.value}{reason}")
        lines.extend(f"{seconds}  unseen  {missed.card.id.raw}  ({missed.reason.value})" for missed in refresh.unseen)
        for card, ended in refresh.pipeline.ends:
            since = (ended.at - timeline.start).total_seconds()
            lines.append(f"{seconds}  final   {card.raw}  (ended {since:.1f}, {ended.basis.value})")
        chosen = painted.presented.reason if painted.presented is not None else None
        lines.extend(
            f"{seconds}  {line}" for line in map(lambda change: _describe(change, chosen), painted.changes) if line
        )
        lines.extend(f"{seconds}  note    {note.value}" for note in painted.notes)
        stale = painted.shown.raw if painted.shown is not None and painted.stale else None
        if stale is not None and stale != seen["stale"]:
            lines.append(f"{seconds}  stale   {stale}")
        seen["stale"] = stale

    _drive(timeline, profile, until, broadcast_lag, favorites, policy, observe, every=None, draw=False, seek=seek)
    return lines


def replay_versions(
    profile: PanelProfile,
    *,
    broadcast_lag: DurationSeconds = DurationSeconds(0),
    favorites: Favorites = Favorites(),
    policy: BoardPolicy = BoardPolicy(),
) -> Versions:
    """What a replay of the shared board decides with. A replay wires no win meter, no odds and no clock
    zone: the meter and odds are off, and its marquees scroll at the default speed."""
    return Versions(
        policy=PolicyName.BOARD_V1,
        config=DecisionConfig(
            profile=profile,
            delay=broadcast_lag.as_timedelta(),
            favorites=favorites,
            turn=policy.slot,
            takeover_cap=policy.takeover_cap,
            meter=False,
            odds=False,
            scroll_speed=ScrollSpeed.SLOW,
        ),
        rules=RULES,
    )


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
    favorites: Favorites,
    policy: BoardPolicy,
    observe: _Observer,
    *,
    every: timedelta | None,
    draw: bool,
    seek: Seek | None = None,
) -> None:
    """Step the loop through ``timeline`` at every instant something can change, and every ``every``."""
    stop = until if until is not None else timeline.end
    if stop < timeline.start:
        raise ValueError("until cannot be before the timeline start")

    clock = FakeClock(timeline.start)
    arrivals: dict[tuple[LeagueScopedId, datetime], ClockReading] = {}  # when each frame's answer first arrived

    def fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
        frame = timeline.frame_at(game.id, now)
        if timeline.down_at(now):
            arrivals.clear()  # the first answer after the outage is new, however old the state in it
            raise ProviderError("replay outage")
        if frame is None or frame.state is None:
            raise ProviderError(f"no replay state for {game.id}")
        return LiveBaseballFeed(
            state=frame.state,
            events=frame.events,
            decisions=frame.decisions,
            status=frame.game.status,  # the feed says what the frame says
            received=arrivals.setdefault((game.id, frame.at), clock.read()),
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
    changes = [instant for instant in timeline.changes if instant > timeline.start]
    sample_at = timeline.start
    while clock.now() <= stop:
        if seek is not None and clock.now() >= seek.at:
            clock.set(seek.to)
            arrivals.clear()  # whatever is read from here on arrives now
            changes = [instant for instant in timeline.changes if instant > seek.to]
            seek = None
        at = clock.read()
        refresh = loop.refresh(at)
        observe(at.utc, refresh, loop.paint(at))

        while changes and changes[0] <= at.utc:
            changes.pop(0)
        candidates = changes[:1] + ([seek.at] if seek is not None else [])
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
