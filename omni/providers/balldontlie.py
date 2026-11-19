"""BALLDONTLIE's NBA feed, read into the board's basketball types. Raw JSON stops here.

**The slate.** `poll_slate` reads one date's games (``/v1/games?dates[]=``, cursor-paged) into a `SlateGame` a
row: the contest (its clubs, tip and status), the phase a card shows, and either a `SlateRead` of the game's state,
for a game under way or over, or the `Pregame` a game not yet under way shows: scheduled with its tip, or postponed.

A row's phase comes from its ``status_state``, then its ``time``:

- ``scheduled`` is scheduled, or postponed when the row says ``postponed``; ``postponed`` is postponed. A postponed
  game shows no tip: the row's ``datetime`` is the slot it was moved off, and a new one isn't known.
- ``in_progress``, and ``delayed`` once a period has begun, read the ``time``: a clock (``Q3 6:07``, ``Q4 :48.2``)
  is live; ``END Q1`` is a period's end, as is ``Final`` before the status turns final; ``Half`` (or a ``Halftime``
  status) is halftime; no time, or the tip's instant, is live with no clock yet. A ``delayed`` game with no period
  yet is still scheduled.
- ``final`` is final.
- ``canceled``, ``abandoned`` and ``suspended`` are called off: the contest says which, and a card has no phase.
- Anything else (BALLDONTLIE's own ``unknown`` among them) is `Unreadable`, as is a ``time`` in no form above.

The ``period`` field is the period; the ``time``'s own label (``Q3``, ``END Q1``) only says which form it is, so a
late overtime the feed labels some other way still reads. The points by period are ``*_q1`` to ``*_q4``, then
``*_ot1`` to ``*_ot3``: the feed fills a regulation quarter not yet played with 0, so the line runs only to the
period under way. There are only three overtime columns, so in a fourth overtime or later the line score stops at
the third: the later periods have no points of their own, and the score is still the feed's whole score. A final
counts at least as many periods as it has overtime points, since some old overtime finals read period 4.

**The player lines.** `poll_lines` reads every player's line so far in the games it names, all in one request
(``/v1/stats?game_ids[]=``, cursor-paged), and hands on a `LinesRead` for each game whose rows came back:

- each row's side is the side whose club the row's ``team.id`` names, as the game's slate row numbered its clubs, so
  a game's lines are read only once a slate poll has read the game;
- the rows keep the order the feed sent them in, which is by the stat row's ``id``;
- the read's period and clock are the stat rows' own (``game.period`` and ``game.time``), from the newest page;
- every player who has appeared stays: a row the feed stops sending keeps its last line, in its place by its row's
  ``id``;
- a row whose numbers break a line's rules (points that don't add up, more makes than attempts), or that can't be
  read, is `Unreadable`, and its player keeps their last line.

A line names its player in the board's short form (`short_name`): ``A.Edwards``.

**Fetching.** The HTTP fetch is injected (`BdlFetch`) and holds the key: the provider never sees it, so nothing it
returns can carry it. `BalldontlieHttp` is the fetch over HTTPS. Every request is an `Exchange`: the answer's status
and rate-limit window, or why no answer came. A 401 or 403 (the key refused, or an endpoint above its tier), a 429
(the minute spent, with its ``retry-after``), another status, a timeout, no answer at all, and a body that isn't the
endpoint's JSON each come back as a `FetchOutcome`, never as an exception. A poll reads every page or none: one that
fails partway hands on nothing, and the next poll reads it all again.

Each poll is stamped on arrival with the injected clock's monotonic reading, so a game's reads come out in the order
they arrived.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, TypeGuard
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from omni.core.clock import Clock, MonotonicTime
from omni.core.enum import GameStatus, HomeAway, League, SeasonStage, StrEnumMixin
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.basketball import (
    REGULATION_PERIODS,
    UNSTARTED,
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    GameClock,
    PlayerLine,
)
from omni.domain.basketball_reads import LinesRead, PlayerKey, RosterLine, SlateRead
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.providers.nba_teams import NbaTeamRegistry

__all__ = [
    "BALLDONTLIE_SOURCE",
    "BASE_URL",
    "PAGE_SIZE",
    "BalldontlieHttp",
    "BalldontlieProvider",
    "BdlAnswer",
    "BdlFetch",
    "BdlQuery",
    "CupStage",
    "Exchange",
    "FeedStatus",
    "FetchOutcome",
    "GameLines",
    "LinesPoll",
    "Poll",
    "Pregame",
    "RateLimit",
    "RowProblem",
    "SlateGame",
    "SlatePoll",
    "Unreadable",
    "short_name",
]

BALLDONTLIE_SOURCE = SourceRef("balldontlie")
BASE_URL = "https://api.balldontlie.io"
PAGE_SIZE = 100  # the most rows a page holds
_MAX_PAGES = 30  # a slate fits in one page; past this many the cursor isn't ending
_OVERTIME_COLUMNS = 3  # ``*_ot1`` to ``*_ot3``
_USER_AGENT = "omni-scoreboard"

BdlQuery = tuple[tuple[str, str], ...]
"""A request's query as name and value pairs, in order; a list parameter (``game_ids[]``) repeats its name."""


