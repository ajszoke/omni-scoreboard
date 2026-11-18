"""Mine an NFLMeta recording into our own telemetry, before the recording has to be deleted.

NFLMeta's terms allow ordinary caching but no standing copy, so a recording (`tools/nflmeta_record.py`, or
``python -m omni.app.football --record``) is deleted by its stamp's date. What may be kept is what we measured
about it (`omni.providers.rights.Telemetry.OWN_DECISIONS`): how the feed and the game's breaks behaved, as our
own kinds, counts and spans. `mine_recording` turns a recording folder into that digest
(`build/telemetry/nflmeta/<folder>.json`, schema ``omni-mined/1``) and a short Markdown summary beside it.

**Through the provider.** The recorded scoreboards are replayed in arrival order through `NflMetaLiveProvider`,
behind a fetcher that answers as the recording did: each scoreboard as it arrived, and the play feed with every
change row first received before the next scoreboard. So the kinds, states, coverage and notes measured here are
the provider's own reading. The replay reads the play feed on every poll and never holds over halftime, so it
sees each row when the recording first had it. Each change row is also typed on its own, by the provider's row
reader, to follow every play's revisions; only what the provider doesn't type (a row's change id and the time the
feed wrote it) is read raw, in `_untyped`.

**What it measures:**
- **Breaks**, per `BreakKind`: a team or official timeout, the two-minute warning, a review's ruling (a play
  first seen with its ruling counts too), an injury report, a quarter break and halftime. For each, its onset (the
  feed's write time for the break's row: the play row, or for a quarter break or halftime the scoreboard row),
  its first receipt, and the next action's write and receipt. A scoreboard row that doesn't say when it was
  written dates the onset by when NFLMeta observed it instead, and those are counted per kind. The next action is
  the next snap: the next play that isn't a stoppage or a marker, by play number; after a quarter break or
  halftime, the next quarter's first; after an injury report (which has no play), the first written after the
  report arrived.
- **Feed latency:** each play's write to its first receipt, and for each move of a side's score on the
  scoreboard, how long before (or after) the plays accounted for it.
- **Order and corrections:** plays first seen after a later play; revisions after a play was first seen; plays
  missing from a later read of the whole feed (a read from its start, paged to its end), and whether they came back.
- **Cadence:** the gaps between snaps, by when the feed wrote them, each quarter's wall length against its game
  clock, the halves' and the game's.
- **Event mix:** the provider's events per kind and quarter, its notes per kind, and scoring plays per kind.
- **Billing and polling:** requests, billed rows, rows per read and empty reads per endpoint; the gaps between
  answers; the pace the feed recommended.
- **Coverage:** per `FootballField`, the share of snapshots where it was reported, inferred, defaulted,
  unavailable or not applicable (`FieldCoverage`).

**What the digest never holds:** a play's description or any other provider string, a player, a score, yards,
down and distance, or a per-play sequence. Per-break and per-score rows keep only our kind, the quarter and spans,
and are sorted, so their order doesn't retell the game. `omni.telemetry.digest.check_digest` enforces the strings.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import HomeAway, StrEnumMixin
from omni.domain.breaks import BreakKind
from omni.domain.contest import TeamGame
from omni.domain.coverage import FieldCoverage
from omni.domain.football import FootballField, FootballInterval
from omni.events.football import FieldGoalResult, FootballEvent, FootballEventKind, FootballPlay, FootballPlayType
from omni.events.football_ledger import PlayLedger
from omni.providers.base import ProviderError
from omni.providers.nfl_teams import NflTeamRegistry, canonical_abbreviation
from omni.providers.nflmeta import _play as provider_play  # the provider's own reading of one change row
from omni.providers.nflmeta import NFLMETA_SOURCE, FeedNoteKind, NflLiveSnapshot, NflMetaLiveProvider, PlayFeedPace
from omni.providers.rights import DataSource, read_stamp
from omni.telemetry.digest import (
    SCHEMA,
    SPREAD_MEASURES,
    DigestKind,
    Spread,
    allowed_values,
    figure,
    seconds,
    write_digest,
)

__all__ = ["MEASURES", "Endpoint", "ScoringKind", "mine", "mine_recording", "summary"]


class Endpoint(StrEnumMixin, str, Enum):
    """An NFLMeta endpoint a recording keeps, by the name of its file."""

    LIVE_SCORES = "live-scores"
    PLAY_CHANGES = "play-changes"


class ScoringKind(StrEnumMixin, str, Enum):
    """What a scoring play scored."""

    TOUCHDOWN = "touchdown"
    FIELD_GOAL = "field goal"
    SAFETY = "safety"
    EXTRA_POINT = "extra point"
    TWO_POINT = "two-point try"


_EVENT_BREAKS = {
    FootballEventKind.TIMEOUT: BreakKind.NFL_TEAM_TIMEOUT,
    FootballEventKind.OFFICIAL_TIMEOUT: BreakKind.NFL_OFFICIAL_TIMEOUT,
    FootballEventKind.TWO_MINUTE_WARNING: BreakKind.NFL_TWO_MINUTE_WARNING,
    FootballEventKind.REVIEW: BreakKind.NFL_REVIEW,
    FootballEventKind.INJURY: BreakKind.NFL_INJURY_TIMEOUT,
}
_INTERVAL_BREAKS = {
    FootballInterval.END_OF_QUARTER: BreakKind.NFL_QUARTER_BREAK,
    FootballInterval.HALFTIME: BreakKind.NFL_HALFTIME,
}
# Feed rows that aren't a snap: stoppages and markers. Every other play is action.
_NOT_ACTION = frozenset(
    {
        FootballPlayType.TIMEOUT,
        FootballPlayType.OFFICIAL_TIMEOUT,
        FootballPlayType.TWO_MINUTE_WARNING,
        FootballPlayType.END_PERIOD,
        FootballPlayType.END_OF_HALF,
        FootballPlayType.END_OF_GAME,
        FootballPlayType.COIN_TOSS,
    }
)
_HALVES = ((1, frozenset({1, 2})), (2, frozenset({3, 4})))
_FOLDER = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})-(?P<away>[A-Za-z]{2,3})-(?P<home>[A-Za-z]{2,3})")
_SHARE_PLACES = 4

MEASURES = SPREAD_MEASURES | frozenset(
    {
        "schema",
        "kind",
        "folder",
        "date",
        "away",
        "home",
        "replay",
        "scoreboards",
        "looks_failed",
        "unreadable_lines",
        "unreadable_rows",
        "breaks",
        "break_rows",
        "onsets_observed",
        "quarter",
        "onset_to_receipt",
        "onset_to_next_action",
        "receipt_to_next_action_receipt",
        "latency",
        "plays",
        "write_to_receipt",
        "score_moves",
        "scoreboard_first",
        "plays_first",
        "together",
        "never_explained",
        "scoreboard_lead",
        "score_rows",
        "order",
        "late_plays",
        "late_lag",
        "revised_plays",
        "revisions",
        "revision_delay",
        "history_reads",
        "vanished_plays",
        "returned_plays",
        "cadence",
        "quarters",
        "wall",
        "game_clock",
        "wall_per_clock_second",
        "snap_gap",
        "halves",
        "half",
        "game_wall",
        "events",
        "notes",
        "scoring",
        "billing",
        "endpoint",
        "requests",
        "billed_rows",
        "billed_per_read",
        "rows_per_read",
        "empty_reads",
        "polling",
        "answer_gap",
        "endpoints",
        "recommended",
        "seconds",
        "answers",
        "coverage",
        "snapshots",
        "fields",
        "field",
        *(level.value for level in FieldCoverage),
    }
)
_ENUMS: tuple[type[Enum], ...] = (
    BreakKind,
    Endpoint,
    FeedNoteKind,
    FieldCoverage,
    FootballEventKind,
    FootballField,
    ScoringKind,
)


# --- reading the recording: the only code here that touches the recorded JSON --------------------------------


@dataclass(frozen=True, slots=True)
class _Answer:
    """One recorded response: its endpoint, when it arrived, what it billed, and its body (raw)."""

    endpoint: Endpoint
    received: datetime
    billed: int | None  # the response's billed rows, when the recording kept the header
    body: Any
    cursor: str | None = None  # the place in the play feed it was asked from, when the recording says
    from_start: bool = False  # a play-feed read asked from the feed's start (the recording says so)


def _answers(folder: Path) -> tuple[list[_Answer], int]:
    """Every recorded response in ``folder``, and how many lines couldn't be read."""
    answers: list[_Answer] = []
    unreadable = 0
    for endpoint in Endpoint:
        path = folder / f"{endpoint.value}.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            answer = _answer(endpoint, line)
            if answer is None:
                unreadable += 1
            else:
                answers.append(answer)
    return answers, unreadable


