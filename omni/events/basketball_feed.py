"""Basketball's events from a feed with no play-by-play: one game's reads, in arrival order, as the captions a board
tells and the timeouts a break planner needs.

The feed gives two kinds of read (`omni.domain.basketball_reads`). A lines read holds every player's box-score line,
so a play shows only as a change between one lines read and the next. A slate read holds the game's state, so a
timeout or a challenge shows only as a change in a side's timeouts left. `BasketballFeedDeriver` takes one game's
reads as they arrive and answers each with a `FeedUpdate`: the captions it can now tell, each with the read that
first showed its play; the timeouts' onsets and ends; and what it is holding back, and why.

It keeps no clock of its own; every moment it reports is a read's arrival. Between reads it keeps:

- **the last lines**: each player's line by key. The first lines read is the baseline, so plays before it are old
  news; after that, a player's first line is a change from nothing (a substitute who enters with a foul fouled).
- **the game clock as last read**, from either kind of read, with the arrival of the first read that showed it
  there. A reading further on (a lower clock, a later period, a break) means the clock ran; a reading back from it
  (a read lagging another, or a correction upward) is set aside. While the reading stands, play is stopped: that
  span is the stoppage.
- **each side's timeouts left** (with the period it was last read in), the timeouts still open, and the count of
  timeouts so far in each period. A side's count down by one is a timeout: its length class is `timeout_length` of
  its order in the period (both teams counted), and its onset is the first read at the clock it stopped on. It ends
  when the clock runs again. A drop at a period's full clock, or in a break, falls in the break; a drop by two is a
  correction. Neither is a timeout, nor counted in the period's order.
- **the league's caps on what a team may hold.** The feed applies them by lowering the count, and the share of a
  drop down to a cap is never a timeout; only a fall below the cap is. A team holds at most 4 for the fourth
  quarter: a count first read in it, lower than in the third, comes down to 4 there (at its full clock, the break
  rule already covers it). And at most 2 after its 3:00 mark: once any read has shown the fourth quarter's clock at
  3:00 or under, a team holding more than 2 comes down to 2 at the next stoppage, whenever the feed posts it. An
  overtime brings a fresh allotment, so the count a side is first read with in one is its new baseline, never a
  timeout or a won challenge.
- **each side's drop in this stoppage.** A count back up while the clock is still stopped is a won challenge; a
  count back later is a correction. A won challenge names the call change when a line shows one in its stoppage,
  waiting one lines read for it.
- **the open trip to the line**: one side's free throws since the first, each shooter's with whether a foul by the
  other side covers them. A trip ends at a basket (by either team), a turnover, the other side's free throw, the
  next period, or once 20 s of game clock has run since its first free throw. The stat rows' clock can move before
  a trip's last free throw posts, so a running clock alone doesn't end one.
- **the fouls pending**: each personal foul (not an offensive one) waits to be claimed by the other side's trip
  until a later basket, the next period, or 20 s of game clock.
- **a basket waiting for its assist**, for one lines read, and the last basket made, for an and-one.
- **the slates since the last lines read**, which can order two teams' baskets in the next one.

How the captions are told:

- **A basket** is a line's made field goals up by one: a three when its threes went up too. A teammate's assist in
  the same read names the assister when the team has exactly one basket and one assist there; with no assist, the
  basket waits one lines read for a late one. A basket taken back is no caption; one moved to a teammate in the
  same read is no new basket, though a basket still waiting for its assist moves with it.
- **A free throw** that lands alone in its read is told as it lands (`FreeThrow`), numbered among its shooter's
  free throws on the trip. Once two of a trip's free throws land in one read, the whole trip is held and told when
  it ends, one `FreeThrows` a shooter, counting every free throw of the trip, those told already among them.
- **A technical** has no foul by the other side behind its free throw. A shooter with no foul to claim and no
  basket just before (an and-one) waits for the next lines read in which a count changed: a foul turning up covers
  them; a second free throw makes them a foul's shooter whose foul didn't post (or, when another shooter on the
  trip has one free throw and the only foul, that other shooter took the technical); a lone free throw is a
  technical's (`TechnicalFoul`), charged to the other side. A lone free throw that follows its side's last trip,
  by the shooter who had just one free throw under a foul on it (a foul gives two or three), is that trip's, posted
  late. A flagrant reads like a common foul in the bonus, so none is told.
- **A personal foul** is a line's fouls up by one. One down is a call taken back (`CallReversed`): alone, with a
  block added, or with a turnover taken off. Other counts corrected down are no caption.
- **Two plays in one read** are ordered by a slate caught between them that shows one team's baskets and not the
  other's; then a foul before the free throws it gave; then the order the feed sent the rows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum, IntEnum

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway, StrEnumMixin
from omni.domain.basketball import (
    REGULATION_PERIODS,
    TIMEOUTS_PER_GAME,
    BasketballGameState,
    BasketballPhase,
    BySide,
    CallChange,
    GameClock,
    PlayerCallChange,
    PlayerLine,
    TimeoutLength,
    timeout_length,
)
from omni.domain.basketball_reads import BasketballRead, LinesRead, PlayerKey, RosterLine, SlateRead
from omni.domain.football import other_side
from omni.events.basketball import (
    BasketballCaption,
    CallReversed,
    ChallengeWon,
    FieldGoalMade,
    FreeThrow,
    FreeThrows,
    PersonalFoul,
    ShotValue,
    TechnicalFoul,
    TimeoutTaken,
)

__all__ = [
    "FOURTH_QUARTER_CAP",
    "LATE_CAP",
    "LATE_CAP_CLOCK",
    "TRIP_WINDOW_TENTHS",
    "BasketballFeedDeriver",
    "FeedCaption",
    "FeedUpdate",
    "HeldCaption",
    "HeldReason",
    "TimeoutEnd",
    "TimeoutOnset",
]

TRIP_WINDOW_TENTHS = 200  # 20 s of game clock: a trip, a pending foul or an and-one reaches no further
FOURTH_QUARTER_CAP = 4  # the most timeouts a team may hold for the fourth quarter
LATE_CAP = 2  # the most a team may hold once the fourth quarter's clock reaches LATE_CAP_CLOCK
LATE_CAP_CLOCK = 3 * 60 * 10  # 3:00, in tenths
_SIDES = (HomeAway.AWAY, HomeAway.HOME)
_REGULATION_CLOCK = 12 * 60 * 10  # a regulation period's full clock, in tenths
_OVERTIME_CLOCK = 5 * 60 * 10
_BREAK = _REGULATION_CLOCK + 2  # a break after a period, as progress through it: past any running clock
_COUNTS = (
    "fg_made",
    "fg_attempted",
    "threes_made",
    "threes_attempted",
    "ft_made",
    "ft_attempted",
    "offensive_rebounds",
    "defensive_rebounds",
    "assists",
    "steals",
    "blocks",
    "turnovers",
    "fouls",
)


# --- what an update carries ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedCaption:
    """A caption the board can tell, with the read that first showed its play: its arrival, and the game's period
    and clock as that read had them (None when it showed no running clock)."""

    caption: BasketballCaption
    seen_at: MonotonicTime
    period: int
    clock: GameClock | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeoutOnset:
    """A timeout, as a break planner needs it: the side, its length class, and when it began.

    ``onset`` is the arrival of the first read that showed the clock stopped where the timeout came; ``seen_at``
    the arrival of the slate read that showed the count drop.
    """

    side: HomeAway
    length: TimeoutLength
    onset: MonotonicTime
    seen_at: MonotonicTime
    period: int
    clock: GameClock | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeoutEnd:
    """A timeout over: the arrival of the first read that showed the clock running again (or the period over)."""

    side: HomeAway
    onset: MonotonicTime
    ended_at: MonotonicTime


class HeldReason(StrEnumMixin, str, Enum):
    """Why a caption is waiting."""

    ASSIST = "assist"  # a basket waiting one lines read for a late assist
    FOUL_CHECK = "foul_check"  # a free throw with no foul behind it yet: a technical's, unless a foul turns up
    FREE_THROW_GROUP = "free_throw_group"  # a trip whose free throws landed together, held whole until it ends
    CHALLENGE = "challenge"  # a won challenge waiting one lines read for its call change


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldCaption:
    """A caption being held, as it would read if it were told now."""

    reason: HeldReason
    caption: BasketballCaption
    seen_at: MonotonicTime


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedUpdate:
    """What one read told: the captions now known, in the order to tell them; the timeouts begun and ended; and
    everything still held."""

    at: MonotonicTime
    captions: tuple[FeedCaption, ...] = ()
    timeouts: tuple[TimeoutOnset | TimeoutEnd, ...] = ()
    held: tuple[HeldCaption, ...] = ()


# --- what the deriver keeps ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Seen:
    """Where a play was first seen: its read's arrival and game clock, and its rank among that read's plays."""

    at: MonotonicTime
    period: int
    clock: GameClock | None
    rank: float = 0.0

    @property
    def tenths(self) -> int | None:
        return self.clock.tenths if self.clock is not None else None


@dataclass(frozen=True, slots=True)
class _Reading:
    """The game clock as last read: the period, the tenths left (None at the break after it), and the arrival of
    the first read that showed it."""

    period: int
    tenths: int | None
    first_at: MonotonicTime

    @property
    def progress(self) -> tuple[int, int]:
        return self.period, _BREAK if self.tenths is None else _REGULATION_CLOCK - self.tenths


@dataclass(slots=True)
class _Change:
    """One player's line against their last: how each count moved."""

    roster: RosterLine
    row: int
    fg: int
    threes: int
    fta: int
    ftm: int
    fouls: int
    assists: int
    turnovers: int
    blocks: int
    moved: bool

    @classmethod
    def of(cls, row: int, roster: RosterLine, before: PlayerLine | None) -> _Change:
        line = roster.line
        delta = {name: getattr(line, name) - (getattr(before, name) if before is not None else 0) for name in _COUNTS}
        return cls(
            roster=roster,
            row=row,
            fg=delta["fg_made"],
            threes=delta["threes_made"],
            fta=delta["ft_attempted"],
            ftm=delta["ft_made"],
            fouls=delta["fouls"],
            assists=delta["assists"],
            turnovers=delta["turnovers"],
            blocks=delta["blocks"],
            moved=any(delta.values()),
        )

    @property
    def key(self) -> PlayerKey:
        return self.roster.key

    @property
    def side(self) -> HomeAway:
        return self.roster.side

    @property
    def name(self) -> str:
        return self.roster.line.name


