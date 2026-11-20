"""The three recorded games of 2026-11-18 replayed through the BALLDONTLIE provider: every read typed, in arrival
order, and the moments the fixtures' README names read as they happened."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeVar

import pytest

from omni.core.clock import MonotonicTime
from omni.core.enum import HomeAway
from omni.domain.basketball import BasketballGameState, BasketballPhase, GameClock
from omni.domain.basketball_reads import LinesRead, PlayerKey
from omni.providers.balldontlie import BalldontlieProvider, LinesPoll, SlatePoll
from omni.providers.nba_teams import NbaTeamRegistry
from tests.balldontlie_replay import GAMES, BdlReplay, Kind, records

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Replayed:
    """What a replay handed on: every poll in arrival order, and the reads of its game."""

    polls: tuple[SlatePoll | LinesPoll, ...]

    @property
    def states(self) -> list[BasketballGameState]:
        return [game.read.state for poll in self.slates for game in poll.games if game.read is not None]

    @property
    def slates(self) -> list[SlatePoll]:
        return [poll for poll in self.polls if isinstance(poll, SlatePoll)]

    @property
    def lines(self) -> list[LinesRead]:
        return [game.read for poll in self.polls if isinstance(poll, LinesPoll) for game in poll.games]


def _replay(name: str, page_size: int = 100) -> Replayed:
    replay = BdlReplay(name, page_size=page_size)
    provider = BalldontlieProvider(NbaTeamRegistry.default(), fetch=replay.fetch, clock=replay.clock)
    polls: list[SlatePoll | LinesPoll] = []
    for step in replay:
        if step.kind is Kind.SLATE:
            polls.append(provider.poll_slate(step.day))
        else:
            polls.append(provider.poll_lines([step.game]))
    return Replayed(tuple(polls))


REPLAYS = {name: _replay(name) for name in GAMES}


def _runs(values: Iterable[T]) -> list[T]:
    """The values with each run of repeats cut to one."""
    runs: list[T] = []
    for value in values:
        if not runs or runs[-1] != value:
            runs.append(value)
    return runs


def _clock(minutes: int, seconds: int) -> GameClock:
    return GameClock.of(minutes, seconds)


@pytest.mark.parametrize("name", GAMES)
def test_every_read_is_typed_with_nothing_unreadable(name: str) -> None:
    replayed = REPLAYS[name]
    assert all(poll.complete for poll in replayed.polls)
    slates, lines = records(name, Kind.SLATE), records(name, Kind.LINES)
    assert len(replayed.slates) == len(slates) and len(replayed.lines) == len(lines)
    assert all(len(poll.games) == 1 for poll in replayed.slates)


@pytest.mark.parametrize("name", GAMES)
def test_reads_come_out_in_arrival_order_on_the_recorders_clock(name: str) -> None:
    replayed = REPLAYS[name]
    arrivals = [poll.at for poll in replayed.polls]
    assert arrivals == sorted(arrivals)
    recorded = sorted(record["monotonic"] for kind in Kind for record in records(name, kind))
    assert [at.seconds for at in arrivals] == pytest.approx(recorded)
    assert [read.at for read in replayed.lines] == sorted(read.at for read in replayed.lines)


@pytest.mark.parametrize("name", GAMES)
def test_the_lines_keep_every_player_who_has_appeared(name: str) -> None:
    lines = REPLAYS[name].lines
    counts = [len(read.lines) for read in lines]
    assert counts == sorted(counts)
    players = {row["player"]["id"] for record in records(name, Kind.LINES) for row in record["changed"]}
    assert {roster.key.value for roster in lines[-1].lines} == players
    assert [roster.key for roster in lines[-1].lines] == [
        PlayerKey(row["player"]["id"])
        for row in sorted(
            {row["player"]["id"]: row for record in records(name, Kind.LINES) for row in record["changed"]}.values(),
            key=lambda row: row["id"],
        )
    ]


@pytest.mark.parametrize("name", GAMES)
def test_each_lines_read_takes_the_stat_rows_own_period_and_clock(name: str) -> None:
    for read, record in zip(REPLAYS[name].lines, records(name, Kind.LINES), strict=True):
        view = record["changed"][0]["game"]
        assert read.period == view["period"]
        assert (read.clock is None) == (not str(view["time"]).startswith("Q"))


@pytest.mark.parametrize("name", GAMES)
def test_paging_the_rebuilt_pages_changes_no_read(name: str) -> None:
    paged = _replay(name, page_size=7)
    assert [read.lines for read in paged.lines] == [read.lines for read in REPLAYS[name].lines]
    assert max(len(poll.exchanges) for poll in paged.polls) > 1


def test_uta_at_lal_from_before_the_tip_to_the_final() -> None:
    replayed = REPLAYS["uta-lal"]
    first = replayed.slates[0].games[0]
    assert first.phase is BasketballPhase.SCHEDULED and first.pregame is not None and first.read is None
    assert (first.game.away.abbreviation, first.game.home.abbreviation) == ("UTA", "LAL")
    phases = _runs(state.phase for state in replayed.states)
    live, end, half = BasketballPhase.LIVE, BasketballPhase.END_OF_PERIOD, BasketballPhase.HALFTIME
    assert phases == [live, end, live, half, live, end, live, end, BasketballPhase.FINAL]
    assert _runs((state.phase, state.period) for state in replayed.states) == [
        (live, 1),
        (end, 1),
        (live, 2),
        (half, 2),
        (live, 3),
        (end, 3),
        (live, 4),
        (end, 4),
        (BasketballPhase.FINAL, 4),
    ]
    final = replayed.states[-1]
    assert (final.away.score, final.home.score, final.winner) == (109, 97, HomeAway.AWAY)
    assert (final.away.periods, final.home.periods) == ((29, 27, 22, 31), (34, 20, 22, 21))


def test_uta_at_lal_timeouts_left_follow_the_slate_rows() -> None:
    states = REPLAYS["uta-lal"].states
    rows = [record["body"]["data"][0] for record in records("uta-lal", Kind.SLATE)]
    started = [row for row in rows if row["status_state"] != "scheduled"]
    assert [state.away.timeouts for state in states] == [row["visitor_timeouts_remaining"] for row in started]
    assert [state.home.timeouts for state in states] == [row["home_timeouts_remaining"] for row in started]
    assert _runs(state.away.timeouts for state in states) == [7, 6, 5, 4, 3, 2]
    assert _runs(state.home.timeouts for state in states) == [7, 6, 5, 4, 3, 2, 1]


def test_det_at_nop_gets_its_challenge_timeout_back_at_q2_1_58() -> None:
    states = REPLAYS["det-nop"].states
    moments = _runs((state.home.timeouts, state.period, state.clock) for state in states)
    fell = moments.index((3, 2, _clock(1, 58)))
    assert moments[fell - 1][0] == 4 and moments[fell + 1] == (4, 2, _clock(1, 58))
    assert _runs(state.home.timeouts for state in states)[3:6] == [4, 3, 4]


def test_hou_at_ind_thompsons_foul_is_taken_back_at_q3_8_17() -> None:
    lines = REPLAYS["hou-ind"].lines
    thompson = PlayerKey(56677825)
    fouls = [
        (next(roster.line.fouls for roster in read.lines if roster.key == thompson), read.period, read.clock)
        for read in lines
    ]
    series = _runs(fouls)
    added = series.index((2, 3, _clock(8, 17)))
    assert series[added - 1][0] == 1 and series[added + 1] == (1, 3, _clock(8, 17))
    (name,) = {roster.line.name for read in lines for roster in read.lines if roster.key == thompson}
    assert name == "A.Thompson"


def test_names_come_out_in_the_boards_short_form() -> None:
    names = {roster.line.name for name in GAMES for roster in REPLAYS[name].lines[-1].lines}
    assert {"C.Mañon", "J.Jackson", "T.Murphy", "R.Holland", "J.Pierre", "T.McConnell", "J.Tate"} <= names
    assert all("." in name and " Jr" not in name for name in names)


def test_the_replay_answers_only_in_turn() -> None:
    replay = BdlReplay("hou-ind")
    with pytest.raises(AssertionError, match="out of turn"):
        replay.fetch("/v1/games", ())
    step = next(iter(replay))
    assert step.kind is Kind.SLATE and replay.clock.monotonic() == MonotonicTime(step.at)
    with pytest.raises(AssertionError, match="what the recorder asked"):
        replay.fetch("/v1/games", (("dates[]", "2026-11-19"), ("per_page", "100")))
