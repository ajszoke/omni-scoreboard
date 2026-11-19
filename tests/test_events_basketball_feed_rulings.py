"""The NBA feed deriver's fouls and rulings: personal fouls and calls taken back, technicals told from their free
throws, timeouts with their length class, onset and end, and won challenges."""

from __future__ import annotations

import pytest

from omni.core.enum import HomeAway
from omni.domain.basketball import BasketballPhase, CallChange, GameClock, PlayerCallChange, TimeoutLength
from omni.events.basketball import (
    CallReversed,
    ChallengeWon,
    FreeThrow,
    FreeThrows,
    PersonalFoul,
    TechnicalFoul,
    TimeoutTaken,
)
from omni.events.basketball_feed import FeedUpdate, HeldCaption, HeldReason, TimeoutEnd, TimeoutOnset
from tests.basketball_feed_game import A, H, Game, clock, told

LONG, SHORT = TimeoutLength.LONG, TimeoutLength.SHORT


def _game(**kwargs: int) -> Game:
    """POR (away: Sharpe, Avdija, Grant) at MIN (home: Edwards, Reid, Gobert), past the baseline read."""
    game = Game(
        (1, A, "S.Sharpe"),
        (2, A, "D.Avdija"),
        (3, A, "J.Grant"),
        (11, H, "A.Edwards"),
        (12, H, "N.Reid"),
        (13, H, "R.Gobert"),
        **kwargs,
    )
    game.lines()
    return game


def _shot(name: str, place: int, made: int, attempted: int, *, side: HomeAway = A) -> FreeThrow:
    return FreeThrow(side=side, shooter=name, place=place, made=True, game_made=made, game_attempted=attempted)


def _technical(name: str, made: int, attempted: int, *, charged: HomeAway = H) -> TechnicalFoul:
    return TechnicalFoul(side=charged, shooter=name, made=True, game_made=made, game_attempted=attempted)


# --- personal fouls and calls taken back --------------------------------------------------------------------


def test_a_foul_is_told_with_the_players_count_and_a_substitute_who_enters_with_one_fouled() -> None:
    game = _game()
    game.bump(11, pf=1)
    assert told(game.lines()) == [PersonalFoul(side=H, player="A.Edwards", count=1)]
    game.bump(11, pf=2)
    assert told(game.lines()) == [
        PersonalFoul(side=H, player="A.Edwards", count=2),
        PersonalFoul(side=H, player="A.Edwards", count=3),
    ]
    game.join(14, H, "J.Randle", pf=1)
    assert told(game.lines()) == [PersonalFoul(side=H, player="J.Randle", count=1)]


@pytest.mark.parametrize(
    "counts, change",
    [
        ({"pf": -1}, CallChange.FOUL_OFF),
        ({"pf": -1, "blk": 1}, CallChange.FOUL_TO_BLOCK),
        ({"pf": -1, "tov": -1}, CallChange.OFFENSIVE_FOUL_OFF),
    ],
    ids=["foul-off", "foul-to-block", "offensive-foul-off"],
)
def test_a_call_taken_back_with_no_challenge_is_a_reversal_naming_no_challenger(
    counts: dict[str, int], change: CallChange
) -> None:
    game = _game()
    game.bump(11, pf=2, tov=1)
    game.lines()
    game.bump(11, **counts)
    assert told(game.lines()) == [CallReversed(change=PlayerCallChange(change=change, player="A.Edwards"))]


def test_other_counts_corrected_down_are_no_caption() -> None:
    game = _game()
    game.bump(11, pf=3, dreb=2, ast=2, blk=1, stl=1)
    game.lines()
    game.bump(11, pf=-2, dreb=-1, ast=-1, blk=-1, stl=-1)
    assert game.lines().captions == ()


# --- a foul and its free throws -----------------------------------------------------------------------------


def test_a_foul_comes_before_its_free_throws_in_one_read_whatever_the_rows_order() -> None:
    game = _game()
    game.bump(1, fta=1, ftm=1)  # the shooter's row comes first
    game.bump(11, pf=1)
    assert told(game.lines()) == [PersonalFoul(side=H, player="A.Edwards", count=1), _shot("S.Sharpe", 1, 1, 1)]


