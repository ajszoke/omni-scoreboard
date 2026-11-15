"""What the poller records: one reading per successful poll, kept in the table and the archive alike.

A reading's body is JSON-ready and carries the matchup, so an archived line can be read long after the
schedule that produced it is gone. Raw JSON stays at this boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from omni.core.enum import StrEnumMixin
from omni.domain.baseball import BettingLine, GameLines
from omni.domain.contest import TeamGame
from omni.providers.polymarket_us import MarketSample

__all__ = ["QuoteSource", "Reading", "ReadingKind", "format_instant", "lines_reading", "win_reading"]

_SCHEMA_VERSION = 1


class ReadingKind(StrEnumMixin, str, Enum):
    """What a reading is of: the live market's price, or the pregame betting lines."""

    WIN = "win"
    LINES = "lines"


class QuoteSource(StrEnumMixin, str, Enum):
    """Where a market price came from: the keyed stream, or the public quote cached for 30 seconds."""

    STREAM = "stream"
    REST = "rest"


def format_instant(moment: datetime) -> str:
    """A UTC timestamp in the one form the service writes, to the millisecond."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class Reading:
    """One poll's result for one game."""

    game_id: str
    kind: ReadingKind
    observed_at: datetime
    body: Mapping[str, Any]

    def archive_line(self) -> str:
        """This reading as one self-describing JSON line."""
        return json.dumps(
            {
                "v": _SCHEMA_VERSION,
                "game_id": self.game_id,
                "kind": self.kind.value,
                "observed_at": format_instant(self.observed_at),
                **self.body,
            },
            separators=(",", ":"),
        )


def _matchup(game: TeamGame) -> dict[str, Any]:
    return {
        "league": game.league.value,
        "away": game.away.abbreviation,
        "home": game.home.abbreviation,
        "start": format_instant(game.scheduled_start),
    }


def win_reading(game: TeamGame, sample: MarketSample, observed_at: datetime) -> Reading:
    """A reading of a market's price. Its percentages are null when the market is too thin to trust."""
    source = QuoteSource.STREAM if sample.streamed else QuoteSource.REST
    probability = sample.probability
    return Reading(
        game_id=str(game.id),
        kind=ReadingKind.WIN,
        observed_at=observed_at,
        body={
            **_matchup(game),
            "slug": sample.slug,
            "bid": sample.quote.bid,
            "ask": sample.quote.ask,
            "source": source.value,
            "away_pct": probability.away if probability is not None else None,
            "home_pct": probability.home if probability is not None else None,
        },
    )


def _line(line: BettingLine | None) -> dict[str, Any] | None:
    return asdict(line) if line is not None else None


def lines_reading(game: TeamGame, lines: GameLines, observed_at: datetime) -> Reading:
    """A reading of a game's pregame betting lines."""
    return Reading(
        game_id=str(game.id),
        kind=ReadingKind.LINES,
        observed_at=observed_at,
        body={**_matchup(game), "lines": {"away": _line(lines.away), "home": _line(lines.home)}},
    )
