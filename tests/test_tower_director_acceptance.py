"""What the v2 acceptance replays found in the board's policy director, each pinned at `BoardSession`, in the synthetic
time loop of `tests.board_policy`: a favorite's own play that never aired, a lone card written every turn, a final
shown in 8 s turns long past its own, two games in one break starving each other, a favorite's final held behind
another, and a no-hit bid reminded once however long it stood."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from omni.core.enum import League
from omni.domain.breaks import BreakKind
from omni.preferences.favorites import Favorites
from omni.domain.contest import TeamGame
from omni.policy.promotion import PromotionRefusal
from omni.tower.board import BoardFrame, FrameKind
from omni.tower.decisions import CardNote, CardRole, ClampNote, PromotionNote, WaitNote
from tests.board_policy import Run, big_play, evidence, facts, final, games, live, major, pregame, reminder

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
    game = games(2)[1]
    lone = pregame(dataclasses.replace(game, scheduled_start=game.scheduled_start + timedelta(hours=2)))
    run = Run(favorites=NO_FAVORITES)
    run.stage(lone)
    run.publish()
    run.run(0, 60)
    assert run.shown(lone.dedupe_key.raw) == [(0.0, None)]
    assert [(note.role, note.at.monotonic.seconds) for note in run.of(CardNote)] == [(CardRole.NEUTRAL, 0.0)]


def test_a_lone_final_airs_its_turn_then_the_clock_then_its_repeats_5_and_15_minutes_after_it_first_went_up() -> None:
    """With nothing live, the final has its one 8 s turn in end order; nothing else is due, so the board shows its
    idle frame (the clock) until each repeat falls due, rather than the final in 8 s turns for half an hour."""
    lone = final(games(2)[1], ended=-1)
    run = Run(favorites=NO_FAVORITES)
    run.stage(lone)
    run.publish()
    run.run(0, 1500)
    assert run.shown(lone.dedupe_key.raw) == [(0.0, 8.0), (300.0, 308.0), (900.0, 908.0)]
    assert [(note.role, note.at.monotonic.seconds) for note in run.of(CardNote)] == [
        (CardRole.RESULT, 0.0),
        (CardRole.REPEAT, 300.0),
        (CardRole.REPEAT, 900.0),
    ]
    idle = [cue.payload for when, cue in run.cues if 8 <= when < 300]
    assert idle and all(isinstance(frame, BoardFrame) and frame.kind is FrameKind.IDLE for frame in idle)


def test_a_final_first_shown_long_after_its_confirmation_repeats_only_while_its_turn_ends_by_its_hard_end() -> None:
    """Confirmed 1000 s before it first went up (held back by a favorite's action, say), its hard end is 800 s after
    that: the repeat due at 300 fits; the one due at 900 doesn't, and the live game keeps the board."""
    slate = games(3)
    playing = slate[2]
    result = final(slate[1], ended=-1000, available=-1000)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(playing), result, when=-1000)
    run.describe(facts(playing, 0.4))
    run.publish()
    run.run(0, 1000)
    assert run.shown(result.dedupe_key.raw) == [(0.0, 8.0), (308.0, 316.0)]  # 300 fell inside a base turn
    assert [(note.role, note.at.monotonic.seconds) for note in run.of(CardNote)] == [
        (CardRole.RESULT, 0.0),
        (CardRole.REPEAT, 308.0),
    ]


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


def _leave(run: Run, game: TeamGame) -> None:
    del run.facts[game.id]


def _republish(run: Run, until: float, every: float = 5.0) -> None:
    """Publish the input again every ``every`` seconds, as the loop does at each refresh."""
    for at in range(0, int(until) + 1, int(every)):
        run.on(at, run.publish)


def test_a_bid_s_open_time_pauses_while_a_favorite_acts_and_its_next_reminder_waits_for_60_open_seconds() -> None:
    slate = games(3)
    braves, bid = slate[0], slate[1]
    nohitter = big_play(bid, "nohitter", window=1000)
    run = Run(favorites=ATL_COL)
    run.stage(live(braves), live(bid), nohitter)
    run.describe(facts(bid, 0.4))
    run.offers.append(reminder(nohitter))
    run.on(10, lambda: run.describe(facts(braves, 0.5)))  # the Braves take the field: open time pauses
    run.on(200, lambda: _leave(run, braves))  # and leave it
    _republish(run, 400)
    run.run(0, 400)
    starts = [at for at, _ in run.promotions()]
    # Open time counts from what the board's decisions see: at most 20 s before its turn at 20 found the Braves
    # acting, so at least 40 s more after they left at 200, and no more than a turn later.
    assert starts[0] == 0.0 and 240.0 <= starts[1] < 262.0
    assert len(starts) == 3 and starts[2] - starts[1] >= 60.0


def test_a_reminder_whose_lifetime_ran_out_unshown_is_offered_again_as_the_next_attempt() -> None:
    slate = games(3)
    bid = slate[1]
    nohitter = big_play(bid, "nohitter", available=100, window=1000)  # its card can't air before 100
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(bid), live(slate[2]), nohitter)
    run.describe(facts(bid, 0.4), facts(slate[2], 0.4))
    run.offers.append(reminder(nohitter))
    _republish(run, 200)
    run.run(0, 200)
    assert [note.key.play for note in run.of(PromotionNote)][:1] == ["bid reminder 2"]
    assert any(
        note.key.play == "bid reminder 1" and note.reason is PromotionRefusal.RETIRED for note in run.of(WaitNote)
    )
