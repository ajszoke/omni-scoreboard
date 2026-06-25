"""Baseball card payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.cards.base import ScoreboardCard
from omni.core.enum import HomeAway

# Domain value objects used by the payload fields below (imported for use, not re-exported).
from omni.domain.baseball import BaseballBaseState, BaseballCount, InningPhase, PitchingDecisions, WinProbability
from omni.events.baseball import BaseballGameEventType

__all__ = [
    "LiveBaseballCardPayload",
    "LiveBaseballCard",
    "PregameCardPayload",
    "PregameCard",
    "FinalCardPayload",
    "FinalCard",
    "BigPlayCardPayload",
    "BigPlayCard",
    "NoHitterCardPayload",
    "NoHitterCard",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveBaseballCardPayload:
    """The live state a baseball card needs to render; teams come from the contest."""

    away_score: int
    home_score: int
    inning: int
    phase: InningPhase
    count: BaseballCount
    bases: BaseballBaseState
    last_play: str | None = None
    win_probability: WinProbability | None = None  # drives the per-team meter; None hides it

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")
        if self.inning < 1:
            raise ValueError("inning must be >= 1")


# A live baseball card is a ScoreboardCard carrying the live payload above.
LiveBaseballCard = ScoreboardCard[LiveBaseballCardPayload]


@dataclass(frozen=True, slots=True, kw_only=True)
class PregameCardPayload:
    """The pregame "situation" a baseball card renders before first pitch: when the
    game starts. Teams come from the contest. Held as a self-contained snapshot (a
    copy of the scheduled start) so the renderer derives the live countdown from the
    render clock, not from a mutable contest. Probable pitchers / team records will
    join here when the provider surfaces them.
    """

    scheduled_start: datetime

    def __post_init__(self) -> None:
        if self.scheduled_start.tzinfo is None:
            raise ValueError("scheduled_start must be timezone-aware")


# A pregame baseball card is a ScoreboardCard carrying the pregame payload above.
PregameCard = ScoreboardCard[PregameCardPayload]


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalCardPayload:
    """A completed game's line: final score, with the winner *derived*, not stored.

    Teams come from the contest; `winner` is computed from the scores so it can never
    contradict them (an equal score — e.g. a weather-shortened tie — yields None).
    `decisions` is the winning/losing/saving pitchers (None on a tie or a feed without
    them); a renderer shortens to last names and a small panel may drop it.
    """

    away_score: int
    home_score: int
    decisions: PitchingDecisions | None = None

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")

    @property
    def winner(self) -> HomeAway | None:
        """The winning side derived from the score (None on a tie)."""
        if self.away_score > self.home_score:
            return HomeAway.AWAY
        if self.home_score > self.away_score:
            return HomeAway.HOME
        return None


# A final baseball card is a ScoreboardCard carrying the final payload above.
FinalCard = ScoreboardCard[FinalCardPayload]


@dataclass(frozen=True, slots=True, kw_only=True)
class BigPlayCardPayload:
    """A notable play flashed onto the screen: what happened + the resulting score.

    A render snapshot, denormalized from the `BaseballGameEvent` that triggered it.
    The play's *lineage* — the event id(s) that produced this card — lives on the
    card's `source_event_ids`, not here, so big plays stay dedupable and auditable.
    """

    event_type: BaseballGameEventType
    description: str
    away_score: int
    home_score: int

    def __post_init__(self) -> None:
        if self.away_score < 0 or self.home_score < 0:
            raise ValueError("scores cannot be negative")


# A big-play baseball card is a ScoreboardCard carrying the big-play payload above.
BigPlayCard = ScoreboardCard[BigPlayCardPayload]


@dataclass(frozen=True, slots=True, kw_only=True)
class NoHitterCardPayload:
    """A no-hitter (or perfect game) in progress: which side is throwing it, and how far.

    The feat belongs to the *defending* team, so `pitching_side` says which side of the
    contest that is and the renderer names the team from the contest (never a stored
    string). `perfect` distinguishes a perfect game (no baserunner has reached at all)
    from a plain no-hitter; `through_inning` is how deep it has carried — the pitching
    side's *completed* innings, so 6 means through six full innings, not the current
    inning number. Unlike a big play this is a standing condition, not a one-shot event —
    it carries no lineage and lives (recurring) until it is broken.
    """

    pitching_side: HomeAway
    through_inning: int
    perfect: bool = False

    def __post_init__(self) -> None:
        if self.through_inning < 1:
            raise ValueError("through_inning must be >= 1")


# A no-hitter baseball card is a ScoreboardCard carrying the no-hitter payload above.
NoHitterCard = ScoreboardCard[NoHitterCardPayload]
