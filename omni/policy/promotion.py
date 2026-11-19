"""The promotion broker: when a promoted card may start (`BOARD_POLICY.md` §4: cooldown, durations, the hold and
lifetimes).

The board offers it each promotion candidate as it's released, asks it at each legal decision what may start, and
reports each start once the card's first frame is confirmed. Everything runs on the monotonic clock.

- **The cooldown:** one promoted card per 60 s, board-wide, counted from its confirmed start. A cue that never
  showed spends nothing. Ordinary turns, ordered finals and in-place strip updates never come here. Its state is a
  value (`PromotionCooldown`) the board carries over a config reload.
- **The hold:** one episode opens when the first usable event needs a better moment, with its deadline at that
  event's eligibility + 24 s. Later candidates join it and don't extend it. An ideal moment (a break with room for
  the whole card and its return margin, or open rotation with no live favorite) starts the best candidate at once.
  Past the deadline, a candidate that qualifies for action may cut in at the next legal boundary; one that doesn't
  stays break-only. A confirmed start ends the episode, and its members never get a second one; an event first
  offered once an episode has closed opens its own.
- **Choosing:** the highest ``I``, then the earliest expiry, then the oldest, then the typed id; snapshots and
  reminders, which carry no ``I``, come after scored events. Having no ``I`` to clear a bar with, they start only
  where the lowest bar stands: every live favorite in an ordinary break, or none live; never in a micro-break.
- **Lifetimes:** a major must start within 120 s of eligibility, a snapshot or reminder within 60 s, and every
  promotion is gone by 120 s, so a 12 s major starts by +108 s. One that can't is retired to the results queue.
- **Dedupe:** by contest, play and revision. A scoring summary offered under its play's key merges into it; a newer
  revision replaces a waiting one without moving its eligibility; a shown or retired play is refused. A first
  baseline or an outage catch-up never alerts.

Every refusal carries a typed reason (`PromotionRefusal`) the trace can print.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from enum import Enum
from typing import Any, Generic, TypeVar

from omni.core.clock import MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.policy.interrupts import BarContext, InterruptBars, InterruptValue, bar_context
from omni.policy.leases import FavoriteMoment

__all__ = [
    "Airtime",
    "CandidateOrigin",
    "CandidateRefusal",
    "HoldEpisode",
    "OfferOutcome",
    "OfferResult",
    "PromotionBroker",
    "PromotionCandidate",
    "PromotionCooldown",
    "PromotionDecision",
    "PromotionKey",
    "PromotionKind",
    "PromotionMoment",
    "PromotionPolicy",
    "PromotionRefusal",
    "StartVerdict",
]

CardT = TypeVar("CardT")
_ZERO = timedelta(0)


class PromotionKind(StrEnumMixin, str, Enum):
    """What a promoted card is: a scored event (a major), or an unscored snapshot or reminder, which only ever plays
    in a break or open rotation."""

    MAJOR = "major"
    SNAPSHOT = "snapshot"
    REMINDER = "reminder"

    @property
    def scored(self) -> bool:
        return self is PromotionKind.MAJOR


class CandidateOrigin(StrEnumMixin, str, Enum):
    """Where a candidate came from: only live play alerts."""

    LIVE = "live"
    BASELINE = "baseline"  # the first reading of a game the board picked up mid-way
    CATCH_UP = "catch_up"  # what a source returning from an outage brought back


@dataclass(frozen=True, slots=True)
class PromotionKey:
    """Which moment: the contest and the lineage key of the play it's about (a scoring summary uses its play's, so
    the two merge), or of the snapshot or reminder."""

    contest: LeagueScopedId
    play: str

    def __post_init__(self) -> None:
        if not self.play:
            raise ValueError("a promotion needs its play's lineage key")

    def __str__(self) -> str:
        return f"{self.contest} {self.play}"


@dataclass(frozen=True, slots=True, kw_only=True)
class PromotionCandidate(Generic[CardT]):
    """A card that would promote a moment: what it is, when it became eligible (its release), and the card itself.

    A major carries its interrupt value; a snapshot or reminder carries none.
    """

    key: PromotionKey
    kind: PromotionKind
    eligible: MonotonicTime
    card: CardT
    value: InterruptValue | None = None
    revision: int = 0
    origin: CandidateOrigin = CandidateOrigin.LIVE

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("a revision can't be negative")
        if (self.value is not None) != self.kind.scored:
            raise ValueError("a major carries its interrupt value, and a snapshot or reminder carries none")

    @property
    def interrupt(self) -> float | None:
        """Its ``I``, or None for a snapshot or reminder."""
        return None if self.value is None else self.value.value


@dataclass(frozen=True, slots=True)
class Airtime:
    """A promoted card's full run and the minimum it keeps once up."""

    full: timedelta
    minimum: timedelta

    def __post_init__(self) -> None:
        if not _ZERO < self.minimum <= self.full:
            raise ValueError("a card's minimum is positive and no longer than its full run")


