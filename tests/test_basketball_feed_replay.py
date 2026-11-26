"""The three recorded BALLDONTLIE games read back through the feed deriver: the moments each fixture's README names,
and each game's captions by kind."""

from __future__ import annotations

from collections import Counter
from functools import cache

import pytest

from omni.core.enum import HomeAway
from omni.domain.basketball import CallChange, GameClock, PlayerCallChange, TimeoutLength
from omni.domain.basketball_reads import LinesRead, SlateRead
from omni.events.basketball import (
    BasketballCaption,
    CallReversed,
    ChallengeWon,
    FieldGoalMade,
    FreeThrow,
    FreeThrows,
    PersonalFoul,
    ShotValue,
    TechnicalFoul,
    TimeoutTaken,
)
from omni.events.basketball_feed import BasketballFeedDeriver, FeedCaption, FeedUpdate, TimeoutEnd, TimeoutOnset
from tests.basketball_replay import DET_NOP, GAMES, HOU_IND, UTA_LAL, replay

A, H = HomeAway.AWAY, HomeAway.HOME
LONG, SHORT = TimeoutLength.LONG, TimeoutLength.SHORT


@cache
def _updates(name: str) -> tuple[FeedUpdate, ...]:
    deriver = BasketballFeedDeriver()
    return tuple(deriver.read(read) for read in replay(name))


def _captions(name: str) -> list[FeedCaption]:
    return [caption for update in _updates(name) for caption in update.captions]


def _at(name: str, period: int, clock: str) -> list[BasketballCaption]:
    """The captions told for plays seen at one moment of the game clock."""
    return [
        told.caption
        for told in _captions(name)
        if told.period == period and told.clock is not None and told.clock.text == clock
    ]


def _onsets(name: str) -> list[TimeoutOnset]:
    return [onset for update in _updates(name) for onset in update.timeouts if isinstance(onset, TimeoutOnset)]


def _timeouts(name: str) -> list[tuple[HomeAway, int, str, TimeoutLength]]:
    return [
        (onset.side, onset.period, onset.clock.text if onset.clock is not None else "", onset.length)
        for onset in _onsets(name)
    ]


def test_the_reader_lays_the_changed_rows_over_the_last_and_reads_in_arrival_order() -> None:
    for name in GAMES:
        reads = replay(name)
        assert [read.at for read in reads] == sorted(read.at for read in reads)
        lines = [read for read in reads if isinstance(read, LinesRead)]
        assert all(len(later.lines) >= len(earlier.lines) for earlier, later in zip(lines, lines[1:]))
        assert any(isinstance(read, SlateRead) for read in reads)


def test_uta_at_lal_timeouts_with_their_classes_the_late_cap_left_out() -> None:
    assert _timeouts(UTA_LAL) == [
        (A, 1, "7:24", LONG),
        (H, 1, "3:23", LONG),
        (A, 2, "9:02", LONG),
        (H, 2, "6:15", LONG),
        (A, 3, "8:33", LONG),
        (H, 3, "4:25", LONG),
        (H, 4, "7:50", LONG),
        (H, 4, "6:35", LONG),
        (A, 4, "4:13", SHORT),
        (H, 4, "1:51", SHORT),
    ]
    ends = [end for update in _updates(UTA_LAL) for end in update.timeouts if isinstance(end, TimeoutEnd)]
    assert [end.onset for end in ends] == [onset.onset for onset in _onsets(UTA_LAL)]
    assert all(end.ended_at > end.onset for end in ends)
    assert not any(isinstance(caption, TimeoutTaken) for caption in _at(UTA_LAL, 4, "3:00"))  # UTA capped 3 to 2


def test_uta_at_lal_two_free_throws_in_one_read_are_one_trip() -> None:
    groups = [told.caption for told in _captions(UTA_LAL) if isinstance(told.caption, FreeThrows)]
    assert groups == [
        FreeThrows(side=H, shooter="W.Kessler", made=2, attempted=2, game_made=2, game_attempted=2),
        FreeThrows(side=A, shooter="D.Peterson", made=1, attempted=2, game_made=2, game_attempted=4),
    ]


def test_uta_at_lal_georges_lone_free_throws_with_no_foul_are_technicals_on_lal() -> None:
    technicals = [told for told in _captions(UTA_LAL) if isinstance(told.caption, TechnicalFoul)]
    assert [(told.period, told.clock) for told in technicals] == [(2, GameClock.of(4, 13)), (3, GameClock.of(9, 51))]
    assert [told.caption for told in technicals] == [
        TechnicalFoul(side=H, shooter="K.George", made=True, game_made=3, game_attempted=3),
        TechnicalFoul(side=H, shooter="K.George", made=True, game_made=6, game_attempted=7),
    ]


def test_det_at_nop_the_won_challenge_the_full_clock_drop_and_the_late_cap() -> None:
    assert _at(DET_NOP, 2, "1:58") == [TimeoutTaken(side=H, left=3), ChallengeWon(side=H)]
    assert (H, 2, "1:58", SHORT) in _timeouts(DET_NOP)
    assert _at(DET_NOP, 4, "12:00") == [] and all(onset.clock != GameClock.of(12, 0) for onset in _onsets(DET_NOP))
    assert not any(isinstance(caption, TimeoutTaken) for caption in _at(DET_NOP, 4, "2:57"))  # DET capped 4 to 2


