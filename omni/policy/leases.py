"""Break leases: how long a favorite's break may be lent to other games (`BOARD_POLICY.md` §5).

A lease is a ceiling, not a promise. It opens on typed break evidence (`omni.domain.breaks.BreakEvidence`) and is
anchored once, on the viewer's delayed timeline, at when the break began there:
- a known onset dates it, so the age it already had when it arrived comes off the ceiling;
- with the onset uncertain, one poll interval comes off, and the lease is marked provisional;
- a break most of whose ceiling had passed before it reached the board (severe backfill) gets no lease.

Only new evidence opens one: a repeated row of the same break, a break already marked over when it arrives (a
completed ruling, a backfilled start and end), or a second break inside an open lease (a pitching change inside an
inning break) opens and extends nothing. Released action in the game revokes it. A frozen clock isn't evidence:
the detectors never report one, and nothing here reads a clock.

A guest needs its whole card plus a return margin inside what's left, and some breaks cap their guests. When a
lease runs out with no sign of play, the favorite goes back to weighted eligibility under the action bar; that's
not a claim that play resumed.

Spans run on the monotonic clock; the onset and the arrival are compared on the wall clock only to date the break.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import Enum
from types import MappingProxyType

from omni.core.clock import MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakEvidence, BreakId, BreakKind
from omni.policy.interrupts import FavoriteActivity

__all__ = [
    "BreakScale",
    "Ceiling",
    "FavoriteMoment",
    "Lease",
    "LeaseBook",
    "LeaseClosure",
    "LeasePolicy",
    "LeaseRefusal",
    "LeaseState",
    "LeaseVerdict",
]

_ZERO = timedelta(0)


def _seconds(value: float) -> timedelta:
    return timedelta(seconds=value)


class BreakScale(StrEnumMixin, str, Enum):
    """Whether a break is a short in-game stoppage, which only clears the middle bar, or an ordinary break."""

    MICRO = "micro"
    ORDINARY = "ordinary"

    @property
    def activity(self) -> FavoriteActivity:
        return FavoriteActivity.MICRO_BREAK if self is BreakScale.MICRO else FavoriteActivity.ORDINARY_BREAK


@dataclass(frozen=True, slots=True, kw_only=True)
class Ceiling:
    """One kind of break's lease: its longest span (None: open for as long as a status stands), how many guests it
    may host (None: as many as fit), its length on verified national TV where the rules differ, and its scale."""

    length: timedelta | None
    scale: BreakScale
    turns: int | None = None
    national: timedelta | None = None

    def __post_init__(self) -> None:
        if any(span is not None and span <= _ZERO for span in (self.length, self.national)):
            raise ValueError("a ceiling's length must be positive")
        if self.turns is not None and self.turns < 1:
            raise ValueError("a capped break hosts at least one guest")


_MICRO, _ORDINARY = BreakScale.MICRO, BreakScale.ORDINARY

# `BOARD_POLICY.md` §5's table. MLB's inning break uses the feed's stated length less a trim; this is its length
# when unknown. NFL's halftime is the 13-minute polling quiet hold, then up to 120 s of grace.
CEILINGS: Mapping[BreakKind, Ceiling] = MappingProxyType(
    {
        BreakKind.MLB_INNING_BREAK: Ceiling(length=_seconds(60), scale=_ORDINARY),
        BreakKind.MLB_MOUND_VISIT: Ceiling(length=_seconds(36), scale=_MICRO, turns=1),
        BreakKind.MLB_PITCHING_CHANGE: Ceiling(length=_seconds(36), scale=_MICRO, turns=1),
        BreakKind.MLB_REVIEW: Ceiling(length=_seconds(48), scale=_MICRO, turns=2),
        BreakKind.MLB_STOPPAGE: Ceiling(length=_seconds(30), scale=_MICRO, turns=1),
        BreakKind.MLB_WEATHER: Ceiling(length=None, scale=_ORDINARY),
        BreakKind.NFL_TV_TIMEOUT: Ceiling(length=_seconds(120), scale=_ORDINARY),
        BreakKind.NFL_OFFICIAL_TIMEOUT: Ceiling(length=_seconds(30), scale=_MICRO, turns=1),
        BreakKind.NFL_TEAM_TIMEOUT: Ceiling(length=_seconds(30), scale=_MICRO, turns=1),
        BreakKind.NFL_TWO_MINUTE_WARNING: Ceiling(length=_seconds(90), scale=_ORDINARY),
        BreakKind.NFL_QUARTER_BREAK: Ceiling(length=_seconds(90), scale=_ORDINARY),
        BreakKind.NFL_HALFTIME: Ceiling(length=timedelta(minutes=13, seconds=120), scale=_ORDINARY),
        BreakKind.NFL_INJURY_TIMEOUT: Ceiling(length=_seconds(30), scale=_MICRO, turns=1),
        BreakKind.NFL_REVIEW: Ceiling(length=_seconds(36), scale=_MICRO, turns=1),
        BreakKind.NBA_TEAM_TIMEOUT: Ceiling(length=_seconds(60), scale=_MICRO),
        BreakKind.NBA_TV_TIMEOUT: Ceiling(length=_seconds(135), scale=_ORDINARY, national=_seconds(165)),
        BreakKind.NBA_QUARTER_BREAK: Ceiling(length=_seconds(135), scale=_ORDINARY, national=_seconds(195)),
        BreakKind.NBA_HALFTIME: Ceiling(length=_seconds(840), scale=_ORDINARY),
        BreakKind.NBA_BEFORE_OVERTIME: Ceiling(length=_seconds(135), scale=_ORDINARY),
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class LeasePolicy:
    """The ceilings, MLB's trim on a stated inning break, a guest's return margin, and how much of its ceiling a
    break may have used up before it reaches the board and still get a lease."""

    ceilings: Mapping[BreakKind, Ceiling] = CEILINGS
    inning_break_trim: timedelta = _seconds(15)
    return_margin: timedelta = _seconds(6)
    severe_backfill: float = 0.5  # more than this share of the ceiling gone at release: no lease

    def __post_init__(self) -> None:
        if set(self.ceilings) != set(BreakKind):
            raise ValueError("a lease policy has a ceiling for every kind of break")
        if self.inning_break_trim < _ZERO or self.return_margin < _ZERO:
            raise ValueError("a trim and a margin can't be negative")
        if not 0.0 < self.severe_backfill <= 1.0:
            raise ValueError("severe backfill is a share of the ceiling")

    def length(self, evidence: BreakEvidence, *, national: bool = False) -> timedelta | None:
        """A break's ceiling: MLB's inning break as stated less the trim, otherwise the table's (the national
        length on verified national TV); None while a status stands."""
        ceiling = self.ceilings[evidence.kind]
        if evidence.kind is BreakKind.MLB_INNING_BREAK and evidence.stated is not None:
            return max(evidence.stated - self.inning_break_trim, _ZERO)
        if national and ceiling.national is not None:
            return ceiling.national
        return ceiling.length


class LeaseState(StrEnumMixin, str, Enum):
    OPEN = "open"  # within its ceiling, with no sign of play since
    REVOKED = "revoked"  # later released action ended it
    ENDED = "ended"  # the feed marked its end
    LAPSED = "lapsed"  # its ceiling ran out with no sign of play: back to weighted eligibility


class LeaseRefusal(StrEnumMixin, str, Enum):
    """Why evidence opened no lease, or why a guest can't start in one."""

    DUPLICATE = "duplicate"  # the same break again: a repeated row or a correction renews nothing
    ENDED_ON_ARRIVAL = "ended_on_arrival"  # already over when it arrived: a completed ruling, a backfilled pair
    INSIDE_OPEN_BREAK = "inside_open_break"  # a break inside an open lease, which it can't extend
    BACKFILL = "backfill"  # most of its ceiling had passed before it reached the board
    SPENT = "spent"  # all of its ceiling had passed before it reached the board
    NO_BREAK = "no_break"  # the game has no lease
    RESUMED = "resumed"  # released action revoked the lease
    ENDED = "ended"  # the feed marked the break over
    LAPSED = "lapsed"  # the lease ran out
    TURNS_SPENT = "turns_spent"  # the break has hosted all the guests it may
    NO_ROOM = "no_room"  # what's left can't fit the card and the return margin


