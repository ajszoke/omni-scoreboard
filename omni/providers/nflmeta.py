"""NFLMeta's live scores, for a followed game or a slate of games, put into the board's football types.

NFLMeta re-publishes each game's scoreboard about every ten seconds and says so in its response, so the
provider is polled at that pace. Each poll is one request against a monthly quota, which is what the
halftime hold is for: the first time a game reaches halftime, the provider marks the moment, and once the
half's plays are in it makes no request until `HALFTIME_HOLD` after it, answering with what it last saw.

The scoreboard moves first. The play behind it lands in the play-change feed about ten seconds later,
and the scoreboard says when by matching that play (`PlaySyncStatus.MATCHED`), which is when the feed is
read (or on every poll, with `PlayFeedPace.EVERY_POLL`, for a plan whose quota allows it). A matched
scoreboard that changes with no new play behind it (a review moving the ball back, the try after a
touchdown) means a correction is on its way, so the feed is watched for a short while. A matched play
that a complete read doesn't have yet is read for again, for up to `MISMATCH_RETRY`. The feed carries no
timeouts and no win probability; timeouts and challenges come from the play text (`PlayLedger`), and the
meter from elsewhere.

Breaks drain the feed too:
- **Halftime**, until the half's last play is in, for up to `HALFTIME_GRACE`. A board that starts
  mid-break drains too, when its first read of the game didn't finish. The hold runs from the break's
  onset (when the half ended, as near as the provider can tell), not from when the drain finished.
- **The final**, for `FINAL_DRAIN`, ending with one last read at or after its end. Each answer while
  it drains says how long is left (`NflLiveSnapshot.drain_left`), so the asker can keep to the live pace
  and ask on the boundary, however slowly NFLMeta says to poll a finished game. If that last read leaves
  the plays short of the game's end (it didn't finish, or the feed never wrote the end), the gap is
  noted (`FeedNoteKind.FINAL_INCOMPLETE`), and the game settles anyway. A score no play accounts for is
  shown from the scoreboard on that read, since nothing is read after it.

Each poll is given the clock's two readings. Instants are dated on the wall clock: the break's onset
(for its countdown), when the feed wrote a play, an injury's report. Spans run on the monotonic clock:
the halftime hold and its grace, the final's drain, the correction watch, the mismatch re-reads and the
score backstop. So a wall-clock step neither stops nor stretches any of them.

A read of the feed keeps what it got: each page that arrives is kept, the place in the feed moves only
past what was kept, and a read that fails partway carries on from there at the next poll. Only plays
that were read and kept count toward being caught up.

Each entry a play brings is dated by when the feed wrote it (`FootballEvent.written_at`): the one change to that
play in the read that brought it, which is the revision the entry announces. A correction (a ruling, a try) is
dated by the change that carried it, not by the play it corrects. A play the read brings more than one change of (a
snap and its try, or a score revision, read together) can't say which write the entry stands for: it stays undated,
and waits the full delay from its receipt. That's how a board tells a play that reached it late, and never releases
a later revision early on its snap's time.

**Slate reads** list several games in one live-scores request, with no plays (`NflSlate`):
- `day_list`, unfiltered: every game NFLMeta lists for the day, finals and games not yet started included. It
  bills each game and each of its injury entries, so a board reads it once a day, for the games' ids.
- `live_scores`, the listed games in progress (``phase=in``, halftime included), by id: a game not started or
  already final isn't listed, and bills nothing.
- `scoreboard`, one game by its id: a game's last read once it has left the games in progress.

Each gives a `NflLiveSnapshot` per game it could read, with no events and nothing proven about the plays; a final
is settled, since nothing is left to ask. NFLMeta files a game under its kickoff's date in the league's own time
zone (`SCHEDULE_TIMEZONE`).

Raw JSON stays in this module: what leaves it is a `NflLiveSnapshot`, or a `NflSlate` of them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, HomeAway, League, SeasonStage, StrEnumMixin
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.coverage import CoverageMap, FieldCoverage
from omni.domain.football import (
    FootballField,
    FootballGameState,
    FootballInterval,
    PlaySummary,
    PlayTone,
    SetPiece,
)
from omni.events.football import (
    ConversionResult,
    EntryLabel,
    FieldGoalResult,
    FootballEvent,
    FootballEventKind,
    FootballPlay,
    FootballPlayType,
)
from omni.events.football_ledger import PlayLedger
from omni.providers.base import ProviderError
from omni.providers.nfl_teams import NflTeamRegistry, canonical_abbreviation

__all__ = [
    "CORRECTION_WATCH",
    "DEFAULT_POLL",
    "FINAL_DRAIN",
    "HALFTIME_GRACE",
    "HALFTIME_HOLD",
    "INJURY_NEWS",
    "MAX_GAME_IDS",
    "MISMATCH_RETRY",
    "NFLMETA_SOURCE",
    "SCHEDULE_TIMEZONE",
    "FeedNote",
    "FeedNoteKind",
    "InjuryStatus",
    "NflLiveSnapshot",
    "NflMetaFetcher",
    "NflMetaLiveProvider",
    "NflPhase",
    "NflSlate",
    "PlayFeedPace",
    "PlaySyncStatus",
    "SCORE_BACKSTOP",
]

NFLMETA_SOURCE = SourceRef("nflmeta")
SCHEDULE_TIMEZONE = ZoneInfo("America/New_York")  # the league's own: a game is filed under its kickoff's date here
MAX_GAME_IDS = 100  # the most games one live-scores read may name
HALFTIME_HOLD = timedelta(minutes=13)  # no requests until this long after a game's halftime began
HALFTIME_GRACE = timedelta(seconds=30)  # the hold waits up to this long for the half's last play to land
FINAL_DRAIN = timedelta(minutes=2)  # after the final, the feed is still read this long for its last plays and fixes
CORRECTION_WATCH = timedelta(seconds=45)  # how long to keep reading the play feed for an expected correction
MISMATCH_RETRY = timedelta(seconds=30)  # how long to re-read for a matched play the feed doesn't have yet
SCORE_BACKSTOP = timedelta(minutes=2)  # a score no play accounts for is shown from the scoreboard after this
INJURY_NEWS = timedelta(minutes=10)  # a board starting up still announces an injury this recent
DEFAULT_POLL = DurationSeconds(10)  # NFLMeta's own recommended pace, used until a response says otherwise
_PLAY_PAGE = 100
_CATCH_UP_PAGES = 30  # a first look reads the whole game so far, corrections and all
_MAX_PLAY_PAGES = 5  # a whole game's plays fit in three pages; past five something is looping
_REJECTED_CURSOR = frozenset({400, 410})  # the feed's answers for a place in it that's malformed or gone
_REGULAR_SEASON_WEEKS = 18  # a later week is the postseason, whose overtime has its own timeouts

# How the provider reads the API: a path and its query, to the parsed response.
NflMetaFetcher = Callable[[str, Mapping[str, str | int]], Any]


class NflPhase(StrEnumMixin, str, Enum):
    """Where a game is in its day, as NFLMeta names it."""

    PRE = "pre"
    IN = "in"
    POST = "post"


class PlaySyncStatus(StrEnumMixin, str, Enum):
    """Whether the scoreboard's latest play has its row in the play feed yet.

    Only `MATCHED` says anything about the plays: `UNKNOWN` is a scoreboard with no play to point at
    (before kickoff, after the final) and `STALE` one that can't be taken as current.
    """

    MATCHED = "matched"
    AWAITING_PLAY = "awaiting_play"
    STALE = "stale"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> PlaySyncStatus:
        return cls.UNKNOWN


class PlayFeedPace(StrEnumMixin, str, Enum):
    """When the play feed is read: when the scoreboard points at a new play, or on every poll.

    Every poll finds a play as soon as the feed has it, including one the scoreboard never points at,
    for about twice the requests.
    """

    ON_CHANGE = "on-change"
    EVERY_POLL = "every-poll"


class FeedNoteKind(StrEnumMixin, str, Enum):
    """Something about a poll worth a line in the log, though nothing the board shows."""

    PLAY_READ_FAILED = "play read failed"  # kept what arrived; the rest is read next poll
    CURSOR_RESET = "cursor reset"  # the feed refused our place in it: re-read from its start, quietly
    LATE_PLAY = "late play"  # first seen after a later play: counted in the stats, not shown
    PLAY_MISSING = "matched play missing"  # the scoreboard names a play a complete read doesn't have
    FINAL_INCOMPLETE = "final plays incomplete"  # the final's last read left the plays short: parked without the rest


@dataclass(frozen=True, slots=True)
class FeedNote:
    """A note about a poll. Our own parts may go anywhere: its kind, what happens next (``detail``), and the entry
    it's about by our own label (``entry``). ``quoting`` is the feed's own words (a play's text, a failure's message),
    which only a log stamped to expire may keep (`omni.app.run_log`)."""

    kind: FeedNoteKind
    detail: str | None = None
    entry: EntryLabel | None = None
    quoting: str | None = None

    @property
    def ours(self) -> tuple[str, ...]:
        """What the note says in our own words, past its kind: the entry it's about, then what happens next."""
        entry = str(self.entry) if self.entry is not None else None
        return tuple(part for part in (entry, self.detail) if part is not None)

    @property
    def told(self) -> str:
        """The note in our own words: its kind, the entry it's about, and what happens next."""
        return ": ".join((self.kind.value, *self.ours))

    @property
    def quoted(self) -> str:
        """The note as a stamped log keeps it: in the feed's words, when it quotes the feed."""
        return f"{self.kind.value}: {self.quoting}" if self.quoting is not None else self.told


