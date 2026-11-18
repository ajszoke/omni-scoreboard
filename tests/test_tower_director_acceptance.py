"""What the v2 acceptance replays found in the board's policy director, each pinned at `BoardSession`, in the synthetic
time loop of `tests.board_policy`: a favorite's own play that never aired, a lone card written every turn, a final
shown back to back, two games in one break starving each other, and a favorite's final held behind another."""

from __future__ import annotations

from omni.core.enum import League
from omni.domain.breaks import BreakKind
from omni.preferences.favorites import Favorites
from omni.tower.decisions import CardNote, CardRole, ClampNote, PromotionNote
from tests.board_policy import Run, big_play, evidence, facts, final, games, live, major

ATL_COL = Favorites({League.MLB: ["ATL", "COL"]})
NO_FAVORITES = Favorites()


def test_a_favorite_s_own_play_waiting_on_a_bar_it_can_t_clear_airs_first_in_its_own_turn_and_is_never_promoted() -> (
    None
):
    """The Rockies' homer, below the action bar while the Braves act, airs at the head of the Rockies' next turn,
    before its window runs out; the promotion it waited on is settled, so it isn't shown twice."""
    slate = games(5)
    atl, col = slate[0], slate[4]
    homer = big_play(col, "hr", window=120)
    run = Run(favorites=ATL_COL)
    run.stage(live(atl), live(col), homer)
    run.describe(facts(atl, 0.5), facts(col, 0.5))
    run.offers.append(major(homer, 0.9, model=False))  # a proxy value: it can never cut into action
    run.publish()
    run.run(0, 150)
    turns = [note.at.monotonic.seconds for note in run.notes if getattr(note, "contest", None) == col.id]
    [(up, down)] = run.shown(homer.dedupe_key.raw)
    assert up == turns[0] and down == up + 6  # the head of the Rockies' first turn, for its minimum
    assert run.of(PromotionNote) == []


def test_a_lone_card_rotating_at_neutral_weight_is_written_once_not_every_turn() -> None:
    lone = final(games(2)[1], ended=-1)
    run = Run(favorites=NO_FAVORITES)
    run.stage(lone)
    run.publish()
    run.run(0, 60)
    assert run.shown(lone.dedupe_key.raw) == [(0.0, None)]
    assert [(note.role, note.at.monotonic.seconds) for note in run.of(CardNote)] == [
        (CardRole.RESULT, 0.0),
        (CardRole.NEUTRAL, 8.0),
    ]


def test_a_final_shown_long_after_its_confirmation_repeats_at_most_twice_then_the_base_comes_back() -> None:
    """A final confirmed 16 minutes ago is owed both its repeats at once; held up for them, each counts, so it
    airs three turns (its result and two repeats) and the live game has the board back at 24."""
    slate = games(3)
    playing = slate[2]
    result = final(slate[1], ended=-1000, available=-1000)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(playing), result, when=-1000)
    run.describe(facts(playing, 0.4))
    run.publish()
    run.run(0, 60)
    assert run.shown(result.dedupe_key.raw) == [(0.0, 24.0)]
    assert [(note.role, note.at.monotonic.seconds) for note in run.of(CardNote)] == [
        (CardRole.RESULT, 0.0),
        (CardRole.REPEAT, 8.0),
        (CardRole.REPEAT, 16.0),
    ]
    assert run.keys(since=24) == [live(playing).dedupe_key.raw]


def test_two_games_in_one_league_both_in_a_break_share_the_league_s_turns_and_neither_starves() -> None:
    """Games still in the pool through their breaks take turns as they pay for them: neither is frozen."""
    slate = games(3)
    first, second = slate[1], slate[2]
    stopped = BreakKind.MLB_WEATHER  # an open lease: each game is in its break the whole time
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(first), live(second))
    run.describe(
        facts(first, 0.6, breaks=(evidence(first, stopped, received=0),)),
        facts(second, 0.4, breaks=(evidence(second, stopped, received=0),)),
    )
    run.publish()
    run.run(0, 600)
    turns = run.base_turns()
    assert turns[first.id.raw] > 0 and turns[second.id.raw] >= 0.4 * turns[first.id.raw]
    assert run.base_turns() == run.base_turns(before=601) and not run.of(ClampNote)


def test_a_favorite_s_own_final_isn_t_held_behind_an_unrelated_final_waiting_for_a_break() -> None:
    slate = games(5)
    braves, unrelated, rockies = slate[0], slate[1], slate[4]
    theirs = final(unrelated, ended=-100)
    ours = final(braves, ended=-50)
    run = Run(favorites=ATL_COL)
    run.stage(live(rockies), theirs, ours)
    run.describe(facts(rockies, 0.5))
    run.publish()
    run.run(0, 60)
    assert run.keys()[0] == ours.dedupe_key.raw  # at once: it's a favorite's own result
    assert theirs.dedupe_key.raw not in run.keys()  # the Rockies are in action: it waits for a break
