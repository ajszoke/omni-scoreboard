"""MLB break detection: one answer from StatsAPI's live feed, as the provider types it, to break and action evidence.

Both functions are pure: the same answer gives the same evidence, so a re-read of a break gives the same
`BreakId` and renews nothing. Each piece of evidence carries the answer's arrival (``received``); the lease rules
keep the first.

**What it detects** (`docs/agent_context/BOARD_POLICY.md` §5):
- **An inning break:** the linescore's Middle or End. Its marker is the half that just ended ("top 5 middle"),
  its stated length the game's scheduled inning break when the provider has it, and its onset the end of that
  half's last plate appearance.
- **A mound visit, a pitching change, an injury delay:** an event of that type in the newest plate appearance
  with no pitch since. Its marker is the event's place ("at-bat 41 event 2"), its onset the event's start. One
  first seen long after it happened keeps its old onset, so its age refuses it a lease.
- **A replay review:** in progress while ``reviewDetails.inProgress`` says so; once ruled, the same break with
  its end marked, which ends a lease and opens none. The feed doesn't time a review, so its onset is unknown.
- **A rain delay or a suspension:** the feed's (else the schedule's) status, as `MLB_WEATHER`. While it stands
  nothing else is read: the game isn't paused for an inning break or a visit, it's stopped.

A pause inside an inning break (a pitching change between halves) isn't evidenced on its own: the inning break
covers it, and §5 says it doesn't extend it. What §5 says lends nothing (a ball-strike challenge, a batter's
timeout, a pinch hitter, a pickoff, a step-off, the automatic runner) isn't typed by the provider at all.

An onset is given only when the feed's time for it is credible (`credible_onset`). The feed marks no correction
of a stoppage, so every piece of evidence here is revision 0.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omni.core.clock import ClockReading
from omni.core.enum import GameStatus
from omni.domain.baseball import BaseballStoppageKind, InningPhase
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.events.break_detection import ActionEvidence, credible_onset

__all__ = ["mlb_break_evidence", "mlb_latest_action"]

_STOPPED = frozenset({GameStatus.DELAYED, GameStatus.SUSPENDED})
# A break between halves -> the half it follows: the middle of an inning follows its top, the end its bottom.
_ENDED_HALF = {InningPhase.MIDDLE: InningPhase.TOP, InningPhase.END: InningPhase.BOTTOM}
_STOPPAGE_KINDS = {
    BaseballStoppageKind.MOUND_VISIT: BreakKind.MLB_MOUND_VISIT,
    BaseballStoppageKind.PITCHING_CHANGE: BreakKind.MLB_PITCHING_CHANGE,
    BaseballStoppageKind.INJURY: BreakKind.MLB_STOPPAGE,
}


def mlb_break_evidence(feed: LiveBaseballFeed, game: TeamGame, received: ClockReading) -> tuple[BreakEvidence, ...]:
    """The breaks one answer about ``game`` shows, as evidence that arrived at ``received``.

    Empty for a game that's final or in action with nothing stopping it.
    """
    state = feed.state
    status = feed.status if feed.status is not None else game.status
    if status in _STOPPED:
        marker = f"{status.value} {state.phase.value} {state.inning}"
        return (_evidence(game, BreakKind.MLB_WEATHER, marker, received),)
    if status is GameStatus.FINAL:
        return ()
    if state.phase.is_break:
        return (_inning_break(feed, game, received),)
    signals = state.break_signals
    found = [
        _evidence(
            game,
            _STOPPAGE_KINDS[stoppage.kind],
            f"at-bat {stoppage.at_bat} event {stoppage.event}",
            received,
            onset=credible_onset(stoppage.began, as_of=feed.as_of, received=received),
        )
        for stoppage in signals.stoppages
        if not stoppage.resumed
    ]
    found += [
        _evidence(
            game,
            BreakKind.MLB_REVIEW,
            f"at-bat {review.at_bat}" + (f" event {review.event}" if review.event is not None else ""),
            received,
            ended=not review.in_progress,
        )
        for review in signals.reviews
    ]
    return tuple(found)


def mlb_latest_action(feed: LiveBaseballFeed, game: TeamGame, received: ClockReading) -> ActionEvidence | None:
    """The newest action one answer about ``game`` shows (a pitch, or a plate appearance's end), or None."""
    action = feed.state.break_signals.last_action
    if action is None:
        return None
    marker = f"at-bat {action.at_bat} " + (f"pitch {action.pitch}" if action.pitch is not None else "end")
    return ActionEvidence(contest=game.id, marker=marker, received=received, at=action.at)


def _inning_break(feed: LiveBaseballFeed, game: TeamGame, received: ClockReading) -> BreakEvidence:
    """The break between halves the linescore shows; it began when the half's last plate appearance ended."""
    state = feed.state
    half = _ENDED_HALF[state.phase]
    last = state.break_signals.last_finished
    ended = last.ended if last is not None and (last.inning, last.phase) == (state.inning, half) else None
    return _evidence(
        game,
        BreakKind.MLB_INNING_BREAK,
        f"{half.value} {state.inning} {state.phase.value}",
        received,
        onset=credible_onset(ended, as_of=feed.as_of, received=received),
        stated=state.break_signals.inning_break_length,
    )


def _evidence(
    game: TeamGame,
    kind: BreakKind,
    marker: str,
    received: ClockReading,
    *,
    onset: datetime | None = None,
    stated: timedelta | None = None,
    ended: bool = False,
) -> BreakEvidence:
    """Evidence the feed marks outright: every MLB break is read off a field that names it."""
    return BreakEvidence(
        id=BreakId(game.id, kind, marker),
        received=received,
        confidence=BreakConfidence.EXPLICIT,
        onset=onset,
        stated=stated,
        ended=ended,
    )
