"""NFL break detection: typed looks at a game, as NFLMeta's provider gives them, to break and action evidence."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.time import DurationSeconds
from omni.domain.breaks import AgeBasis, BreakConfidence, BreakEvidence, BreakId, BreakKind, OnsetBasis
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone
from omni.events.break_detection import ActionEvidence, detection_age, onset_age
from omni.events.football import FootballEvent, FootballEventKind
from omni.events.nfl_breaks import nfl_break_evidence, nfl_latest_action
from omni.providers.nflmeta import HALFTIME_HOLD, NflLiveSnapshot, NflPhase
from tests.football_fixtures import T, nfl_game

S = timedelta(seconds=1)
GAME = nfl_game()
RECEIVED = ClockReading(T + 2 * S, MonotonicTime(800.0))  # the look arrived 2 s after NFLMeta's observation


def _state(*, period: int = 2, interval: FootballInterval | None = None) -> FootballGameState:
    return FootballGameState(period=period, clock_seconds=112, away_score=7, home_score=3, interval=interval)


def _entry(kind: FootballEventKind, play: int | None, *, written: datetime | None = None) -> FootballEvent:
    summary = PlaySummary(tone=PlayTone.STANDARD, headline=kind.value.upper())
    return FootballEvent(key=f"{play}:{kind.value}", kind=kind, summary=summary, play=play, written_at=written)


def _look(
    *events: FootballEvent,
    state: FootballGameState | None = _state(),
    break_ends_at: datetime | None = None,
    observed: datetime | None = T,
) -> NflLiveSnapshot:
    return NflLiveSnapshot(
        game=GAME,
        phase=NflPhase.IN,
        state=state,
        events=events,
        caught_up=True,
        break_ends_at=break_ends_at,
        observed_at=observed,
        poll_every=DurationSeconds(10),
    )


def _breaks(look: NflLiveSnapshot, received: ClockReading = RECEIVED) -> tuple[BreakEvidence, ...]:
    return nfl_break_evidence(look, received)


# --- stoppage rows --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "evidenced"),
    [
        (FootballEventKind.TIMEOUT, BreakKind.NFL_TEAM_TIMEOUT),
        (FootballEventKind.OFFICIAL_TIMEOUT, BreakKind.NFL_OFFICIAL_TIMEOUT),
        (FootballEventKind.TWO_MINUTE_WARNING, BreakKind.NFL_TWO_MINUTE_WARNING),
    ],
)
def test_a_stoppage_row_is_evidenced_from_when_the_feed_wrote_it(kind: FootballEventKind, evidenced: BreakKind) -> None:
    snap = _entry(FootballEventKind.PLAY, 411, written=T - 40 * S)
    assert _breaks(_look(snap, _entry(kind, 412, written=T - 6 * S))) == (
        BreakEvidence(
            id=BreakId(GAME.id, evidenced, "play 412"),
            received=RECEIVED,
            confidence=BreakConfidence.EXPLICIT,
            onset=T - 6 * S,
            as_of=T,
            onset_basis=OnsetBasis.PUBLISHED,  # the row's write: the stoppage began no later
        ),
    )


def test_an_official_timeout_is_never_taken_for_a_tv_timeout() -> None:
    (evidence,) = _breaks(_look(_entry(FootballEventKind.OFFICIAL_TIMEOUT, 300, written=T - 5 * S)))
    assert evidence.kind is BreakKind.NFL_OFFICIAL_TIMEOUT
    assert evidence.kind is not BreakKind.NFL_TV_TIMEOUT


def test_the_same_row_delivered_again_is_the_same_break() -> None:
    timeout = _entry(FootballEventKind.TIMEOUT, 412, written=T - 6 * S)
    later = ClockReading(RECEIVED.utc + 60 * S, RECEIVED.monotonic + 60 * S)  # a provider catching up after a restart
    (first,), (again,) = _breaks(_look(timeout)), _breaks(_look(timeout, observed=T + 58 * S), later)
    assert first.id == again.id and (first.revision, again.revision) == (0, 0)


def test_a_stoppage_row_with_a_newer_snap_in_the_same_look_is_over() -> None:
    timeout = _entry(FootballEventKind.TIMEOUT, 412, written=T - 50 * S)
    assert _breaks(_look(timeout, _entry(FootballEventKind.PLAY, 413, written=T - 8 * S))) == ()


def test_an_entry_without_a_row_behind_it_is_no_stoppage() -> None:
    assert _breaks(_look(_entry(FootballEventKind.TIMEOUT, None, written=T))) == ()


@pytest.mark.parametrize(
    "kind",
    [
        FootballEventKind.PLAY,
        FootballEventKind.REVIEW,  # a ruling: nothing marks a review in progress
        FootballEventKind.INJURY,  # can't tell an injury stopping play now from one reported after
        FootballEventKind.CONVERSION,  # the try after a score
        FootballEventKind.SCORE,  # points no row accounts for
    ],
)
def test_an_entry_that_lends_nothing_is_not_evidenced(kind: FootballEventKind) -> None:
    assert _breaks(_look(_entry(kind, 500, written=T - 3 * S))) == ()


# --- breaks the scoreboard states -----------------------------------------------------------------------------


def test_a_quarter_break_shows_on_every_look_as_the_same_break_with_no_onset() -> None:
    end_of_first = _state(period=1, interval=FootballInterval.END_OF_QUARTER)
    late_row = _entry(FootballEventKind.TIMEOUT, 120, written=T - 70 * S)  # the quarter's last rows, still landing
    first, again = _breaks(_look(late_row, state=end_of_first)), _breaks(_look(state=end_of_first))
    assert (
        first
        == again
        == (
            BreakEvidence(
                id=BreakId(GAME.id, BreakKind.NFL_QUARTER_BREAK, "end of period 1"),
                received=RECEIVED,
                confidence=BreakConfidence.EXPLICIT,
                as_of=T,
            ),
        )
    )


def test_halftime_runs_from_when_the_provider_counts_it_to_the_end_of_its_hold() -> None:
    (evidence,) = _breaks(_look(state=_state(interval=FootballInterval.HALFTIME), break_ends_at=T + 12 * 60 * S))
    assert evidence.id == BreakId(GAME.id, BreakKind.NFL_HALFTIME, "halftime")
    assert (evidence.onset, evidence.stated) == (T - 60 * S, HALFTIME_HOLD)
    assert evidence.onset is not None and evidence.stated is not None
    assert evidence.onset + evidence.stated == T + 12 * 60 * S
    assert evidence.onset_basis is OnsetBasis.LOCAL_HOLD  # the hold runs on this board's clock


def test_halftime_s_hold_is_aged_on_this_board_s_clock_whatever_the_scoreboard_s_says() -> None:
    """The hold began 60 s before the look's observation by our clock; the look arrived 2 s after: 62 s old. A
    scoreboard that doesn't say when it stood, or whose clock is well off ours, changes nothing."""
    halftime = _state(interval=FootballInterval.HALFTIME)
    for observed in (T, None, T + 10 * 60 * S):
        (evidence,) = _breaks(_look(state=halftime, break_ends_at=T + 12 * 60 * S, observed=observed))
        age = detection_age(evidence, poll_interval=10 * S)
        assert (age.age, age.basis, age.provisional) == (62 * S, AgeBasis.LOCAL_HOLD, False)


