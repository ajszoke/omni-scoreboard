"""An NFL offer names a card of the play's own, fixed for that play's revision: the field its answer showed, with the
play as its caption, drawn by the live football renderer. A later play never changes it; a ruling withdraws it; the
final names the one that decided the game.

Every test drives `LiveFootballPipeline.refresh` over a fake `NflFeeds` (`tests.test_app_nfl_pipeline`) on a fake
clock, refreshing as each answer arrives, with a 45 s NFL delay. Plays and states are synthetic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omni.app.contest_store import ContestStore
from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop
from omni.app.nfl_pipeline import LiveFootballPipeline
from omni.app.pipeline import LiveBaseballPipeline
from omni.cards.base import CardKind, DedupeKey, ScoreboardCard
from omni.cards.factory import CardFactory
from omni.cards.football import LiveFootballCardPayload, PlayRef, StagedCaption
from omni.core.clock import FakeClock, MonotonicTime
from omni.core.enum import PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.football import FootballGameState, FootballInterval
from omni.events.football import FootballEventKind
from omni.policy.interrupts import InterruptClass, Stakes, interrupt_value
from omni.policy.leverage import football_proxy
from omni.policy.promotion import PromotionCandidate, PromotionKey, PromotionKind
from omni.policy.settings import PolicySettings
from omni.providers.nflmeta import NflPhase
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue, SelectionReason
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.panels.geometry import geometry_for
from omni.renderers.registry import RendererRegistry, default_registry
from tests.test_app_loop_nfl import _mlb_feed, nfl_slate
from tests.test_app_nfl_pipeline import T, Board, FakeNflFeeds, drive, nfl_game
from tests.test_app_nfl_pipeline_policy import _answer, _row, _view

GAME = nfl_game()
LATE = drive(away=10, home=14, period=4, clock=300)
AHEAD = drive(away=17, home=14, period=4, clock=270)
KEY = f"{GAME.id}:play:57:r0"


def _go_ahead(board: Board) -> None:
    """A late go-ahead score (play 57, released at 53, the state after it at 55), then a routine snap (play 58,
    released at 63)."""
    _answer(board, 0, LATE, _row(50, -30, major=True))  # the first look
    _answer(board, 10, AHEAD, _row(57, 8, major=True))
    _answer(board, 20, drive(away=17, home=14, period=4, clock=260, down=2), _row(58, 18))


def test_an_offer_names_the_play_s_own_card_which_a_later_play_never_changes() -> None:
    board = Board()
    _go_ahead(board)
    score = PromotionCandidate(
        key=PromotionKey(GAME.id, "57"),
        kind=PromotionKind.MAJOR,
        eligible=MonotonicTime(53.0),
        card=DedupeKey(KEY),
        value=interrupt_value(InterruptClass.MAJOR, predecessor=football_proxy(LATE), stakes=Stakes()),
    )
    assert _view(board, 55).offers == (score,)
    card = board.queue.card(KEY)
    assert card is not None and card.kind is CardKind.BIG_PLAY and card.contest == GAME
    assert card.payload == LiveFootballCardPayload(
        state=AHEAD,
        last_play=_row(57, 8, major=True).summary,
        caption=StagedCaption(
            play=PlayRef("57:play", "57", 0), latest_start=MonotonicTime(173), hard_end=MonotonicTime(203)
        ),
    )
    assert card.timing.available_at == T + timedelta(seconds=53)
    assert card.timing.expires_at == T + timedelta(seconds=173)  # as long as its offer stands
    assert _view(board, 65).offers == (score,)
    assert board.caption(GAME) == "play 58"  # the live card has moved on to the routine snap
    assert board.queue.card(KEY) is card  # the offered card hasn't


@pytest.mark.parametrize("profile", list(PanelProfile))
def test_a_play_s_own_card_is_drawn_by_the_live_football_renderer_on_every_profile(profile: PanelProfile) -> None:
    board = Board()
    _go_ahead(board)
    board.at(55, GAME)
    card = board.queue.card(KEY)
    assert card is not None
    geometry = geometry_for(profile)
    canvas = RecordingCanvas(geometry.width, geometry.height)
    default_registry().render(card, RenderContext(profile=profile, now=board.now.utc), canvas)
    words = {text.text for text in canvas.texts()}
    assert {"17", "14"} <= words  # the score the play left
    assert ("play 57" in words) is (profile is PanelProfile.QUAD_128X64)  # the strip, where the profile has one


def test_a_try_merges_into_its_touchdown_s_card() -> None:
    board = Board()
    _answer(board, 0, LATE)
    _answer(board, 10, AHEAD, _row(57, 8, major=True))
    kicked = drive(away=18, home=14, period=4, clock=270)
    _answer(board, 15, kicked, _row(57, 13, kind=FootballEventKind.CONVERSION, major=True))  # out at 58, settled 60
    [offer] = _view(board, 61).offers
    assert offer.card == DedupeKey(KEY) and offer.revision == 0
    assert [key for key in _staged(board) if ":play:" in key] == [KEY]


def test_a_ruling_withdraws_its_play_s_card_and_offer_and_its_own_card_stands_in_their_place() -> None:
    board = Board()
    _go_ahead(board)
    tied = drive(away=10, home=14, period=4, clock=250)
    _answer(board, 25, tied, _row(57, 23, kind=FootballEventKind.REVIEW))  # overturned: no score
    assert [offer.card for offer in _view(board, 66.9).offers] == [DedupeKey(KEY)]
    assert _view(board, 68).offers == () and board.queue.card(KEY) is None  # the ruling is out: withdrawn
    [ruling] = _view(board, 70).offers  # the state after it is out too
    assert ruling.revision == 1 and ruling.card == DedupeKey(f"{GAME.id}:play:57:r1")
    card = board.queue.card(ruling.card.raw)
    assert card is not None and card.payload.state == tied
    assert card.payload.caption is not None and card.payload.caption.play == PlayRef("57:review", "57", 1)


def test_a_ruling_that_leaves_nothing_to_offer_leaves_no_offer_standing() -> None:
    board = Board()
    early = drive(away=0, home=0, period=1, clock=600)
    _answer(board, 0, early)
    _answer(board, 10, drive(away=7, home=0, period=1, clock=580), _row(5, 8, major=True))
    _answer(board, 20, early, _row(5, 18, kind=FootballEventKind.REVIEW))
    assert _view(board, 200).offers == ()
    assert [key for key in _staged(board) if ":play:" in key] == []


def test_a_play_whose_ruling_cleared_first_is_never_offered() -> None:
    board = Board()
    _answer(board, 0, LATE)
    _answer(board, 10, AHEAD, _row(57, 8, major=True))  # out at 53, the state after it at 55
    tied = drive(away=10, home=14, period=4, clock=250)
    _answer(board, 11, tied, _row(57, -19, kind=FootballEventKind.REVIEW))  # written at -19: out at 26
    assert _view(board, 55).offers == () and board.queue.card(KEY) is None
    [ruling] = _view(board, 56).offers
    assert ruling.revision == 1


def test_the_final_names_the_play_that_decided_it_and_that_play_s_card_airs_before_it() -> None:
    board = Board()
    tied = drive(away=17, home=17, period=4, clock=120)
    _answer(board, 0, tied)
    over = FootballGameState(period=4, clock_seconds=0, away_score=17, home_score=20, interval=FootballInterval.FINAL)
    _answer(board, 10, over, _row(88, 8, major=True), phase=NflPhase.POST)  # a walk-off field goal, then FINAL
    view = _view(board, 55)
    [offer] = view.offers
    key = f"{GAME.id}:play:88:r0"
    assert offer.card == DedupeKey(key)
    final = board.card(GAME, "final")
    assert final is not None and final.after == (DedupeKey(key),)
    assert final.payload.decided_by == PlayRef("88:play", "88", 0)
    pick = board.queue.peek(board.now, PanelProfile.QUAD_128X64, view=board.queue.publish())
    assert pick is not None and pick.card.dedupe_key == DedupeKey(key)  # it waits for the play that decided it


def test_a_game_that_leaves_the_live_phase_takes_its_plays_cards_and_offers_with_it() -> None:
    board = Board()
    _go_ahead(board)
    assert _view(board, 55).offers and board.queue.card(KEY) is not None
    gone = board.at(56)  # off the slate's live games
    assert gone.policy.offers == () and board.queue.card(KEY) is None


def test_a_game_off_the_slate_after_its_final_takes_its_plays_cards_with_it() -> None:
    board = Board()
    tied = drive(away=17, home=17, period=4, clock=120)
    _answer(board, 0, tied)
    over = FootballGameState(period=4, clock_seconds=0, away_score=17, home_score=20, interval=FootballInterval.FINAL)
    _answer(board, 10, over, _row(88, 8, major=True), phase=NflPhase.POST)
    board.at(55, GAME)
    assert board.queue.card(f"{GAME.id}:play:88:r0") is not None
    board.at(60)
    assert board.queue.card(f"{GAME.id}:play:88:r0") is None and board.card(GAME, "final") is None


def _staged(board: Board) -> list[str]:
    return sorted(board.queue.publish().cards)


class _Drawn(RendererRegistry):
    """Today's renderers, keeping the card each frame drew."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()
        self.card: ScoreboardCard[Any] | None = None

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self._real.render(card, context, canvas)
        self.card = card


