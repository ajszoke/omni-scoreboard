"""What the NFL pipeline tells the board's policy: `LiveFootballPipeline.refresh`'s ``policy``, from released state only.

Every test drives `LiveFootballPipeline.refresh` over a fake `NflFeeds` (`tests.test_app_nfl_pipeline`) on a fake
clock, refreshing as each answer arrives, with a 45 s NFL delay unless it says otherwise. Plays and states are
synthetic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omni.app.pipeline import MeterReading
from omni.cards.base import DedupeKey
from omni.core.clock import ClockReading, MonotonicTime
from omni.domain.baseball import WinProbability
from omni.domain.breaks import BreakEvidence, BreakKind
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone
from omni.events.football import FootballEvent, FootballEventKind
from omni.policy.interrupts import InterruptClass, LeverageBasis, Stakes, interrupt_value
from omni.policy.leverage import football_proxy
from omni.policy.promotion import PromotionCandidate, PromotionKey, PromotionKind
from omni.policy.view import GameFacts, PolicyView
from omni.providers.nflmeta import NflPhase
from tests.test_app_nfl_pipeline import T, Board, drive, nfl_game

GAME = nfl_game()


def _row(
    number: int, written: float, *, kind: FootballEventKind = FootballEventKind.PLAY, major: bool = False
) -> FootballEvent:
    """A play-feed entry about row ``number``, written ``written`` seconds past ``T``: a snap unless ``kind`` says."""
    tone = PlayTone.SCORE if major else PlayTone.STANDARD
    return FootballEvent(
        key=f"{number}:{kind.value}",
        kind=kind,
        summary=PlaySummary(tone=tone, headline=f"{kind.value} {number}"),
        play=number,
        written_at=T + timedelta(seconds=written),
    )


def _arrived(seconds: float) -> ClockReading:
    return ClockReading(T + timedelta(seconds=seconds), MonotonicTime(float(seconds)))


def _answer(
    board: Board, seconds: float, state: FootballGameState | None, *events: FootballEvent, **kwargs: Any
) -> None:
    """An answer arriving ``seconds`` past ``T``, taken by a refresh then."""
    board.to(seconds)
    board.answer(GAME, state, events=events, **kwargs)
    board.refresh(GAME)


def _view(board: Board, seconds: float) -> PolicyView:
    return board.at(seconds, GAME).policy


def _facts(board: Board, seconds: float) -> GameFacts:
    facts = _view(board, seconds).facts(GAME.id)
    assert facts is not None
    return facts


def _breaks(board: Board, seconds: float) -> tuple[BreakEvidence, ...]:
    return _facts(board, seconds).breaks


def _action(board: Board, seconds: float) -> str | None:
    action = _facts(board, seconds).action
    return action.marker if action is not None else None


# --- the facts --------------------------------------------------------------------------------------------------------


def test_a_game_s_facts_come_only_from_its_released_state_and_its_leverage_moves_with_the_released_score() -> None:
    board = Board()
    opening = drive(period=2, clock=300)
    _answer(board, 0, opening)
    assert _view(board, 44.9) == PolicyView()  # still inside the delay
    assert _facts(board, 45) == GameFacts(game=GAME, leverage=football_proxy(opening), fresh=True)  # no records yet
    _answer(board, 50, drive(away=21, period=2, clock=280))
    assert _facts(board, 94).leverage == football_proxy(opening)
    blowout = _facts(board, 95).leverage
    assert blowout == football_proxy(drive(away=21, period=2, clock=280)) and blowout.g < football_proxy(opening).g


def test_a_stale_card_s_facts_say_so() -> None:
    board = Board(lag=0)
    _answer(board, 0, drive(), current_for=timedelta(seconds=20))
    assert _facts(board, 19).fresh and not _facts(board, 20).fresh


def test_a_stoppage_row_is_remembered_across_looks_until_a_later_snap_supersedes_it() -> None:
    board = Board()
    _answer(board, 0, drive(clock=500), _row(50, -2))
    _answer(board, 10, drive(clock=480), _row(57, 9, kind=FootballEventKind.TIMEOUT))  # a club's timeout, once
    _answer(board, 20, drive(clock=480))  # a quiet look
    _answer(board, 25, drive(clock=480), _row(56, 5))  # a late row of a snap from before the timeout
    _answer(board, 30, drive(clock=470), _row(58, 29))  # play resumes
    assert _breaks(board, 45) == () and _action(board, 45) == "play 50"
    assert _breaks(board, 54.9) == ()  # the timeout has reached the board, not the viewer
    [timeout] = _breaks(board, 55)
    assert timeout.id.kind is BreakKind.NFL_TEAM_TIMEOUT and timeout.id.marker == "play 57"
    assert timeout.received == _arrived(10) and timeout.onset == T + timedelta(seconds=9)
    assert _breaks(board, 65) == (timeout,)  # remembered through a quiet look
    assert _breaks(board, 70) == (timeout,) and _action(board, 70) == "play 56"  # older than the timeout: it stands
    assert _breaks(board, 75) == () and _action(board, 75) == "play 58"


def test_a_snap_s_late_row_never_rolls_the_newest_action_back() -> None:
    board = Board(lag=0)
    _answer(board, 0, drive(), _row(60, -1))
    _answer(board, 5, drive(), _row(59, -10))
    assert _action(board, 5) == "play 60"


def test_a_quarter_break_whose_onset_is_unknown_stands_while_shown_and_goes_at_the_next_snap() -> None:
    board = Board(lag=0)
    _answer(board, 0, drive(clock=20), _row(10, -1))
    _answer(board, 10, drive(interval=FootballInterval.END_OF_QUARTER))
    [quarter] = _breaks(board, 10)
    assert quarter.id.kind is BreakKind.NFL_QUARTER_BREAK and quarter.onset is None
    _answer(board, 20, drive(interval=FootballInterval.END_OF_QUARTER), _row(11, 5))  # the quarter's last row, late
    assert _breaks(board, 20) == (quarter,) and _action(board, 20) == "play 11"
    _answer(board, 30, drive(period=2, clock=900))  # the scoreboard has turned; no snap yet
    assert _breaks(board, 30) == (quarter,)
    _answer(board, 40, drive(period=2, clock=880), _row(12, 38))
    assert _breaks(board, 40) == ()


# --- offers -----------------------------------------------------------------------------------------------------------


def test_a_scoring_play_offers_its_own_card_once_the_state_after_it_clears_and_a_ruling_offers_its_next_revision() -> (
    None
):
    board = Board()
    late = drive(away=10, home=14, period=4, clock=300)
    _answer(board, 0, late, _row(50, -30, major=True))  # the first look: its plays are from before the board tuned in
    ahead = drive(away=17, home=14, period=4, clock=270)
    _answer(board, 10, ahead, _row(57, 8, major=True))  # a go-ahead score, 2 s old when the answer stood
    kicked = drive(away=18, home=14, period=4, clock=270)
    _answer(board, 15, kicked, _row(57, 13, kind=FootballEventKind.CONVERSION, major=True))  # its extra point
    _answer(board, 20, drive(away=10, home=14, period=4, clock=260), _row(57, 18, kind=FootballEventKind.REVIEW))
    _answer(board, 30, drive(away=10, home=14, period=4, clock=250), _row(58, 28))  # a routine snap
    assert _view(board, 53).offers == () and board.card(GAME) is not None  # the play is out; the state after isn't
    score = PromotionCandidate(
        key=PromotionKey(GAME.id, "57"),
        kind=PromotionKind.MAJOR,
        eligible=MonotonicTime(53.0),  # the play's release
        card=DedupeKey(f"{GAME.id}:play:57:r0"),  # the play's own card
        value=interrupt_value(InterruptClass.MAJOR, predecessor=football_proxy(late), stakes=Stakes()),
    )
    assert _view(board, 55).offers == (score,)
    assert _view(board, 60).offers == (score,)  # the try merges into its touchdown
    assert _view(board, 62.9).offers == (score,)
    assert _view(board, 63).offers == ()  # the ruling is out: what stood for the play is withdrawn
    [ruling] = _view(board, 65).offers  # overturned on review: the same moment's next revision, the state after it out
    assert ruling.key == score.key and ruling.revision == 1 and ruling.eligible == MonotonicTime(63.0)
    assert ruling.card == DedupeKey(f"{GAME.id}:play:57:r1")
    assert ruling.value == interrupt_value(
        InterruptClass.DECISIVE_LATE_WINNER, predecessor=football_proxy(kicked), stakes=Stakes()
    )
    assert _view(board, 75).offers == (ruling,)  # the routine snap offers nothing
    assert _view(board, 182.9).offers == (ruling,) and _view(board, 183).offers == ()  # gone with every promotion


def test_what_an_outage_s_catch_up_brings_is_never_offered() -> None:
    board = Board(lag=0)
    _answer(board, 0, drive(away=10, home=14, period=4, clock=300), current_for=timedelta(seconds=5))
    board.at(10, GAME)  # stale from 5 s
    _answer(board, 20, drive(away=17, home=14, period=4, clock=270), _row(57, 15, major=True))
    assert _view(board, 20).offers == ()


def test_a_game_whose_final_has_revealed_is_no_longer_carded_live() -> None:
    board = Board(lag=0)
    _answer(board, 0, drive())
    final = drive(away=24, home=17, interval=FootballInterval.FINAL)
    _answer(board, 10, final, phase=NflPhase.POST)
    assert _view(board, 10) == PolicyView()


# --- the swing --------------------------------------------------------------------------------------------------------


def _quote(board: Board, seconds: float, away: float) -> None:
    """The market's reading of the game, the away side at ``away`` points, arriving ``seconds`` past ``T``."""
    board.to(seconds)
    board.feeds.market.append(MeterReading(WinProbability(home=100.0 - away, away=away), board.now))


