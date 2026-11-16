"""Tests for the replay harness: the real AppLoop driven over a Timeline, deterministically.

These drive the same `run_once` code path production uses, ticked
under a FakeClock from a fixture timeline, yielding a reproducible queue trace.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omni.app.supervisor import ProviderStatus
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
import dataclasses

from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.providers.base import ProviderError
from omni.queue.scheduler import InterleavedCardQueue
from omni.queue.scheduler import SelectionReason
from omni.tower.board import FrameKind
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.replay.harness import ReplayProvider, TraceEntry, replay, replay_decisions
from omni.replay.timeline import GameFrame, Outage, Timeline

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
QUAD = PanelProfile.QUAD_128X64
SLATE_DECISIONS = Path(__file__).parent / "fixtures" / "replay" / "mlb-slate.decisions.txt"
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
    # One game pregame -> live -> final, all through the real loop: the pregame card shows the
    # upcoming matchup, the live card takes over at first pitch, and the final card reveals once
    # the game ends (no broadcast delay here, so the result surfaces as soon as it is seen final).
    timeline = Timeline(
        frames=(
            GameFrame(at=T0, game=_game("g1", GameStatus.PREGAME)),
            _live(T0 + timedelta(seconds=30), "g1"),
            GameFrame(at=T0 + timedelta(seconds=60), game=_game("g1", GameStatus.FINAL), state=_state(away=2, home=5)),
        )
    )
    shown = _by_offset(replay(timeline, profile=QUAD, tick=DurationSeconds(10), until=T0 + timedelta(seconds=90)))
    assert shown[0] == "g1:pregame" and shown[20] == "g1:pregame"  # pregame: matchup + countdown
    assert shown[30] == "g1:live" and shown[50] == "g1:live"  # live: the live card takes over
    assert shown[60] == "g1:final" and shown[80] == "g1:final"  # final: the result is revealed


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


def test_decisions_list_each_change_on_the_board_with_its_reason() -> None:
    timeline = Timeline(frames=(_live(T0, "g1"), _live(T0, "g2")))
    lines = replay_decisions(
        timeline, profile=QUAD, until=T0 + timedelta(seconds=60), broadcast_lag=DurationSeconds(30)
    )
    assert lines[:6] == [
        "     0.0  source  fresh",
        "     0.0  frame   held  (2 games in the TV delay)",
        "    30.0  up      g2:live  (rotation)",
        "    42.0  down    g2:live  (ran its time, 12.0s)",
        "    42.0  up      g1:live  (rotation)",
        "    54.0  down    g1:live  (ran its time, 12.0s)",
    ]


def test_an_outage_dims_the_cards_then_takes_them_down_and_the_board_recovers() -> None:
    timeline = Timeline(
        frames=(_live(T0, "g1"),),
        outages=(Outage(start=T0 + timedelta(seconds=10), end=T0 + timedelta(seconds=700)),),
    )
    provider = ReplayProvider(timeline)
    with pytest.raises(ProviderError, match="outage"):
        provider.refresh(T0 + timedelta(seconds=10))
    lines = replay_decisions(timeline, profile=QUAD, until=T0 + timedelta(seconds=720))
    assert lines == [
        "     0.0  source  fresh",
        "     0.0  up      g1:live  (rotation)",
        "    10.0  stale   g1:live",  # its feed stopped answering
        "   129.0  source  stale  (no schedule for over 2m: ProviderError: replay outage)",
        "   130.0  down    g1:live  (no longer current, 130.0s)",  # two minutes on, its last good comes down
        "   130.0  frame   offline  (no feed for a game)",
        "   609.0  source  offline  (no schedule for over 10m: ProviderError: replay outage)",
        "   700.0  source  fresh",
        "   700.0  up      g1:live  (rotation)",
    ]


def test_a_game_without_a_state_has_no_feed() -> None:
    final_without_state = GameFrame(at=T0, game=_game("g1", GameStatus.FINAL))
    trace = replay(Timeline(frames=(final_without_state,)), profile=QUAD, tick=DurationSeconds(10))
    assert [(entry.shown, entry.frame) for entry in trace] == [(None, FrameKind.IDLE)]  # the final can't be read


def test_the_slate_replays_to_its_recorded_decisions() -> None:
    from tests.mlb_slate import END, FAVORITES, LAG, slate

    lines = replay_decisions(slate(), profile=QUAD, until=END, broadcast_lag=LAG, favorites=FAVORITES)
    assert lines == SLATE_DECISIONS.read_text().splitlines()


def test_replay_samples_what_the_panel_showed_and_why() -> None:
    timeline = Timeline(frames=(_live(T0, "g1"),))
    trace = replay(timeline, profile=QUAD, tick=DurationSeconds(5), until=T0 + timedelta(seconds=10))
    assert [(entry.shown, entry.reason) for entry in trace] == [("g1:live", SelectionReason.ROTATION)] * 3


def test_a_card_that_never_drew_is_listed_as_dropped() -> None:
    from omni.replay.harness import _describe
    from omni.tower.board import BoardPolicy, BoardSession
    from omni.tower.lane import ChangeKind, EndReason

    queue = InterleavedCardQueue()
    queue.ingest(CardFactory().pregame(_game("g1", GameStatus.PREGAME), now=T0))
    board = BoardSession(queue, QUAD, policy=BoardPolicy())
    reading = ClockReading(T0, MonotonicTime(0.0))
    board.cue(reading)
    queue.remove("g1:pregame")
    board.cue(reading)
    (dropped,) = board.changes()
    assert dropped.kind is ChangeKind.DROPPED and dropped.reason is EndReason.INVALIDATED
    assert _describe(dropped, None) == "drop    g1:pregame  (no longer current)"
    up = dataclasses.replace(dropped, kind=ChangeKind.UP)
    assert _describe(up, None) == "up      g1:pregame  (-)"
