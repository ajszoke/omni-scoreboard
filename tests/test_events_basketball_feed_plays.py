"""The NBA feed deriver's plays: baskets and their assists, free throws one at a time or as a trip, and the order of
two plays in one read.

Each trip to the line here follows a foul by the other side, so its free throws have a foul behind them.
"""

from __future__ import annotations

from omni.core.clock import MonotonicTime
from omni.domain.basketball import BasketballPhase, GameClock
from omni.events.basketball import FieldGoalMade, FreeThrow, FreeThrows, ShotValue
from omni.events.basketball_feed import FeedCaption, HeldCaption, HeldReason
from tests.basketball_feed_game import A, H, Game, clock, told, told_as

TWO, THREE = ShotValue.TWO, ShotValue.THREE
FREE_THROWS = (FreeThrow, FreeThrows)


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


def _fouled(game: Game, fouler: int = 11) -> None:
    """A foul by ``fouler`` that gives the other side free throws."""
    game.bump(fouler, pf=1)
    game.lines()


# --- baskets ------------------------------------------------------------------------------------------------


def test_the_first_lines_read_is_the_baseline_and_what_it_holds_is_old_news() -> None:
    game = Game((1, A, "S.Sharpe"), (11, H, "A.Edwards"))
    game.bump(1, fg=3, three=1, fta=2, ftm=2, pf=2)
    first = game.lines()
    assert first.captions == () and first.held == ()
    assert game.lines().captions == ()


def test_a_basket_names_its_assister_when_the_team_has_exactly_one_basket_and_one_assist() -> None:
    game = _game()
    game.bump(1, fg=1, three=1)
    game.bump(2, ast=1)
    update = game.lines(at_clock=clock(5, 41))
    assert told(update) == [FieldGoalMade(side=A, shooter="S.Sharpe", value=THREE, assister="D.Avdija")]
    assert update.captions[0] == FeedCaption(
        caption=told(update)[0], seen_at=MonotonicTime(game.at), period=1, clock=GameClock(clock(5, 41))
    )
    assert update.at == MonotonicTime(game.at)


def test_a_basket_with_no_assist_waits_one_lines_read_for_a_late_one() -> None:
    game = _game()
    game.bump(11, fg=1)
    first = game.lines()
    basket = FieldGoalMade(side=H, shooter="A.Edwards", value=TWO)
    assert told(first) == []
    assert first.held == (HeldCaption(reason=HeldReason.ASSIST, caption=basket, seen_at=first.at),)
    assert told(game.slate()) == []  # a slate isn't the read it waits for
    game.bump(12, ast=1)
    second = game.lines()
    assert told(second) == [FieldGoalMade(side=H, shooter="A.Edwards", value=TWO, assister="N.Reid")]
    assert second.captions[0].seen_at == first.at and second.held == ()


def test_a_basket_still_without_an_assist_a_read_later_is_told_without_one() -> None:
    game = _game()
    game.bump(11, fg=1)
    game.lines()
    game.bump(1, ast=1)  # the other side's assist is no help
    assert told(game.lines()) == [FieldGoalMade(side=H, shooter="A.Edwards", value=TWO)]


def test_a_late_assist_goes_to_the_new_basket_when_the_team_scores_again_in_that_read() -> None:
    game = _game()
    game.bump(11, fg=1)
    game.lines()
    game.bump(13, fg=1)
    game.bump(12, ast=1)
    assert told(game.lines()) == [
        FieldGoalMade(side=H, shooter="A.Edwards", value=TWO),
        FieldGoalMade(side=H, shooter="R.Gobert", value=TWO, assister="N.Reid"),
    ]


def test_several_baskets_for_one_team_in_one_read_name_no_assister() -> None:
    game = _game()
    game.bump(1, fg=1)
    game.bump(3, fg=1, three=1)
    game.bump(2, ast=1)
    assert told(game.lines()) == [
        FieldGoalMade(side=A, shooter="S.Sharpe", value=TWO),
        FieldGoalMade(side=A, shooter="J.Grant", value=THREE),
    ]


def test_two_baskets_by_one_player_in_one_read_are_two_captions() -> None:
    game = _game()
    game.bump(11, fg=2, three=1)
    assert told(game.lines()) == [
        FieldGoalMade(side=H, shooter="A.Edwards", value=THREE),
        FieldGoalMade(side=H, shooter="A.Edwards", value=TWO),
    ]


