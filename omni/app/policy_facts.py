"""What a league pipeline tells the board's policy, kept from released state only (`BOARD_POLICY.md` §1, §4, §5).

Each refresh a pipeline describes every game it cards live (`omni.policy.view.GameFacts`) and offers cards for
promotion. All of it reads what has cleared the viewer's delay, never a raw receipt:

- **Breaks and action** (`ReleasedBreaks`): a league's detector reads each answer as it arrives, and what it finds is
  held through the same `ReleaseGuard`, from the same arrival, as the state that answer brought. It's published only
  once released: a break is something the viewer sees on the delayed broadcast, not on the raw feed. Each break
  carries the release instant pinned for its answer, which is where its lease's anchor starts from: a refresh that
  publishes it later doesn't move it.
  - Each break's newest revision is kept, the first copy of a revision winning (a re-read renews nothing).
  - A revision that marks the break over is published in the refresh it clears the delay, then dropped for good.
  - The newest action is the newest released: one with a new marker, unless the source's clock dates it before the
    one it would replace (a late row of an older snap).
  - Later released action closes a break the same answer doesn't still show: action with a new marker that, when
    both times are known on the source's clock, happened after the break began. A stoppage row NFL writes once is so
    remembered across looks until a snap follows it. The action names every break it has closed (its ``closes``),
    so the board ends a lease only for the break the action came after, never for a newer marker alone.
- **Plays waiting on their successor** (`AwaitingSuccessor`): a play's class reads the released states on either side
  of it, and the state after it clears the delay from its answer's receipt, at or after the play itself. So a play's
  offer waits until both have.
- **Offers** (`StandingOffers`): each stands in the view from its release until every promotion is gone
  (`PromotionPolicy.hard_end`), while the card it names is staged. A newer revision of the same moment replaces it;
  a scoring summary of the same play merges into it.
- **Stakes** (`interrupt_stakes`): the matchup's quality from the records frozen at the start (None when unknown),
  and a confirmed postseason game from the schedule's stage.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Generic, TypeVar

from omni.cards.base import DedupeKey
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import SeasonStage
from omni.domain.breaks import BreakEvidence, BreakId
from omni.domain.contest import TeamGame
from omni.events.break_detection import ActionEvidence
from omni.policy.allocation import interrupt_quality
from omni.policy.interrupts import InterruptClass, InterruptPolicy, Stakes, interrupt_value
from omni.policy.leverage import Leverage
from omni.policy.pool import MatchupRecords
from omni.policy.promotion import CandidateOrigin, PromotionCandidate, PromotionKey, PromotionKind, PromotionPolicy
from omni.queue.delay_policy import Arrival, Held, ReleaseGuard

__all__ = [
    "OFFER_LIFETIME",
    "AwaitingSuccessor",
    "Offer",
    "PlayContext",
    "ReleasedBreaks",
    "StandingOffers",
    "interrupt_stakes",
    "major_offer",
]

OFFER_LIFETIME = PromotionPolicy().hard_end  # an offer stands this long from its release: then every promotion's gone
_INTERRUPTS = InterruptPolicy()

Offer = PromotionCandidate[DedupeKey]
T = TypeVar("T")
StateT = TypeVar("StateT")


@dataclass(frozen=True, slots=True)
class _Seen:
    """What one answer showed: its breaks, and its newest action."""

    breaks: tuple[BreakEvidence, ...]
    action: ActionEvidence | None


class ReleasedBreaks:
    """One game's break and action evidence, held through the viewer's delay with the answers that brought it."""

    def __init__(self, guard: ReleaseGuard) -> None:
        self._guard = guard
        self._held: list[Held[_Seen]] = []
        self._newest: MonotonicTime | None = None  # the receipt of the newest answer pushed
        self._open: dict[BreakId, BreakEvidence] = {}  # released breaks still standing, each its newest revision
        self._ending: list[BreakEvidence] = []  # revisions marking a break over, released in the latest release
        self._over: set[BreakId] = set()  # breaks whose end has been published
        self._closed: set[BreakId] = set()  # breaks released action has closed
        self._action: ActionEvidence | None = None

    def push(
        self,
        breaks: tuple[BreakEvidence, ...],
        action: ActionEvidence | None,
        arrival: Arrival,
        admitted: ClockReading,
    ) -> None:
        """Hold what one answer showed, released when its state is: an answer received no later than one already
        pushed (the same answer read again) is ignored, as the state's delay ignores it."""
        received = arrival.received.monotonic
        if self._newest is not None and received <= self._newest:
            return
        self._newest = received
        if breaks or action is not None:
            self._held.append(Held(_Seen(breaks, action), arrival, self._guard.pin(arrival, admitted)))

    def release(self, at: ClockReading) -> None:
        """Take in, oldest first, what every answer held showed once it has cleared the delay by ``at``. An ended
        break published by the last release is dropped now."""
        self._ending = []
        ready = [held for held in self._held if held.release <= at.monotonic]
        self._held = [held for held in self._held if held.release > at.monotonic]
        for held in ready:
            self._take(held.value, held.release)
        action = self._action
        if action is not None and action.closes != self._closed:
            self._action = replace(action, closes=frozenset(self._closed))

    def _take(self, seen: _Seen, released: MonotonicTime) -> None:
        shown = {evidence.id for evidence in seen.breaks}
        action = seen.action
        if action is not None and _later(action, self._action):
            self._action = action
            for break_id, evidence in list(self._open.items()):
                if break_id not in shown and _after(action, evidence):
                    del self._open[break_id]  # play went on since it began
                    self._closed.add(break_id)
        for evidence in seen.breaks:
            kept = self._open.get(evidence.id)
            if evidence.id in self._over or (kept is not None and not _newer(evidence, kept)):
                continue
            evidence = replace(evidence, released=released)
            if evidence.ended:
                self._open.pop(evidence.id, None)
                self._over.add(evidence.id)
                self._ending.append(evidence)
            else:
                self._open[evidence.id] = evidence

    @property
    def breaks(self) -> tuple[BreakEvidence, ...]:
        """The released breaks standing, then any that the latest release marked over."""
        return (*self._open.values(), *self._ending)

    @property
    def action(self) -> ActionEvidence | None:
        """The newest released action."""
        return self._action


