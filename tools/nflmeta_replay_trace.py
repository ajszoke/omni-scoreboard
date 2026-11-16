"""Distill an NFLMeta game recording into a football replay trace; prints no secret and needs no key.

    .venv/bin/python tools/nflmeta_replay_trace.py build/recordings/nflmeta/2026-11-15-PIT-CIN \\
        --name 2026-11-15-PIT-CIN --out tests/fixtures/replay/football-2026-11-15-pit-cin.trace.jsonl

The recording (from ``tools/nflmeta_record.py`` and ``--record`` runs) holds NFLMeta's raw answers,
which its terms don't let us keep in the repo or share. This replays them through today's provider
as the board's feed would have asked, against a stand-in NFLMeta built from the recording:
- a scoreboard ask gets the last scoreboard the recording received by then;
- a play-feed ask gets every recorded change written by then, in the feed's order, after the cursor.

It writes two traces of the answers the board would have received (`omni.replay.football`):
- ``board-trace.jsonl`` beside the recording (git-ignored), as the provider worded them;
- ``--out``, minimized for the repo. It keeps the timing, the phases and breaks, the score, who has the
  ball, the timeouts and challenges, and each strip entry's kind and order. It has no NFLMeta text or
  ids: entries are renamed by their order ("PLAY 17"), and the next snap's down, distance and spot,
  like the game clock, are stand-ins. The stand-ins change exactly when the real values did, so a
  board decides alike on both traces. This tool checks that before it writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.app.clock import FakeClock
from omni.app.football_feed import FeedResult, GameFeed
from omni.domain.football import FootballGameState
from omni.events.football import FootballEvent
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, FeedNote, NflLiveSnapshot, NflMetaLiveProvider
from omni.replay.football import answer_record, header_record, load_trace, replay_board

_PERIOD_SECONDS = 900
# Recorded arrivals run up to a couple of seconds ahead of the feed's pace (request times vary, and the
# dev machine's wall clock steps), so an arrival this close to due counts as the ask the board made.
_EARLY = timedelta(seconds=2.5)


def _instant(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


class RecordedNflMeta:
    """NFLMeta as the recording saw it, answering as of a clock's reading."""

    def __init__(self, recording: Path, clock: FakeClock) -> None:
        self._clock = clock
        self._scores: list[tuple[datetime, Any]] = []
        for line in (recording / "live-scores.jsonl").open():
            entry = json.loads(line)
            self._scores.append((_instant(entry["received_at"]), entry["body"]))
        self._scores.sort(key=lambda item: item[0])
        changes: dict[int, tuple[datetime, Mapping[str, Any]]] = {}
        for line in (recording / "play-changes.jsonl").open():
            entry = json.loads(line)
            received = _instant(entry["received_at"])
            for row in (entry["body"] or {}).get("data") or []:
                written = _instant(row["changed_at"]) if row.get("changed_at") else received
                seen = changes.get(int(row["change_id"]))
                visible = min(written, received, seen[0]) if seen is not None else min(written, received)
                changes[int(row["change_id"])] = (visible, seen[1] if seen is not None else row)
        self._changes = sorted(changes.items())  # the feed's own order: by change id
        self.game_id = int(self._scores[0][1]["data"][0]["game_id"])

    @property
    def first(self) -> datetime:
        return self._scores[0][0]

    @property
    def arrivals(self) -> list[datetime]:
        """When each recorded scoreboard arrived, in order."""
        return [received for received, _ in self._scores]

    def __call__(self, path: str, query: Mapping[str, str | int]) -> Any:
        now = self._clock.now()
        if path == "/api/v1/live-scores":
            body = self._scores[0][1]
            for received, answer in self._scores:
                if received > now:
                    break
                body = answer
            return body
        if path.endswith("/play-changes"):
            after = int(str(query.get("cursor", "after:0")).removeprefix("after:"))
            limit = int(query.get("limit", 100))
            rows = [row for change_id, (visible, row) in self._changes if change_id > after and visible <= now]
            page = rows[:limit]
            last = int(page[-1]["change_id"]) if page else after
            return {"data": page, "meta": {"has_more": len(rows) > limit, "next_cursor": f"after:{last}"}}
        raise ValueError(f"the recording has no answers for {path}")


def follow(recording: Path) -> tuple[list[FeedResult], RecordedNflMeta]:
    """The answers a board's feed would have had, asking the recorded game when due.

    The feed asks at the moments scoreboards actually arrived, so the answers keep the recording's own
    timing: its pace, its jitter, and its gaps (no asks while the halftime hold ran).
    """
    clock = FakeClock(datetime(2000, 1, 1, tzinfo=timezone.utc))
    server = RecordedNflMeta(recording, clock)
    clock.set(server.first)
    feed = GameFeed(NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=server), server.game_id)
    results: list[FeedResult] = []
    due: datetime | None = None
    for arrived in server.arrivals:
        clock.advance(arrived - clock.now())
        if due is None or arrived >= due - _EARLY:
            results.append(feed.ask(clock.now()))
            due = results[-1].next_ask
    return results, server


