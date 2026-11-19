"""A tests-only reader for the recorded BALLDONTLIE games: each fixture game as the reads a board would get.

The slate file holds one row per read; the stats file holds only the rows that changed since the read before, so
each read's rows are laid over the ones before to give every player's full line. A poll that took its stat rows in
more than one page left one record per page, stamped within a few milliseconds of each other: those pages are one
read. The feed sends the rows ordered by their row id, so a read's full lines keep that order.

It parses only what the reads need: the slate's scores, period, clock, timeouts and bonus; each stat row's numbers,
player, side and short name. A slate read from before the tip has no game state, so none is made.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway
from omni.domain.basketball import (
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    GameClock,
    PlayerLine,
)
from omni.domain.basketball_reads import BasketballRead, LinesRead, PlayerKey, RosterLine, SlateRead

__all__ = ["DET_NOP", "GAMES", "HOU_IND", "UTA_LAL", "replay"]

_DIR = Path(__file__).parent / "fixtures" / "providers" / "balldontlie"
UTA_LAL = "nba-2026-11-18-uta-lal"
DET_NOP = "nba-2026-11-18-det-nop"
HOU_IND = "nba-2026-11-18-hou-ind"
GAMES = (UTA_LAL, DET_NOP, HOU_IND)
_PAGES_WITHIN = 0.5  # seconds: one poll's pages land milliseconds apart, and polls seconds apart
_PERIOD_KEYS = ("q1", "q2", "q3", "q4", "ot1", "ot2", "ot3")


def _records(name: str, kind: str) -> list[dict[str, Any]]:
    with gzip.open(_DIR / f"{name}.{kind}.jsonl.gz", "rt", encoding="utf-8") as lines:
        return [json.loads(line) for line in lines]


def _clock(time: str | None) -> GameClock | None:
    """``Q2 3:09`` or ``Q1 :49.4`` as a clock; None for a break's words (``END Q1``, ``Half``, ``Final``)."""
    if time is None or not time.startswith(("Q", "OT")) or " " not in time:
        return None
    reading = time.split(" ", 1)[1]
    minutes, seconds = reading.split(":")
    whole, _, tenths = seconds.partition(".")
    return GameClock.of(int(minutes or 0), int(whole), int(tenths or 0))


def _phase(row: Mapping[str, Any]) -> BasketballPhase:
    time = row["time"] or ""
    if row["status_state"] == "final":
        return BasketballPhase.FINAL
    if time == "Half":
        return BasketballPhase.HALFTIME
    if time.startswith("END"):
        return BasketballPhase.END_OF_PERIOD
    return BasketballPhase.LIVE


def _state(row: Mapping[str, Any]) -> BasketballGameState:
    period = row["period"]
    phase = _phase(row)

    def side(prefix: str) -> BasketballSide:
        periods = tuple(row[f"{prefix}_{key}"] or 0 for key in _PERIOD_KEYS[:period])
        bonus = row[f"{prefix}_in_bonus"]
        return BasketballSide(
            score=row[f"{prefix}_team_score"],
            periods=periods,
            timeouts=row[f"{prefix}_timeouts_remaining"],
            in_bonus=bool(bonus),
        )

    return BasketballGameState(
        phase=phase,
        period=period,
        away=side("visitor"),
        home=side("home"),
        clock=_clock(row["time"]) if phase is BasketballPhase.LIVE else None,
    )


def _slates(name: str) -> Iterator[tuple[float, BasketballRead]]:
    for record in _records(name, "games"):
        row = record["body"]["data"][0]
        if row["status_state"] == "scheduled":
            continue  # before the tip there's no game state to read
        at = record["monotonic"]
        yield at, SlateRead(at=MonotonicTime(at), state=_state(row))


def _line(row: Mapping[str, Any]) -> PlayerLine:
    player = row["player"]
    return PlayerLine(
        name=f"{player['first_name'][0]}.{player['last_name']}",
        points=row["pts"],
        fg_made=row["fgm"],
        fg_attempted=row["fga"],
        threes_made=row["fg3m"],
        threes_attempted=row["fg3a"],
        ft_made=row["ftm"],
        ft_attempted=row["fta"],
        offensive_rebounds=row["oreb"],
        defensive_rebounds=row["dreb"],
        assists=row["ast"],
        steals=row["stl"],
        blocks=row["blk"],
        turnovers=row["turnover"],
        fouls=row["pf"],
        plus_minus=row["plus_minus"],
    )


def _polls(records: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """The records in arrival order, each poll's pages together."""
    poll: list[dict[str, Any]] = []
    for record in records:
        if poll and record["monotonic"] - poll[0]["monotonic"] > _PAGES_WITHIN:
            yield poll
            poll = []
        poll.append(record)
    if poll:
        yield poll


def _lines(name: str, home_team: int) -> Iterator[tuple[float, BasketballRead]]:
    rows: dict[int, tuple[int, RosterLine]] = {}  # by player id: the row's id (the feed's order) and its line
    for records in _polls(_records(name, "stats")):
        at = records[-1]["monotonic"]
        for record in records:
            for row in record["changed"]:
                side = HomeAway.HOME if row["team"]["id"] == home_team else HomeAway.AWAY
                key = PlayerKey(row["player"]["id"])
                rows[key.value] = (row["id"], RosterLine(key=key, side=side, line=_line(row)))
        last = max(records, key=lambda record: record["received_at"])  # the page that saw the game latest
        game = last["changed"][0]["game"]
        ordered = tuple(roster for _, roster in sorted(rows.values(), key=lambda pair: pair[0]))
        yield at, LinesRead(at=MonotonicTime(at), period=game["period"], clock=_clock(game["time"]), lines=ordered)


def replay(name: str) -> tuple[BasketballRead, ...]:
    """One fixture game's reads in arrival order; a slate read before a lines read that arrived at the same time."""
    home_team = _records(name, "games")[0]["body"]["data"][0]["home_team"]["id"]
    merged = [(at, 0, read) for at, read in _slates(name)] + [(at, 1, read) for at, read in _lines(name, home_team)]
    return tuple(read for _, _, read in sorted(merged, key=lambda item: (item[0], item[1])))
