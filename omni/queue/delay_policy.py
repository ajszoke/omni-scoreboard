"""DelayPolicy: when a delayed observation becomes eligible to surface.

The kernel's TV-delay rule is "reveal a play only once the watcher could have seen
it on their (lagging) broadcast". That anchors to when the play *happened*
(``source_time``), not when our poller received it (``observed_at``) — so a slow
or bursty fetch can't leak a score early. Receipt-time anchoring is available as an
explicit fallback for sources whose event time isn't trustworthy.

**Display priority never shortens this.** A sports ALERT (walk-off, no-hitter) is
exactly the most spoiler-heavy content, so it waits like everything else; only a
separate system/setup status (not sports content) may use a different policy. This
is the typed home for the delay timing the `DelayBuffer` currently hard-codes to
receipt time; the orchestration pipeline computes release times from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from omni.core.enum import StrEnumMixin
from omni.core.observation import Observation
from omni.core.time import DurationSeconds

__all__ = ["DelayAnchor", "DelayPolicy"]


class DelayAnchor(StrEnumMixin, str, Enum):
    """What the broadcast lag is measured from."""

    SOURCE_TIME = "source_time"  # when the play happened — the safe default
    OBSERVED_AT = "observed_at"  # when we received it — explicit fallback only


@dataclass(frozen=True, slots=True, kw_only=True)
class DelayPolicy:
    """Turns an :class:`Observation` into the instant it may be revealed."""

    broadcast_lag: DurationSeconds
    anchor: DelayAnchor = DelayAnchor.SOURCE_TIME

    def eligible_at(self, observation: Observation[Any]) -> datetime:
        """The earliest instant ``observation`` may surface (never before receipt).

        ``SOURCE_TIME`` anchors to ``source_time`` (falling back to ``observed_at``
        when the source gave none); ``OBSERVED_AT`` anchors to receipt. Either way
        the result is clamped to ``>= observed_at`` so a reveal never predates when
        we actually had the data.
        """
        if self.anchor is DelayAnchor.OBSERVED_AT:
            anchor = observation.observed_at
        else:
            anchor = observation.source_time or observation.observed_at
        return max(observation.observed_at, anchor + self.broadcast_lag.as_timedelta())
