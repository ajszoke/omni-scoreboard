"""The feed's drain after an NFL final: a deciding play it brings once the final is up updates that final once, as its
caption and the play that decided it, or is said to have missed; nothing it brings is offered.

Every test drives `LiveFootballPipeline.refresh` over a fake `NflFeeds` (`tests.test_app_nfl_pipeline`) on a fake
clock, with a 45 s NFL delay. A tied game late in the fourth is called final at 60 (revealed at 105) before its
deciding field goal is in; the drain brings it at 110, written at 108: out at 153, the answer after it at 155.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from omni.app.nfl_pipeline import NflCardPolicy
from omni.cards.base import CardId
from omni.cards.football import LiveFootballCardPayload, PlayRef, StagedCaption
from omni.core.clock import MonotonicTime
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone
from omni.events.football import FootballEvent, FootballEventKind
from omni.providers.nflmeta import FeedNote, FeedNoteKind, NflPhase
from tests.test_app_nfl_pipeline import T, Board, drive, nfl_game

GAME = nfl_game()
S = timedelta(seconds=1)
FINAL_KEY = f"{GAME.id}:final"
TIED = drive(17, 17, period=4, clock=120)
WON = FootballGameState(period=4, clock_seconds=0, away_score=17, home_score=20, interval=FootballInterval.FINAL)


def _kick(board: Board, number: int = 88, *, tone: PlayTone = PlayTone.SCORE) -> FootballEvent:
    return FootballEvent(
        key=f"{number}:play",
        kind=FootballEventKind.PLAY,
        summary=PlaySummary(tone=tone, headline=f"KICK {number}"),
        play=number,
        written_at=board.now.utc - 2 * S,
    )


def _final_up(board: Board) -> None:
    board.answer(GAME, TIED)
    board.at(45, GAME)
    board.to(60)
    board.answer(GAME, WON, phase=NflPhase.POST)  # the scoreboard says final before the field goal is in
    assert board.at(105, GAME).finals == (CardId(FINAL_KEY),)


def _drained(board: Board, *events: FootballEvent, at: float = 110, state: FootballGameState = WON) -> None:
    board.to(at)
    board.answer(GAME, state, phase=NflPhase.POST, events=events)
    board.refresh(GAME)


def test_a_deciding_play_the_drain_brings_after_the_final_updates_it_once_and_is_never_offered() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    kick = _kick(board)
    _drained(board, kick)
    assert board.at(154.9, GAME).ingested == ()  # out at 153; the answer after it not until 155
    updated = board.at(155, GAME)
    assert updated.ingested == (CardId(FINAL_KEY),) and updated.corrections == () and updated.policy.offers == ()
    final = board.card(GAME, "final")
    assert final is not None and final.timing.available_at == T + 105 * S  # updated in place: its window runs on
    assert final.payload == LiveFootballCardPayload(
        state=WON,
        last_play=kick.summary,
        caption=StagedCaption(
            play=PlayRef("88:play", "88", 0), latest_start=MonotonicTime(273), hard_end=MonotonicTime(303)
        ),
        decided_by=PlayRef("88:play", "88", 0),
    )


def test_a_second_deciding_play_is_said_to_have_missed_the_final_already_names_one() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    _drained(board, _kick(board))
    board.at(155, GAME)
    board.to(160)
    _drained(board, _kick(board, 89), at=160)
    missed = board.at(205, GAME)
    assert missed.skipped == (f"{GAME.id}: major play #89 unseen (its final already names the play that decided it)",)
    payload = board.payload(GAME, "final")
    assert payload.decided_by == PlayRef("88:play", "88", 0)


def test_a_deciding_play_the_drain_brings_after_the_final_s_window_is_said_to_have_missed_it() -> None:
    board = Board(policy=NflCardPolicy(postgame_window=timedelta(seconds=40)))
    _final_up(board)
    board.queue.prune(board.to(145))  # the final's window is over
    board.to(110 + 40)
    _drained(board, _kick(board), at=150)
    missed = board.at(197, GAME)
    assert missed.skipped == (f"{GAME.id}: major play #88 unseen (its final's window is over)",)


def test_a_routine_play_the_drain_brings_changes_nothing_and_its_notes_are_ours() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    kneel = _kick(board, 87, tone=PlayTone.STANDARD)
    board.answer(GAME, WON, phase=NflPhase.POST, events=(kneel,), notes=(FeedNote(FeedNoteKind.PLAY_MISSING, "x"),))
    board.answer(GAME, TIED)  # a live answer after the final: nothing
    drained = board.refresh(GAME)
    assert drained.warnings == (f"{GAME.id}: matched play missing: x",)
    assert board.at(155, GAME).ingested == () and board.payload(GAME, "final").last_play is None


def test_the_final_s_caption_goes_at_its_latest_start_unless_a_confirmed_frame_carried_it() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    _drained(board, _kick(board))
    board.at(155, GAME)
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(273)
    dropped = board.at(273, GAME)
    assert dropped.skipped == (f"{GAME.id}: major play #88 unseen (too late to start)",)
    payload = board.payload(GAME, "final")
    assert payload.last_play is None and payload.caption is None
    assert payload.decided_by == PlayRef("88:play", "88", 0)  # it still names the play that decided it


def test_the_final_s_caption_a_confirmed_frame_carried_stays_until_its_hard_end() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    _drained(board, _kick(board))
    board.at(155, GAME)
    board.to(160)
    board.show(GAME, "final")
    board.refresh(GAME)
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(303)
    assert board.at(302.9, GAME).skipped == () and board.payload(GAME, "final").last_play is not None
    assert board.at(303, GAME).skipped == () and board.payload(GAME, "final").last_play is None


def test_a_frame_shown_only_at_the_final_s_caption_s_latest_start_starts_nothing() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    _drained(board, _kick(board))
    board.at(155, GAME)
    board.to(273)  # drawn before its latest start, shown at it
    board.show(GAME, "final")
    assert board.refresh(GAME).skipped == (f"{GAME.id}: major play #88 unseen (too late to start)",)
    assert board.payload(GAME, "final").last_play is None


def test_a_frame_without_the_final_s_caption_or_with_another_entry_or_another_game_s_starts_nothing() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    _drained(board, _kick(board))
    board.at(155, GAME)
    card = board.card(GAME, "final")
    assert card is not None and isinstance(card.payload, LiveFootballCardPayload)
    staged = card.payload.caption
    assert staged is not None
    alone = replace(card, payload=replace(card.payload, last_play=None))
    another = replace(card, payload=replace(card.payload, caption=replace(staged, play=PlayRef("q", "q", 0))))
    elsewhere = replace(card, contest=nfl_game("402", "MIN", "GB"))
    board.to(160)
    for shown in (alone, another, elsewhere):
        board.pipeline.presented(shown, board.now)
    board.refresh(GAME)
    assert board.at(273, GAME).skipped == (f"{GAME.id}: major play #88 unseen (too late to start)",)


def test_a_correction_that_changes_the_final_s_score_takes_its_caption_down() -> None:
    board = Board()
    _final_up(board)
    board.to(110)
    _drained(board, _kick(board))
    board.at(155, GAME)
    corrected = FootballGameState(
        period=4, clock_seconds=0, away_score=17, home_score=23, interval=FootballInterval.FINAL
    )
    _drained(board, at=170, state=corrected)
    fixed = board.at(215, GAME)
    assert fixed.corrections == (CardId(FINAL_KEY),) and fixed.skipped == ()
    payload = board.payload(GAME, "final")
    assert payload.state == corrected and payload.last_play is None


def test_a_final_whose_window_ends_with_its_caption_up_lets_the_caption_go() -> None:
    board = Board(policy=NflCardPolicy(postgame_window=timedelta(seconds=100)))
    _final_up(board)
    board.to(110)
    _drained(board, _kick(board))
    board.at(155, GAME)
    board.queue.prune(board.to(205))  # the final's window is over, its caption's time not
    assert board.at(205, GAME).skipped == () and board.pipeline.next_deadline(board.now) is None