class InjuryStatus(StrEnumMixin, str, Enum):
    """Where an in-game injury stands."""

    QUESTIONABLE_TO_RETURN = "questionable_to_return"
    DOUBTFUL_TO_RETURN = "doubtful_to_return"
    OUT = "out"
    RETURNED = "returned"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> InjuryStatus:
        return cls.OTHER

    @property
    def words(self) -> str:
        return _INJURY_WORDS[self]


_INJURY_WORDS = {
    InjuryStatus.QUESTIONABLE_TO_RETURN: "questionable to return",
    InjuryStatus.DOUBTFUL_TO_RETURN: "doubtful to return",
    InjuryStatus.OUT: "out",
    InjuryStatus.RETURNED: "returned",
    InjuryStatus.OTHER: "being evaluated",
}
_INTERVALS: tuple[tuple[str, FootballInterval], ...] = (  # by how the status text starts
    ("halftime", FootballInterval.HALFTIME),
    ("end of 2nd", FootballInterval.HALFTIME),
    ("end of", FootballInterval.END_OF_QUARTER),
)
_REGULATION_PERIODS = 4
_HALFTIME_PERIOD = 2
_FIELD = 100  # goal line to goal line
_GAME_STATUS = {NflPhase.PRE: GameStatus.PREGAME, NflPhase.IN: GameStatus.LIVE, NflPhase.POST: GameStatus.FINAL}
_NEXT_SNAP = (FootballField.POSSESSION, FootballField.DOWN_DISTANCE, FootballField.SPOT)
_COUNTS = (FootballField.TIMEOUTS, FootballField.CHALLENGES)  # what each side has left, which only the plays tell
_CORRECTIONS = frozenset({FootballEventKind.REVIEW, FootballEventKind.CONVERSION})


@dataclass(frozen=True, slots=True, kw_only=True)
class NflLiveSnapshot:
    """One look at a game: who's playing, how it stands, and what's happened since the last look.

    ``state`` is None before kickoff. ``events`` usually trail ``state``: the scoreboard shows a play's
    result before the play itself arrives, and ``caught_up`` says whether it has. ``settled`` means the
    final is in and the feed has had its time for late plays and fixes: there's nothing left to ask.
    Until then, ``drain_left`` says how long the final's drain has to its last read.
    """

    game: TeamGame
    phase: NflPhase
    state: FootballGameState | None
    events: tuple[FootballEvent, ...]
    caught_up: bool
    break_ends_at: datetime | None  # when the halftime hold runs out: the break as the board counts it
    observed_at: datetime | None
    poll_every: DurationSeconds
    settled: bool = False
    notes: tuple[FeedNote, ...] = ()
    drain_left: timedelta | None = None  # while the final drains: how long after this look its last read is due


