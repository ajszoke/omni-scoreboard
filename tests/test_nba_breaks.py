"""NBA break detection: a timeout, long or short by its order in the quarter, from the deriver's inferred onset to the
read that shows the clock running again; the breaks between periods from the slate's phase, before overtime only when
tied; and nothing from the lines or a timeout this board never saw begin."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import HomeAway, League
from omni.core.ids import LeagueScopedId
from omni.domain.basketball import (
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    GameClock,
    TimeoutLength,
)
from omni.domain.basketball_reads import LinesRead, SlateRead
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind, OnsetBasis
from omni.events.basketball_feed import BasketballFeedDeriver, FeedUpdate, TimeoutEnd
from omni.events.break_detection import detection_age
from omni.domain.breaks import AgeBasis
from omni.events.nba_breaks import NbaBreaks
from omni.providers.balldontlie import BALLDONTLIE_SOURCE

GAME = LeagueScopedId(League.NBA, BALLDONTLIE_SOURCE, "21718141")
T0 = datetime(2026, 11, 19, 3, 30, tzinfo=timezone.utc)
S = timedelta(seconds=1)
A, H = HomeAway.AWAY, HomeAway.HOME


def _state(
    period: int = 1,
    tenths: int | None = 4440,
    timeouts: tuple[int, int] = (7, 7),
    phase: BasketballPhase = BasketballPhase.LIVE,
    scores: tuple[int, int] = (10, 8),
) -> BasketballGameState:
    return BasketballGameState(
        phase=phase,
        period=period,
        away=BasketballSide(score=scores[0], timeouts=timeouts[0]),
        home=BasketballSide(score=scores[1], timeouts=timeouts[1]),
        clock=GameClock(tenths) if tenths is not None else None,
    )


def at(seconds: float) -> ClockReading:
    return ClockReading(T0 + seconds * S, MonotonicTime(seconds))


def watch(*reads: tuple[float, BasketballGameState]) -> list[tuple[BreakEvidence, ...]]:
    """Each slate read, at its second, through a deriver and the detector: the evidence each showed."""
    deriver, breaks = BasketballFeedDeriver(), NbaBreaks(GAME)
    shown = []
    for second, state in reads:
        read = SlateRead(at=MonotonicTime(second), state=state)
        shown.append(breaks.read(read, deriver.read(read), at(second)))
    return shown


def test_a_quarter_s_first_timeout_is_a_tv_timeout_from_its_inferred_onset_until_the_clock_runs() -> None:
    shown = watch(
        (0, _state(tenths=4440)),
        (5, _state(tenths=4430)),  # the clock stops at 7:23 here
        (10, _state(tenths=4430)),
        (20, _state(tenths=4430, timeouts=(6, 7))),  # UTA's count drops: its timeout, onset at 5 s
        (200, _state(tenths=4420, timeouts=(6, 7))),  # the clock runs again
    )
    (begun,) = shown[3]
    assert begun == BreakEvidence(
        id=BreakId(GAME, BreakKind.NBA_TV_TIMEOUT, "timeout period 1 7:23 away"),
        received=at(20),
        confidence=BreakConfidence.INFERRED,
        onset=T0 + 5 * S,
        onset_basis=OnsetBasis.LOCAL_READ,
    )
    age = detection_age(begun, poll_interval=15 * S)
    assert (age.age, age.basis, age.provisional) == (15 * S, AgeBasis.LOCAL_READ, False)
    (ended,) = shown[4]
    assert ended.id == begun.id and ended.ended and ended.received == at(200)
    assert shown[:3] == [(), (), ()]


def test_a_quarter_s_third_timeout_is_a_short_one() -> None:
    reads = [(0.0, _state(tenths=7000))]
    second, tenths, timeouts = 1.0, 7000, [7, 7]
    for side in (0, 1, 0):
        tenths -= 100
        reads.append((second, _state(tenths=tenths, timeouts=(timeouts[0], timeouts[1]))))
        timeouts[side] -= 1
        reads.append((second + 1, _state(tenths=tenths, timeouts=(timeouts[0], timeouts[1]))))
        second += 2
    kinds = [evidence.kind for shown in watch(*reads) for evidence in shown if not evidence.ended]
    assert kinds == [BreakKind.NBA_TV_TIMEOUT, BreakKind.NBA_TV_TIMEOUT, BreakKind.NBA_TEAM_TIMEOUT]


def test_the_breaks_between_periods_come_from_the_slate_and_end_when_play_is_on() -> None:
    end_q1 = _state(1, None, phase=BasketballPhase.END_OF_PERIOD)
    shown = watch((0, _state(1, 100)), (10, end_q1), (20, end_q1), (150, _state(2, 7200)))
    first = BreakEvidence(
        id=BreakId(GAME, BreakKind.NBA_QUARTER_BREAK, "end of period 1"),
        received=at(10),
        confidence=BreakConfidence.EXPLICIT,
    )
    assert shown[1] == (first,) and shown[2][0].id == first.id  # every read while it stands
    (ended,) = shown[3]
    assert ended.id == first.id and ended.ended and ended.received == at(150)


def test_the_end_of_the_second_period_and_halftime_are_one_break() -> None:
    end_q2 = _state(2, None, phase=BasketballPhase.END_OF_PERIOD)
    half = _state(2, None, phase=BasketballPhase.HALFTIME)
    shown = watch((0, _state(2, 50)), (5, end_q2), (10, half), (900, _state(3, 7200)))
    ids = {evidence.id for evidence in (*shown[1], *shown[2])}
    assert ids == {BreakId(GAME, BreakKind.NBA_HALFTIME, "halftime")} and shown[3][0].ended


def test_before_overtime_only_when_tied_and_an_end_with_the_score_apart_lends_nothing() -> None:
    tied = _state(4, None, phase=BasketballPhase.END_OF_PERIOD, scores=(100, 100))
    apart = _state(4, None, phase=BasketballPhase.END_OF_PERIOD, scores=(101, 100))
    (before,) = watch((0, _state(4, 10, scores=(100, 100))), (5, tied))[1]
    assert before.id == BreakId(GAME, BreakKind.NBA_BEFORE_OVERTIME, "end of period 4")
    assert watch((0, _state(4, 10, scores=(101, 100))), (5, apart))[1] == ()
    final = _state(4, None, phase=BasketballPhase.FINAL, scores=(101, 100))
    (over,) = watch((0, _state(4, 10, scores=(100, 100))), (5, tied), (6, final))[2]
    assert over.id == before.id and over.ended


def test_a_break_followed_straight_by_another_ends_the_first() -> None:
    end_q3 = _state(3, None, phase=BasketballPhase.END_OF_PERIOD, scores=(80, 70))
    tied = _state(4, None, phase=BasketballPhase.END_OF_PERIOD, scores=(90, 90))
    shown = watch((0, _state(3, 10, scores=(80, 70))), (5, end_q3), (600, tied))
    assert [(evidence.kind, evidence.ended) for evidence in shown[2]] == [
        (BreakKind.NBA_QUARTER_BREAK, True),
        (BreakKind.NBA_BEFORE_OVERTIME, False),
    ]


def test_the_players_lines_lend_nothing_and_a_timeout_never_seen_beginning_ends_nothing() -> None:
    breaks = NbaBreaks(GAME)
    lines = LinesRead(at=MonotonicTime(1.0), period=1, clock=GameClock(100), lines=())
    end = TimeoutEnd(side=H, onset=MonotonicTime(0.0), ended_at=MonotonicTime(1.0))
    assert breaks.read(lines, FeedUpdate(at=MonotonicTime(1.0), timeouts=(end,)), at(1)) == ()
    assert TimeoutLength.SHORT.value == "short"