SECOND = drive(period=2, clock=560)  # a tied second quarter
DOWNFIELD = drive(period=2, clock=520, to_go=15)


def _pick_then_long_completion(board: Board, *, market: bool = True) -> None:
    """A second-quarter pick (play 31, its state after it released at 85), with no reading from before the answer
    before it; then a long completion (play 32, released at 103, the state after it at 105), the market reading 30
    before it and 42 after it."""
    if market:
        _quote(board, 10, 50.0)
    _answer(board, 20, drive(period=2, clock=600), _row(30, 15))  # the first look
    if market:
        _quote(board, 24, 30.0)  # the market moves on the pick before the feed reports it
    _answer(board, 40, SECOND, _row(31, 38, major=True))  # read from 5 (the first look's arrival, less the lag) to 25
    if market:
        _quote(board, 44, 42.0)
    _answer(board, 60, DOWNFIELD, _row(32, 58))  # from 25 to 45


def test_an_early_play_that_moved_the_market_ten_points_is_offered_as_a_win_swing_and_only_in_a_break() -> None:
    board = Board()
    _pick_then_long_completion(board)
    assert _view(board, 85).offers == ()  # the pick: no reading by 5, so no swing, and early it's routine
    assert _view(board, 104.9).offers == ()  # the completion is out; the state after it isn't
    [swing] = _view(board, 105).offers
    assert swing == PromotionCandidate(
        key=PromotionKey(GAME.id, "32"),
        kind=PromotionKind.MAJOR,
        eligible=MonotonicTime(103.0),
        card=DedupeKey(f"{GAME.id}:play:32:r0"),
        value=interrupt_value(InterruptClass.WIN_SWING, predecessor=football_proxy(SECOND), stakes=Stakes()),
    )
    worth = swing.value
    assert worth is not None and worth.event_class is InterruptClass.WIN_SWING
    assert worth.z_pre == 1.0 and worth.value == pytest.approx(1.2 * 1.0 * 0.65)  # B, z_pre = 1, unknown records
    assert worth.basis is LeverageBasis.PROXY and not worth.action_evidence  # the proxy's: break-only
    card = board.queue.card(f"{GAME.id}:play:32:r0")
    assert card is not None and card.payload.state == DOWNFIELD and card.payload.last_play == _row(32, 58).summary


