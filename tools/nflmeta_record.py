"""Record one live NFL game from NFLMeta, to measure it and to keep as a replay fixture; prints no secret.

    .venv/bin/python tools/nflmeta_record.py --list
    .venv/bin/python tools/nflmeta_record.py --teams PIT,CIN [--lead-minutes 10] [--max-requests 2000]

Reads ``NFLMETA_API_KEY`` from the environment or the repo's git-ignored ``.env``. ``--list`` makes one
request and prints the current slate. Otherwise it finds the game, makes no requests until shortly
before kickoff, then polls the scoreboard at the pace the API recommends and pulls only new or
corrected plays through the game's change cursor. It stops a few polls after the final score, or at
the request cap.

Everything lands in ``build/recordings/nflmeta/<date>-<away>-<home>/``: the raw responses as JSON
lines, stamped with when they arrived, and ``summary.log``, a readable line per change to compare
against the broadcast (``tail -f`` it during the game).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.core.env import read_env_file

_BASE = "https://nflmeta.org"
_KEY_VAR = "NFLMETA_API_KEY"
_MIN_POLL_S = 10.0
_MAX_POLL_S = 60.0
_PLAYS_SAFETY_S = 60.0  # re-read the change feed this often even with no new play, to catch corrections
_FINAL_POLLS = 3  # polls to keep making after the game reports itself completed


class Stop(Exception):
    """An answer that means recording can't usefully go on."""


class Client:
    def __init__(self, key: str, *, max_requests: int) -> None:
        self._key = key
        self._max_requests = max_requests
        self.requests = 0
        self.rows = 0

    def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        if self.requests >= self._max_requests:
            raise Stop(f"request cap of {self._max_requests} reached")
        url = _BASE + path + (f"?{urlencode(params)}" if params else "")
        request = Request(url, headers={"X-NFLMeta-Key": self._key, "User-Agent": "omni-scoreboard"})
        for _ in range(3):
            self.requests += 1
            try:
                with urlopen(request, timeout=15) as response:
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    self.rows += int(headers.get("x-nflmeta-response-rows", "0") or 0)
                    return json.load(response), headers
            except HTTPError as exc:
                body = exc.read()[:300].decode("utf-8", "replace")
                if exc.code == 429:
                    wait = float(exc.headers.get("Retry-After", "30"))
                    if wait > 120:
                        raise Stop(f"429 with Retry-After {wait:.0f}s (monthly quota?): {body}") from exc
                    time.sleep(wait)
                    continue
                if exc.code in (401, 402, 403):
                    raise Stop(f"HTTP {exc.code}: {body}") from exc
                raise
        raise Stop("still rate-limited after retries")


def _key() -> str:
    import os

    key = os.environ.get(_KEY_VAR) or read_env_file(_ROOT / ".env").get(_KEY_VAR)
    if not key:
        raise SystemExit(f"{_KEY_VAR} is not set in the environment or .env")
    return key


def _games(body: Any) -> list[dict[str, Any]]:
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        data = data.get("games")
    return [game for game in data or [] if isinstance(game, dict)]


def _abbr(game: dict[str, Any], side: str) -> str:
    return str((game.get(f"{side}_team") or {}).get("abbr") or "?")