@dataclass(frozen=True, slots=True, kw_only=True)
class NflSlate:
    """One live-scores read that lists games: an answer for each row it could read, the id of every game it
    listed (read or not), and a warning for each row it couldn't read."""

    snapshots: tuple[NflLiveSnapshot, ...] = ()
    listed: frozenset[int] = frozenset()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Injury:
    player: str
    team: str
    injury: str | None
    status: InjuryStatus


@dataclass(slots=True)
class _TrackedGame:
    """What the provider remembers of one game between polls."""

    ledger: PlayLedger | None = None
    latest: NflLiveSnapshot | None = None
    halftime_seen: ClockReading | None = None  # when this game was first seen at halftime
    joined_at_half: bool = False  # the provider's first look at it was at halftime
    halftime_at: ClockReading | None = None  # the break's onset, once the half's plays are in: the hold runs from it
    break_timed: bool = True  # whether the onset is known well enough to count the break down
    final_at: MonotonicTime | None = None  # when this game was first seen final
    settled: bool = False  # the final's drain has had its last read: nothing is left to ask
    cursor: str | None = None
    play_ids: set[str] = field(default_factory=set)  # every play the change feed has delivered and we kept
    written_at: dict[int, datetime] = field(default_factory=dict)  # when the feed first wrote each play
    read_for: str | None = None  # the matched play the feed was last read through for, so it's read once
    missing: tuple[str, MonotonicTime] | None = None  # a matched play a complete read lacked, and since when
    draining: bool = False  # the last read stopped short of the feed's end; the next poll reads on
    matched_board: tuple[Any, ...] | None = None  # the scoreboard as of the last matched play
    watch_until: MonotonicTime | None = None  # reading the feed every poll until then, for a correction
    injuries: dict[tuple[str, str], _Injury] | None = None  # None until the first look
    caught_up_plays: bool = False  # the play feed has been read to its end since the last (re)start
    stale_situation: str | None = None  # a down line the feed left behind when the score moved
    last_situation: str | None = None
    last_scores: tuple[int, int] | None = None
    shown_points: dict[HomeAway, int] = field(default_factory=dict)  # scored from the scoreboard alone
    gaps: dict[HomeAway, tuple[MonotonicTime, int]] = field(default_factory=dict)  # since, and the newest play then
    baselined: bool = False  # gaps already open on the first full look are old news, not events


@dataclass(slots=True)
class _PlayBatch:
    """One read of the play feed: what arrived, and where in the feed it leaves us."""

    cursor: str | None
    plays: list[FootballPlay] = field(default_factory=list)
    kept_ids: set[str] = field(default_factory=set)
    written_at: dict[int, datetime] = field(default_factory=dict)  # each play's first change in the read
    dated: dict[int, datetime | None] = field(default_factory=dict)  # its one change's write; None: several, or none
    complete: bool = False  # read through to the feed's end
    restarted: bool = False  # the feed refused the cursor: the next read starts over from its start
    notes: list[FeedNote] = field(default_factory=list)


