"""The display queue (ROADMAP Phase 4): score, delay, and interleave cards.

- `PriorityScorer` turns game state into an explainable `CardPriority`.
- `DelayPolicy` decides when a delayed observation may surface (anchored to
  source time, not receipt); `DelayBuffer` gates items by TV-delay so live
  scores never spoil.
- `InterleavedCardQueue` picks the next card fairly across contests.
"""

from __future__ import annotations

from omni.queue.delay_buffer import DelayBuffer
from omni.queue.delay_policy import DelayAnchor, DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

__all__ = ["DelayAnchor", "DelayBuffer", "DelayPolicy", "InterleavedCardQueue", "PriorityScorer"]
