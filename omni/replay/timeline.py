"""Timeline: a timestamped, multi-game fixture the replay harness drives the loop from.

The kernel asks for fixture *record/replay* and deterministic queue behavior, not
just static snapshots. A `Timeline` is the format: a set of per-game `GameFrame`s,
each effective from its timestamp until that game's next frame. At any `now`, each
game shows the latest frame at or before `now` — a game enters the schedule at its
first frame, and its status (pregame -> live -> final) and live state advance as
frames do. The replay harness (`omni.replay.harness`) reads a timeline through a
`Provider`/state-fetcher pair so the *real* `AppLoop` runs against it unchanged.

Frames carry already-typed domain objects (a `TeamGame`, its `BaseballGameState`, the
feed's plays so far and a final's pitching decisions), not raw provider JSON — parsing
the wire shape is the provider boundary's job and is tested there. A timeline is
therefore trivial to author in a test or a fixture.

An `Outage` is a span when every source fails: the schedule and every game's feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.core.enum import GameStatus
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import BaseballGameState, PitchingDecisions
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent

__all__ = ["GameFrame", "Outage", "Timeline"]


@dataclass(frozen=True, slots=True, kw_only=True)
class GameFrame:
    """One game's situation effective from ``at`` until that game's next frame.

    Carries the full typed contest (whose ``status`` may change across frames) and,
    while the game is LIVE, its observed state. A LIVE frame must carry state — that
    is what the loop's live pipeline fetches. ``events`` are the feed's plays as of this
    frame, oldest first, as a real feed lists every play so far; ``decisions`` are a
    final's winning, losing and saving pitchers.
    """

    at: datetime
    game: TeamGame
    state: BaseballGameState | None = None
    events: tuple[BaseballGameEvent, ...] = ()
    decisions: PitchingDecisions | None = None

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("frame `at` must be timezone-aware")
        if self.game.status is GameStatus.LIVE and self.state is None:
            raise ValueError(f"LIVE frame for {self.game.id.raw} must carry game state")


@dataclass(frozen=True, slots=True, kw_only=True)
class Outage:
    """A span, ``start`` up to ``end``, when every source fails."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("an outage's instants must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("an outage must end after it starts")


@dataclass(frozen=True, slots=True, kw_only=True)
class Timeline:
    """A timestamped, multi-game timeline resolved by effective-as-of-``now`` lookup."""

    frames: tuple[GameFrame, ...]
    outages: tuple[Outage, ...] = ()

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("a timeline needs at least one frame")

    @property
    def start(self) -> datetime:
        return min(frame.at for frame in self.frames)

    @property
    def end(self) -> datetime:
        return max(frame.at for frame in self.frames)

    @property
    def changes(self) -> tuple[datetime, ...]:
        """Every instant something in the timeline changes (a frame lands, an outage starts or ends), in order."""
        instants = {frame.at for frame in self.frames}
        for outage in self.outages:
            instants.update((outage.start, outage.end))
        return tuple(sorted(instants))

    def frame_at(self, game_id: LeagueScopedId, now: datetime) -> GameFrame | None:
        """The latest frame for ``game_id`` at or before ``now`` (None before its first)."""
        candidates = [frame for frame in self.frames if frame.game.id == game_id and frame.at <= now]
        return max(candidates, key=lambda frame: frame.at) if candidates else None

    def schedule_at(self, now: datetime) -> tuple[TeamGame, ...]:
        """Every game in the schedule as of ``now``, each at its effective status.

        Sorted by id so the provider snapshot — and the resulting trace — is stable.
        """
        ids = {frame.game.id for frame in self.frames}
        effective = [self.frame_at(game_id, now) for game_id in ids]
        games = [frame.game for frame in effective if frame is not None]
        return tuple(sorted(games, key=lambda game: str(game.id)))

    def state_at(self, game_id: LeagueScopedId, now: datetime) -> BaseballGameState | None:
        """The game's live state as of ``now`` (None before its first frame / when not live)."""
        frame = self.frame_at(game_id, now)
        return frame.state if frame is not None else None

    def down_at(self, now: datetime) -> bool:
        """Whether every source is failing at ``now``."""
        return any(outage.start <= now < outage.end for outage in self.outages)
