"""NFLMeta's live scores for one game at a time, put into the board's football types.

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
- **The final**, for `FINAL_DRAIN`, ending with one last read at or after its end. If that last read
  doesn't finish, the gap is noted (`FeedNoteKind.FINAL_INCOMPLETE`), and the game settles anyway.

A read of the feed keeps what it got: each page that arrives is kept, the place in the feed moves only
past what was kept, and a read that fails partway carries on from there at the next poll. Only plays
that were read and kept count toward being caught up.

Each entry a play brings is dated by when the feed wrote it (`FootballEvent.written_at`): the earliest
change to that play in the read that brought it. A correction (a ruling, a try) is dated by the change
that carried it, not by the play it corrects. That's how a board tells a play that reached it late.

Raw JSON stays in this module: what leaves it is a `NflLiveSnapshot`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from omni.core.enum import GameStatus, HomeAway, League, StrEnumMixin
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone, SetPiece
from omni.events.football import (
    ConversionResult,
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
    "FINAL_DRAIN",
    "HALFTIME_GRACE",
    "HALFTIME_HOLD",
    "INJURY_NEWS",
    "MISMATCH_RETRY",
    "NFLMETA_SOURCE",
    "FeedNote",
    "FeedNoteKind",
    "InjuryStatus",
    "NflLiveSnapshot",
    "NflMetaFetcher",
    "NflMetaLiveProvider",
    "NflPhase",
    "PlayFeedPace",
    "PlaySyncStatus",
    "SCORE_BACKSTOP",
]

NFLMETA_SOURCE = SourceRef("nflmeta")
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
    FINAL_INCOMPLETE = "final plays incomplete"  # the final's last read didn't finish: parked without the rest


@dataclass(frozen=True, slots=True)
class FeedNote:
    kind: FeedNoteKind
    detail: str


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
_CORRECTIONS = frozenset({FootballEventKind.REVIEW, FootballEventKind.CONVERSION})


@dataclass(frozen=True, slots=True, kw_only=True)
class NflLiveSnapshot:
    """One look at a game: who's playing, how it stands, and what's happened since the last look.

    ``state`` is None before kickoff. ``events`` usually trail ``state``: the scoreboard shows a play's
    result before the play itself arrives, and ``caught_up`` says whether it has. ``settled`` means the
    final is in and the feed has had its time for late plays and fixes: there's nothing left to ask.
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
    halftime_seen: datetime | None = None  # when this game was first seen at halftime
    joined_at_half: bool = False  # the provider's first look at it was at halftime
    halftime_at: datetime | None = None  # the break's onset, set once the half's plays are in: the hold runs from it
    break_timed: bool = True  # whether the onset is known well enough to count the break down
    final_at: datetime | None = None  # when this game was first seen final
    settled: bool = False  # the final's drain has had its last read: nothing is left to ask
    cursor: str | None = None
    play_ids: set[str] = field(default_factory=set)  # every play the change feed has delivered and we kept
    written_at: dict[int, datetime] = field(default_factory=dict)  # when the feed first wrote each play
    read_for: str | None = None  # the matched play the feed was last read through for, so it's read once
    missing: tuple[str, datetime] | None = None  # a matched play a complete read lacked, and since when
    draining: bool = False  # the last read stopped short of the feed's end; the next poll reads on
    matched_board: tuple[Any, ...] | None = None  # the scoreboard as of the last matched play
    watch_until: datetime | None = None  # reading the feed every poll until then, for a correction
    injuries: dict[tuple[str, str], _Injury] | None = None  # None until the first look
    caught_up_plays: bool = False  # the play feed has been read to its end since the last (re)start
    stale_situation: str | None = None  # a down line the feed left behind when the score moved
    last_situation: str | None = None
    last_scores: tuple[int, int] | None = None
    shown_points: dict[HomeAway, int] = field(default_factory=dict)  # scored from the scoreboard alone
    gaps: dict[HomeAway, tuple[datetime, int]] = field(default_factory=dict)  # since, and the newest play then
    baselined: bool = False  # gaps already open on the first full look are old news, not events