def _answer(endpoint: Endpoint, line: str) -> _Answer | None:
    """One line of a recording: ``received_at``, ``rows`` and ``body``, and the asked ``query`` when the board
    wrote it (`tools/nflmeta_record.py` doesn't)."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    received = _instant(entry.get("received_at")) if isinstance(entry, Mapping) else None
    if received is None:
        return None
    query = entry.get("query")
    cursor = query.get("cursor") if isinstance(query, Mapping) else None
    asked_from_start = isinstance(query, Mapping) and "cursor" not in query and "start" not in query
    return _Answer(
        endpoint=endpoint,
        received=received,
        billed=_count(entry.get("rows")),
        body=entry.get("body"),
        cursor=cursor if isinstance(cursor, str) else None,
        from_start=endpoint is Endpoint.PLAY_CHANGES and asked_from_start,
    )


def _rows(body: Any) -> list[Mapping[str, Any]]:
    data = body.get("data") if isinstance(body, Mapping) else None
    return [row for row in data if isinstance(row, Mapping)] if isinstance(data, list) else []


def _page(body: Any) -> tuple[str | None, bool]:
    """A play-feed page's place for the next page, and whether there's more."""
    meta = body.get("meta") if isinstance(body, Mapping) else None
    meta = meta if isinstance(meta, Mapping) else {}
    cursor = meta.get("next_cursor")
    return (cursor if isinstance(cursor, str) else None), bool(meta.get("has_more"))


def _untyped(row: Mapping[str, Any]) -> tuple[int | None, datetime | None]:
    """What the provider doesn't type about a row: its change id (a play-feed row) and when the feed wrote it."""
    change_id = row.get("change_id")
    return (change_id if type(change_id) is int else None), _instant(row.get("changed_at"))


