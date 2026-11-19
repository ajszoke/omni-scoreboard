"""What the NFL pipeline tells the board's policy: `LiveFootballPipeline.refresh`'s ``policy``, from released state only.

Every test drives `LiveFootballPipeline.refresh` over a fake `NflFeeds` (`tests.test_app_nfl_pipeline`) on a fake
clock, refreshing as each answer arrives, with a 45 s NFL delay unless it says otherwise. Plays and states are
synthetic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from omni.cards.base import DedupeKey
from omni.core.clock import ClockReading, MonotonicTime
from omni.domain.breaks import BreakEvidence, BreakKind
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone
from omni.events.football import FootballEvent, FootballEventKind
from omni.policy.interrupts import InterruptClass, Stakes, interrupt_value
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


def _answer(board: Board, seconds: float, state: FootballGameState, *events: FootballEvent, **kwargs: Any) -> None:
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