class NflMetaLiveProvider:
    """Answers "how does this game stand, and what just happened?", holding off over halftime."""

    def __init__(
        self,
        registry: NflTeamRegistry,
        *,
        fetch: NflMetaFetcher,
        halftime_hold: timedelta = HALFTIME_HOLD,
        play_feed: PlayFeedPace = PlayFeedPace.ON_CHANGE,
    ) -> None:
        self._registry = registry
        self._fetch = fetch
        self._hold = halftime_hold
        self._play_feed = play_feed
        self._games: dict[int, _TrackedGame] = {}

    @property
    def schedule_timezone(self) -> ZoneInfo:
        """The time zone whose calendar date NFLMeta files a game under."""
        return SCHEDULE_TIMEZONE

    def find_game(self, away: str, home: str) -> int | None:
        """Today's game between ``away`` and ``home`` (any spelling a feed uses), or None."""
        wanted = (canonical_abbreviation(away), canonical_abbreviation(home))
        for row in _rows(self._get("/api/v1/live-scores", {})):
            if (_abbreviation(row, "away_team"), _abbreviation(row, "home_team")) == wanted:
                return int(row["game_id"])
        return None

    def holding(self, game_id: int, at: ClockReading) -> bool:
        """Whether ``game_id`` is inside its halftime hold as of ``at``, when polling it makes no request."""
        tracked = self._games.get(game_id)
        return (
            tracked is not None
            and tracked.halftime_at is not None
            and tracked.latest is not None
            and at.monotonic.since(tracked.halftime_at.monotonic) < self._hold
        )

    def hold_ends(self, game_id: int) -> MonotonicTime | None:
        """When ``game_id``'s halftime hold runs out, on the monotonic clock, once the hold has begun; None before."""
        tracked = self._games.get(game_id)
        if tracked is None or tracked.halftime_at is None:
            return None
        return tracked.halftime_at.monotonic + self._hold

    def day_list(self) -> NflSlate:
        """Every game NFLMeta lists for the day, read unfiltered: not started, in progress and final alike.

        Raises `ProviderError` when the read fails.
        """
        return self._slate({})

    def live_scores(self, game_ids: Collection[int]) -> NflSlate:
        """The scores of those of ``game_ids`` in progress (halftime included), read together; no plays.

        Raises `ProviderError` when the read fails, and `ValueError` for no ids or more than `MAX_GAME_IDS`.
        """
        if not 0 < len(game_ids) <= MAX_GAME_IDS:
            raise ValueError(f"a live-scores read names 1 to {MAX_GAME_IDS} games, not {len(game_ids)}")
        named = ",".join(str(game_id) for game_id in sorted(game_ids))
        return self._slate({"phase": NflPhase.IN.value, "game_ids": named})

    def scoreboard(self, game_id: int) -> NflLiveSnapshot:
        """One game's scoreboard, read by its id, with no plays.

        Raises `ProviderError` when the read fails, or doesn't list the game in a row it can read.
        """
        slate = self._slate({"game_id": game_id})
        wanted = str(game_id)
        snapshot = next((each for each in slate.snapshots if each.game.id.raw == wanted), None)
        if snapshot is None:
            why = "; ".join(slate.warnings) if game_id in slate.listed else "no longer lists it"
            raise ProviderError(f"NFLMeta has no readable game {game_id}: {why}")
        return snapshot

    def poll(self, game_id: int, at: ClockReading) -> NflLiveSnapshot:
        """The game as it stands at ``at``; inside the halftime hold, the last answer (with no new events),
        unasked.

        Raises `ProviderError` when the feed fails or no longer lists the game.
        """
        tracked = self._games.setdefault(game_id, _TrackedGame())
        if self.holding(game_id, at) and tracked.latest is not None:
            return dataclasses.replace(tracked.latest, events=(), notes=())
        first_look = tracked.latest is None
        body = self._get("/api/v1/live-scores", {"game_id": game_id})
        row = next((r for r in _rows(body) if r.get("game_id") == game_id), None)
        if row is None:
            raise ProviderError(f"NFLMeta no longer lists game {game_id}")
        try:
            game = self._game(row)
            phase = NflPhase(row["phase"])
            state = _state(row, game) if phase is not NflPhase.PRE else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"NFLMeta sent an unreadable game {game_id}: {exc}") from exc
        if state is not None and _stale_situation(tracked, row, state):
            left_behind = {field: FieldCoverage.UNAVAILABLE for field in _NEXT_SNAP if state.known(field)}
            state = _marked(state, left_behind, possession=None, down=None, distance=None, yards_to_goal=None)
        if phase is NflPhase.POST and tracked.final_at is None:
            tracked.final_at = at.monotonic
        at_half = state is not None and state.interval is FootballInterval.HALFTIME
        # A break drains the feed: halftime until its hold begins, the final until a read at its drain's end.
        final_at = tracked.final_at if not tracked.settled else None  # the final drains from then
        draining = (at_half and tracked.halftime_at is None) or final_at is not None
        ledger = tracked.ledger if tracked.ledger is not None else PlayLedger(game.away, game.home)
        notes: list[FeedNote] = []
        plays = self._follow_plays(game_id, tracked, ledger, row, at, drain=draining, notes=notes)
        tracked.ledger = ledger
        drain_left = FINAL_DRAIN - at.monotonic.since(final_at) if final_at is not None else None
        last_read = drain_left is not None and drain_left <= timedelta(0)
        if last_read:
            tracked.settled, drain_left = True, None  # that was the drain's last read
        scored: list[FootballEvent] = []
        if state is not None:
            scored = _unexplained_points(game, tracked, ledger, state, at.monotonic, last=last_read)
        events = scored + plays + _injury_events(tracked, row, at.utc)
        if state is not None:
            postseason = game.stage is SeasonStage.POSTSEASON
            state = _with_plays(state, ledger, postseason=postseason, known=tracked.caught_up_plays)
        caught_up = _caught_up(row, phase, tracked, ledger)
        if last_read and not caught_up:  # parked short of the game's end: say why
            short = "the last read after the final didn't finish" if tracked.draining else "no end of game in the feed"
            notes.append(FeedNote(FeedNoteKind.FINAL_INCOMPLETE, short))
        if at_half and tracked.halftime_at is None:
            _drain_half(tracked, ledger, at, first_look=first_look, caught_up=caught_up)
        timed = at_half and tracked.halftime_at is not None and tracked.break_timed
        tracked.latest = NflLiveSnapshot(
            game=game,
            phase=phase,
            state=state,
            events=tuple(events),
            caught_up=caught_up,
            break_ends_at=tracked.halftime_at.utc + self._hold if timed and tracked.halftime_at else None,
            observed_at=_instant(row.get("observed_at")),
            poll_every=_poll_every(body),
            settled=tracked.settled,
            notes=tuple(notes),
            drain_left=drain_left,
        )
        return tracked.latest

    def _follow_plays(
        self,
        game_id: int,
        tracked: _TrackedGame,
        ledger: PlayLedger,
        row: Mapping[str, Any],
        at: ClockReading,
        *,
        drain: bool,
        notes: list[FeedNote],
    ) -> list[FootballEvent]:
        """Read the play feed when there's something new in it, and keep what the read brings."""
        if tracked.settled:
            return []  # the final has had its drain, finished or not: nothing more is read
        status, play_id = _sync(row)
        board = _board_key(row)
        read = drain or not tracked.caught_up_plays or tracked.draining or self._play_feed is PlayFeedPace.EVERY_POLL
        if status is PlaySyncStatus.MATCHED:
            if play_id is not None and play_id not in tracked.play_ids and play_id != tracked.read_for:
                read = True
            elif tracked.matched_board is not None and board != tracked.matched_board:
                tracked.watch_until = at.monotonic + CORRECTION_WATCH  # the scoreboard moved with no new play
            tracked.matched_board = board
        if tracked.watch_until is not None and at.monotonic < tracked.watch_until:
            read = True
        if not read:
            return []
        batch = self._read_plays(game_id, tracked)
        announce = tracked.caught_up_plays and not batch.restarted
        events = ledger.absorb(batch.plays, announce=announce)
        if not announce and not batch.complete:
            events = []  # still catching up on the game: nothing is news until all of it is in
        events = [_dated(event, batch.dated) for event in events]
        tracked.cursor = batch.cursor
        tracked.play_ids |= batch.kept_ids
        for number, written in batch.written_at.items():
            tracked.written_at[number] = min(written, tracked.written_at.get(number, written))
        tracked.draining = not batch.complete
        if batch.restarted:
            tracked.caught_up_plays = False
        if batch.complete:
            tracked.caught_up_plays = True
            if status is PlaySyncStatus.MATCHED:
                _read_through(tracked, play_id, at.monotonic, notes)
        if any(event.kind in _CORRECTIONS for event in events):
            tracked.watch_until = None
        notes += batch.notes
        notes += [_late(event) for event in ledger.held_back]
        return events

    def _read_plays(self, game_id: int, tracked: _TrackedGame) -> _PlayBatch:
        """Every play change since the cursor (the first read starts from the game's first play).

        A page that fails ends the read with what came before it: those pages are kept, and the next
        read starts after them. A cursor the feed refuses starts the reading over from the feed's start.
        """
        batch = _PlayBatch(cursor=tracked.cursor)
        for _ in range(_MAX_PLAY_PAGES if tracked.caught_up_plays else _CATCH_UP_PAGES):
            query: dict[str, str | int] = {"limit": _PLAY_PAGE}
            if batch.cursor is not None:
                query["cursor"] = batch.cursor
            try:
                body = self._get(f"/api/v1/games/{game_id}/play-changes", query)
            except ProviderError as exc:
                if batch.cursor is not None and getattr(exc.__cause__, "code", None) in _REJECTED_CURSOR:
                    batch.cursor, batch.restarted = None, True
                    batch.notes.append(FeedNote(FeedNoteKind.CURSOR_RESET, quoting=str(exc)))
                else:
                    batch.notes.append(FeedNote(FeedNoteKind.PLAY_READ_FAILED, quoting=str(exc)))
                return batch
            for change in _rows(body):
                if (play := _play(change)) is None:
                    continue
                batch.plays.append(play)
                if isinstance(change.get("source_play_id"), str):
                    batch.kept_ids.add(change["source_play_id"])
                if (written := _instant(change.get("changed_at"))) is not None:
                    batch.written_at[play.number] = min(written, batch.written_at.get(play.number, written))
                if play.number in batch.dated and batch.dated[play.number] != written:
                    batch.dated[play.number] = None  # written more than once in the read: which write is whose?
                else:
                    batch.dated[play.number] = written
            meta = (body or {}).get("meta") or {}
            batch.cursor = meta.get("next_cursor") or batch.cursor
            if not meta.get("has_more"):
                batch.complete = True
                break
        return batch

    def _slate(self, query: Mapping[str, str | int]) -> NflSlate:
        """A live-scores read of several games, each row that can be read as an answer with no plays."""
        body = self._get("/api/v1/live-scores", query)
        poll_every = _poll_every(body)
        snapshots: list[NflLiveSnapshot] = []
        listed: set[int] = set()
        warnings: list[str] = []
        for row in _rows(body):
            game_id = row.get("game_id")
            if type(game_id) is int:
                listed.add(game_id)
            try:
                snapshots.append(self._listed(row, poll_every))
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"game {game_id}: unreadable ({type(exc).__name__}: {exc})")
        return NflSlate(snapshots=tuple(snapshots), listed=frozenset(listed), warnings=tuple(warnings))

    def _listed(self, row: Mapping[str, Any], poll_every: DurationSeconds) -> NflLiveSnapshot:
        """A slate's row as an answer: the scoreboard alone, no plays read. A final is settled: nothing is left
        to ask of it."""
        game = self._game(row)
        phase = NflPhase(row["phase"])
        state = _without_plays(_state(row, game)) if phase is not NflPhase.PRE else None
        return NflLiveSnapshot(
            game=game,
            phase=phase,
            state=state,
            events=(),
            caught_up=False,
            break_ends_at=None,
            observed_at=_instant(row.get("observed_at")),
            poll_every=poll_every,
            settled=phase is NflPhase.POST,
        )

    def _game(self, row: Mapping[str, Any]) -> TeamGame:
        kickoff = _instant(row.get("kickoff_at"))
        if kickoff is None:
            raise ValueError("no kickoff time")
        return TeamGame(
            id=LeagueScopedId(League.NFL, NFLMETA_SOURCE, str(row["game_id"])),
            league=League.NFL,
            status=_GAME_STATUS[NflPhase(row["phase"])],
            scheduled_start=kickoff,
            away=self._registry.resolve(row["away_team"]["abbr"]),
            home=self._registry.resolve(row["home_team"]["abbr"]),
            stage=_stage(row),
        )

    def _get(self, path: str, query: Mapping[str, str | int]) -> Any:
        try:
            return self._fetch(path, query)
        except Exception as exc:
            raise ProviderError(f"NFLMeta fetch failed for {path}: {exc}") from exc


