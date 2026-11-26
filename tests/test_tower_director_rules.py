"""The board under `omni-board-policy/2`, rule by rule: NFL's line in rotation, a favorite's break and its guests,
leases, promotion offers, the trace's records, and the settings under either policy."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import Any

import pytest

from omni.app.versions import PolicyName
from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, DisplayTiming, LayoutSupport, ScoreboardCard
from omni.cards.football import LiveFootballCardPayload, PlayRef, StagedCaption
from omni.core.clock import MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile, SeasonStage
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.breaks import BreakKind
from omni.domain.contest import GolfTournament
from omni.domain.football import FootballGameState, PlaySummary, PlayTone
from omni.policy.allocation import AllocationPolicy
from omni.policy.leases import LeaseRefusal
from omni.policy.promotion import CandidateOrigin, PromotionRefusal
from omni.policy.settings import Mutes, PolicySettings
from omni.preferences.favorites import Favorites
from omni.queue.scheduler import InterleavedCardQueue
from omni.tower.board import BoardFrame, BoardSession, FrameKind
from omni.tower.decisions import (
    BaseReason,
    BaseTurnNote,
    CardNote,
    ClampNote,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    OfferSkip,
    WaitNote,
)
from omni.tower.director import DirectorPolicy
from omni.tower.lane import EndReason
from tests.board_policy import (
    PROFILES,
    T0,
    Run,
    action,
    at,
    big_play,
    evidence,
    facts,
    final,
    games,
    live,
    major,
    offline,
)
from tests.football_fixtures import nfl_game

NO_FAVORITES = Favorites()
_STATE = FootballGameState(period=2, clock_seconds=600, away_score=7, home_score=3)
_FIRST = PlaySummary(tone=PlayTone.STANDARD, headline="Run for 5")
_SECOND = PlaySummary(tone=PlayTone.SCORE, headline="Field goal good")


def _key(card: ScoreboardCard[Any]) -> str:
    return card.dedupe_key.raw


def _football(
    raw: str,
    away: str,
    home: str,
    *,
    caption: PlaySummary | None,
    available: float = 0,
    staged: StagedCaption | None = None,
) -> ScoreboardCard[Any]:
    contest = nfl_game(away, home, raw)
    key = f"{contest.id}:live"
    return ScoreboardCard(
        id=CardId(key),
        kind=CardKind.LIVE_GAME,
        contest=contest,
        timing=DisplayTiming(
            available_at=T0 + timedelta(seconds=available),
            min_display=DurationSeconds(8),
            max_display=DurationSeconds(30),
        ),
        priority=CardPriority(band=DisplayPriority.NORMAL, score=0.0),
        layout_support=LayoutSupport(profiles=frozenset(PROFILES)),
        dedupe_key=DedupeKey(key),
        payload=LiveFootballCardPayload(state=_STATE, last_play=caption, caption=staged),
    )


def _caption(run: Run, seconds: float) -> PlaySummary | None:
    payload = next(cue.payload for when, cue in reversed(run.cues) if when <= seconds)
    assert isinstance(payload, ScoreboardCard) and isinstance(payload.payload, LiveFootballCardPayload)
    return payload.payload.last_play


def _lines(run: Run, card: ScoreboardCard[Any], since: float, until: float) -> list[tuple[float, PlaySummary | None]]:
    """Each frame ``card`` was cued with from ``since`` until (not at) ``until``, with the line it drew."""
    drawn = []
    for when, cue in run.cues:
        payload = cue.payload
        if since <= when < until and isinstance(payload, ScoreboardCard) and _key(payload) == _key(card):
            assert isinstance(payload.payload, LiveFootballCardPayload)
            drawn.append((when, payload.payload.last_play))
    return drawn


def _alone(card: ScoreboardCard[Any], *, when: float) -> Run:
    """``card``'s game the only one on, its card up from 0, and the board run to ``when``."""
    run = Run(favorites=NO_FAVORITES)
    run.stage(card)
    run.describe(facts(card.contest, 0.4))  # type: ignore[arg-type]
    run.publish()
    run.run(0, when)
    return run


# --- NFL's line in rotation ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_an_nfl_line_holds_to_its_turn_s_end_and_shows_again_on_its_game_s_next_turn(profile: PanelProfile) -> None:
    steelers = _football("1", "PIT", "CIN", caption=_FIRST)
    ravens = _football("2", "BAL", "CLE", caption=None)
    run = Run(profile=profile, favorites=NO_FAVORITES)
    run.stage(steelers, ravens)
    run.describe(facts(steelers.contest, 0.4), facts(ravens.contest, 0.4))  # type: ignore[arg-type]
    run.publish()
    run.run(0, 40)
    turns = run.shown(_key(steelers))
    assert turns == [(0, 12), (24, 36)]  # the game's turn, and its next
    for up, down in turns:
        assert down is not None
        drawn = _lines(run, steelers, up, down)
        assert drawn[0][0] == up and drawn[-1][0] > up  # its first frame and a later one, the turn's last
        assert {line for _, line in drawn} == {_FIRST}  # up at the turn's start, still up on its last frame


