"""Tests for the replay harness: the real AppLoop driven over a Timeline, deterministically.

These drive the same `run_once` code path production uses, ticked
under a FakeClock from a fixture timeline, yielding a reproducible queue trace.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from omni.app.supervisor import ProviderStatus
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.replay.harness import ReplayProvider, TraceEntry, replay
from omni.replay.timeline import GameFrame, Timeline

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
QUAD = PanelProfile.QUAD_128X64
_REG = MlbTeamRegistry.from_color_file()


def _game(gid: str, status: GameStatus) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, gid),
        league=League.MLB,
        status=status,
        scheduled_start=T0,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def _state(away: int = 0, home: int = 0) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=1,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=0, strikes=0, outs=0),
        bases=BaseballBaseState(),
    )


def _live(at: datetime, gid: str, **score: int) -> GameFrame:
    return GameFrame(at=at, game=_game(gid, GameStatus.LIVE), state=_state(**score))


def _by_offset(trace: list[TraceEntry]) -> dict[int, str | None]:
    return {int((e.at - T0).total_seconds()): e.shown for e in trace}


def test_replay_is_deterministic() -> None:
    timeline = Timeline(frames=(_live(T0, "g1"), _live(T0, "g2")))
    until = T0 + timedelta(seconds=60)
    first = replay(timeline, profile=QUAD, tick=DurationSeconds(10), until=until)
    second = replay(timeline, profile=QUAD, tick=DurationSeconds(10), until=until)
    assert first == second  # identical input -> identical trace


def test_two_simultaneous_games_both_get_airtime() -> None:
    timeline = Timeline(frames=(_live(T0, "g1"), _live(T0, "g2")))
    trace = replay(timeline, profile=QUAD, tick=DurationSeconds(10), until=T0 + timedelta(seconds=120))
    shown = Counter(e.shown for e in trace)
    assert shown["g1:live"] > 0 and shown["g2:live"] > 0  # neither contest starves
    assert all(e.provider_status is ProviderStatus.FRESH for e in trace)  # provider stayed healthy


def test_lifecycle_transition_enters_and_removes_the_card() -> None:
    # One game pregame -> live -> final; with no card kinds for pregame/final yet, the
    # live card enters when the game goes live and is removed when it goes final.
    timeline = Timeline(
        frames=(
            GameFrame(at=T0, game=_game("g1", GameStatus.PREGAME)),
            _live(T0 + timedelta(seconds=30), "g1"),
            GameFrame(at=T0 + timedelta(seconds=60), game=_game("g1", GameStatus.FINAL)),
        )
    )
    shown = _by_offset(replay(timeline, profile=QUAD, tick=DurationSeconds(10), until=T0 + timedelta(seconds=90)))
    assert shown[0] is None and shown[20] is None  # pregame: no live card
    assert shown[30] == "g1:live" and shown[50] == "g1:live"  # live: carded
    assert shown[60] is None and shown[80] is None  # final: card removed


def test_live_card_persists_through_inning_breaks() -> None:
    # A game cycling top -> middle -> bottom -> end stays carded the whole inning,
    # including the between-halves breaks (status stays LIVE through them).
    phases = (InningPhase.TOP, InningPhase.MIDDLE, InningPhase.BOTTOM, InningPhase.END)
    frames = tuple(
        GameFrame(
            at=T0 + timedelta(seconds=20 * i),
            game=_game("g1", GameStatus.LIVE),
            state=BaseballGameState(
                away_score=0,
                home_score=0,
                inning=7,
                phase=phase,
                count=BaseballCount(balls=0, strikes=0, outs=0),
                bases=BaseballBaseState(),
                away_hits=5,
                home_hits=4,  # a normal game — not an accidental no-hitter at this inning
            ),
        )
        for i, phase in enumerate(phases)
    )
    shown = _by_offset(
        replay(Timeline(frames=frames), profile=QUAD, tick=DurationSeconds(20), until=T0 + timedelta(seconds=60))
    )
    assert set(shown.values()) == {"g1:live"}  # never drops across TOP/MIDDLE/BOTTOM/END


def test_broadcast_delay_is_respected_end_to_end() -> None:
    # A game live from the start, behind a 30s TV delay: nothing shows until the first
    # observation becomes eligible, proving the delay flows through the real loop.
    timeline = Timeline(frames=(_live(T0, "g1"),))
    shown = _by_offset(
        replay(
            timeline,
            profile=QUAD,
            tick=DurationSeconds(10),
            until=T0 + timedelta(seconds=50),
            broadcast_lag=DurationSeconds(30),
        )
    )
    assert shown[0] is None and shown[10] is None and shown[20] is None  # inside the delay
    assert shown[30] == "g1:live" and shown[40] == "g1:live"  # delayed state now eligible


def test_replay_rejects_nonpositive_tick() -> None:
    timeline = Timeline(frames=(_live(T0, "g1"),))
    with pytest.raises(ValueError, match="positive duration"):
        replay(timeline, profile=QUAD, tick=DurationSeconds(0))


def test_replay_rejects_until_before_start() -> None:
    timeline = Timeline(frames=(_live(T0, "g1"),))
    with pytest.raises(ValueError, match="before the timeline start"):
        replay(timeline, profile=QUAD, tick=DurationSeconds(10), until=T0 - timedelta(seconds=1))


def test_replay_provider_serves_the_schedule_as_of_now() -> None:
    provider = ReplayProvider(Timeline(frames=(GameFrame(at=T0, game=_game("g1", GameStatus.PREGAME)),)))
    assert provider.league is League.MLB
    assert provider.source.name == "replay"
    update = provider.refresh(T0)
    assert update.observed_at == T0
    assert tuple(c.id.raw for c in update.contests) == ("g1",)
    assert provider.refresh(T0 - timedelta(seconds=1)).contests == ()  # before the first frame
