"""Break evidence: a typed observation that a game's action has stopped.

A break is a stretch when a game pauses: an inning break, a timeout, a quarter break. The board policy
lends a favorite's breaks to other games (`omni.policy`), so a break must be evidenced, never guessed.
Each observation says what kind of break it is, which break (so a re-read of the same one renews nothing),
when it was first received, when it began if the feed says so credibly (and what that instant is: the stoppage's
own time, the feed's write of its row, a hold this board counts, or the first read that showed play stopped), when the answer that showed it stood, any
length the feed states, and how sure the evidence is. Each league's detector builds these from its parsed feed;
once the viewer's delay releases one, it also carries the release instant pinned for its answer. The policy turns
them into leases, which are ceilings, not promises.

Times follow the clock rule: when the break began and when its answer stood are instants on the source's clock (UTC),
but for a local hold's start or a first read's arrival, which are on ours; when the evidence arrived is a `ClockReading`, and its release a
monotonic instant, so a lease is anchored on the monotonic clock.

How long a break had run when its evidence arrived (`BreakAge`) says how it's known: told on the source's own clock,
counted by a local hold, or one poll interval, marked provisional, when the onset can't be told.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League, StrEnumMixin
from omni.core.ids import LeagueScopedId

__all__ = ["AgeBasis", "BreakAge", "BreakConfidence", "BreakEvidence", "BreakId", "BreakKind", "OnsetBasis"]


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
    NBA_TEAM_TIMEOUT = "nba team timeout"  # a short timeout: a quarter's third or later, or one in overtime
    NBA_TV_TIMEOUT = "nba tv timeout"  # a long one: a regulation quarter's first or second, the TV timeouts
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


class OnsetBasis(StrEnumMixin, str, Enum):
    """What a break's onset is, and so whose clock it reads."""

    EVENT = "event"  # the source's time for the stoppage itself: a mound visit's start, a half's last out
    PUBLISHED = "published"  # when the source wrote the row that names it: the stoppage began no later
    LOCAL_HOLD = "local_hold"  # when a hold this board counts began, on this board's clock (NFL's halftime hold)
    LOCAL_READ = "local_read"  # the arrival of the first read that showed play stopped, on this board's clock (NBA's)

    @property
    def local(self) -> bool:
        """Whether the onset is on this board's clock, never to be compared with the source's."""
        return self in (OnsetBasis.LOCAL_HOLD, OnsetBasis.LOCAL_READ)


class AgeBasis(StrEnumMixin, str, Enum):
    """How a break's age at detection is known."""

    SAME_SOURCE = "same_source"  # its answer's time less its onset, both on the source's clock, and the transport
    LOCAL_HOLD = "local_hold"  # from a local hold's start to the evidence's receipt, both on this board's clock
    LOCAL_READ = "local_read"  # from the first read that showed play stopped to the evidence's receipt, likewise
    PROVISIONAL = "provisional"  # the onset can't be told: one poll interval


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakAge:
    """How long a break had run when its evidence reached the board, how that's known, and how much older it may
    have been (the part of the age that's an allowance, not a measurement). ``provisional`` marks an age that
    includes a poll interval for what couldn't be told."""

    age: timedelta
    basis: AgeBasis
    uncertainty: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if self.age < timedelta(0) or self.uncertainty < timedelta(0):
            raise ValueError("a break's age and its uncertainty can't be negative")

    @property
    def provisional(self) -> bool:
        return self.uncertainty > timedelta(0)


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
    as_of: datetime | None = None  # when the source's answer that showed it stood, by its clock
    onset_basis: OnsetBasis = OnsetBasis.EVENT  # what the onset is
    released: MonotonicTime | None = None  # the release instant pinned for its answer, once the delay released it

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
