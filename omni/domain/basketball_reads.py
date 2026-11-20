"""What a basketball feed hands the board, one game at a time, read by read.

A game is read two ways, at different moments. The slate gives the game's state: the phase, the period and clock,
the scores by period, each side's timeouts left and the bonus. The stat rows give every player's box-score line so
far, along with the period and clock those rows carry, which can differ from the slate's at the same moment. A board
learns the plays only from how the lines change between reads, and timeouts and challenges only from how the slate's
counts change. So each read is stamped with when it arrived, and a game's reads are handed on in arrival order.

A player's short name isn't unique (two Thompsons can share a court), so each line travels under the key the feed
numbers that player by. A lines read keeps the rows in the order the feed sent them: when one read holds two plays
and nothing else tells which came first, that order is the last resort.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway
from omni.domain.basketball import BasketballGameState, GameClock, PlayerLine

__all__ = ["BasketballRead", "LinesRead", "PlayerKey", "RosterLine", "SlateRead"]


@dataclass(frozen=True, slots=True, order=True)
class PlayerKey:
    """A player as the feed numbers them: the same in every read of every game."""

    value: int

    def __post_init__(self) -> None:
        if self.value < 1:
            raise ValueError("a player key is a positive number")


@dataclass(frozen=True, slots=True, kw_only=True)
class RosterLine:
    """One player's line in a read: their side, and the key that ties it to the same player's lines in other reads."""

    key: PlayerKey
    side: HomeAway
    line: PlayerLine


@dataclass(frozen=True, slots=True, kw_only=True)
class SlateRead:
    """One game's state as the slate had it when the read arrived."""

    at: MonotonicTime
    state: BasketballGameState


@dataclass(frozen=True, slots=True, kw_only=True)
class LinesRead:
    """One game's player lines as the stat rows had them when the read arrived.

    ``period`` and ``clock`` are the ones the stat rows carry; ``clock`` is None when the rows show no running
    period (at a break, or a final). Every player who has appeared so far is here, each under one key, in the order
    the feed sent the rows.
    """

    at: MonotonicTime
    period: int
    clock: GameClock | None
    lines: tuple[RosterLine, ...]

    def __post_init__(self) -> None:
        if self.period < 1:
            raise ValueError("period must be >= 1")
        keys = [roster.key for roster in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("a player has one line in a read")

    def side(self, which: HomeAway) -> tuple[RosterLine, ...]:
        """One side's lines, in the order the feed sent them."""
        return tuple(roster for roster in self.lines if roster.side is which)


BasketballRead = SlateRead | LinesRead
"""One read of a game, of either kind."""