@dataclass(slots=True)
class _PlayBatch:
    """One read of the play feed: what arrived, and where in the feed it leaves us."""

    cursor: str | None
    plays: list[FootballPlay] = field(default_factory=list)
    kept_ids: set[str] = field(default_factory=set)
    written_at: dict[int, datetime] = field(default_factory=dict)
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

    def find_game(self, away: str, home: str) -> int | None:
        """Today's game between ``away`` and ``home`` (any spelling a feed uses), or None."""
        wanted = (canonical_abbreviation(away), canonical_abbreviation(home))
        for row in _rows(self._get("/api/v1/live-scores", {})):
            if (_abbreviation(row, "away_team"), _abbreviation(row, "home_team")) == wanted:
                return int(row["game_id"])
        return None

    def holding(self, game_id: int, now: datetime) -> bool:
        """Whether ``game_id`` is inside its halftime hold, when polling it makes no request."""
        tracked = self._games.get(game_id)
        return (
            tracked is not None
            and tracked.halftime_at is not None
            and tracked.latest is not None
            and now - tracked.halftime_at < self._hold
        )

    def poll(self, game_id: int, now: datetime) -> NflLiveSnapshot:
        """The game as it stands; inside the halftime hold, the last answer (with no new events), unasked.

        Raises `ProviderError` when the feed fails or no longer lists the game.
        """
        tracked = self._games.setdefault(game_id, _TrackedGame())
        if self.holding(game_id, now) and tracked.latest is not None:
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
            state = dataclasses.replace(state, possession=None, down=None, distance=None, yards_to_goal=None)
        if phase is NflPhase.POST and tracked.final_at is None:
            tracked.final_at = now
        at_half = state is not None and state.interval is FootballInterval.HALFTIME
        # A break drains the feed: halftime until its hold begins, the final until a read at its drain's end.
        final_read = tracked.final_at is not None and not tracked.settled
        draining = (at_half and tracked.halftime_at is None) or final_read
        ledger = tracked.ledger if tracked.ledger is not None else PlayLedger(game.away, game.home)
        notes: list[FeedNote] = []
        plays = self._follow_plays(game_id, tracked, ledger, row, now, drain=draining, notes=notes)
        tracked.ledger = ledger
        if final_read and tracked.final_at is not None and now - tracked.final_at >= FINAL_DRAIN:
            tracked.settled = True  # that was the drain's last read
            if tracked.draining:
                notes.append(FeedNote(FeedNoteKind.FINAL_INCOMPLETE, "the last read after the final didn't finish"))
        scored = _unexplained_points(game, tracked, ledger, state, now) if state is not None else []
        events = scored + plays + _injury_events(tracked, row, now)
        if state is not None:
            state = _with_plays(state, ledger, postseason=_postseason(row), known=tracked.caught_up_plays)
        caught_up = _caught_up(row, phase, tracked, ledger)
        if at_half and tracked.halftime_at is None:
            _drain_half(tracked, ledger, now, first_look=first_look, caught_up=caught_up)
        timed = at_half and tracked.halftime_at is not None and tracked.break_timed
        tracked.latest = NflLiveSnapshot(
            game=game,
            phase=phase,
            state=state,
            events=tuple(events),
            caught_up=caught_up,
            break_ends_at=tracked.halftime_at + self._hold if timed and tracked.halftime_at else None,
            observed_at=_instant(row.get("observed_at")),
            poll_every=_poll_every(body),
            settled=tracked.settled,
            notes=tuple(notes),
        )
        return tracked.latest

    def _follow_plays(
        self,
        game_id: int,
        tracked: _TrackedGame,
        ledger: PlayLedger,
        row: Mapping[str, Any],
        now: datetime,
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
                tracked.watch_until = now + CORRECTION_WATCH  # the scoreboard moved with no new play
            tracked.matched_board = board
        if tracked.watch_until is not None and now < tracked.watch_until:
            read = True
        if not read:
            return []
        batch = self._read_plays(game_id, tracked)
        announce = tracked.caught_up_plays and not batch.restarted
        events = ledger.absorb(batch.plays, announce=announce)
        if not announce and not batch.complete:
            events = []  # still catching up on the game: nothing is news until all of it is in
        events = [_dated(event, batch.written_at) for event in events]
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
                _read_through(tracked, play_id, now, notes)
        if any(event.kind in _CORRECTIONS for event in events):
            tracked.watch_until = None
        notes += batch.notes
        notes += [FeedNote(FeedNoteKind.LATE_PLAY, _event_words(event)) for event in ledger.held_back]
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
                    batch.notes.append(FeedNote(FeedNoteKind.CURSOR_RESET, str(exc)))
                else:
                    batch.notes.append(FeedNote(FeedNoteKind.PLAY_READ_FAILED, str(exc)))
                return batch
            for change in _rows(body):
                if (play := _play(change)) is None:
                    continue
                batch.plays.append(play)
                if isinstance(change.get("source_play_id"), str):
                    batch.kept_ids.add(change["source_play_id"])
                if (written := _instant(change.get("changed_at"))) is not None:
                    batch.written_at[play.number] = min(written, batch.written_at.get(play.number, written))
            meta = (body or {}).get("meta") or {}
            batch.cursor = meta.get("next_cursor") or batch.cursor
            if not meta.get("has_more"):
                batch.complete = True
                break
        return batch

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
        )

    def _get(self, path: str, query: Mapping[str, str | int]) -> Any:
        try:
            return self._fetch(path, query)
        except Exception as exc:
            raise ProviderError(f"NFLMeta fetch failed for {path}: {exc}") from exc