def test_an_assist_that_cant_be_told_apart_names_no_one() -> None:
    game = _game()
    game.bump(11, fg=1, ast=1)  # the shooter's own: no teammate assisted
    assert told(game.lines()) == [FieldGoalMade(side=H, shooter="A.Edwards", value=TWO)]
    game.bump(11, fg=1)
    game.bump(12, ast=1)
    game.bump(13, ast=1)  # two assists for one basket
    assert told(game.lines()) == [FieldGoalMade(side=H, shooter="A.Edwards", value=TWO)]
    game.bump(11, fg=1)
    game.bump(12, ast=2)  # one teammate, two assists
    assert told(game.lines()) == [FieldGoalMade(side=H, shooter="A.Edwards", value=TWO)]


def test_a_player_who_joins_with_a_basket_made_it() -> None:
    game = _game()
    game.join(4, A, "T.Camara", fg=1)
    game.join(5, A, "D.Clingan")  # a substitute with an empty line is no play
    game.bump(2, ast=1)
    assert told(game.lines()) == [FieldGoalMade(side=A, shooter="T.Camara", value=TWO, assister="D.Avdija")]


def test_a_basket_credited_to_a_teammate_is_no_new_basket() -> None:
    game = _game()
    game.bump(1, fg=1, three=1)
    game.bump(2, ast=1)
    game.lines()
    game.bump(1, fg=-1, three=-1)
    game.bump(3, fg=1, three=1)
    assert game.lines().captions == ()
    game.bump(3, fg=1)
    game.bump(2, ast=1)
    game.lines()
    game.bump(11, fg=1)
    game.bump(12, ast=1)
    game.lines()  # the game's last basket is another's when the credit moves
    game.bump(3, fg=-1)
    game.bump(1, fg=1)
    assert game.lines().captions == ()


def test_a_basket_waiting_for_its_assist_moves_with_its_credit() -> None:
    game = _game()
    game.bump(2, fg=1)
    game.lines()
    game.bump(2, fg=-1, ast=1)  # the basket was Grant's, and Avdija assisted it
    game.bump(3, fg=1)
    update = game.lines()
    assert told(update) == [FieldGoalMade(side=A, shooter="J.Grant", value=TWO, assister="D.Avdija")]


def test_a_basket_taken_back_is_no_caption_whether_or_not_it_was_told() -> None:
    game = _game()
    game.bump(11, fg=1)
    game.lines()
    game.bump(11, fg=-1)
    taken = game.lines()
    assert taken.captions == () and taken.held == ()
    game.bump(1, fg=1)
    game.bump(2, ast=1)
    game.lines()
    game.bump(1, fg=-1)
    assert game.lines().captions == ()


# --- two plays in one read ----------------------------------------------------------------------------------


def _assisted(game: Game) -> None:
    game.bump(2, ast=1)
    game.bump(12, ast=1)


def test_two_teams_baskets_in_one_read_follow_the_feeds_row_order_when_nothing_else_tells() -> None:
    game = _game()
    game.bump(11, fg=1)
    game.bump(1, fg=1, three=1)
    _assisted(game)
    assert [caption.side for caption in told(game.lines(), FieldGoalMade)] == [A, H]


def test_a_slate_caught_between_two_teams_baskets_orders_them() -> None:
    game = _game()
    game.slate(score=(0, 2))  # the home basket, and not the away three
    game.bump(11, fg=1)
    game.bump(1, fg=1, three=1)
    _assisted(game)
    assert [caption.side for caption in told(game.lines(), FieldGoalMade)] == [H, A]


def test_slates_that_disagree_or_show_both_baskets_leave_the_row_order() -> None:
    game = _game()
    game.slate(score=(3, 0))
    game.slate(score=(0, 2))
    game.bump(11, fg=1)
    game.bump(1, fg=1, three=1)
    _assisted(game)
    assert [caption.side for caption in told(game.lines(), FieldGoalMade)] == [A, H]
    game.slate(score=(5, 4))  # both at once
    game.bump(11, fg=1)
    game.bump(1, fg=1)
    _assisted(game)
    assert [caption.side for caption in told(game.lines(), FieldGoalMade)] == [A, H]


# --- free throws, one at a time -----------------------------------------------------------------------------


