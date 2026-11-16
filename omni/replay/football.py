"""Football replay: a game's feed answers, as a board received them, run back through a board.

A *football trace* is a board's input written down: each answer of its feed (a `FeedResult`), with
when it arrived, the game's state and the strip entries it brought. The format is JSON lines: a
header naming the game and the trace's start, then an answer a line, its times in seconds since
that start. `header_record` and `answer_record` write the lines, and `load_trace` reads them back.

`ReplayFeed` hands a trace's answers to a board as a clock passes their arrival. `replay_board` runs
a board over a whole trace under a fake clock, stepping at a frame rate, and returns its decisions,
a line each:
- the strip's entry and frame, or a blank strip;
- the field it draws (the score, the period, and the next snap or the break);
- each entry dropped unseen, and why.
Two boards that decide alike on a trace give the same lines, so a change to the board is checked on
them. The clock alone, the meter and the pixels are not decisions.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from omni.app.clock import FakeClock
from omni.app.football_board import FootballBoard
from omni.app.football_feed import FeedResult
from omni.cards.base import ScoreboardCard
from omni.cards.football import LiveFootballCardPayload
from omni.core.enum import GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.football import (
    FootballGameState,
    FootballInterval,
    PlaySummary,
    PlayTone,
    SetPiece,
    TeamChallenges,
    TeamTimeouts,
)
from omni.events.football import FootballEvent, FootballEventKind
from omni.panels.geometry import geometry_for
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import FeedNote, FeedNoteKind, NflLiveSnapshot, NflPhase
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext

__all__ = [
    "REPLAY_SOURCE",
    "TRACE_VERSION",
    "FootballTrace",
    "ReplayFeed",
    "answer_record",
    "header_record",
    "load_trace",
    "replay_board",
]

TRACE_VERSION = 1
REPLAY_SOURCE = SourceRef("replay", "fixture://football-trace")
_STATUS = {NflPhase.PRE: GameStatus.PREGAME, NflPhase.IN: GameStatus.LIVE, NflPhase.POST: GameStatus.FINAL}
_NUDGE = timedelta(milliseconds=1)  # a step on its deadline is due; the next is always a little later


@dataclass(frozen=True, slots=True)
class FootballTrace:
    """One game's feed answers, oldest first, and the instant the trace's times count from."""

    start: datetime
    game: TeamGame
    answers: tuple[FeedResult, ...]


def header_record(game: TeamGame, *, name: str, start: datetime) -> dict[str, Any]:
    """A trace's first line: which game, and the instant its times count from."""
    return {
        "football_trace": TRACE_VERSION,
        "game": name,
        "start": _iso(start),
        "kickoff": _iso(game.scheduled_start),
        "away": game.away.abbreviation,
        "home": game.home.abbreviation,
    }


def answer_record(result: FeedResult, start: datetime) -> dict[str, Any]:
    """One answer as a trace line. Defaults (no events, not held, ...) are left out."""
    record: dict[str, Any] = {"t": _seconds(result.received_at - start)}
    if result.asked_at != result.received_at:
        record["asked"] = _seconds(result.asked_at - start)
    record["next"] = _seconds(result.next_ask - result.asked_at)
    if result.held:
        record["held"] = True
    if result.holding:
        record["holding"] = True
    if result.snapshot is None:
        record["error"] = result.error or "no answer"
        return record
    snapshot = result.snapshot
    shot: dict[str, Any] = {"phase": snapshot.phase.value, "poll": snapshot.poll_every.value}
    if snapshot.state is not None:
        shot["state"] = _state_record(snapshot.state)
    if snapshot.caught_up:
        shot["caught_up"] = True
    if snapshot.settled:
        shot["settled"] = True
    if snapshot.break_ends_at is not None:
        shot["break_ends"] = _seconds(snapshot.break_ends_at - start)
    if snapshot.observed_at is not None:
        shot["as_of"] = _seconds(snapshot.observed_at - start)
    if snapshot.events:
        shot["events"] = [_event_record(event) for event in snapshot.events]
    if snapshot.notes:
        shot["notes"] = [{"kind": note.kind.value, "detail": note.detail} for note in snapshot.notes]
    record["snapshot"] = shot
    return record


