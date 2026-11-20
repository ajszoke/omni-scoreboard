"""A recorded BALLDONTLIE game, served back to the provider as the live feed answered it, in arrival order.

The fixtures (``tests/fixtures/providers/balldontlie/``, described in their README) hold each read of one game from the
night of 2026-11-18: every slate read whose row changed, and every stat read with the rows that changed since the read
before it. `BdlReplay` merges the two files on the recorder's monotonic clock (a slate read first on a tie, as the
recorder asked the slate first) and steps through them. At each step its clock reads the record's monotonic time, and
its fetch answers the request the provider makes with the record's status and headers:

- **a slate read** with the row as the feed sent it;
- **a stat read** with the full page the feed sent: each read's rows laid over the rows before, in the feed's order
  (by the stat row's ``id``). Every row of one answer carries the same view of the game (every recorded read's rows
  do), so each row of the rebuilt page gets the read's view. The page is served ``page_size`` rows at a time with
  the feed's cursor (the last row's ``id``), so a smaller page size replays the paging too.

It's a tool for tests: it serves raw JSON, which stays out of ``omni/`` beyond the provider.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from omni.core.clock import FakeClock, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId
from omni.providers.balldontlie import BALLDONTLIE_SOURCE, PAGE_SIZE, BdlAnswer, BdlQuery

FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "balldontlie"
GAMES = ("uta-lal", "det-nop", "hou-ind")


class Kind(Enum):
    SLATE = "games"
    LINES = "stats"


@dataclass(frozen=True, slots=True)
class Step:
    """One recorded read: what was asked, and when it arrived on the recorder's monotonic clock."""

    kind: Kind
    at: float
    received: datetime
    path: str
    query: BdlQuery
    status: int
    headers: dict[str, str]
    body: Any  # a slate read's body as the feed sent it; a stat read's rows, the full page rebuilt

    @property
    def day(self) -> date:
        return date.fromisoformat(dict(self.query)["dates[]"])

    @property
    def game(self) -> LeagueScopedId:
        return LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, dict(self.query)["game_ids[]"])


def records(name: str, kind: Kind) -> list[dict[str, Any]]:
    """A fixture file's records, in the order they were written."""
    with gzip.open(FIXTURES / f"nba-2026-11-18-{name}.{kind.value}.jsonl.gz", "rt", encoding="utf-8") as lines:
        return [json.loads(line) for line in lines]


class BdlReplay:
    """Serves one recorded game's reads, step by step, to a provider asking with `fetch` and `clock`."""

    def __init__(self, name: str, *, page_size: int = PAGE_SIZE) -> None:
        self._page_size = page_size
        self.steps = _steps(name)
        first = self.steps[0]
        self.clock = FakeClock(first.received, elapsed=MonotonicTime(first.at))
        self._current: Step | None = None

    def __iter__(self) -> Iterator[Step]:
        """Each step in arrival order, the clock moved to it before it's handed on."""
        for step in self.steps:
            self.clock.advance(MonotonicTime(step.at).since(self.clock.monotonic()))
            self._current = step
            yield step
        self._current = None

    def fetch(self, path: str, query: BdlQuery) -> BdlAnswer:
        """The current step's answer to the request, a page of it for a stat read."""
        step = self._current
        assert step is not None and path == step.path, f"{path} asked out of turn"
        asked = tuple(pair for pair in query if pair[0] != "cursor")
        assert sorted(asked) == sorted(step.query), f"{query} isn't what the recorder asked: {step.query}"
        if step.kind is Kind.SLATE or step.status != 200:
            return BdlAnswer(step.status, json.dumps(step.body), step.headers)
        cursor = dict(query).get("cursor")
        rows = [row for row in step.body if cursor is None or row["id"] > int(cursor)]
        page = rows[: self._page_size]
        meta: dict[str, Any] = {"per_page": self._page_size}
        if len(rows) > len(page):
            meta["next_cursor"] = page[-1]["id"]
        return BdlAnswer(200, json.dumps({"data": page, "meta": meta}), step.headers)