def test_a_foul_that_posts_a_read_late_is_no_technical_and_is_told_first() -> None:
    game = _game()
    game.bump(1, fta=1, ftm=1)
    first = game.lines()
    assert told(first) == []
    assert first.held == (
        HeldCaption(reason=HeldReason.FOUL_CHECK, caption=_shot("S.Sharpe", 1, 1, 1), seen_at=first.at),
    )
    game.bump(11, pf=1)
    late = game.lines()
    assert told(late) == [PersonalFoul(side=H, player="A.Edwards", count=1), _shot("S.Sharpe", 1, 1, 1)]
    assert late.captions[1].seen_at == first.at


def test_a_pending_foul_outlives_a_short_run_of_the_clock() -> None:
    game = _game(at_clock=clock(1, 31))
    game.bump(11, pf=1)
    game.lines()
    game.slate(at_clock=clock(1, 28))
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 1, 1, 1)]


# --- technicals ---------------------------------------------------------------------------------------------


def test_a_lone_free_throw_with_no_foul_behind_it_is_a_technical_charged_to_the_other_side() -> None:
    game = _game()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    unchanged = game.lines()  # a read with no count changed is no evidence either way
    assert told(unchanged) == [] and unchanged.held[0].reason is HeldReason.FOUL_CHECK
    assert told(game.slate()) == []
    game.bump(12, dreb=1)
    assert told(game.lines()) == [_technical("S.Sharpe", 1, 1)]


def test_a_technical_in_a_trip_between_a_foul_shooters_free_throws() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 1, 1, 1)]
    game.bump(2, fta=1, ftm=1)
    held = game.lines()
    assert told(held) == [] and [h.reason for h in held.held] == [HeldReason.FOUL_CHECK]
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_technical("D.Avdija", 1, 1), _shot("S.Sharpe", 2, 2, 2)]


def test_the_lone_free_throw_ahead_of_a_foul_shooters_pair_was_the_technical() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 1, 1, 1)]  # it looked like the foul's
    game.bump(2, fta=1, ftm=1)
    game.lines()
    game.bump(2, fta=1, ftm=1)
    assert told(game.lines()) == [_technical("S.Sharpe", 1, 1), _shot("D.Avdija", 1, 1, 1), _shot("D.Avdija", 2, 2, 2)]


def test_a_lone_free_throw_after_a_foul_shooters_pair_is_a_technical() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    for _ in range(2):
        game.bump(1, fta=1, ftm=1)
        game.lines()
    game.bump(2, fta=1, ftm=1)
    game.lines()
    game.bump(12, dreb=1)
    assert told(game.lines()) == [_technical("D.Avdija", 1, 1)]


def test_two_lone_shooters_and_one_foul_leave_the_later_one_the_technical_when_the_trip_ends() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(2, fta=1, ftm=1)
    game.lines()
    game.bump(13, tov=1)
    assert told(game.lines(), TechnicalFoul) == [_technical("D.Avdija", 1, 1)]


def test_a_second_free_throw_with_no_foul_found_makes_a_foul_shooter_not_a_technical() -> None:
    game = _game()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 1, 1, 1), _shot("S.Sharpe", 2, 2, 2)]


def test_a_technical_shooter_shooting_again_on_the_trip_starts_their_own_count() -> None:
    game = _game()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, dreb=1)
    assert told(game.lines()) == [_technical("S.Sharpe", 1, 1)]
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 1, 2, 2)]


def test_an_and_one_is_no_technical_even_with_no_foul_posted() -> None:
    game = _game()
    game.bump(1, fg=1)
    game.bump(2, ast=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 1, 1, 1)]


@pytest.mark.parametrize("scorer, later", [(1, clock(5, 39)), (2, clock(5, 50))], ids=["too-long-ago", "a-teammate"])
def test_a_basket_long_before_or_by_someone_else_is_no_and_one(scorer: int, later: int) -> None:
    game = _game()
    game.bump(scorer, fg=1)
    game.bump(3, ast=1)
    game.lines()
    game.slate(at_clock=later)
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, dreb=1)
    assert told(game.lines(), TechnicalFoul) == [_technical("S.Sharpe", 1, 1)]