def _parse(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _local(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M:%S")


class Recorder:
    def __init__(self, client: Client, game_id: int, out: Path) -> None:
        self._client = client
        self._game_id = game_id
        self._out = out
        self._cursor: str | None = None
        self._last_play_id: str | None = None
        self._last_plays_at = 0.0
        self._last_line: str | None = None
        self._summary = (out / "summary.log").open("a", buffering=1)

    def _write(self, name: str, record: dict[str, Any]) -> None:
        with (self._out / name).open("a") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def _log(self, line: str) -> None:
        self._summary.write(line + "\n")

    def poll_scoreboard(self) -> tuple[dict[str, Any] | None, float]:
        body, headers = self._client.get("/api/v1/live-scores", {"game_id": self._game_id})
        received = datetime.now(timezone.utc)
        self._write(
            "live-scores.jsonl", {"received_at": _stamp(), "rows": headers.get("x-nflmeta-response-rows"), "body": body}
        )
        meta = body.get("meta") if isinstance(body, dict) else {}
        recommended = (meta or {}).get("recommended_poll_seconds")
        pace = min(max(float(recommended or _MIN_POLL_S), _MIN_POLL_S), _MAX_POLL_S)
        games = _games(body)
        game = games[0] if games else None
        if game is not None:
            self._log_scoreboard(game, received, stale=bool((meta or {}).get("stale")))
        return game, pace

    def _log_scoreboard(self, game: dict[str, Any], received: datetime, *, stale: bool) -> None:
        situation = game.get("situation") or {}
        sync = game.get("play_sync") or {}
        line = (
            f"Q{game.get('period')} {game.get('clock')}  {_abbr(game, 'away')} {(game.get('away_team') or {}).get('score')}"
            f" - {_abbr(game, 'home')} {(game.get('home_team') or {}).get('score')}  [{game.get('phase')}]"
            f" | {situation.get('possession_abbr') or '-'} ball, {situation.get('text') or 'no situation'}"
            f" | play #{sync.get('last_play_number')} {sync.get('status')}{'  STALE' if stale else ''}"
        )
        if line == self._last_line:
            return
        self._last_line = line
        observed = _parse(game.get("changed_at") or game.get("observed_at"))
        lag = f"{(received - observed).total_seconds():5.1f}s after NFLMeta saw it" if observed else "no observed_at"
        self._log(f"{_local(received)}  {line}  ({lag})")

    def maybe_poll_plays(self, game: dict[str, Any] | None) -> None:
        sync = (game or {}).get("play_sync") or {}
        play_id = sync.get("last_play_id")
        due = time.monotonic() - self._last_plays_at >= _PLAYS_SAFETY_S
        if play_id == self._last_play_id and not due and self._cursor is not None:
            return
        self._last_play_id = play_id
        self._last_plays_at = time.monotonic()
        while True:
            params: dict[str, Any] = {"limit": 100}
            if self._cursor is not None:
                params["cursor"] = self._cursor
            try:
                body, headers = self._client.get(f"/api/v1/games/{self._game_id}/play-changes", params)
            except HTTPError as exc:
                if exc.code != 410:
                    raise
                self._log(f"{_local(datetime.now(timezone.utc))}  !! play cursor expired (410); a gap starts here")
                self._cursor = None
                body, headers = self._client.get(f"/api/v1/games/{self._game_id}/play-changes", {"start": "latest"})
            received = datetime.now(timezone.utc)
            self._write(
                "play-changes.jsonl",
                {"received_at": _stamp(), "rows": headers.get("x-nflmeta-response-rows"), "body": body},
            )
            for change in body.get("data") or []:
                self._log_play(change, received)
            meta = body.get("meta") or {}
            self._cursor = meta.get("next_cursor") or self._cursor
            if not meta.get("has_more"):
                return

    def _log_play(self, change: dict[str, Any], received: datetime) -> None:
        play = change.get("play") or {}
        changed = _parse(change.get("changed_at"))
        lag = f"{(received - changed).total_seconds():.1f}s" if changed else "?"
        timeouts = f"TO {play.get('posteam_timeouts_remaining')}/{play.get('defteam_timeouts_remaining')}"
        description = " ".join(str(play.get("description") or "").split())[:140]
        self._log(
            f"{_local(received)}    {change.get('change_type'):>9} #{play.get('play_number')} r{change.get('revision')}"
            f" {play.get('play_type') or '-'}: {description}  [{play.get('posteam_abbr')} {timeouts}] (+{lag})"
        )

    def close(self) -> None:
        self._summary.close()


def _find_game(client: Client, teams: set[str]) -> dict[str, Any]:
    body, _ = client.get("/api/v1/live-scores")
    for game in _games(body):
        if {_abbr(game, "away").upper(), _abbr(game, "home").upper()} == teams:
            return game
    raise SystemExit(f"no game between {', '.join(sorted(teams))} in the current live-score window")


def _print_slate(client: Client) -> None:
    body, headers = client.get("/api/v1/live-scores")
    games = _games(body)
    for game in games:
        kickoff = _parse(game.get("kickoff_at"))
        when = _local(kickoff) if kickoff else "?"
        print(
            f"{game.get('game_id'):>8}  {_abbr(game, 'away'):>3} @ {_abbr(game, 'home'):<3}  kickoff {when}  {game.get('phase')}  {game.get('status_detail') or ''}"
        )
    meta = body.get("meta") if isinstance(body, dict) else {}
    print(f"{len(games)} games; meta {json.dumps(meta)[:200]}; rows billed {headers.get('x-nflmeta-response-rows')}")
    print(f"quota headers: {({k: v for k, v in headers.items() if 'ratelimit' in k or 'quota' in k})}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the current slate and exit")
    parser.add_argument("--teams", help="the two teams, e.g. PIT,CIN")
    parser.add_argument("--lead-minutes", type=float, default=10.0, help="start polling this long before kickoff")
    parser.add_argument("--max-requests", type=int, default=2000)
    args = parser.parse_args()
    client = Client(_key(), max_requests=args.max_requests)
    if args.list:
        _print_slate(client)
        return 0
    if not args.teams:
        parser.error("--teams is required unless --list")
    teams = {team.strip().upper() for team in args.teams.split(",")}
    game = _find_game(client, teams)
    game_id = int(game["game_id"])
    kickoff = _parse(game.get("kickoff_at")) or datetime.now(timezone.utc)
    out = (
        _ROOT
        / "build"
        / "recordings"
        / "nflmeta"
        / f"{kickoff.astimezone():%Y-%m-%d}-{_abbr(game, 'away')}-{_abbr(game, 'home')}"
    )
    out.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(client, game_id, out)
    recorder._log(
        f"{_local(datetime.now(timezone.utc))}  game {game_id}, kickoff {_local(kickoff)}; recording to {out}"
    )
    wait = (kickoff - timedelta(minutes=args.lead_minutes) - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        recorder._log(f"{_local(datetime.now(timezone.utc))}  waiting {wait / 60:.0f} min before the first poll")
        time.sleep(wait)
    finals = 0
    try:
        while finals < _FINAL_POLLS:
            started = time.monotonic()
            try:
                current, pace = recorder.poll_scoreboard()
                recorder.maybe_poll_plays(current)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                recorder._log(f"{_local(datetime.now(timezone.utc))}  !! {type(exc).__name__}: {exc}")
                current, pace = None, 30.0
            if current is not None and current.get("completed"):
                finals += 1
            time.sleep(max(0.0, pace - (time.monotonic() - started)))
    except Stop as exc:
        recorder._log(f"{_local(datetime.now(timezone.utc))}  stopped: {exc}")
    finally:
        recorder._log(
            f"{_local(datetime.now(timezone.utc))}  done: {client.requests} requests, {client.rows} rows billed"
        )
        recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
