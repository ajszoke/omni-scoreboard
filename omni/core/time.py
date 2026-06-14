"""Duration value object for the Omni domain (TV-delay, display timing).

A typed alternative to the loose ``min_seconds`` / ``max_seconds`` ints the type
policy in ``AGENTS.md`` warns against scattering across the code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

__all__ = ["DurationSeconds"]


@dataclass(frozen=True, slots=True)
class DurationSeconds:
    """A non-negative whole-second duration."""

    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("duration cannot be negative")

    def as_timedelta(self) -> timedelta:
        return timedelta(seconds=self.value)
