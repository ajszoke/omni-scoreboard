"""Break leases: how long a favorite's break may be lent to other games (`BOARD_POLICY.md` §5).

A lease is a ceiling, not a promise. It opens on typed break evidence (`omni.domain.breaks.BreakEvidence`) and is
anchored once, on the viewer's delayed timeline, at when the break began there: the release instant pinned for the
answer that showed it, less the break's age when that answer arrived (`omni.events.break_detection.detection_age`).
So ``ends = pinned release + ceiling - age at detection``, wherever the decision that reads it falls:
- the viewer's delay lives only in the pinned release; a decision that takes the evidence in late can't restore the
  time the break had already used;
- a known onset's age is told on the source's own clock, with the answer's measured transport; a local hold (NFL's
  halftime) is aged on this board's clock; with the onset unknown, one poll interval comes off, marked provisional;
- a break more than half of whose ceiling has passed by the decision that reads it (severe backfill) gets no lease,
  and one already over, from a stale game, or of a kind no detector finds gets none either. A refusal carries the
  age, its uncertainty and how much of the ceiling had gone, for the trace.

Only new evidence opens one: a repeated row of the same break, a break already marked over when it arrives (a
completed ruling, a backfilled start and end), or a break merely nested inside an open lease (a pitching change inside
an inning break) opens and extends nothing. A new independent interval of a higher precedence (a rain delay during a
mound visit) supersedes the open lease: the old budget is discarded, and the new one runs from its own onset. Released
action after the break began revokes it, by the break's identity (`resumed`). A frozen clock isn't evidence: the
detectors never report one, and nothing here reads a clock.

A guest needs its whole card plus a return margin inside what's left, and some breaks cap their guests. Its
permission (`LeaseGrant`) names the exact lease, its latest start and its expiry, and is checked again (`holds`) as
the card is handed to the display and when its first frame is confirmed. When a lease runs out with no sign of play,
the favorite goes back to weighted eligibility under the action bar; that's not a claim that play resumed.

Spans run on the monotonic clock; the onset and the answer's time are compared on the source's clock only to date
the break.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import Enum
from types import MappingProxyType

from omni.core.clock import MonotonicTime
from omni.core.enum import StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakAge, BreakEvidence, BreakId, BreakKind
from omni.events.break_detection import DETECTED, detection_age
from omni.policy.interrupts import FavoriteActivity

__all__ = [
    "BreakScale",
    "Ceiling",
    "FavoriteMoment",
    "Lease",
    "LeaseBook",
    "LeaseClosure",
    "LeaseGrant",
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
    """The ceilings, MLB's trim on a stated inning break, a guest's return margin, how much of its ceiling a break
    may have used up by the decision that reads it and still get a lease, and the kinds of break evidence can bring
    (those a detector finds today)."""

    ceilings: Mapping[BreakKind, Ceiling] = CEILINGS
    inning_break_trim: timedelta = _seconds(15)
    return_margin: timedelta = _seconds(6)
    severe_backfill: float = 0.5  # more than this share of the ceiling gone at the decision: no lease
    supported: frozenset[BreakKind] = DETECTED

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

    def supersedes(self, new: BreakKind, standing: BreakKind) -> bool:
        """Whether a new break is an independent interval that replaces the standing one's lease, by precedence: a
        status that stands (open-ended, such as a rain delay) over any break, an ordinary break over a micro one. A
        break of the same precedence or lower is merely nested in the standing one, and adds nothing."""
        return _precedence(self.ceilings[new]) > _precedence(self.ceilings[standing])


def _precedence(ceiling: Ceiling) -> int:
    if ceiling.length is None:
        return 2
    return 1 if ceiling.scale is BreakScale.ORDINARY else 0


class LeaseState(StrEnumMixin, str, Enum):
    OPEN = "open"  # within its ceiling, with no sign of play since
    REVOKED = "revoked"  # released action after the break began ended it
    ENDED = "ended"  # the feed marked its end
    LAPSED = "lapsed"  # its ceiling ran out with no sign of play: back to weighted eligibility
    SUPERSEDED = "superseded"  # a new independent interval replaced it: its budget is discarded


class LeaseRefusal(StrEnumMixin, str, Enum):
    """Why evidence opened no lease, or why a guest can't start in one."""

    DUPLICATE = "duplicate"  # the same break again: a repeated row or a correction renews nothing
    ENDED_ON_ARRIVAL = "ended_on_arrival"  # already over when it arrived: a completed ruling, a backfilled pair
    INSIDE_OPEN_BREAK = "inside_open_break"  # merely nested in an open lease, which it can't extend
    UNSUPPORTED = "unsupported"  # a kind no detector finds: it never enters from guesswork
    STALE = "stale"  # its game's released state isn't current
    BACKFILL = "backfill"  # more than half of its ceiling had passed by the decision that read it
    SPENT = "spent"  # all of its ceiling had passed by the decision that read it
    NO_BREAK = "no_break"  # the game has no lease
    RESUMED = "resumed"  # released action revoked the lease
    ENDED = "ended"  # the feed marked the break over
    LAPSED = "lapsed"  # the lease ran out
    CHANGED = "changed"  # the lease a guest was granted in isn't the game's lease any more
    TURNS_SPENT = "turns_spent"  # the break has hosted all the guests it may
    NO_ROOM = "no_room"  # what's left can't fit the card and the return margin


