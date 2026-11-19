"""Basketball's events from a feed with no play-by-play: one game's reads, in arrival order, as the captions a board
tells.

The feed gives two kinds of read (`omni.domain.basketball_reads`). A lines read holds every player's box-score line,
so a play shows only as a change between one lines read and the next. A slate read holds the game's state.
`BasketballFeedDeriver` takes one game's reads as they arrive and answers each with a `FeedUpdate`: the captions it
can now tell, each with the read that first showed its play, and what it is holding back, and why.

It keeps no clock of its own; every moment it reports is a read's arrival. Between reads it keeps:

- **the last lines**: each player's line by key. The first lines read is the baseline, so plays before it are old
  news; after that, a player's first line is a change from nothing.
- **the game clock as last read**, from either kind of read. A reading further on (a lower clock, a later period,
  a break) means the clock ran; a reading back from it (a read lagging another, or a correction upward) is set
  aside.
- **the open trip to the line**: one side's free throws since the first, by shooter. A trip ends at a basket (by
  either team), a turnover, the other side's free throw, the next period, or once 20 s of game clock has run since
  its first free throw. The stat rows' clock can move before a trip's last free throw posts, so a running clock
  alone doesn't end one.
- **a basket waiting for its assist**, for one lines read.
- **the slates since the last lines read**, which can order two teams' baskets in the next one.

How the captions are told:

- **A basket** is a line's made field goals up by one: a three when its threes went up too. A teammate's assist in
  the same read names the assister when the team has exactly one basket and one assist there; with no assist, the
  basket waits one lines read for a late one. A basket taken back is no caption; one moved to a teammate in the
  same read is no new basket, though a basket still waiting for its assist moves with it.
- **A free throw** that lands alone in its read is told as it lands (`FreeThrow`), numbered among its shooter's
  free throws on the trip. Once two of a trip's free throws land in one read, the whole trip is held and told when
  it ends, one `FreeThrows` a shooter, counting every free throw of the trip, those told already among them.
- **Two plays in one read** are ordered by a slate caught between them that shows one team's baskets and not the
  other's; then by the order the feed sent the rows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum, IntEnum

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway, StrEnumMixin
from omni.domain.basketball import BasketballGameState, BasketballPhase, BySide, GameClock, PlayerLine
from omni.domain.basketball_reads import BasketballRead, LinesRead, PlayerKey, RosterLine, SlateRead
from omni.domain.football import other_side
from omni.events.basketball import BasketballCaption, FieldGoalMade, FreeThrow, FreeThrows, ShotValue

__all__ = [
    "TRIP_WINDOW_TENTHS",
    "BasketballFeedDeriver",
    "FeedCaption",
    "FeedUpdate",
    "HeldCaption",
    "HeldReason",
]

TRIP_WINDOW_TENTHS = 200  # 20 s of game clock: a trip reaches no further
_SIDES = (HomeAway.AWAY, HomeAway.HOME)
_REGULATION_CLOCK = 12 * 60 * 10  # a regulation period's full clock, in tenths
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


class HeldReason(StrEnumMixin, str, Enum):
    """Why a caption is waiting."""

    ASSIST = "assist"  # a basket waiting one lines read for a late assist
    FREE_THROW_GROUP = "free_throw_group"  # a trip whose free throws landed together, held whole until it ends


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldCaption:
    """A caption being held, as it would read if it were told now."""

    reason: HeldReason
    caption: BasketballCaption
    seen_at: MonotonicTime


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedUpdate:
    """What one read told: the captions now known, in the order to tell them, and everything still held."""

    at: MonotonicTime
    captions: tuple[FeedCaption, ...] = ()
    held: tuple[HeldCaption, ...] = ()


# --- what the deriver keeps ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Seen:
    """Where a play was first seen: its read's arrival and game clock, and its rank among that read's plays."""

    at: MonotonicTime
    period: int
    clock: GameClock | None
    rank: float = 0.0


@dataclass(frozen=True, slots=True)
class _Reading:
    """The game clock as last read: the period, and the tenths left (None at the break after it)."""

    period: int
    tenths: int | None

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
    assists: int
    turnovers: int

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
            assists=delta["assists"],
            turnovers=delta["turnovers"],
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
    shots: list[_Shot] = field(default_factory=list)

    def free_throw(self, shot: _Shot) -> FreeThrow:
        return FreeThrow(
            side=self.side,
            shooter=self.name,
            place=self.shots.index(shot) + 1,
            made=shot.made,
            game_made=shot.game_made,
            game_attempted=shot.game_attempted,
        )

    def group(self) -> FreeThrows:
        """The shooter's free throws on the trip as one, against their line at the last of them.

        The line is the word on the game: when the scorer has corrected a free throw on the trip since it landed
        (a miss made good, or an attempt taken off), the trip's count gives way to it.
        """
        shots = self.shots
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


class _Play(IntEnum):
    """A play's kind, in the order a player's own plays in one read are told."""

    BASKET = 1
    SHOT = 2


_SortKey = tuple[float, float]
_SeenFor = Callable[[_Play, PlayerKey, int], _Seen]  # where a read's play was seen, by its kind, player and number


class _Out:
    """The captions one read tells, put in the order to tell them."""

    def __init__(self) -> None:
        self._captions: list[tuple[_SortKey, FeedCaption]] = []

    def tell(self, caption: BasketballCaption, seen: _Seen) -> None:
        told = FeedCaption(caption=caption, seen_at=seen.at, period=seen.period, clock=seen.clock)
        self._captions.append(((seen.at.seconds, seen.rank), told))

    def update(self, at: MonotonicTime, held: Sequence[HeldCaption]) -> FeedUpdate:
        ordered = tuple(caption for _, caption in sorted(self._captions, key=lambda item: item[0]))
        return FeedUpdate(at=at, captions=ordered, held=tuple(held))


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


# --- the deriver --------------------------------------------------------------------------------------------


class BasketballFeedDeriver:
    """One game's reads in, in arrival order; what each one told out."""

    def __init__(self) -> None:
        self._players: dict[PlayerKey, RosterLine] | None = None
        self._reading: _Reading | None = None
        self._trip: _Trip | None = None
        self._waiting: list[_Basket] = []
        self._slates: list[BasketballGameState] = []
        self._final = False

    def read(self, read: BasketballRead) -> FeedUpdate:
        """Take the game's next read, and say what it told."""
        out = _Out()
        if isinstance(read, SlateRead):
            self._slate(read, out)
        else:
            self._lines(read, out)
        return out.update(read.at, self._held())

    # --- the clock ------------------------------------------------------------------------------------------

    def _track(self, period: int, tenths: int | None, out: _Out) -> None:
        """Note a read's clock; a reading further on than the last means the clock ran."""
        reading = _Reading(period, tenths)
        if self._reading is not None and reading.progress <= self._reading.progress:
            return  # still stopped here, or a read behind the last
        self._reading = reading
        if self._trip is not None and self._beyond(self._trip.period, self._trip.tenths):
            self._close(out)

    def _beyond(self, period: int, tenths: int | None) -> bool:
        """Whether the clock as last read has left ``period``, or run more than the window past ``tenths``."""
        reading = self._reading
        assert reading is not None
        if reading.period != period:
            return True
        return tenths is not None and reading.tenths is not None and tenths - reading.tenths > TRIP_WINDOW_TENTHS

    # --- slate reads ----------------------------------------------------------------------------------------

    def _slate(self, read: SlateRead, out: _Out) -> None:
        state = read.state
        self._final = state.phase is BasketballPhase.FINAL
        if state.phase is not BasketballPhase.LIVE:
            self._track(state.period, None, out)
        elif state.clock is not None:
            self._track(state.period, state.clock.tenths, out)
        self._slates.append(state)

    # --- lines reads: plays -----------------------------------------------------------------------------------

    def _lines(self, read: LinesRead, out: _Out) -> None:
        self._track(read.period, read.clock.tenths if read.clock is not None else None, out)
        slates, self._slates = self._slates, []
        before = self._players
        self._players = {**(before or {}), **{roster.key: roster for roster in read.lines}}
        if before is None:
            return  # the baseline: what the lines hold already happened before the board was watching
        changes = [
            _Change.of(row, roster, prior.line if (prior := before.get(roster.key)) is not None else None)
            for row, roster in enumerate(read.lines)
        ]
        self._moves(changes)
        points = BySide(
            sum(2 * c.fg + max(c.threes, 0) for c in changes if c.side is HomeAway.AWAY and c.fg > 0),
            sum(2 * c.fg + max(c.threes, 0) for c in changes if c.side is HomeAway.HOME and c.fg > 0),
        )
        first = _first_to_score(slates, _team_points(before.values()), points)
        ranks = self._ranks(changes, first)

        def seen(kind: _Play, key: PlayerKey, n: int = 0) -> _Seen:
            return _Seen(read.at, read.period, read.clock, ranks[(kind, key, n)])

        scorers = {change.key for change in changes if change.fg > 0}
        self._assists(changes, scorers, out)
        self._baskets(changes, seen, out)
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
            gainer.fg -= 1
            gainer.threes -= 1 if gainer.threes > 0 and loser.threes < 0 else 0
        for loser in (change for change in changes if change.fta < 0):
            gainer = next((c for c in changes if c.fta == -loser.fta and c.ftm == -loser.ftm), None)
            if gainer is not None:
                gainer.fta = gainer.ftm = 0

    def _ranks(self, changes: Sequence[_Change], first: HomeAway | None) -> dict[tuple[_Play, PlayerKey, int], float]:
        """Each of the read's plays' place among them: by the feed's row order, a player's own plays in `_Play`'s
        order, except that a side a slate caught scoring first has its baskets told first."""
        plays: list[tuple[int, _Play, int, _Change]] = []
        for change in changes:
            plays += [(change.row, _Play.BASKET, n, change) for n in range(max(change.fg, 0))]
            plays += [(change.row, _Play.SHOT, n, change) for n in range(max(change.fta, 0))]
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
            ongoing = [new for new in shots if new.change.side is trip.side and new.change.key not in scorers]
            rest = [new for new in shots if new not in ongoing]
            self._land(trip, ongoing, out)
            if ends or rest:
                self._close(out)
        for side in dict.fromkeys(new.change.side for new in rest):
            if self._trip is not None:
                self._close(out)
            self._trip = _Trip(side, read.period, read.clock.tenths if read.clock is not None else None)
            self._land(self._trip, [new for new in rest if new.change.side is side], out)

    def _land(self, trip: _Trip, shots: Sequence[_NewShots], out: _Out) -> None:
        """Free throws landing on ``trip``: two or more in one read hold the whole trip until it ends."""
        if sum(len(new.shots) for new in shots) >= 2:
            trip.grouped = True
        for new in shots:
            shooter = trip.shooters.setdefault(new.change.key, _Shooter(trip.side, new.change.key, new.change.name))
            shooter.shots += new.shots
            if trip.grouped:
                continue
            for shot in shooter.shots:
                if not shot.told:
                    shot.told = True
                    out.tell(shooter.free_throw(shot), shot.seen)

    def _close(self, out: _Out) -> None:
        """End the open trip, telling a grouped trip whole."""
        trip = self._trip
        assert trip is not None
        self._trip = None
        if trip.grouped:
            for shooter in trip.shooters.values():
                out.tell(shooter.group(), shooter.shots[0].seen)

    # --- what's held ------------------------------------------------------------------------------------------

    def _held(self) -> list[HeldCaption]:
        held = [HeldCaption(reason=HeldReason.ASSIST, caption=b.caption(), seen_at=b.seen.at) for b in self._waiting]
        trip = self._trip
        if trip is not None and trip.grouped:
            reason = HeldReason.FREE_THROW_GROUP
            for shooter in trip.shooters.values():
                held.append(HeldCaption(reason=reason, caption=shooter.group(), seen_at=shooter.shots[0].seen.at))
        return held