def test_without_a_market_the_same_plays_are_routine() -> None:
    board = Board()
    _pick_then_long_completion(board, market=False)
    assert _view(board, 105).offers == ()


def test_a_play_with_no_answer_before_it_takes_no_swing() -> None:
    board = Board(lag=0)
    _quote(board, 0, 50.0)
    _answer(board, 20, None)  # the first look, before kickoff
    _quote(board, 30, 70.0)
    _answer(board, 60, SECOND, _row(1, 58))  # no state before it: no swing, and no class but routine
    assert _view(board, 60).offers == ()


def test_one_answer_s_swing_is_put_down_to_its_first_score_turnover_or_ruling_else_its_last_play() -> None:
    board = Board()
    _quote(board, 0, 50.0)
    _answer(board, 20, drive(period=2, clock=600))  # the first look
    _quote(board, 24, 35.0)
    turnover = _row(32, 35, major=True)  # a second-quarter turnover: a major event, but early
    snaps = (_row(31, 32), turnover, _row(33, 38, kind=FootballEventKind.TIMEOUT))
    _answer(board, 40, SECOND, *snaps)  # the market from 50 at 5 to 35 at 25
    _quote(board, 50, 20.0)
    runs = (_row(34, 60), _row(35, 66), _row(36, 68, kind=FootballEventKind.OFFICIAL_TIMEOUT))
    _answer(board, 70, DOWNFIELD, *runs)  # from 35 at 25 to 20 at 55
    assert [offer.key.play for offer in _view(board, 85).offers] == ["32"]
    offers = _view(board, 115).offers
    assert [offer.key.play for offer in offers] == ["32", "35"]  # no major among the runs: the last of them
    assert all(offer.value is not None and offer.value.event_class is InterruptClass.WIN_SWING for offer in offers)


def test_a_swing_in_an_outage_s_catch_up_is_never_offered() -> None:
    board = Board(lag=0)
    _quote(board, 0, 50.0)
    _answer(board, 20, drive(period=2, clock=600), current_for=timedelta(seconds=5))
    board.at(30, GAME)  # stale from 25
    _quote(board, 35, 80.0)
    _answer(board, 60, SECOND, _row(57, 55, major=True))  # 30 points across it, but history
    assert _view(board, 60).offers == () and _view(board, 70).offers == ()