def _game_row(body: Any, game_id: int) -> Mapping[str, Any] | None:
    return next((row for row in _rows(body) if row.get("game_id") == game_id), None)


def _count(raw: Any) -> int | None:
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# --- the recorded feed -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Change:
    """One play-feed change row, as the recording first received it, typed by the provider."""

    change_id: int
    received: datetime
    written: datetime | None
    play: FootballPlay
    row: Mapping[str, Any]


def _typed(row: Mapping[str, Any]) -> FootballPlay | None:
    """A change row as the provider reads it, or None if it isn't readable as a play."""
    try:
        return provider_play(row)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class _Page:
    """One recorded play-feed answer, and the plays its rows were about."""

    answer: _Answer
    numbers: frozenset[int]


def _changes(answers: Iterable[_Answer]) -> tuple[list[_Change], list[_Page], int]:
    """Every change row the recording holds, once each (at its first receipt) and by change id; each answer as a
    page; and how many rows weren't readable as a play."""
    first: dict[int, _Change] = {}
    pages: list[_Page] = []
    unreadable = 0
    for answer in sorted(answers, key=lambda answer: answer.received):
        numbers: set[int] = set()
        for row in _rows(answer.body):
            change_id, written = _untyped(row)
            play = _typed(row)
            if change_id is None or play is None:
                unreadable += 1
                continue
            numbers.add(play.number)
            if change_id not in first:
                first[change_id] = _Change(change_id, answer.received, written, play, row)
        pages.append(_Page(answer, frozenset(numbers)))
    return sorted(first.values(), key=lambda change: change.change_id), pages, unreadable


class _RecordedFeed:
    """NFLMeta as the recording received it, for the provider's fetcher.

    A scoreboard ask gets the scoreboard being replayed. A play-feed ask gets, after its cursor and in the feed's
    order, every change row the recording had first received before ``horizon`` (the next scoreboard's arrival).
    Its cursor is the last change id it served, and ``served`` keeps every row it hands out.
    """

    def __init__(self, changes: Sequence[_Change]) -> None:
        self._changes = changes
        self.scoreboard: Any = None
        self.horizon: datetime | None = None  # None: everything recorded
        self.served: list[_Change] = []

    def __call__(self, path: str, query: Mapping[str, str | int]) -> Any:
        if not path.endswith("/play-changes"):
            return self.scoreboard
        after = int(query.get("cursor", -1))
        limit = int(query.get("limit", 100))
        horizon = self.horizon
        rows = [c for c in self._changes if c.change_id > after and (horizon is None or c.received < horizon)]
        page = rows[:limit]
        self.served += page
        last = page[-1].change_id if page else after
        return {"data": [c.row for c in page], "meta": {"has_more": len(rows) > limit, "next_cursor": str(last)}}


@dataclass(frozen=True, slots=True)
class _Look:
    """One replayed scoreboard: when it arrived, the provider's answer, and the rows its play read brought."""

    received: datetime
    snapshot: NflLiveSnapshot
    served: tuple[_Change, ...]
    written: datetime | None  # when the feed wrote the scoreboard row


def _replay(
    scoreboards: Sequence[_Answer], changes: Sequence[_Change], away: str, home: str
) -> tuple[list[_Look], int]:
    """The provider's answer at each recorded scoreboard, and how many scoreboards it couldn't answer from.

    Raises `ValueError` when no scoreboard lists the game between ``away`` and ``home``.
    """
    feed = _RecordedFeed(changes)
    provider = NflMetaLiveProvider(
        NflTeamRegistry.default(source=NFLMETA_SOURCE),
        fetch=feed,
        halftime_hold=timedelta(0),
        play_feed=PlayFeedPace.EVERY_POLL,
    )
    game_id = None
    for answer in scoreboards:
        feed.scoreboard = answer.body
        if (game_id := provider.find_game(away, home)) is not None:
            break
    if game_id is None:
        raise ValueError(f"no recorded scoreboard lists {away} @ {home}")
    start = scoreboards[0].received
    looks: list[_Look] = []
    failed = 0
    for index, answer in enumerate(scoreboards):
        feed.scoreboard = answer.body
        feed.horizon = scoreboards[index + 1].received if index + 1 < len(scoreboards) else None
        feed.served = []
        at = ClockReading(answer.received, MonotonicTime((answer.received - start).total_seconds()))
        try:
            snapshot = provider.poll(game_id, at)
        except ProviderError:
            failed += 1
            continue
        row = _game_row(answer.body, game_id)
        written = _untyped(row)[1] if row is not None else None
        looks.append(_Look(answer.received, snapshot, tuple(feed.served), written))
    return looks, failed


