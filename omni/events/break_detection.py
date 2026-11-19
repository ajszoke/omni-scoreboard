"""What the league break detectors share: action evidence, the onset and age rules, and the capability matrix.

Each league's detector (`omni.events.mlb_breaks`, `omni.events.nfl_breaks`) reads one answer about one game and gives two things:
- **break evidence** (`omni.domain.breaks.BreakEvidence`): the game has stopped, what kind of stop, which one,
  when it began if the feed says so credibly, and when that answer stood;
- **action evidence** (`ActionEvidence`): the newest action the feed shows. A lease lent from a break ends
  when released action after the break began closes it (`ActionEvidence.closes`).

**A break's age at detection** (`detection_age`) is how long it had run when its evidence reached the board. It's
told on the source's own clock where it can be: the answer's time less the onset, which no clock of ours enters,
plus the answer's transport to us, measured when our clock and the source's agree well enough to measure it. A hold
this board counts itself (NFL's halftime) is aged on our clock alone. When neither can be told, one poll interval
stands in, marked provisional. The viewer's delay never enters it: that lives in the release instant pinned for the
answer.

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
from omni.domain.breaks import AgeBasis, BreakAge, BreakConfidence, BreakEvidence, BreakId, BreakKind, OnsetBasis
from omni.queue.delay_policy import SOURCE_LATENESS, SOURCE_LEAD

__all__ = [
    "BREAK_CAPABILITIES",
    "DETECTED",
    "TRANSPORT_LIMIT",
    "ActionEvidence",
    "BreakCapability",
    "credible_onset",
    "detection_age",
    "onset_age",
]

_ZERO = timedelta(0)

# How long an answer may take from when it stood to when it reached us and its transport still be measured on the
# two clocks. Longer (or the source's clock well ahead of ours), one of the clocks is off, and a poll interval stands
# in for it.
TRANSPORT_LIMIT = SOURCE_LATENESS


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionEvidence:
    """The newest action a game's feed shows: a pitch, the end of a plate appearance, a snap.

    ``marker`` says where in the game it was ("at-bat 41 pitch 3"), so newer action has a marker other than
    the one in force when a break's evidence arrived. ``at`` is when it happened by the source's clock, as the
    feed gives it: compare it with a break's onset, which is on the same clock, never with ours. ``closes`` names
    every break that released action has closed so far: one it came after (`omni.app.policy_facts.ReleasedBreaks`).
    """

    contest: LeagueScopedId
    marker: str
    received: ClockReading  # when the answer showing it arrived
    at: datetime | None = None
    closes: frozenset[BreakId] = frozenset()


def credible_onset(onset: datetime | None, *, as_of: datetime | None) -> datetime | None:
    """``onset`` if it can be believed as when a break began, by the source's clock; else None.

    The source's answer must say when it stood (``as_of``), and the break mustn't be dated after it, beyond the
    second's rounding: both are on the source's clock, so the age between them is told with no clock of ours. How
    the answer's transport to us is told is `detection_age`'s to judge. An onset long before its answer is kept,
    however old: its age shortens the lease or refuses it (a stoppage first seen long after it happened), the safe
    way. The release guard refuses such a time because there it would shorten a delay instead.
    """
    if onset is None or as_of is None:
        return None
    return onset if onset <= as_of + SOURCE_LEAD else None


def detection_age(evidence: BreakEvidence, *, poll_interval: timedelta) -> BreakAge:
    """How long the break had run when its evidence arrived, and how that's known.

    - **A local hold:** from its start to the evidence's receipt, both on this board's clock.
    - **An onset on the source's clock:** the answer's time less the onset, plus the transport from when the answer
      stood to its receipt. The transport is measured while it's plausible (no more than `TRANSPORT_LIMIT`, and the
      source's clock no further ahead of ours than `SOURCE_LEAD`); otherwise one poll interval stands in, marked.
    - **No onset to tell, or none coherent with its answer's time:** one poll interval, marked provisional.
    """
    onset, received = evidence.onset, evidence.received.utc
    if onset is not None and evidence.onset_basis is OnsetBasis.LOCAL_HOLD:
        return BreakAge(age=max(received - onset, _ZERO), basis=AgeBasis.LOCAL_HOLD)
    as_of = evidence.as_of
    if onset is None or as_of is None or onset > as_of + SOURCE_LEAD:
        return BreakAge(age=poll_interval, basis=AgeBasis.PROVISIONAL, uncertainty=poll_interval)
    told = max(as_of - onset, _ZERO)
    transport = received - as_of
    if -SOURCE_LEAD <= transport <= TRANSPORT_LIMIT:
        return BreakAge(age=told + max(transport, _ZERO), basis=AgeBasis.SAME_SOURCE)
    return BreakAge(age=told + poll_interval, basis=AgeBasis.SAME_SOURCE, uncertainty=poll_interval)


def onset_age(evidence: BreakEvidence) -> timedelta | None:
    """How long the break had run when its evidence arrived, as far as its onset tells it, never below zero; None
    when its onset can't tell it."""
    age = detection_age(evidence, poll_interval=_ZERO)
    return None if age.basis is AgeBasis.PROVISIONAL else age.age


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
_WRITTEN = "when the feed wrote the row (changed_at): its publication, so the stoppage began no later"
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
    BreakCapability(
        kind=BreakKind.NFL_TV_TIMEOUT,
        confidence=None,
        signal="no feed signal identifies a commercial; an official timeout, its reason unknown, is never taken for one",
    ),
    BreakCapability(
        kind=BreakKind.NFL_OFFICIAL_TIMEOUT,
        confidence=_EXPLICIT,
        signal="a play-feed row of play_type official_timeout, with no newer snap in the same look",
        onset=_WRITTEN,
    ),
    BreakCapability(
        kind=BreakKind.NFL_TEAM_TIMEOUT,
        confidence=_EXPLICIT,
        signal='a play-feed row of play_type timeout ("Timeout #2 by CIN"), with no newer snap in the same look',
        onset=_WRITTEN,
    ),
    BreakCapability(
        kind=BreakKind.NFL_TWO_MINUTE_WARNING,
        confidence=_EXPLICIT,
        signal="a play-feed row of play_type two_minute_warning, with no newer snap in the same look",
        onset=_WRITTEN,
    ),
    BreakCapability(
        kind=BreakKind.NFL_QUARTER_BREAK,
        confidence=_EXPLICIT,
        signal='the scoreboard\'s status_detail "End of 1st" or "End of 3rd" (or of regulation, before overtime)',
    ),
    BreakCapability(
        kind=BreakKind.NFL_HALFTIME,
        confidence=_EXPLICIT,
        signal="the scoreboard's status_detail \"Halftime\"; its length is the provider's polling hold (break_ends_at)",
        onset="when the provider's hold counts the break from, on this board's clock: break_ends_at less the hold",
    ),
    BreakCapability(
        kind=BreakKind.NFL_INJURY_TIMEOUT,
        confidence=None,
        signal="the scoreboard's injuries can't tell an injury that stopped play now from one reported after play "
        "resumed, and the typed injury entry carries no time",
    ),
    BreakCapability(
        kind=BreakKind.NFL_REVIEW,
        confidence=None,
        signal="nothing marks a review in progress: the feed writes only the ruling, into the reviewed play's row",
    ),
    BreakCapability(kind=BreakKind.NBA_TEAM_TIMEOUT, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_TV_TIMEOUT, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_QUARTER_BREAK, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_HALFTIME, confidence=None, signal=_NO_NBA),
    BreakCapability(kind=BreakKind.NBA_BEFORE_OVERTIME, confidence=None, signal=_NO_NBA),
)

BREAK_CAPABILITIES: Mapping[BreakKind, BreakCapability] = {row.kind: row for row in _CAPABILITIES}

# The kinds a detector finds today: the only ones evidence can bring. A kind no feed signal identifies never enters
# from guesswork.
DETECTED: frozenset[BreakKind] = frozenset(row.kind for row in _CAPABILITIES if row.detected)