class Minimizer:
    """Strips NFLMeta's words and ids from answers, keeping what a board decides on."""

    def __init__(self) -> None:
        self._keys: dict[str, int] = {}
        self._snaps: dict[tuple[int | None, ...], tuple[int | None, ...]] = {}
        self._counts: dict[tuple[bool, ...], int] = {}
        self._period_began: dict[int, datetime] = {}

    def answer(self, result: FeedResult) -> FeedResult:
        snapshot = result.snapshot
        if snapshot is None:
            return replace(result, error="the feed failed")
        events = tuple(self._event(event) for event in snapshot.events)
        notes = tuple(FeedNote(note.kind, "") for note in snapshot.notes)
        state = self._state(snapshot.state, result.received_at) if snapshot.state is not None else None
        return replace(result, snapshot=replace(snapshot, state=state, events=events, notes=notes))

    def _event(self, event: FootballEvent) -> FootballEvent:
        number = self._keys.setdefault(event.key, len(self._keys) + 1)
        summary = event.summary
        return replace(
            event,
            key=f"e{number}",
            summary=replace(
                summary,
                headline=f"{event.kind.value.upper().replace('_', ' ')} {number}",
                detail=f"detail {number}" if summary.detail else None,
            ),
            players_line=f"players {number}" if event.players_line else None,
        )

    def _state(self, state: FootballGameState, received: datetime) -> FootballGameState:
        down, distance, to_goal = self._snap((state.down, state.distance, state.yards_to_goal))
        began = self._period_began.setdefault(state.period, received)
        clock = (
            0 if state.interval is not None else max(1, _PERIOD_SECONDS - int((received - began).total_seconds() / 4))
        )
        return replace(state, down=down, distance=distance, yards_to_goal=to_goal, clock_seconds=clock)

    def _snap(self, real: tuple[int | None, ...]) -> tuple[int | None, ...]:
        """A stand-in for (down, distance, yards to goal): one per distinct real value, same blanks."""
        if real in self._snaps:
            return self._snaps[real]
        present = tuple(value is not None for value in real)
        number = self._counts.get(present, 0)
        self._counts[present] = number + 1
        stand_in: list[int | None] = []
        for there, radix in zip(present, (4, 20, 99)):
            stand_in.append(1 + number % radix if there else None)
            number //= radix if there else 1
        if number:
            raise ValueError("more distinct snaps than stand-ins")
        self._snaps[real] = tuple(stand_in)
        return self._snaps[real]


def _shape(line: str) -> tuple[str, str, bool]:
    """A decision line without its words: when, what kind, and whether the strip went blank."""
    when, kind, text = line[:9], line[11:16].strip(), line[18:]
    return when, kind, text == "-"


def _write(path: Path, header: dict[str, Any], answers: list[FeedResult], start: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        handle.write(json.dumps(header, separators=(",", ":")) + "\n")
        for result in answers:
            handle.write(json.dumps(answer_record(result, start), separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--name", required=True, help="the game's name in the trace, e.g. 2026-11-15-PIT-CIN")
    parser.add_argument("--out", type=Path, required=True, help="where the minimized trace goes")
    args = parser.parse_args()
    results, server = follow(args.recording)
    first = next(result.snapshot for result in results if result.snapshot is not None)
    start = results[0].received_at
    header = header_record(first.game, name=args.name, start=start)
    full = args.recording / "board-trace.jsonl"
    _write(full, header, results, start)
    minimizer = Minimizer()
    minimized = [minimizer.answer(result) for result in results]
    whole = replay_board(load_trace(full.read_text().splitlines()))
    small = replay_board(load_trace(_lines(header, minimized, start)))
    if [_shape(line) for line in whole] != [_shape(line) for line in small]:
        raise SystemExit("the minimized trace doesn't replay alike; nothing written to --out")
    _write(args.out, header, minimized, start)
    events = sum(len(result.snapshot.events) for result in results if result.snapshot is not None)
    print(
        f"{len(results)} answers ({events} strip entries) from {start:%H:%M:%S} to {results[-1].received_at:%H:%M:%S}"
        f" UTC; {len(whole)} decisions, alike on both traces.\nfull: {full}\nminimized: {args.out}"
        f" ({args.out.stat().st_size // 1024} KB)"
    )
    return 0


def _lines(header: dict[str, Any], answers: list[FeedResult], start: datetime) -> list[str]:
    return [json.dumps(header)] + [json.dumps(answer_record(result, start)) for result in answers]


if __name__ == "__main__":
    raise SystemExit(main())