@dataclass(frozen=True, slots=True, kw_only=True)
class PromotionPolicy:
    """The cooldown, the hold, airtimes, lifetimes and the bars, as `BOARD_POLICY.md` §4 sets them."""

    cooldown: timedelta = timedelta(seconds=60)
    hold: timedelta = timedelta(seconds=24)
    major: Airtime = Airtime(timedelta(seconds=12), timedelta(seconds=8))
    snapshot: Airtime = Airtime(timedelta(seconds=8), timedelta(seconds=6))  # snapshots and reminders
    major_start_within: timedelta = timedelta(seconds=120)
    routine_start_within: timedelta = timedelta(seconds=60)
    hard_end: timedelta = timedelta(seconds=120)  # every promotion is gone this long after eligibility
    remember_for: timedelta = timedelta(minutes=15)  # past its hard end, how long a shown or retired play is refused
    bars: InterruptBars = InterruptBars()

    def __post_init__(self) -> None:
        spans = (self.cooldown, self.hold, self.major_start_within, self.routine_start_within, self.hard_end)
        if any(span <= _ZERO for span in spans) or self.remember_for < _ZERO:
            raise ValueError("a promotion policy's spans must be positive")
        if self.major.full > self.hard_end or self.snapshot.full > self.hard_end:
            raise ValueError("a promoted card must fit its own lifetime")

    def airtime(self, kind: PromotionKind) -> Airtime:
        return self.major if kind.scored else self.snapshot

    def latest_start(self, candidate: PromotionCandidate[Any]) -> MonotonicTime:
        """When the candidate must have started: within its start window, with its full run inside its hard end."""
        within = self.major_start_within if candidate.kind.scored else self.routine_start_within
        return candidate.eligible + min(within, self.hard_end - self.airtime(candidate.kind).full)

    def hard_end_of(self, candidate: PromotionCandidate[Any]) -> MonotonicTime:
        return candidate.eligible + self.hard_end


@dataclass(frozen=True, slots=True)
class PromotionCooldown:
    """The board-wide cooldown: when the last promoted card's first frame was confirmed. A value, so the board
    carries it over a config reload."""

    last_start: MonotonicTime | None = None

    def ready_at(self, cooldown: timedelta) -> MonotonicTime | None:
        """When the next promotion may start; None when nothing has started."""
        return None if self.last_start is None else self.last_start + cooldown

    def blocks(self, at: MonotonicTime, cooldown: timedelta) -> bool:
        ready = self.ready_at(cooldown)
        return ready is not None and at < ready


@dataclass(frozen=True, slots=True)
class PromotionMoment:
    """What the healthy live favorites are doing at a decision; with none live, the board is in open rotation."""

    favorites: tuple[FavoriteMoment, ...] = ()

    @property
    def context(self) -> BarContext:
        return bar_context(favorite.activity for favorite in self.favorites)

    @property
    def room(self) -> timedelta | None:
        """The longest guest card the moment can host: the shortest of the favorites' rooms (two favorites in
        breaks: the shorter opportunity limits it), zero while any is in action, None with no limit (no live
        favorite, or only open-ended statuses)."""
        rooms = [favorite.room for favorite in self.favorites if favorite.room is not None]
        return min(rooms, default=None)

    def ideal_for(self, card: timedelta) -> bool:
        """Whether a card this long may start at once: it fits the room."""
        room = self.room
        return room is None or room >= card


class PromotionRefusal(StrEnumMixin, str, Enum):
    """Why an offer was refused, why a candidate waits, or why a confirmed start was rejected."""

    BASELINE = "baseline"  # from a game's first baseline: no alerts
    CATCH_UP = "catch_up"  # from an outage's catch-up: no alerts
    ALREADY_SHOWN = "already_shown"  # the play was promoted already
    RETIRED = "retired"  # the play ran out its lifetime already
    STALE_REVISION = "stale_revision"  # an older revision than the one waiting
    BELOW_EVERY_BAR = "below_every_bar"  # worth less than the lowest bar: it can never promote
    EXPIRED = "expired"  # past its latest start: its run wouldn't fit its lifetime
    NOTHING_PENDING = "nothing_pending"
    COOLDOWN = "cooldown"  # a promoted card started within the cooldown
    BELOW_BAR = "below_bar"  # under the bar the favorites set now
    HOLDING = "holding"  # it may cut into action, but the hold is waiting for a better moment
    BREAK_ONLY = "break_only"  # it can't cut into action: below the action bar, unscored, or on proxy leverage
    ORDINARY_BREAK_ONLY = "ordinary_break_only"  # unscored: it plays only in an ordinary break or open rotation
    NO_ROOM = "no_room"  # the favorites' break can't fit the card and its return margin
    NOT_PENDING = "not_pending"  # a start reported for a candidate that isn't waiting
    SUPERSEDED = "superseded"  # a newer revision replaced the one cued
    BREAK_OVER = "break_over"  # the break it was cued into ended (play resumed, or its lease ran out) before its frame
    WITHDRAWN = "withdrawn"  # the published view no longer offers it (a correction took it back)