@dataclass(slots=True)
class _Basket:
    """A made basket waiting one lines read for its assist."""

    side: HomeAway
    shooter: PlayerKey
    name: str
    value: ShotValue
    seen: _Seen

    def caption(self, assister: str | None = None) -> FieldGoalMade:
        return FieldGoalMade(side=self.side, shooter=self.name, value=self.value, assister=assister)


@dataclass(frozen=True, slots=True)
class _LastBasket:
    shooter: PlayerKey
    period: int
    tenths: int | None


@dataclass(slots=True)
class _Foul:
    """A personal foul waiting to be claimed by the other side's trip to the line."""

    side: HomeAway  # the side charged
    seen: _Seen


class _Cover(Enum):
    """What accounts for a shooter's free throws on a trip."""

    FOUL = "foul"  # a foul by the other side
    AND_ONE = "and_one"  # the shooter's own basket just before
    NO_FOUL = "no_foul"  # no foul turned up, but more than one free throw: a foul's that didn't post
    TECHNICAL = "technical"  # a lone free throw with nothing behind it
    UNKNOWN = "unknown"  # no foul yet: waiting for the next lines read with a count changed


@dataclass(slots=True)
class _Shot:
    made: bool
    seen: _Seen
    game_made: int  # the shooter's free throws for the game, after this one
    game_attempted: int
    told: bool = False