def _read_through(tracked: _TrackedGame, play_id: str | None, now: MonotonicTime, notes: list[FeedNote]) -> None:
    """A complete read for a matched scoreboard. Its play is read for once it's in; while it isn't, the
    feed is read for it again each poll, until `MISMATCH_RETRY` has passed since the first read lacked it."""
    if play_id is None or play_id in tracked.play_ids:
        tracked.read_for, tracked.missing = play_id, None
    elif tracked.missing is None or tracked.missing[0] != play_id:
        tracked.missing = (play_id, now)
        notes.append(FeedNote(FeedNoteKind.PLAY_MISSING, "not in the feed yet: reading again next poll"))
    elif now.since(tracked.missing[1]) >= MISMATCH_RETRY:
        tracked.read_for, tracked.missing = play_id, None
        notes.append(FeedNote(FeedNoteKind.PLAY_MISSING, "still not in the feed: waiting for the next play"))


def _dated(event: FootballEvent, dated: Mapping[int, datetime | None]) -> FootballEvent:
    """``event``, dated by when the feed wrote its play's one change in the read that brought it: the revision it
    announces. Undated when the read brought several changes of that play (a try, a score revision or a ruling read
    with its snap), or didn't say: then it waits the full delay from its receipt."""
    written = dated.get(event.play) if event.play is not None else None
    return dataclasses.replace(event, written_at=written)


