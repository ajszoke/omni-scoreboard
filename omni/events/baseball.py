"""Baseball event taxonomy, play payload, and typed event."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import StrEnumMixin
from omni.domain.baseball import BaseballCount, BaseballGameState, InningPhase, PitchingDecisions, PitchType
from omni.events.base import GameEvent

__all__ = [
    "BaseballGameEventType",
    "BaseballPlayPayload",
    "BaseballGameEvent",
    "LiveBaseballFeed",
]


class BaseballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    PITCH = "pitch"
    BALL = "ball"
    CALLED_STRIKE = "called_strike"
    SWINGING_STRIKE = "swinging_strike"
    FOUL = "foul"
    BALL_IN_PLAY = "ball_in_play"
    STRIKEOUT_LOOKING = "strikeout_looking"
    STRIKEOUT_SWINGING = "strikeout_swinging"
    WALK = "walk"
    HIT_BY_PITCH = "hit_by_pitch"
    SINGLE = "single"
    DOUBLE = "double"
    TRIPLE = "triple"
    HOME_RUN = "home_run"
    SAC_FLY = "sac_fly"
    SAC_BUNT = "sac_bunt"
    DOUBLE_PLAY = "double_play"
    TRIPLE_PLAY = "triple_play"
    RUN_SCORED = "run_scored"
    RBI = "rbi"
    PITCHING_CHANGE = "pitching_change"
    CHALLENGE = "challenge"
    ABS_CHALLENGE = "abs_challenge"
    INNING_START = "inning_start"
    HALF_INNING_END = "half_inning_end"
    GAME_FINAL = "game_final"
    NO_HITTER_ACTIVE = "no_hitter_active"
    NO_HITTER_BROKEN = "no_hitter_broken"
    PERFECT_GAME_ACTIVE = "perfect_game_active"
    PERFECT_GAME_BROKEN = "perfect_game_broken"


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballPlayPayload:
    """The baseball-specific body of a `BaseballGameEvent`.

    `away_score`/`home_score` are the score *after* this play — what the play left the
    game at — so an event-derived card (a big play) shows the exact resulting score, not
    the live, possibly-spoiler score. They are optional because a non-scoring status
    event (e.g. a no-hitter badge) carries no score; the provider fills them for plays.

    `pitch_type` is the typed `PitchType` of the at-bat's decisive (last) pitch, or None
    when the feed carries no pitch detail — never a raw code string.

    `fielder_sequence` is structured as ints (e.g. ``(9, 6, 4)``), not a string
    like ``"9-6-4-"``; rendering joins it late and avoids dangling delimiters.
    """

    inning: int
    phase: InningPhase
    description: str
    count: BaseballCount | None = None
    rbi: int = 0
    away_score: int | None = None
    home_score: int | None = None
    pitch_type: PitchType | None = None
    fielder_sequence: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseballGameEvent(GameEvent[BaseballGameEventType, BaseballPlayPayload]):
    """A `GameEvent` specialized to baseball event types and play payloads."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveBaseballFeed:
    """The typed result of one per-game live-feed fetch: the current `state` plus the
    typed `events` parsed from the *same* payload.

    One network fetch yields both, so the pipeline never double-fetches the feed; the
    events carry the lineage (`BaseballGameEvent.id`) that a bare state snapshot lacks,
    which is what lets a derived big-play card point back at the play that produced it.
    The pipeline consumes `state` for the live card and `events` for big-play cards.

    `decisions` carries the winning/losing/saving pitchers once the game is final (None
    while it is in progress, a tie, or absent from the feed) — what the final card needs
    beyond the score.
    """

    state: BaseballGameState
    events: tuple[BaseballGameEvent, ...] = ()
    decisions: PitchingDecisions | None = None