def test_an_and_one_from_the_last_period_is_no_and_one() -> None:
    game = _game(at_clock=clock(0, 1))
    game.bump(1, fg=1)
    game.bump(2, ast=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines(at_clock=clock(12), period=2)
    game.bump(12, dreb=1)
    assert told(game.lines(), TechnicalFoul) == [_technical("S.Sharpe", 1, 1)]


@pytest.mark.parametrize(
    "between",
    ["offensive-foul", "basket-since", "clock-ran"],
)
def test_a_foul_that_cant_have_given_the_free_throw_doesnt_cover_it(between: str) -> None:
    game = _game()
    if between == "offensive-foul":
        game.bump(11, pf=1, tov=1)
        game.lines()
    else:
        game.bump(11, pf=1)
        game.lines()
        if between == "basket-since":
            game.bump(2, fg=1)
            game.bump(3, ast=1)
            game.lines()
        else:
            game.slate(at_clock=clock(5, 39))
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, dreb=1)
    assert told(game.lines(), TechnicalFoul) == [_technical("S.Sharpe", 1, 1)]


def test_a_free_throw_posted_after_its_trip_ended_is_that_trips_not_a_technical() -> None:
    game = _game(at_clock=clock(2, 43))
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines(), FreeThrow) == [_shot("S.Sharpe", 1, 1, 1)]
    game.slate(at_clock=clock(2, 21))
    game.bump(12, fg=1)
    game.bump(13, ast=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 2, 2, 2)]


@pytest.mark.parametrize("next_shooter, foul_first", [(2, False), (1, True)], ids=["another-shooter", "a-new-foul"])
def test_a_lone_free_throw_after_a_trip_that_isnt_its_late_one(next_shooter: int, foul_first: bool) -> None:
    game = _game(at_clock=clock(2, 43))
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, tov=1)
    game.lines()
    if foul_first:
        game.bump(13, pf=1)
        game.lines()
    game.bump(next_shooter, fta=1, ftm=1)
    update = game.lines()
    game.bump(12, dreb=1)
    later = game.lines()
    if foul_first:
        assert told(update, FreeThrow) == [_shot("S.Sharpe", 1, 2, 2)]
    else:
        assert told(later) == [_technical("D.Avdija", 1, 1)]


def test_a_late_free_throw_is_one_alone_from_the_same_side_in_the_same_period() -> None:
    game = _game(at_clock=clock(0, 30))
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(1, pf=1)
    game.bump(11, fta=1, ftm=1)  # the other side's trip ends it, and is the last
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, dreb=1)
    assert told(game.lines(), TechnicalFoul) == [_technical("S.Sharpe", 2, 2)]


def test_two_free_throws_at_once_after_a_trip_are_a_new_trip_not_its_late_one() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, tov=1)
    game.lines()
    game.bump(1, fta=2, ftm=2)
    game.lines()
    game.bump(12, tov=1)
    assert told(game.lines(), FreeThrows) == [
        FreeThrows(side=A, shooter="S.Sharpe", made=2, attempted=2, game_made=3, game_attempted=3)
    ]


def test_a_lone_free_throw_whose_trip_ends_before_another_read_is_settled_as_it_ends() -> None:
    game = _game()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    assert told(game.slate(at_clock=clock(5, 39))) == [_technical("S.Sharpe", 1, 1)]


def test_a_free_throw_in_the_next_period_is_no_late_one() -> None:
    game = _game(at_clock=clock(0, 1))
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.lines(at_clock=clock(12), period=2)
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, dreb=1)
    assert told(game.lines(), TechnicalFoul) == [_technical("S.Sharpe", 2, 2)]


def test_a_grouped_trip_with_a_technical_tells_it_beside_the_foul_shooters_group() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines()) == [_shot("S.Sharpe", 1, 1, 1)]
    game.bump(1, fta=1, ftm=1)
    game.bump(2, fta=1, ftm=1)
    together = game.lines()
    sharpe = FreeThrows(side=A, shooter="S.Sharpe", made=2, attempted=2, game_made=2, game_attempted=2)
    avdija = FreeThrows(side=A, shooter="D.Avdija", made=1, attempted=1, game_made=1, game_attempted=1)
    assert told(together) == []
    assert [held.caption for held in together.held] == [sharpe, avdija]
    game.bump(12, dreb=1)
    settled = game.lines()
    assert [held.caption for held in settled.held] == [sharpe, _technical("D.Avdija", 1, 1)]
    game.bump(13, tov=1)
    assert told(game.lines()) == [sharpe, _technical("D.Avdija", 1, 1)]