def test_det_at_nop_a_basket_credited_to_a_teammate_is_no_new_caption() -> None:
    assert not any(isinstance(caption, FieldGoalMade) for caption in _at(DET_NOP, 3, ":29.0"))  # Reed's three to Joe
    # Jenkins's basket, still waiting for its assist, moved to Huerter with Jenkins assisting.
    assert [caption for caption in _at(DET_NOP, 2, "1:08") if isinstance(caption, FieldGoalMade)] == [
        FieldGoalMade(side=A, shooter="K.Huerter", value=ShotValue.TWO, assister="D.Jenkins")
    ]


def test_det_at_nop_a_lone_shooter_ahead_of_a_pair_took_the_technical_and_a_late_free_throw_is_its_trips() -> None:
    assert [
        caption for caption in _at(DET_NOP, 4, "5:56") if not isinstance(caption, (FieldGoalMade, PersonalFoul))
    ] == [
        FreeThrow(side=A, shooter="K.Huerter", place=1, made=True, game_made=5, game_attempted=5),
        TechnicalFoul(side=H, shooter="K.Huerter", made=True, game_made=5, game_attempted=5),
        FreeThrow(side=A, shooter="E.Harkless", place=1, made=True, game_made=3, game_attempted=3),
        FreeThrow(side=A, shooter="E.Harkless", place=2, made=True, game_made=4, game_attempted=4),
    ]
    # Fears's second free throw posted after the trip had ended: it reads as the trip's, not as a technical.
    assert [caption for caption in _at(DET_NOP, 1, "2:21") if isinstance(caption, (FreeThrow, TechnicalFoul))] == [
        FreeThrow(side=H, shooter="J.Fears", place=2, made=True, game_made=2, game_attempted=2)
    ]


def test_hou_at_ind_the_technical_inside_a_two_shooter_trip() -> None:
    assert [caption for caption in _at(HOU_IND, 3, "1:04") if not isinstance(caption, FieldGoalMade)][1:] == [
        FreeThrow(side=A, shooter="A.Thompson", place=1, made=True, game_made=2, game_attempted=3),
        TechnicalFoul(side=H, shooter="K.Durant", made=True, game_made=3, game_attempted=4),
        FreeThrow(side=A, shooter="A.Thompson", place=2, made=True, game_made=3, game_attempted=4),
    ]


def test_hou_at_ind_two_calls_taken_back_after_the_same_teams_count_dropped() -> None:
    reversals = [told for told in _captions(HOU_IND) if isinstance(told.caption, CallReversed)]
    assert [told.caption for told in reversals] == [
        CallReversed(change=PlayerCallChange(change=CallChange.FOUL_OFF, player="A.Thompson")),
        CallReversed(change=PlayerCallChange(change=CallChange.OFFENSIVE_FOUL_OFF, player="P.Siakam")),
    ]
    assert not any(isinstance(caption, TimeoutTaken) for caption in _at(HOU_IND, 4, "2:57"))  # HOU capped 3 to 2


def _player_sides(name: str) -> dict[str, HomeAway]:
    last = [read for read in replay(name) if isinstance(read, LinesRead)][-1]
    return {roster.line.name: roster.side for roster in last.lines}


def _reversed_after_own_drop(name: str) -> int:
    """Calls taken back on a player of a side whose count dropped at the same stopped clock."""
    sides = _player_sides(name)
    drops = {(onset.side, onset.period, onset.clock) for onset in _onsets(name)}
    return sum(
        1
        for told in _captions(name)
        if isinstance(told.caption, CallReversed)
        and (sides[told.caption.change.player], told.period, told.clock) in drops
    )


@pytest.mark.parametrize("name, count", [(UTA_LAL, 0), (DET_NOP, 0), (HOU_IND, 2)])
def test_drops_followed_by_the_same_teams_call_taken_back(name: str, count: int) -> None:
    assert _reversed_after_own_drop(name) == count


_TOTALS = {
    UTA_LAL: {
        "FieldGoalMade": 69,
        "FreeThrow": 49,
        "FreeThrows": 2,
        "PersonalFoul": 39,
        "TechnicalFoul": 2,
        "TimeoutTaken": 10,
    },
    DET_NOP: {
        "FieldGoalMade": 68,
        "FreeThrow": 51,
        "PersonalFoul": 42,
        "TechnicalFoul": 2,
        "TimeoutTaken": 7,
        "ChallengeWon": 1,
    },
    HOU_IND: {
        "FieldGoalMade": 39,
        "FreeThrow": 16,
        "PersonalFoul": 20,
        "TechnicalFoul": 1,
        "TimeoutTaken": 7,
        "CallReversed": 2,
    },
}


@pytest.mark.parametrize("name", GAMES)
def test_each_games_captions_by_kind(name: str) -> None:
    totals = Counter(type(told.caption).__name__ for told in _captions(name))
    assert dict(totals) == _TOTALS[name]
    assert _updates(name)[-1].held == ()