# --- plays -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Play:
    """One play's history in the feed: its change rows, in the order they first arrived."""

    changes: tuple[_Change, ...]

    @property
    def first(self) -> _Change:
        return self.changes[0]

    @property
    def latest(self) -> FootballPlay:
        """The play at its newest revision."""
        return max(self.changes, key=lambda change: change.change_id).play

    @property
    def number(self) -> int:
        return self.first.play.number

    @property
    def quarter(self) -> int:
        return self.latest.quarter

    @property
    def received(self) -> datetime:
        return self.first.received

    @property
    def written(self) -> datetime:
        """When the feed wrote the row we first had, or when we had it if the row doesn't say."""
        return self.first.written or self.first.received

    @property
    def action(self) -> bool:
        """Whether it's a snap, not a stoppage or a marker."""
        return self.latest.play_type not in _NOT_ACTION


def _plays(changes: Iterable[_Change]) -> dict[int, _Play]:
    grouped: dict[int, list[_Change]] = {}
    for change in sorted(changes, key=lambda change: (change.received, change.change_id)):
        grouped.setdefault(change.play.number, []).append(change)
    return {number: _Play(tuple(group)) for number, group in sorted(grouped.items())}


def _span(later: datetime | None, earlier: datetime | None) -> float | None:
    return seconds((later - earlier).total_seconds()) if later is not None and earlier is not None else None


def _between(later: _Change, earlier: _Change) -> float:
    """How long after ``earlier`` the feed wrote ``later``; by when they arrived, if either row doesn't say."""
    if later.written is not None and earlier.written is not None:
        return seconds((later.written - earlier.written).total_seconds())
    return seconds((later.received - earlier.received).total_seconds())


# --- breaks ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BreakRow:
    """One break: its kind, its quarter, and its spans in seconds (None where a time isn't known).

    ``observed`` marks an onset taken from when NFLMeta observed the scoreboard, its row not saying when it was
    written; it's counted per kind, not kept per row.
    """

    kind: BreakKind
    quarter: int
    onset_to_receipt: float | None
    onset_to_next_action: float | None
    receipt_to_next_action_receipt: float | None
    observed: bool = False

    def record(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "quarter": self.quarter,
            "onset_to_receipt": self.onset_to_receipt,
            "onset_to_next_action": self.onset_to_next_action,
            "receipt_to_next_action_receipt": self.receipt_to_next_action_receipt,
        }


def _break_row(
    kind: BreakKind,
    quarter: int,
    onset: datetime | None,
    receipt: datetime,
    after: _Play | None,
    *,
    observed: bool = False,
) -> _BreakRow:
    return _BreakRow(
        kind=kind,
        quarter=quarter,
        onset_to_receipt=_span(receipt, onset),
        onset_to_next_action=_span(after.written if after else None, onset),
        receipt_to_next_action_receipt=_span(after.received if after else None, receipt),
        observed=observed,
    )


def _event_break(event: FootballEvent, plays: Mapping[int, _Play]) -> BreakKind | None:
    """The break an event announces; a play first seen with a review's ruling on it is a ruling too."""
    if event.kind is FootballEventKind.PLAY:
        play = plays.get(event.play) if event.play is not None else None
        return BreakKind.NFL_REVIEW if play is not None and play.first.play.text.review is not None else None
    return _EVENT_BREAKS.get(event.kind)


def _breaks(looks: Sequence[_Look], plays: Mapping[int, _Play]) -> list[_BreakRow]:
    """Every break the replay announced, once each."""
    actions = sorted((play for play in plays.values() if play.action), key=lambda play: play.number)
    rows: list[_BreakRow] = []
    seen: set[tuple[BreakKind, object]] = set()
    period = 1
    for look in looks:
        state = look.snapshot.state
        if state is not None:
            period = state.period
            kind = _INTERVAL_BREAKS.get(state.interval) if state.interval is not None else None
            if kind is not None and (kind, period) not in seen:
                seen.add((kind, period))
                after = next((play for play in actions if play.quarter > period), None)
                onset = look.written or look.snapshot.observed_at
                observed = look.written is None and onset is not None
                rows.append(_break_row(kind, period, onset, look.received, after, observed=observed))
        for event in look.snapshot.events:
            kind = _event_break(event, plays)
            if kind is None:
                continue
            if event.play is None:  # an injury report: no play behind it, no onset the feed gives
                key: tuple[BreakKind, object] = (kind, event.key)
                after = next((play for play in actions if play.written > look.received), None)
                row = _break_row(kind, period, None, look.received, after)
            else:
                key = (kind, event.play)
                brought = [change.received for change in look.served if change.play.number == event.play]
                receipt = min(brought, default=look.received)
                number = event.play
                after = next((play for play in actions if play.number > number), None)
                quarter = plays[number].quarter if number in plays else period
                row = _break_row(kind, quarter, event.written_at, receipt, after)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return sorted(rows, key=_break_order)


