"""Probe BALLDONTLIE's NBA API, or record one night's slate from it; prints no secret.

    .venv/bin/python tools/balldontlie_record.py --probe
    .venv/bin/python tools/balldontlie_record.py [--date YYYY-MM-DD] [--per-minute 55] [--every 5]

Reads ``BALLDONTLIE_API_KEY`` from the environment or the repo's git-ignored ``.env``.

``--probe`` asks every documented NBA endpoint once (about two dozen requests), to see which ones this
key's tier answers. It prints a line per endpoint: the HTTP status, the rows, and the fields of a row,
and writes ``capabilities.json`` beside the raw answers.

Otherwise it records one date's slate (default: today, US Eastern, the API's own dates). Every request
goes through one admission, ``--per-minute`` in any 60 seconds, retries and pages included, so the
recorder never asks for more than the tier allows (Free: 5 a minute; ALL-STAR: 60; GOAT: 600). It
reads, before the first tip and then while games run:

- the slate (``/v1/games``) every ``--every`` seconds: score, period and clock, timeouts and bonus;
- each running game's player lines (``/v1/stats``, ALL-STAR) every ``--stats-every`` seconds, all running
  games in one paged request; a game stays in it for ``--final-minutes`` after its final, for the
  finished lines. A line is written when it changed, and ``stat-deltas.log`` gets a readable line per
  change (``tail -f`` it during the games);
- play-by-play (``/v1/plays``) and live box scores (``/v1/box_scores/live``), if the key's tier answers
  them (GOAT; tried once at the start);
- teams, active players and injuries at the start, injuries again every 30 minutes.

It stops once every game has been over for ``--final-minutes``, or at the request cap or deadline.

Everything lands in ``build/recordings/balldontlie/nba/``, a folder per probe (``probe-<instant>/``) or
night (``<date>/``): the raw answers as JSON lines, stamped with when they arrived, and ``summary.log``,
a readable line per change in any game. Each folder is made with a ``RETENTION.json`` stamp;
BALLDONTLIE's terms allow keeping what it answers, so the stamp says so (``tools/recordings_due.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.core.env import read_env_file
from omni.providers.rights import DataSource, stamp_recording

_BASE = "https://api.balldontlie.io"
_KEY_VAR = "BALLDONTLIE_API_KEY"
_EASTERN = ZoneInfo("America/New_York")  # the API dates games by their US Eastern date
_OUT = _ROOT / "build" / "recordings" / "balldontlie" / "nba"
_DONE = {"final", "postponed", "canceled", "abandoned"}  # status_state values that end a game's night
_KEPT_HEADERS = ("ratelimit", "rate-limit", "retry-after", "cache", "age", "date", "etag", "last-modified", "cf-ray")
_MAX_PAGES = 30  # a guard against a cursor that never ends
# A player line's counters, in the order a change is described, with their short labels.
_COUNTERS: tuple[tuple[str, str], ...] = (
    ("pts", "PTS"),
    ("ast", "AST"),
    ("reb", "REB"),
    ("stl", "STL"),
    ("blk", "BLK"),
    ("turnover", "TOV"),
    ("pf", "PF"),
)


class Stop(Exception):
    """An answer that means going on can't help."""


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: Any
    headers: dict[str, str]
    received: datetime
    took: float
    query: str

    def record(self, **extra: Any) -> dict[str, Any]:
        return {
            "received_at": _stamp(self.received),
            "monotonic": round(time.monotonic(), 3),
            "took_s": round(self.took, 3),
            "status": self.status,
            "headers": self.headers,
            "query": self.query,
            **extra,
        }


