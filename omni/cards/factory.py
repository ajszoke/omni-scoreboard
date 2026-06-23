"""CardFactory: builds renderable `ScoreboardCard`s from typed domain state.

This is the seam between "what the game is" (domain `BaseballGameState`) and
"how a card shows it" (a `LiveBaseballCardPayload` plus display metadata). It is
pure — no I/O, no provider JSON — so it is fully unit-testable.

The factory stays pure: it stamps timing from the `now` it is handed and takes an
already-scored `priority` (defaulting to NORMAL only when none is supplied). The
queue layer wires the real values around it — `PriorityScorer` produces the
priority, and the TV-delay is applied to the timing upstream (a typed delay
policy in the forthcoming orchestration spine, not inside the factory).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.cards.base import (
    CardId,
    CardKind,
    CardPriority,
    DedupeKey,
    DisplayTiming,
    LayoutSupport,
    ScoreboardCard,
)
from omni.cards.baseball import LiveBaseballCardPayload
from omni.core.enum import DisplayPriority, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballGameState
from omni.domain.contest import TeamGame

__all__ = ["CardFactory"]

# The live-baseball renderer natively supports all three profiles (see PR #7).
_LIVE_BASEBALL_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_DEFAULT_PRIORITY = CardPriority(band=DisplayPriority.NORMAL, score=0.0)


@dataclass(frozen=True, slots=True)
class CardFactory:
    """Turns domain game state into renderable cards.

    Default min/max on-screen durations are configurable so device profiles can
    tune dwell time without touching call sites.
    """

    live_min_display: DurationSeconds = DurationSeconds(8)
    live_max_display: DurationSeconds = DurationSeconds(30)

    def live_baseball(
        self,
        game: TeamGame,
        state: BaseballGameState,
        *,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[LiveBaseballCardPayload]:
        """Build a live MLB card from a matchup + its observed game state."""
        payload = LiveBaseballCardPayload(
            away_score=state.away_score,
            home_score=state.home_score,
            inning=state.inning,
            half=state.half,
            count=state.count,
            bases=state.bases,
        )
        key = f"{game.id.raw}:live"
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.LIVE_GAME,
            contest=game,
            timing=DisplayTiming(
                available_at=now,
                min_display=self.live_min_display,
                max_display=self.live_max_display,
            ),
            priority=priority if priority is not None else _DEFAULT_PRIORITY,
            layout_support=LayoutSupport(profiles=_LIVE_BASEBALL_PROFILES),
            dedupe_key=DedupeKey(key),
            payload=payload,
        )