def test_free_throws_that_land_one_a_read_are_told_one_at_a_time_numbered_on_the_trip() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=1, ftm=1)
    first = game.lines()
    assert told(first, *FREE_THROWS) == [
        FreeThrow(side=A, shooter="S.Sharpe", place=1, made=True, game_made=1, game_attempted=1)
    ]
    assert first.held == ()
    game.bump(1, fta=1)
    assert told(game.lines(), *FREE_THROWS) == [
        FreeThrow(side=A, shooter="S.Sharpe", place=2, made=False, game_made=1, game_attempted=2)
    ]


def test_a_three_shot_trip_told_one_at_a_time_runs_one_two_three() -> None:
    game = _game()
    _fouled(game)
    places = []
    for _ in range(3):
        game.bump(1, fta=1, ftm=1)
        places += [caption.place for caption in told_as(game.lines(), FreeThrow)]
    assert places == [1, 2, 3]


def test_a_running_clock_alone_doesnt_end_a_trip_within_20_s() -> None:
    game = _game(at_clock=clock(1, 26))
    _fouled(game)
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    second = told_as(game.lines(at_clock=clock(1, 23)), FreeThrow)
    assert [caption.place for caption in second] == [2]


def test_more_than_20_s_of_clock_ends_a_trip() -> None:
    game = _game(at_clock=clock(1, 26))
    _fouled(game)
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.slate(at_clock=clock(1, 5))  # 21 s on
    _fouled(game)
    game.bump(1, fta=1, ftm=1)
    assert [caption.place for caption in told_as(game.lines(), FreeThrow)] == [1]


# --- free throws together -----------------------------------------------------------------------------------


def test_two_free_throws_in_one_read_hold_the_trip_until_a_basket_ends_it() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=2, ftm=2)
    landed = game.lines()
    trip = FreeThrows(side=A, shooter="S.Sharpe", made=2, attempted=2, game_made=2, game_attempted=2)
    assert told(landed, *FREE_THROWS) == []
    assert landed.held == (HeldCaption(reason=HeldReason.FREE_THROW_GROUP, caption=trip, seen_at=landed.at),)
    game.bump(12, dreb=1)
    assert told(game.lines(), *FREE_THROWS) == []
    game.bump(11, fg=1)
    game.bump(12, ast=1)
    ended = game.lines()
    assert told(ended, FreeThrows, FieldGoalMade) == [
        trip,
        FieldGoalMade(side=H, shooter="A.Edwards", value=TWO, assister="N.Reid"),
    ]
    assert ended.captions[0].seen_at == landed.at


def test_a_trip_is_never_shown_in_part_a_single_then_two_together_is_one_group() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=1)
    assert told(game.lines(), *FREE_THROWS) == [
        FreeThrow(side=A, shooter="S.Sharpe", place=1, made=False, game_made=0, game_attempted=1)
    ]
    game.bump(1, fta=2, ftm=2)
    assert told(game.lines(), *FREE_THROWS) == []
    game.bump(12, tov=1)  # a turnover ends it
    assert told(game.lines(), *FREE_THROWS) == [
        FreeThrows(side=A, shooter="S.Sharpe", made=2, attempted=3, game_made=2, game_attempted=3)
    ]


def test_two_together_then_a_third_alone_is_one_group_of_three() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=2, ftm=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    assert told(game.lines(), *FREE_THROWS) == []  # held with the rest of its trip
    _fouled(game, fouler=1)
    game.bump(11, fta=1, ftm=1)  # the other side's free throw ends it
    assert told(game.lines(), *FREE_THROWS) == [
        FreeThrows(side=A, shooter="S.Sharpe", made=2, attempted=3, game_made=2, game_attempted=3),
        FreeThrow(side=H, shooter="A.Edwards", place=1, made=True, game_made=1, game_attempted=1),
    ]


def test_a_grouped_trip_ends_at_the_next_period_or_after_20_s_of_clock() -> None:
    game = _game(at_clock=clock(0, 30))
    _fouled(game)
    game.bump(1, fta=2, ftm=2)
    game.lines()
    ended = game.slate(phase=BasketballPhase.END_OF_PERIOD)
    assert told(ended) == []  # a break isn't the next period: buzzer free throws can still come
    ended = game.lines(at_clock=clock(12), period=2)
    assert len(told(ended, FreeThrows)) == 1
    _fouled(game)
    game.bump(1, fta=2, ftm=2)
    game.lines()
    assert told(game.slate(at_clock=clock(11, 45)), FreeThrows) == []  # 15 s on
    assert len(told(game.slate(at_clock=clock(11, 39)), FreeThrows)) == 1  # 21 s on


