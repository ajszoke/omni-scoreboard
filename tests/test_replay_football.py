"""Football replay: the trace format, the replay feed, and the board's decisions on PIT@CIN."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omni.app.football_feed import FeedResult
from omni.core.enum import GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.football import (
    FootballGameState,
    FootballInterval,
    PlaySummary,
    PlayTone,
    SetPiece,
    TeamChallenges,
    TeamTimeouts,
)
from omni.events.football import FootballEvent, FootballEventKind
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import FeedNote, FeedNoteKind, NflLiveSnapshot, NflPhase
from omni.replay.football import (
    REPLAY_SOURCE,
    FootballTrace,
    ReplayFeed,
    _Cards,
    answer_record,
    header_record,
    load_trace,
    replay_board,
)

FIXTURES = Path(__file__).parent / "fixtures" / "replay"
PIT_CIN = FIXTURES / "football-2026-11-15-pit-cin.trace.jsonl"
START = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
TEAMS = NflTeamRegistry.default(source=REPLAY_SOURCE)
GAME = TeamGame(
    id=LeagueScopedId(League.NFL, REPLAY_SOURCE, "test"),
    league=League.NFL,
    status=GameStatus.PREGAME,
    scheduled_start=START + timedelta(minutes=10),
    away=TEAMS.resolve("PIT"),
    home=TEAMS.resolve("CIN"),
)


def _play(key: str, *, players: bool = True, kind: FootballEventKind = FootballEventKind.PLAY) -> FootballEvent:
    return FootballEvent(
        key=key,
        kind=kind,
        summary=PlaySummary(tone=PlayTone.STANDARD, headline=f"PLAY {key}", detail=f"detail {key}"),
        players_line=f"players {key}" if players else None,
    )


def _answer(
    at: timedelta,
    state: FootballGameState | None,
    *events: FootballEvent,
    phase: NflPhase = NflPhase.IN,
    **fields: object,
) -> FeedResult:
    received = START + at
    snapshot = NflLiveSnapshot(
        game=GAME,
        phase=phase,
        state=state,
        events=events,
        caught_up=bool(fields.pop("caught_up", True)),
        break_ends_at=fields.pop("break_ends_at", None),  # type: ignore[arg-type]
        observed_at=fields.pop("observed_at", None),  # type: ignore[arg-type]
        poll_every=DurationSeconds(10),
        settled=bool(fields.pop("settled", False)),
        notes=fields.pop("notes", ()),  # type: ignore[arg-type]
    )
    return FeedResult(asked_at=received, received_at=received, next_ask=received + 10 * S, snapshot=snapshot, **fields)  # type: ignore[arg-type]


def _state(**fields: object) -> FootballGameState:
    base: dict[str, object] = {"period": 1, "clock_seconds": 900, "away_score": 0, "home_score": 0}
    return FootballGameState(**{**base, **fields})  # type: ignore[arg-type]


def _trace(*answers: FeedResult) -> list[str]:
    header = header_record(GAME, name="test", start=START)
    return [json.dumps(header)] + [json.dumps(answer_record(answer, START)) for answer in answers]


def test_the_board_decides_pit_cin_as_it_did() -> None:
    """The board's decisions on the minimized PIT@CIN trace, against the baseline taken before any change."""
    decisions = replay_board(load_trace(PIT_CIN.read_text().splitlines()))
    expected = (FIXTURES / "football-2026-11-15-pit-cin.decisions.txt").read_text().splitlines()
    assert decisions == expected


