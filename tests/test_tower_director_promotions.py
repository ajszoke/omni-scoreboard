"""A promotion airs its exact event, under `omni-board-policy/2`, through `BoardSession` in the synthetic time loop of
`tests.board_policy`: an offer the published view withdrew never airs, nor is cited; an offer whose card no longer
shows its event is never promoted; a moment whose story aired is never promoted again, whenever its offer arrives;
and a start counts only at the offered card's confirmed first frame."""

from __future__ import annotations

import dataclasses
from typing import Any

from omni.cards.base import ScoreboardCard
from omni.cards.baseball import FinalCardPayload
from omni.core.enum import League
from omni.domain.football import PlaySummary, PlayTone
from omni.policy.interrupts import InterruptClass
from omni.policy.promotion import PromotionRefusal
from omni.preferences.favorites import Favorites
from omni.tower.board import BoardCue
from omni.tower.decisions import CardNote, CardRole, OfferSkip, PromotionNote, UnspentNote, WaitNote
from tests.board_policy import Run, at, big_play, facts, final, games, live, major, reminder
from tests.test_tower_director_rules import _football

NO_FAVORITES = Favorites()
_A = PlaySummary(tone=PlayTone.SCORE, headline="Go-ahead touchdown")
_B = PlaySummary(tone=PlayTone.STANDARD, headline="Run for 2")


def _key(card: ScoreboardCard[Any]) -> str:
    return card.dedupe_key.raw


def _waits(run: Run) -> list[tuple[float, str, PromotionRefusal | OfferSkip]]:
    return [(note.at.monotonic.seconds, note.key.play, note.reason) for note in run.of(WaitNote)]


def _cooling(run: Run, contest: Any) -> ScoreboardCard[Any]:
    """A big play promoted at 0, so the board's cooldown holds every other promotion until 60."""
    first = big_play(contest, "first")
    run.stage(first)
    run.offers.append(major(first, 0.9, model=False))
    return first


def test_a_perfect_game_offer_a_correction_withdrew_never_airs_and_no_line_cites_it() -> None:
    """The perfect game waits out the cooldown when a correction takes it back at 20: the view no longer offers it, so
    it's withdrawn there, and the corrected final airs as an ordinary result, never as a perfect game."""
    slate = games(3)
    playing, done = slate[1], slate[2]
    perfect = final(done, ended=-10, available=0)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(playing), perfect)
    run.describe(facts(playing, 0.4))
    first = _cooling(run, playing)
    feat = major(perfect, 1.1375, eligible=5, event=InterruptClass.PERFECT_GAME)
    run.publish_at(5, lambda: run.offers.append(feat))

    def corrected() -> None:
        payload = perfect.payload
        assert isinstance(payload, FinalCardPayload)
        run.stage(dataclasses.replace(perfect, payload=dataclasses.replace(payload, away_score=2)), when=20)
        run.offers.remove(feat)
        run.publish()

    run.on(20, corrected)
    run.publish()
    run.run(0, 120)
    assert run.promotions() == [(0.0, _key(first))]
    assert not [note for note in run.notes if "perfect game" in getattr(note, "why", "")]
    assert (20.0, _key(perfect), PromotionRefusal.WITHDRAWN) in _waits(run)
    results = [note for note in run.of(CardNote) if note.role is CardRole.RESULT]
    assert [(note.card.raw, note.why) for note in results] == [(_key(perfect), "Orioles at Red Sox: final")]


def test_an_offer_whose_card_now_shows_another_play_is_never_promoted() -> None:
    """Go-ahead play A is offered naming the Steelers' card while A is its caption; by 20 the card shows routine play
    B under the same offer. Promoted at 60, it would air B as A's moment: it's withdrawn when the card changes."""
    steelers = _football("1", "PIT", "CIN", caption=_A)
    elsewhere = games(2)[1]
    run = Run(favorites=NO_FAVORITES)
    run.stage(steelers, live(elsewhere))
    run.describe(facts(steelers.contest, 0.4), facts(elsewhere, 0.4))  # type: ignore[arg-type]
    first = _cooling(run, elsewhere)
    go_ahead = major(steelers, 0.9, eligible=5, model=False, play="nfl:play:a")
    run.publish_at(5, lambda: run.offers.append(go_ahead))
    run.publish_at(20, lambda: run.stage(_football("1", "PIT", "CIN", caption=_B, available=20), when=20))
    run.publish()
    run.run(0, 120)
    assert run.promotions() == [(0.0, _key(first))]
    assert (20.0, "nfl:play:a", OfferSkip.CHANGED) in _waits(run)


def test_a_newer_revision_of_a_changed_card_is_taken_afresh_and_keeps_its_eligibility() -> None:
    steelers = _football("1", "PIT", "CIN", caption=_A)
    elsewhere = games(2)[1]
    run = Run(favorites=NO_FAVORITES)
    run.stage(steelers, live(elsewhere))
    run.describe(facts(steelers.contest, 0.4), facts(elsewhere, 0.4))  # type: ignore[arg-type]
    _cooling(run, elsewhere)
    go_ahead = major(steelers, 0.9, eligible=5, model=False, play="nfl:play:a")
    run.publish_at(5, lambda: run.offers.append(go_ahead))
    revised = _football("1", "PIT", "CIN", caption=PlaySummary(tone=PlayTone.SCORE, headline="Go-ahead TD"))

    def revise() -> None:
        run.stage(dataclasses.replace(revised, timing=dataclasses.replace(revised.timing)), when=20)
        run.offers[run.offers.index(go_ahead)] = dataclasses.replace(go_ahead, revision=1, eligible=at(20).monotonic)
        run.publish()

    run.on(20, revise)
    run.publish()
    run.run(0, 120)
    [promoted] = [note for note in run.of(PromotionNote) if note.key.play == "nfl:play:a"]
    assert promoted.at.monotonic.seconds == 60.0  # the cooldown's end: one promotion, the revision's
    assert not [reason for _, play, reason in _waits(run) if play == "nfl:play:a" and reason is OfferSkip.CHANGED]