def test_a_grouped_trip_follows_its_shooters_line_where_the_scorer_corrected_it() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=2, ftm=1)
    game.lines()
    game.bump(1, ftm=1)  # the miss made good
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(12, tov=1)
    assert told(game.lines(), FreeThrows) == [
        FreeThrows(side=A, shooter="S.Sharpe", made=3, attempted=3, game_made=3, game_attempted=3)
    ]
    _fouled(game)
    game.bump(3, fta=2, ftm=2)
    game.lines()
    game.bump(3, fta=-1, ftm=-1)  # an attempt taken off
    game.lines()
    game.bump(3, fta=1, ftm=1)
    game.lines()
    game.bump(12, tov=1)
    assert told(game.lines(), FreeThrows) == [
        FreeThrows(side=A, shooter="J.Grant", made=2, attempted=2, game_made=2, game_attempted=2)
    ]


def test_free_throws_moved_to_another_player_are_no_new_free_throws() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.bump(1, fta=-2, ftm=-2)
    game.bump(11, fta=2, ftm=2)  # credited to the other side's player, then put right
    moved = game.lines()
    assert told(moved, *FREE_THROWS) == [] and moved.held == ()


def test_a_shooters_basket_in_the_read_ends_the_trip_before_their_and_one() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=2, ftm=2)
    game.lines()
    game.bump(2, fg=1, fta=1, ftm=1)
    game.bump(12, pf=1)
    update = game.lines()
    assert told(update, FreeThrows, FreeThrow) == [
        FreeThrows(side=A, shooter="S.Sharpe", made=2, attempted=2, game_made=2, game_attempted=2),
        FreeThrow(side=A, shooter="D.Avdija", place=1, made=True, game_made=1, game_attempted=1),
    ]


def test_a_slate_with_no_clock_leaves_the_clock_where_it_was() -> None:
    game = _game()
    _fouled(game)
    game.bump(1, fta=1, ftm=1)
    game.lines()
    game.slate(at_clock=None)
    game.bump(1, fta=1, ftm=1)
    assert [caption.place for caption in told_as(game.lines(), FreeThrow)] == [2]


def test_both_sides_free_throws_in_one_read_are_two_trips() -> None:
    game = _game()
    game.bump(11, pf=1)
    game.bump(1, pf=1)
    game.lines()
    game.bump(1, fta=1, ftm=1)
    game.bump(11, fta=1, ftm=1)
    assert told(game.lines(), FreeThrow) == [
        FreeThrow(side=A, shooter="S.Sharpe", place=1, made=True, game_made=1, game_attempted=1),
        FreeThrow(side=H, shooter="A.Edwards", place=1, made=True, game_made=1, game_attempted=1),
    ]


def test_a_trip_still_open_at_the_final_is_told_whole_and_a_basket_without_its_wait() -> None:
    game = _game(period=4, at_clock=clock(0, 3))
    _fouled(game)
    game.bump(1, fta=2, ftm=1)
    game.lines()
    game.slate(phase=BasketballPhase.FINAL, score=(1, 0))
    game.bump(11, fg=1)
    final = game.lines(at_clock=None)
    assert told(final, FreeThrows, FieldGoalMade) == [
        FreeThrows(side=A, shooter="S.Sharpe", made=1, attempted=2, game_made=1, game_attempted=2),
        FieldGoalMade(side=H, shooter="A.Edwards", value=TWO),
    ]
    assert final.held == ()


def test_a_trip_still_open_when_the_game_goes_final_is_told_at_the_next_lines_read() -> None:
    game = _game(period=4, at_clock=clock(0, 3))
    _fouled(game)
    game.bump(1, fta=2, ftm=2)
    game.lines()
    game.slate(phase=BasketballPhase.FINAL, score=(2, 0))
    assert told(game.lines(at_clock=None), FreeThrows) == [
        FreeThrows(side=A, shooter="S.Sharpe", made=2, attempted=2, game_made=2, game_attempted=2)
    ]