def _read_through(tracked: _TrackedGame, play_id: str | None, now: datetime, notes: list[FeedNote]) -> None:
    """A complete read for a matched scoreboard. Its play is read for once it's in; while it isn't, the
    feed is read for it again each poll, until `MISMATCH_RETRY` has passed since the first read lacked it."""
    if play_id is None or play_id in tracked.play_ids:
        tracked.read_for, tracked.missing = play_id, None
    elif tracked.missing is None or tracked.missing[0] != play_id:
        tracked.missing = (play_id, now)
        notes.append(FeedNote(FeedNoteKind.PLAY_MISSING, "not in the feed yet: reading again next poll"))
    elif now - tracked.missing[1] >= MISMATCH_RETRY:
        tracked.read_for, tracked.missing = play_id, None
        notes.append(FeedNote(FeedNoteKind.PLAY_MISSING, "still not in the feed: waiting for the next play"))


def _dated(event: FootballEvent, written: Mapping[int, datetime]) -> FootballEvent:
    """``event``, dated by when the feed wrote its play in the read that brought it, if the feed said."""
    if event.play is None or event.play not in written:
        return event
    return dataclasses.replace(event, written_at=written[event.play])


def _drain_half(tracked: _TrackedGame, ledger: PlayLedger, now: datetime, *, first_look: bool, caught_up: bool) -> None:
    """Halftime, before its hold: the hold begins once the half's plays are in, or after `HALFTIME_GRACE`.

    Seen live, the half's last play is in once the scoreboard's play has landed, and the break began
    when halftime was first seen. Joined mid-break, the game so far has to be read through first; the
    break began when the feed wrote the half's last play, when that read finished and says so.
    """
    if tracked.halftime_seen is None:
        tracked.halftime_seen, tracked.joined_at_half = now, first_look
    seen = tracked.halftime_seen
    read_all = tracked.caught_up_plays and not tracked.draining
    if not (read_all if tracked.joined_at_half else caught_up) and now - seen < HALFTIME_GRACE:
        return
    began = tracked.written_at.get(ledger.latest) if tracked.joined_at_half and read_all else None
    tracked.halftime_at = min(began, seen) if began is not None else seen
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
    game: TeamGame, tracked: _TrackedGame, ledger: PlayLedger, state: FootballGameState, now: datetime
) -> list[FootballEvent]:
    """Scores the scoreboard shows that no play accounts for, once the feed has plainly skipped them.

    The plays usually catch up within half a minute. A score is shown from the scoreboard when the
    feed delivers a later play without it, or after `SCORE_BACKSTOP`. A try still owed after a
    touchdown in the feed is left to its own row, however late: the scoreboard already shows it.
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
        if ledger.latest > newest or now - since >= SCORE_BACKSTOP:
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


def _postseason(row: Mapping[str, Any]) -> bool:
    """Whether a game is in the postseason, by its week (the regular season's are numbered first)."""
    week = row.get("week")
    return isinstance(week, int) and week > _REGULAR_SEASON_WEEKS


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
    """
    if not known:
        return dataclasses.replace(state, timeouts=None, challenges=None)
    overtime = state.period > _REGULATION_PERIODS
    challenges = None if overtime else ledger.challenges()
    timeouts = None if overtime and postseason else ledger.timeouts(state.period)
    interval = state.interval
    if interval is FootballInterval.FINAL:
        return dataclasses.replace(state, timeouts=None, challenges=None)
    if interval is FootballInterval.HALFTIME:
        return dataclasses.replace(
            state, possession=ledger.second_half_receiver(), timeouts=None, challenges=challenges
        )
    if interval is FootballInterval.END_OF_QUARTER and state.period == _REGULATION_PERIODS:
        return dataclasses.replace(
            state, possession=None, down=None, distance=None, yards_to_goal=None, timeouts=None, challenges=None
        )
    state = dataclasses.replace(state, timeouts=timeouts, challenges=challenges)
    if interval is None and state.down is None and (kickoff := ledger.next_kickoff(state.period)) is not None:
        kicker, kicked_from = kickoff  # the kicking side has the ball, at its own yard line
        to_goal = _FIELD - kicked_from if kicked_from is not None else None
        state = dataclasses.replace(state, set_piece=SetPiece.KICKOFF, possession=kicker, yards_to_goal=to_goal)
    return state


def _event_words(event: FootballEvent) -> str:
    summary = event.summary
    return summary.headline + (f" | {summary.detail}" if summary.detail else "")


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
    final = row["phase"] == NflPhase.POST
    interval = FootballInterval.FINAL if final else _interval(str(row.get("status_detail") or ""))
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
    return FootballGameState(
        period=max(int(row["period"]), 1),
        clock_seconds=_clock_seconds(str(row.get("clock") or "0:00")),
        away_score=int(row["away_team"]["score"]),
        home_score=int(row["home_team"]["score"]),
        possession=possession,
        down=down,
        distance=distance,
        yards_to_goal=yards_to_goal,
        interval=interval,
    )