def test_a_lone_nfl_card_shows_its_line_on_consecutive_turns_in_one_frame() -> None:
    card = _football("1", "PIT", "CIN", caption=_FIRST)
    run = _alone(card, when=30)
    assert [(up.at, up.down) for up in run.ups] == [(0.0, None)]  # its game won again: its frame stayed up
    assert _lines(run, card, 0, 30) == [(0, _FIRST), (8, _FIRST), (12, _FIRST), (24, _FIRST)]  # turns at 0, 12, 24


def test_a_newer_play_mid_turn_takes_the_line_s_place_with_no_blank_frame_between() -> None:
    card = _football("1", "PIT", "CIN", caption=_FIRST)
    run = Run(favorites=NO_FAVORITES)
    run.stage(card)
    run.describe(facts(card.contest, 0.4))  # type: ignore[arg-type]
    newer = _football("1", "PIT", "CIN", caption=_SECOND, available=10)
    run.publish_at(10, lambda: run.stage(newer, when=10))  # 2 s of the turn left: too little to start one, not to swap
    run.publish()
    run.run(0, 11)
    assert _lines(run, card, 0, 12) == [(0, _FIRST), (8, _FIRST), (10, _SECOND)]


@pytest.mark.parametrize(("when", "line"), [(8, _FIRST), (8.5, None)])
def test_a_line_starts_mid_turn_only_with_4_s_of_the_turn_left(when: float, line: PlaySummary | None) -> None:
    card = _football("1", "PIT", "CIN", caption=None)
    run = Run(favorites=NO_FAVORITES)
    run.stage(card)
    run.describe(facts(card.contest, 0.4))  # type: ignore[arg-type]
    run.publish_at(when, lambda: run.stage(_football("1", "PIT", "CIN", caption=_FIRST, available=when), when=when))
    run.publish()
    run.run(0, 12)
    assert (_caption(run, when), _caption(run, 11.9)) == (line, line)  # it starts at once, or not this turn
    assert _caption(run, 12) == _FIRST  # the next turn has the runway


def test_a_line_starts_no_turn_from_its_latest_start_but_one_up_by_then_holds_to_its_turn_s_end() -> None:
    staged = StagedCaption(play=PlayRef("p", "p", 0), latest_start=MonotonicTime(6), hard_end=MonotonicTime(36))
    card = _football("1", "PIT", "CIN", caption=_FIRST, staged=staged)
    run = _alone(card, when=30)
    assert _lines(run, card, 0, 30) == [(0, _FIRST), (8, _FIRST), (12, None), (24, None)]  # turns at 0, 12, 24


# --- A favorite's break and its guests -----------------------------------------------------------------------------


def test_a_favorite_whose_break_cant_fit_a_guest_gets_its_own_turn_back() -> None:
    car = nfl_game("CAR", "ATL", "9")
    mlb = games(2)[1]
    run = Run(favorites=Favorites({League.NFL: ["CAR"]}))
    run.stage(_football("9", "CAR", "ATL", caption=None), live(mlb))
    timeout = evidence(car, BreakKind.NFL_TEAM_TIMEOUT, received=-45, onset=14)  # 16 s left: a 12 s guest needs 18
    run.describe(facts(car, 0.4, breaks=(timeout,)), facts(mlb, 0.4))
    run.publish()
    run.step(0)
    [refused] = run.of(GuestNote)
    assert (refused.card.raw, refused.refusal) == (_key(live(mlb)), LeaseRefusal.NO_ROOM)
    [back] = run.of(BaseTurnNote)
    assert (back.contest, back.reason, back.debt, back.actual) == (car.id, BaseReason.RETURN, 0.0, None)
    assert back.why == "Panthers: favorite, back before its break ends"


def test_a_break_that_hosted_its_one_guest_refuses_an_ordinary_card_too() -> None:
    atl, other, done = games(3)
    run = Run()
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=-45),)), facts(other, 0.4))
    run.publish()
    run.step(0)  # the other game is the visit's one guest
    result = final(done, ended=-1, available=1)
    run.stage(result, when=1)
    run.publish()
    run.run(1, 30)
    refusals = [(note.card.raw, note.refusal) for note in run.of(GuestNote) if note.refusal is not None]
    assert (_key(result), LeaseRefusal.TURNS_SPENT) in refusals
    assert run.shown(_key(result)) == []