def load_trace(lines: Iterable[str], *, registry: NflTeamRegistry | None = None) -> FootballTrace:
    """A trace from its lines (a header, then answers); raises ValueError for anything else."""
    records = [json.loads(line) for line in lines if line.strip()]
    if not records or records[0].get("football_trace") != TRACE_VERSION:
        raise ValueError(f"not a football trace (version {TRACE_VERSION})")
    header = records[0]
    teams = registry or NflTeamRegistry.default(source=REPLAY_SOURCE)
    start = _instant(header["start"])
    game = TeamGame(
        id=LeagueScopedId(League.NFL, REPLAY_SOURCE, str(header["game"])),
        league=League.NFL,
        status=GameStatus.PREGAME,
        scheduled_start=_instant(header["kickoff"]),
        away=teams.resolve(header["away"]),
        home=teams.resolve(header["home"]),
    )
    return FootballTrace(start=start, game=game, answers=tuple(_answer(record, start, game) for record in records[1:]))


class ReplayFeed:
    """A `FeedSource` that hands over a trace's answers as the clock passes their arrival."""

    def __init__(self, answers: Sequence[FeedResult]) -> None:
        self._answers = answers
        self._next = 0

    def collect(self, now: datetime) -> list[FeedResult]:
        first = self._next
        while self._next < len(self._answers) and self._answers[self._next].received_at <= now:
            self._next += 1
        return list(self._answers[first : self._next])

    def next_arrival(self) -> datetime | None:
        """When the next answer arrives, if any is left."""
        return self._answers[self._next].received_at if self._next < len(self._answers) else None


def replay_board(
    trace: FootballTrace,
    *,
    fps: float | None = 4.0,
    delay: timedelta = timedelta(0),
    tail: timedelta = timedelta(minutes=2),
    profile: PanelProfile = PanelProfile.QUAD_128X64,
) -> list[str]:
    """Run a board over ``trace`` from its start to ``tail`` past its last answer; its decisions.

    With ``fps`` the board steps at that frame rate, as it does live. With None it steps only when
    something is due: the next answer's arrival, or the board's own next deadline.
    """
    clock = FakeClock(trace.start)
    cards = _Cards(profile)
    logs: list[str] = []
    feed = ReplayFeed(trace.answers)
    board = FootballBoard(feed, _NullSink(profile), delay=delay, renderer=cards, log=logs.append)
    decisions = _Decisions(trace.start)
    end = (trace.answers[-1].received_at if trace.answers else trace.start) + delay + tail
    while clock.now() <= end:
        at = clock.read()
        board.step(at)
        decisions.take(at.utc, cards.take(), logs)
        logs.clear()
        if fps is not None:
            clock.advance(timedelta(seconds=1 / fps))
            continue
        deadline, arrival = board.next_deadline(at), feed.next_arrival()
        waits = [deadline.since(at.monotonic)] if deadline is not None else []
        waits += [arrival - at.utc] if arrival is not None else []
        if not waits:
            break
        clock.advance(max(min(waits), _NUDGE))
    return decisions.lines


class _Decisions:
    """Turns what a board drew and logged, step by step, into its decisions."""

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._strip: str | None = None
        self._field: str | None = None
        self.lines: list[str] = []

    def take(self, now: datetime, card: ScoreboardCard[LiveFootballCardPayload] | None, logs: list[str]) -> None:
        for line in logs:
            if line.startswith("  - "):
                self._add(now, "drop", line[4:])
        if card is None:
            return
        field = _field_label(card.payload.state)
        if field != self._field:
            self._field = field
            self._add(now, "field", field)
        strip = _strip_label(card.payload.last_play)
        if strip != self._strip:
            self._strip = strip
            self._add(now, "strip", strip)

    def _add(self, now: datetime, kind: str, text: str) -> None:
        self.lines.append(f"{(now - self._start).total_seconds():9.2f}  {kind:<5}  {text}")


class _Cards:
    """Stands in for the renderer: keeps the card it was last asked to draw."""

    def __init__(self, profile: PanelProfile) -> None:
        self._profile = profile
        self._latest: ScoreboardCard[LiveFootballCardPayload] | None = None

    @property
    def supported_profiles(self) -> frozenset[PanelProfile]:
        return frozenset({self._profile})

    def render(self, card: ScoreboardCard[LiveFootballCardPayload], context: RenderContext, canvas: Canvas) -> None:
        self._latest = card

    def take(self) -> ScoreboardCard[LiveFootballCardPayload] | None:
        latest, self._latest = self._latest, None
        return latest


class _NullSink:
    """A display that takes every frame and keeps none: replay decides, it doesn't draw."""

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
        return None


def _field_label(state: FootballGameState) -> str:
    """The field as a board decides on it: score, period, then the break or the next snap."""
    label = f"Q{state.period} {state.away_score}-{state.home_score}"
    if state.interval is not None:
        return f"{label} {state.interval.value}"
    side = state.possession.value if state.possession is not None else "-"
    if state.set_piece is not None:
        return f"{label} {state.set_piece.value} {side}"
    if state.possession is None:
        return f"{label} -"  # no snap to describe: a try, or a period's last plays before its break
    return f"{label} {side} {state.down}&{state.distance} @{state.yards_to_goal}"