def test_a_big_play_shown_in_its_game_s_turn_before_its_offer_arrived_is_never_promoted_after() -> None:
    """The Angels' homer leads their turn at 0, before its offer clears at 10: that offer is its story told already,
    so it's refused, and no promotion repeats the play."""
    slate = games(3)
    angels, other = slate[1], slate[2]
    homer = big_play(angels, "hr")
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(angels), live(other), homer)
    run.describe(facts(angels, 0.4), facts(other, 0.4))
    run.publish_at(10, lambda: run.offers.append(major(homer, 0.9, eligible=10, model=False)))
    run.publish()
    run.run(0, 120)
    assert run.shown(_key(homer)) == [(0.0, 6.0)]
    assert run.promotions() == []
    assert _waits(run) == [(10.0, _key(homer), PromotionRefusal.ALREADY_SHOWN)]


def test_a_promoted_moment_is_never_promoted_again_while_its_game_is_on() -> None:
    """Offered again at a new revision 20 minutes on, long after the broker's own memory of it lapsed, it's refused."""
    slate = games(3)
    angels, other = slate[1], slate[2]
    homer = big_play(angels, "hr", window=1500)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(angels), live(other), homer)
    run.describe(facts(angels, 0.4), facts(other, 0.4))
    offer = major(homer, 0.9, model=False)
    run.offers.append(offer)
    again = dataclasses.replace(offer, revision=1, eligible=at(1200).monotonic)
    run.publish_at(130, lambda: run.offers.clear())
    run.publish_at(1200, lambda: run.offers.append(again))
    run.publish()
    run.run(0, 1300)
    assert run.promotions() == [(0.0, _key(homer))]
    assert (1200.0, _key(homer), PromotionRefusal.ALREADY_SHOWN) in _waits(run)


def test_a_promotion_withdrawn_between_its_cue_and_its_first_frame_comes_down_unspent() -> None:
    slate = games(3)
    angels, other = slate[1], slate[2]
    homer = big_play(angels, "hr")
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(angels), live(other), homer)
    run.describe(facts(angels, 0.4), facts(other, 0.4))
    run.offers.append(major(homer, 0.9, model=False))
    run.publish()
    cue: BoardCue = run.board.cue(at(0))
    assert isinstance(cue.payload, ScoreboardCard) and cue.payload.dedupe_key.raw == _key(homer)
    run.offers.clear()
    run.publish()  # the correction lands while the frame is being drawn
    run.board.committed(cue, at(0))
    run.notes.extend(run.board.decisions())
    assert run.promotions() == []
    [unspent] = run.of(UnspentNote)
    assert unspent.reason is PromotionRefusal.NOT_PENDING
    run.run(1, 70)
    assert run.promotions() == [] and run.of(PromotionNote) == []


def test_a_reminder_waiting_when_its_bid_is_broken_is_withdrawn() -> None:
    slate = games(3)
    bid, other = slate[1], slate[2]
    nohitter = big_play(bid, "nohitter", window=1000)
    run = Run(favorites=NO_FAVORITES)
    run.stage(live(bid), live(other), nohitter)
    run.describe(facts(bid, 0.4), facts(other, 0.4))
    first = _cooling(run, other)
    run.publish_at(5, lambda: run.offers.append(reminder(nohitter, eligible=5)))
    run.publish_at(30, lambda: run.offers.remove(run.offers[-1]))  # the bid is broken
    run.publish()
    run.run(0, 90)
    assert run.promotions() == [(0.0, _key(first))]
    assert (30.0, "bid reminder 1", PromotionRefusal.WITHDRAWN) in _waits(run)


def test_a_candidate_past_its_latest_start_when_its_offer_leaves_the_view_is_retired_not_withdrawn() -> None:
    """The Braves act through its whole lifetime, so the homer below the action bar retires at +108 s; the view stops
    offering it at 110, inside a turn's protection, before any decision has swept it: it's written as run out, not
    as taken back."""
    slate = games(3)
    atl, angels = slate[0], slate[1]
    homer = big_play(angels, "hr", window=200)
    run = Run(favorites=Favorites({League.MLB: ["ATL"]}))
    run.stage(live(atl), homer)
    run.describe(facts(atl, 0.5))
    run.offers.append(major(homer, 0.9, model=False))
    run.publish()
    run.run(0, 109)
    run.offers.clear()
    run.publish()
    run.step(110)  # the Braves' turn began at 108: a cue inside its protection
    assert [(play, reason) for _, play, reason in _waits(run)][-1] == (_key(homer), PromotionRefusal.RETIRED)
    assert PromotionRefusal.WITHDRAWN not in [reason for _, _, reason in _waits(run)]