def _break_order(row: _BreakRow) -> tuple[str, int, float, float, float]:
    """Rows sorted by what they say, not by when they happened, so their order doesn't retell the game."""
    spans = [row.onset_to_receipt, row.onset_to_next_action, row.receipt_to_next_action_receipt]
    first, second, third = (span if span is not None else float("-inf") for span in spans)
    return row.kind.value, row.quarter, first, second, third


def _breaks_by_kind(rows: Sequence[_BreakRow]) -> list[dict[str, Any]]:
    kinds = sorted({row.kind for row in rows}, key=lambda kind: kind.value)
    summaries = []
    for kind in kinds:
        mine = [row for row in rows if row.kind is kind]
        summaries.append(
            {
                "kind": kind.value,
                "count": len(mine),
                "onsets_observed": sum(row.observed for row in mine),
                **{
                    name: Spread.of(v for row in mine if (v := getattr(row, name)) is not None).record()
                    for name in ("onset_to_receipt", "onset_to_next_action", "receipt_to_next_action_receipt")
                },
            }
        )
    return summaries


# --- feed latency, order and corrections ---------------------------------------------------------------------


def _score_leads(looks: Sequence[_Look], changes: Sequence[_Change], game: TeamGame) -> list[tuple[int, float | None]]:
    """For each move of a side's score on the scoreboard (after the first look), its quarter and how long after
    it the plays accounted for the new total (negative: the plays had it first; None: they never did)."""
    accounted = _accounted(changes, game)
    moves: list[tuple[int, float | None]] = []
    shown: dict[HomeAway, int] | None = None
    for look in looks:
        state = look.snapshot.state
        if state is None:
            continue
        scores = {HomeAway.AWAY: state.away_score, HomeAway.HOME: state.home_score}
        if shown is not None:
            for side, score in scores.items():
                if score > shown[side]:
                    reached = accounted[side].get(score)
                    moves.append((state.period, _span(reached, look.received)))
        shown = {side: max(score, shown[side]) if shown else score for side, score in scores.items()}
    return moves


def _accounted(changes: Sequence[_Change], game: TeamGame) -> dict[HomeAway, dict[int, datetime]]:
    """When the plays first accounted for each total of each side's score, as their rows arrived."""
    ledger = PlayLedger(game.away, game.home)
    reached: dict[HomeAway, dict[int, datetime]] = {HomeAway.AWAY: {}, HomeAway.HOME: {}}
    scoring: set[int] = set()  # plays that scored, at some revision: only they can move a total
    for change in sorted(changes, key=lambda change: (change.received, change.change_id)):
        ledger.absorb([change.play])
        if _scored(change.play):
            scoring.add(change.play.number)
        if change.play.number not in scoring:
            continue
        for side, totals in reached.items():
            for total in range(1, ledger.points(side) + 1):
                totals.setdefault(total, change.received)
    return reached


def _scored(play: FootballPlay) -> list[ScoringKind]:
    """What a play scored as it stands (a play a penalty wiped out scored nothing)."""
    if play.text.nullified:
        return []
    kinds: list[ScoringKind] = []
    if play.touchdown:
        kinds.append(ScoringKind.TOUCHDOWN)
    elif play.play_type is FootballPlayType.SAFETY:
        kinds.append(ScoringKind.SAFETY)
    elif play.field_goal is FieldGoalResult.MADE or play.play_type is FootballPlayType.FIELD_GOAL_GOOD:
        kinds.append(ScoringKind.FIELD_GOAL)
    if play.extra_point is not None and play.extra_point.scored:
        kinds.append(ScoringKind.EXTRA_POINT)
    if play.two_point is not None and play.two_point.scored:
        kinds.append(ScoringKind.TWO_POINT)
    return kinds