def _strip_label(summary: PlaySummary | None) -> str:
    if summary is None:
        return "-"
    return summary.headline + (f" | {summary.detail}" if summary.detail else "")


def _answer(record: dict[str, Any], start: datetime, game: TeamGame) -> FeedResult:
    received = start + timedelta(seconds=record["t"])
    asked = start + timedelta(seconds=record.get("asked", record["t"]))
    next_ask = asked + timedelta(seconds=record["next"])
    shot = record.get("snapshot")
    if shot is None:
        return FeedResult(asked_at=asked, received_at=received, next_ask=next_ask, snapshot=None, error=record["error"])
    phase = NflPhase(shot["phase"])
    snapshot = NflLiveSnapshot(
        game=TeamGame(
            id=game.id,
            league=game.league,
            status=_STATUS[phase],
            scheduled_start=game.scheduled_start,
            away=game.away,
            home=game.home,
        ),
        phase=phase,
        state=_state(shot["state"]) if "state" in shot else None,
        events=tuple(_event(event) for event in shot.get("events", ())),
        caught_up=shot.get("caught_up", False),
        break_ends_at=start + timedelta(seconds=shot["break_ends"]) if "break_ends" in shot else None,
        observed_at=start + timedelta(seconds=shot["as_of"]) if "as_of" in shot else None,
        poll_every=DurationSeconds(shot["poll"]),
        settled=shot.get("settled", False),
        notes=tuple(FeedNote(FeedNoteKind(note["kind"]), note["detail"]) for note in shot.get("notes", ())),
    )
    return FeedResult(
        asked_at=asked,
        received_at=received,
        next_ask=next_ask,
        snapshot=snapshot,
        held=record.get("held", False),
        holding=record.get("holding", False),
    )


def _state_record(state: FootballGameState) -> dict[str, Any]:
    record: dict[str, Any] = {
        "period": state.period,
        "clock": state.clock_seconds,
        "score": [state.away_score, state.home_score],
    }
    optional = {
        "possession": state.possession.value if state.possession is not None else None,
        "down": state.down,
        "distance": state.distance,
        "to_goal": state.yards_to_goal,
        "timeouts": [state.timeouts.away, state.timeouts.home] if state.timeouts is not None else None,
        "challenges": [state.challenges.away, state.challenges.home] if state.challenges is not None else None,
        "interval": state.interval.value if state.interval is not None else None,
        "set_piece": state.set_piece.value if state.set_piece is not None else None,
    }
    record.update({key: value for key, value in optional.items() if value is not None})
    return record


def _state(record: dict[str, Any]) -> FootballGameState:
    timeouts, challenges = record.get("timeouts"), record.get("challenges")
    return FootballGameState(
        period=record["period"],
        clock_seconds=record["clock"],
        away_score=record["score"][0],
        home_score=record["score"][1],
        possession=HomeAway(record["possession"]) if "possession" in record else None,
        down=record.get("down"),
        distance=record.get("distance"),
        yards_to_goal=record.get("to_goal"),
        timeouts=TeamTimeouts(away=timeouts[0], home=timeouts[1]) if timeouts is not None else None,
        challenges=TeamChallenges(away=challenges[0], home=challenges[1]) if challenges is not None else None,
        interval=FootballInterval(record["interval"]) if "interval" in record else None,
        set_piece=SetPiece(record["set_piece"]) if "set_piece" in record else None,
    )


def _event_record(event: FootballEvent) -> dict[str, Any]:
    record: dict[str, Any] = {
        "key": event.key,
        "kind": event.kind.value,
        "tone": event.summary.tone.value,
        "headline": event.summary.headline,
    }
    if event.summary.detail is not None:
        record["detail"] = event.summary.detail
    if event.players_line is not None:
        record["players"] = event.players_line
    return record


def _event(record: dict[str, Any]) -> FootballEvent:
    return FootballEvent(
        key=record["key"],
        kind=FootballEventKind(record["kind"]),
        summary=PlaySummary(tone=PlayTone(record["tone"]), headline=record["headline"], detail=record.get("detail")),
        players_line=record.get("players"),
    )


def _seconds(span: timedelta) -> float:
    return round(span.total_seconds(), 3)


def _iso(instant: datetime) -> str:
    return instant.isoformat(timespec="milliseconds")


def _instant(raw: str) -> datetime:
    instant = datetime.fromisoformat(raw)
    if instant.tzinfo is None:
        raise ValueError(f"a trace's instants carry their offset: {raw}")
    return instant