def test_a_guest_whose_host_s_game_is_over_runs_its_full_turn() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_INNING_BREAK, received=-45),)), facts(other, 0.4))
    run.publish()
    run.step(0)
    del run.facts[atl.id]
    run.publish()
    run.run(1, 13)
    assert run.shown(_key(live(other)))[0] == (0.0, None)  # still up: nothing closed the break it was lent


# --- Leases --------------------------------------------------------------------------------------------------------


def test_leases_are_written_opened_ended_and_refused_with_their_terms() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other))
    stated = evidence(atl, BreakKind.MLB_INNING_BREAK, received=-45, stated=145, onset=None)
    run.describe(facts(atl, 0.4, breaks=(stated,)), facts(other, 0.4))
    run.publish()
    run.step(0)
    run.describe(facts(atl, 0.4, breaks=(dataclasses.replace(stated, ended=True),)))
    run.publish()
    run.step(1)
    late = evidence(other, BreakKind.MLB_MOUND_VISIT, received=-45, onset=30, marker="v")  # most of its 36 s gone
    rain = evidence(atl, BreakKind.MLB_WEATHER, received=-44, marker="w")
    run.describe(facts(other, 0.4, breaks=(late,)), facts(atl, 0.4, breaks=(rain,)))
    run.publish()
    run.step(2)
    opened, ended, rained, refused = run.of(LeaseNote)
    assert (opened.event, opened.remaining, opened.provisional, opened.turns) == (
        LeaseEvent.OPENED,
        timedelta(seconds=115),  # 145 - 15, less one poll interval for its unknown onset
        True,
        None,
    )
    assert (ended.event, rained.event, rained.remaining) == (LeaseEvent.ENDED, LeaseEvent.OPENED, None)
    assert (refused.event, refused.refusal) == (LeaseEvent.REFUSED, LeaseRefusal.BACKFILL)


# --- Promotion offers ----------------------------------------------------------------------------------------------


def test_offers_the_board_cant_air_are_kept_from_the_broker_with_why() -> None:
    atl, other = games(2)
    quad_only = dataclasses.replace(
        big_play(other, "q"), layout_support=LayoutSupport(profiles=frozenset({PanelProfile.QUAD_128X64}))
    )
    run = Run(profile=PanelProfile.SINGLE_64X32)
    run.stage(live(atl), live(other), quad_only)
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    baseline = dataclasses.replace(major(big_play(other, "b"), 3.5), origin=CandidateOrigin.BASELINE)
    run.stage(big_play(other, "b"))
    run.offers.extend((major(big_play(other, "missing"), 3.5), major(quad_only, 3.5), baseline))
    run.publish()
    run.step(0)
    run.publish()  # offered again: each reason is written once
    run.step(1)
    assert [note.reason for note in run.of(WaitNote)] == [
        OfferSkip.NOT_IN_VIEW,
        OfferSkip.NOT_DRAWABLE,
        PromotionRefusal.BASELINE,
    ]


# --- The trace's records -------------------------------------------------------------------------------------------


def test_a_rescue_and_a_clamp_are_written_with_their_turns() -> None:
    atl, other = games(2)
    rules = DirectorPolicy(allocation=AllocationPolicy(rescue_turns=0.3, debt_limit=timedelta(seconds=1)))
    run = Run(rules=rules)
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 1.0), facts(other, 0.0))
    run.publish()
    run.run(0, 72)
    rescued = [note for note in run.of(BaseTurnNote) if note.reason is BaseReason.RESCUE]
    assert rescued[0].contest == other.id and rescued[0].rescue is not None and rescued[0].rescue.misses == 4
    assert rescued[0].why == "Angels at D-backs: lopsided game, overdue for a turn"
    clamps = [clamp for note in run.of(ClampNote) for clamp in note.clamps]
    assert clamps[0].limit in (1.0, -1.0)


def test_a_league_without_a_season_length_has_no_base_turns_and_its_cards_rotate() -> None:
    atl = games(1)[0]
    rules = DirectorPolicy(allocation=AllocationPolicy(season_games=((League.NFL, 17),)))
    run = Run(rules=rules)
    run.stage(live(atl))
    run.describe(facts(atl, 0.4))
    run.publish()
    run.step(0)
    assert run.of(BaseTurnNote) == [] and run.cards()[0][1] == "neutral"