def test_halftime_is_timed_by_the_hold_the_provider_was_given() -> None:
    look = _look(state=_state(interval=FootballInterval.HALFTIME), break_ends_at=T + 9 * 60 * S)
    (evidence,) = nfl_break_evidence(look, RECEIVED, halftime_hold=timedelta(minutes=10))
    assert (evidence.onset, evidence.stated) == (T - 60 * S, timedelta(minutes=10))


def test_halftime_that_cant_be_timed_has_no_onset_or_length() -> None:
    """Joined mid-break, or the half's last plays still landing: the hold isn't timed."""
    halftime = _state(interval=FootballInterval.HALFTIME)
    (evidence,) = _breaks(_look(state=halftime, break_ends_at=None))
    assert (evidence.kind, evidence.onset, evidence.stated) == (BreakKind.NFL_HALFTIME, None, None)


def test_before_kickoff_and_after_the_final_there_are_no_breaks() -> None:
    timeout = _entry(FootballEventKind.TIMEOUT, 900, written=T - 3 * S)
    assert _breaks(_look(timeout, state=None)) == ()
    assert _breaks(_look(timeout, state=_state(period=4, interval=FootballInterval.FINAL))) == ()


# --- onsets ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observed", "written", "credible"),
    [
        (T, T - 6 * S, True),
        (None, T - 6 * S, False),  # no observation time: the source's clock can't be checked
        (T + 30 * S, T + 25 * S, True),  # the source's clock runs ahead of ours: its own age is still told
        (T, T + 9 * S, False),  # written after the look that brought it
        (T, None, False),  # the provider couldn't date the row
    ],
)
def test_a_rows_onset_is_given_only_when_credible(
    observed: datetime | None, written: datetime | None, credible: bool
) -> None:
    (evidence,) = _breaks(_look(_entry(FootballEventKind.TIMEOUT, 412, written=written), observed=observed))
    assert evidence.onset == (written if credible else None)


def test_a_stoppage_row_first_seen_long_after_it_was_written_keeps_its_onset_and_age() -> None:
    (evidence,) = _breaks(_look(_entry(FootballEventKind.OFFICIAL_TIMEOUT, 250, written=T - 300 * S)))
    assert evidence.onset == T - 300 * S and onset_age(evidence) == 302 * S


# --- action evidence ------------------------------------------------------------------------------------------


def test_the_newest_action_is_the_newest_snap_a_look_brought() -> None:
    look = _look(
        _entry(FootballEventKind.PLAY, 413, written=T - 30 * S),
        _entry(FootballEventKind.TIMEOUT, 414, written=T - 25 * S),
        _entry(FootballEventKind.PLAY, 415, written=T - 4 * S),
        _entry(FootballEventKind.CONVERSION, 380, written=T - 1 * S),
    )
    assert nfl_latest_action(look, RECEIVED) == ActionEvidence(
        contest=GAME.id, marker="play 415", received=RECEIVED, at=T - 4 * S
    )


def test_a_look_that_brought_no_snap_shows_no_action() -> None:
    assert nfl_latest_action(_look(), RECEIVED) is None
    stoppages = _look(_entry(FootballEventKind.TIMEOUT, 414), _entry(FootballEventKind.INJURY, None))
    assert nfl_latest_action(stoppages, RECEIVED) is None
    assert nfl_latest_action(_look(_entry(FootballEventKind.PLAY, None)), RECEIVED) is None