@dataclass(slots=True)
class _Shooter:
    side: HomeAway
    key: PlayerKey
    name: str
    cover: _Cover
    since: int  # how many lines reads with a change there had been when the shooter's first free throw landed
    shots: list[_Shot] = field(default_factory=list)
    foul: _Foul | None = None
    technical: _Shot | None = None  # the technical's free throw, once the shooter is known to have taken it
    technical_told: bool = False

    @property
    def free_throws(self) -> list[_Shot]:
        """The shooter's free throws on the trip, the technical's left out."""
        return [shot for shot in self.shots if shot is not self.technical]

    def technical_foul(self) -> TechnicalFoul:
        shot = self.technical
        assert shot is not None
        return TechnicalFoul(
            side=other_side(self.side),
            shooter=self.name,
            made=shot.made,
            game_made=shot.game_made,
            game_attempted=shot.game_attempted,
        )

    def free_throw(self, shot: _Shot) -> FreeThrow:
        return FreeThrow(
            side=self.side,
            shooter=self.name,
            place=self.free_throws.index(shot) + 1,
            made=shot.made,
            game_made=shot.game_made,
            game_attempted=shot.game_attempted,
        )

    def group(self) -> FreeThrows:
        """The shooter's free throws on the trip as one, against their line at the last of them.

        The line is the word on the game: when the scorer has corrected a free throw on the trip since it landed
        (a miss made good, or an attempt taken off), the trip's count gives way to it.
        """
        shots = self.free_throws
        game_made, game_attempted = shots[-1].game_made, shots[-1].game_attempted
        attempted = min(len(shots), game_attempted)
        made = max(
            min(sum(shot.made for shot in shots), attempted, game_made), attempted - (game_attempted - game_made)
        )
        return FreeThrows(
            side=self.side,
            shooter=self.name,
            made=made,
            attempted=attempted,
            game_made=game_made,
            game_attempted=game_attempted,
        )


@dataclass(slots=True)
class _Trip:
    """One side's trip to the line: from its first free throw until something ends it."""

    side: HomeAway
    period: int
    tenths: int | None  # the clock its first free throw's read carried
    shooters: dict[PlayerKey, _Shooter] = field(default_factory=dict)
    grouped: bool = False


@dataclass(slots=True)
class _NewShots:
    """A player's free throws that landed in this read."""

    change: _Change
    shots: list[_Shot]


@dataclass(slots=True)
class _Challenge:
    """A won challenge waiting one lines read for its call change."""

    side: HomeAway
    seen: _Seen


@dataclass(frozen=True, slots=True)
class _OpenTimeout:
    side: HomeAway
    onset: MonotonicTime


class _Play(IntEnum):
    """A play's kind, in the order a player's own plays in one read are told."""

    REVERSAL = 0
    BASKET = 1
    SHOT = 2
    FOUL = 3


_SortKey = tuple[float, float, float, float]
_SeenFor = Callable[[_Play, PlayerKey, int], _Seen]  # where a read's play was seen, by its kind, player and number