def test_a_technical_told_before_its_trip_was_grouped_isnt_told_again() -> None:
    game = _game()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, dreb=1)
    assert told(game.lines()) == [_technical("S.Sharpe", 1, 1)]
    game.bump(11, pf=1)
    game.lines()
    game.bump(2, fta=2, ftm=2)
    held = game.lines()
    avdija = FreeThrows(side=A, shooter="D.Avdija", made=2, attempted=2, game_made=2, game_attempted=2)
    assert [h.caption for h in held.held] == [avdija]
    game.bump(13, tov=1)
    assert told(game.lines(), FreeThrows, TechnicalFoul) == [avdija]


# --- timeouts -----------------------------------------------------------------------------------------------


def _onsets(update: FeedUpdate) -> list[TimeoutOnset | TimeoutEnd]:
    return list(update.timeouts)


def _lengths(*updates: FeedUpdate) -> list[TimeoutLength]:
    return [onset.length for update in updates for onset in update.timeouts if isinstance(onset, TimeoutOnset)]


def test_a_timeout_is_told_with_its_class_by_order_in_the_quarter_its_onset_and_its_end() -> None:
    game = _game()
    game.slate()
    stopped = game.lines(at_clock=clock(5, 50))  # the first read with the clock stopped where the timeout comes
    first = game.slate(timeouts=(6, 7))
    onset = TimeoutOnset(
        side=A,
        length=LONG,
        onset=stopped.at,
        seen_at=first.at,
        period=1,
        clock=GameClock(clock(5, 50)),
    )
    assert told(first) == [TimeoutTaken(side=A, left=6)] and _onsets(first) == [onset]
    resumed = game.slate(at_clock=clock(5, 41))
    assert _onsets(resumed) == [TimeoutEnd(side=A, onset=stopped.at, ended_at=resumed.at)]
    second = game.slate(at_clock=clock(2, 59), timeouts=(6, 6))
    third = game.slate(at_clock=clock(1, 2), timeouts=(5, 6))
    assert _lengths(second, third) == [LONG, SHORT]
    game.slate(at_clock=clock(5), period=5)
    assert _lengths(game.slate(at_clock=clock(4), timeouts=(5, 5))) == [SHORT]


@pytest.mark.parametrize(
    "period, at, phase",
    [
        (2, clock(12), BasketballPhase.LIVE),
        (5, clock(5), BasketballPhase.LIVE),
        (1, None, BasketballPhase.END_OF_PERIOD),
    ],
    ids=["full-clock", "overtime-full-clock", "at-the-break"],
)
def test_a_count_down_in_the_break_is_no_timeout(period: int, at: int | None, phase: BasketballPhase) -> None:
    game = _game()
    game.slate()
    update = game.slate(at_clock=at, period=period, phase=phase, timeouts=(6, 7))
    assert update.captions == () and update.timeouts == ()


def test_a_count_down_by_two_is_a_correction_and_one_unknown_is_skipped() -> None:
    game = _game()
    game.slate()
    assert game.slate(timeouts=(5, 7)).captions == ()
    assert told(game.slate(timeouts=(None, 6))) == [TimeoutTaken(side=H, left=6)]
    assert game.slate(timeouts=(5, 6)).captions == ()


def test_a_period_first_read_midway_takes_its_timeouts_as_short_and_the_next_period_by_order() -> None:
    game = _game(period=3, at_clock=clock(9, 49))
    game.slate(timeouts=(5, 4))
    assert _lengths(game.slate(at_clock=clock(8, 17), timeouts=(4, 4))) == [SHORT]
    game.slate(at_clock=clock(12), period=4)
    assert _lengths(game.slate(at_clock=clock(11), timeouts=(3, 4))) == [LONG]


