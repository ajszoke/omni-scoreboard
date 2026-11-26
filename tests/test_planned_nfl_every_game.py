"""On Builder each other live NFL game is read on its own, plays and all, as a favorite's is but every 30 s at normal:
its card gets its timeouts, challenges and play line, and its plays are offered. On Free nothing changes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import PipelineResult
from omni.app.planned_nfl import NFL_POLICIES, NflSchedulePolicy, PlannedNfl
from omni.app.poll_plan import PollPlan, PollPolicy, PollSource
from omni.app.quota import BillingMonth, MonthUsage, NflMetaPlan, QuotaLedger, account_limits
from omni.cards.football import LiveFootballCardPayload
from omni.core.clock import FakeClock
from omni.core.enum import GameStatus
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.football import FootballField, FootballGameState, TeamChallenges, TeamTimeouts
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import HALFTIME_HOLD, NFLMETA_SOURCE, NflMetaLiveProvider
from omni.queue.delay_policy import DelayPolicy
from omni.queue.scheduler import InterleavedCardQueue
from tests.test_planned_nfl import (
    CAR_ATL,
    EARLY,
    MIN_GB,
    NFL,
    PIT_CIN,
    SUNDAY,
    S,
    _Board,
    _board,
    _change,
    _end,
    _et,
    _game,
    _NflMeta,
    _row,
    _scope,
    _start,
    _sunday,
)

BUILDER = NflMetaPlan.BUILDER
_TOUCHDOWN = "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN."


def test_only_builder_s_month_carries_each_game_read_on_its_own() -> None:
    assert [plan.every_game for plan in NflMetaPlan] == [False, True]


# --- the reads ----------------------------------------------------------------------------------------------------


def test_on_builder_each_other_live_game_is_read_on_its_own_every_30_s_with_its_plays_and_not_with_the_others() -> None:
    board = _board(_et(SUNDAY, 12, 59, 55), tier=BUILDER)
    _sunday(board, CAR_ATL, PIT_CIN, MIN_GB)
    board.run(EARLY)
    _start(board, 101, 102, 103)
    board.run(_et(SUNDAY, 13, 0, 35))
    board.nflmeta.plays[102] = [_change("p1", 1)]
    board.nflmeta.set(102, play_sync={"status": "matched", "last_play_id": "p1"}, clock="14:21")
    board.run(_et(SUNDAY, 13, 1, 35))
    own = [EARLY + n * 30 * S for n in range(4)]
    assert board.nflmeta.asked(game_id=102) == board.nflmeta.asked(game_id=103) == own
    assert board.nflmeta.play_reads(102) == [own[0], own[2]]  # the first look, then the new play
    assert board.nflmeta.play_reads(103) == [own[0]]
    assert board.nflmeta.asked(game_id=101) == [EARLY + n * 10 * S for n in range(10)]  # the favorite at its own pace
    assert [query for _, _, query in board.nflmeta.requests if "game_ids" in query] == []  # its score is read once
    readings = board.planned.take(_game(board, 102))
    assert [reading.arrived.utc for reading in readings] == [_et(SUNDAY, 12, 59, 55), *own]
    assert {reading.current_for for reading in readings[1:]} == {60 * S}  # two of its own 30 s
    assert [len(reading.snapshot.events) for reading in readings] == [0, 0, 0, 1, 0]


def test_on_free_another_game_is_read_only_with_the_others_and_never_its_plays() -> None:
    board = _board(_et(SUNDAY, 12, 59, 55))
    _sunday(board, CAR_ATL, PIT_CIN)
    board.run(EARLY)
    _start(board, 101, 102)
    board.nflmeta.plays[102] = [_change("p1", 1)]
    board.nflmeta.set(102, play_sync={"status": "matched", "last_play_id": "p1"})
    board.run(_et(SUNDAY, 13, 1, 35))
    assert board.nflmeta.asked(game_id=102) == [] and board.nflmeta.play_reads(102) == []
    assert board.nflmeta.asked(phase="in", game_ids="102") == [EARLY + n * 45 * S for n in range(3)]


def test_on_builder_another_game_isn_t_read_through_its_halftime_hold() -> None:
    board = _board(_et(SUNDAY, 14, 20), tier=BUILDER)
    board.nflmeta.rows[102] = _row(*PIT_CIN, phase="in", play_sync={"status": "matched", "last_play_id": "p1"})
    board.nflmeta.plays[102] = [_change("p1", 1)]
    board.run(_et(SUNDAY, 14, 20, 10))  # the day list, then the game's first read on its own
    board.nflmeta.plays[102].append(_change("p2", 2))
    board.nflmeta.set(
        102, period=2, clock="0:00", status_detail="Halftime", play_sync={"status": "matched", "last_play_id": "p2"}
    )
    board.run(_et(SUNDAY, 14, 33, 35))
    seen = _et(SUNDAY, 14, 20, 35)
    assert board.nflmeta.asked(game_id=102) == [_et(SUNDAY, 14, 20, 5), seen]  # the read that saw halftime; none since
    latest = board.planned.latest(_game(board, 102))
    assert latest is not None and latest.snapshot.break_ends_at == seen + HALFTIME_HOLD
    board.nflmeta.set(102, period=3, clock="15:00", status_detail="15:00 - 3rd")
    board.run(_et(SUNDAY, 14, 34, 10))
    assert board.nflmeta.asked(game_id=102)[2:] == [seen + HALFTIME_HOLD, seen + HALFTIME_HOLD + 30 * S]


def test_on_builder_another_game_s_final_is_read_through_its_drain_then_never_again() -> None:
    board = _board(_et(SUNDAY, 16, 8, 55), tier=BUILDER)
    board.nflmeta.rows[102] = _row(*PIT_CIN, phase="in", period=4)
    board.run(_et(SUNDAY, 16, 10))
    _end(board, 102)
    board.run(_et(SUNDAY, 16, 20))
    # Every 30 s from its first read; the final seen at 16:10, its drain's last read two minutes on.
    assert board.nflmeta.asked(game_id=102) == [_et(SUNDAY, 16, 9) + n * 30 * S for n in range(7)]
    assert [query for _, _, query in board.nflmeta.requests if "game_ids" in query] == []
    assert board.status(SUNDAY, 102) is GameStatus.FINAL


def test_on_builder_the_other_games_own_reads_are_normal_held_back_low_in_the_month_as_the_favorite_s_go_on() -> None:
    usage = {NFL: MonthUsage(BillingMonth(2026, 11), requests=72_000)}  # 4% left: normal is held back
    ledger = QuotaLedger(account_limits(BUILDER), usage=usage)
    board = _board(_et(SUNDAY, 12, 59, 55), ledger=ledger, tier=BUILDER)
    _sunday(board, CAR_ATL, PIT_CIN)
    board.run(EARLY)
    _start(board, 101, 102)
    board.run(_et(SUNDAY, 13, 1))
    assert board.nflmeta.asked(game_id=102) == [] and len(board.nflmeta.asked(game_id=101)) == 6
    reason = board.planned.held_back(_scope(SUNDAY))
    assert reason is not None and reason.endswith("normal held back")


def test_a_plan_reading_a_game_on_its_own_at_another_pace_than_nfl_s_policy_doesn_t_build_on_builder() -> None:
    clock = FakeClock(EARLY)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=_NflMeta(clock))
    slower = {**NFL_POLICIES, PollSource.FOLLOWED_GAME: PollPolicy(every=45 * S, keep_for=90 * S)}
    with pytest.raises(ValueError, match="reads a game on its own every 45 s, not every 30 s as NFL's policy says"):
        PlannedNfl(PollPlan(slower), clock, provider, tier=BUILDER)
    PlannedNfl(PollPlan(slower), clock, provider)  # Free reads no game on its own but a favorite's
    PlannedNfl(PollPlan(slower), clock, provider, tier=BUILDER, policy=NflSchedulePolicy(every_game=45 * S))
    with pytest.raises(ValueError, match="must be positive"):
        NflSchedulePolicy(every_game=timedelta(0))


# --- the card -----------------------------------------------------------------------------------------------------


def _history(count: int) -> list[dict[str, Any]]:
    """A game's plays so far, ending on PIT's first timeout of the second half: more than one page of the feed."""
    plays = [_change(f"p{number}", number) for number in range(1, count)]
    timeout = _change(f"p{count}", count)
    timeout["play"].update(play_type="timeout", quarter=4, description="Timeout #1 by PIT at 12:10.")
    return [*plays, timeout]


def _touchdown(number: int) -> dict[str, Any]:
    change = _change(f"p{number}", number)
    change["play"].update(
        play_type="passing_touchdown",
        quarter=4,
        description=_TOUCHDOWN,
        touchdown=True,
        posteam_abbr="CIN",
        yards_gained=2,
    )
    return change


def _pass(board: _Board, pipeline: LiveFootballPipeline) -> PipelineResult:
    board.step()
    games = [contest for contest in board.store.contests if isinstance(contest, TeamGame)]
    return pipeline.refresh(games, at=board.clock.read())


def _card(queue: InterleavedCardQueue) -> LiveFootballCardPayload:
    card = queue.card("nfl:nflmeta:102:live")
    assert card is not None and isinstance(card.payload, LiveFootballCardPayload)
    return card.payload


def _mid_game(tier: NflMetaPlan) -> tuple[_Board, LiveFootballPipeline, InterleavedCardQueue]:
    """A board started at 14:00 with PIT@CIN scoreless in its fourth quarter: 120 plays in, an injury two minutes
    old. A touchdown then is a major event, offered for promotion."""
    board = _board(_et(SUNDAY, 14), tier=tier)
    injury = {
        "player_name": "A.Player",
        "team_abbr": "CIN",
        "injury": "Knee",
        "state": "questionable_to_return",
        "reported_at": _et(SUNDAY, 13, 58).isoformat(),
    }
    board.nflmeta.rows[102] = _row(
        *PIT_CIN, phase="in", period=4, play_sync={"status": "matched", "last_play_id": "p120"}, injuries=[injury]
    )
    board.nflmeta.plays[102] = _history(120)
    queue = InterleavedCardQueue()
    pipeline = LiveFootballPipeline(
        feeds=board.planned, delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)), queue=queue
    )
    return board, pipeline, queue


def _counts(state: FootballGameState) -> tuple[TeamTimeouts | None, TeamChallenges | None, bool]:
    return state.timeouts, state.challenges, state.known(FootballField.TIMEOUTS)


def test_on_builder_another_game_s_card_gets_its_counts_and_its_later_plays_caption_it_and_are_offered() -> None:
    board, pipeline, queue = _mid_game(BUILDER)
    results = [_pass(board, pipeline)]  # the day list: the game in progress, its card from the score alone
    assert _counts(_card(queue).state) == (None, None, False)
    board.clock.advance(5 * S)
    results.append(_pass(board, pipeline))  # its first read on its own: the whole feed so far, in two pages
    assert len(board.nflmeta.play_reads(102)) == 2
    first = _card(queue)
    assert _counts(first.state) == (TeamTimeouts(away=2, home=3), TeamChallenges(away=2, home=2), True)
    assert first.last_play is None  # the plays so far (the timeout, the injury) are from before the board read it
    latest = board.planned.latest(_game(board, 102))
    assert latest is not None and latest.snapshot.events == ()  # the provider's catch-up hands over nothing
    board.nflmeta.plays[102].append(_touchdown(121))
    board.nflmeta.set(
        102, home_team={"abbr": "CIN", "score": 6}, play_sync={"status": "matched", "last_play_id": "p121"}
    )
    until = _et(SUNDAY, 14, 1, 30)
    while board.clock.now() < until:
        board.clock.advance(5 * S)
        results.append(_pass(board, pipeline))
    assert board.nflmeta.play_reads(102)[-1] == _et(SUNDAY, 14, 0, 35)  # the next read on its own: the touchdown
    card = _card(queue)
    assert card.last_play is not None and card.last_play.headline == "TOUCHDOWN CIN"
    assert card.state.home_score == 6 and card.state.known(FootballField.CHALLENGES)
    offered = {offer.key.play for result in results for offer in result.policy.offers}
    assert offered == {"121"}  # the touchdown alone: nothing from before the board read the game


def test_on_free_another_game_s_card_has_no_counts_and_no_line() -> None:
    board, pipeline, queue = _mid_game(NflMetaPlan.FREE)
    board.nflmeta.set(102, home_team={"abbr": "CIN", "score": 6})
    until = _et(SUNDAY, 14, 1, 30)
    offered = 0
    while board.clock.now() < until:
        offered += len(_pass(board, pipeline).policy.offers)
        board.clock.advance(5 * S)
    card = _card(queue)
    assert card.state.home_score == 6 and _counts(card.state) == (None, None, False)
    assert card.last_play is None and offered == 0 and board.nflmeta.play_reads(102) == []