@dataclass(frozen=True, slots=True)
class LeaseClosure:
    """When a lease closed before its ceiling, and how: revoked by action, ended by the feed, or superseded."""

    at: MonotonicTime
    state: LeaseState


@dataclass(frozen=True, slots=True, kw_only=True)
class Lease:
    """One break lent out: anchored at when it began on the viewer's timeline, with its ceiling's end."""

    id: BreakId
    scale: BreakScale
    opened: MonotonicTime  # when the board took in its released evidence
    anchor: MonotonicTime  # when the break began on the viewer's delayed timeline: its pinned release less its age
    ends: MonotonicTime | None  # the ceiling's end; None while a status stands
    turns: int | None
    age: BreakAge  # how long it had run when its evidence arrived, and how that's known
    guests: int = 0  # guests whose first frame was confirmed in it
    closure: LeaseClosure | None = None

    @property
    def provisional(self) -> bool:
        """Whether its age includes a poll interval for what couldn't be told."""
        return self.age.provisional

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


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseVerdict:
    """What one piece of evidence did: the lease it opened or ended, or why it did nothing; the lease a superseding
    interval closed; and, when it was judged for a lease, its age and how much of its ceiling had passed by the
    decision (both None for a duplicate, or evidence refused before it was dated)."""

    lease: Lease | None
    refusal: LeaseRefusal | None = None
    superseded: Lease | None = None
    age: BreakAge | None = None
    elapsed: timedelta | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseGrant:
    """A guest's permission in one favorite's break: the exact lease, the latest its first frame may land with its
    whole turn and the return margin inside the lease, and when the lease runs out (both None while a status
    stands)."""

    contest: LeagueScopedId
    id: BreakId
    latest_start: MonotonicTime | None
    expires: MonotonicTime | None


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
_CLOSED = MappingProxyType(
    {
        LeaseState.REVOKED: LeaseRefusal.RESUMED,
        LeaseState.ENDED: LeaseRefusal.ENDED,
        LeaseState.LAPSED: LeaseRefusal.LAPSED,
        LeaseState.SUPERSEDED: LeaseRefusal.CHANGED,
    }
)


