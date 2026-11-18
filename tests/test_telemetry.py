"""Our own decision telemetry carries no provider's words: the MLB board's trace and the replay decisions.

They may be kept (`omni.providers.rights.Telemetry.OWN_DECISIONS`), so they hold ids, kinds, reasons,
timings, versions and counts, and never a play's description, a player's name or anything else a source
wrote.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from omni.app.loop import Painted, Refresh
from omni.app.trace import DecisionTracer
from omni.core.enum import PanelProfile
from omni.replay.harness import _drive, replay_decisions, replay_versions
from omni.replay.timeline import Timeline
from omni.tower.board import BoardPolicy
from tests.mlb_slate import END, FAVORITES, LAG, slate

QUAD = PanelProfile.QUAD_128X64


def _worded_slate() -> Timeline:
    """The evening's slate, its games given a venue as a schedule names one."""
    timeline = slate()
    frames = tuple(
        dataclasses.replace(frame, game=dataclasses.replace(frame.game, venue_name=f"{frame.game.id.raw} Field"))
        for frame in timeline.frames
    )
    return Timeline(frames=frames, outages=timeline.outages)


def _provider_words(timeline: Timeline) -> set[str]:
    """Everything a source worded in the timeline: venues, play descriptions and the players they name,
    and a final's pitchers."""
    words: set[str] = set()
    for frame in timeline.frames:
        if frame.game.venue_name is not None:
            words.add(frame.game.venue_name)
        for event in frame.events:
            description = event.payload.description
            words |= {description, description.split()[0]}
        if frame.decisions is not None:
            decisions = frame.decisions
            words |= {name for name in (decisions.winner, decisions.loser, decisions.save) if name is not None}
    return words


def test_the_replay_decisions_carry_no_provider_text() -> None:
    timeline = _worded_slate()
    words = _provider_words(timeline)
    assert {"Freeman homers to right field. Walk-off!", "Freeman", "Evan Phillips", "col-lad Field"} <= words
    lines = replay_decisions(timeline, profile=QUAD, until=END, broadcast_lag=LAG, favorites=FAVORITES)
    assert any("bigplay" in line for line in lines) and any("final" in line for line in lines)
    assert [line for line in lines if any(word in line for word in words)] == []


def test_the_running_board_s_trace_carries_no_provider_text() -> None:
    timeline = _worded_slate()
    words = _provider_words(timeline)
    lines: list[str] = []
    tracer = DecisionTracer(lines.append, timezone=timezone.utc)
    tracer.versions(replay_versions(QUAD, broadcast_lag=LAG, favorites=FAVORITES))

    def observe(at: datetime, refresh: Refresh, painted: Painted) -> None:
        tracer.refreshed(refresh, took=timedelta(0), next_at=None)
        tracer.painted(painted)

    _drive(timeline, QUAD, END, LAG, FAVORITES, BoardPolicy(), observe, every=None, draw=True)
    assert any(line.startswith("up ") and " | away " in line for line in lines)  # with how each team was drawn
    assert any(line.startswith("  final: ") for line in lines)
    assert [line for line in lines if any(word in line for word in words)] == []
