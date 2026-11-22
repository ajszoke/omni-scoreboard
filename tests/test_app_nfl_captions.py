"""NFL's line on the shared board: staged with its entry and bounds, recent for 5 minutes from its release with no
confirmed frame needed, never history, and only beside a field that agrees with it; on each of its game's turns while
it's recent, held to the turn's end; and a stale card current again only once a state sent after the outage clears
the delay. A final's caption, by contrast, is started by a confirmed frame the board took.

Most tests drive `LiveFootballPipeline.refresh` (and `presented`, the display's word that a frame was shown) over a
fake `NflFeeds` (`tests.test_app_nfl_pipeline`) on a fake clock, with a 45 s NFL delay unless they say otherwise; the
board's own run through `AppLoop` has none.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import LiveBaseballPipeline
from omni.cards.factory import CardFactory
from omni.cards.football import LiveFootballCardPayload, PlayRef, StagedCaption
from omni.core.clock import FakeClock, MonotonicTime
from omni.core.enum import PanelProfile
from omni.core.time import DurationSeconds
from omni.policy.settings import PolicySettings
from omni.providers.nflmeta import NflPhase
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue
from omni.tower.decisions import BaseTurnNote
from tests.test_app_loop_nfl import _Board, _mlb_feed, nfl_slate
from tests.test_app_nfl_final_drain import TIED, WON
from tests.test_app_nfl_pipeline import Board, FakeNflFeeds, T, drive, nfl_game, play
from tests.test_app_nfl_play_cards import _Drawn

GAME = nfl_game()
S = timedelta(seconds=1)


def _staged(board: Board, release: float, *, major: bool = False) -> None:
    """A play written as its answer arrives at ``release`` - 45, so it clears the delay at ``release``."""
    board.answer(GAME, drive())
    board.at(release - 50, GAME)
    board.to(release - 45)
    board.answer(GAME, drive(clock=590), events=(play("p", board.now.utc, major=major),))
    board.at(release, GAME)


# --- the line's bounds ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("major", [False, True])
def test_a_line_is_staged_with_its_entry_and_one_set_of_bounds_and_needs_no_confirmed_frame(major: bool) -> None:
    board = Board()
    _staged(board, 50, major=major)
    assert board.payload(GAME).caption == StagedCaption(
        play=PlayRef("p", "p", 0), latest_start=MonotonicTime(350), hard_end=MonotonicTime(380)
    )  # it may first go up until 5 minutes after its release, and a turn of its card later it's gone
    board.to(55)
    board.show(GAME)  # the display's word that a frame carried it changes nothing: it has no start to miss
    board.refresh(GAME)
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(380)
    assert board.at(350, GAME).skipped == () and board.caption(GAME) == "RUN +4"  # its latest start: still carried


def test_the_board_s_confirmed_frames_start_the_final_s_caption_they_carried() -> None:
    """`AppLoop.paint` tells the pipeline what each frame the display took carried: the final's deciding play, shown
    before its latest start, stays past it."""
    board = _Board(nfl_slate(GAME))  # no delay
    at = board.to(0)
    board.loop.refresh(at)
    assert board.loop.paint(at).frame is not None  # nothing to show yet: an explicit frame
    board.feeds.answer(GAME, TIED, at)
    board.loop.refresh(at)
    board.loop.paint(at)
    at = board.to(5)
    board.feeds.answer(GAME, WON, at, phase=NflPhase.POST)  # the scoreboard says final before the kick is in
    board.loop.refresh(at)
    board.loop.paint(at)
    at = board.to(10)
    kick = play("kick", at.utc - 2 * S, major=True, headline="FIELD GOAL")  # the play that decided it
    board.feeds.answer(GAME, WON, at, phase=NflPhase.POST, events=(kick,))
    board.loop.refresh(at)
    board.loop.paint(at)  # the final on the panel, with its deciding play
    at = board.to(140)
    assert board.loop.refresh(at).pipeline.skipped == ()
    card = board.queue.card(f"{GAME.id}:final")
    assert card is not None and card.payload.last_play is not None  # past its latest start (130): it had started


# --- on the shared board -----------------------------------------------------------------------------------------------


def _rotation(release: int) -> dict[int, set[str | None]]:
    """`AppLoop` under `omni-board-policy/2`, with no delay: DAL@PHI and MIN@GB share the board in 12 s turns, and
    DAL@PHI's play clears at ``release``, and after it only its clock runs. Each of DAL@PHI's turns by its first
    second, with the lines its frames drew."""
    other = nfl_game("402", "MIN", "GB")
    clock = FakeClock(T)
    feeds = FakeNflFeeds()
    queue = InterleavedCardQueue()
    drawn = _Drawn()
    loop = AppLoop(
        schedules=(nfl_slate(GAME, other),),
        store=ContestStore(),
        pipeline=LiveBaseballPipeline(
            scorer=PriorityScorer(),
            delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)),
            factory=CardFactory(),
            queue=queue,
        ),
        queue=queue,
        registry=drawn,
        sink=RecordingDisplaySink(PanelProfile.QUAD_128X64),
        fetch_feed=_mlb_feed,
        football=LiveFootballPipeline(
            feeds=feeds, delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(0)), queue=queue
        ),
        settings=PolicySettings(),
    )
    turns: dict[int, set[str | None]] = {}
    turn: int | None = None
    for second in range(400):
        at = clock.read()
        if second == 0:
            feeds.answer(GAME, drive(), at)
            feeds.answer(other, drive(), at)
        elif second == release:
            feeds.answer(GAME, drive(clock=590), at, events=(play("p", at.utc),))
        elif second > release and second % 5 == 0:  # only the clock runs: the card is drawn anew, its field the same
            feeds.answer(GAME, drive(clock=590 - second // 5), at)
        loop.refresh(at)
        painted = loop.paint(at)
        for note in painted.decisions:
            if isinstance(note, BaseTurnNote):
                turn = second if note.contest == GAME.id else None
        card = drawn.card
        if turn is not None and card is not None and isinstance(card.payload, LiveFootballCardPayload):
            line = card.payload.last_play
            turns.setdefault(turn, set()).add(line.headline if line is not None else None)
        clock.advance(timedelta(seconds=1))
    return turns


@pytest.mark.parametrize(
    "release",
    [
        11,  # its turns start at 3:49, 4:13 and 4:37 after it, then at 5:01
        17,  # its turn at 4:55 holds it to that turn's last frame, at 5:06; its next, at 5:19, doesn't start it
    ],
)
def test_the_shared_board_shows_a_game_s_line_on_each_of_its_turns_until_it_s_5_minutes_old(release: int) -> None:
    # Every other turn is DAL@PHI's. One that starts after the release and before the line is 5 minutes old draws it
    # on every frame; one before the release (or with too little of it left when the play cleared), or after, none.
    expected = {start: {"RUN +4"} if release < start < release + 300 else {None} for start in range(0, 400, 24)}
    assert _rotation(release) == expected


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