class Admission:
    """At most ``per_minute`` requests in any 60 seconds, and none while the server says the window is spent."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._sent: deque[float] = deque()
        self._closed_until = 0.0

    def admit(self) -> None:
        while True:
            now = time.monotonic()
            while self._sent and now - self._sent[0] >= 60.0:
                self._sent.popleft()
            wait = self._closed_until - now
            if len(self._sent) >= self._per_minute:
                wait = max(wait, self._sent[0] + 60.0 - now)
            if wait <= 0:
                break
            time.sleep(wait)
        self._sent.append(time.monotonic())

    def heed(self, headers: dict[str, str]) -> None:
        """Close admission until the window's reset when the server says none remain."""
        if headers.get("x-ratelimit-remaining") != "0":
            return
        try:
            wait = float(headers.get("x-ratelimit-reset", "")) - time.time()
        except ValueError:
            return
        if 0 < wait <= 65:
            self._closed_until = time.monotonic() + wait


class Client:
    def __init__(self, key: str, *, max_requests: int, per_minute: int) -> None:
        self._key = key
        self._max_requests = max_requests
        self._admission = Admission(per_minute)
        self.requests = 0

    def scrub(self, text: str) -> str:
        return text.replace(self._key, "<key>") if self._key else text

    def get(self, path: str, params: dict[str, Any] | None = None) -> Answer:
        """One answer; a 429 is waited out and retried, each try admitted and counted."""
        query = f"{path}?{urlencode(params, doseq=True, safe='[]')}" if params else path
        request = Request(_BASE + query, headers={"Authorization": self._key, "User-Agent": "omni-scoreboard"})
        for _ in range(3):
            if self.requests >= self._max_requests:
                raise Stop(f"request cap of {self._max_requests} reached")
            self._admission.admit()
            self.requests += 1
            started = time.monotonic()
            try:
                with urlopen(request, timeout=15) as response:
                    headers = _kept(response.headers.items())
                    self._admission.heed(headers)
                    body = json.load(response)
                    return Answer(response.status, body, headers, _now(), time.monotonic() - started, query)
            except HTTPError as exc:
                headers = _kept(exc.headers.items())
                self._admission.heed(headers)
                text = self.scrub(exc.read()[:400].decode("utf-8", "replace"))
                if exc.code == 429:
                    wait = float(headers.get("retry-after") or 60)
                    if wait > 600:
                        raise Stop(f"429 with Retry-After {wait:.0f}s: {text}") from exc
                    time.sleep(wait)
                    continue
                try:
                    parsed: Any = json.loads(text)
                except ValueError:
                    parsed = text
                return Answer(exc.code, parsed, headers, _now(), time.monotonic() - started, query)
        raise Stop("still rate-limited after retries")

    def pages(self, path: str, params: dict[str, Any]) -> list[Answer]:
        """Every page of a cursor-paged list, until the cursor ends or an answer isn't 200."""
        answers: list[Answer] = []
        cursor: Any = None
        for _ in range(_MAX_PAGES):
            answer = self.get(path, {**params, "cursor": cursor} if cursor is not None else params)
            answers.append(answer)
            meta = answer.body.get("meta") if isinstance(answer.body, dict) else None
            cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            if answer.status != 200 or cursor is None:
                break
        return answers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _kept(items: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in items if any(part in k.lower() for part in _KEPT_HEADERS)}


def _key() -> str:
    key = os.environ.get(_KEY_VAR) or read_env_file(_ROOT / ".env").get(_KEY_VAR)
    if not key:
        raise SystemExit(f"{_KEY_VAR} is not set in the environment or .env")
    return key


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M:%S")