def _drain_half(
    tracked: _TrackedGame, ledger: PlayLedger, at: ClockReading, *, first_look: bool, caught_up: bool
) -> None:
    """Halftime, before its hold: the hold begins once the half's plays are in, or after `HALFTIME_GRACE`.

    Seen live, the half's last play is in once the scoreboard's play has landed, and the break began
    when halftime was first seen. Joined mid-break, the game so far has to be read through first; the
    break began when the feed wrote the half's last play, when that read finished and says so. That
    instant is placed on the monotonic clock as ``at`` reads it, for the hold to run from.
    """
    if tracked.halftime_seen is None:
        tracked.halftime_seen, tracked.joined_at_half = at, first_look
    seen = tracked.halftime_seen
    read_all = tracked.caught_up_plays and not tracked.draining
    if not (read_all if tracked.joined_at_half else caught_up) and at.monotonic.since(seen.monotonic) < HALFTIME_GRACE:
        return
    began = tracked.written_at.get(ledger.latest) if tracked.joined_at_half and read_all else None
    if began is not None and began < seen.utc:  # the half ended before it was seen
        tracked.halftime_at = ClockReading(began, at.monotonic + (began - at.utc))
    else:
        tracked.halftime_at = seen
    tracked.break_timed = began is not None or not tracked.joined_at_half


def _sync(row: Mapping[str, Any]) -> tuple[PlaySyncStatus, str | None]:
    sync = row.get("play_sync") or {}
    play_id = sync.get("last_play_id")
    return PlaySyncStatus(sync.get("status")), play_id if isinstance(play_id, str) else None


def _stale_situation(tracked: _TrackedGame, row: Mapping[str, Any], state: FootballGameState) -> bool:
    """Whether the feed's down line is one it left behind: the score moved and the line didn't.

    It stays stale until the feed writes a new one.
    """
    text = (row.get("situation") or {}).get("text")
    scores = (state.away_score, state.home_score)
    if tracked.last_scores is not None and scores != tracked.last_scores and text == tracked.last_situation:
        tracked.stale_situation = text
    elif text != tracked.stale_situation:
        tracked.stale_situation = None
    tracked.last_scores, tracked.last_situation = scores, text
    return text is not None and text == tracked.stale_situation


def _unexplained_points(
    game: TeamGame,
    tracked: _TrackedGame,
    ledger: PlayLedger,
    state: FootballGameState,
    now: MonotonicTime,
    *,
    last: bool = False,
) -> list[FootballEvent]:
    """Scores the scoreboard shows that no play accounts for, once the feed has plainly skipped them.

    The plays usually catch up within half a minute. A score is shown from the scoreboard when the
    feed delivers a later play without it, after `SCORE_BACKSTOP`, or on the ``last`` read of the game.
    A try still owed after a touchdown in the feed is left to its own row, however late: the
    scoreboard already shows it.
    """
    if not tracked.caught_up_plays:
        return []  # the plays so far aren't all in yet
    sides = (
        (HomeAway.AWAY, state.away_score, game.away.abbreviation),
        (HomeAway.HOME, state.home_score, game.home.abbreviation),
    )
    if not tracked.baselined:
        tracked.baselined = True
        tracked.shown_points = {side: max(scored - ledger.points(side), 0) for side, scored, _ in sides}
        return []
    events: list[FootballEvent] = []
    for side, scored, team in sides:
        accounted = ledger.points(side, shown=True)  # a late play's points are the scoreboard's to show
        shown = min(tracked.shown_points.get(side, 0), max(scored - accounted, 0))  # real plays caught up
        tracked.shown_points[side] = shown
        gap = scored - accounted - shown
        if gap <= 0:
            tracked.gaps.pop(side, None)
            continue
        if gap <= 2 and ledger.try_pending(side):
            continue
        since, newest = tracked.gaps.setdefault(side, (now, ledger.latest))
        if last or ledger.latest > newest or now.since(since) >= SCORE_BACKSTOP:
            events += _score_events(team, gap, side, scored)
            tracked.shown_points[side] = shown + gap
            tracked.gaps.pop(side, None)
    return events


def _score_events(team: str, points: int, side: HomeAway, total: int) -> list[FootballEvent]:
    """Points on the scoreboard that no play accounts for, as one strip event.

    The same points can come from more than one sequence of plays, so the event says only who scored
    how many, and that the play behind them wasn't reported.
    """
    summary = PlaySummary(tone=PlayTone.SCORE, headline=f"{team} +{points}", detail="Score update; play not reported")
    return [FootballEvent(key=f"score:{side.value}:{total}", kind=FootballEventKind.SCORE, summary=summary)]


def _caught_up(row: Mapping[str, Any], phase: NflPhase, tracked: _TrackedGame, ledger: PlayLedger) -> bool:
    """Whether the play feed has delivered the play behind the scoreboard.

    Only a matched scoreboard names that play. After the final it names none, and the feed's own
    end-of-game row says the plays are in. Otherwise nothing proves they are.
    """
    if tracked.draining:
        return False
    status, play_id = _sync(row)
    if status is PlaySyncStatus.MATCHED:
        return play_id is None or play_id in tracked.play_ids
    return phase is NflPhase.POST and ledger.ended


def _stage(row: Mapping[str, Any]) -> SeasonStage:
    """The game's part of the season, by its week: the regular season's are numbered first, and a later week is
    the postseason. A row without a week is UNKNOWN. Whether a preseason week is numbered apart is unverified until
    one is seen."""
    week = row.get("week")
    if type(week) is not int or week < 1:
        return SeasonStage.UNKNOWN
    return SeasonStage.POSTSEASON if week > _REGULAR_SEASON_WEEKS else SeasonStage.REGULAR


def _marked(
    state: FootballGameState, coverage: Mapping[FootballField, FieldCoverage], **changes: Any
) -> FootballGameState:
    """``state`` with ``changes`` made, and ``coverage`` saying what the changed fields now rest on."""
    return dataclasses.replace(state, coverage=state.coverage.updated(coverage), **changes)


def _from_feed(value: object) -> FieldCoverage:
    """A field the scoreboard row gives (REPORTED), or leaves None (UNAVAILABLE)."""
    return FieldCoverage.UNAVAILABLE if value is None else FieldCoverage.REPORTED