@dataclass(frozen=True, slots=True)
class BdlAnswer:
    """One HTTP answer: its status, its body as text, and its headers, names lowercased."""

    status: int
    body: str
    headers: Mapping[str, str]


BdlFetch = Callable[[str, BdlQuery], BdlAnswer]
"""Asks a path with its query and returns whatever answer came back, any status included. It raises only when no
answer came (a timeout, a refused connection). It holds the key, so the provider never does."""


class FetchOutcome(StrEnumMixin, str, Enum):
    """What a request came to."""

    ANSWERED = "answered"  # a 200 whose body is the endpoint's JSON
    UNAUTHORIZED = "unauthorized"  # 401 or 403: the key was refused, or the endpoint is above the key's tier
    RATE_LIMITED = "rate_limited"  # 429: the minute's requests are spent
    HTTP_ERROR = "http_error"  # any other status
    TIMEOUT = "timeout"  # no answer in time
    NO_ANSWER = "no_answer"  # no answer at all: a refused connection, a failed lookup
    MALFORMED = "malformed"  # a 200 whose body isn't the endpoint's JSON, or whose pages never end


_STATUS_OUTCOMES = {
    401: FetchOutcome.UNAUTHORIZED,
    403: FetchOutcome.UNAUTHORIZED,
    429: FetchOutcome.RATE_LIMITED,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimit:
    """The minute's window as an answer's headers left it: its size, what's left, and when it resets."""

    limit: int
    remaining: int
    resets_at: datetime

    @classmethod
    def of(cls, headers: Mapping[str, str]) -> RateLimit | None:
        """The window ``x-ratelimit-limit``, ``-remaining`` and ``-reset`` (an epoch second) describe; None when
        any of them is missing or unreadable, as on a 401."""
        try:
            limit = int(headers["x-ratelimit-limit"])
            remaining = int(headers["x-ratelimit-remaining"])
            resets_at = datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), timezone.utc)
        except (KeyError, ValueError, OverflowError, OSError):
            return None
        return cls(limit=limit, remaining=remaining, resets_at=resets_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class Exchange:
    """One request and what it came to: the answer's status and rate-limit window, or why no answer came.

    ``retry_after`` is how long a 429 says to wait, when it says. ``detail`` says why a request failed, in the
    provider's words; the key is never in it.
    """

    path: str
    outcome: FetchOutcome
    status: int | None = None
    rate_limit: RateLimit | None = None
    retry_after: timedelta | None = None
    detail: str | None = None


class RowProblem(StrEnumMixin, str, Enum):
    """Why a row couldn't be read."""

    MALFORMED = "malformed"  # a field missing, or not of its type
    UNKNOWN_STATUS = "unknown_status"  # a status the provider doesn't read
    UNKNOWN_TIME = "unknown_time"  # a ``time`` in no form the provider reads
    UNKNOWN_TEAM = "unknown_team"  # a club the registry doesn't know
    IMPOSSIBLE = "impossible"  # fields that can't all be true at once, like halftime in the third period
    INVALID_LINE = "invalid_line"  # a player's numbers that break a line's rules, like more makes than attempts
    WRONG_TEAM = "wrong_team"  # a stat row whose club is neither of its game's
    STRAY = "stray"  # a stat row for a game the poll didn't name
    NOT_ON_SLATE = "not_on_slate"  # lines for a game no slate poll has read, so its sides aren't known


@dataclass(frozen=True, slots=True, kw_only=True)
class Unreadable:
    """A row the provider couldn't read: why, the game and player it names when it names them, and a line for the
    log."""

    problem: RowProblem
    detail: str
    game: LeagueScopedId | None = None
    player: PlayerKey | None = None

    @property
    def line(self) -> str:
        names = [f"game {self.game.raw}"] if self.game is not None else []
        names += [f"player {self.player.value}"] if self.player is not None else []
        return ": ".join((str(self.problem), *([" ".join(names)] if names else []), self.detail))


class FeedStatus(StrEnumMixin, str, Enum):
    """A game's ``status_state``, as BALLDONTLIE names it."""

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"
    DELAYED = "delayed"
    SUSPENDED = "suspended"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


_GAME_STATUS = {
    FeedStatus.SCHEDULED: GameStatus.SCHEDULED,
    FeedStatus.IN_PROGRESS: GameStatus.LIVE,
    FeedStatus.FINAL: GameStatus.FINAL,
    FeedStatus.POSTPONED: GameStatus.POSTPONED,
    FeedStatus.CANCELED: GameStatus.CANCELED,
    FeedStatus.DELAYED: GameStatus.DELAYED,
    FeedStatus.SUSPENDED: GameStatus.SUSPENDED,
    FeedStatus.ABANDONED: GameStatus.CANCELED,  # stopped, never to be finished
}
_CALLED_OFF = frozenset({FeedStatus.CANCELED, FeedStatus.ABANDONED, FeedStatus.SUSPENDED})
_UNDER_WAY_OR_OVER = frozenset(
    {BasketballPhase.LIVE, BasketballPhase.END_OF_PERIOD, BasketballPhase.HALFTIME, BasketballPhase.FINAL}
)


class CupStage(StrEnumMixin, str, Enum):
    """The NBA Cup stage a game is played in, read from ``ist_stage``.

    The stage's words aren't documented and no recorded game had one, so they're read loosely: a stage naming a
    group, a quarterfinal or a semifinal is that; any other stage naming a final or a championship is the
    championship, the one Cup game that doesn't count toward the records.
    """

    GROUP = "group"
    QUARTERFINAL = "quarterfinal"
    SEMIFINAL = "semifinal"
    CHAMPIONSHIP = "championship"
    OTHER = "other"

    @classmethod
    def of(cls, raw: Any) -> CupStage | None:
        """The stage ``ist_stage`` names; None for a game outside the Cup."""
        if raw is None or raw == "":
            return None
        text = str(raw).lower()
        for words, stage in _CUP_WORDS:
            if any(word in text for word in words):
                return stage
        return cls.OTHER


_CUP_WORDS: tuple[tuple[tuple[str, ...], CupStage], ...] = (  # checked in order: "semifinal" names a final too
    (("group",), CupStage.GROUP),
    (("quarter",), CupStage.QUARTERFINAL),
    (("semi",), CupStage.SEMIFINAL),
    (("final", "championship"), CupStage.CHAMPIONSHIP),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Pregame:
    """What a game not yet under way shows: scheduled, with its tip, or postponed, with no tip until a new one is
    known."""

    phase: BasketballPhase
    tip: datetime | None

    def __post_init__(self) -> None:
        if self.phase not in UNSTARTED:
            raise ValueError("a pregame is for a game not yet under way")
        if (self.tip is None) != (self.phase is BasketballPhase.POSTPONED):
            raise ValueError("a scheduled game has its tip, and a postponed one none")


@dataclass(frozen=True, slots=True, kw_only=True)
class SlateGame:
    """One slate row, read: the contest, the feed's status, and the phase a card shows (None when it's called off).

    A game under way or over has its ``read``; one not yet under way has its ``pregame``. ``cup`` is its NBA Cup
    stage, if it's a Cup game.
    """

    game: TeamGame
    status: FeedStatus
    phase: BasketballPhase | None
    read: SlateRead | None = None
    pregame: Pregame | None = None
    cup: CupStage | None = None

    def __post_init__(self) -> None:
        if (self.read is not None) != (self.phase in _UNDER_WAY_OR_OVER):
            raise ValueError("a game under way or over has its read, and only then")
        if (self.pregame is not None) != (self.phase in UNSTARTED):
            raise ValueError("a game not yet under way has its pregame, and only then")
        if self.read is not None and self.read.state.phase is not self.phase:
            raise ValueError("the read's phase is the game's")
        if self.pregame is not None and self.pregame.phase is not self.phase:
            raise ValueError("the pregame's phase is the game's")


@dataclass(frozen=True, slots=True, kw_only=True)
class Poll:
    """One poll: when it arrived, what it came to, every request it made, and the rows it couldn't read.

    A poll answers only when every page did; ``detail`` says why one that didn't failed.
    """

    at: MonotonicTime
    outcome: FetchOutcome
    exchanges: tuple[Exchange, ...] = ()
    unreadable: tuple[Unreadable, ...] = ()
    detail: str | None = None

    @property
    def answered(self) -> bool:
        return self.outcome is FetchOutcome.ANSWERED

    @property
    def complete(self) -> bool:
        """Answered, with every row read: what it leaves out isn't there."""
        return self.answered and not self.unreadable

    @property
    def rate_limit(self) -> RateLimit | None:
        """The rate-limit window as the poll's last answer that gave one left it."""
        return next((each.rate_limit for each in reversed(self.exchanges) if each.rate_limit is not None), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class SlatePoll(Poll):
    """A slate poll: a `SlateGame` for each row it could read."""

    games: tuple[SlateGame, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class GameLines:
    """One game's lines, as a poll read them."""

    game: LeagueScopedId
    read: LinesRead


@dataclass(frozen=True, slots=True, kw_only=True)
class LinesPoll(Poll):
    """A lines poll: a read for each game whose rows came back. Each row it couldn't read is `Unreadable`, naming
    its game and player where it can."""

    games: tuple[GameLines, ...] = ()


@dataclass(frozen=True, slots=True)
class _Kept:
    """A player's last good line, under the stat row's ``id`` the feed orders its rows by."""

    row: int
    roster: RosterLine


@dataclass(slots=True)
class _Tracked:
    """What the provider remembers of a game between polls: its clubs' ids in the feed, which say whose side a stat
    row is on, and each player's last good line."""

    away_team: int
    home_team: int
    lines: dict[PlayerKey, _Kept] = field(default_factory=dict)


@dataclass(slots=True)
class _Pages:
    """A paged read: its rows in page order, its requests, and what it came to."""

    rows: list[Any] = field(default_factory=list)
    exchanges: list[Exchange] = field(default_factory=list)
    outcome: FetchOutcome = FetchOutcome.ANSWERED
    detail: str | None = None


class _Unreadable(Exception):
    """A row can't be read, and why."""

    def __init__(self, problem: RowProblem, detail: str) -> None:
        super().__init__(detail)
        self.problem = problem
        self.detail = detail


class BalldontlieProvider:
    """Reads BALLDONTLIE's NBA slate into typed games, each poll stamped on arrival."""

    def __init__(self, registry: NbaTeamRegistry, *, fetch: BdlFetch, clock: Clock) -> None:
        self._registry = registry
        self._fetch = fetch
        self._clock = clock
        self._games: dict[int, _Tracked] = {}

    def poll_slate(self, day: date) -> SlatePoll:
        """The games BALLDONTLIE files under ``day`` (a US Eastern date), every page read."""
        pages = self._pages("/v1/games", (("dates[]", day.isoformat()), ("per_page", str(PAGE_SIZE))), _MAX_PAGES)
        at = self._clock.monotonic()
        if pages.outcome is not FetchOutcome.ANSWERED:
            return SlatePoll(at=at, outcome=pages.outcome, exchanges=tuple(pages.exchanges), detail=pages.detail)
        games: list[SlateGame] = []
        unreadable: list[Unreadable] = []
        for row in pages.rows:
            try:
                game, clubs = self._slate_game(row, at)
            except _Unreadable as exc:
                unreadable.append(Unreadable(problem=exc.problem, detail=exc.detail, game=_game_id(row)))
                continue
            games.append(game)
            self._track(int(game.game.id.raw), *clubs)
        return SlatePoll(
            at=at,
            outcome=FetchOutcome.ANSWERED,
            exchanges=tuple(pages.exchanges),
            unreadable=tuple(unreadable),
            games=tuple(games),
        )

    def poll_lines(self, games: Collection[LeagueScopedId]) -> LinesPoll:
        """Every player's line so far in each of ``games``, read together, every page read: a read for each game
        whose rows came back. Raises `ValueError` for no games, or one BALLDONTLIE doesn't number."""
        wanted = sorted({_feed_id(game) for game in games})
        if not wanted:
            raise ValueError("a lines poll names at least one game")
        query = (*(("game_ids[]", str(game_id)) for game_id in wanted), ("per_page", str(PAGE_SIZE)))
        pages = self._pages("/v1/stats", query, _MAX_PAGES)
        at = self._clock.monotonic()
        if pages.outcome is not FetchOutcome.ANSWERED:
            return LinesPoll(at=at, outcome=pages.outcome, exchanges=tuple(pages.exchanges), detail=pages.detail)
        rows: dict[int, list[Mapping[str, Any]]] = {game_id: [] for game_id in wanted}
        unreadable: list[Unreadable] = []
        for row in pages.rows:
            game_id = _stat_game(row)
            if game_id is None:
                unreadable.append(Unreadable(problem=RowProblem.MALFORMED, detail="a stat row that names no game"))
            elif game_id not in rows:
                unreadable.append(Unreadable(problem=RowProblem.STRAY, detail="not asked for", game=_scoped(game_id)))
            else:
                rows[game_id].append(row)
        read = [self._game_lines(game_id, each, at, unreadable) for game_id, each in rows.items() if each]
        return LinesPoll(
            at=at,
            outcome=FetchOutcome.ANSWERED,
            exchanges=tuple(pages.exchanges),
            unreadable=tuple(unreadable),
            games=tuple(lines for lines in read if lines is not None),
        )

    def _track(self, game_id: int, away_team: int, home_team: int) -> None:
        """Remember a game's clubs by their feed ids, keeping the lines already read."""
        tracked = self._games.get(game_id)
        if tracked is None:
            self._games[game_id] = _Tracked(away_team, home_team)
        else:
            tracked.away_team, tracked.home_team = away_team, home_team

    def _game_lines(
        self, game_id: int, rows: list[Mapping[str, Any]], at: MonotonicTime, unreadable: list[Unreadable]
    ) -> GameLines | None:
        """One game's rows as its read, every player who has appeared in it in the feed's order; None, with why in
        ``unreadable``, when it can't be read."""
        game = _scoped(game_id)
        tracked = self._games.get(game_id)
        if tracked is None:
            unreadable.append(Unreadable(problem=RowProblem.NOT_ON_SLATE, detail="its clubs aren't known", game=game))
            return None
        try:
            period, clock = _view(rows[-1])
        except _Unreadable as exc:
            unreadable.append(Unreadable(problem=exc.problem, detail=exc.detail, game=game))
            return None
        sent: dict[PlayerKey, _Kept] = {}
        for row in rows:
            key: PlayerKey | None = None
            try:
                key = _player_key(row)
                sent[key] = _kept(row, key, tracked)
            except _Unreadable as exc:
                unreadable.append(Unreadable(problem=exc.problem, detail=exc.detail, game=game, player=key))
        kept = sorted((each for key, each in tracked.lines.items() if key not in sent), key=lambda each: each.row)
        lines = _in_feed_order(list(sent.values()), kept)
        tracked.lines.update(sent)
        return GameLines(game=game, read=LinesRead(at=at, period=period, clock=clock, lines=lines))

    def _slate_game(self, row: Any, at: MonotonicTime) -> tuple[SlateGame, tuple[int, int]]:
        """A slate row as a game, with its away and home clubs' feed ids; `_Unreadable` when it can't be read."""
        row = _object(row, "the row")
        game_id = LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(_int(row, "id")))
        away_row = _object(row.get("visitor_team"), "visitor_team")
        home_row = _object(row.get("home_team"), "home_team")
        clubs = (_int(away_row, "id"), _int(home_row, "id"))
        status = _status(row)
        tip = _instant(row.get("datetime"))
        if tip is None:
            raise _Unreadable(RowProblem.MALFORMED, "datetime isn't an instant")
        try:
            game = TeamGame(
                id=game_id,
                league=League.NBA,
                status=_GAME_STATUS[status],
                scheduled_start=tip,
                away=self._team(away_row),
                home=self._team(home_row),
                stage=SeasonStage.POSTSEASON if _flag(row, "postseason") else SeasonStage.REGULAR,
                provider_date=_date(row),
            )
            phase, state = _phase(row, status)
            pregame = None
            if phase is not None and phase in UNSTARTED:
                pregame = Pregame(phase=phase, tip=None if phase is BasketballPhase.POSTPONED else tip)
            slate_game = SlateGame(
                game=game,
                status=status,
                phase=phase,
                read=SlateRead(at=at, state=state) if state is not None else None,
                pregame=pregame,
                cup=CupStage.of(row.get("ist_stage")),
            )
            return slate_game, clubs
        except ValueError as exc:
            raise _Unreadable(RowProblem.IMPOSSIBLE, str(exc)) from exc

    def _team(self, team: Mapping[str, Any]) -> Team:
        abbreviation = team.get("abbreviation")
        if not isinstance(abbreviation, str):
            raise _Unreadable(RowProblem.MALFORMED, "a club has no abbreviation")
        try:
            return self._registry.resolve(abbreviation)
        except KeyError:
            raise _Unreadable(RowProblem.UNKNOWN_TEAM, f"no NBA club {abbreviation!r}") from None

    def _pages(self, path: str, query: BdlQuery, max_pages: int) -> _Pages:
        """Every page of a cursor-paged list, joined in page order; at the first request that fails, nothing."""
        pages = _Pages()
        cursor: str | None = None
        for _ in range(max_pages):
            exchange, body = self._ask(path, query if cursor is None else (*query, ("cursor", cursor)))
            pages.exchanges.append(exchange)
            if body is None:
                pages.rows, pages.outcome, pages.detail = [], exchange.outcome, exchange.detail
                return pages
            pages.rows.extend(body["data"])
            cursor = _cursor(body)
            if cursor is None:
                return pages
        pages.rows, pages.outcome = [], FetchOutcome.MALFORMED
        pages.detail = f"the pages didn't end in {max_pages} requests"
        return pages

    def _ask(self, path: str, query: BdlQuery) -> tuple[Exchange, Mapping[str, Any] | None]:
        """One request: what it came to, and the body when it's the endpoint's JSON."""
        try:
            answer = self._fetch(path, query)
        except Exception as exc:  # a fetch that got no answer is an outcome the caller acts on, never an escape
            outcome = FetchOutcome.TIMEOUT if _timed_out(exc) else FetchOutcome.NO_ANSWER
            return Exchange(path=path, outcome=outcome, detail=f"no answer ({type(exc).__name__})"), None
        headers = {name.lower(): value for name, value in answer.headers.items()}
        rate_limit = RateLimit.of(headers)
        if answer.status != 200:
            outcome = _STATUS_OUTCOMES.get(answer.status, FetchOutcome.HTTP_ERROR)
            return (
                Exchange(
                    path=path,
                    outcome=outcome,
                    status=answer.status,
                    rate_limit=rate_limit,
                    retry_after=_retry_after(headers) if outcome is FetchOutcome.RATE_LIMITED else None,
                    detail=f"HTTP {answer.status}",
                ),
                None,
            )
        body = _listing(answer.body)
        if body is None:
            detail = "the body isn't the endpoint's JSON"
            return (
                Exchange(path=path, outcome=FetchOutcome.MALFORMED, status=200, rate_limit=rate_limit, detail=detail),
                None,
            )
        return Exchange(path=path, outcome=FetchOutcome.ANSWERED, status=200, rate_limit=rate_limit), body


class BalldontlieHttp:
    """The fetch over HTTPS: the key goes in the ``Authorization`` header and nowhere else, and a request carries
    nothing about who's asking past a generic user agent.

    Every HTTP status comes back as an answer; only a failure with no answer raises. One whose message would name
    the key raises without it.
    """

    def __init__(
        self, key: str, *, timeout: float = 10.0, opener: Callable[..., Any] = urlopen, base_url: str = BASE_URL
    ) -> None:
        if not key.strip():
            raise ValueError("a BALLDONTLIE key is needed")
        self._key = key
        self._timeout = timeout
        self._open = opener
        self._base = base_url

    def __repr__(self) -> str:
        return f"BalldontlieHttp(base_url={self._base!r})"

    def __call__(self, path: str, query: BdlQuery) -> BdlAnswer:
        url = self._base + path + (f"?{urlencode(query, safe='[]')}" if query else "")
        request = Request(url, headers={"Authorization": self._key, "User-Agent": _USER_AGENT})
        try:
            with self._open(request, timeout=self._timeout) as response:
                return BdlAnswer(response.status, _text(response.read()), _lowered(response.headers))
        except HTTPError as exc:
            return BdlAnswer(exc.code, _text(exc.read()), _lowered(exc.headers))
        except Exception as exc:
            if self._key in str(exc):
                raise ConnectionError(f"{type(exc).__name__}, its message withheld: it named the key") from None
            raise


def _feed_id(game: LeagueScopedId) -> int:
    """A game's number in the feed; `ValueError` for an id the feed didn't give."""
    if game.league is not League.NBA or game.source != BALLDONTLIE_SOURCE or not game.raw.isdigit():
        raise ValueError(f"{game} isn't a BALLDONTLIE NBA game")
    return int(game.raw)


def _scoped(game_id: int) -> LeagueScopedId:
    return LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(game_id))


def _stat_game(row: Any) -> int | None:
    """The game a stat row is for; None when it names none."""
    game = row.get("game") if isinstance(row, dict) else None
    game_id = game.get("id") if isinstance(game, dict) else None
    return game_id if _is_int(game_id) else None


def _view(row: Mapping[str, Any]) -> tuple[int, GameClock | None]:
    """The period and clock a stat row has the game at: no clock at a break, at the final, or before one's read."""
    game = _object(row.get("game"), "game")
    period = _int(game, "period")
    if period < 1:
        raise _Unreadable(RowProblem.IMPOSSIBLE, "lines before the first period")
    return period, _reading(game.get("time")).clock


def _player_key(row: Mapping[str, Any]) -> PlayerKey:
    player_id = _int(_object(row.get("player"), "player"), "id")
    if player_id < 1:
        raise _Unreadable(RowProblem.MALFORMED, "player.id isn't a player's number")
    return PlayerKey(player_id)


_LINE_FIELDS = (  # a line's numbers, by the feed's names
    ("points", "pts"),
    ("fg_made", "fgm"),
    ("fg_attempted", "fga"),
    ("threes_made", "fg3m"),
    ("threes_attempted", "fg3a"),
    ("ft_made", "ftm"),
    ("ft_attempted", "fta"),
    ("offensive_rebounds", "oreb"),
    ("defensive_rebounds", "dreb"),
    ("assists", "ast"),
    ("steals", "stl"),
    ("blocks", "blk"),
    ("turnovers", "turnover"),
    ("fouls", "pf"),
    ("plus_minus", "plus_minus"),
)


def _kept(row: Mapping[str, Any], key: PlayerKey, tracked: _Tracked) -> _Kept:
    """A stat row as its player's line, on the side its club is; `_Unreadable` when it can't be read."""
    row_id = _int(row, "id")
    club = _int(_object(row.get("team"), "team"), "id")
    if club not in (tracked.away_team, tracked.home_team):
        raise _Unreadable(RowProblem.WRONG_TEAM, f"club {club} isn't one of the game's")
    side = HomeAway.AWAY if club == tracked.away_team else HomeAway.HOME
    player = _object(row.get("player"), "player")
    first, last = player.get("first_name"), player.get("last_name")
    if not isinstance(first, str) or not isinstance(last, str):
        raise _Unreadable(RowProblem.MALFORMED, "the player's name isn't text")
    name = short_name(first, last)
    if not name:
        raise _Unreadable(RowProblem.MALFORMED, "the player has no name")
    numbers = {ours: _int(row, theirs) for ours, theirs in _LINE_FIELDS}
    try:
        line = PlayerLine(name=name, **numbers)
    except ValueError as exc:
        raise _Unreadable(RowProblem.INVALID_LINE, str(exc)) from None
    return _Kept(row_id, RosterLine(key=key, side=side, line=line))


def _in_feed_order(sent: list[_Kept], kept: list[_Kept]) -> tuple[RosterLine, ...]:
    """The lines a read sent, in the page's order, with each kept line the feed didn't send placed before the first
    sent one whose row comes after it (``kept`` is in row order)."""
    merged: list[_Kept] = []
    waiting = 0
    for each in sent:
        while waiting < len(kept) and kept[waiting].row < each.row:
            merged.append(kept[waiting])
            waiting += 1
        merged.append(each)
    merged += kept[waiting:]
    return tuple(each.roster for each in merged)


_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV", "V"})
_LOOKALIKES = {"‘": "'", "’": "'", "ʼ": "'", "‐": "-", "‑": "-"}
# Latin-1's letters and Latin Extended-A: the fonts a board names players in (4x6, 5x7) draw every one.
_FONT_LETTERS = range(0xC0, 0x180)


def short_name(first: str, last: str) -> str:
    """A player's name as a board shows it: the first initial, a dot, and the last name (``A.Edwards``).

    - A generational suffix ending the last name is dropped: ``Jaquez Jr.`` gives ``J.Jaquez``, ``Holland II``
      ``R.Holland``. A last name of one word is never a suffix.
    - A last name of more than one part is kept whole: ``T.Jackson-Davis``, ``T.da Silva``. Shortening it would
      trade one player for another (there are many Jacksons), and a caption's lane scrolls a long one.
    - The initial is the first name's first letter: ``T.J.`` and ``AJ`` give ``T`` and ``A``.
    - A letter the board's name fonts draw is kept, accent and all (``L.Dončić``): they draw every letter of
      Latin-1 and Latin Extended-A. Any other letter is folded to its plain ASCII letters (``Ș`` to ``S``), or left
      out when it has none. Curly apostrophes and hyphens are made straight.
    - With no first name, the last name alone; with no last name, the first name alone.
    """
    given = _drawable(first.strip())
    words = [word for word in (_drawable(part) for part in last.split()) if word]
    while len(words) > 1 and words[-1].rstrip(".").upper() in _SUFFIXES:
        words.pop()
    surname = " ".join(words).rstrip(",")
    initial = next((char for char in given if char.isalpha()), "")
    if initial and surname:
        return f"{initial.upper()}.{surname}"
    return surname or " ".join(given.split())


def _drawable(text: str) -> str:
    """``text`` with each character the name fonts can't draw folded to plain ASCII, or left out."""
    kept: list[str] = []
    for char in text:
        char = _LOOKALIKES.get(char, char)
        if " " <= char <= "~" or (ord(char) in _FONT_LETTERS and char.isalpha()):
            kept.append(char)
        else:
            kept += [part for part in unicodedata.normalize("NFKD", char) if " " <= part <= "~"]
    return "".join(kept)


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _lowered(headers: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def _timed_out(exc: Exception) -> bool:
    """Whether a fetch's failure was a timeout, raised as one or wrapped as a ``URLError``'s reason."""
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)


def _retry_after(headers: Mapping[str, str]) -> timedelta | None:
    """How long a 429's ``retry-after`` says to wait, in seconds; None when it doesn't say."""
    raw = headers.get("retry-after", "")
    return timedelta(seconds=int(raw)) if raw.isdigit() else None


def _listing(text: str) -> Mapping[str, Any] | None:
    """A body as the endpoint's JSON, an object with a ``data`` list; None when it isn't."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    return body


def _cursor(body: Mapping[str, Any]) -> str | None:
    """The next page's cursor, None on the last page."""
    meta = body.get("meta")
    cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
    if isinstance(cursor, bool) or not isinstance(cursor, (int, str)) or cursor == "":
        return None
    return str(cursor)


def _game_id(row: Any) -> LeagueScopedId | None:
    """The game a row names, when it names one."""
    raw = row.get("id") if isinstance(row, dict) else None
    return LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, str(raw)) if _is_int(raw) else None


def _status(row: Mapping[str, Any]) -> FeedStatus:
    raw = row.get("status_state")
    try:
        status = FeedStatus(raw)
    except ValueError:
        raise _Unreadable(RowProblem.UNKNOWN_STATUS, f"status_state {raw!r}") from None
    if status is FeedStatus.UNKNOWN:
        raise _Unreadable(RowProblem.UNKNOWN_STATUS, "the feed doesn't know the game's status")
    return status


def _phase(row: Mapping[str, Any], status: FeedStatus) -> tuple[BasketballPhase | None, BasketballGameState | None]:
    """The phase a row's game is in, and its state when it's under way or over."""
    if status in _CALLED_OFF:
        return None, None
    if status is FeedStatus.POSTPONED:
        return BasketballPhase.POSTPONED, None
    if status is FeedStatus.SCHEDULED:
        return _unstarted(row), None
    period = _int(row, "period")
    if status is FeedStatus.DELAYED and period < 1:
        return _unstarted(row), None
    if status is FeedStatus.FINAL:
        away, home = _columns(row, "visitor"), _columns(row, "home")
        period = max(period, REGULATION_PERIODS + _overtimes_scored(away, home))
        return BasketballPhase.FINAL, _state(row, BasketballPhase.FINAL, period, None, (away, home))
    if period < 1:
        raise _Unreadable(RowProblem.IMPOSSIBLE, "under way before the first period")
    reading = _reading(row.get("time"))
    if reading.form is _Form.HALF or row.get("status") == "Halftime":
        phase = BasketballPhase.HALFTIME
    elif reading.form in (_Form.END, _Form.FINAL):
        phase = BasketballPhase.END_OF_PERIOD
    else:
        phase = BasketballPhase.LIVE
    away, home = _columns(row, "visitor"), _columns(row, "home")
    return phase, _state(row, phase, period, reading.clock, (away, home))


def _unstarted(row: Mapping[str, Any]) -> BasketballPhase:
    """A game not yet under way: scheduled, or postponed when the row says so."""
    return BasketballPhase.POSTPONED if _flag(row, "postponed") else BasketballPhase.SCHEDULED


def _state(
    row: Mapping[str, Any],
    phase: BasketballPhase,
    period: int,
    clock: GameClock | None,
    columns: tuple[list[int | None], list[int | None]],
) -> BasketballGameState:
    away, home = (_side(row, prefix, period, points) for prefix, points in zip(("visitor", "home"), columns))
    return BasketballGameState(phase=phase, period=period, away=away, home=home, clock=clock)


def _side(row: Mapping[str, Any], prefix: str, period: int, columns: list[int | None]) -> BasketballSide:
    """One side's part of a row: its score, its points by period up to the one under way (ending early at a column
    the feed hasn't filled), its timeouts left and its bonus."""
    periods: list[int] = []
    for points in columns[:period]:
        if points is None:
            break
        periods.append(points)
    return BasketballSide(
        score=_int(row, f"{prefix}_team_score"),
        periods=tuple(periods),
        timeouts=_int_or_none(row, f"{prefix}_timeouts_remaining"),
        in_bonus=_flag(row, f"{prefix}_in_bonus"),
    )


def _columns(row: Mapping[str, Any], prefix: str) -> list[int | None]:
    """A side's points by period as the feed's columns hold them: Q1 to Q4, then each overtime column."""
    names = [f"{prefix}_q{n}" for n in range(1, REGULATION_PERIODS + 1)]
    names += [f"{prefix}_ot{n}" for n in range(1, _OVERTIME_COLUMNS + 1)]
    return [_int_or_none(row, name) for name in names]


def _overtimes_scored(away: list[int | None], home: list[int | None]) -> int:
    """How many overtime columns lead up to the last one either side scored in."""
    scored = [n for n in range(_OVERTIME_COLUMNS) if (away[REGULATION_PERIODS + n] or home[REGULATION_PERIODS + n])]
    return scored[-1] + 1 if scored else 0


class _Form(Enum):
    CLOCK = "clock"  # Q3 6:07
    END = "end"  # END Q1
    HALF = "half"  # Half
    FINAL = "final"  # Final
    UNSET = "unset"  # no time, or the tip's instant


@dataclass(frozen=True, slots=True)
class _Reading:
    form: _Form
    clock: GameClock | None = None


_LABEL = r"(?:Q\d+|OT\d*|\d+OT)"
_CLOCK = re.compile(rf"{_LABEL}\s+(\d*):(\d\d)(?:\.(\d))?")
_END = re.compile(rf"END\s+{_LABEL}")


def _reading(raw: Any) -> _Reading:
    """A ``time`` as its form, and its clock when it's one; `_Unreadable` for any other form."""
    if raw is None:
        return _Reading(_Form.UNSET)
    if not isinstance(raw, str):
        raise _Unreadable(RowProblem.MALFORMED, "time isn't text")
    text = raw.strip().upper()
    if text in ("HALF", "HALFTIME"):
        return _Reading(_Form.HALF)
    if text == "FINAL":
        return _Reading(_Form.FINAL)
    if _END.fullmatch(text):
        return _Reading(_Form.END)
    if (clock := _CLOCK.fullmatch(text)) is not None:
        minutes, seconds, tenths = clock.groups()
        try:
            return _Reading(_Form.CLOCK, GameClock.of(int(minutes or 0), int(seconds), int(tenths or 0)))
        except ValueError:
            raise _Unreadable(RowProblem.UNKNOWN_TIME, f"time {raw!r} isn't a period's clock") from None
    if _instant(raw) is not None:
        return _Reading(_Form.UNSET)
    raise _Unreadable(RowProblem.UNKNOWN_TIME, f"time {raw!r}")


def _object(raw: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise _Unreadable(RowProblem.MALFORMED, f"{what} isn't an object")
    return raw


def _is_int(raw: object) -> TypeGuard[int]:
    return isinstance(raw, int) and not isinstance(raw, bool)


def _int(row: Mapping[str, Any], name: str) -> int:
    raw = row.get(name)
    if not _is_int(raw):
        raise _Unreadable(RowProblem.MALFORMED, f"{name} isn't a whole number")
    return raw


def _int_or_none(row: Mapping[str, Any], name: str) -> int | None:
    return None if row.get(name) is None else _int(row, name)


def _flag(row: Mapping[str, Any], name: str) -> bool:
    """A yes-or-no field; one the feed leaves null is no."""
    raw = row.get(name)
    if raw is not None and not isinstance(raw, bool):
        raise _Unreadable(RowProblem.MALFORMED, f"{name} isn't true or false")
    return raw is True


def _instant(raw: Any) -> datetime | None:
    """An ISO instant with its offset; None for anything else."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _date(row: Mapping[str, Any]) -> date:
    """The US Eastern date the feed files a game under."""
    raw = row.get("date")
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise _Unreadable(RowProblem.MALFORMED, "date isn't a date") from None
