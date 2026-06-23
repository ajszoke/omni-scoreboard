"""Timeline: a timestamped, multi-game fixture the replay harness drives the loop from.

The kernel asks for fixture *record/replay* and deterministic queue behaviour, not
just static snapshots. A `Timeline` is the format: a set of per-game `GameFrame`s,
each effective from its timestamp until that game's next frame. At any `now`, each
game shows the latest frame at or before `now` — a game enters the schedule at its
first frame, and its status (pregame -> live -> final) and live state advance as
frames do. The replay harness (`omni.replay.harness`) reads a timeline through a
`Provider`/state-fetcher pair so the *real* `AppLoop` runs against it unchanged.

Frames carry already-typed domain objects (a `TeamGame` and its `BaseballGameState`),
not raw provider JSON — parsing the wire shape is the provider boundary's job and is
tested there. A timeline is therefore trivial to author in a test or a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.core.enum import GameStatus
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import BaseballGameState
from omni.domain.contest import TeamGame

__all__ = ["GameFrame", "Timeline"]


@dataclass(frozen=True, slots=True, kw_only=True)
class GameFrame:
    """One game's situation effective from ``at`` until that game's next frame.

    Carries the full typed contest (whose ``status`` may change across frames) and,
    while the game is LIVE, its observed state. A LIVE frame must carry state — that
    is what the loop's live pipeline fetches.
    """

    at: datetime
    game: TeamGame
    state: BaseballGameState | None = None

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("frame `at` must be timezone-aware")
        if self.game.status is GameStatus.LIVE and self.state is None:
            raise ValueError(f"LIVE frame for {self.game.id.raw} must carry game state")


@dataclass(frozen=True, slots=True, kw_only=True)
class Timeline:
    """A timestamped, multi-game timeline resolved by effective-as-of-``now`` lookup."""

    frames: tuple[GameFrame, ...]

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("a timeline needs at least one frame")

    @property
    def start(self) -> datetime:
        return min(frame.at for frame in self.frames)

    @property
    def end(self) -> datetime:
        return max(frame.at for frame in self.frames)

    def _effective(self, game_id: LeagueScopedId, now: datetime) -> GameFrame | None:
        """The latest frame for ``game_id`` at or before ``now`` (None before its first)."""
        candidates = [frame for frame in self.frames if frame.game.id == game_id and frame.at <= now]
        return max(candidates, key=lambda frame: frame.at) if candidates else None

    def schedule_at(self, now: datetime) -> tuple[TeamGame, ...]:
        """Every game in the schedule as of ``now``, each at its effective status.

        Sorted by id so the provider snapshot — and the resulting trace — is stable.
        """
        ids = {frame.game.id for frame in self.frames}
        effective = [self._effective(game_id, now) for game_id in ids]
        games = [frame.game for frame in effective if frame is not None]
        return tuple(sorted(games, key=lambda game: str(game.id)))

    def state_at(self, game_id: LeagueScopedId, now: datetime) -> BaseballGameState | None:
        """The game's live state as of ``now`` (None before its first frame / when not live)."""
        frame = self._effective(game_id, now)
        return frame.state if frame is not None else None