class _Out:
    """The captions and timeouts one read tells, put in the order to tell them."""

    def __init__(self) -> None:
        self._captions: list[tuple[_SortKey, FeedCaption]] = []
        self.timeouts: list[TimeoutOnset | TimeoutEnd] = []

    def tell(self, caption: BasketballCaption, seen: _Seen, *, after: _Seen | None = None) -> None:
        """Tell ``caption``, seen at ``seen``: after ``after`` (its foul) when that was seen later."""
        key: _SortKey = (seen.at.seconds, seen.rank, 0.0, 0.0)
        if after is not None and (after.at.seconds, after.rank) >= (seen.at.seconds, seen.rank):
            key = (after.at.seconds, after.rank + 0.5, seen.at.seconds, seen.rank)
        told = FeedCaption(caption=caption, seen_at=seen.at, period=seen.period, clock=seen.clock)
        self._captions.append((key, told))

    def update(self, at: MonotonicTime, held: Sequence[HeldCaption]) -> FeedUpdate:
        ordered = tuple(caption for _, caption in sorted(self._captions, key=lambda item: item[0]))
        return FeedUpdate(at=at, captions=ordered, timeouts=tuple(self.timeouts), held=tuple(held))


def _foul_seen(shooter: _Shooter) -> _Seen | None:
    return shooter.foul.seen if shooter.foul is not None else None


def _full_clock(period: int) -> int:
    return _OVERTIME_CLOCK if period > REGULATION_PERIODS else _REGULATION_CLOCK


def _in_break(state: BasketballGameState) -> bool:
    """Whether the slate shows the game between periods: a break, or a period's clock not yet started."""
    if state.phase is not BasketballPhase.LIVE:
        return True
    return state.clock is not None and state.clock.tenths == _full_clock(state.period)


def _team_points(players: Iterable[RosterLine]) -> BySide[int]:
    lines = list(players)
    return BySide(
        sum(roster.line.points for roster in lines if roster.side is HomeAway.AWAY),
        sum(roster.line.points for roster in lines if roster.side is HomeAway.HOME),
    )


def _first_to_score(slates: Sequence[BasketballGameState], base: BySide[int], points: BySide[int]) -> HomeAway | None:
    """The side whose baskets came first, when both scored in one read and a slate between shows one side's and
    not the other's; None when nothing tells."""
    if not (points.away and points.home):
        return None
    firsts = {
        side
        for state in slates
        for side in _SIDES
        if state.side(side).score >= base[side] + points[side]
        and state.side(other_side(side)).score < base[other_side(side)] + points[other_side(side)]
    }
    return firsts.pop() if len(firsts) == 1 else None


def _reversal(change: _Change) -> CallChange | None:
    """How a line took a call back, if it did: one foul fewer, alone, with a block more, or a turnover fewer."""
    if change.fouls != -1:
        return None
    if change.blocks > 0:
        return CallChange.FOUL_TO_BLOCK
    if change.turnovers < 0:
        return CallChange.OFFENSIVE_FOUL_OFF
    return CallChange.FOUL_OFF


# --- the deriver --------------------------------------------------------------------------------------------