def test_the_first_period_read_midway_takes_its_order_from_the_timeouts_used() -> None:
    game = _game(at_clock=clock(5, 48))
    game.slate(timeouts=(6, 7))  # one already taken this quarter
    second = game.slate(at_clock=clock(5, 6), timeouts=(6, 6))
    third = game.slate(at_clock=clock(3), timeouts=(5, 6))
    assert _lengths(second, third) == [LONG, SHORT]


def test_a_timeout_read_before_any_clock_dates_its_onset_from_its_own_read() -> None:
    game = Game((1, A, "S.Sharpe"), at_clock=None)
    game.slate()
    update = game.slate(timeouts=(6, 7))
    assert [onset.onset for onset in _onsets(update)] == [update.at]


def test_a_read_behind_the_clock_doesnt_move_the_onset() -> None:
    game = _game(at_clock=clock(5, 2))
    stopped = game.slate(at_clock=clock(5))
    game.lines(at_clock=clock(5, 2))  # the stat rows a step behind
    update = game.slate(timeouts=(6, 7))
    assert [onset.onset for onset in _onsets(update)] == [stopped.at]
    assert _onsets(game.lines(at_clock=clock(5, 1))) == []


def _into_the_fourth(game: Game, timeouts: tuple[int, int]) -> None:
    """Carry a game read late in the third quarter into the fourth, seen from its start."""
    game.slate(timeouts=timeouts)
    game.slate(at_clock=None, phase=BasketballPhase.END_OF_PERIOD)
    game.slate(at_clock=clock(12), period=4)


def test_the_fourth_quarters_cap_at_its_full_clock_is_no_timeout_and_not_counted_in_its_order() -> None:
    game = _game(period=3, at_clock=clock(0, 30))
    game.slate(timeouts=(5, 6))
    game.slate(at_clock=None, phase=BasketballPhase.END_OF_PERIOD)
    capped = game.slate(at_clock=clock(12), period=4, timeouts=(4, 4))
    assert capped.captions == () and capped.timeouts == ()
    first = game.slate(at_clock=clock(9, 10), timeouts=(4, 3))
    assert told(first) == [TimeoutTaken(side=H, left=3)] and _lengths(first) == [LONG]


def test_a_count_first_read_in_the_fourth_quarter_comes_down_to_its_cap_and_only_a_fall_below_is_a_timeout() -> None:
    game = _game(period=3, at_clock=clock(1))
    game.slate(timeouts=(6, 5))
    entered = game.slate(at_clock=clock(11, 40), period=4, timeouts=(4, 3))
    assert told(entered) == [TimeoutTaken(side=H, left=3)] and _lengths(entered) == [LONG]


def test_the_late_cap_lowers_a_count_to_two_by_one_or_two_for_both_teams_and_isnt_counted() -> None:
    game = _game(period=3, at_clock=clock(0, 5))
    _into_the_fourth(game, (4, 3))
    assert game.slate(at_clock=clock(3, 5)).captions == ()  # before the mark, nothing is due
    capped = game.slate(at_clock=clock(2, 57), timeouts=(2, 2))
    assert capped.captions == () and capped.timeouts == ()
    game.slate(at_clock=clock(2, 40))
    real = game.slate(at_clock=clock(1, 51), timeouts=(1, 2))
    assert told(real) == [TimeoutTaken(side=A, left=1)] and _lengths(real) == [LONG]  # the quarter's first


def test_only_a_fall_below_the_late_cap_is_a_timeout_whenever_the_feed_posts_it() -> None:
    game = _game(period=3, at_clock=clock(0, 5))
    _into_the_fourth(game, (4, 4))
    game.lines(at_clock=clock(2, 59))  # any read at 3:00 or under brings the cap due
    update = game.slate(timeouts=(1, 3))
    assert told(update) == [TimeoutTaken(side=A, left=1)]
    assert game.slate(timeouts=(1, 2)).captions == ()  # the rest of the cap, posted a step later


def test_a_drop_by_two_away_from_any_cap_is_still_a_correction() -> None:
    game = _game(period=3, at_clock=clock(0, 5))
    _into_the_fourth(game, (5, 5))
    assert game.slate(at_clock=clock(6), timeouts=(3, 5)).captions == ()


