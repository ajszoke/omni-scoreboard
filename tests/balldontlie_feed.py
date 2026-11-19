"""Synthetic BALLDONTLIE answers for the provider's tests: slate rows, stat rows, and a fetch that plays them back.

Every value is made up; the shapes are the feed's, as the recorded fixtures have them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from omni.core.clock import FakeClock
from omni.providers.balldontlie import BdlAnswer, BdlQuery

T0 = datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)
GAME = 21718141
AWAY_ID, HOME_ID = 29, 14  # UTA at LAL, as the feed numbers them
_SECOND = timedelta(seconds=1)
RATE = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "41", "x-ratelimit-reset": "1795058002"}


def team(team_id: int, abbreviation: str, conference: str = "West", division: str = "Pacific") -> dict[str, Any]:
    return {"id": team_id, "abbreviation": abbreviation, "conference": conference, "division": division}


def slate_row(
    *,
    game_id: int = GAME,
    state: str = "in_progress",
    status: str = "3rd Qtr",
    period: Any = 3,
    time: Any = "Q3 6:07",
    scores: tuple[int, int] = (70, 66),
    away_periods: Sequence[int | None] = (29, 27, 14, 0),
    home_periods: Sequence[int | None] = (34, 20, 12, 0),
    away_overtime: Sequence[int | None] = (None, None, None),
    home_overtime: Sequence[int | None] = (None, None, None),
    timeouts: tuple[Any, Any] = (4, 5),
    bonus: tuple[Any, Any] = (False, True),
    away: Any = None,
    home: Any = None,
    **changes: Any,
) -> dict[str, Any]:
    """One `/v1/games` row; by default UTA at LAL, under way in the third quarter."""
    row: dict[str, Any] = {
        "id": game_id,
        "date": "2026-11-18",
        "season": 2026,
        "status": status,
        "status_state": state,
        "period": period,
        "time": time,
        "postseason": False,
        "postponed": False,
        "visitor_team_score": scores[0],
        "home_team_score": scores[1],
        "datetime": "2026-11-19T03:00:00.000Z",
        "visitor_timeouts_remaining": timeouts[0],
        "home_timeouts_remaining": timeouts[1],
        "visitor_in_bonus": bonus[0],
        "home_in_bonus": bonus[1],
        "ist_stage": None,
        "visitor_team": away if away is not None else team(AWAY_ID, "UTA", division="Northwest"),
        "home_team": home if home is not None else team(HOME_ID, "LAL"),
    }
    for prefix, quarters, overtime in (("visitor", away_periods, away_overtime), ("home", home_periods, home_overtime)):
        for n, points in enumerate(quarters, start=1):
            row[f"{prefix}_q{n}"] = points
        for n, points in enumerate(overtime, start=1):
            row[f"{prefix}_ot{n}"] = points
    row.update(changes)
    return row


def scheduled_row(**changes: Any) -> dict[str, Any]:
    """A row before the tip: the tip as its status, period 0, and nothing else filled."""
    fields: dict[str, Any] = {
        "state": "scheduled",
        "status": "2026-11-19T03:00:00Z",
        "period": 0,
        "time": None,
        "scores": (0, 0),
        "away_periods": (None,) * 4,
        "home_periods": (None,) * 4,
        "timeouts": (None, None),
        "bonus": (None, None),
    }
    fields.update(changes)
    return slate_row(**fields)


def final_row(**changes: Any) -> dict[str, Any]:
    """UTA's 109-97 win at LAL, final."""
    fields: dict[str, Any] = {
        "state": "final",
        "status": "Final",
        "period": 4,
        "time": "Final",
        "scores": (109, 97),
        "away_periods": (29, 27, 22, 31),
        "home_periods": (34, 20, 22, 21),
        "timeouts": (2, 1),
        "bonus": (True, False),
    }
    fields.update(changes)
    return slate_row(**fields)


def body(*rows: Any, cursor: Any = None) -> str:
    """A list answer's body: the rows, and the next page's cursor when there is one."""
    meta: dict[str, Any] = {"per_page": 100}
    if cursor is not None:
        meta["next_cursor"] = cursor
    return json.dumps({"data": list(rows), "meta": meta})


def ok(*rows: Any, cursor: Any = None, headers: dict[str, str] | None = None) -> BdlAnswer:
    return BdlAnswer(200, body(*rows, cursor=cursor), RATE if headers is None else headers)


@dataclass(slots=True)
class ScriptedFetch:
    """Answers each request with the next scripted answer (raising it, when it's an exception), and keeps every
    request it was asked. Each answer moves the clock on a second, as a request takes time."""

    answers: list[BdlAnswer | Exception]
    clock: FakeClock = field(default_factory=lambda: FakeClock(T0))
    asked: list[tuple[str, BdlQuery]] = field(default_factory=list)

    def __call__(self, path: str, query: BdlQuery) -> BdlAnswer:
        self.asked.append((path, query))
        self.clock.advance(_SECOND)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