def _newer(evidence: BreakEvidence, kept: BreakEvidence) -> bool:
    """Whether ``evidence`` revises the break ``kept`` has: a later revision, or the same one marking it over."""
    return evidence.revision > kept.revision or (evidence.revision == kept.revision and evidence.ended)


def _later(action: ActionEvidence, current: ActionEvidence | None) -> bool:
    """Whether ``action`` is newer than the ``current`` newest: another marker, not dated before it by the source."""
    if current is None:
        return True
    if action.marker == current.marker:
        return False
    return action.at is None or current.at is None or action.at >= current.at


def _after(action: ActionEvidence, evidence: BreakEvidence) -> bool:
    """Whether ``action`` came after the break began: by the source's clock when both say, else by its new marker. A
    local hold's start is on this board's clock, never compared with the source's."""
    if action.at is None or evidence.onset is None or evidence.onset_basis.local:
        return True
    return action.at > evidence.onset


@dataclass(frozen=True, slots=True)
class PlayContext(Generic[StateT]):
    """A play's place in its game: the states of the answer before the one that brought it and of that answer, when
    that answer's state clears the delay, where the play came from, and, where a league reads the market either side
    of a play, when those two answers arrived."""

    before: StateT | None
    after: StateT | None
    settled: MonotonicTime  # when the state after it clears the delay
    origin: CandidateOrigin = CandidateOrigin.LIVE
    before_arrived: ClockReading | None = None  # when the answer before arrived; None when there wasn't one
    after_arrived: ClockReading | None = None  # when the answer that brought it arrived


class AwaitingSuccessor(Generic[T]):
    """Released plays whose offer waits for the state after them to clear the delay too."""

    def __init__(self) -> None:
        self._waiting: list[tuple[MonotonicTime, T]] = []

    def add(self, settled: MonotonicTime, item: T) -> None:
        self._waiting.append((settled, item))

    def ready(self, at: MonotonicTime) -> list[T]:
        """Those whose successor has cleared the delay by ``at``, in the order they were added; each once."""
        ready = [item for settled, item in self._waiting if settled <= at]
        self._waiting = [(settled, item) for settled, item in self._waiting if settled > at]
        return ready

    def forget(self, keep: Callable[[T], bool]) -> None:
        """Drop those ``keep`` turns down."""
        self._waiting = [(settled, item) for settled, item in self._waiting if keep(item)]


class StandingOffers:
    """Offers standing in the view: each from its release until its lifetime runs out, while its card is staged."""

    def __init__(self, lifetime: timedelta = OFFER_LIFETIME) -> None:
        self._lifetime = lifetime
        self._standing: list[Offer] = []

    def add(self, offer: Offer) -> None:
        """Stand ``offer`` in place of an older revision of the same moment. One no newer than the revision standing
        merges into it: a scoring summary offered under its play's key, or the same offer again."""
        if any(each.key == offer.key and each.revision >= offer.revision for each in self._standing):
            return
        self._standing = [each for each in self._standing if each.key != offer.key]
        self._standing.append(offer)

    def withdraw(self, key: PromotionKey) -> None:
        self._standing = [each for each in self._standing if each.key != key]

    def current(self, at: MonotonicTime, staged: Callable[[DedupeKey], bool]) -> tuple[Offer, ...]:
        """The offers standing at ``at`` whose card is ``staged``; those past their lifetime are dropped."""
        self._standing = [each for each in self._standing if at < each.eligible + self._lifetime]
        return tuple(each for each in self._standing if staged(each.card))


def interrupt_stakes(game: TeamGame, records: MatchupRecords | None) -> Stakes:
    """What the matchup brings to an interrupt: its quality from the records (None when unknown, which counts as
    nothing), and 1 for a confirmed postseason game. Nothing verifies a clinch or elimination game yet."""
    quality = interrupt_quality(records) if records is not None else None
    return Stakes(quality=quality, postseason=1.0 if game.stage is SeasonStage.POSTSEASON else 0.0)


def major_offer(
    event_class: InterruptClass,
    *,
    key: PromotionKey,
    card: DedupeKey,
    eligible: MonotonicTime,
    predecessor: Leverage | None,
    stakes: Stakes,
    revision: int = 0,
    origin: CandidateOrigin = CandidateOrigin.LIVE,
) -> Offer | None:
    """A major's offer for an event of ``event_class``, valued on its released predecessor's leverage; None when the
    class scores nothing (routine)."""
    if _INTERRUPTS.weight(event_class) <= 0:
        return None
    value = interrupt_value(event_class, predecessor=predecessor, stakes=stakes, policy=_INTERRUPTS)
    return PromotionCandidate(
        key=key,
        kind=PromotionKind.MAJOR,
        eligible=eligible,
        card=card,
        value=value,
        revision=revision,
        origin=origin,
    )