def test_an_overtime_brings_a_fresh_allotment_whatever_the_count_becomes() -> None:
    game = _game(period=4, at_clock=clock(0, 10))
    game.slate(timeouts=(1, 0))
    game.slate(at_clock=None, phase=BasketballPhase.END_OF_PERIOD)
    fresh = game.slate(at_clock=clock(5), period=5, timeouts=(2, 2))
    assert fresh.captions == () and fresh.held == () and fresh.timeouts == ()
    assert _lengths(game.slate(at_clock=clock(4), timeouts=(2, 1))) == [SHORT]
    game.slate(at_clock=None, phase=BasketballPhase.END_OF_PERIOD)
    second = game.slate(at_clock=clock(4, 30), period=6, timeouts=(1, 2))  # first read in the period, already running
    assert second.captions == () and second.timeouts == ()


# --- challenges ---------------------------------------------------------------------------------------------


def _dropped(game: Game) -> None:
    game.slate()
    game.slate(timeouts=(7, 6))


def test_a_count_back_with_the_clock_still_stopped_is_a_won_challenge_told_after_one_lines_read() -> None:
    game = _game()
    _dropped(game)
    restored = game.slate(timeouts=(7, 7))
    assert told(restored) == []
    assert restored.held == (
        HeldCaption(reason=HeldReason.CHALLENGE, caption=ChallengeWon(side=H), seen_at=restored.at),
    )
    update = game.lines()
    assert told(update) == [ChallengeWon(side=H)] and update.captions[0].seen_at == restored.at


def test_a_won_challenge_names_the_call_change_its_side_had_taken_back() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    _dropped(game)
    game.bump(11, pf=-1)
    change = PlayerCallChange(change=CallChange.FOUL_OFF, player="A.Edwards")
    assert told(game.lines()) == [CallReversed(change=change)]
    assert told(game.slate(timeouts=(7, 7))) == [ChallengeWon(side=H, change=change)]


def test_a_call_change_posted_after_the_count_came_back_is_the_challenges() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.lines()
    _dropped(game)
    game.slate(timeouts=(7, 7))
    game.bump(11, pf=-1, blk=1)
    change = PlayerCallChange(change=CallChange.FOUL_TO_BLOCK, player="A.Edwards")
    assert told(game.lines()) == [ChallengeWon(side=H, change=change)]


def test_a_call_taken_back_on_the_other_side_isnt_the_challenges() -> None:
    game = _game()
    game.bump(1, pf=2)
    game.lines()
    reversed_call = CallReversed(change=PlayerCallChange(change=CallChange.FOUL_OFF, player="S.Sharpe"))
    _dropped(game)
    game.bump(1, pf=-1)
    assert told(game.lines()) == [reversed_call]
    restored = game.slate(timeouts=(7, 7))
    assert told(restored) == [] and [held.caption for held in restored.held] == [ChallengeWon(side=H)]
    game.bump(1, pf=-1)
    assert told(game.lines()) == [ChallengeWon(side=H), reversed_call]


def test_a_won_challenge_still_waiting_is_told_when_the_clock_runs() -> None:
    game = _game()
    _dropped(game)
    game.slate(timeouts=(7, 7))
    resumed = game.slate(at_clock=clock(5, 50))
    assert told(resumed) == [ChallengeWon(side=H)]
    assert [type(end) for end in _onsets(resumed)] == [TimeoutEnd]


def test_a_second_won_challenge_in_the_stoppage_tells_the_first_as_it_stands() -> None:
    game = _game()
    game.slate()
    game.slate(timeouts=(6, 6))
    game.slate(timeouts=(7, 6))
    update = game.slate(timeouts=(7, 7))
    assert told(update) == [ChallengeWon(side=A)]
    assert [held.caption for held in update.held] == [ChallengeWon(side=H)]


def test_a_count_back_after_the_clock_ran_is_a_correction_and_a_lost_challenge_is_a_timeout() -> None:
    game = _game()
    _dropped(game)
    game.slate(at_clock=clock(5, 50))
    corrected = game.slate(timeouts=(7, 7))
    assert corrected.captions == () and corrected.held == ()
    assert told(game.slate(timeouts=(7, 6))) == [TimeoutTaken(side=H, left=6)]  # a challenge lost: it cost the timeout
    assert told(game.lines()) == []