def _parse(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rows(body: Any) -> list[dict[str, Any]]:
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        return [data]
    return [row for row in data or [] if isinstance(row, dict)]


def _abbr(game: dict[str, Any], side: str) -> str:
    return str((game.get(f"{side}_team") or {}).get("abbreviation") or "?")


def _state(game: dict[str, Any]) -> str:
    state = game.get("status_state")
    if state:
        return str(state)
    return "final" if str(game.get("status")) == "Final" else "?"


def _describe(game: dict[str, Any]) -> str:
    """One game as a line: matchup, score, period and clock, state, timeouts and bonus."""
    bonus = (
        f"{'V' if game.get('visitor_in_bonus') else '-'}{'H' if game.get('home_in_bonus') else '-'}"
        if "home_in_bonus" in game
        else "??"
    )
    return (
        f"{_abbr(game, 'visitor'):>3} {game.get('visitor_team_score')!s:>3} @ {_abbr(game, 'home'):<3}"
        f" {game.get('home_team_score')!s:>3}  P{game.get('period')} {game.get('time')!r:<8}"
        f" {_state(game)}/{game.get('status')!r}  TO {game.get('visitor_timeouts_remaining')}"
        f"/{game.get('home_timeouts_remaining')}  bonus {bonus}"
    )


def _clock_seconds(raw: Any) -> float | None:
    """A game clock like "Q2 3:44" or "Q1 :38.2" in seconds; None for a break, a missing or an odd value."""
    words = str(raw or "").split()
    text = words[-1] if words else ""
    try:
        if ":" in text:
            minutes, seconds = text.split(":", 1)
            return int(minutes or 0) * 60 + float(seconds)
        return float(text) if text else None
    except ValueError:
        return None


def _oddities(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What, between two reads of one game, looks like a correction rather than play."""
    notes = []
    for side in ("visitor", "home"):
        old, new = before.get(f"{side}_team_score"), after.get(f"{side}_team_score")
        if isinstance(old, int) and isinstance(new, int) and new < old:
            notes.append(f"{side} score down {old}->{new}")
    old_period, new_period = before.get("period"), after.get("period")
    if isinstance(old_period, int) and isinstance(new_period, int):
        if new_period < old_period:
            notes.append(f"period down {old_period}->{new_period}")
        elif new_period == old_period:
            old_clock, new_clock = _clock_seconds(before.get("time")), _clock_seconds(after.get("time"))
            if old_clock is not None and new_clock is not None and new_clock > old_clock:
                notes.append(f"clock up {before.get('time')}->{after.get('time')}")
    if (
        _state(after) == "in_progress"
        and _clock_seconds(after.get("time")) is None
        and after.get("status") != "Halftime"
        and not str(after.get("time") or "").startswith(("END", "Half"))
    ):
        notes.append(f"no clock ({after.get('time')!r})")
    return notes


def _number(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    return value if isinstance(value, int) else 0


def _shot(before: dict[str, Any], after: dict[str, Any]) -> str:
    """The made shots between two lines of one player, as "3PT", "FG", "2 FT"; empty if none."""
    threes = _number(after, "fg3m") - _number(before, "fg3m")
    twos = _number(after, "fgm") - _number(before, "fgm") - threes
    frees = _number(after, "ftm") - _number(before, "ftm")
    parts = [f"{n} {label}" if n > 1 else label for n, label in ((threes, "3PT"), (twos, "FG"), (frees, "FT")) if n > 0]
    return "+".join(parts)


def _player_change(before: dict[str, Any] | None, after: dict[str, Any]) -> str | None:
    """A readable line for one player's line that changed, or None when no counter moved."""
    player = after.get("player") or {}
    team = after.get("team") or {}
    game = after.get("game") or {}
    old = before or {}
    moves = []
    for field, label in _COUNTERS:
        change = _number(after, field) - _number(old, field)
        if change:
            shot = _shot(old, after) if field == "pts" else ""
            moves.append(f"{change:+d} {label}{f' ({shot})' if shot else ''}")
    misses = (_number(after, "fga") - _number(after, "fgm")) - (_number(old, "fga") - _number(old, "fgm"))
    if misses > 0:
        moves.append(f"{misses} missed FG")
    if not moves:
        return None
    name = f"{str(player.get('first_name') or '?')[:1]}.{player.get('last_name') or '?'}"
    line = f"{_number(after, 'pts')} PTS {_number(after, 'reb')} REB {_number(after, 'ast')} AST"
    return f"g{game.get('id')} P{game.get('period')} {game.get('time')!s:<8} {team.get('abbreviation', '?'):>3} {name:<16} {', '.join(moves)}  [{line}]"


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _probe_targets(
    today: date, yesterday: date, game: int | None, done: int | None, team: int | None, season: int
) -> list[tuple[str, str, dict[str, Any]]]:
    """Every documented NBA endpoint, with the least a request needs: (tier the docs name, path, params)."""
    g = game or done
    targets: list[tuple[str, str, dict[str, Any]]] = [
        ("Free", "/v1/teams", {}),
        ("Free", "/v1/players", {"per_page": 5}),
        ("ALL-STAR", "/v1/stats", {"game_ids[]": [g] if g else [], "per_page": 5}),
        ("ALL-STAR", "/v1/players/active", {"per_page": 5}),
        ("ALL-STAR", "/v1/player_injuries", {"per_page": 5}),
        ("GOAT", "/v1/box_scores/live", {}),
        ("GOAT", "/v1/box_scores", {"date": yesterday.isoformat()}),
        ("GOAT", "/v1/plays", {"game_id": g}),
        ("GOAT", "/v1/standings", {"season": season}),
        ("GOAT", "/v1/leaders", {"stat_type": "pts", "season": season}),
        ("GOAT", "/v1/season_averages/general", {"season": season, "season_type": "regular", "type": "base"}),
        ("GOAT", "/nba/v1/team_season_averages/general", {"season": season, "season_type": "regular", "type": "base"}),
        ("GOAT", "/nba/v1/stats/advanced", {"game_ids[]": [done] if done else [], "per_page": 5}),
        ("GOAT", "/nba/v2/stats/advanced", {"game_ids[]": [done] if done else [], "per_page": 5}),
        ("GOAT", "/nba/v1/win_probabilities", {"game_id": done}),
        ("GOAT", "/nba/v1/lineups", {"game_ids[]": [g] if g else [], "per_page": 5}),
        ("GOAT", "/nba/v1/projected_lineups", {"dates[]": [today.isoformat()], "per_page": 5}),
        ("GOAT", "/nba/v1/shot_charts", {"game_ids[]": [done] if done else [], "per_page": 5}),
        ("GOAT", "/nba/v1/lineup_stats", {"game_ids[]": [done] if done else [], "per_page": 5}),
        ("GOAT", "/nba/v1/matchups", {"game_ids[]": [done] if done else [], "per_page": 5}),
        ("GOAT", "/nba/v1/stats/tracking", {"type": "speed_distance", "game_ids[]": [done] if done else []}),
        ("GOAT", "/v2/odds", {"dates[]": [today.isoformat()]}),
        ("GOAT", "/v2/odds/player_props", {"game_id": g}),
        ("GOAT", "/nba/v1/dfs/slates", {"per_page": 5}),
        ("GOAT", "/v1/contracts/teams", {"team_id": team}),
    ]
    return [(tier, path, {k: v for k, v in params.items() if v is not None}) for tier, path, params in targets]


def _probe(client: Client) -> int:
    now = _now()
    today = now.astimezone(_EASTERN).date()
    yesterday = today - timedelta(days=1)
    folder = _OUT / f"probe-{now:%Y%m%dT%H%M%SZ}"
    stamp_recording(folder, DataSource.BALLDONTLIE, now)
    saved = folder / "responses.jsonl"
    print(f"BALLDONTLIE NBA probe at {_local(now)} local ({now.astimezone(_EASTERN):%Y-%m-%d %H:%M} US Eastern)")
    slates = {}
    for day in (yesterday, today):
        answer = client.get("/v1/games", {"dates[]": day.isoformat(), "per_page": 100})
        _append(saved, answer.record(body=answer.body))
        if answer.status in (401, 403):
            print(f"GET /v1/games: HTTP {answer.status}; the key was refused, check {_KEY_VAR} in .env.")
            return 1
        slates[day] = _rows(answer.body)
        print(f"{day} (US Eastern): {len(slates[day])} games")
    live = [g for g in slates[today] if _state(g) == "in_progress"] or slates[today]
    done = [g for g in slates[yesterday] if _state(g) == "final"]
    game = live[0].get("id") if live else None
    finished = done[0].get("id") if done else None
    team = (live[0].get("home_team") or {}).get("id") if live else None
    season = int(live[0].get("season") or today.year) if live else today.year
    print(f"Probing with game {game} (today), game {finished} (yesterday's final), team {team}, season {season}\n")
    capabilities: dict[str, Any] = {"at": _stamp(now), "endpoints": {}}
    print(f"{'endpoint':<40} {'docs':<9} {'HTTP':>4} {'rows':>5}  fields of a row")
    for tier, path, params in _probe_targets(today, yesterday, game, finished, team, season):
        answer = client.get(path, params)
        _append(saved, answer.record(body=answer.body))
        rows = _rows(answer.body) if answer.status == 200 else []
        fields = sorted(rows[0]) if rows else []
        meta = answer.body.get("meta") if isinstance(answer.body, dict) else None
        capabilities["endpoints"][path] = {
            "docs_tier": tier,
            "status": answer.status,
            "rows": len(rows),
            "fields": fields,
            "paged": isinstance(meta, dict) and "next_cursor" in meta,
            "took_s": round(answer.took, 3),
        }
        shown = ", ".join(fields) if answer.status == 200 else client.scrub(json.dumps(answer.body)[:80])
        print(f"{path:<40} {tier:<9} {answer.status:>4} {len(rows):>5}  {shown[:150]}")
    (folder / "capabilities.json").write_text(json.dumps(capabilities, indent=2) + "\n", encoding="utf-8")
    print(f"\n{client.requests} requests; raw answers in {saved.relative_to(_ROOT)}, summary in capabilities.json")
    return 0


def _sleep_until(moment: datetime) -> None:
    """Sleep until a wall-clock moment, in short naps, so a suspended machine wakes on time."""
    while (remaining := (moment - _now()).total_seconds()) > 0:
        time.sleep(min(remaining, 60.0))


@dataclass(frozen=True, slots=True)
class Cadence:
    every: float
    stats_every: float
    plays_every: float
    box_every: float
    injuries_every: float
    final_minutes: float


class Recorder:
    def __init__(self, client: Client, day: date, out: Path, cadence: Cadence) -> None:
        self._client = client
        self._day = day
        self._out = out
        self._cadence = cadence
        self._last: dict[Any, dict[str, Any]] = {}
        self._lines: dict[Any, dict[str, Any]] = {}  # a player line by its id, as last written
        self._plays_seen: dict[Any, set[Any]] = {}
        self._over_since: dict[Any, float] = {}  # a game's id -> monotonic second it was first seen over
        self._summary: TextIO = (out / "summary.log").open("a", buffering=1)
        self._deltas: TextIO = (out / "stat-deltas.log").open("a", buffering=1)
        self._plays_log: TextIO = (out / "plays.log").open("a", buffering=1)
        self.plays = False
        self.box = False

    def log(self, line: str) -> None:
        self._summary.write(f"{_local(_now())}  {line}\n")

    def _refused(self, answer: Answer) -> None:
        if answer.status in (401, 403) and answer.query.startswith("/v1/games"):
            raise Stop(f"HTTP {answer.status}: the key was refused")
        if answer.status != 200:
            self.log(
                f"!! {answer.query.split('?')[0]} HTTP {answer.status}: {self._client.scrub(json.dumps(answer.body)[:200])}"
            )

    def poll(self) -> list[dict[str, Any]]:
        answer = self._client.get("/v1/games", {"dates[]": self._day.isoformat(), "per_page": 100})
        _append(self._out / "games.jsonl", answer.record(body=answer.body))
        self._refused(answer)
        if answer.status != 200:
            return []
        games = _rows(answer.body)
        now = time.monotonic()
        for game in games:
            if _state(game) in _DONE:
                self._over_since.setdefault(game.get("id"), now)
            before = self._last.get(game.get("id"))
            line = _describe(game)
            if before is not None and _describe(before) == line:
                continue
            notes = _oddities(before, game) if before is not None else []
            self.log(f"{line}{'  !! ' + '; '.join(notes) if notes else ''}  ({answer.took:.2f}s)")
            self._last[game.get("id")] = game
        return games

    def hot(self, games: list[dict[str, Any]]) -> list[Any]:
        """The games whose lines are worth reading: running, or over for less than the final window."""
        now = time.monotonic()
        window = self._cadence.final_minutes * 60
        return [
            game.get("id")
            for game in games
            if _state(game) == "in_progress"
            or (_state(game) == "final" and now - self._over_since.get(game.get("id"), now) < window)
        ]

    def stats(self, game_ids: list[Any]) -> None:
        for answer in self._client.pages("/v1/stats", {"game_ids[]": game_ids, "per_page": 100}):
            self._refused(answer)
            changed = []
            for row in _rows(answer.body) if answer.status == 200 else []:
                comparable = {k: v for k, v in row.items() if k != "game"}
                before = self._lines.get(row.get("id"))
                if before is not None and {k: v for k, v in before.items() if k != "game"} == comparable:
                    continue
                changed.append(row)
                described = _player_change(before, row)
                if described is not None:
                    self._deltas.write(f"{_local(answer.received)}  {described}\n")
                self._lines[row.get("id")] = row
            meta = answer.body.get("meta") if isinstance(answer.body, dict) else None
            _append(
                self._out / "stats.jsonl",
                answer.record(meta=meta, rows=len(_rows(answer.body)), changed=changed, game_ids=game_ids),
            )

    def try_plays(self, game_id: Any) -> None:
        answers = self._client.pages("/v1/plays", {"game_id": game_id})
        seen = self._plays_seen.setdefault(game_id, set())
        for answer in answers:
            new = []
            for play in _rows(answer.body) if answer.status == 200 else []:
                ident = play.get("id") or (play.get("order"), play.get("period"), play.get("clock"))
                if ident in seen:
                    continue
                seen.add(ident)
                new.append(play)
                self._plays_log.write(
                    f"{_local(answer.received)}  g{game_id} P{play.get('period')} {play.get('clock')!s:<6}"
                    f" {play.get('away_score')}-{play.get('home_score')}  {play.get('text')}\n"
                )
            _append(self._out / "plays.jsonl", answer.record(game_id=game_id, new=new))
            if answer.status in (401, 403):
                self.plays = False

    def box_scores(self) -> None:
        answer = self._client.get("/v1/box_scores/live")
        _append(self._out / "box-live.jsonl", answer.record(body=answer.body if answer.status == 200 else None))
        if answer.status in (401, 403):
            self.box = False

    def listing(self, name: str, path: str, params: dict[str, Any]) -> None:
        for answer in self._client.pages(path, params):
            _append(self._out / f"{name}.jsonl", answer.record(body=answer.body))
            self._refused(answer)

    def start(self, games: list[dict[str, Any]]) -> None:
        """What a night needs once: teams, active players, injuries, and which GOAT endpoints answer."""
        self.listing("teams", "/v1/teams", {})
        self.listing("players-active", "/v1/players/active", {"per_page": 100})
        self.listing("injuries", "/v1/player_injuries", {"per_page": 100})
        sample = games[0].get("id") if games else None
        if sample is not None:
            probe = self._client.get("/v1/plays", {"game_id": sample})
            _append(self._out / "plays.jsonl", probe.record(game_id=sample, body=probe.body))
            self.plays = probe.status == 200
        answer = self._client.get("/v1/box_scores/live")
        self.box = answer.status == 200
        self.log(
            f"this key answers: player stats yes; play-by-play {'yes' if self.plays else 'no'}; live box scores {'yes' if self.box else 'no'}"
        )

    def run(self, games: list[dict[str, Any]], *, lead: timedelta, deadline: datetime | None) -> None:
        tips = [tip for game in games if (tip := _parse(game.get("datetime"))) is not None]
        first = min(tips) if tips else _now()
        until = deadline or (max(tips) if tips else first) + timedelta(hours=5)
        self.log(
            f"{len(games)} games on {self._day}; first tip {_local(first)}, last {_local(max(tips)) if tips else '?'}; stop by {_local(until)}"
        )
        start = first - lead
        if start > _now():
            self.log(f"no requests until {_local(start)}")
            _sleep_until(start)
        self.start(games)
        cadence = self._cadence
        due = {
            "games": 0.0,
            "stats": 0.0,
            "plays": 0.0,
            "box": 0.0,
            "injuries": time.monotonic() + cadence.injuries_every,
        }
        plays_turn = 0
        current = games
        while _now() < until:
            now = time.monotonic()
            try:
                if now >= due["games"]:
                    due["games"] = now + cadence.every
                    current = self.poll() or current
                    over = [self._over_since.get(game.get("id")) for game in current]
                    if current and all(at is not None and now - at >= cadence.final_minutes * 60 for at in over):
                        self.log("every game is over")
                        return
                hot = self.hot(current)
                if hot and now >= due["stats"]:
                    due["stats"] = now + cadence.stats_every
                    self.stats(hot)
                if hot and self.plays and now >= due["plays"]:
                    due["plays"] = now + cadence.plays_every / len(hot)
                    self.try_plays(hot[plays_turn % len(hot)])
                    plays_turn += 1
                if hot and self.box and now >= due["box"]:
                    due["box"] = now + cadence.box_every
                    self.box_scores()
                if now >= due["injuries"]:
                    due["injuries"] = now + cadence.injuries_every
                    self.listing("injuries", "/v1/player_injuries", {"per_page": 100})
            except (URLError, TimeoutError, OSError) as exc:
                self.log(f"!! {type(exc).__name__}: {self._client.scrub(str(exc))}")
            waits = [due["games"], due["injuries"]]
            if self.hot(current):
                waits += [due["stats"]] + ([due["plays"]] if self.plays else []) + ([due["box"]] if self.box else [])
            time.sleep(max(0.0, min(waits) - time.monotonic()))

    def close(self) -> None:
        for handle in (self._summary, self._deltas, self._plays_log):
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true", help="ask every NBA endpoint once and print what answers")
    parser.add_argument("--date", type=date.fromisoformat, help="the slate to record (US Eastern date); default today")
    parser.add_argument("--per-minute", type=int, default=55, help="requests in any 60 s (Free 5, ALL-STAR 60)")
    parser.add_argument("--every", type=float, default=5.0, help="seconds between slate reads")
    parser.add_argument("--stats-every", type=float, default=5.0, help="seconds between player-line reads")
    parser.add_argument("--plays-every", type=float, default=10.0, help="seconds between one game's play reads")
    parser.add_argument("--box-every", type=float, default=30.0, help="seconds between live box score reads")
    parser.add_argument("--injuries-every", type=float, default=1800.0, help="seconds between injury reads")
    parser.add_argument("--final-minutes", type=float, default=3.0, help="keep reading a game this long after it ends")
    parser.add_argument("--lead-minutes", type=float, default=10.0, help="start this long before the first tip")
    parser.add_argument(
        "--hours", type=float, help="stop this many hours after starting (default: 5 h past the last tip)"
    )
    parser.add_argument("--max-requests", type=int, default=20000)
    args = parser.parse_args()
    client = Client(_key(), max_requests=args.max_requests, per_minute=args.per_minute)
    if args.probe:
        return _probe(client)
    day = args.date or datetime.now(_EASTERN).date()
    out = _OUT / day.isoformat()
    stamp_recording(out, DataSource.BALLDONTLIE, _now())
    cadence = Cadence(
        args.every, args.stats_every, args.plays_every, args.box_every, args.injuries_every, args.final_minutes
    )
    recorder = Recorder(client, day, out, cadence)
    deadline = _now() + timedelta(hours=args.hours) if args.hours else None
    try:
        games = recorder.poll()
        if not games:
            recorder.log(f"no games on {day}; nothing to record")
            return 1
        recorder.run(games, lead=timedelta(minutes=args.lead_minutes), deadline=deadline)
    except Stop as exc:
        recorder.log(f"stopped: {exc}")
    finally:
        recorder.log(f"done: {client.requests} requests")
        recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