def _from_plays(value: object) -> FieldCoverage:
    """A field the plays work out (INFERRED), or can't (UNAVAILABLE)."""
    return FieldCoverage.UNAVAILABLE if value is None else FieldCoverage.INFERRED


def _with_plays(
    state: FootballGameState, ledger: PlayLedger, *, postseason: bool, known: bool = True
) -> FootballGameState:
    """The state with what only the plays can tell: timeouts, challenges, who has a kickoff coming.

    Until the play feed has been read through (``known``), the ledger holds only part of the game, so
    its counts would be guesses: no timeouts or challenges show, and nothing is inferred from it.

    Over halftime the ball is the second half's receiver's and timeouts start over, so none show. At
    regulation's end overtime has its own toss, so no one has the ball yet; after the final, nothing
    is left to spend. In overtime every review is the booth's, so no challenges show, and postseason
    overtime's timeouts (three for each of its halves) aren't counted yet, so none show there either.

    The coverage says which of those a None is: a count the plays can't give yet is UNAVAILABLE, one
    with nothing to count is NOT_APPLICABLE, and whatever the plays do give is INFERRED.
    """
    if not known:
        return _marked(state, dict.fromkeys(_COUNTS, FieldCoverage.UNAVAILABLE), timeouts=None, challenges=None)
    overtime = state.period > _REGULATION_PERIODS
    challenges = None if overtime else ledger.challenges()
    timeouts = None if overtime and postseason else ledger.timeouts(state.period)
    counted = {
        FootballField.TIMEOUTS: _from_plays(timeouts),
        FootballField.CHALLENGES: FieldCoverage.NOT_APPLICABLE if overtime else FieldCoverage.INFERRED,
    }
    spent = dict.fromkeys(_COUNTS, FieldCoverage.NOT_APPLICABLE)
    interval = state.interval
    if interval is FootballInterval.FINAL:
        return _marked(state, spent, timeouts=None, challenges=None)
    if interval is FootballInterval.HALFTIME:
        receiver = ledger.second_half_receiver()
        coverage = {
            **counted,
            FootballField.POSSESSION: _from_plays(receiver),
            FootballField.TIMEOUTS: FieldCoverage.NOT_APPLICABLE,  # they start over for the second half
        }
        return _marked(state, coverage, possession=receiver, timeouts=None, challenges=challenges)
    if interval is FootballInterval.END_OF_QUARTER and state.period == _REGULATION_PERIODS:
        to_overtime = {**spent, **dict.fromkeys(_NEXT_SNAP, FieldCoverage.NOT_APPLICABLE)}
        return _marked(
            state,
            to_overtime,
            possession=None,
            down=None,
            distance=None,
            yards_to_goal=None,
            timeouts=None,
            challenges=None,
        )
    state = _marked(state, counted, timeouts=timeouts, challenges=challenges)
    if interval is None and state.down is None and (kickoff := ledger.next_kickoff(state.period)) is not None:
        kicker, kicked_from = kickoff  # the kicking side has the ball, at its own yard line
        to_goal = _FIELD - kicked_from if kicked_from is not None else None
        coverage = {
            FootballField.POSSESSION: _from_plays(kicker),
            FootballField.SPOT: _from_plays(to_goal),
            FootballField.DOWN_DISTANCE: FieldCoverage.NOT_APPLICABLE,  # a kickoff has no down
        }
        state = _marked(state, coverage, set_piece=SetPiece.KICKOFF, possession=kicker, yards_to_goal=to_goal)
    return state


def _without_plays(state: FootballGameState) -> FootballGameState:
    """A state no plays were read for. The counts only the plays give stay a gap, except where there is nothing
    to count: after the final, and the timeouts over halftime, which start over for the second half."""
    if state.interval is FootballInterval.FINAL:
        return _marked(state, dict.fromkeys(_COUNTS, FieldCoverage.NOT_APPLICABLE))
    if state.interval is FootballInterval.HALFTIME:
        return _marked(state, {FootballField.TIMEOUTS: FieldCoverage.NOT_APPLICABLE})
    return state


def _late(event: FootballEvent) -> FeedNote:
    """A late play's note: the entry by our own label, and its words for a stamped log only."""
    summary = event.summary
    words = summary.headline + (f" | {summary.detail}" if summary.detail else "")
    return FeedNote(FeedNoteKind.LATE_PLAY, entry=event.label, quoting=words)


def _board_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """What a scoreboard shows, to tell whether it moved."""
    situation = row.get("situation") or {}
    scores = ((row.get("away_team") or {}).get("score"), (row.get("home_team") or {}).get("score"))
    return (row.get("period"), row.get("clock"), scores, situation.get("possession_abbr"), situation.get("text"))


def _injury_events(tracked: _TrackedGame, row: Mapping[str, Any], now: datetime) -> list[FootballEvent]:
    """An event for each injury reported since the last look, or whose status changed.

    On a first look, only an injury reported within `INJURY_NEWS` is news; older ones are just noted.
    """
    seen = tracked.injuries
    current: dict[tuple[str, str], _Injury] = {}
    recent: set[tuple[str, str]] = set()
    for raw in row.get("injuries") or []:
        if isinstance(raw, Mapping) and isinstance(raw.get("player_name"), str):
            team = canonical_abbreviation(str(raw.get("team_abbr") or ""))
            injury = raw.get("injury") if isinstance(raw.get("injury"), str) else None
            reported = _Injury(raw["player_name"], team, injury, InjuryStatus(raw.get("state")))
            current[(reported.player, reported.team)] = reported
            reported_at = _instant(raw.get("reported_at"))
            if reported_at is not None and now - reported_at <= INJURY_NEWS:
                recent.add((reported.player, reported.team))
    tracked.injuries = {**(seen or {}), **current}
    events: list[FootballEvent] = []
    for key, reported in current.items():
        if seen is None and key not in recent:
            continue
        if seen is not None and key in seen and seen[key].status is reported.status:
            continue
        what = f" ({reported.injury})" if reported.injury else ""
        summary = PlaySummary(
            tone=PlayTone.ALERT,
            headline=f"INJURY {reported.team}".strip(),
            detail=f"{reported.player}{what}, {reported.status.words}",
        )
        event_key = f"injury:{reported.team}:{reported.player}:{reported.status.value}"
        events.append(FootballEvent(key=event_key, kind=FootballEventKind.INJURY, summary=summary))
    return events


