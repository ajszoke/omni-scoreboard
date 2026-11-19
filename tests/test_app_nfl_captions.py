"""NFL's caption on the shared board: staged with its entry and bounds, started only by a confirmed frame, never shown
late, never history, and only beside a field that agrees with it; and a stale card current again only once a state
sent after the outage clears the delay.

Every test drives `LiveFootballPipeline.refresh` (and `presented`, the display's word that a frame was shown) over a
fake `NflFeeds` (`tests.test_app_nfl_pipeline`) on a fake clock, with a 45 s NFL delay unless it says otherwise.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from omni.cards.football import LiveFootballCardPayload, PlayRef, StagedCaption
from omni.core.clock import MonotonicTime
from tests.test_app_loop_nfl import _Board, nfl_slate
from tests.test_app_nfl_pipeline import KICKOFF, Board, drive, nfl_game, play
from tests.test_renderer_registry import _live_card

GAME = nfl_game()
S = timedelta(seconds=1)


def _staged(board: Board, release: float, *, major: bool = False) -> None:
    """A play written as its answer arrives at ``release`` - 45, so it clears the delay at ``release``."""
    board.answer(GAME, drive())
    board.at(release - 50, GAME)
    board.to(release - 45)
    board.answer(GAME, drive(clock=590), events=(play("p", board.now.utc, major=major),))
    board.at(release, GAME)


# --- the first start is the confirmed frame -------------------------------------------------------------------------


@pytest.mark.parametrize(("major", "start_within", "label"), [(False, 60, "routine play"), (True, 120, "major play")])
def test_a_caption_no_confirmed_frame_carried_is_dropped_at_its_latest_start_and_said_so(
    major: bool, start_within: int, label: str
) -> None:
    board = Board()
    _staged(board, 50, major=major)
    payload = board.payload(GAME)
    assert payload.caption == StagedCaption(
        play=PlayRef("p", "p", 0),
        latest_start=MonotonicTime(50 + start_within),
        hard_end=MonotonicTime(50 + start_within + 30),
    )
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(50 + start_within)
    assert board.at(50 + start_within - 0.1, GAME).skipped == () and board.caption(GAME) == "RUN +4"
    dropped = board.at(50 + start_within, GAME)
    assert board.caption(GAME) is None and board.payload(GAME).caption is None
    assert dropped.skipped == (f"{GAME.id}: {label} unseen (too late to start)",)


def test_a_caption_a_confirmed_frame_carried_in_time_stays_until_its_hard_end() -> None:
    board = Board()
    _staged(board, 50)
    board.to(55)
    board.show(GAME)  # the display's word: a frame with the caption on it was shown at 55
    board.refresh(GAME)
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(140)  # no latest start left to wait on
    assert board.at(139.9, GAME).skipped == () and board.caption(GAME) == "RUN +4"
    assert board.at(140, GAME).skipped == () and board.caption(GAME) is None


def test_a_frame_shown_only_after_the_latest_start_starts_nothing() -> None:
    board = Board()
    _staged(board, 50)
    board.to(110)  # drawn before its latest start, shown at it
    board.show(GAME)
    assert board.refresh(GAME).skipped == (f"{GAME.id}: routine play unseen (too late to start)",)
    assert board.caption(GAME) is None


def test_a_frame_without_the_caption_or_with_another_entry_or_another_card_starts_nothing() -> None:
    board = Board()
    _staged(board, 50)
    card = board.card(GAME)
    assert card is not None and isinstance(card.payload, LiveFootballCardPayload)
    alone = dataclasses.replace(card, payload=dataclasses.replace(card.payload, last_play=None))  # the field alone
    staged = card.payload.caption
    assert staged is not None
    other = dataclasses.replace(staged, play=PlayRef("q", "q", 0))
    elsewhere = dataclasses.replace(card, payload=dataclasses.replace(card.payload, caption=other))
    board.to(55)
    for shown in (alone, elsewhere, _live_card()):
        board.pipeline.presented(shown, board.now)
    board.refresh(GAME)
    assert board.at(110, GAME).skipped == (f"{GAME.id}: routine play unseen (too late to start)",)


def test_the_board_s_confirmed_frames_start_the_caption_they_carried() -> None:
    """`AppLoop.paint` tells the pipeline what each frame the display took carried."""
    board = _Board(nfl_slate(GAME))  # no delay
    at = board.to(0)
    board.loop.refresh(at)
    assert board.loop.paint(at).frame is not None  # nothing to show yet: an explicit frame
    board.feeds.answer(GAME, KICKOFF, at)
    board.loop.refresh(at)
    board.loop.paint(at)
    at = board.to(5)
    board.feeds.answer(GAME, drive(clock=880), at, events=(play("p", at.utc),), current_for=timedelta(minutes=5))
    board.loop.refresh(at)
    board.loop.paint(at)  # the live card on the panel, with its caption
    at = board.to(70)
    assert board.loop.refresh(at).pipeline.skipped == ()
    card = board.queue.card(f"{GAME.id}:live")
    assert card is not None and card.payload.last_play is not None  # past its latest start: it had started


# --- history is never a current caption ------------------------------------------------------------------------------


def test_what_an_outage_s_catch_up_brings_never_captions_the_card_nor_is_offered() -> None:
    board = Board()
    board.answer(GAME, drive(10, 14, period=4, clock=300), current_for=20 * S)
    board.at(45, GAME)
    board.at(50, GAME)  # stale since 20
    board.to(60)
    touchdown = play("td", board.now.utc - 2 * S, major=True, headline="TOUCHDOWN")
    board.answer(GAME, drive(17, 14, period=4, clock=250), events=(touchdown,))
    board.refresh(GAME)  # the outage's catch-up
    released = board.at(103, GAME)
    assert released.skipped == (f"{GAME.id}: 1 from an outage's catch-up set aside",)
    assert board.at(105, GAME).policy.offers == () and board.caption(GAME) is None
    assert board.at(200, GAME).policy.offers == () and board.caption(GAME) is None


# --- a coherent field -------------------------------------------------------------------------------------------------


def test_a_caption_airs_only_beside_a_field_whose_score_agrees_with_its_own_answer() -> None:
    board = Board()
    board.answer(GAME, drive())
    board.to(10)
    touchdown = play("td", board.now.utc - 2 * S, major=True, headline="TOUCHDOWN")
    board.answer(GAME, drive(away=7), events=(touchdown,))
    board.at(53, GAME)  # the play has cleared the delay; the field it left hasn't
    assert board.payload(GAME).state.away_score == 0 and board.caption(GAME) is None
    board.at(55, GAME)
    assert board.payload(GAME).state.away_score == 7 and board.caption(GAME) == "TOUCHDOWN"


def test_a_stale_card_is_current_again_only_once_a_state_sent_after_it_went_stale_clears_the_delay() -> None:
    board = Board()
    board.answer(GAME, drive(), current_for=20 * S)
    board.at(45, GAME)
    assert board.at(50, GAME).stale == (GAME.id,)  # stale since 20
    board.to(60)
    board.answer(GAME, drive(clock=500))  # the feed answers again, inside the delay
    answering = board.refresh(GAME)
    facts = answering.policy.facts(GAME.id)
    assert answering.stale == (GAME.id,) and facts is not None and not facts.fresh
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(105)
    assert board.at(104.9, GAME).stale == (GAME.id,)
    recovered = board.at(105, GAME)
    facts = recovered.policy.facts(GAME.id)
    assert recovered.stale == () and facts is not None and facts.fresh
    card = board.card(GAME)
    assert card is not None and card.stale_since is None and card.payload.state == drive(clock=500)


def test_a_stale_card_whose_feed_goes_quiet_again_before_it_recovers_stays_stale_from_the_first() -> None:
    board = Board()
    board.answer(GAME, drive(), current_for=20 * S)
    board.at(45, GAME)
    board.to(60)
    board.answer(GAME, drive(clock=500), current_for=10 * S)
    board.refresh(GAME)  # answering, until 70
    still = board.at(80, GAME)  # quiet again, before the state from 60 cleared the delay
    assert still.stale == (GAME.id,) and still.warnings == ()
    card = board.card(GAME)
    assert card is not None and card.stale_since is not None
    assert board.at(105, GAME).stale == (GAME.id,)  # what cleared is from before it went quiet again