def test_a_card_without_two_teams_is_named_by_its_league_and_muted_with_it() -> None:
    open_ = GolfTournament(
        id=LeagueScopedId(League.PGA, SourceRef("pga"), "t1"),
        league=League.PGA,
        status=GameStatus.LIVE,
        scheduled_start=T0,
        tournament_name="The Open",
    )
    card = dataclasses.replace(live(games(1)[0]), contest=open_, id=CardId("pga:t1"), dedupe_key=DedupeKey("pga:t1"))
    run = Run()
    run.stage(card)
    run.publish()
    run.step(0)
    assert [note.why for note in run.of(CardNote)] == ["PGA game: no live game to weigh"]
    muted = Run(settings=PolicySettings(mutes=Mutes.of(leagues=[League.PGA])))
    muted.stage(card)
    muted.publish()
    muted.step(0)
    assert muted.ups == []


# --- Settings under either policy ---------------------------------------------------------------------------------


def test_under_policy_v1_settings_mute_cards_and_set_the_turn() -> None:
    atl, other, third = games(3)
    queue = InterleavedCardQueue()
    queue.ingest_all((live(atl), live(other), live(third)), at(0))
    settings = PolicySettings(
        policy=PolicyName.BOARD_V1, mutes=Mutes.of(teams={League.MLB: ["ATL"]}), slot=timedelta(seconds=20)
    )
    board = BoardSession(queue, PanelProfile.QUAD_128X64, settings=settings)
    shown: list[tuple[float, str]] = []
    for seconds in (0, 12, 20, 40, 60):
        board.publish(board.health)
        cue = board.cue(at(seconds))
        board.committed(cue, at(seconds))
        shown.append((seconds, cue.payload.contest.id.raw))  # type: ignore[union-attr]
    assert [raw for _, raw in shown] == ["g02", "g02", "g01", "g02", "g01"]  # 20 s turns, the Braves never
    assert board.decisions() == []
    unmuted = BoardSession(queue, PanelProfile.QUAD_128X64, settings=PolicySettings(policy=PolicyName.BOARD_V1))
    unmuted.publish(unmuted.health)
    assert unmuted.cue(at(70)).payload is not None


def test_a_director_policy_keeps_its_runs_positive_and_reads_a_poll_interval_for_any_league() -> None:
    with pytest.raises(ValueError, match="positive"):
        DirectorPolicy(line_runway=timedelta(0))
    rules = DirectorPolicy()
    assert (rules.poll_interval(League.NFL), rules.poll_interval(League.NHL)) == (
        timedelta(seconds=10),
        timedelta(seconds=15),
    )


def test_resumed_action_without_a_lease_revokes_nothing() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 0.4, action=action(atl, "ab 1 p1", received=-45)), facts(other, 0.4))
    run.publish()
    run.step(0)
    assert run.of(LeaseNote) == []


# --- Edges of a turn ---------------------------------------------------------------------------------------------


def test_a_big_play_whose_game_s_card_is_gone_at_its_handover_ends_the_turn_there() -> None:
    atl, other = games(2)
    play = big_play(other, "p1")
    run = Run(settings=PolicySettings(promotions=False), favorites=NO_FAVORITES)
    run.stage(live(atl), live(other), play)
    run.describe(facts(atl, 0.0), facts(other, 1.0))
    run.publish()
    run.step(0)
    assert run.ups[0].key == _key(play)
    [turn] = run.of(BaseTurnNote)
    assert turn.why == "Angels at D-backs: close late game, its big play first"
    run.queue.remove(_key(live(other)))
    run.publish()
    run.run(3, 7)
    assert run.shown(_key(play)) == [(0.0, 6.0)] and run.ups[1].key == _key(live(atl))


def test_a_lone_card_too_short_for_another_turn_leaves_the_board_idle() -> None:
    play = big_play(games(2)[1], "p1", window=15)
    run = Run(favorites=NO_FAVORITES)
    run.stage(play)
    run.publish()
    run.run(0, 14)
    assert run.shown(_key(play)) == [(0.0, 12.0)]  # 3 s left: less than its minimum
    assert run.changes[-2].reason is EndReason.RAN_ITS_TIME


def test_a_live_card_whose_game_left_the_pool_hands_off_to_its_final_as_a_result() -> None:
    atl, other = games(2)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 1.0), facts(other, 0.0))
    run.publish()
    run.step(0)
    run.queue.remove(_key(live(atl)))
    ended = dataclasses.replace(final(atl, ended=8, available=8), replaces=live(atl).dedupe_key)
    run.stage(ended, when=8)
    del run.facts[atl.id]
    run.publish()
    run.run(8, 9)
    assert run.ups[1].key == _key(ended) and run.cards()[0][1] == "result"


