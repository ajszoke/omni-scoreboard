"""Segments: the units of airtime a surface schedules, and the frame of one that airs at a time.

A *segment* is one thing to put on a surface. Today that's an entry in a live football card's play
strip; the board's own card rotation comes next. A segment carries the presentation contract:
- **Identity:** a key that stays the same across versions, and a revision. A newer revision edits the
  segment in place. A material correction is a segment of its own, so it airs as one.
- **Place:** its surface, and the parent it belongs to (for a strip entry, the card it's part of).
- **Payload:** its frames, typed, shown in order, and its provenance (who said it, and when the answer
  carrying it arrived).
- **Availability:** the earliest it may air, the latest it may still start, and when it expires
  outright, on air or not. These are UTC instants.
- **Dwell:** each frame's minimum and maximum time on air. It counts on the monotonic clock from when
  the display first committed that frame, so a frame that failed to draw spends none of its time.
- **Hold:** a typed condition that keeps it up past its maximum (a stoppage, until play resumes).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Generic, TypeVar

from omni.core.enum import StrEnumMixin
from omni.core.ids import SourceRef

__all__ = ["Cue", "Dwell", "Hold", "Provenance", "Segment", "SegmentKey", "Surface"]

PayloadT = TypeVar("PayloadT")


class Surface(StrEnumMixin, str, Enum):
    """Where a segment airs."""

    BOARD = "board"  # the whole panel, one card at a time (A1a step 4)
    PLAY_STRIP = "play_strip"  # the play lane of a live football card


class Hold(StrEnumMixin, str, Enum):
    """What a segment waits for, past its maximum dwell, before it comes down."""

    PLAY_RESUMES = "play_resumes"  # a stoppage: up until play goes on, or something else comes


@dataclass(frozen=True, slots=True)
class SegmentKey:
    """A segment's identity on its surface: what it's about, the same in every revision."""

    surface: Surface
    subject: str


@dataclass(frozen=True, slots=True)
class Dwell:
    """How long each frame of a segment stays on air, from its first committed presentation."""

    minimum: timedelta  # nothing that merely waits cuts a frame shorter than this
    maximum: timedelta  # a frame goes at this, unless its segment is held

    def __post_init__(self) -> None:
        if self.minimum < timedelta(0) or self.maximum < self.minimum:
            raise ValueError("a dwell's minimum is zero or more, and its maximum no shorter")


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a segment's content came from."""

    source: SourceRef
    received_at: datetime  # when the answer that carried it arrived
    as_of: datetime | None = None  # when the source says it happened, if it says


@dataclass(frozen=True, slots=True, kw_only=True)
class Segment(Generic[PayloadT]):
    """One thing to put on a surface, frame by frame, with the terms it airs on."""

    key: SegmentKey
    frames: tuple[PayloadT, ...]
    dwell: Dwell
    provenance: Provenance
    available_at: datetime
    revision: int = 0
    parent: str | None = None
    latest_start: datetime | None = None  # if it hasn't started by then, it's dropped unseen
    expires_at: datetime | None = None  # gone by then, on air or waiting (a hold on the panel, while current)
    hold: Hold | None = None

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("a segment has at least one frame")
        instants = (self.available_at, self.latest_start, self.expires_at, self.provenance.received_at)
        if any(instant is not None and instant.tzinfo is None for instant in (*instants, self.provenance.as_of)):
            raise ValueError("a segment's instants must be timezone-aware")
        if self.latest_start is not None and self.latest_start < self.available_at:
            raise ValueError("a segment's latest start can't come before it's available")
        if self.expires_at is not None and self.expires_at <= self.available_at:
            raise ValueError("a segment must expire after it's available")


@dataclass(frozen=True, slots=True)
class Cue(Generic[PayloadT]):
    """What should be on a surface now: one frame of one segment."""

    segment: Segment[PayloadT]
    frame: int

    @property
    def payload(self) -> PayloadT:
        return self.segment.frames[self.frame]