class BasketballFeedDeriver:
    """One game's reads in, in arrival order; what each one told out."""

    def __init__(self) -> None:
        self._players: dict[PlayerKey, RosterLine] | None = None
        self._left: dict[HomeAway, tuple[int, int]] = {}  # each side's count, and the period it was read in
        self._late_cap = False  # the fourth quarter's clock has been read at 3:00 or under
        self._reading: _Reading | None = None
        self._open: list[_OpenTimeout] = []
        self._timeouts_in: dict[int, int] = {}
        self._midway: set[int] = set()  # periods first read after they began: their earlier timeouts are unknown
        self._drops: set[HomeAway] = set()
        self._reversals: list[tuple[HomeAway, PlayerCallChange]] = []  # calls taken back in this stoppage
        self._challenge: _Challenge | None = None
        self._trip: _Trip | None = None
        self._last_trip: _Trip | None = None
        self._fouls: list[_Foul] = []
        self._waiting: list[_Basket] = []
        self._last_basket: _LastBasket | None = None
        self._slates: list[BasketballGameState] = []
        self._final = False
        self._lines_reads = 0
        self._changed_reads = 0

    def read(self, read: BasketballRead) -> FeedUpdate:
        """Take the game's next read, and say what it told."""
        out = _Out()
        if isinstance(read, SlateRead):
            self._slate(read, out)
        else:
            self._lines(read, out)
        return out.update(read.at, self._held())

    # --- the clock ------------------------------------------------------------------------------------------

    def _track(self, at: MonotonicTime, period: int, tenths: int | None, out: _Out) -> None:
        """Note a read's clock; a reading further on than the last means the clock ran."""
        reading = _Reading(period, tenths, at)
        if period == REGULATION_PERIODS and tenths is not None and tenths <= LATE_CAP_CLOCK:
            self._late_cap = True
        if self._reading is not None and reading.progress <= self._reading.progress:
            return  # still stopped here, or a read behind the last
        ran = self._reading is not None
        if not ran and tenths is not None and tenths < _full_clock(period):
            self._midway.add(period)
        self._reading = reading
        if ran:
            self._clock_ran(at, out)

    def _clock_ran(self, at: MonotonicTime, out: _Out) -> None:
        """The stoppage is over: its timeouts end, a challenge still waiting is told, and what has aged out goes."""
        out.timeouts.extend(TimeoutEnd(side=open.side, onset=open.onset, ended_at=at) for open in self._open)
        self._open.clear()
        self._drops.clear()
        self._reversals.clear()
        if self._challenge is not None:
            self._tell_challenge(None, out)
        self._fouls = [foul for foul in self._fouls if not self._beyond(foul.seen.period, foul.seen.tenths)]
        if self._trip is not None and self._beyond(self._trip.period, self._trip.tenths):
            self._close(out)

    def _beyond(self, period: int, tenths: int | None) -> bool:
        """Whether the clock as last read has left ``period``, or run more than the window past ``tenths``."""
        reading = self._reading
        assert reading is not None
        if reading.period != period:
            return True
        return tenths is not None and reading.tenths is not None and tenths - reading.tenths > TRIP_WINDOW_TENTHS

    # --- slate reads: timeouts and challenges ---------------------------------------------------------------

    def _slate(self, read: SlateRead, out: _Out) -> None:
        state = read.state
        self._final = state.phase is BasketballPhase.FINAL
        if state.phase is not BasketballPhase.LIVE:
            self._track(read.at, state.period, None, out)
        elif state.clock is not None:
            self._track(read.at, state.period, state.clock.tenths, out)
        self._slates.append(state)
        seen = _Seen(read.at, state.period, state.clock)
        if not self._left and state.period == 1 and state.away.timeouts is not None and state.home.timeouts is not None:
            # Every timeout so far in the game came in the first period: the counts give its order.
            self._timeouts_in[1] = 2 * TIMEOUTS_PER_GAME - state.away.timeouts - state.home.timeouts
            self._midway.discard(1)
        for side in _SIDES:
            left = state.side(side).timeouts
            if left is None:
                continue
            last = self._left.get(side)
            self._left[side] = (left, state.period)
            if last is None or (state.period > REGULATION_PERIODS and last[1] < state.period):
                continue  # a first count, or an overtime's fresh allotment: the new baseline
            before = last[0]
            if left < before and not _in_break(state):
                cap = self._cap(state.period, crossed=last[1] < state.period)
                capped = min(before, cap) if cap is not None else before  # the cap's share is no timeout
                if capped - left == 1:
                    self._timeout(side, left, seen, out)
            elif left == before + 1 and side in self._drops:
                self._drops.discard(side)
                if self._challenge is not None:
                    self._tell_challenge(None, out)  # another side's, still waiting: it goes as it is
                self._challenge = _Challenge(side, seen)
                change = self._change_for(side, self._reversals)
                if change is not None:
                    self._tell_challenge(change, out)
            # Anything else is the scorer's: a drop in the break, a drop to a cap, a drop by two past one, or a count
            # back after the stoppage.

    def _cap(self, period: int, *, crossed: bool) -> int | None:
        """The most a team may hold here: 2 once the fourth quarter's 3:00 mark is reached, 4 for a count first read
        in the fourth quarter; None where no cap applies."""
        if period != REGULATION_PERIODS:
            return None
        if self._late_cap:
            return LATE_CAP
        return FOURTH_QUARTER_CAP if crossed else None

    def _timeout(self, side: HomeAway, left: int, seen: _Seen, out: _Out) -> None:
        nth = self._timeouts_in.get(seen.period, 0) + 1
        self._timeouts_in[seen.period] = nth
        onset = self._reading.first_at if self._reading is not None else seen.at
        # In a period first read midway, the timeout's order is unknown: it's taken as short, since a long break
        # the board can't vouch for would hold it through play.
        length = TimeoutLength.SHORT if seen.period in self._midway else timeout_length(seen.period, nth)
        out.tell(TimeoutTaken(side=side, left=left), seen)
        out.timeouts.append(
            TimeoutOnset(
                side=side,
                length=length,
                onset=onset,
                seen_at=seen.at,
                period=seen.period,
                clock=seen.clock,
            )
        )
        self._open.append(_OpenTimeout(side, onset))
        self._drops.add(side)

    @staticmethod
    def _change_for(side: HomeAway, reversals: Sequence[tuple[HomeAway, PlayerCallChange]]) -> PlayerCallChange | None:
        """The latest call taken back on one of ``side``'s players: the call a coach would challenge."""
        changes = [change for player_side, change in reversals if player_side is side]
        return changes[-1] if changes else None

    def _tell_challenge(self, change: PlayerCallChange | None, out: _Out) -> None:
        challenge = self._challenge
        assert challenge is not None
        self._challenge = None
        out.tell(ChallengeWon(side=challenge.side, change=change), challenge.seen)

    # --- lines reads: plays -----------------------------------------------------------------------------------

    def _lines(self, read: LinesRead, out: _Out) -> None:
        self._track(read.at, read.period, read.clock.tenths if read.clock is not None else None, out)
        slates, self._slates = self._slates, []
        before = self._players
        self._players = {**(before or {}), **{roster.key: roster for roster in read.lines}}
        if before is None:
            return  # the baseline: what the lines hold already happened before the board was watching
        self._lines_reads += 1
        changes = [
            _Change.of(row, roster, prior.line if (prior := before.get(roster.key)) is not None else None)
            for row, roster in enumerate(read.lines)
        ]
        if any(change.moved for change in changes):
            self._changed_reads += 1
        self._moves(changes)
        reversals = [(change, kind) for change in changes if (kind := _reversal(change)) is not None]
        points = BySide(
            sum(2 * c.fg + max(c.threes, 0) for c in changes if c.side is HomeAway.AWAY and c.fg > 0),
            sum(2 * c.fg + max(c.threes, 0) for c in changes if c.side is HomeAway.HOME and c.fg > 0),
        )
        first = _first_to_score(slates, _team_points(before.values()), points)
        ranks = self._ranks(changes, reversals, first)

        def seen(kind: _Play, key: PlayerKey, n: int = 0) -> _Seen:
            return _Seen(read.at, read.period, read.clock, ranks[(kind, key, n)])

        scorers = {change.key for change in changes if change.fg > 0}
        self._assists(changes, scorers, out)
        self._baskets(changes, seen, out)
        if scorers:
            self._fouls.clear()  # a basket since a foul ends its chance of free throws
        for change in changes:
            for n in range(max(change.fouls, 0)):
                count = change.roster.line.fouls - change.fouls + n + 1
                foul_seen = seen(_Play.FOUL, change.key, n)
                out.tell(PersonalFoul(side=change.side, player=change.name, count=count), foul_seen)
                if change.turnovers <= 0:  # an offensive foul gives no free throws
                    self._fouls.append(_Foul(change.side, foul_seen))
        for change, kind in reversals:
            player_change = PlayerCallChange(change=kind, player=change.name)
            if self._challenge is not None and self._challenge.side is change.side:
                self._tell_challenge(player_change, out)
            else:
                out.tell(CallReversed(change=player_change), seen(_Play.REVERSAL, change.key))
            self._reversals.append((change.side, player_change))
        if self._challenge is not None:
            self._tell_challenge(None, out)  # its one lines read is up
        shots = [
            _NewShots(change, self._shots(change, [seen(_Play.SHOT, change.key, n) for n in range(change.fta)]))
            for change in changes
            if change.fta > 0
        ]
        ends = bool(scorers) or any(change.turnovers > 0 for change in changes)
        self._free_throws(shots, scorers, ends, read, out)
        if self._final:
            self._flush(out)

    def _flush(self, out: _Out) -> None:
        """The game is over, so no read is coming to settle what waits: tell it as it stands."""
        for basket in self._waiting:
            out.tell(basket.caption(), basket.seen)
        self._waiting.clear()
        if self._trip is not None:
            self._close(out)

    def _moves(self, changes: list[_Change]) -> None:
        """Take out credit moved from one player to another, so it reads as no new play.

        A basket moved to a teammate is no new basket; a basket still waiting for its assist moves with it. Free
        throws moved to another player are no new free throws.
        """
        for loser in (change for change in changes if change.fg < 0):
            gainer = next((c for c in changes if c.side is loser.side and c.fg > 0), None)
            waiting = next((basket for basket in self._waiting if basket.shooter == loser.key), None)
            if gainer is None:
                if waiting is not None:
                    self._waiting.remove(waiting)  # taken back before it was told
                continue
            if waiting is not None:
                waiting.shooter, waiting.name = gainer.key, gainer.name
                waiting.value = ShotValue.THREE if gainer.threes > 0 else ShotValue.TWO
            if self._last_basket is not None and self._last_basket.shooter == loser.key:
                self._last_basket = _LastBasket(gainer.key, self._last_basket.period, self._last_basket.tenths)
            gainer.fg -= 1
            gainer.threes -= 1 if gainer.threes > 0 and loser.threes < 0 else 0
        for loser in (change for change in changes if change.fta < 0):
            gainer = next((c for c in changes if c.fta == -loser.fta and c.ftm == -loser.ftm), None)
            if gainer is not None:
                gainer.fta = gainer.ftm = 0

    def _ranks(
        self, changes: Sequence[_Change], reversals: Sequence[tuple[_Change, CallChange]], first: HomeAway | None
    ) -> dict[tuple[_Play, PlayerKey, int], float]:
        """Each of the read's plays' place among them: by the feed's row order, a player's own plays in `_Play`'s
        order, except that a side a slate caught scoring first has its baskets told first."""
        plays: list[tuple[int, _Play, int, _Change]] = []
        for change in changes:
            plays += [(change.row, _Play.BASKET, n, change) for n in range(max(change.fg, 0))]
            plays += [(change.row, _Play.SHOT, n, change) for n in range(max(change.fta, 0))]
            plays += [(change.row, _Play.FOUL, n, change) for n in range(max(change.fouls, 0))]
        plays += [(change.row, _Play.REVERSAL, 0, change) for change, _ in reversals]
        plays.sort(key=lambda play: (play[0], play[1], play[2]))
        if first is not None:
            slots = [i for i, play in enumerate(plays) if play[1] is _Play.BASKET]
            baskets = sorted((plays[i] for i in slots), key=lambda play: play[3].side is not first)
            for i, play in zip(slots, baskets):
                plays[i] = play
        return {(kind, change.key, n): float(rank) for rank, (_, kind, n, change) in enumerate(plays)}

    def _assists(self, changes: Sequence[_Change], scorers: set[PlayerKey], out: _Out) -> None:
        """Tell the baskets that waited a read for their assist, with one if it came."""
        waiting, self._waiting = self._waiting, []
        for basket in waiting:
            side_scored = any(change.side is basket.side and change.key in scorers for change in changes)
            out.tell(basket.caption(None if side_scored else self._assister(changes, basket)), basket.seen)

    @staticmethod
    def _assister(changes: Sequence[_Change], basket: _Basket) -> str | None:
        """The teammate credited with ``basket``'s assist: the side's one assist in the read, if it has exactly
        one, and not the shooter's own."""
        assists = [change for change in changes if change.side is basket.side and change.assists > 0]
        if len(assists) != 1 or assists[0].assists != 1 or assists[0].key == basket.shooter:
            return None
        return assists[0].name

    def _baskets(self, changes: Sequence[_Change], seen: _SeenFor, out: _Out) -> None:
        for side in _SIDES:
            scored = [change for change in changes if change.side is side and change.fg > 0]
            baskets = [
                _Basket(
                    side,
                    change.key,
                    change.name,
                    ShotValue.THREE if n < change.threes else ShotValue.TWO,
                    seen(_Play.BASKET, change.key, n),
                )
                for change in scored
                for n in range(change.fg)
            ]
            for basket in baskets:
                self._last_basket = _LastBasket(basket.shooter, basket.seen.period, basket.seen.tenths)
            if len(baskets) != 1:
                for basket in baskets:
                    out.tell(basket.caption(), basket.seen)  # several at once: no assist can be told apart
                continue
            basket = baskets[0]
            if any(change.side is side and change.assists > 0 for change in changes):
                out.tell(basket.caption(self._assister(changes, basket)), basket.seen)
            else:
                self._waiting.append(basket)

    @staticmethod
    def _shots(change: _Change, seen: Sequence[_Seen]) -> list[_Shot]:
        line = change.roster.line
        made = min(max(change.ftm, 0), change.fta)
        return [_Shot(n < made, at, line.ft_made, line.ft_attempted) for n, at in enumerate(seen)]

    # --- free throws ------------------------------------------------------------------------------------------

    def _free_throws(
        self, shots: list[_NewShots], scorers: set[PlayerKey], ends: bool, read: LinesRead, out: _Out
    ) -> None:
        """Add the read's free throws to the open trip, end it if the read did, and open the next."""
        trip = self._trip
        rest = shots
        if trip is not None:
            for shooter in self._waiting_shooters(trip):
                self._cover(trip, shooter, out)  # a foul that turned up a read late
            ongoing = [new for new in shots if new.change.side is trip.side and new.change.key not in scorers]
            rest = [new for new in shots if new not in ongoing]
            self._land(trip, ongoing, out)
            for shooter in self._waiting_shooters(trip):
                self._settle(trip, shooter, out)
            if ends or rest:
                self._close(out)
        for side in dict.fromkeys(new.change.side for new in rest):
            if self._trip is not None:
                self._close(out)
            landing = [new for new in rest if new.change.side is side]
            trip = _Trip(side, read.period, read.clock.tenths if read.clock is not None else None)
            self._trip = self._late(trip, landing) or trip
            self._land(self._trip, landing, out)

    def _late(self, trip: _Trip, landing: Sequence[_NewShots]) -> _Trip | None:
        """The side's last trip, when the one free throw landing is plainly its own posted late: the shooter had
        one free throw on it, under a foul (a foul gives two or three), and nothing else accounts for this one.

        The stat rows can post a trip's last free throw after play has gone on, past a basket or 20 s of clock.
        """
        last = self._last_trip
        if last is None or last.side is not trip.side or last.grouped or last.period != trip.period:
            return None
        if len(landing) != 1 or len(landing[0].shots) != 1:
            return None
        shooter = last.shooters.get(landing[0].change.key)
        if shooter is None or shooter.cover is not _Cover.FOUL or len(shooter.shots) != 1:
            return None
        if self._and_one(shooter.key, trip) or any(foul.side is not trip.side for foul in self._fouls):
            return None
        return last

    def _waiting_shooters(self, trip: _Trip) -> list[_Shooter]:
        """The shooters still without a cover whose free throws landed before this read."""
        return [
            shooter
            for shooter in trip.shooters.values()
            if shooter.cover is _Cover.UNKNOWN and shooter.since < self._changed_reads
        ]

    def _land(self, trip: _Trip, shots: Sequence[_NewShots], out: _Out) -> None:
        """Free throws landing on ``trip``: two or more in one read hold the whole trip until it ends."""
        if sum(len(new.shots) for new in shots) >= 2:
            trip.grouped = True
        for new in shots:
            shooter = trip.shooters.get(new.change.key)
            if shooter is None:
                shooter = _Shooter(trip.side, new.change.key, new.change.name, _Cover.UNKNOWN, self._changed_reads)
                trip.shooters[shooter.key] = shooter
                if self._and_one(shooter.key, trip):
                    shooter.cover = _Cover.AND_ONE
                    shooter.foul = self._claim(trip.side)
                else:
                    self._cover(trip, shooter, out)
            elif shooter.cover is _Cover.TECHNICAL:
                shooter.cover = _Cover.NO_FOUL  # free throws of their own after the technical's
            shooter.shots += new.shots
            self._tell(trip, shooter, out)

    def _and_one(self, key: PlayerKey, trip: _Trip) -> bool:
        """Whether the game's last basket was the shooter's, just before the trip's first free throw."""
        last = self._last_basket
        if last is None or last.shooter != key or last.period != trip.period:
            return False
        return last.tenths is None or trip.tenths is None or last.tenths - trip.tenths <= TRIP_WINDOW_TENTHS

    def _claim(self, side: HomeAway) -> _Foul | None:
        """The earliest pending foul by the side that didn't shoot, now accounted for."""
        foul = next((foul for foul in self._fouls if foul.side is not side), None)
        if foul is not None:
            self._fouls.remove(foul)
        return foul

    def _cover(self, trip: _Trip, shooter: _Shooter, out: _Out) -> None:
        """Give an uncovered shooter a pending foul by the other side, if there is one."""
        foul = self._claim(trip.side)
        if foul is not None:
            shooter.cover, shooter.foul = _Cover.FOUL, foul
            self._tell(trip, shooter, out)

    def _settle(self, trip: _Trip, shooter: _Shooter, out: _Out) -> None:
        """Decide a shooter no foul covered.

        More than one free throw means a foul's shooter whose foul didn't post, unless another shooter on the trip
        has one free throw and the only foul: then that one took the technical, and this one shot the foul's. A
        lone free throw is a technical's.
        """
        if len(shooter.shots) >= 2:
            partner = next(
                (
                    other
                    for other in trip.shooters.values()
                    if other.cover is _Cover.FOUL and len(other.shots) == 1 and other.technical is None
                ),
                None,
            )
            if partner is None:
                shooter.cover = _Cover.NO_FOUL
            else:
                shooter.cover, shooter.foul, partner.foul = _Cover.FOUL, partner.foul, None
                partner.cover, partner.technical = _Cover.TECHNICAL, partner.shots[0]
                self._tell(trip, partner, out)
        else:
            shooter.cover, shooter.technical = _Cover.TECHNICAL, shooter.shots[0]
        self._tell(trip, shooter, out)

    def _tell(self, trip: _Trip, shooter: _Shooter, out: _Out) -> None:
        """Tell what a shooter has that can be told: free throws once covered, a technical once known. A grouped
        trip tells nothing until it ends."""
        if trip.grouped or shooter.cover is _Cover.UNKNOWN:
            return
        if shooter.technical is not None and not shooter.technical_told:
            shooter.technical_told = True
            out.tell(shooter.technical_foul(), shooter.technical.seen)
        for shot in shooter.free_throws:
            if not shot.told:
                shot.told = True
                out.tell(shooter.free_throw(shot), shot.seen, after=_foul_seen(shooter))

    def _close(self, out: _Out) -> None:
        """End the open trip: settle anyone still waiting, and tell a grouped trip whole."""
        trip = self._trip
        assert trip is not None
        self._trip, self._last_trip = None, trip
        for shooter in trip.shooters.values():
            if shooter.cover is _Cover.UNKNOWN:
                self._settle(trip, shooter, out)
        if not trip.grouped:
            return
        for shooter in trip.shooters.values():
            if shooter.technical is not None and not shooter.technical_told:
                out.tell(shooter.technical_foul(), shooter.technical.seen)
            shots = shooter.free_throws
            if shots:
                out.tell(shooter.group(), shots[0].seen, after=_foul_seen(shooter))

    # --- what's held ------------------------------------------------------------------------------------------

    def _held(self) -> list[HeldCaption]:
        held = [HeldCaption(reason=HeldReason.ASSIST, caption=b.caption(), seen_at=b.seen.at) for b in self._waiting]
        trip = self._trip
        if trip is not None:
            for shooter in trip.shooters.values():
                held += self._held_shots(trip, shooter)
        if self._challenge is not None:
            challenge = ChallengeWon(side=self._challenge.side)
            held.append(HeldCaption(reason=HeldReason.CHALLENGE, caption=challenge, seen_at=self._challenge.seen.at))
        return held

    @staticmethod
    def _held_shots(trip: _Trip, shooter: _Shooter) -> list[HeldCaption]:
        if trip.grouped:
            reason = HeldReason.FREE_THROW_GROUP
            group: list[HeldCaption] = []
            if shooter.technical is not None and not shooter.technical_told:
                technical = shooter.technical_foul()
                group.append(HeldCaption(reason=reason, caption=technical, seen_at=shooter.technical.seen.at))
            shots = shooter.free_throws
            if shots:
                group.append(HeldCaption(reason=reason, caption=shooter.group(), seen_at=shots[0].seen.at))
            return group
        if shooter.cover is not _Cover.UNKNOWN:
            return []
        return [
            HeldCaption(reason=HeldReason.FOUL_CHECK, caption=shooter.free_throw(shot), seen_at=shot.seen.at)
            for shot in shooter.shots
        ]
