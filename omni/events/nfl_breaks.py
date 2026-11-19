"""NFL break detection: one look at a game from NFLMeta, as the provider types it, to break and action evidence.

A look (`NflLiveSnapshot`) carries the scoreboard's state and only the play-feed entries that are new since the
last look, so the two sources of evidence differ in how often they show:
- **A break the scoreboard states** (a quarter break, halftime) shows on every look while it stands.
- **A stoppage the play feed writes as a row of its own** (a club's timeout, an official timeout, the two-minute
  warning) shows on the look that brings its row, unless a newer snap came with it. Its marker is the row's play
  number ("play 412"), so the same row delivered again (a provider catching up after a restart) is the same break.

**What it detects** (`docs/agent_context/BOARD_POLICY.md` §5):
- **A team timeout:** a ``timeout`` row ("Timeout #2 by CIN at 1:52.").
- **An official timeout:** an ``official_timeout`` row. The feed doesn't say why the officials stopped play, so
  it's never taken for a TV timeout: no feed signal identifies one, and `NFL_TV_TIMEOUT` is never emitted.
- **The two-minute warning:** its own ``two_minute_warning`` row, never inferred from the game clock.
- **A quarter break:** the scoreboard's "End of 1st" (or 3rd, or of regulation before overtime). The scoreboard
  doesn't say when the quarter ended, so its onset is unknown.
- **Halftime:** the scoreboard's halftime, timed by the provider's polling hold. Its onset is when the provider
  counts the break from and its stated length is the hold, so the two end at the snapshot's ``break_ends_at``.
  While the hold isn't timed (the provider joined mid-break, or the half's last plays are still landing), both
  are unknown.

**What it can't detect:**
- **An injury timeout:** the scoreboard's injury list can't tell an injury that stopped play now from one
  reported after play resumed, and the typed injury entry carries no time.
- **A review in progress:** NFLMeta writes only the ruling, into the reviewed play's row after the fact. A ruling
  lends nothing.

A row's onset is when the feed wrote it, when that's credible (`credible_onset`, against the scoreboard's
observation time). The provider announces no entry for a corrected stoppage row, and an entry doesn't carry its
row's revision, so every piece of evidence here is revision 0.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from omni.core.clock import ClockReading
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.domain.contest import TeamGame
from omni.domain.football import FootballInterval
from omni.events.break_detection import ActionEvidence, credible_onset
from omni.events.football import FootballEvent, FootballEventKind
from omni.providers.nflmeta import HALFTIME_HOLD, NflLiveSnapshot

__all__ = ["nfl_break_evidence", "nfl_latest_action"]

# The play feed's stoppage rows -> the breaks they evidence.
_ROW_BREAKS = {
    FootballEventKind.TIMEOUT: BreakKind.NFL_TEAM_TIMEOUT,
    FootballEventKind.OFFICIAL_TIMEOUT: BreakKind.NFL_OFFICIAL_TIMEOUT,
    FootballEventKind.TWO_MINUTE_WARNING: BreakKind.NFL_TWO_MINUTE_WARNING,
}


def nfl_break_evidence(
    snapshot: NflLiveSnapshot, received: ClockReading, *, halftime_hold: timedelta = HALFTIME_HOLD
) -> tuple[BreakEvidence, ...]:
    """The breaks one look at a game shows, as evidence that arrived at ``received``.

    ``halftime_hold`` is the hold the provider was given, which times halftime. Empty before kickoff, after the
    final, and in action with nothing stopping it.
    """
    state, game = snapshot.state, snapshot.game
    if state is None or state.interval is FootballInterval.FINAL:
        return ()
    if state.interval is FootballInterval.HALFTIME:
        return (_halftime(snapshot, received, halftime_hold),)
    if state.interval is FootballInterval.END_OF_QUARTER:
        return (_evidence(game, BreakKind.NFL_QUARTER_BREAK, f"end of period {state.period}", received),)
    newest_snap = _newest_snap(snapshot.events)
    return tuple(
        _evidence(
            game,
            _ROW_BREAKS[event.kind],
            f"play {event.play}",
            received,
            onset=credible_onset(event.written_at, as_of=snapshot.observed_at, received=received),
        )
        for event in snapshot.events
        if event.kind in _ROW_BREAKS and event.play is not None and (newest_snap is None or event.play > newest_snap)
    )


def nfl_latest_action(snapshot: NflLiveSnapshot, received: ClockReading) -> ActionEvidence | None:
    """The newest snap one look brought, or None when it brought none (a look carries only what's new)."""
    snaps = [event for event in snapshot.events if _is_snap(event)]
    if not snaps:
        return None
    newest = max(snaps, key=lambda event: event.play or 0)
    return ActionEvidence(
        contest=snapshot.game.id, marker=f"play {newest.play}", received=received, at=newest.written_at
    )


def _is_snap(event: FootballEvent) -> bool:
    """A play the feed wrote a row for: a snap, a kick, a flag before the snap. Not a stoppage, a ruling on an
    earlier play, a try (written into its touchdown's row), an injury, or points no row accounts for."""
    return event.kind is FootballEventKind.PLAY and event.play is not None


def _newest_snap(events: Iterable[FootballEvent]) -> int | None:
    return max((event.play for event in events if _is_snap(event) and event.play is not None), default=None)


def _halftime(snapshot: NflLiveSnapshot, received: ClockReading, hold: timedelta) -> BreakEvidence:
    """Halftime, from when the provider counts it to the end of its hold; both unknown while it isn't timed."""
    ends = snapshot.break_ends_at
    onset = credible_onset(ends - hold, as_of=snapshot.observed_at, received=received) if ends is not None else None
    stated = hold if onset is not None else None
    return _evidence(snapshot.game, BreakKind.NFL_HALFTIME, "halftime", received, onset=onset, stated=stated)


def _evidence(
    game: TeamGame,
    kind: BreakKind,
    marker: str,
    received: ClockReading,
    *,
    onset: datetime | None = None,
    stated: timedelta | None = None,
) -> BreakEvidence:
    """Evidence the feed marks outright: every NFL break here is read off a row or a status that names it."""
    return BreakEvidence(
        id=BreakId(game.id, kind, marker),
        received=received,
        confidence=BreakConfidence.EXPLICIT,
        onset=onset,
        stated=stated,
    )