def _steps(name: str) -> list[Step]:
    slate = [_step(Kind.SLATE, record, record["body"]) for record in records(name, Kind.SLATE)]
    lines: list[Step] = []
    latest: dict[int, dict[str, Any]] = {}  # each player's row as last sent
    for record in records(name, Kind.LINES):
        changed = record["changed"]
        for row in changed:
            latest[row["player"]["id"]] = row
        view = changed[0]["game"]
        page = sorted(({**row, "game": view} for row in latest.values()), key=lambda row: row["id"])
        lines.append(_step(Kind.LINES, record, page))
    return sorted(slate + lines, key=lambda step: (step.at, step.kind is Kind.LINES))


def _step(kind: Kind, record: dict[str, Any], body: Any) -> Step:
    url = urlsplit(record["query"])
    return Step(
        kind=kind,
        at=record["monotonic"],
        received=datetime.fromisoformat(record["received_at"].replace("Z", "+00:00")),
        path=url.path,
        query=tuple(parse_qsl(url.query)),
        status=record["status"],
        headers=dict(record["headers"]),
        body=body,
    )


class RecordedNight:
    """The recorded games of one night, served as the live feed would have answered at each moment of the replay's
    clock, whatever is asked and whenever.

    The replay's clock starts at the first record, on the recorder's own monotonic clock, so a moment of the replay is a
    moment of the night. A slate read of the night's date answers each game's newest row recorded by then; a stat read
    answers each game asked for with its newest full page recorded by then, every row in one page; a season read
    answers no games. Every answer carries the rate-limit window of the newest record it came from. Each request is
    kept, with when it was made.
    """

    def __init__(self, names: tuple[str, ...] = GAMES) -> None:
        steps = {name: _steps(name) for name in names}
        self._slates = {name: [step for step in each if step.kind is Kind.SLATE] for name, each in steps.items()}
        self._lines = {name: [step for step in each if step.kind is Kind.LINES] for name, each in steps.items()}
        first = min((each[0] for each in steps.values()), key=lambda step: step.at)
        last = max((each[-1] for each in steps.values()), key=lambda step: step.at)
        self.start = first.received
        self.end = first.received + timedelta(seconds=last.at - first.at)
        self.clock = FakeClock(first.received, elapsed=MonotonicTime(first.at))
        self.asked: list[tuple[float, str, BdlQuery]] = []

    def fetch(self, path: str, query: BdlQuery) -> BdlAnswer:
        """The answer the feed would have given at the clock's moment."""
        now = self.clock.monotonic().seconds
        self.asked.append((now, path, query))
        asked = dict(query)
        if path == "/v1/stats":
            games = {value for name, value in query if name == "game_ids[]"}
            newest = [_newest(steps, now) for steps in self._lines.values()]
            used = [step for step in newest if step is not None and step.game.raw in games]
            return _answer([row for step in used for row in step.body], used)
        if "seasons[]" in asked:
            return _answer([], [])
        newest = [_newest(steps, now) for steps in self._slates.values()]
        used = [step for step in newest if step is not None and step.body["data"][0]["date"] == asked["dates[]"]]
        return _answer([step.body["data"][0] for step in used], used)


def _newest(steps: list[Step], now: float) -> Step | None:
    """The newest of a game's records (in arrival order) that had arrived by ``now``."""
    index = bisect_right([step.at for step in steps], now)
    return steps[index - 1] if index else None


def _answer(rows: list[Any], used: list[Step]) -> BdlAnswer:
    """A one-page answer of ``rows``, with the rate-limit window of the newest record they came from."""
    newest = max(used, key=lambda step: step.at, default=None)
    headers = newest.headers if newest is not None else {}
    window = {name: value for name, value in headers.items() if name.startswith("x-ratelimit")}
    return BdlAnswer(200, json.dumps({"data": rows, "meta": {"per_page": PAGE_SIZE}}), window)
