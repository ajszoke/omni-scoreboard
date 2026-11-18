"""What the league break detectors share: action evidence, the onset rule, and the capability matrix.

Each league's detector (`omni.events.mlb_breaks`) reads one answer about one game and gives two things:
- **break evidence** (`omni.domain.breaks.BreakEvidence`): the game has stopped, what kind of stop, which one,
  and when it began if the feed says so credibly;
- **action evidence** (`ActionEvidence`): the newest action the feed shows. A lease lent from a break ends
  when a later answer shows newer action.

**The capability matrix** (`BREAK_CAPABILITIES`) says, for every `BreakKind`, whether a detector finds it
today, from which feed field, with what confidence, and where its onset comes from. A kind that isn't detected
says why not. The tests walk it with one example each, so it can't drift from the detectors.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from omni.core.clock import ClockReading
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakKind
from omni.queue.delay_policy import SOURCE_LEAD

__all__ = ["BREAK_CAPABILITIES", "ActionEvidence", "BreakCapability", "credible_onset", "onset_age"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionEvidence:
    """The newest action a game's feed shows: a pitch, the end of a plate appearance, a snap.

    ``marker`` says where in the game it was ("at-bat 41 pitch 3"), so newer action has a marker other than
    the one in force when a break's evidence arrived. ``at`` is when it happened by the source's clock, as the
    feed gives it: compare it with a break's onset, which is on the same clock, never with ours.
    """

    contest: LeagueScopedId
    marker: str
    received: ClockReading  # when the answer showing it arrived
    at: datetime | None = None


def credible_onset(onset: datetime | None, *, as_of: datetime | None, received: ClockReading) -> datetime | None:
    """``onset`` if it can be believed as when a break began, by the source's clock; else None.

    This reads a source's times as the release guard does (`omni.queue.delay_policy`), turned for a lease. A
    lease runs from the onset, so an onset dated too late would lengthen it, and one dated early only shortens it.
    - The source's answer must say when it stood (``as_of``), and not after it reached us. A source clock running
      ahead of ours would make every break look younger than it is.
    - The break mustn't be dated after it reached us.

    An onset long before its receipt is kept, however old: its age shortens the lease or refuses it (a stoppage
    first seen long after it happened), the safe way. The release guard refuses such a time because there it
    would shorten a delay instead.
    """
    if onset is None or as_of is None:
        return None
    latest = received.utc + SOURCE_LEAD
    return onset if as_of <= latest and onset <= latest else None


def onset_age(evidence: BreakEvidence) -> timedelta | None:
    """How long the break had run when its evidence arrived, never below zero; None when its onset isn't known."""
    if evidence.onset is None:
        return None
    return max(evidence.received.utc - evidence.onset, timedelta(0))


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakCapability:
    """What the detectors can say about one kind of break today."""

    kind: BreakKind
    confidence: BreakConfidence | None  # None: not detected
    signal: str  # the feed field it's read from; for a kind not detected, why not
    onset: str | None = None  # where its onset comes from, when the feed can say

    def __post_init__(self) -> None:
        if self.onset is not None and self.confidence is None:
            raise ValueError("a break that isn't detected has no onset")

    @property
    def detected(self) -> bool:
        return self.confidence is not None


_EXPLICIT = BreakConfidence.EXPLICIT
_NO_NFL = "no NFL detector yet"
_NO_NBA = "no NBA detector yet: NBA isn't on the shared board"

_CAPABILITIES = (
    BreakCapability(
        kind=BreakKind.MLB_INNING_BREAK,
        confidence=_EXPLICIT,
        signal="liveData.linescore.inningState Middle or End; its length is the schedule's inningBreakLength",
        onset="the half's last plate appearance's about.endTime",
    ),
    BreakCapability(
        kind=BreakKind.MLB_MOUND_VISIT,
        confidence=_EXPLICIT,
        signal="playEvents[].details.eventType mound_visit in the newest plate appearance, with no pitch since",
        onset="the event's startTime",
    ),
    BreakCapability(
        kind=BreakKind.MLB_PITCHING_CHANGE,
        confidence=_EXPLICIT,
        signal="playEvents[].details.eventType pitching_substitution in the newest plate appearance, no pitch since",
        onset="the event's startTime",
    ),
    BreakCapability(
        kind=BreakKind.MLB_REVIEW,
        confidence=_EXPLICIT,
        signal="reviewDetails.inProgress on the newest plate appearances and their non-pitch events; a ruling "
        "(inProgress false) is evidence that has ended",
    ),
    BreakCapability(
        kind=BreakKind.MLB_STOPPAGE,
        confidence=_EXPLICIT,
        signal="playEvents[].details.eventType injury in the newest plate appearance, with no pitch since; the feed "
        "has no event type for an equipment or an umpire stoppage",
        onset="the event's startTime",
    ),
    BreakCapability(
        kind=BreakKind.MLB_WEATHER,
        confidence=_EXPLICIT,
        signal="gameData.status.detailedState Delayed or Suspended (a status card, not a lease)",
    ),
    BreakCapability(kind=BreakKind.NFL_TV_TIMEOUT, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NFL_OFFICIAL_TIMEOUT, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NFL_TEAM_TIMEOUT, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NFL_TWO_MINUTE_WARNING, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NFL_QUARTER_BREAK, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NFL_HALFTIME, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NFL_INJURY_TIMEOUT, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NFL_REVIEW, confidence=None, signal=_NO_NFL),
    BreakCapability(kind=BreakKind.NBA_TEAM_TIMEOUT, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_TV_TIMEOUT, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_QUARTER_BREAK, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_HALFTIME, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_BEFORE_OVERTIME, confidence=None, signal=_NO_NBA),
)

BREAK_CAPABILITIES: Mapping[BreakKind, BreakCapability] = {row.kind: row for row in _CAPABILITIES}
