"""What NFL's cards read: each game's answers, as they arrived, from whatever source plans the reads.

The NFL pipeline consumes this; the planned NFL source (NFLMeta behind the poll plan and the quota ledger)
provides it. Each answer is taken once, oldest first, so no play the feed delivered between two refreshes is
lost; the newest answer stays readable for the card's state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from omni.core.clock import ClockReading
from omni.domain.contest import TeamGame
from omni.providers.nflmeta import NflLiveSnapshot

__all__ = ["NflFeeds", "NflReading"]


@dataclass(frozen=True, slots=True)
class NflReading:
    """One answer about one NFL game, and when it arrived."""

    snapshot: NflLiveSnapshot
    arrived: ClockReading


class NflFeeds(Protocol):
    """Each NFL game's answers, for the pipeline."""

    def take(self, game: TeamGame) -> list[NflReading]:
        """The answers about ``game`` that arrived since the last take, oldest first; each is taken once."""
        ...  # pragma: no cover

    def latest(self, game: TeamGame) -> NflReading | None:
        """The newest answer about ``game``, taken or not; None before the first."""
        ...  # pragma: no cover
