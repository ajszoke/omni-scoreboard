"""What NFL's cards read: each game's answers, as they arrived, from whatever source plans the reads.

The NFL pipeline consumes this; the planned NFL source (NFLMeta behind the poll plan and the quota ledger)
provides it. Each answer is taken once, oldest first, so no play the feed delivered between two refreshes is
lost; the newest answer stays readable for the card's state. Beside them it hands on a game's win probability as it
stood at a moment, None when nothing is read for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from omni.app.pipeline import MeterReading
from omni.core.clock import ClockReading
from omni.domain.contest import TeamGame
from omni.providers.nflmeta import NflLiveSnapshot

__all__ = ["NflFeeds", "NflReading"]


@dataclass(frozen=True, slots=True)
class NflReading:
    """One answer about one NFL game, and when it arrived.

    ``current_for`` is how long it counts as current from its arrival: two of the cadence its read is asked at,
    which the source knows and the answer may not (a game read with others every 45 s, say). None leaves it to
    the reader, which takes two of the cadence the answer itself recommends.
    """

    snapshot: NflLiveSnapshot
    arrived: ClockReading
    current_for: timedelta | None = None


class NflFeeds(Protocol):
    """Each NFL game's answers, and its win probability, for the pipeline."""

    def take(self, game: TeamGame) -> list[NflReading]:
        """The answers about ``game`` that arrived since the last take, oldest first; each is taken once."""
        ...  # pragma: no cover

    def latest(self, game: TeamGame) -> NflReading | None:
        """The newest answer about ``game``, taken or not; None before the first."""
        ...  # pragma: no cover

    def win_probability(self, game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        """``game``'s win probability as it stood at ``as_of``: the newest reading that arrived by then; None without
        one."""
        ...  # pragma: no cover
