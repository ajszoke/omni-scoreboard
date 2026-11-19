"""The board under `omni-board-policy/2`, through `BoardSession`: the reference numbers of `BOARD_POLICY.md` §9 and
the packet's acceptance, measured from confirmed airtime in a synthetic time loop that publishes hand-built
views and commits every frame at once."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from omni.core.enum import League, PanelProfile
from omni.domain.breaks import BreakKind
from omni.policy.promotion import PromotionRefusal
from omni.policy.settings import Mutes, PolicySettings
from omni.preferences.favorites import Favorites
from omni.queue.scheduler import SelectionReason
from omni.tower.board import BoardFrame, BoardHealth, FrameKind
from omni.tower.decisions import (
    BaseReason,
    BaseTurnNote,
    CapNote,
    CardNote as CardNoteType,
    CardRole,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    OfferSkip,
    PromotionNote,
    UnspentNote,
    WaitNote,
    YieldNote,
)
from omni.tower.lane import ChangeKind, EndReason
from tests.board_policy import (
    PROFILES,
    Run,
    action,
    at,
    break_id,
    big_play,
    evidence,
    facts,
    final,
    games,
    live,
    major,
    offline,
    pregame,
    status,
)

NO_FAVORITES = Favorites()


def _key(card: object) -> str:
    return card.dedupe_key.raw  # type: ignore[attr-defined, no-any-return]


# --- Allocation, from committed airtime --------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_two_same_league_non_favorites_at_g_1_and_0_share_confirmed_base_airtime_2_5_to_1(
    profile: PanelProfile,
) -> None:
    high, low = games(2)
    run = Run(profile=profile, favorites=NO_FAVORITES)
    run.stage(live(high), live(low))
    run.describe(facts(high, 1.0), facts(low, 0.0))
    run.publish()
    run.run(0, 4200)
    assert run.base_turns(before=4200) == {"g00": 250, "g01": 100}
    airtime = run.airtime(until=4200)
    assert (airtime[_key(live(high))], airtime[_key(live(low))]) == (3000.0, 1200.0)  # 2.5:1, all confirmed


@pytest.mark.parametrize("g, turns", [(0.0, 30), (1.0, 70)])
def test_atl_against_14_high_leverage_games_gets_30_percent_in_a_blowout_and_70_close(g: float, turns: int) -> None:
    slate = games(15)
    run = Run()
    run.stage(*(live(each) for each in slate))
    run.describe(facts(slate[0], g), *(facts(each, 1.0) for each in slate[1:]))
    run.publish()
    run.run(0, 1200)
    granted = run.base_turns(before=1200)
    assert (granted["g00"], sum(granted.values())) == (turns, 100)
    assert run.airtime(until=1200)[_key(live(slate[0]))] == turns * 12.0


# --- Interrupt timing ----------------------------------------------------------------------------------------------


def _favorite_and_play(worth: float, *, model: bool = True, profile: PanelProfile = PanelProfile.QUAD_128X64) -> Run:
    """ATL (the favorite) and another game at equal leverage, and a big play elsewhere offered at 0."""
    atl, other = games(2)
    play = big_play(other, "p1")
    run = Run(profile=profile)
    run.stage(live(atl), live(other), play)
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    run.offers.append(major(play, worth, model=model))
    run.publish()
    return run


@pytest.mark.parametrize("profile", PROFILES)
def test_a_mound_visit_releasing_at_18_takes_an_action_qualified_cut_in_at_18_to_30(profile: PanelProfile) -> None:
    run = _favorite_and_play(1.2, profile=profile)
    atl = games(1)[0]

    def visit() -> None:  # received 45 s earlier, as the viewer's delay holds it, the visit just begun
        run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=18 - 45),)))
        run.publish()

    run.on(18, visit)
    run.run(0, 60)
    play = "mlb:mlb_statsapi:g01:bigplay:p1"
    assert run.shown(play) == [(18.0, 30.0)]
    assert run.ups[0].key == _key(live(atl)) and run.ups[0].down == 18.0  # cut after its minimum, mid-turn
    [promoted] = run.of(PromotionNote)
    assert promoted.why == "Angels at D-backs: late winner; Braves in mound visit"
    assert run.board.decisions() == []
    # The visit hosts one guest: once the promotion has had it, the board goes back to the Braves.
    returned = [note for note in run.of(BaseTurnNote) if note.reason is BaseReason.RETURN]
    assert returned[0].at.monotonic.seconds == 30.0


def test_with_no_break_a_1_2_event_starts_at_24_and_a_0_78_one_never_starts_in_action() -> None:
    strong = _favorite_and_play(1.2)
    strong.run(0, 60)
    assert strong.shown("mlb:mlb_statsapi:g01:bigplay:p1") == [(24.0, 36.0)]
    assert [(note.at.monotonic.seconds, note.reason) for note in strong.of(WaitNote)] == [
        (0.0, PromotionRefusal.HOLDING)
    ]

    weak = _favorite_and_play(0.78, model=False)
    weak.run(0, 130)
    assert weak.promotions() == [] and not any(up.reason is SelectionReason.TAKEOVER for up in weak.ups)
    waits = [(note.at.monotonic.seconds, note.reason) for note in weak.of(WaitNote)]
    assert waits == [
        (0.0, PromotionRefusal.BELOW_BAR),
        (114.0, PromotionRefusal.RETIRED),
    ]  # the first decision past +108


@pytest.mark.parametrize("profile", PROFILES)
def test_four_promoted_events_in_open_rotation_promote_at_0_to_12_and_60_to_72_only(profile: PanelProfile) -> None:
    slate = games(5)[1:]  # no favorite plays
    plays = [big_play(each, "p") for each in slate]
    run = Run(profile=profile)
    run.stage(*(live(each) for each in slate), *plays)
    run.describe(*(facts(each, 0.4) for each in slate))
    run.offers.extend(major(play, worth, model=False) for play, worth in zip(plays, (0.9, 0.8, 0.7, 0.6)))
    run.publish()
    run.run(0, 130)
    assert run.promotions() == [(0.0, _key(plays[0])), (60.0, _key(plays[1]))]
    assert run.shown(_key(plays[0])) == [(0.0, 12.0)] and run.shown(_key(plays[1])) == [(60.0, 72.0)]
    assert run.shown(_key(plays[2])) == [] and run.shown(_key(plays[3])) == []


# --- The cap ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_four_initial_finals_are_admitted_at_0_8_16_24_then_a_base_turn_is_reserved_32_to_44(
    profile: PanelProfile,
) -> None:
    slate = games(7)
    base = slate[1]
    finals = [final(each, ended=-50 + 10 * index) for index, each in enumerate(slate[2:])]
    run = Run(profile=profile, favorites=NO_FAVORITES)
    run.stage(live(base), *finals)
    run.describe(facts(base, 0.4))
    run.publish()
    run.run(0, 60)
    assert [(up.at, up.down) for up in run.ups[:6]] == [(0, 8), (8, 16), (16, 24), (24, 32), (32, 44), (44, 52)]
    assert run.keys(before=32) == [_key(each) for each in finals[:4]]  # in end order
    assert run.keys(since=32, before=52) == [_key(live(base)), _key(finals[4])]
    [cap] = run.of(CapNote)
    reserved = [note for note in run.of(BaseTurnNote) if note.reason is BaseReason.RECOVERY]
    assert cap.at.monotonic.seconds == 32.0 and reserved[0].at.monotonic.seconds == 32.0
    assert reserved[0].why == "Angels at D-backs: a full turn after the alerts"


# --- A break closing -------------------------------------------------------------------------------------------


def test_when_a_break_closes_at_104_its_guest_begun_at_100_yields_at_108() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other), when=0)
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    run.publish()

    def inning_break() -> None:
        run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_INNING_BREAK, received=90 - 45),)))
        run.publish()

    def resumed() -> None:
        run.describe(
            facts(
                atl,
                0.4,
                action=action(atl, "ab 41 p1", received=104 - 45, closes=(break_id(atl, BreakKind.MLB_INNING_BREAK),)),
            )
        )
        run.publish()

    run.on(90, inning_break)
    run.on(104, resumed)
    run.run(52, 130)  # the Braves hold 88-100; at 100 their break is the other game's
    assert run.shown(_key(live(other)))[:2] == [(76.0, 88.0), (100.0, 108.0)]
    [guest] = [note for note in run.of(GuestNote) if note.refusal is None]
    [closed] = run.of(YieldNote)
    assert guest.at.monotonic.seconds == 100.0 and closed.closed.seconds == 104.0
    assert [note.event for note in run.of(LeaseNote)] == [LeaseEvent.OPENED, LeaseEvent.REVOKED]


def test_a_reserved_recovery_begun_at_100_in_a_break_runs_to_112_when_the_break_closes_at_104() -> None:
    slate = games(7)
    atl, other = slate[0], slate[1]
    finals = [final(each, ended=-50 + 10 * index) for index, each in enumerate(slate[2:6])]
    run = Run()
    run.stage(live(atl), live(other), *finals, when=0)
    in_break = evidence(atl, BreakKind.MLB_INNING_BREAK, received=68 - 45, stated=175)  # 160 s of lease
    run.describe(facts(atl, 0.4, breaks=(in_break,)), facts(other, 0.4))
    run.publish()

    def resumed() -> None:
        run.describe(
            facts(
                atl,
                0.4,
                action=action(atl, "ab 41 p1", received=104 - 45, closes=(break_id(atl, BreakKind.MLB_INNING_BREAK),)),
            )
        )
        run.publish()

    run.on(104, resumed)
    run.run(68, 130)
    assert [up.at for up in run.ups[:4]] == [68, 76, 84, 92]  # the finals, each a guest of the Braves' break
    assert run.shown(_key(live(other))) == [(100.0, 112.0)]
    reserved = [note for note in run.of(BaseTurnNote) if note.reason is BaseReason.RECOVERY]
    assert reserved[0].why == "Angels at D-backs: a full turn after the alerts; Braves in inning break"
    assert run.of(YieldNote) == []


# --- A cue that never landed -------------------------------------------------------------------------------------


def test_a_promotion_the_display_failed_to_draw_spends_no_cooldown_and_no_turn() -> None:
    slate = games(3)[1:]
    play = big_play(slate[0], "p1")
    run = Run(fail=lambda t, cue: t == 0)
    run.stage(live(slate[0]), live(slate[1]), play)
    run.describe(facts(slate[0], 0.4), facts(slate[1], 0.4))
    run.offers.append(major(play, 0.9, model=False))
    run.publish()
    first = run.step(0)
    assert first.reason is SelectionReason.TAKEOVER and run.ups == []  # cued, but never drawn
    assert run.promotions() == []
    assert run.board.decisions() == []
    assert run.changes[-1].kind is ChangeKind.DROPPED and run.changes[-1].reason is EndReason.UNDRAWN
    run.fail = lambda t, cue: False
    run.step(0)  # set aside: a base card takes the board, and the cooldown hasn't started
    assert run.ups[0].reason is SelectionReason.ROTATION and run.of(PromotionNote) == []
    run.run(12, 70)
    assert run.promotions() == [(60.0, _key(play))]  # tried again once it's no longer set aside


def test_a_promotion_whose_break_ended_before_its_first_frame_comes_down_unspent() -> None:
    atl, other = games(2)
    play = big_play(other, "p1")
    run = Run()
    run.stage(live(atl), live(other), play)
    run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=-45),)), facts(other, 0.4))
    run.offers.append(major(play, 0.9, model=False))  # break-only: it may start in the visit, not in action
    run.publish()
    cue = run.board.cue(at(0))
    assert cue.reason is SelectionReason.TAKEOVER
    run.describe(
        facts(
            atl, 0.4, action=action(atl, "ab 41 p2", received=-44, closes=(break_id(atl, BreakKind.MLB_MOUND_VISIT),))
        )
    )
    run.publish()  # play resumed before the promotion's first frame
    run.board.committed(cue, at(0.1))
    [dropped] = run.board.changes()
    assert (dropped.kind, dropped.reason) == (ChangeKind.DROPPED, EndReason.INVALIDATED)
    notes = run.board.decisions()
    [unspent] = [note for note in notes if isinstance(note, UnspentNote)]
    assert unspent.reason is PromotionRefusal.BREAK_OVER
    run.step(0.1)
    assert run.ups[-1].reason is SelectionReason.ROTATION  # the board moves on at once

    def visit() -> None:
        run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=-35, marker="b2"),)))
        run.publish()

    run.on(10, visit)
    run.run(8, 30)
    assert run.promotions() == [(10.0, _key(play))]  # no cooldown was spent at 0: it starts in the next visit


# --- Mutes ---------------------------------------------------------------------------------------------------------


def test_a_muted_favorite_s_game_never_airs_even_when_its_rank_would_win() -> None:
    atl, other = games(2)
    run = Run(settings=PolicySettings(mutes=Mutes.of(teams={League.MLB: ["ATL"]})))
    run.stage(live(atl), live(other), big_play(atl, "p1"))
    run.describe(facts(atl, 1.0), facts(other, 0.0))
    run.offers.append(major(big_play(atl, "p1"), 3.5))
    run.publish()
    run.run(0, 300)
    assert set(run.keys()) == {_key(live(other))}
    assert [note.reason for note in run.of(WaitNote)] == [OfferSkip.MUTED]


def test_promotions_off_keep_the_weighted_rotation_with_no_cut_ins() -> None:
    atl, other = games(2)
    play = big_play(other, "p1")
    run = Run(settings=PolicySettings(promotions=False))
    run.stage(live(atl), live(other), play)
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    run.offers.append(major(play, 3.5))
    run.publish()
    run.run(0, 120)
    assert run.promotions() == [] and run.of(WaitNote) == []
    assert not any(up.reason is SelectionReason.TAKEOVER for up in run.ups)
    assert run.base_turns(before=120) == {"g00": 8, "g01": 2}
    assert run.shown(_key(play)) == [(24.0, 30.0)]  # unpromoted, it airs first in its own game's turn


def test_an_idle_board_says_so_under_policy_v2() -> None:
    run = Run()
    run.publish()
    cue = run.step(0)
    assert cue.payload == BoardFrame(FrameKind.IDLE, "nothing eligible")
    assert run.board.next_deadline(at(0)) is None


# --- The card contract under policy v2 ---------------------------------------------------------------------------


def test_an_outage_clears_the_board_and_starts_the_leases_and_the_ledger_over() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other))
    in_break = evidence(atl, BreakKind.MLB_INNING_BREAK, received=-45, stated=175)  # a 160 s ceiling from 0
    run.describe(facts(atl, 0.4, breaks=(in_break,)), facts(other, 0.4))
    run.publish()
    run.run(0, 30)
    assert run.keys() == [_key(live(other))]  # the Braves' break is the other game's
    run.health = offline()
    run.publish()
    cleared = run.step(31)
    assert isinstance(cleared.payload, BoardFrame) and cleared.payload.kind is FrameKind.OFFLINE
    assert run.changes[-2].reason is EndReason.CLEARED
    run.health = BoardHealth()
    run.publish()  # the same break evidence again: after the outage, its old lease is void and it opens anew
    run.step(40)
    opened = [note for note in run.of(LeaseNote) if note.event is LeaseEvent.OPENED]
    assert [note.at.monotonic.seconds for note in opened] == [0.0, 40.0]
    assert opened[1].remaining == timedelta(seconds=120)  # still from its release at 0: the outage restores nothing
    first = [note for note in run.of(BaseTurnNote) if note.at.monotonic.seconds == 40.0]
    assert first[0].debt == 0.0 and first[0].actual is None  # its account started over at zero


def test_a_game_that_leaves_the_facts_has_its_leases_forgotten_and_its_account_retired() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other))
    in_break = evidence(atl, BreakKind.MLB_INNING_BREAK, received=-45, stated=175)  # a 160 s ceiling from 0
    run.describe(facts(atl, 0.4, breaks=(in_break,)), facts(other, 0.4))
    run.publish()
    run.run(0, 30)
    del run.facts[atl.id]
    run.publish()
    run.run(31, 60)
    run.describe(facts(atl, 0.4, breaks=(in_break,)))
    run.publish()  # back: the same break is new to the lease book again
    run.step(61)
    opened = [note.at.monotonic.seconds for note in run.of(LeaseNote) if note.event is LeaseEvent.OPENED]
    assert opened == [0.0, 61.0]


def test_a_newer_version_of_the_card_up_swaps_in_and_keeps_its_frame() -> None:
    atl = games(1)[0]
    run = Run()
    run.stage(live(atl))
    run.describe(facts(atl, 0.4))
    run.publish()
    first = run.step(0)
    newer = live(atl, available=3)
    run.stage(newer, when=3)
    run.publish()
    edited = run.step(3)
    assert edited.occurrence == first.occurrence and edited.payload is newer and edited.segment.revision == 1


def test_a_card_on_air_expires_at_its_window_s_end_and_the_board_says_it_s_idle() -> None:
    play = big_play(games(3)[2], "p1", window=20)
    run = Run(favorites=NO_FAVORITES)
    run.stage(play)
    run.publish()  # no facts: nothing live to weigh, so the cards on hand rotate at neutral weight
    run.run(0, 30)
    assert run.shown(_key(play)) == [(0.0, 20.0)]  # a lone card keeps its frame into its next turn
    assert run.changes[-2].reason is EndReason.EXPIRED
    assert isinstance(run.cues[-1][1].payload, BoardFrame)


def test_a_card_that_leaves_the_queue_comes_down_at_once() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    run.publish()
    run.step(0)
    run.queue.remove(_key(live(atl)))
    del run.facts[atl.id]
    run.publish()
    run.step(3)
    assert run.shown(_key(live(atl))) == [(0.0, 3.0)] and run.ups[-1].key == _key(live(other))
    assert run.changes[-2].reason is EndReason.INVALIDATED


def test_a_live_card_replaced_by_its_final_keeps_its_minimum_then_its_final_takes_the_game_s_turn() -> None:
    atl, other = games(2)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 0.4), facts(other, 0.0))
    run.publish()
    run.step(0)
    run.queue.remove(_key(live(atl)))
    ended = final(atl, ended=2, available=2)
    run.stage(dataclasses.replace(ended, replaces=live(atl).dedupe_key), when=2)
    run.publish()  # its facts still describe it live: the final is its base card now
    run.run(2, 30)
    assert run.shown(_key(live(atl))) == [(0.0, 8.0)]  # its minimum, then the hand-off
    assert run.shown(_key(ended))[0] == (8.0, 12.0)  # the rest of the game's turn is its final's
    assert run.base_turns(before=24) == {"g00": 1, "g01": 1}


# --- Ordinary cards ----------------------------------------------------------------------------------------------


def test_with_nothing_live_to_weigh_the_cards_on_hand_rotate_longest_unseen_first_and_finals_keep_end_order() -> None:
    slate = games(4)
    early, late = final(slate[0], ended=-20), final(slate[1], ended=-10)
    soon = pregame(slate[2])
    run = Run(favorites=NO_FAVORITES)
    run.stage(late, early, soon, status(slate[3]))
    run.publish()
    run.run(0, 50)
    # Each final has had its turn; neither is due again before 5 minutes, so the pregame and the status rotate.
    assert run.keys(before=40) == [_key(early), _key(late), _key(soon), _key(status(slate[3])), _key(soon)]
    assert [role for _, role, _ in run.cards()][:5] == ["result", "result", "neutral", "neutral", "neutral"]
    assert run.cards()[2][0] == 16.0 and run.cards()[3][0] == 24.0  # 8 s each: a pregame, then a status


def test_with_games_live_a_pregame_is_due_on_its_ramp_at_most_once_per_four_base_turns() -> None:
    slate = games(3)
    live_game = slate[1]
    starting = pregame(dataclasses.replace(slate[2], scheduled_start=at(25 * 60).utc))  # 25 minutes out: every 120 s
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(live_game), starting)
    run.describe(facts(live_game, 0.4))
    run.publish()
    run.run(0, 300)
    assert [up for up, _ in run.shown(_key(starting))] == [0.0, 128.0, 256.0]


def test_a_shown_final_repeats_5_and_15_minutes_after_it_first_went_up_and_an_unrelated_one_waits_for_a_break() -> None:
    slate = games(3)
    atl, other = slate[0], slate[2]
    result = final(slate[1], ended=-1)
    run = Run()
    run.stage(live(atl), live(other), result)
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    run.publish()
    run.run(0, 30)
    assert run.shown(_key(result)) == []  # the Braves are in action
    del run.facts[atl.id]
    run.publish()
    run.run(31, 16 * 60)
    # It first went up at 36: its repeats fall due at 336 and 936, and air at the first decisions after.
    assert [up for up, _ in run.shown(_key(result))] == [36.0, 344.0, 940.0]


def test_a_final_s_unseen_decisive_play_goes_first_unless_it_waits_on_its_promotion() -> None:
    slate = games(3)
    done = slate[1]
    walk_off = big_play(done, "wo")
    result = final(done, ended=-1, after=(walk_off.dedupe_key,))
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(slate[2]), walk_off, result)
    run.describe(facts(slate[2], 0.4))
    run.publish()
    run.run(0, 20)
    assert run.keys(before=16) == [_key(walk_off), _key(result)]
    assert [why for _, _, why in [(n.at, n.role, n.why) for n in run.of(CardNoteType)]][:2] == [
        "Angels at D-backs: the play that decided it",
        "Angels at D-backs: final",
    ]

    # The Braves' own final, while the Rockies act: its walk-off waits on a promotion that can't cut into action.
    atl, col = games(1)[0], games(5)[4]
    braves_walk_off = big_play(atl, "wo", window=300)
    braves_final = final(atl, ended=-1, after=(braves_walk_off.dedupe_key,))
    waiting = Run(favorites=Favorites({League.MLB: ["ATL", "COL"]}))
    waiting.stage(live(col), braves_walk_off, braves_final)
    waiting.describe(facts(col, 0.4))
    waiting.offers.append(major(braves_walk_off, 0.9, model=False))
    waiting.publish()
    waiting.run(0, 100)
    assert set(waiting.keys()) == {_key(live(col))}
    waiting.run(101, 140)  # retired from the broker past +108, it airs as the result's decisive play
    assert waiting.keys(since=101)[:2] == [_key(braves_walk_off), _key(braves_final)]


def test_a_status_shows_once_and_a_favorite_s_again_after_120_open_seconds() -> None:
    slate = games(3)
    atl, other, third = slate
    run = Run()
    run.stage(status(atl), status(third), live(other))
    run.describe(facts(other, 0.4))
    run.publish()
    run.run(0, 260)
    assert [up for up, _ in run.shown(_key(status(third)))] == [8.0]
    assert [up for up, _ in run.shown(_key(status(atl)))] == [0.0, 124.0, 252.0]  # the first decisions once due
    assert run.cards()[0][2] == _key(status(atl)) and "Braves: delayed" in [n.why for n in run.of(CardNoteType)]
