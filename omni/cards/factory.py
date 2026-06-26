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
    NoHitterCardPayload,
    PregameCardPayload,
    StatusCardPayload,
)
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballGameState, PitchingDecisions, TeamLinescore, WinProbability
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent

__all__ = ["CardFactory"]

# The live-baseball renderer natively supports all three profiles.
_LIVE_BASEBALL_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
# Pregame renders natively on all three; the small panel drops the "first pitch" label.
_PREGAME_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_PREGAME_COMPROMISE = ("single_64x32: matchup + countdown only — the 'first pitch' label is dropped (no room).",)
# Final renders natively on all three; the small panel shortens the status label.
_FINAL_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_FINAL_COMPROMISE = (
    'single_64x32: the status reads "FIN" ("FINAL" does not fit at 64px wide) and the W/L/S pitching line is dropped.',
    "stack_64x64: the save line is dropped from the pitching line (no vertical room); the winner and loser remain.",
)
# A big play flashes on all three; the small panel drops the play description.
_BIG_PLAY_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_BIG_PLAY_COMPROMISE = ("single_64x32: headline + score only — the play description is dropped (no room).",)
# A no-hitter renders natively on all three; the small panel drops the "through N" inning.
_NO_HITTER_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
_NO_HITTER_COMPROMISE = ("single_64x32: headline + team only — the 'through N' inning is dropped (no room).",)
# A status (delay/suspension) card carries only a matchup + banner, so it fits all three natively.
_STATUS_PROFILES = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})
# An active no-hitter resurfaces periodically (not constantly) while the bid is alive; it
# has no max_repeats — the pipeline removes the card when the no-hitter is broken or ends.
_NO_HITTER_ATTENTION = AttentionPolicy(mode=AttentionMode.RECURRING, cooldown=DurationSeconds(60))
_DEFAULT_PRIORITY = CardPriority(band=DisplayPriority.NORMAL, score=0.0)


def _no_hitter_priority(perfect: bool) -> CardPriority:
    """A no-hitter is an ALERT; a perfect game outranks a plain one (explainable, not a magic float)."""
    return CardPriority(
        band=DisplayPriority.ALERT,
        score=1.0 if perfect else 0.0,
        reasons=("perfect game",) if perfect else ("no-hitter",),
    )


def _big_play_priority(event: BaseballGameEvent) -> CardPriority:
    """The big-play card's priority from the event's *contextual* importance.

    Carrying the event's band/leverage/reasons (which now reflect what the play did —
    walk-off, go-ahead, tying — not just its bare type) is what lets a walk-off single
    outrank a routine RBI in the queue instead of every scoring play looking identical.
    """
    return CardPriority(
        band=event.importance.priority,
        score=event.importance.leverage,
        reasons=event.importance.reasons,
    )