def test_a_promotion_on_the_board_draws_its_own_play_however_many_plays_came_while_it_waited() -> None:
    """`AppLoop` under `omni-board-policy/2`: play 52's promotion (at 53) starts the 60 s cooldown, so play 57's waits
    until 113, by when the live card's caption is play 59. Each promoted frame shows its own play."""
    clock = FakeClock(T)
    feeds = FakeNflFeeds()
    queue = InterleavedCardQueue()
    drawn = _Drawn()
    loop = AppLoop(
        schedules=(nfl_slate(GAME),),
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
            feeds=feeds, delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(45)), queue=queue
        ),
        settings=PolicySettings(),
    )
    answers = {
        0: (LATE, _row(50, -30, major=True)),
        5: (drive(away=10, home=14, period=4, clock=290), _row(52, 3, major=True)),  # a turnover: out at 48
        10: (AHEAD, _row(57, 8, major=True)),  # the go-ahead score: out at 53
        20: (drive(away=17, home=14, period=4, clock=260, down=2), _row(58, 18)),
        30: (drive(away=17, home=14, period=4, clock=250, down=3), _row(59, 28)),
    }
    promoted: list[tuple[int, str | None]] = []
    for second in range(0, 200):
        at = clock.read()
        if second in answers:
            state, row = answers[second]
            feeds.answer(GAME, state, at, events=(row,), current_for=timedelta(minutes=10))
        loop.refresh(at)
        painted = loop.paint(at)
        if painted.presented is not None and painted.presented.reason is SelectionReason.TAKEOVER:
            assert drawn.card is not None and isinstance(drawn.card.payload, LiveFootballCardPayload)
            caption = drawn.card.payload.last_play
            promoted.append((second, caption.headline if caption is not None else None))
        clock.advance(timedelta(seconds=1))
    assert promoted == [(53, "play 52"), (113, "play 57")]