def _play(change: Mapping[str, Any]) -> FootballPlay | None:
    """A play-change record as a typed play, or None if it isn't readable as one."""
    raw = change.get("play")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("play_number"), int):
        return None
    yards = raw.get("yards_gained")
    offense = raw.get("posteam_abbr")
    field_goal, extra_point, two_point = (
        raw.get(column) for column in ("field_goal_result", "extra_point_result", "two_point_conv_result")
    )
    return FootballPlay(
        number=raw["play_number"],
        revision=int(change.get("revision") or 0),
        play_type=FootballPlayType(raw.get("play_type")),
        quarter=int(raw.get("quarter") or 1),
        description=str(raw.get("description") or ""),
        offense=canonical_abbreviation(offense) if isinstance(offense, str) else None,
        yards_gained=yards if isinstance(yards, int) else None,
        first_down=bool(raw.get("first_down")),
        touchdown=bool(raw.get("touchdown")),
        interception=bool(raw.get("interception")),
        fumble_lost=bool(raw.get("fumble_lost")),
        penalty=bool(raw.get("penalty")),
        field_goal=FieldGoalResult(field_goal) if isinstance(field_goal, str) else None,
        extra_point=ConversionResult(extra_point) if isinstance(extra_point, str) else None,
        two_point=ConversionResult(two_point) if isinstance(two_point, str) else None,
    )


def _rows(body: Any) -> list[Mapping[str, Any]]:
    data = body.get("data") if isinstance(body, Mapping) else None
    return [row for row in data or [] if isinstance(row, Mapping)]


def _abbreviation(row: Mapping[str, Any], side: str) -> str | None:
    raw = (row.get(side) or {}).get("abbr")
    return canonical_abbreviation(raw) if isinstance(raw, str) else None


def _poll_every(body: Any) -> DurationSeconds:
    raw = ((body or {}).get("meta") or {}).get("recommended_poll_seconds")
    return DurationSeconds(int(raw)) if isinstance(raw, int) and raw > 0 else DEFAULT_POLL


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _clock_seconds(raw: str) -> int:
    minutes, _, seconds = raw.partition(":")
    return int(minutes) * 60 + int(seconds or 0)


def _interval(status_detail: str) -> FootballInterval | None:
    detail = status_detail.strip().lower()
    return next((interval for start, interval in _INTERVALS if detail.startswith(start)), None)


def _state(row: Mapping[str, Any], game: TeamGame) -> FootballGameState:
    """The scoreboard row as a football state, with what each field rests on.

    The row has no timeouts or challenges; only the plays can give those (`_with_plays`). A part the row leaves
    out shows a stand-in where the board needs one: the 1st period, a 0:00 clock, play on. The next snap's
    possession, down and spot are None when the row doesn't place them. After the final they mean nothing, and
    over halftime only possession does: who receives the second half's kickoff, which the plays may tell.
    """
    final = row["phase"] == NflPhase.POST
    detail = row.get("status_detail")
    interval = FootballInterval.FINAL if final else _interval(str(detail or ""))
    if interval is FootballInterval.HALFTIME and int(row["period"]) > _HALFTIME_PERIOD:
        interval = FootballInterval.END_OF_QUARTER  # "End of 2nd" in overtime: its second period, no halftime
    # Between quarters the situation is the next snap's, by the same team; over halftime it's stale.
    live = row["phase"] == NflPhase.IN and interval is not FootballInterval.HALFTIME
    situation = row.get("situation") if live else None
    possession: HomeAway | None = None
    down = distance = yards_to_goal = None
    if isinstance(situation, Mapping):
        holder = situation.get("possession_abbr")
        sides = {game.away.abbreviation: HomeAway.AWAY, game.home.abbreviation: HomeAway.HOME}
        possession = sides.get(canonical_abbreviation(holder)) if isinstance(holder, str) else None
        raw_down, raw_distance, raw_yards = (situation.get(k) for k in ("down", "distance", "yardline_100"))
        if possession is not None and isinstance(raw_yards, int) and 1 <= raw_yards <= 99:
            yards_to_goal = raw_yards
            if isinstance(raw_down, int) and 1 <= raw_down <= 4 and isinstance(raw_distance, int):
                down, distance = raw_down, max(raw_distance, 0)
    period, clock = int(row["period"]), row.get("clock")
    coverage = {
        FootballField.PERIOD: FieldCoverage.REPORTED if period >= 1 else FieldCoverage.DEFAULTED,
        FootballField.CLOCK: FieldCoverage.REPORTED if clock else FieldCoverage.DEFAULTED,
        FootballField.INTERVAL: FieldCoverage.REPORTED if final or detail else FieldCoverage.DEFAULTED,
        FootballField.TIMEOUTS: FieldCoverage.UNAVAILABLE,
        FootballField.CHALLENGES: FieldCoverage.UNAVAILABLE,
    }
    if live:
        coverage.update(
            {
                FootballField.POSSESSION: _from_feed(possession),
                FootballField.DOWN_DISTANCE: _from_feed(down),
                FootballField.SPOT: _from_feed(yards_to_goal),
            }
        )
    else:
        coverage.update(dict.fromkeys(_NEXT_SNAP, FieldCoverage.NOT_APPLICABLE))
        if not final:
            coverage[FootballField.POSSESSION] = FieldCoverage.UNAVAILABLE  # halftime's receiver, until the plays say
    return FootballGameState(
        period=max(period, 1),
        clock_seconds=_clock_seconds(str(clock or "0:00")),
        away_score=int(row["away_team"]["score"]),
        home_score=int(row["home_team"]["score"]),
        possession=possession,
        down=down,
        distance=distance,
        yards_to_goal=yards_to_goal,
        interval=interval,
        coverage=CoverageMap.of(coverage),
    )
