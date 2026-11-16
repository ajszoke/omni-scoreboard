"""Basic decision traces: one line per change in what the board chose or what reached the panel."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from omni.app.loop import Decision, Painted, Presentation
from omni.app.supervisor import ProviderStatus
from omni.app.trace import DecisionTracer
from omni.cards.base import CardId
from tests.test_app_loop import T, _build

LATER = T + timedelta(seconds=12)


def _decision() -> Decision:
    loop, _, _ = _build(lag=0)
    return loop.decide(T)


def _presentation() -> Presentation:
    loop, _, _ = _build(lag=0)
    loop.decide(T)
    presented = loop.paint(T).presented
    assert presented is not None
    return presented


def test_a_decision_is_written_when_it_changes_and_names_the_next_one() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    decision = _decision()
    assert decision.pick is not None
    card = decision.pick.card.id.raw
    tracer.decided(decision, took=timedelta(milliseconds=420), next_at=LATER)
    tracer.decided(dataclasses.replace(decision, at=LATER), took=timedelta(0), next_at=LATER)  # no change
    assert lines == [f"decide 23:00:00 in 0.4s: {card} (rotation) | provider fresh | next 23:00:12"]
    stale = dataclasses.replace(decision, provider_status=ProviderStatus.STALE)
    tracer.decided(stale, took=timedelta(0), next_at=LATER)
    assert lines[-1].endswith("| provider stale | next 23:00:12")


def test_a_decision_that_drops_a_game_or_a_play_is_always_written() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    decision = _decision()
    tracer.decided(decision, took=timedelta(0), next_at=LATER)
    pipeline = dataclasses.replace(decision.pipeline, skipped=("g2: feed down",), warnings=("g1: odd play",))
    tracer.decided(dataclasses.replace(decision, pipeline=pipeline), took=timedelta(0), next_at=LATER)
    assert lines[1:] == [lines[0], "  dropped: g2: feed down", "  dropped: g1: odd play"]


def test_nothing_eligible_is_written_as_such() -> None:
    lines: list[str] = []
    DecisionTracer(lines.append, timezone=timezone.utc).decided(
        dataclasses.replace(_decision(), pick=None), took=timedelta(0), next_at=LATER
    )
    assert lines == ["decide 23:00:00 in 0.0s: nothing eligible | provider fresh | next 23:00:12"]


def test_a_presentation_names_how_its_teams_were_drawn_once_per_card() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    presented = _presentation()
    tracer.painted(Painted(shown=presented.card, render_error=None, presented=presented))
    tracer.painted(Painted(shown=presented.card, render_error=None, presented=presented))  # picked again
    assert len(lines) == 1
    assert lines[0].startswith(f"present 23:00:00: {presented.card.raw} (rotation) | away base bar #")
    assert " meter #" in lines[0] and lines[0].endswith("| contrast 3/25")
    other = dataclasses.replace(presented, card=CardId("golf:live"), treatment=None)
    tracer.painted(Painted(shown=other.card, render_error=None, presented=other))
    assert lines[-1] == "present 23:00:00: golf:live (rotation)"


def test_a_paint_failure_is_written_when_it_starts_and_when_frames_commit_again() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    failed = Painted(shown=None, render_error="RenderDispatchError: no renderer", presented=None)
    tracer.painted(failed)
    tracer.painted(failed)
    tracer.painted(Painted(shown=None, render_error=None, presented=None))  # no pick: nothing to recover
    tracer.painted(Painted(shown=CardId("mlb:live"), render_error=None, presented=None))
    tracer.painted(Painted(shown=CardId("mlb:live"), render_error=None, presented=None))
    assert lines == ["paint failed: RenderDispatchError: no renderer", "paint recovered"]


def test_a_decision_that_breaks_is_written() -> None:
    lines: list[str] = []
    DecisionTracer(lines.append).failed(RuntimeError("boom"))
    assert lines == ["decide failed: RuntimeError: boom"]


def test_times_read_in_the_board_zone() -> None:
    from zoneinfo import ZoneInfo

    lines: list[str] = []
    decision = dataclasses.replace(_decision(), pick=None)
    DecisionTracer(lines.append, timezone=ZoneInfo("America/Denver")).decided(
        decision, took=timedelta(0), next_at=datetime(2026, 6, 17, 23, 0, 12, tzinfo=timezone.utc)
    )
    assert lines[0].startswith("decide 17:00:00") and lines[0].endswith("next 17:00:12")