def test_a_trace_reads_back_every_answer_as_written() -> None:
    full = _state(
        period=2,
        clock_seconds=431,
        away_score=7,
        home_score=3,
        possession=HomeAway.HOME,
        down=3,
        distance=8,
        yards_to_goal=41,
        timeouts=TeamTimeouts(away=2, home=3),
        challenges=TeamChallenges(away=2, home=1),
    )
    kickoff = _state(period=3, away_score=7, home_score=3, possession=HomeAway.AWAY, set_piece=SetPiece.KICKOFF)
    half = _state(period=2, clock_seconds=0, away_score=7, home_score=3, interval=FootballInterval.HALFTIME)
    stoppage = FootballEvent(
        key="t", kind=FootballEventKind.TV_TIMEOUT, summary=PlaySummary(tone=PlayTone.ALERT, headline="TV TIMEOUT")
    )
    late = START + 70 * S
    answers = [
        _answer(0 * S, None, phase=NflPhase.PRE, caught_up=False),
        _answer(10 * S, full, _play("1"), stoppage, observed_at=START + 2 * S),
        FeedResult(
            asked_at=START + 20 * S, received_at=START + 21 * S, next_ask=START + 50 * S, snapshot=None, error="down"
        ),
        _answer(
            30 * S, half, break_ends_at=START + 900 * S, holding=True, notes=(FeedNote(FeedNoteKind.LATE_PLAY, "x"),)
        ),
        _answer(40 * S, half, held=True, holding=True),
        _answer(late - START, kickoff, phase=NflPhase.POST, settled=True),
    ]
    trace = load_trace(_trace(*answers))
    assert trace.start == START and trace.game.away.abbreviation == "PIT" and trace.game.home.abbreviation == "CIN"
    assert trace.game.scheduled_start == GAME.scheduled_start
    for read, written in zip(trace.answers, answers, strict=True):
        assert (read.asked_at, read.received_at, read.next_ask) == (
            written.asked_at,
            written.received_at,
            written.next_ask,
        )
        assert (read.error, read.held, read.holding) == (written.error, written.held, written.holding)
        if written.snapshot is None:
            assert read.snapshot is None
            continue
        assert read.snapshot is not None
        assert read.snapshot.state == written.snapshot.state
        assert read.snapshot.events == written.snapshot.events
        assert read.snapshot.notes == written.snapshot.notes
        assert read.snapshot.phase is written.snapshot.phase
        assert (
            read.snapshot.game.status
            is {"pre": GameStatus.PREGAME, "in": GameStatus.LIVE, "post": GameStatus.FINAL}[
                written.snapshot.phase.value
            ]
        )
        for name in ("caught_up", "settled", "break_ends_at", "observed_at", "poll_every"):
            assert getattr(read.snapshot, name) == getattr(written.snapshot, name)


def test_only_a_football_trace_with_offsets_on_its_instants_loads() -> None:
    with pytest.raises(ValueError, match="not a football trace"):
        load_trace([])
    with pytest.raises(ValueError, match="not a football trace"):
        load_trace([json.dumps({"football_trace": 99})])
    header = header_record(GAME, name="test", start=START) | {"start": "2026-11-16T01:00:00"}
    with pytest.raises(ValueError, match="carry their offset"):
        load_trace([json.dumps(header)])


def test_the_replay_feed_hands_over_answers_as_they_arrive() -> None:
    answers = [_answer(10 * S, _state()), _answer(20 * S, _state()), _answer(20 * S, _state(clock_seconds=880))]
    feed = ReplayFeed(answers)
    assert feed.collect(START + 9 * S) == []
    assert feed.collect(START + 10 * S) == answers[:1]
    assert feed.collect(START + 25 * S) == answers[1:]
    assert feed.collect(START + 60 * S) == []


def test_a_replay_lists_each_change_of_strip_and_field_and_each_drop() -> None:
    snap = _state(possession=HomeAway.AWAY, down=1, distance=10, yards_to_goal=75)
    try_after = _state(away_score=6)
    lines = replay_board(
        load_trace(
            _trace(
                _answer(0 * S, _state(interval=FootballInterval.END_OF_QUARTER), _play("a")),  # starts in a break
                _answer(10 * S, _state(possession=HomeAway.AWAY, set_piece=SetPiece.KICKOFF, yards_to_goal=65)),
                _answer(20 * S, snap, _play("1", players=False)),
                _answer(40 * S, try_after, _play("2")),
            )
        ),
        tail=40 * S,
    )
    assert lines == [
        "     0.00  drop   PLAY a | detail a (unseen: during a break)",
        "     0.00  field  Q1 0-0 end_of_quarter",
        "     0.00  strip  -",
        "    10.00  field  Q1 0-0 kickoff away",
        "    20.00  field  Q1 0-0 away 1&10 @75",
        "    20.00  strip  PLAY 1 | detail 1",
        "    40.00  field  Q1 6-0 -",
        "    40.00  strip  PLAY 2 | detail 2",
        "    60.00  strip  PLAY 2 | players 2",
        "    80.00  strip  -",
    ]


def test_a_replay_of_no_answers_decides_nothing() -> None:
    assert replay_board(FootballTrace(start=START, game=GAME, answers=()), tail=S) == []


def test_the_stand_in_renderer_draws_for_its_profile() -> None:
    assert _Cards(PanelProfile.SINGLE_64X32).supported_profiles == frozenset({PanelProfile.SINGLE_64X32})