def test_a_reserved_recovery_cut_short_is_still_owed_to_the_next_base_card() -> None:
    slate = games(8)
    first, second = slate[1], slate[2]
    finals = [final(each, ended=-50 + 10 * index) for index, each in enumerate(slate[3:7])]
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(first), live(second), *finals)
    run.describe(facts(first, 1.0), facts(second, 0.0))
    run.publish()
    run.run(0, 32)
    run.queue.remove(_key(live(first)))
    del run.facts[first.id]
    run.publish()
    run.run(35, 50)
    recoveries = [note for note in run.of(BaseTurnNote) if note.reason is BaseReason.RECOVERY]
    assert [(note.at.monotonic.seconds, note.contest) for note in recoveries] == [(32.0, first.id), (35.0, second.id)]
    assert run.keys(since=35) == [_key(live(second))]  # the full turn it was owed, then its frame stays


def test_an_outage_before_any_card_goes_up_just_shows_offline() -> None:
    run = Run()
    run.health = offline()
    run.publish()
    cue = run.step(0)
    assert isinstance(cue.payload, BoardFrame) and cue.payload.kind is FrameKind.OFFLINE


def test_a_postseason_game_s_turn_says_so() -> None:
    playoff = dataclasses.replace(games(2)[1], stage=SeasonStage.POSTSEASON)
    run = Run()
    run.stage(live(playoff))
    run.describe(facts(playoff, 0.4))
    run.publish()
    run.step(0)
    assert run.of(BaseTurnNote)[0].why == "Angels at D-backs: postseason, the only game on"


def test_a_line_dropped_while_it_airs_leaves_the_field_as_it_is() -> None:
    card = _football("1", "PIT", "CIN", caption=_FIRST)
    run = Run(favorites=NO_FAVORITES)
    run.stage(card)
    run.describe(facts(card.contest, 0.4))  # type: ignore[arg-type]
    run.publish()
    run.step(0)
    plain = _football("1", "PIT", "CIN", caption=None, available=4)
    run.stage(plain, when=4)
    run.publish()
    run.run(4, 11)
    assert run.cues[-1][1].payload is plain


def test_a_card_cued_but_not_yet_drawn_takes_its_newer_version_and_one_changed_while_drawn_counts_for_nothing() -> None:
    atl = games(1)[0]
    run = Run()
    run.stage(live(atl))
    run.describe(facts(atl, 0.4))
    run.publish()
    cued = run.board.cue(at(0))  # the display stalls
    newer = live(atl, available=1)
    run.stage(newer, when=1)
    run.publish()
    again = run.board.cue(at(1))
    assert again.occurrence == cued.occurrence and again.payload is newer and again.segment.revision == 1
    newest = live(atl, available=2)
    run.stage(newest, when=2)
    run.publish()
    run.board.committed(again, at(2))  # what it drew is no longer what the board holds
    assert run.board.changes() == [] and run.board.decisions() == []
    run.step(2)
    assert run.ups[0].at == 2.0


def test_a_promoted_nfl_card_shows_its_line_whole_and_the_game_s_next_turn_shows_it_again() -> None:
    scoring = _football("1", "PIT", "CIN", caption=_SECOND)
    other = _football("2", "BAL", "CLE", caption=None)
    run = Run(favorites=NO_FAVORITES)
    run.stage(scoring, other)
    run.describe(facts(scoring.contest, 0.4), facts(other.contest, 0.4))  # type: ignore[arg-type]
    run.offers.append(major(scoring, 0.9, model=False))
    run.publish()
    run.run(0, 40)
    assert run.promotions() == [(0.0, _key(scoring))]
    assert _lines(run, scoring, 0, 12) == [(0, _SECOND), (8, _SECOND)]  # its 12 s
    later = _lines(run, scoring, 12, 40)
    assert later and {line for _, line in later} == {_SECOND}  # still its game's newest play: rotation shows it


def test_what_the_board_remembers_of_cards_and_games_goes_with_them() -> None:
    atl, other = games(2)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    run.offers.append(major(big_play(other, "gone"), 0.9, model=False))
    run.publish()
    run.run(0, 30)
    assert [note.reason for note in run.of(WaitNote)] == [OfferSkip.NOT_IN_VIEW]
    run.offers.clear()
    run.publish()
    run.step(31)
    run.offers.append(major(big_play(other, "gone"), 0.9, model=False))
    run.publish()
    run.step(32)
    assert [note.reason for note in run.of(WaitNote)] == [OfferSkip.NOT_IN_VIEW] * 2  # forgotten, then new again