@dataclass(frozen=True, slots=True)
class LeaseClosure:
    """When a lease closed before its ceiling, and how: revoked by action, or ended by the feed."""

    at: MonotonicTime
    state: LeaseState


@dataclass(frozen=True, slots=True, kw_only=True)
class Lease:
    """One break lent out: anchored at when it began on the viewer's timeline, with its ceiling's end."""

    id: BreakId
    scale: BreakScale
    opened: MonotonicTime  # when the board released the evidence
    anchor: MonotonicTime  # when the break began on the viewer's delayed timeline
    ends: MonotonicTime | None  # the ceiling's end; None while a status stands
    turns: int | None
    provisional: bool  # the onset was uncertain, so one poll interval came off
    guests: int = 0  # guests whose first frame was confirmed in it
    closure: LeaseClosure | None = None

    def state(self, at: MonotonicTime) -> LeaseState:
        if self.closure is not None and at >= self.closure.at:
            return self.closure.state
        if self.ends is not None and at >= self.ends:
            return LeaseState.LAPSED
        return LeaseState.OPEN

    def remaining(self, at: MonotonicTime) -> timedelta | None:
        """What's left of the ceiling at ``at`` (never negative); None while a status stands."""
        return None if self.ends is None else max(self.ends.since(at), _ZERO)

    @property
    def turns_spent(self) -> bool:
        return self.turns is not None and self.guests >= self.turns


