"""Probe BALLDONTLIE's NBA API, or record one night's slate from it; prints no secret.

    .venv/bin/python tools/balldontlie_record.py --probe
    .venv/bin/python tools/balldontlie_record.py [--date YYYY-MM-DD] [--every 13] [--lead-minutes 10]

Reads ``BALLDONTLIE_API_KEY`` from the environment or the repo's git-ignored ``.env``.

``--probe`` makes four requests: the previous and current US Eastern dates' slates, and two endpoints
above the Free tier (player stats and live box scores), to see what this key's tier answers. It prints
each game, the fields a game carries and any rate-limit or cache headers.

Otherwise it records one date's slate (default: today, US Eastern, the API's own dates). It makes one
request to learn the tip-off times, then none until shortly before the first tip. From then on it reads
the whole slate in one request every ``--every`` seconds; the Free tier allows 5 a minute, so the default
13 seconds leaves a little room. It stops a few polls after every game is final, or at the request cap
or deadline.

Everything lands in ``build/recordings/balldontlie/nba/``, a folder per probe (``probe-<instant>/``) or
night (``<date>/``): the raw responses as JSON lines, stamped with when they arrived, and, when
recording, ``<date>/summary.log``, a readable line per change in any game (``tail -f`` it during the
games). Each folder is made with a ``RETENTION.json`` stamp; BALLDONTLIE's terms allow keeping what it
answers, so the stamp says so (``tools/recordings_due.py`` lists the stamps).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
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

_BASE = "https://api.balldontlie.io/v1"
_KEY_VAR = "BALLDONTLIE_API_KEY"
_EASTERN = ZoneInfo("America/New_York")  # the API dates games by their US Eastern date
_OUT = _ROOT / "build" / "recordings" / "balldontlie" / "nba"
_DONE = {"final", "postponed", "canceled", "abandoned"}  # status_state values that end a game's night
_KEPT_HEADERS = ("ratelimit", "rate-limit", "retry-after", "cache", "age", "date", "etag", "last-modified", "cf-ray")


class Stop(Exception):
    """An answer that means going on can't help."""


class Client:
    def __init__(self, key: str, *, max_requests: int) -> None:
        self._key = key
        self._max_requests = max_requests
        self.requests = 0

    def scrub(self, text: str) -> str:
        return text.replace(self._key, "<key>") if self._key else text

    def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[int, Any, dict[str, str]]:
        """``(HTTP status, parsed body, kept headers)``; a 429 is waited out and retried."""
        query = f"?{urlencode(params, safe='[]')}" if params else ""
        request = Request(_BASE + path + query, headers={"Authorization": self._key, "User-Agent": "omni-scoreboard"})
        for _ in range(3):
            if self.requests >= self._max_requests:
                raise Stop(f"request cap of {self._max_requests} reached")
            self.requests += 1
            try:
                with urlopen(request, timeout=15) as response:
                    return response.status, json.load(response), _kept(response.headers.items())
            except HTTPError as exc:
                headers = _kept(exc.headers.items())
                text = self.scrub(exc.read()[:400].decode("utf-8", "replace"))
                if exc.code == 429:
                    wait = float(headers.get("retry-after") or 60)
                    if wait > 600:
                        raise Stop(f"429 with Retry-After {wait:.0f}s: {text}") from exc
                    time.sleep(wait)
                    continue
                try:
                    body: Any = json.loads(text)
                except ValueError:
                    body = text
                return exc.code, body, headers
        raise Stop("still rate-limited after retries")


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


def _games(body: Any) -> list[dict[str, Any]]:
    data = body.get("data") if isinstance(body, dict) else None
    return [game for game in data or [] if isinstance(game, dict)]


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
    """A game clock like "3:44" or "38.2" in seconds; None for a missing or odd value."""
    text = str(raw or "").strip()
    try:
        if ":" in text:
            minutes, seconds = text.split(":", 1)
            return int(minutes) * 60 + float(seconds)
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
    ):
        notes.append(f"no clock ({after.get('time')!r})")
    return notes


def _timed_get(
    client: Client, path: str, params: dict[str, Any] | None = None
) -> tuple[int, Any, dict[str, str], datetime, float]:
    """``Client.get``'s answer, plus when it arrived and how long it took."""
    started = time.monotonic()
    status, body, headers = client.get(path, params)
    return status, body, headers, datetime.now(timezone.utc), time.monotonic() - started


