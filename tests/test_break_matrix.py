"""The break capability matrix, walked: every kind it says is detected has a raw-to-evidence example here, with the
matrix's confidence and onset; and the hard cases, end to end."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from omni.core.clock import ClockReading
from omni.core.enum import League
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.events.break_detection import BREAK_CAPABILITIES, BreakCapability, onset_age
from omni.events.mlb_breaks import mlb_break_evidence
from tests.mlb_break_feeds import RECEIVED, SOURCE, T, action, answer, game, pitch, play, read

S = timedelta(seconds=1)
_MLB_GAME = LeagueScopedId(League.MLB, SOURCE, "700001")
_EXPLICIT = BreakConfidence.EXPLICIT


def _mlb(raw: dict[str, Any], *, inning_break_length: int | None = None) -> Callable[[], tuple[BreakEvidence, ...]]:
    """A synthetic StatsAPI answer, read through the provider and the MLB detector."""
    return lambda: mlb_break_evidence(read(raw, inning_break_length=inning_break_length), game(), RECEIVED)


def _stopped(event_type: str) -> dict[str, Any]:
    """The plate appearance under way, its last event a stoppage logged 8 s before the answer."""
    return answer(plays=(play(41, complete=False, events=(pitch(), action(event_type, at=T - 8 * S))),))


@dataclass(frozen=True, slots=True)
class _Example:
    """A raw answer, and the evidence the detectors make of it."""

    detect: Callable[[], tuple[BreakEvidence, ...]]
    expected: BreakEvidence


def _expect(
    kind: BreakKind, marker: str, *, onset: datetime | None = None, stated: timedelta | None = None
) -> BreakEvidence:
    return BreakEvidence(
        id=BreakId(_MLB_GAME, kind, marker), received=RECEIVED, confidence=_EXPLICIT, onset=onset, stated=stated
    )


_EXAMPLES: dict[BreakKind, _Example] = {
    # linescore.inningState "Middle" in the 5th; the top's last plate appearance ended at T - 20 s; the schedule
    # states a 120 s inning break.
    BreakKind.MLB_INNING_BREAK: _Example(
        _mlb(
            answer(inning=5, inning_state="Middle", plays=(play(39, ended=T - 20 * S),)),
            inning_break_length=120,
        ),
        _expect(BreakKind.MLB_INNING_BREAK, "top 5 middle", onset=T - 20 * S, stated=timedelta(seconds=120)),
    ),
    # playEvents[1].details.eventType "mound_visit" at T - 8 s, with no pitch since.
    BreakKind.MLB_MOUND_VISIT: _Example(
        _mlb(_stopped("mound_visit")), _expect(BreakKind.MLB_MOUND_VISIT, "at-bat 41 event 1", onset=T - 8 * S)
    ),
    BreakKind.MLB_PITCHING_CHANGE: _Example(
        _mlb(_stopped("pitching_substitution")),
        _expect(BreakKind.MLB_PITCHING_CHANGE, "at-bat 41 event 1", onset=T - 8 * S),
    ),
    BreakKind.MLB_STOPPAGE: _Example(
        _mlb(_stopped("injury")), _expect(BreakKind.MLB_STOPPAGE, "at-bat 41 event 1", onset=T - 8 * S)
    ),
    # reviewDetails {"inProgress": true} on the plate appearance just finished, while the next batter is up.
    BreakKind.MLB_REVIEW: _Example(
        _mlb(answer(plays=(play(40, review={"inProgress": True}), play(41, complete=False)))),
        _expect(BreakKind.MLB_REVIEW, "at-bat 40"),
    ),
    # gameData.status.detailedState "Delayed: Rain" in the top of the 7th.
    BreakKind.MLB_WEATHER: _Example(
        _mlb(answer(inning=7, plays=(play(50, inning=7, complete=False),), status="Delayed: Rain")),
        _expect(BreakKind.MLB_WEATHER, "delayed top 7"),
    ),
}


def _detected() -> list[BreakCapability]:
    return [row for row in BREAK_CAPABILITIES.values() if row.detected]


def test_every_break_kind_has_one_row_and_one_saying_why_when_it_isnt_detected() -> None:
    assert set(BREAK_CAPABILITIES) == set(BreakKind)
    assert all(row.kind is kind and row.signal for kind, row in BREAK_CAPABILITIES.items())


def test_every_kind_the_matrix_says_is_detected_has_an_example() -> None:
    assert set(_EXAMPLES) == {row.kind for row in _detected()}


@pytest.mark.parametrize("row", _detected(), ids=lambda row: row.kind.name)
def test_a_detected_kinds_example_becomes_its_evidence_with_the_matrixs_confidence_and_onset(
    row: BreakCapability,
) -> None:
    example = _EXAMPLES[row.kind]
    assert example.detect() == (example.expected,)
    assert example.expected.confidence is row.confidence
    assert (example.expected.onset is not None) is (row.onset is not None)


def test_a_capability_row_can_name_an_onset_only_for_a_kind_that_is_detected() -> None:
    with pytest.raises(ValueError, match="no onset"):
        BreakCapability(kind=BreakKind.NFL_TV_TIMEOUT, confidence=None, signal="no feed says", onset="never")


# --- the hard cases -------------------------------------------------------------------------------------------


def test_an_active_mlb_review_is_a_break_and_its_ruling_is_the_same_break_ended() -> None:
    def review(in_progress: bool) -> BreakEvidence:
        (evidence,) = _mlb(answer(plays=(play(40, review={"inProgress": in_progress}), play(41, complete=False))))()
        return evidence

    active, ruling = review(True), review(False)
    assert active.id == ruling.id and (active.ended, ruling.ended) == (False, True)


def test_a_backfilled_mound_visit_keeps_its_onset_and_age_so_the_lease_rules_can_refuse_it() -> None:
    raw = answer(plays=(play(41, complete=False, events=(action("mound_visit", at=T - 4 * 60 * S),)),))
    (evidence,) = _mlb(raw)()
    assert evidence.onset == T - 4 * 60 * S and onset_age(evidence) == 4 * 60 * S + 2 * S


def test_a_repeated_same_half_inning_marker_is_the_same_break() -> None:
    def middle(received: ClockReading, *plays: dict[str, Any]) -> BreakEvidence:
        raw = answer(inning=5, inning_state="Middle", plays=(play(39, ended=T - 20 * S), *plays))
        (evidence,) = mlb_break_evidence(read(raw), game(), received)
        return evidence

    later = ClockReading(RECEIVED.utc + 30 * S, RECEIVED.monotonic + 30 * S)
    warming = play(40, half="bottom", complete=False, events=(action("pitching_substitution", at=T),))
    assert (
        middle(RECEIVED).id
        == middle(later, warming).id
        == BreakId(_MLB_GAME, BreakKind.MLB_INNING_BREAK, "top 5 middle")
    )
