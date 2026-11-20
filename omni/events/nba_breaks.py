"""NBA break detection: one game's reads, as its feed deriver tells them, to break evidence for the director.

BALLDONTLIE has no play-by-play and dates nothing it sends, so an NBA break shows only as a change between reads
(`omni.events.basketball_feed`), and `NbaBreaks` keeps one game's from read to read:

- **A timeout** is the deriver's `TimeoutOnset`: one side's count down by one, coherently. The first and second of a
  regulation quarter are the long TV timeouts (`BreakKind.NBA_TV_TIMEOUT`); a later one in the quarter, and every
  overtime one, short (`BreakKind.NBA_TEAM_TIMEOUT`). Its onset is the one the deriver infers, the arrival of the first
  read that showed the clock stopped where the timeout came, on this board's clock (`OnsetBasis.LOCAL_READ`). It ends
  at the deriver's `TimeoutEnd`, the first read that showed the clock running again: the same break, marked over, so
  its lease ends as the clock runs. A won challenge begins as a timeout and is one until the clock runs.
- **END Q1 and END Q3** are the slate's end of the first or third period (`BreakKind.NBA_QUARTER_BREAK`); **halftime**
  its halftime or the end of the second period (`BreakKind.NBA_HALFTIME`); and **before overtime** the end of the fourth
  period or of an overtime with the score tied (`BreakKind.NBA_BEFORE_OVERTIME`). An end with the score apart is the
  final coming, and lends nothing; a ``:00`` reading alone never declares one. A period break shows on every slate read
  while it stands, and its onset is unknown (it began between two reads), so it's aged by one poll interval. It ends at
  the first slate read that shows play on again, or the final.
- **Nothing else** stops the game as far as the board can tell: a decrement at a period's full clock or in a break (the
  break's own ceiling holds), a drop the league's caps make, free throws, fouls and endgame inbounds lend nothing.

Every piece of evidence carries when the read that showed it arrived, and none the source's own time.
"""

from __future__ import annotations

from dataclasses import replace

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import HomeAway
from omni.core.ids import LeagueScopedId
from omni.domain.basketball import REGULATION_PERIODS, BasketballGameState, BasketballPhase, TimeoutLength
from omni.domain.basketball_reads import BasketballRead, SlateRead
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind, OnsetBasis
from omni.events.basketball_feed import FeedUpdate, TimeoutEnd, TimeoutOnset

__all__ = ["NbaBreaks"]

_TIMEOUTS = {TimeoutLength.LONG: BreakKind.NBA_TV_TIMEOUT, TimeoutLength.SHORT: BreakKind.NBA_TEAM_TIMEOUT}
_HALFTIME_PERIOD = 2


class NbaBreaks:
    """One game's breaks, read by read: the period break standing, and the timeouts on."""

    def __init__(self, game: LeagueScopedId) -> None:
        self._game = game
        self._period: BreakEvidence | None = None  # the period break standing, as first seen
        self._timeouts: dict[tuple[HomeAway, MonotonicTime], BreakEvidence] = {}  # each timeout on, by side and onset

    def read(self, read: BasketballRead, update: FeedUpdate, received: ClockReading) -> tuple[BreakEvidence, ...]:
        """The break evidence one read shows, as it arrived at ``received``: the timeouts it began or ended, and the
        period break it stands in or ended."""
        evidence: list[BreakEvidence] = []
        for timeout in update.timeouts:
            if isinstance(timeout, TimeoutOnset):
                begun = self._timeout(timeout, received)
                self._timeouts[(timeout.side, timeout.onset)] = begun
                evidence.append(begun)
            else:
                evidence += self._ended(timeout, received)
        if isinstance(read, SlateRead):
            evidence += self._period_break(read.state, received)
        return tuple(evidence)

    def _timeout(self, timeout: TimeoutOnset, received: ClockReading) -> BreakEvidence:
        """A timeout's evidence: its kind by its length, placed by its period, clock and side, from its inferred
        onset on this board's clock."""
        clock = timeout.clock.text if timeout.clock is not None else "-"
        marker = f"timeout period {timeout.period} {clock} {timeout.side.value}"
        onset = received.utc - received.monotonic.since(timeout.onset)
        return BreakEvidence(
            id=BreakId(self._game, _TIMEOUTS[timeout.length], marker),
            received=received,
            confidence=BreakConfidence.INFERRED,
            onset=onset,
            onset_basis=OnsetBasis.LOCAL_READ,
        )

    def _ended(self, end: TimeoutEnd, received: ClockReading) -> list[BreakEvidence]:
        """A timeout over: its evidence again, marked over."""
        begun = self._timeouts.pop((end.side, end.onset), None)
        if begun is None:
            return []  # one begun before this board's reads: nothing was lent
        return [replace(begun, received=received, ended=True)]

    def _period_break(self, state: BasketballGameState, received: ClockReading) -> list[BreakEvidence]:
        """The period break a slate read stands in, or the end of the one standing once play is on again."""
        kind = _period_kind(state)
        standing = self._period
        if kind is None:
            self._period = None
            if standing is None:
                return []
            return [replace(standing, received=received, ended=True)]
        marker = "halftime" if kind is BreakKind.NBA_HALFTIME else f"end of period {state.period}"
        evidence = BreakEvidence(
            id=BreakId(self._game, kind, marker), received=received, confidence=BreakConfidence.EXPLICIT
        )
        ended = []
        if standing is not None and standing.id != evidence.id:
            ended = [replace(standing, received=received, ended=True)]
        if standing is None or standing.id != evidence.id:
            self._period = evidence
        return [*ended, evidence]


def _period_kind(state: BasketballGameState) -> BreakKind | None:
    """The break between periods a state shows; None in play, at the final, or at an end with the score apart."""
    if state.phase is BasketballPhase.HALFTIME:
        return BreakKind.NBA_HALFTIME
    if state.phase is not BasketballPhase.END_OF_PERIOD:
        return None
    if state.period == _HALFTIME_PERIOD:
        return BreakKind.NBA_HALFTIME
    if state.period < REGULATION_PERIODS:
        return BreakKind.NBA_QUARTER_BREAK
    return BreakKind.NBA_BEFORE_OVERTIME if state.away.score == state.home.score else None