def _slate(client: Client, day: date) -> tuple[int, Any, dict[str, str], datetime, float]:
    return _timed_get(client, "/games", {"dates[]": day.isoformat(), "per_page": 100})


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _probe(client: Client) -> int:
    now = datetime.now(timezone.utc)
    today = now.astimezone(_EASTERN).date()
    folder = _OUT / f"probe-{now:%Y%m%dT%H%M%SZ}"
    stamp_recording(folder, DataSource.BALLDONTLIE, now)
    saved = folder / "responses.jsonl"
    print(f"BALLDONTLIE NBA probe at {_local(now)} local ({now.astimezone(_EASTERN):%Y-%m-%d %H:%M} US Eastern)")
    fields: set[str] = set()
    first_headers: dict[str, str] = {}
    sample_id = None
    for day in (today - timedelta(days=1), today):
        status, body, headers, received, took = _slate(client, day)
        _append(
            saved,
            {
                "received_at": _stamp(received),
                "took_s": round(took, 3),
                "status": status,
                "headers": headers,
                "body": body,
                "query": f"/games {day}",
            },
        )
        if status != 200:
            print(f"GET /games {day}: HTTP {status} {client.scrub(json.dumps(body)[:300])}")
            if status in (401, 403):
                print("The key was refused; check BALLDONTLIE_API_KEY in .env.")
                return 1
            continue
        first_headers = first_headers or headers
        games = _games(body)
        print(f"\n{day} (US Eastern): {len(games)} games, answered in {took:.2f}s")
        for game in sorted(games, key=lambda g: str(g.get("datetime"))):
            tip = _parse(game.get("datetime"))
            print(f"  {game.get('id')!s:>9}  tip {_local(tip) if tip else '?':>8}  {_describe(game)}")
            fields.update(game)
            sample_id = sample_id or game.get("id")
    print(f"\nA game's fields: {', '.join(sorted(fields)) or '(none seen)'}")
    print(f"Headers of note: {json.dumps(first_headers) if first_headers else '(none)'}")
    for path, params in (("/stats", {"game_ids[]": sample_id} if sample_id else {}), ("/box_scores/live", None)):
        status, body, headers, received, took = _timed_get(client, path, params)
        _append(
            saved,
            {
                "received_at": _stamp(received),
                "took_s": round(took, 3),
                "status": status,
                "headers": headers,
                "body": body,
                "query": path,
            },
        )
        answer = f"{len(_games(body))} rows" if status == 200 else client.scrub(json.dumps(body)[:200])
        print(f"GET {path}: HTTP {status}, {answer}")
    print(f"\n{client.requests} requests; raw responses saved to {saved.relative_to(_ROOT)}")
    return 0


def _sleep_until(moment: datetime) -> None:
    """Sleep until a wall-clock moment, in short naps, so a suspended machine wakes on time."""
    while (remaining := (moment - datetime.now(timezone.utc)).total_seconds()) > 0:
        time.sleep(min(remaining, 60.0))


class Recorder:
    def __init__(self, client: Client, day: date, out: Path, *, every: float, final_polls: int) -> None:
        self._client = client
        self._day = day
        self._out = out
        self._every = every
        self._final_polls = final_polls
        self._last: dict[Any, dict[str, Any]] = {}
        self._summary: TextIO = (out / "summary.log").open("a", buffering=1)

    def log(self, line: str) -> None:
        self._summary.write(f"{_local(datetime.now(timezone.utc))}  {line}\n")

    def poll(self) -> list[dict[str, Any]]:
        status, body, headers, received, took = _slate(self._client, self._day)
        _append(
            self._out / "games.jsonl",
            {
                "received_at": _stamp(received),
                "monotonic": round(time.monotonic(), 3),
                "took_s": round(took, 3),
                "status": status,
                "headers": headers,
                "body": body,
            },
        )
        if status != 200:
            self.log(f"!! HTTP {status}: {self._client.scrub(json.dumps(body)[:200])}")
            if status in (401, 403):
                raise Stop(f"HTTP {status}: the key was refused")
            return []
        games = _games(body)
        for game in games:
            before = self._last.get(game.get("id"))
            line = _describe(game)
            if before is not None and _describe(before) == line:
                continue
            notes = _oddities(before, game) if before is not None else []
            self.log(f"{line}{'  !! ' + '; '.join(notes) if notes else ''}  ({took:.2f}s)")
            self._last[game.get("id")] = game
        return games

    def run(self, games: list[dict[str, Any]], *, lead: timedelta, deadline: datetime | None) -> None:
        tips = [tip for game in games if (tip := _parse(game.get("datetime"))) is not None]
        first = min(tips) if tips else datetime.now(timezone.utc)
        until = deadline or (max(tips) if tips else first) + timedelta(hours=5)
        self.log(
            f"{len(games)} games on {self._day}; first tip {_local(first)}, last {_local(max(tips)) if tips else '?'}; stop by {_local(until)}"
        )
        start = first - lead
        if start > datetime.now(timezone.utc):
            self.log(f"no requests until {_local(start)}")
            _sleep_until(start)
        finals = 0
        while finals < self._final_polls and datetime.now(timezone.utc) < until:
            started = time.monotonic()
            try:
                current = self.poll()
            except (URLError, TimeoutError, OSError) as exc:
                self.log(f"!! {type(exc).__name__}: {self._client.scrub(str(exc))}")
                current = []
            done = bool(current) and all(_state(game) in _DONE for game in current)
            finals = finals + 1 if done else 0
            time.sleep(max(0.0, self._every - (time.monotonic() - started)))

    def close(self) -> None:
        self._summary.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true", help="check the key and print what it answers, then exit")
    parser.add_argument("--date", type=date.fromisoformat, help="the slate to record (US Eastern date); default today")
    parser.add_argument("--every", type=float, default=13.0, help="seconds between slate reads (Free tier: >= 12)")
    parser.add_argument("--lead-minutes", type=float, default=10.0, help="start this long before the first tip")
    parser.add_argument(
        "--hours", type=float, help="stop this many hours after starting (default: 5 h past the last tip)"
    )
    parser.add_argument("--final-polls", type=int, default=3, help="polls to keep making once every game is final")
    parser.add_argument("--max-requests", type=int, default=3000)
    args = parser.parse_args()
    client = Client(_key(), max_requests=args.max_requests)
    if args.probe:
        return _probe(client)
    day = args.date or datetime.now(_EASTERN).date()
    out = _OUT / day.isoformat()
    stamp_recording(out, DataSource.BALLDONTLIE, datetime.now(timezone.utc))
    recorder = Recorder(client, day, out, every=args.every, final_polls=args.final_polls)
    deadline = datetime.now(timezone.utc) + timedelta(hours=args.hours) if args.hours else None
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
