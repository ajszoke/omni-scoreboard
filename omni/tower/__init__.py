"""The tower: airtime on the board's surfaces, scheduled (`docs/agent_context/CLOCK_AND_SCHEDULING.md`).

`segment` holds the presentation contract: what airs, where, when, and for how long. `lane` schedules
one surface: segments in order, one on air at a time, frame by frame, each frame's dwell counted from
when the display actually showed it.
"""

from omni.tower.lane import ChangeKind, EndReason, Lane, LaneChange, earliest
from omni.tower.segment import Cue, Dwell, Hold, Provenance, Segment, SegmentKey, Surface

__all__ = [
    "ChangeKind",
    "Cue",
    "Dwell",
    "EndReason",
    "Hold",
    "Lane",
    "LaneChange",
    "Provenance",
    "Segment",
    "SegmentKey",
    "Surface",
    "earliest",
]
