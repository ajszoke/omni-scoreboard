"""A small synthetic NFLMeta recording, in the recorders' own format, for the miner's tests.

Every text field a row carries holds `PHRASE`, so a test can prove the miner keeps none of the provider's words.
Fields that must parse (a clock, a club, a phase, a time) hold what the provider reads, with the phrase beside
them where the field is free text.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PHRASE = "Velvet Quokka Parade"
T0 = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
GAME = 401220


def stamp(at: float) -> str:
    """``at`` seconds after `T0`, as the recorders stamp an arrival."""
    return (T0 + timedelta(seconds=at)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def change(
    change_id: int,
    number: int,
    *,
    written: float,
    quarter: int,
    play_type: str = "rush",
    revision: int = 1,
    offense: str = "PIT",
    description: str | None = None,
    **flags: Any,
) -> dict[str, Any]:
    """A play-feed change row."""
    return {
        "change_id": change_id,
        "change_type": PHRASE,
        "source_play_id": f"{PHRASE} {number}",
        "revision": revision,
        "changed_at": stamp(written),
        "play": {
            "play_number": number,
            "play_type": play_type,
            "quarter": quarter,
            "posteam_abbr": offense,
            "description": description or f"{PHRASE} ran for a while",
            "drive_result": PHRASE,
            "yards_gained": 4,
            **flags,
        },
    }


def game_row(
    *,
    game_id: int = GAME,
    away: str = "PIT",
    home: str = "CIN",
    phase: str = "in",
    period: int = 1,
    clock: str = "15:00",
    scores: tuple[int, int] = (0, 0),
    detail: str | None = None,
    changed: float | None = None,
    injuries: tuple[dict[str, Any], ...] = (),
    situation: bool = True,
) -> dict[str, Any]:
    """A scoreboard row for one game."""
    row: dict[str, Any] = {
        "game_id": game_id,
        "kickoff_at": stamp(60),
        "week": 11,
        "venue": PHRASE,
        "away_team": {"abbr": away, "score": scores[0], "name": PHRASE},
        "home_team": {"abbr": home, "score": scores[1], "name": PHRASE},
        "phase": phase,
        "period": period,
        "clock": clock,
        "status_detail": detail if detail is not None else f"{clock} - {PHRASE}",
        "situation": (
            {"possession_abbr": away, "down": 1, "distance": 10, "yardline_100": 75, "text": f"{clock} {PHRASE}"}
            if situation
            else None
        ),
        "play_sync": {"status": "awaiting_play", "last_play_id": PHRASE},
        "observed_at": stamp(0),
        "injuries": list(injuries),
    }
    if changed is not None:
        row["changed_at"] = stamp(changed)
    return row


def injury(reported: float, state: str = "questionable_to_return") -> dict[str, Any]:
    return {
        "player_name": PHRASE,
        "team_abbr": "CIN",
        "injury": PHRASE,
        "state": state,
        "reported_at": stamp(reported),
    }


class Recording:
    """Writes a recording folder line by line, as the board's ``--record`` (or the recorder tool) does."""

    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder

    def _line(self, name: str, at: float, body: Any, *, billed: Any, query: dict[str, Any] | None) -> None:
        record: dict[str, Any] = {"received_at": stamp(at), "rows": billed, "body": body}
        if query is not None:  # the board's recording; the recorder tool keeps no query
            record["quota_remaining"] = "4000"
            record["query"] = query
        with (self.folder / name).open("a", encoding="utf-8") as out:
            out.write(json.dumps(record) + "\n")

    def raw(self, name: str, text: str) -> None:
        with (self.folder / name).open("a", encoding="utf-8") as out:
            out.write(text + "\n")

    def scoreboard(
        self,
        at: float,
        *rows: dict[str, Any],
        billed: Any = "1",
        poll: Any = 10,
        query: dict[str, Any] | None = None,
        recorder: bool = False,
    ) -> None:
        body = {"data": list(rows), "meta": {"recommended_poll_seconds": poll, "note": PHRASE}}
        asked = None if recorder else (query if query is not None else {"game_id": GAME})
        self._line("live-scores.jsonl", at, body, billed=billed, query=asked)

    def plays(
        self,
        at: float,
        *rows: dict[str, Any],
        billed: Any = None,
        more: bool = False,
        cursor: str | None = f"{PHRASE} cursor",
        next_cursor: str | None = f"{PHRASE} next",
        recorder: bool = False,
    ) -> None:
        body = {"data": list(rows), "meta": {"has_more": more, "next_cursor": next_cursor}}
        query = None if recorder else {"limit": 100, **({"cursor": cursor} if cursor is not None else {})}
        self._line("play-changes.jsonl", at, body, billed=str(len(rows)) if billed is None else billed, query=query)
