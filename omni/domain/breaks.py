"""Break evidence: a typed observation that a game's action has stopped.

A break is a stretch when a game pauses: an inning break, a timeout, a quarter break. The board policy
lends a favorite's breaks to other games (`omni.policy`), so a break must be evidenced, never guessed.
Each observation says what kind of break it is, which break (so a re-read of the same one renews nothing),
when it was first received, when it began if the feed says so credibly, any length the feed states, and how
sure the evidence is. Each league's detector builds these from its parsed feed; the policy turns them into
leases, which are ceilings, not promises.

Times follow the clock rule: when the break began is an event instant (UTC, on the source's clock); when the
evidence arrived is a `ClockReading`, so a lease can be anchored on the monotonic clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from omni.core.clock import ClockReading
from omni.core.enum import League, StrEnumMixin
from omni.core.ids import LeagueScopedId

__all__ = ["BreakConfidence", "BreakEvidence", "BreakId", "BreakKind"]


class BreakKind(StrEnumMixin, str, Enum):
    """What stopped the game, per league."""

    MLB_INNING_BREAK = "mlb inning break"
    MLB_MOUND_VISIT = "mlb mound visit"
    MLB_PITCHING_CHANGE = "mlb pitching change"
    MLB_REVIEW = "mlb review"  # a replay review in progress
    MLB_STOPPAGE = "mlb stoppage"  # an injury, equipment or umpire stoppage, while it's active
    MLB_WEATHER = "mlb weather"  # a rain delay or a suspension
    NFL_TV_TIMEOUT = "nfl tv timeout"  # positively identified as a commercial; no feed says so yet
    NFL_OFFICIAL_TIMEOUT = "nfl official timeout"  # the reason unknown
    NFL_TEAM_TIMEOUT = "nfl team timeout"
    NFL_TWO_MINUTE_WARNING = "nfl two-minute warning"
    NFL_QUARTER_BREAK = "nfl quarter break"
    NFL_HALFTIME = "nfl halftime"
    NFL_INJURY_TIMEOUT = "nfl injury timeout"
    NFL_REVIEW = "nfl review"  # a review in progress, not its ruling
    NBA_TEAM_TIMEOUT = "nba team timeout"  # one team's timeouts went down by one, coherently
    NBA_TV_TIMEOUT = "nba tv timeout"  # a mandatory TV timeout; Free can't tell one
    NBA_QUARTER_BREAK = "nba quarter break"  # after the first or third quarter
    NBA_HALFTIME = "nba halftime"
    NBA_BEFORE_OVERTIME = "nba before overtime"

    @property
    def league(self) -> League:
        """The league whose games have this break."""
        return _LEAGUE[self.name.split("_", 1)[0]]


_LEAGUE = {"MLB": League.MLB, "NFL": League.NFL, "NBA": League.NBA}


class BreakConfidence(StrEnumMixin, str, Enum):
    """How sure the evidence is that the game has stopped."""

    EXPLICIT = "explicit"  # the feed marks it: an inning state, a period-end row, a timeout row
    INFERRED = "inferred"  # derived from a coherent change, such as a timeout count going down by one


@dataclass(frozen=True, slots=True)
class BreakId:
    """Which break: the game, the kind, and where in the game the feed places it (``"top 5 middle"``,
    ``"play 412"``). A later reading of the same break has the same id, and renews nothing."""

    contest: LeagueScopedId
    kind: BreakKind
    marker: str

    def __str__(self) -> str:
        return f"{self.contest} {self.kind.value} @ {self.marker}"


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakEvidence:
    """One observation that a game has stopped, as a league's detector read it from the feed."""

    id: BreakId
    received: ClockReading  # when the evidence first arrived
    confidence: BreakConfidence
    onset: datetime | None = None  # when the break began, on the source's clock, if the feed says so credibly
    stated: timedelta | None = None  # a length the feed states for it (MLB's inning break length)
    ended: bool = False  # the feed has marked its end
    revision: int = 0  # the evidence's revision: a correction of the same break counts up

    def __post_init__(self) -> None:
        if self.id.kind.league is not self.id.contest.league:
            raise ValueError(f"a {self.id.kind.value} can't stop a {self.id.contest.league.value} game")
        if self.stated is not None and self.stated <= timedelta(0):
            raise ValueError("a stated break length must be positive")
        if self.revision < 0:
            raise ValueError("a revision can't be negative")

    @property
    def kind(self) -> BreakKind:
        return self.id.kind
