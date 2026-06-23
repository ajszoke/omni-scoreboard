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

from omni.cards.attention import AttentionMode, AttentionPolicy
from omni.cards.base import (
    CardId,
    CardKind,
    CardPriority,
    DedupeKey,
    DisplayTiming,
    LayoutSupport,
    ScoreboardCard,
)
from omni.cards.baseball import (
    BigPlayCardPayload,
    FinalCardPayload,
    LiveBaseballCardPayload,
    PregameCardPayload,
)
from omni.core.enum import DisplayPriority, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballGameState
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent

__all__ = ["CardFactory"]

# The live-baseball renderer natively supports all three profiles (see PR #7).
_LIVE_BASEBALL_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
# Pregame renders natively on all three; the small panel drops the "first pitch" label.
_PREGAME_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_PREGAME_COMPROMISE = ("single_64x32: matchup + countdown only — the 'first pitch' label is dropped (no room).",)
# Final renders natively on all three; the small panel shortens the status label.
_FINAL_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_FINAL_COMPROMISE = ('single_64x32: the status reads "FIN" — "FINAL" does not fit at 64px wide.',)
# A big play flashes on all three; the small panel drops the play description.
_BIG_PLAY_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_BIG_PLAY_COMPROMISE = ("single_64x32: headline + score only — the play description is dropped (no room).",)
# A scoring play takes the screen with a bounded BURST, then yields (never permanent).
_BIG_PLAY_ATTENTION = AttentionPolicy(mode=AttentionMode.BURST, takeover_for=DurationSeconds(8))
_BIG_PLAY_PRIORITY = CardPriority(band=DisplayPriority.ALERT, score=0.0)
_DEFAULT_PRIORITY = CardPriority(band=DisplayPriority.NORMAL, score=0.0)


@dataclass(frozen=True, slots=True)
class CardFactory:
    """Turns domain game state into renderable cards.

    Default min/max on-screen durations are configurable so device profiles can
    tune dwell time without touching call sites.
    """

    live_min_display: DurationSeconds = DurationSeconds(8)
    live_max_display: DurationSeconds = DurationSeconds(30)
    pregame_min_display: DurationSeconds = DurationSeconds(8)
    pregame_max_display: DurationSeconds = DurationSeconds(20)
    final_min_display: DurationSeconds = DurationSeconds(8)
    final_max_display: DurationSeconds = DurationSeconds(20)
    final_postgame_window: DurationSeconds = DurationSeconds(1800)  # how long a final lingers post-game
    big_play_min_display: DurationSeconds = DurationSeconds(6)
    big_play_max_display: DurationSeconds = DurationSeconds(15)
    big_play_window: DurationSeconds = DurationSeconds(120)  # how long a big play stays in rotation

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
            phase=state.phase,
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

    def pregame(
        self,
        game: TeamGame,
        *,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[PregameCardPayload]:
        """Build a pregame MLB card from a scheduled matchup.

        The card snapshots the scheduled first pitch; the renderer derives the live
        countdown from the render clock, so one card stays correct across ticks.
        """
        payload = PregameCardPayload(scheduled_start=game.scheduled_start)
        key = f"{game.id.raw}:pregame"
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.PREGAME,
            contest=game,
            timing=DisplayTiming(
                available_at=now,
                min_display=self.pregame_min_display,
                max_display=self.pregame_max_display,
            ),
            priority=priority if priority is not None else _DEFAULT_PRIORITY,
            layout_support=LayoutSupport(profiles=_PREGAME_PROFILES, compromise_notes=_PREGAME_COMPROMISE),
            dedupe_key=DedupeKey(key),
            payload=payload,
        )

    def final(
        self,
        game: TeamGame,
        state: BaseballGameState,
        *,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[FinalCardPayload]:
        """Build a final MLB card from a completed game's state.

        The winner is derived from the score by the payload; the card lingers for a
        finite postgame window (``final_postgame_window``), then expires from rotation.
        """
        payload = FinalCardPayload(away_score=state.away_score, home_score=state.home_score)
        key = f"{game.id.raw}:final"
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.FINAL,
            contest=game,
            timing=DisplayTiming(
                available_at=now,
                min_display=self.final_min_display,
                max_display=self.final_max_display,
                expires_at=now + self.final_postgame_window.as_timedelta(),
            ),
            priority=priority if priority is not None else _DEFAULT_PRIORITY,
            layout_support=LayoutSupport(profiles=_FINAL_PROFILES, compromise_notes=_FINAL_COMPROMISE),
            dedupe_key=DedupeKey(key),
            payload=payload,
        )

    def big_play(
        self,
        game: TeamGame,
        event: BaseballGameEvent,
        state: BaseballGameState,
        *,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[BigPlayCardPayload]:
        """Build a big-play card from a typed event + the resulting game state.

        The card carries the event's lineage (`source_event_ids`) so the play stays
        dedupable, replayable, and auditable (round-1 High #1), and a bounded `BURST`
        attention so it flashes then yields rather than monopolizing the screen.
        """
        payload = BigPlayCardPayload(
            event_type=event.event_type,
            description=event.payload.description,
            away_score=state.away_score,
            home_score=state.home_score,
        )
        key = f"{game.id.raw}:bigplay:{event.id.raw}"
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.BIG_PLAY,
            contest=game,
            timing=DisplayTiming(
                available_at=now,
                min_display=self.big_play_min_display,
                max_display=self.big_play_max_display,
                expires_at=now + self.big_play_window.as_timedelta(),
            ),
            priority=priority if priority is not None else _BIG_PLAY_PRIORITY,
            layout_support=LayoutSupport(profiles=_BIG_PLAY_PROFILES, compromise_notes=_BIG_PLAY_COMPROMISE),
            dedupe_key=DedupeKey(key),
            source_event_ids=(event.id,),
            attention=_BIG_PLAY_ATTENTION,
            payload=payload,
        )
