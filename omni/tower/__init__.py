"""The tower: airtime on the board's surfaces, scheduled (`docs/agent_context/CLOCK_AND_SCHEDULING.md`).

`segment` holds the presentation contract: what airs, where, when, and for how long. `lane` schedules
one surface: segments in order, one on air at a time, frame by frame, each frame's dwell counted from
when the display actually showed it. `board` schedules the board surface: one card at a time, chosen by
the card queue and held by its dwell, or an explicit frame when no card should be up.
"""

from omni.tower.board import (
    BoardCue,
    BoardFrame,
    BoardHealth,
    BoardPayload,
    BoardPolicy,
    BoardSession,
    FrameKind,
    SourceHealth,
)
from omni.tower.lane import ChangeKind, EndReason, Lane, LaneChange, earliest
from omni.tower.segment import Cue, Dwell, Hold, Provenance, Segment, SegmentKey, Surface

__all__ = [
    "BoardCue",
    "BoardFrame",
    "BoardHealth",
    "BoardPayload",
    "BoardPolicy",
    "BoardSession",
    "ChangeKind",
    "Cue",
    "Dwell",
    "EndReason",
    "FrameKind",
    "Hold",
    "Lane",
    "LaneChange",
    "Provenance",
    "Segment",
    "SegmentKey",
    "SourceHealth",
    "Surface",
    "earliest",
]