class LeaseBook:
    """Each game's current break lease, fed with break evidence and released action, asked about guests."""

    def __init__(self, policy: LeasePolicy = LeasePolicy()) -> None:
        self._policy = policy
        self._leases: dict[LeagueScopedId, Lease] = {}
        self._past: dict[LeagueScopedId, dict[BreakId, Lease]] = {}  # each game's leases since replaced
        self._seen: dict[LeagueScopedId, set[BreakId]] = {}

    @property
    def policy(self) -> LeasePolicy:
        return self._policy

    def observe(
        self,
        evidence: BreakEvidence,
        *,
        at: MonotonicTime,
        poll_interval: timedelta,
        national: bool = False,
        fresh: bool = True,
    ) -> LeaseVerdict:
        """Take in released break evidence at the decision ``at``: open a lease, end one the feed marked over,
        supersede one, or say why none.

        ``poll_interval`` stands in for an age that can't be told; ``national`` picks a national-TV ceiling where one
        differs; ``fresh`` says the game's released state is current.
        """
        contest = evidence.id.contest
        seen = self._seen.setdefault(contest, set())
        current = self._leases.get(contest)
        if evidence.id in seen:
            if evidence.ended and current is not None and current.id == evidence.id:
                if current.state(at) is LeaseState.OPEN:
                    return LeaseVerdict(lease=self._close(current, at, LeaseState.ENDED))
            return LeaseVerdict(lease=None, refusal=LeaseRefusal.DUPLICATE)
        seen.add(evidence.id)
        refusal = self._screen(evidence, fresh=fresh)
        if refusal is not None:
            return LeaseVerdict(lease=None, refusal=refusal)
        standing = current if current is not None and current.state(at) is LeaseState.OPEN else None
        if standing is not None and not self._policy.supersedes(evidence.kind, standing.id.kind):
            return LeaseVerdict(lease=None, refusal=LeaseRefusal.INSIDE_OPEN_BREAK)
        superseded = self._close(standing, at, LeaseState.SUPERSEDED) if standing is not None else None
        verdict = self._open(evidence, at=at, poll_interval=poll_interval, national=national)
        return replace(verdict, superseded=superseded)

    def _screen(self, evidence: BreakEvidence, *, fresh: bool) -> LeaseRefusal | None:
        """Evidence refused at once: already over, of a kind no detector finds, or from a stale game."""
        if evidence.ended:
            return LeaseRefusal.ENDED_ON_ARRIVAL
        if evidence.kind not in self._policy.supported:
            return LeaseRefusal.UNSUPPORTED
        return None if fresh else LeaseRefusal.STALE

    def _open(
        self, evidence: BreakEvidence, *, at: MonotonicTime, poll_interval: timedelta, national: bool
    ) -> LeaseVerdict:
        age = detection_age(evidence, poll_interval=poll_interval)
        # Without a pinned release (evidence no delay held), its receipt stands in: the earliest it could show.
        pinned = evidence.released if evidence.released is not None else evidence.received.monotonic
        anchor = min(pinned + -age.age, at)
        length = self._policy.length(evidence, national=national)
        elapsed = at.since(anchor)
        if length is not None:
            refusal = None
            if elapsed >= length:
                refusal = LeaseRefusal.SPENT
            elif elapsed > length * self._policy.severe_backfill:
                refusal = LeaseRefusal.BACKFILL
            if refusal is not None:
                return LeaseVerdict(lease=None, refusal=refusal, age=age, elapsed=elapsed)
        ceiling = self._policy.ceilings[evidence.kind]
        lease = Lease(
            id=evidence.id,
            scale=ceiling.scale,
            opened=at,
            anchor=anchor,
            ends=None if length is None else anchor + length,
            turns=ceiling.turns,
            age=age,
        )
        self._replace(lease)
        return LeaseVerdict(lease=lease, age=age, elapsed=elapsed)

    def _replace(self, lease: Lease) -> None:
        """Make ``lease`` its game's current one, keeping the one it replaces for the guests granted in it."""
        contest = lease.id.contest
        current = self._leases.get(contest)
        if current is not None and current.id != lease.id:
            self._past.setdefault(contest, {})[current.id] = current
        self._leases[contest] = lease

    def _close(self, lease: Lease, at: MonotonicTime, state: LeaseState) -> Lease:
        closed = replace(lease, closure=LeaseClosure(at, state))
        self._leases[lease.id.contest] = closed
        return closed

    def resumed(self, contest: LeagueScopedId, closes: Collection[BreakId], at: MonotonicTime) -> Lease | None:
        """Released action in ``contest``, which closes the breaks ``closes`` names (each it came after): it revokes
        the game's lease if that's one of them and nothing has closed it yet. One that ran out first is revoked too,
        recording that play resumed. Returns the revoked lease, or None."""
        lease = self._leases.get(contest)
        if lease is None or lease.id not in closes or lease.closure is not None:
            return None
        return self._close(lease, at, LeaseState.REVOKED)

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
        return LeaseRefusal.NO_BREAK if lease is None else self._room(lease, card, at)

    def _room(self, lease: Lease, card: timedelta, at: MonotonicTime) -> LeaseRefusal | None:
        state = lease.state(at)
        if state is not LeaseState.OPEN:
            return _CLOSED[state]
        if lease.turns_spent:
            return LeaseRefusal.TURNS_SPENT
        remaining = lease.remaining(at)
        if remaining is not None and remaining < card + self._policy.return_margin:
            return LeaseRefusal.NO_ROOM
        return None

    def may_host(self, contest: LeagueScopedId, card: timedelta, at: MonotonicTime) -> bool:
        return self.guest_refusal(contest, card, at) is None

    def grant(self, contest: LeagueScopedId, card: timedelta) -> LeaseGrant:
        """A guest card of length ``card``'s permission in the game's current lease, once a decision has found the
        break may host it: the lease, the latest its first frame may land, and when the lease runs out."""
        lease = self._leases[contest]
        latest = None if lease.ends is None else lease.ends + -(card + self._policy.return_margin)
        return LeaseGrant(contest=contest, id=lease.id, latest_start=latest, expires=lease.ends)

    def holds(self, grant: LeaseGrant, card: timedelta, at: MonotonicTime) -> LeaseRefusal | None:
        """Whether a guest's permission still holds at ``at`` (None) or why not: the game's lease must be the very
        one granted, open, with a turn left and room for the whole card and the return margin."""
        lease = self._leases.get(grant.contest)
        if lease is None:
            return LeaseRefusal.NO_BREAK
        if lease.id != grant.id:
            return LeaseRefusal.CHANGED
        return self._room(lease, card, at)

    def closed(self, grant: LeaseGrant) -> MonotonicTime | None:
        """When the lease a guest was granted in closed before its ceiling (play resumed, the feed marked its end, or
        a new interval superseded it); None while it hasn't, or once its game is forgotten."""
        lease = self._leases.get(grant.contest)
        if lease is not None and lease.id != grant.id:
            lease = self._past.get(grant.contest, {}).get(grant.id)
        return lease.closure.at if lease is not None and lease.closure is not None else None

    def hosted(self, contest: LeagueScopedId, at: MonotonicTime, id: BreakId | None = None) -> Lease | None:
        """A guest's first frame was confirmed in the game's break (in the lease ``id`` names, when given): it spends
        one of the break's turns."""
        lease = self._leases.get(contest)
        if lease is None or lease.state(at) is not LeaseState.OPEN or (id is not None and lease.id != id):
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
        self._past.pop(contest, None)
        self._seen.pop(contest, None)