def _big_play_attention(band: DisplayPriority) -> AttentionPolicy:
    """A bounded BURST takeover; a bigger play (an ALERT walk-off / home run) holds a touch longer."""
    takeover = DurationSeconds(12) if band >= DisplayPriority.ALERT else DurationSeconds(8)
    return AttentionPolicy(mode=AttentionMode.BURST, takeover_for=takeover)


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
    no_hitter_min_display: DurationSeconds = DurationSeconds(8)
    no_hitter_max_display: DurationSeconds = DurationSeconds(20)
    status_min_display: DurationSeconds = DurationSeconds(8)
    status_max_display: DurationSeconds = DurationSeconds(20)

    def live_baseball(
        self,
        game: TeamGame,
        state: BaseballGameState,
        *,
        now: datetime,
        priority: CardPriority | None = None,
        win_probability: WinProbability | None = None,
    ) -> ScoreboardCard[LiveBaseballCardPayload]:
        """Build a live MLB card from a matchup + its observed game state.

        `win_probability`, when given, drives the per-team meter; the caller is
        responsible for handing in a *delay-safe* sample (one matching the lag-old
        state), never a fresh reading — see the pipeline's win-probability path.
        """
        payload = LiveBaseballCardPayload(
            away_line=TeamLinescore(runs=state.away_score, hits=state.away_hits, errors=state.away_errors),
            home_line=TeamLinescore(runs=state.home_score, hits=state.home_hits, errors=state.home_errors),
            inning=state.inning,
            phase=state.phase,
            count=state.count,
            bases=state.bases,
            win_probability=win_probability,
            batter=state.batter,
            pitcher=state.pitcher,
            last_pitch=state.last_pitch,
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
        decisions: PitchingDecisions | None = None,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[FinalCardPayload]:
        """Build a final MLB card from a completed game's state and pitching decisions.

        The winner is derived from the score by the payload; `decisions` (winner/loser/save,
        None on a tie or an undecided feed) rides along for the W/L/S line. The card lingers
        for a finite postgame window (``final_postgame_window``), then expires from rotation.
        """
        payload = FinalCardPayload(away_score=state.away_score, home_score=state.home_score, decisions=decisions)
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
        *,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[BigPlayCardPayload]:
        """Build a big-play card from a typed event.

        The score shown is the play's *resulting* score, carried on the event payload —
        not the live game state — so a delayed big play never reveals a later, un-aired
        score. The card carries the event's lineage (`source_event_ids`) so the play
        stays dedupable, replayable, and auditable, and a bounded `BURST` attention so
        it flashes then yields rather than monopolizing the screen.
        """
        away_score = event.payload.away_score
        home_score = event.payload.home_score
        if away_score is None or home_score is None:
            raise ValueError("big-play event must carry a post-play score")
        payload = BigPlayCardPayload(
            event_type=event.event_type,
            description=event.payload.description,
            away_score=away_score,
            home_score=home_score,
        )
        resolved_priority = priority if priority is not None else _big_play_priority(event)
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
            priority=resolved_priority,
            layout_support=LayoutSupport(profiles=_BIG_PLAY_PROFILES, compromise_notes=_BIG_PLAY_COMPROMISE),
            dedupe_key=DedupeKey(key),
            source_event_ids=(event.id,),
            attention=_big_play_attention(resolved_priority.band),
            payload=payload,
        )

    def no_hitter(
        self,
        game: TeamGame,
        *,
        pitching_side: HomeAway,
        through_inning: int,
        perfect: bool = False,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[NoHitterCardPayload]:
        """Build a no-hitter / perfect-game card for an in-progress feat.

        A standing alert, not a one-shot play: it carries a bounded `RECURRING` attention
        so it resurfaces periodically (not constantly) while the bid is alive, and no
        `expires_at` — the pipeline removes it when the no-hitter is broken or the game
        ends. One card per game (keyed `:nohitter`), refreshed as the bid carries deeper.
        """
        payload = NoHitterCardPayload(pitching_side=pitching_side, through_inning=through_inning, perfect=perfect)
        key = f"{game.id.raw}:nohitter"
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.NO_HITTER,
            contest=game,
            timing=DisplayTiming(
                available_at=now,
                min_display=self.no_hitter_min_display,
                max_display=self.no_hitter_max_display,
            ),
            priority=priority if priority is not None else _no_hitter_priority(perfect),
            layout_support=LayoutSupport(profiles=_NO_HITTER_PROFILES, compromise_notes=_NO_HITTER_COMPROMISE),
            dedupe_key=DedupeKey(key),
            attention=_NO_HITTER_ATTENTION,
            payload=payload,
        )

    def status(
        self,
        game: TeamGame,
        *,
        status: GameStatus,
        now: datetime,
        priority: CardPriority | None = None,
    ) -> ScoreboardCard[StatusCardPayload]:
        """Build a status card for a game paused mid-life (a delay or a suspension).

        It keeps a paused game on the board — matchup + a status banner, no score — instead
        of letting it fall out of every lifecycle phase. It needs no feed fetch and no TV
        delay (it reveals no score), and sits in normal rotation; the pipeline replaces it
        with the live card on resume, or the final card once the game ends. One card per game
        (keyed `:status`), refreshed while the game stays paused.
        """
        payload = StatusCardPayload(status=status)
        key = f"{game.id.raw}:status"
        return ScoreboardCard(
            id=CardId(key),
            kind=CardKind.STATUS,
            contest=game,
            timing=DisplayTiming(
                available_at=now,
                min_display=self.status_min_display,
                max_display=self.status_max_display,
            ),
            priority=priority if priority is not None else _DEFAULT_PRIORITY,
            layout_support=LayoutSupport(profiles=_STATUS_PROFILES),
            dedupe_key=DedupeKey(key),
            payload=payload,
        )