def _latency(
    looks: Sequence[_Look], changes: Sequence[_Change], plays: Mapping[int, _Play]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    published = [_span(play.received, play.first.written) for play in plays.values()]
    game = looks[0].snapshot.game
    moves = _score_leads(looks, changes, game)
    leads = [lead for _, lead in moves if lead is not None]
    summary = {
        "plays": len(plays),
        "write_to_receipt": Spread.of(span for span in published if span is not None).record(),
        "score_moves": len(moves),
        "scoreboard_first": sum(lead > 0 for lead in leads),
        "plays_first": sum(lead < 0 for lead in leads),
        "together": sum(lead == 0 for lead in leads),
        "never_explained": len(moves) - len(leads),
        "scoreboard_lead": Spread.of(leads).record(),
    }
    rows = sorted(({"quarter": quarter, "scoreboard_lead": lead} for quarter, lead in moves), key=_row_order)
    return summary, rows


def _row_order(row: Mapping[str, Any]) -> tuple[int, float]:
    lead = row["scoreboard_lead"]
    return row["quarter"], lead if lead is not None else float("-inf")


@dataclass(frozen=True, slots=True)
class _HistoryRead:
    """A read of the whole play feed, from its start to its end: when it began, and the plays it held."""

    began: datetime
    numbers: frozenset[int]


def _history_reads(pages: Sequence[_Page]) -> list[_HistoryRead]:
    """Each read the recording made from the feed's start and followed, page by page, to its end."""
    by_cursor: dict[str, list[_Page]] = {}
    for page in pages:
        if page.answer.cursor is not None:
            by_cursor.setdefault(page.answer.cursor, []).append(page)
    reads: list[_HistoryRead] = []
    for start in (page for page in pages if page.answer.from_start):
        numbers: set[int] = set()
        current: _Page | None = start
        while current is not None:
            numbers |= current.numbers
            cursor, more = _page(current.answer.body)
            if not more:
                reads.append(_HistoryRead(start.answer.received, frozenset(numbers)))
                break
            after = current.answer.received
            current = next((p for p in by_cursor.get(cursor or "", []) if p.answer.received >= after), None)
    return reads


def _order_and_corrections(plays: Mapping[int, _Play], pages: Sequence[_Page]) -> dict[str, Any]:
    lags: list[float] = []
    for play in plays.values():
        ahead = [
            other.received for other in plays.values() if other.number > play.number and other.received < play.received
        ]
        if ahead:
            lags.append(seconds((play.received - min(ahead)).total_seconds()))
    delays: list[float] = []
    revised = 0
    for play in plays.values():
        later = [change for change in play.changes[1:] if change.play.revision > play.first.play.revision]
        revised += bool(later)
        delays += [_between(change, play.first) for change in later]
    reads = _history_reads(pages)
    vanished = returned = 0
    for play in plays.values():
        missing = [read.began for read in reads if read.began > play.received and play.number not in read.numbers]
        if missing:
            vanished += 1
            gone = min(missing)
            returned += any(page.answer.received > gone and play.number in page.numbers for page in pages)
    return {
        "late_plays": len(lags),
        "late_lag": Spread.of(lags).record(),
        "revised_plays": revised,
        "revisions": len(delays),
        "revision_delay": Spread.of(delays).record(),
        "history_reads": len(reads),
        "vanished_plays": vanished if reads else None,
        "returned_plays": returned if reads else None,
    }


# --- cadence, events, billing, polling, coverage -------------------------------------------------------------


def _wall(plays: Iterable[_Play]) -> float | None:
    writes = sorted(play.written for play in plays)
    return _span(writes[-1], writes[0]) if len(writes) > 1 else None


def _cadence(looks: Sequence[_Look], plays: Mapping[int, _Play]) -> dict[str, Any]:
    quarters = []
    for quarter in sorted({play.quarter for play in plays.values()}):
        mine = [play for play in plays.values() if play.quarter == quarter]
        snaps = sorted(play.written for play in mine if play.action)
        gaps = [(later - earlier).total_seconds() for earlier, later in zip(snaps, snaps[1:])]
        clocks = [
            look.snapshot.state.clock_seconds
            for look in looks
            if look.snapshot.state is not None and look.snapshot.state.period == quarter
        ]
        elapsed = max(clocks) - min(clocks) if clocks else 0
        wall = _wall(mine)
        quarters.append(
            {
                "quarter": quarter,
                "wall": wall,
                "game_clock": elapsed or None,
                "wall_per_clock_second": seconds(wall / elapsed) if wall is not None and elapsed else None,
                "snap_gap": Spread.of(gaps).record(),
            }
        )
    halves = [
        {"half": half, "wall": _wall(play for play in plays.values() if play.quarter in quarters_of)}
        for half, quarters_of in _HALVES
    ]
    return {"quarters": quarters, "halves": halves, "game_wall": _wall(plays.values())}


def _events(looks: Sequence[_Look], plays: Mapping[int, _Play]) -> dict[str, Any]:
    events: Counter[tuple[int, FootballEventKind]] = Counter()
    notes: Counter[FeedNoteKind] = Counter()
    period = 1
    for look in looks:
        if look.snapshot.state is not None:
            period = look.snapshot.state.period
        for event in look.snapshot.events:
            play = plays.get(event.play) if event.play is not None else None
            events[(play.quarter if play is not None else period, event.kind)] += 1
        notes.update(note.kind for note in look.snapshot.notes)
    scoring = Counter(kind for play in plays.values() for kind in _scored(play.latest))
    return {
        "events": [
            {"quarter": quarter, "kind": kind.value, "count": count}
            for (quarter, kind), count in sorted(events.items(), key=lambda item: (item[0][0], item[0][1].value))
        ],
        "notes": [{"kind": kind.value, "count": notes[kind]} for kind in FeedNoteKind if notes[kind]],
        "scoring": [{"kind": kind.value, "count": scoring[kind]} for kind in ScoringKind if scoring[kind]],
    }


def _gaps(answers: Sequence[_Answer]) -> list[float]:
    arrivals = sorted(answer.received for answer in answers)
    return [(later - earlier).total_seconds() for earlier, later in zip(arrivals, arrivals[1:])]


def _billing_and_polling(answers: Sequence[_Answer], looks: Sequence[_Look]) -> tuple[list[Any], dict[str, Any]]:
    billing = []
    polling = []
    for endpoint in Endpoint:
        mine = [answer for answer in answers if answer.endpoint is endpoint]
        billed = [answer.billed for answer in mine if answer.billed is not None]
        rows = [len(_rows(answer.body)) for answer in mine]
        billing.append(
            {
                "endpoint": endpoint.value,
                "requests": len(mine),
                "billed_rows": sum(billed) if billed else None,
                "billed_per_read": Spread.of(billed).record(),
                "rows_per_read": Spread.of(rows).record(),
                "empty_reads": rows.count(0),
            }
        )
        polling.append({"endpoint": endpoint.value, "answer_gap": Spread.of(_gaps(mine)).record()})
    paces = Counter(look.snapshot.poll_every.value for look in looks)
    recommended = [{"seconds": pace, "answers": paces[pace]} for pace in sorted(paces)]
    return billing, {"endpoints": polling, "recommended": recommended}


def _coverage(looks: Sequence[_Look]) -> dict[str, Any]:
    states = [look.snapshot.state for look in looks if look.snapshot.state is not None]
    fields = []
    for field in FootballField:
        levels = Counter(state.coverage[field] for state in states)
        shares = {
            level.value: round(levels[level] / len(states), _SHARE_PLACES) if states else None
            for level in FieldCoverage
        }
        fields.append({"field": field.value, **shares})
    return {"snapshots": len(states), "fields": fields}


# --- the digest ----------------------------------------------------------------------------------------------


def _named(folder: Path) -> tuple[str, str, str]:
    """The date and the two clubs a recording folder is named for (``2026-11-15-PIT-CIN``)."""
    named = _FOLDER.fullmatch(folder.name)
    if named is None:
        raise ValueError("a recording folder is named <date>-<away>-<home>")
    try:
        date.fromisoformat(named["date"])
        away, home = canonical_abbreviation(named["away"]), canonical_abbreviation(named["home"])
    except (KeyError, ValueError):
        raise ValueError("a recording folder is named for a date and two NFL clubs") from None
    return named["date"], away, home


def mine(folder: Path) -> dict[str, Any]:
    """The digest of the recording in ``folder``; `ValueError` when it can't be mined (no game in it)."""
    day, away, home = _named(folder)
    answers, unreadable_lines = _answers(folder)
    scoreboards = sorted((a for a in answers if a.endpoint is Endpoint.LIVE_SCORES), key=lambda a: a.received)
    pages = sorted((a for a in answers if a.endpoint is Endpoint.PLAY_CHANGES), key=lambda a: a.received)
    changes, feed_pages, unreadable_rows = _changes(pages)
    looks, failed = _replay(scoreboards, changes, away, home)
    if not looks:
        raise ValueError(f"the provider read none of the recorded scoreboards for {away} @ {home}")
    plays = _plays(changes)
    break_rows = _breaks(looks, plays)
    latency, score_rows = _latency(looks, changes, plays)
    billing, polling = _billing_and_polling(answers, looks)
    game = looks[0].snapshot.game
    return {
        "schema": SCHEMA,
        "kind": DigestKind.NFLMETA_RECORDING.value,
        "folder": folder.name,
        "date": day,
        "away": game.away.abbreviation,
        "home": game.home.abbreviation,
        "replay": {
            "scoreboards": len(scoreboards),
            "looks_failed": failed,
            "unreadable_lines": unreadable_lines,
            "unreadable_rows": unreadable_rows,
        },
        "breaks": _breaks_by_kind(break_rows),
        "break_rows": [row.record() for row in break_rows],
        "latency": latency,
        "score_rows": score_rows,
        "order": _order_and_corrections(plays, feed_pages),
        "cadence": _cadence(looks, plays),
        "events": _events(looks, plays),
        "billing": billing,
        "polling": polling,
        "coverage": _coverage(looks),
    }


def allowed(digest: Mapping[str, Any]) -> frozenset[str]:
    """The strings a recording's digest may hold: our enums' values and the names the folder vouches for."""
    return allowed_values(*_ENUMS, names=(digest["folder"], digest["date"], digest["away"], digest["home"]))


def _is_nflmeta_recording(folder: Path) -> bool:
    """A folder with recorded scoreboards, stamped for NFLMeta or not stamped at all."""
    if not (folder / f"{Endpoint.LIVE_SCORES.value}.jsonl").is_file():
        return False
    try:
        stamp = read_stamp(folder)
    except ValueError:
        stamp = None  # an unreadable stamp: the files say what it is
    return stamp is None or stamp.source is DataSource.NFLMETA


def mine_recording(folder: Path, out_dir: Path) -> Path | None:
    """Mine the recording in ``folder`` into ``out_dir/<folder name>.json`` (and ``.md``): the digest's path.

    None when ``folder`` isn't an NFLMeta recording. A folder already mined isn't mined again: its digest stays as
    it is, and its path is returned. Raises `ValueError` for an NFLMeta recording that can't be mined (a folder
    not named for its game, or a game no recorded scoreboard lists).
    """
    if not folder.is_dir() or not _is_nflmeta_recording(folder):
        return None
    target = out_dir / f"{folder.name}.json"
    if target.exists():
        return target
    digest = mine(folder)
    return write_digest(digest, target, summary(digest), measures=MEASURES, values=allowed(digest))


# --- the summary -------------------------------------------------------------------------------------------------


def _spread(record: Mapping[str, Any]) -> str:
    return f"{figure(record['p25'])} / {figure(record['median'])} / {figure(record['p75'])}"


def summary(digest: Mapping[str, Any]) -> str:
    """A short Markdown summary of a recording's digest, for a person; it holds nothing the digest doesn't."""
    latency, order, cadence = digest["latency"], digest["order"], digest["cadence"]
    lines = [
        f"# NFLMeta recording {digest['folder']}",
        "",
        f"{digest['away']} @ {digest['home']}, {digest['date']}. Our own telemetry, mined from the recording before"
        " it expires: counts, our own kinds, and spans in seconds (p25 / median / p75).",
        "",
        "## Breaks",
        "",
        "Onset is when the feed wrote the break's row; the next action is the next snap.",
        "",
        "| kind | count | onset to receipt | onset to next action | receipt to next action's receipt |",
        "|---|---|---|---|---|",
    ]
    for kind in digest["breaks"]:
        lines.append(
            f"| {kind['kind']} | {kind['count']} | {_spread(kind['onset_to_receipt'])}"
            f" | {_spread(kind['onset_to_next_action'])} | {_spread(kind['receipt_to_next_action_receipt'])} |"
        )
    lines += [
        "",
        "## Feed",
        "",
        f"- Plays: {latency['plays']}; the feed's write to our receipt {_spread(latency['write_to_receipt'])}.",
        f"- Score moves: {latency['score_moves']}; the scoreboard first {latency['scoreboard_first']}, the plays first"
        f" {latency['plays_first']}, together {latency['together']}, never explained {latency['never_explained']};"
        f" the scoreboard's lead {_spread(latency['scoreboard_lead'])}.",
        f"- Late plays: {order['late_plays']} ({_spread(order['late_lag'])}). Revised plays: {order['revised_plays']}"
        f" ({order['revisions']} revisions, {_spread(order['revision_delay'])} after the first)."
        f" Whole-feed reads: {order['history_reads']}; plays gone from one: {figure(order['vanished_plays'])},"
        f" back later: {figure(order['returned_plays'])}.",
        "",
        "## Cadence",
        "",
        "| quarter | wall | game clock | wall per clock second | snap gap |",
        "|---|---|---|---|---|",
    ]
    for quarter in cadence["quarters"]:
        lines.append(
            f"| {quarter['quarter']} | {figure(quarter['wall'])} | {figure(quarter['game_clock'])}"
            f" | {figure(quarter['wall_per_clock_second'])} | {_spread(quarter['snap_gap'])} |"
        )
    halves = ", ".join(f"{figure(half['wall'])}" for half in cadence["halves"])
    lines += ["", f"Halves: {halves}. Game: {figure(cadence['game_wall'])}.", "", "## Billing and polling", ""]
    lines += ["| endpoint | requests | billed rows | empty reads | gap between answers |", "|---|---|---|---|---|"]
    for billed, polled in zip(digest["billing"], digest["polling"]["endpoints"]):
        lines.append(
            f"| {billed['endpoint']} | {billed['requests']} | {figure(billed['billed_rows'])} | {billed['empty_reads']}"
            f" | {_spread(polled['answer_gap'])} |"
        )
    levels = [level.value for level in FieldCoverage]
    lines += ["", f"## Coverage ({digest['coverage']['snapshots']} snapshots)", ""]
    lines += [f"| field | {' | '.join(levels)} |", "|---|" + "---|" * len(levels)]
    for field in digest["coverage"]["fields"]:
        lines.append(f"| {field['field']} | " + " | ".join(figure(field[level]) for level in levels) + " |")
    return "\n".join(lines) + "\n"
