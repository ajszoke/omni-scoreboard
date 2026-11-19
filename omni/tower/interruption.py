"""Interruptions: a gap in what the board knows, typed, so each pass can drop what the gap made untrue.

Three things can break the board's hold on the present (`BOARD_POLICY.md` §6, "Wake or output loss"):
- **a host suspend:** the machine slept. Its monotonic clock stood still while the world, the wall clock and, where
  the platform has one, the suspend-inclusive clock (`ClockReading.suspend_inclusive`) ran on. The sign is the
  suspend-inclusive clock running ahead of the monotonic one by more than `InterruptionPolicy.suspend_over` (5 s)
  between two readings, whatever the wall clock did. Where there is no suspend-inclusive clock, the wall clock
  running ahead of the monotonic one by more than `wall_over` (30 s) stands in for it, ambiguously: a sleep or a
  wall-clock jump can't be told apart, so the clock isn't trusted. A wall-clock correction alone (the
  suspend-inclusive clock keeping pace with the monotonic one) is no interruption, and never advances an embargo;
- **a process stall:** the board's passes stopped for a while with the machine awake. The loop wakes at least every
  `health_tick` (5 s), whatever its polls are doing (a halftime hold of 13 minutes included), so a pass that comes
  more than `stall_over` (30 s) after the tick it was due is a stall;
- **an output loss:** the display's session ended (`omni.app.frame_stream.DisplayLost`): its receiver blanked,
  restarted, went silent, or its transport dropped.

The first two are the host's: the data the board held from before the gap is history. The third is the display's:
what the board knew stays true, but what the viewer saw since is unknown. Each is one `Interruption`, taken before
the next pass applies any result or cues anything. `InterruptionWatch` reads the passes' clock readings for the
host's two and numbers every interruption of a board, the display's included.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import StrEnumMixin

__all__ = ["Interruption", "InterruptionPolicy", "InterruptionReason", "InterruptionWatch"]


class InterruptionReason(StrEnumMixin, str, Enum):
    """What broke the board's hold on the present."""

    HOST_SUSPEND = "host suspend"  # the machine slept
    PROCESS_STALL = "process stall"  # the board's passes stopped, the machine awake
    OUTPUT_LOSS = "output loss"  # the display's session ended


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptionPolicy:
    """When a gap between two of the board's passes is an interruption."""

    suspend_over: timedelta = timedelta(seconds=5)  # suspend-inclusive time past monotonic time: a sleep
    wall_over: timedelta = timedelta(seconds=30)  # without that clock, wall time past monotonic time: a sleep, maybe
    health_tick: timedelta = timedelta(seconds=5)  # the loop's longest wait between passes
    stall_over: timedelta = timedelta(seconds=30)  # a pass this late past its tick: a stall

    def __post_init__(self) -> None:
        spans = (self.suspend_over, self.wall_over, self.health_tick, self.stall_over)
        if any(span <= timedelta(0) for span in spans):
            raise ValueError("an interruption's thresholds must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class Interruption:
    """One interruption of a board: why, the readings either side of the gap, and where the display stood.

    ``before`` is the last reading the board is sure of: the last pass before a host's gap, or, for an output loss,
    the last moment the display was known to show its frame. ``after`` is the first reading after the gap. The
    sessions are the display's output sessions either side (the same for a host's gap); ``last_swap`` the last frame
    the display confirmed, and ``outstanding`` the frames it had been sent and hadn't confirmed.
    """

    serial: int  # this board's interruptions, counted from 1
    reason: InterruptionReason
    before: ClockReading
    after: ClockReading
    clock_trusted: bool = True  # False when only the wall clock showed the sleep
    old_session: int = 0
    new_session: int = 0
    last_swap: ClockReading | None = None
    outstanding: tuple[int, ...] = ()

    @property
    def host(self) -> bool:
        """Whether the host's gap made the board's data history (a suspend or a stall), not just its display."""
        return self.reason is not InterruptionReason.OUTPUT_LOSS

    @property
    def gap(self) -> timedelta:
        """How long the gap ran on the board's best clock: the suspend-inclusive one, else the wall clock for a host's
        gap; the monotonic clock for an output loss."""
        before, after = self.before, self.after
        if before.suspend_inclusive is not None and after.suspend_inclusive is not None:
            return after.suspend_inclusive.since(before.suspend_inclusive)
        if self.host:
            return max(after.utc - before.utc, after.monotonic.since(before.monotonic))
        return after.monotonic.since(before.monotonic)


class InterruptionWatch:
    """Watches the readings of a board's passes for the host's interruptions, and numbers each interruption.

    Both passes (a refresh, a paint) hand it their reading as they start; it is thread-safe, and only a reading later
    on the monotonic clock than the last one counts. `next_tick` is when the next pass is due at the latest, for the
    loop's deadline.
    """

    def __init__(self, policy: InterruptionPolicy = InterruptionPolicy()) -> None:
        self._policy = policy
        self._lock = threading.Lock()
        self._last: ClockReading | None = None
        self._serial = 0

    @property
    def policy(self) -> InterruptionPolicy:
        return self._policy

    def observe(self, at: ClockReading) -> Interruption | None:
        """The interruption the gap since the last pass's reading shows, if it shows one."""
        with self._lock:
            last = self._last
            if last is not None and at.monotonic < last.monotonic:
                return None  # the other pass's reading, taken earlier: nothing new
            self._last = at
            if last is None:
                return None
            reason, trusted = self._judge(last, at)
            if reason is None:
                return None
            self._serial += 1
            return Interruption(serial=self._serial, reason=reason, before=last, after=at, clock_trusted=trusted)

    def output_lost(
        self,
        *,
        before: ClockReading,
        after: ClockReading,
        old_session: int,
        new_session: int,
        last_swap: ClockReading | None,
        outstanding: tuple[int, ...],
    ) -> Interruption:
        """The display's session ended: an interruption of its own, numbered with the rest."""
        with self._lock:
            self._serial += 1
            return Interruption(
                serial=self._serial,
                reason=InterruptionReason.OUTPUT_LOSS,
                before=before,
                after=after,
                old_session=old_session,
                new_session=new_session,
                last_swap=last_swap,
                outstanding=outstanding,
            )

    def next_tick(self) -> MonotonicTime | None:
        """When the next pass is due at the latest: a health tick after the last; None before the first."""
        with self._lock:
            return self._last.monotonic + self._policy.health_tick if self._last is not None else None

    def _judge(self, last: ClockReading, at: ClockReading) -> tuple[InterruptionReason | None, bool]:
        policy = self._policy
        monotonic = at.monotonic.since(last.monotonic)
        if last.suspend_inclusive is not None and at.suspend_inclusive is not None:
            if at.suspend_inclusive.since(last.suspend_inclusive) - monotonic > policy.suspend_over:
                return InterruptionReason.HOST_SUSPEND, True
        elif (at.utc - last.utc) - monotonic > policy.wall_over:
            return InterruptionReason.HOST_SUSPEND, False
        if monotonic - policy.health_tick > policy.stall_over:
            return InterruptionReason.PROCESS_STALL, True
        return None, True
