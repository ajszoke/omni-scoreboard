"""Time value objects and helpers for the Omni domain (TV-delay, display timing).

A typed alternative to the loose ``min_seconds`` / ``max_seconds`` ints the type
policy in ``AGENTS.md`` warns against scattering across the code, plus the
timezone-aware "what local day is it" helper the schedule window needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["DurationSeconds", "local_date"]


@dataclass(frozen=True, slots=True)
class DurationSeconds:
    """A non-negative whole-second duration."""

    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("duration cannot be negative")

    def as_timedelta(self) -> timedelta:
        return timedelta(seconds=self.value)


def local_date(now: datetime, tz: ZoneInfo) -> date:
    """The local calendar date at ``now`` in ``tz`` — the sports "day" to fetch.

    Anchoring the schedule to a configured IANA zone (not ``now.date()`` in UTC)
    is what keeps a US-evening unit from requesting *tomorrow's* slate while
    tonight's games are still on. Fails fast on a naive ``now``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware to localize it")
    return now.astimezone(tz).date()