@dataclass(frozen=True, slots=True)
class LeaseVerdict:
    """What one piece of evidence did: the lease it opened or ended, or why it did nothing."""

    lease: Lease | None
    refusal: LeaseRefusal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FavoriteMoment:
    """One healthy live favorite at a decision: what it's doing, and the longest guest card its break can host now
    with the return margin kept (zero in action or once its guests are spent; None while an open-ended status
    stands)."""

    activity: FavoriteActivity
    room: timedelta | None = _ZERO

    def __post_init__(self) -> None:
        if self.room is not None and self.room < _ZERO:
            raise ValueError("a break's room can't be negative")
        if self.activity is FavoriteActivity.IN_ACTION and self.room != _ZERO:
            raise ValueError("a favorite in action has no room for a guest")


_IN_ACTION = FavoriteMoment(activity=FavoriteActivity.IN_ACTION)


class LeaseBook:
    """Each game's current break lease, fed with break evidence and released action, asked about guests."""

    def __init__(self, policy: LeasePolicy = LeasePolicy()) -> None:
        self._policy = policy
        self._leases: dict[LeagueScopedId, Lease] = {}
        self._seen: dict[LeagueScopedId, set[BreakId]] = {}

    @property
    def policy(self) -> LeasePolicy:
        return self._policy

    def observe(
        self,
        evidence: BreakEvidence,
        *,
        released: MonotonicTime,
        poll_interval: timedelta,
        delay: timedelta = _ZERO,
        national: bool = False,
    ) -> LeaseVerdict:
        """Take in released break evidence: open a lease, end one the feed marked over, or say why neither.

        ``released`` is when the board released it; ``delay`` is the game's viewer delay, which puts the break's
        onset on the viewer's timeline; ``poll_interval`` is what comes off when the onset is uncertain;
        ``national`` picks a national-TV ceiling where one differs.
        """
        contest = evidence.id.contest
        seen = self._seen.setdefault(contest, set())
        current = self._leases.get(contest)
        if evidence.id in seen:
            if evidence.ended and current is not None and current.id == evidence.id:
                if current.state(released) is LeaseState.OPEN:
                    return LeaseVerdict(self._close(contest, current, released, LeaseState.ENDED))
            return LeaseVerdict(None, LeaseRefusal.DUPLICATE)
        seen.add(evidence.id)
        if evidence.ended:
            return LeaseVerdict(None, LeaseRefusal.ENDED_ON_ARRIVAL)
        if current is not None and current.state(released) is LeaseState.OPEN:
            return LeaseVerdict(None, LeaseRefusal.INSIDE_OPEN_BREAK)
        return self._open(evidence, released=released, poll_interval=poll_interval, delay=delay, national=national)

    def _open(
        self,
        evidence: BreakEvidence,
        *,
        released: MonotonicTime,
        poll_interval: timedelta,
        delay: timedelta,
        national: bool,
    ) -> LeaseVerdict:
        received = evidence.received
        if evidence.onset is not None:
            # On the source's clock; an onset that reads as after its arrival (a skewed clock) counts as fresh.
            age_at_arrival = max(received.utc - evidence.onset, _ZERO)
        else:
            age_at_arrival = poll_interval
        anchor = min(received.monotonic + (delay - age_at_arrival), released)
        length = self._policy.length(evidence, national=national)
        if length is not None:
            age = released.since(anchor)
            if age >= length:
                return LeaseVerdict(None, LeaseRefusal.SPENT)
            if age > length * self._policy.severe_backfill:
                return LeaseVerdict(None, LeaseRefusal.BACKFILL)
        ceiling = self._policy.ceilings[evidence.kind]
        lease = Lease(
            id=evidence.id,
            scale=ceiling.scale,
            opened=released,
            anchor=anchor,
            ends=None if length is None else anchor + length,
            turns=ceiling.turns,
            provisional=evidence.onset is None,
        )
        self._leases[evidence.id.contest] = lease
        return LeaseVerdict(lease)

    def _close(self, contest: LeagueScopedId, lease: Lease, at: MonotonicTime, state: LeaseState) -> Lease:
        closed = replace(lease, closure=LeaseClosure(at, state))
        self._leases[contest] = closed
        return closed

    def action(self, contest: LeagueScopedId, at: MonotonicTime) -> Lease | None:
        """Released action in ``contest`` at ``at``: it revokes a lease released before it. Returns the revoked
        lease, or None when there was nothing to revoke."""
        lease = self._leases.get(contest)
        if lease is None or lease.closure is not None or at <= lease.opened:
            return None
        return self._close(contest, lease, at, LeaseState.REVOKED)

    def lease(self, contest: LeagueScopedId) -> Lease | None:
        """The game's latest lease, whatever its state."""
        return self._leases.get(contest)

    def in_break(self, contest: LeagueScopedId, at: MonotonicTime) -> bool:
        """Whether the game is in a confirmed break with its lease still open."""
        lease = self._leases.get(contest)
        return lease is not None and lease.state(at) is LeaseState.OPEN

    def remaining(self, contest: LeagueScopedId, at: MonotonicTime) -> timedelta | None:
        """What's left of the game's open lease; zero with none open, None while an open-ended status stands."""
        lease = self._leases.get(contest)
        if lease is None or lease.state(at) is not LeaseState.OPEN:
            return _ZERO
        return lease.remaining(at)

    def guest_refusal(self, contest: LeagueScopedId, card: timedelta, at: MonotonicTime) -> LeaseRefusal | None:
        """Why a guest card of length ``card`` can't start now in the game's break, or None when it may."""
        lease = self._leases.get(contest)
        if lease is None:
            return LeaseRefusal.NO_BREAK
        state = lease.state(at)
        if state is not LeaseState.OPEN:
            return {
                LeaseState.REVOKED: LeaseRefusal.RESUMED,
                LeaseState.ENDED: LeaseRefusal.ENDED,
                LeaseState.LAPSED: LeaseRefusal.LAPSED,
            }[state]
        if lease.turns_spent:
            return LeaseRefusal.TURNS_SPENT
        remaining = lease.remaining(at)
        if remaining is not None and remaining < card + self._policy.return_margin:
            return LeaseRefusal.NO_ROOM
        return None

    def may_host(self, contest: LeagueScopedId, card: timedelta, at: MonotonicTime) -> bool:
        return self.guest_refusal(contest, card, at) is None

    def hosted(self, contest: LeagueScopedId, at: MonotonicTime) -> Lease | None:
        """A guest's first frame was confirmed in the game's break: it spends one of the break's turns."""
        lease = self._leases.get(contest)
        if lease is None or lease.state(at) is not LeaseState.OPEN:
            return None
        counted = replace(lease, guests=lease.guests + 1)
        self._leases[contest] = counted
        return counted

    def favorite(self, contest: LeagueScopedId, at: MonotonicTime) -> FavoriteMoment:
        """A live favorite's moment: in action unless its lease is open, else its break's scale and room."""
        lease = self._leases.get(contest)
        if lease is None or lease.state(at) is not LeaseState.OPEN:
            return _IN_ACTION
        remaining = lease.remaining(at)
        if lease.turns_spent:
            room: timedelta | None = _ZERO
        elif remaining is None:
            room = None
        else:
            room = max(remaining - self._policy.return_margin, _ZERO)
        return FavoriteMoment(activity=lease.scale.activity, room=room)

    def next_deadline(self, at: MonotonicTime) -> MonotonicTime | None:
        """The next instant an open lease runs out, when its favorite goes back to weighted eligibility."""
        ends = [
            lease.ends
            for lease in self._leases.values()
            if lease.ends is not None and lease.state(at) is LeaseState.OPEN
        ]
        return min(ends, default=None)

    def forget(self, contest: LeagueScopedId) -> None:
        """Drop a game's leases and the breaks it has seen, once it's over."""
        self._leases.pop(contest, None)
        self._seen.pop(contest, None)