class OfferOutcome(StrEnumMixin, str, Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"  # the same play and revision as one waiting: folded into it
    CORRECTED = "corrected"  # a newer revision of one waiting: replaces it, keeping its eligibility and hold
    REFUSED = "refused"


@dataclass(frozen=True, slots=True, kw_only=True)
class OfferResult(Generic[CardT]):
    """What an offer did, and the candidate now waiting under its key (None when none is)."""

    outcome: OfferOutcome
    standing: PromotionCandidate[CardT] | None = None
    refusal: PromotionRefusal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateRefusal:
    """Why one waiting candidate didn't start at a decision."""

    key: PromotionKey
    revision: int
    reason: PromotionRefusal


@dataclass(frozen=True, slots=True, kw_only=True)
class PromotionDecision(Generic[CardT]):
    """A decision's answer: the candidate to cue (None: wait), each other candidate's reason, the bar it was judged
    against, and any candidates whose lifetime ran out, for the results queue."""

    context: BarContext
    bar: float
    start: PromotionCandidate[CardT] | None = None
    refusals: tuple[CandidateRefusal, ...] = ()
    retired: tuple[PromotionCandidate[CardT], ...] = ()

    @property
    def reason(self) -> PromotionRefusal | None:
        """Why nothing starts: the best candidate's reason; None when one starts."""
        if self.start is not None:
            return None
        return self.refusals[0].reason if self.refusals else PromotionRefusal.NOTHING_PENDING


@dataclass(frozen=True, slots=True)
class StartVerdict:
    """The broker's answer to a confirmed first frame: counted (the cooldown starts), or rejected and why."""

    refusal: PromotionRefusal | None = None

    @property
    def confirmed(self) -> bool:
        return self.refusal is None


@dataclass(frozen=True, slots=True)
class HoldEpisode:
    """The open hold: from the first member's eligibility to its deadline."""

    opened: MonotonicTime
    deadline: MonotonicTime


class PromotionBroker(Generic[CardT]):
    """Promotion candidates waiting for their moment, the hold, and the board-wide cooldown."""

    def __init__(
        self, policy: PromotionPolicy = PromotionPolicy(), *, cooldown: PromotionCooldown = PromotionCooldown()
    ) -> None:
        self._policy = policy
        self._cooldown = cooldown
        self._pending: dict[PromotionKey, PromotionCandidate[CardT]] = {}
        self._released: dict[PromotionKey, MonotonicTime] = {}  # when each waiting major's hold lets it cut in
        self._episode: HoldEpisode | None = None
        self._shown: dict[PromotionKey, MonotonicTime] = {}  # refused again until this instant
        self._retired: dict[PromotionKey, MonotonicTime] = {}

    @property
    def policy(self) -> PromotionPolicy:
        return self._policy

    @property
    def cooldown(self) -> PromotionCooldown:
        """The cooldown's state, to carry over a config reload."""
        return self._cooldown

    def hold(self, at: MonotonicTime) -> HoldEpisode | None:
        """The hold episode open at ``at``, if one is."""
        episode = self._episode
        return episode if episode is not None and at < episode.deadline else None

    def pending(self) -> tuple[PromotionCandidate[CardT], ...]:
        """The waiting candidates, best first."""
        return tuple(sorted(self._pending.values(), key=self._rank))

    def waiting(self, key: PromotionKey) -> PromotionCandidate[CardT] | None:
        """The candidate waiting under ``key``, if one is."""
        return self._pending.get(key)

    def offer(self, candidate: PromotionCandidate[CardT], at: MonotonicTime) -> OfferResult[CardT]:
        """Take in a released candidate: wait it, merge it, or refuse it."""
        refusal = self._screen(candidate, at)
        if refusal is not None:
            return OfferResult(outcome=OfferOutcome.REFUSED, refusal=refusal)
        waiting = self._pending.get(candidate.key)
        if waiting is not None:
            if candidate.revision < waiting.revision:
                return OfferResult(
                    outcome=OfferOutcome.REFUSED, standing=waiting, refusal=PromotionRefusal.STALE_REVISION
                )
            if candidate.revision == waiting.revision:
                return OfferResult(outcome=OfferOutcome.MERGED, standing=waiting)
            corrected = replace(candidate, eligible=min(candidate.eligible, waiting.eligible))
            if self._below_every_bar(corrected):
                self._drop(candidate.key)
                return OfferResult(outcome=OfferOutcome.REFUSED, refusal=PromotionRefusal.BELOW_EVERY_BAR)
            self._pending[candidate.key] = corrected
            return OfferResult(outcome=OfferOutcome.CORRECTED, standing=corrected)
        if at > self._policy.latest_start(candidate):
            return OfferResult(outcome=OfferOutcome.REFUSED, refusal=PromotionRefusal.EXPIRED)
        if self._below_every_bar(candidate):
            return OfferResult(outcome=OfferOutcome.REFUSED, refusal=PromotionRefusal.BELOW_EVERY_BAR)
        self._pending[candidate.key] = candidate
        if candidate.kind.scored:
            episode = self.hold(at)
            if episode is None:
                episode = HoldEpisode(candidate.eligible, candidate.eligible + self._policy.hold)
                self._episode = episode
            self._released[candidate.key] = episode.deadline
        return OfferResult(outcome=OfferOutcome.ACCEPTED, standing=candidate)

    def _screen(self, candidate: PromotionCandidate[CardT], at: MonotonicTime) -> PromotionRefusal | None:
        if candidate.origin is CandidateOrigin.BASELINE:
            return PromotionRefusal.BASELINE
        if candidate.origin is CandidateOrigin.CATCH_UP:
            return PromotionRefusal.CATCH_UP
        self._forget_old(at)
        if candidate.key in self._shown:
            return PromotionRefusal.ALREADY_SHOWN
        if candidate.key in self._retired:
            return PromotionRefusal.RETIRED
        return None

    def _below_every_bar(self, candidate: PromotionCandidate[CardT]) -> bool:
        worth = candidate.interrupt
        return worth is not None and worth < self._policy.bars.open

    def aired(self, card: CardT) -> tuple[PromotionCandidate[CardT], ...]:
        """The card waiting candidates name aired without a promotion (in its own game's turn): they're dropped, and
        refused from now on as shown. No cooldown is spent. Returns them."""
        settled = tuple(c for c in self._pending.values() if c.card == card)
        for candidate in settled:
            self._drop(candidate.key)
            self._shown[candidate.key] = self._policy.hard_end_of(candidate) + self._policy.remember_for
        return settled

    def withdraw(self, contest: LeagueScopedId) -> tuple[PromotionCandidate[CardT], ...]:
        """Drop a contest's waiting candidates at once (a mute); returns them."""
        dropped = tuple(c for c in self._pending.values() if c.key.contest == contest)
        for candidate in dropped:
            self._drop(candidate.key)
        return dropped

    def retract(self, key: PromotionKey) -> PromotionCandidate[CardT] | None:
        """Drop the candidate waiting under ``key`` (its offer was withdrawn, or no longer shows its event), and
        return it. It isn't remembered as shown or retired: a later revision may be offered afresh."""
        waiting = self._pending.get(key)
        self._drop(key)
        return waiting

    def sweep(self, at: MonotonicTime) -> tuple[PromotionCandidate[CardT], ...]:
        """Retire the candidates past their latest start at ``at``, oldest first, for the results queue, as a
        decision would; returns them."""
        return self._sweep(at)

    def decide(self, moment: PromotionMoment, at: MonotonicTime) -> PromotionDecision[CardT]:
        """At a legal decision: the best candidate that may start now, or why each waits. Candidates past their
        latest start are retired first, for the results queue. Nothing is spent until `started`."""
        retired = self._sweep(at)
        refusals: list[CandidateRefusal] = []
        context = moment.context
        bar = self._policy.bars.for_context(context)
        for candidate in self.pending():
            reason = self._refusal(candidate, moment, at, cueing=True)
            if reason is None:
                return PromotionDecision(
                    context=context, bar=bar, start=candidate, refusals=tuple(refusals), retired=retired
                )
            refusals.append(CandidateRefusal(key=candidate.key, revision=candidate.revision, reason=reason))
        return PromotionDecision(context=context, bar=bar, refusals=tuple(refusals), retired=retired)

    def started(self, candidate: PromotionCandidate[CardT], moment: PromotionMoment, at: MonotonicTime) -> StartVerdict:
        """The board confirmed the cued candidate's first frame at ``at``. A cue isn't a grant: if play resumed, a
        correction replaced it, or its latest start passed before the frame, it's rejected and spends nothing.
        Otherwise the cooldown starts from ``at`` and the hold ends."""
        waiting = self._pending.get(candidate.key)
        if waiting is None:
            return StartVerdict(PromotionRefusal.NOT_PENDING)
        if waiting.revision != candidate.revision:
            return StartVerdict(PromotionRefusal.SUPERSEDED)
        reason = self._refusal(waiting, moment, at, cueing=False)
        if reason is not None:
            return StartVerdict(reason)
        self._drop(candidate.key)
        self._shown[candidate.key] = self._policy.hard_end_of(waiting) + self._policy.remember_for
        self._cooldown = PromotionCooldown(at)
        self._episode = None  # the episode ends with its promotion; its members never get a second hold
        self._released = {key: min(released, at) for key, released in self._released.items()}
        return StartVerdict()

    def next_deadline(self, at: MonotonicTime) -> MonotonicTime | None:
        """The next instant the broker's answer can change by itself: the cooldown ending, the hold's deadline, or a
        waiting candidate's latest start. None with nothing waiting."""
        if not self._pending:
            return None
        instants = [self._policy.latest_start(candidate) for candidate in self._pending.values()]
        # A hold's deadline changes the answer only for a candidate that may cut into action.
        instants.extend(self._released[c.key] for c in self._pending.values() if self._qualifies_action(c))
        ready = self._cooldown.ready_at(self._policy.cooldown)
        if ready is not None:
            instants.append(ready)
        return min((instant for instant in instants if instant > at), default=None)

    def _refusal(
        self, candidate: PromotionCandidate[CardT], moment: PromotionMoment, at: MonotonicTime, *, cueing: bool
    ) -> PromotionRefusal | None:
        """Why the candidate can't start at ``at`` in ``moment``, or None when it may. Cueing needs an ideal moment
        or a released hold; confirming a cue only needs the break it went into to still be on."""
        policy = self._policy
        if self._cooldown.blocks(at, policy.cooldown):
            return PromotionRefusal.COOLDOWN
        if at > policy.latest_start(candidate):
            return PromotionRefusal.EXPIRED
        holding = self._qualifies_action(candidate)
        if holding and at >= self._released[candidate.key]:
            return None  # it may cut into action at this legal boundary, and so clears every bar
        context = moment.context
        if context is BarContext.ACTION and not cueing:
            return PromotionRefusal.BREAK_OVER
        worth = candidate.interrupt
        if worth is not None and worth < policy.bars.for_context(context):
            return PromotionRefusal.BELOW_BAR
        if context is BarContext.ACTION:
            return PromotionRefusal.HOLDING if holding else PromotionRefusal.BREAK_ONLY
        if worth is None and context is not BarContext.OPEN:  # no value to weigh against a micro-break's bar
            return PromotionRefusal.ORDINARY_BREAK_ONLY
        if cueing and not moment.ideal_for(policy.airtime(candidate.kind).full):
            return PromotionRefusal.HOLDING if holding else PromotionRefusal.NO_ROOM
        return None

    def _qualifies_action(self, candidate: PromotionCandidate[CardT]) -> bool:
        """Whether it may ever cut into action: scored, on leverage evidence that allows it, above the action bar."""
        return candidate.value is not None and self._policy.bars.qualifies_action(candidate.value)

    def _rank(self, candidate: PromotionCandidate[CardT]) -> tuple[int, float, float, float, str, str, int]:
        worth = candidate.interrupt
        return (
            0 if worth is not None else 1,
            -(worth or 0.0),
            self._policy.latest_start(candidate).seconds,
            candidate.eligible.seconds,
            str(candidate.key.contest),
            candidate.key.play,
            candidate.revision,
        )

    def _drop(self, key: PromotionKey) -> None:
        self._pending.pop(key, None)
        self._released.pop(key, None)

    def _sweep(self, at: MonotonicTime) -> tuple[PromotionCandidate[CardT], ...]:
        """Retire the candidates past their latest start, oldest first."""
        self._forget_old(at)
        expired = sorted(
            (c for c in self._pending.values() if at > self._policy.latest_start(c)), key=lambda c: c.eligible
        )
        for candidate in expired:
            self._drop(candidate.key)
            self._retired[candidate.key] = self._policy.hard_end_of(candidate) + self._policy.remember_for
        return tuple(expired)

    def _forget_old(self, at: MonotonicTime) -> None:
        for memory in (self._shown, self._retired):
            for key in [key for key, until in memory.items() if until <= at]:
                del memory[key]
