"""What the MLB pipeline tells the board's policy: `LiveBaseballPipeline.refresh`'s ``policy``, from released state only.

Every test drives `LiveBaseballPipeline.refresh` over a fake game feed whose answers arrive when the test says: a read
hands the newest answer to have arrived by then, again until a newer one does, as the poll plan does. Each test
refreshes as each answer arrives, on a 30 s MLB delay unless it says otherwise. States, plays and break signals are
synthetic.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest

from omni.app.pipeline import LiveBaseballPipeline, PipelineResult
from omni.cards.base import DedupeKey
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballAction,
    BaseballBaseState,
    BaseballBreakSignals,
    BaseballCount,
    BaseballFinishedPlay,
    BaseballGameState,
    BaseballReview,
    BaseballScoringImpact,
    InningPhase,
    PlayLogCoverage,
    PregameDetails,
    PregameSide,
    WinLossRecord,
)
from omni.domain.breaks import BreakEvidence
from omni.domain.contest import TeamGame
from omni.events.base import EventImportance
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload, LiveBaseballFeed
from omni.events.mlb_breaks import mlb_break_evidence
from omni.policy.interrupts import InterruptClass, Stakes, interrupt_value
from omni.policy.leverage import baseball_proxy
from omni.policy.pool import MatchupRecords, TeamRecord
from omni.policy.promotion import CandidateOrigin, PromotionCandidate, PromotionKey, PromotionKind
from omni.policy.view import GameFacts, PolicyView
from omni.providers.base import ProviderError
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.delay_policy import DelayPolicy
from omni.queue.priority import PriorityScorer
from omni.queue.scheduler import InterleavedCardQueue

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
LAG = 30


def _seconds(seconds: float) -> ClockReading:
    """``seconds`` past ``T``, read both ways, the monotonic clock running alongside UTC."""
    return ClockReading(T + timedelta(seconds=seconds), MonotonicTime(float(seconds)))


def _id(raw: str) -> LeagueScopedId:
    return LeagueScopedId(League.MLB, SOURCE, raw)


def _game(status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=_id("g1"),
        league=League.MLB,
        status=status,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


GAME = _game()
LIVE_KEY = "mlb:mlb_statsapi:g1:live"
NO_HITTER_KEY = "mlb:mlb_statsapi:g1:nohitter"
FINAL_KEY = "mlb:mlb_statsapi:g1:final"


def _state(
    away: int = 0,
    home: int = 0,
    *,
    inning: int = 5,
    phase: InningPhase = InningPhase.TOP,
    away_hits: int = 3,
    home_hits: int = 3,
    away_reached_base: bool | None = None,
    home_reached_base: bool | None = None,
    play_log: PlayLogCoverage = PlayLogCoverage.COMPLETE,
    signals: BaseballBreakSignals = BaseballBreakSignals(),
) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=inning,
        phase=phase,
        count=BaseballCount(balls=0, strikes=0, outs=1),
        bases=BaseballBaseState(),
        away_hits=away_hits,
        home_hits=home_hits,
        away_reached_base=away_reached_base,
        home_reached_base=home_reached_base,
        play_log=play_log,
        break_signals=signals,
    )


def _pitch(at_bat: int, pitch: int | None, at: float) -> BaseballBreakSignals:
    """The newest action: a pitch of a plate appearance (or, with no pitch, its end) at ``at`` seconds past ``T``."""
    return BaseballBreakSignals(last_action=BaseballAction(at_bat=at_bat, pitch=pitch, at=T + timedelta(seconds=at)))


def _play(
    eid: str,
    *,
    away: int,
    home: int,
    at: float,
    event_type: BaseballGameEventType = BaseballGameEventType.HOME_RUN,
    impact: BaseballScoringImpact | None = None,
) -> BaseballGameEvent:
    """A play in the bottom of the 7th, ``at`` seconds past ``T``, leaving the score at ``away``-``home``."""
    return BaseballGameEvent(
        id=_id(eid),
        contest=GAME,
        event_type=event_type,
        source=SOURCE,
        source_time=T + timedelta(seconds=at),
        observed_at=T + timedelta(seconds=at),
        importance=EventImportance(
            priority=DisplayPriority.ALERT, urgency=UpdateUrgency.HIGH, leverage=0.0, rarity=0.5, favorite_relevance=0.0
        ),
        payload=BaseballPlayPayload(
            inning=7,
            phase=InningPhase.BOTTOM,
            description="a play",
            rbi=impact.rbi if impact is not None else 0,
            away_score=away,
            home_score=home,
            scoring_impact=impact,
        ),
    )


class _Feed:
    """The game's answers as they arrive: a read hands the newest to have arrived by then."""

    def __init__(self) -> None:
        self.answers: list[LiveBaseballFeed] = []
        self.failing = False

    def answer(
        self,
        state: BaseballGameState,
        at: float,
        *,
        events: tuple[BaseballGameEvent, ...] = (),
        status: GameStatus | None = None,
    ) -> LiveBaseballFeed:
        """An answer about the game that arrived ``at`` seconds past ``T``, standing as of then."""
        feed = LiveBaseballFeed(
            state=state, events=events, status=status, as_of=T + timedelta(seconds=at), received=_seconds(at)
        )
        self.answers.append(feed)
        return feed

    def __call__(self, game: TeamGame, now: datetime) -> LiveBaseballFeed:
        if self.failing:
            raise ProviderError("game feed down")
        return [answer for answer in self.answers if answer.received is not None and answer.received.utc <= now][-1]


class _Board:
    """The pipeline, its queue and its feed; each refresh returns what the policy reads."""

    def __init__(self, *, lag: int = LAG) -> None:
        self.queue = InterleavedCardQueue()
        self.pipeline = LiveBaseballPipeline(
            scorer=PriorityScorer(),
            delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(lag)),
            factory=CardFactory(),
            queue=self.queue,
        )
        self.feed = _Feed()
        self.details: PregameDetails | None = None

    def result(self, seconds: float, game: TeamGame = GAME) -> PipelineResult:
        return self.pipeline.refresh(
            [game], at=_seconds(seconds), fetch_feed=self.feed, fetch_pregame_details=lambda _: self.details
        )

    def at(self, seconds: float, game: TeamGame = GAME) -> PolicyView:
        return self.result(seconds, game).policy

    def facts(self, seconds: float, game: TeamGame = GAME) -> GameFacts | None:
        return self.at(seconds, game).facts(GAME.id)

    def majors(self, seconds: float, game: TeamGame = GAME) -> tuple[PromotionCandidate[DedupeKey], ...]:
        return tuple(offer for offer in self.at(seconds, game).offers if offer.kind is PromotionKind.MAJOR)


def _marker(facts: GameFacts | None) -> str | None:
    assert facts is not None
    return facts.action.marker if facts.action is not None else None


def _breaks(board: _Board, seconds: float) -> tuple[BreakEvidence, ...]:
    facts = board.facts(seconds)
    assert facts is not None
    return facts.breaks


# --- the facts --------------------------------------------------------------------------------------------------------


def test_a_game_s_facts_come_only_from_its_released_state_and_its_leverage_moves_with_the_released_score() -> None:
    board = _Board()
    tied = _state()
    board.feed.answer(tied, 0)
    assert board.at(0) == PolicyView() and board.at(29) == PolicyView()  # held: nothing to read yet
    released = board.facts(30)
    assert released == GameFacts(game=GAME, leverage=baseball_proxy(tied), fresh=True)  # records unknown: live at start
    board.feed.answer(_state(away=3), 40)  # a three-run lead, still inside the delay
    board.at(40)
    held = board.facts(69)
    assert held is not None and held.leverage == baseball_proxy(tied)
    moved = board.facts(70)
    assert moved is not None and moved.leverage == baseball_proxy(_state(away=3))
    assert moved.leverage.g < released.leverage.g  # a three-run game matters less than a tie


def test_a_stale_card_s_facts_say_so() -> None:
    board = _Board(lag=0)
    board.feed.answer(_state(), 0)
    board.at(0)
    board.feed.failing = True
    stale = board.facts(1)
    assert stale is not None and not stale.fresh
    assert board.facts(121) is None  # its card came down: nothing carded live to describe


def test_a_break_appears_only_once_released_and_later_released_action_supersedes_it() -> None:
    board = _Board()
    board.feed.answer(_state(signals=_pitch(40, 2, at=-1)), 0)

    def half_over(last: BaseballAction) -> BaseballGameState:
        ended = BaseballFinishedPlay(at_bat=40, inning=5, phase=InningPhase.TOP, ended=T + timedelta(seconds=8))
        signals = BaseballBreakSignals(
            inning_break_length=timedelta(seconds=120), last_finished=ended, last_action=last
        )
        return _state(phase=InningPhase.MIDDLE, signals=signals)

    inning_break = board.feed.answer(half_over(BaseballAction(at_bat=40, pitch=3, at=T + timedelta(seconds=7))), 10)
    board.feed.answer(half_over(BaseballAction(at_bat=40, at=T + timedelta(seconds=8))), 15)  # the out, logged late
    board.feed.answer(_state(phase=InningPhase.BOTTOM, signals=_pitch(41, 1, at=19)), 20)  # the next half's first pitch
    for arrival in (0, 10, 15, 20):
        board.at(arrival)
    first = board.facts(30)
    assert first is not None and first.breaks == () and _marker(first) == "at-bat 40 pitch 2"
    still_inside = board.facts(39)
    assert still_inside is not None and still_inside.breaks == ()  # the break has reached the board, not the viewer
    shown = board.facts(40)
    assert shown is not None and shown.breaks == mlb_break_evidence(inning_break, GAME, _seconds(10))
    assert len(shown.breaks) == 1 and _marker(shown) == "at-bat 40 pitch 3"
    standing = board.facts(45)  # newer action, but the answer that brings it still shows the break
    assert standing is not None and standing.breaks == shown.breaks and _marker(standing) == "at-bat 40 end"
    over = board.facts(50)
    assert over is not None and over.breaks == () and _marker(over) == "at-bat 41 pitch 1"


def test_an_ended_break_is_published_once_when_its_end_is_released_then_dropped_for_good() -> None:
    board = _Board()

    def reviewing(in_progress: bool) -> BaseballGameState:
        review = BaseballReview(at_bat=41, in_progress=in_progress)
        return _state(signals=BaseballBreakSignals(last_action=BaseballAction(at_bat=41), reviews=(review,)))

    board.feed.answer(reviewing(True), 0)
    board.feed.answer(reviewing(True), 5)  # the same review read again
    board.feed.answer(reviewing(False), 10)  # ruled
    board.feed.answer(reviewing(False), 20)  # the ruling, read again
    for arrival in (0, 5, 10, 20):
        board.at(arrival)
    [open_] = _breaks(board, 35)
    assert not open_.ended and open_.received == _seconds(0)  # the first copy stands: a re-read renews nothing
    [ruled] = _breaks(board, 40)
    assert ruled.id == open_.id and ruled.ended
    assert _breaks(board, 45) == ()
    assert _breaks(board, 50) == ()  # read again, it isn't published again


def test_records_are_the_pregame_details_as_they_stood_at_the_start_and_unknown_for_a_game_already_live() -> None:
    def details(away: tuple[int, int], home: tuple[int, int]) -> PregameDetails:
        return PregameDetails(
            away=PregameSide(record=WinLossRecord(wins=away[0], losses=away[1])),
            home=PregameSide(record=WinLossRecord(wins=home[0], losses=home[1])),
        )

    board = _Board(lag=0)
    board.details = details((10, 5), (5, 10))
    assert board.at(0, _game(GameStatus.PREGAME)) == PolicyView()  # upcoming: nothing carded live
    board.details = details((11, 5), (5, 11))
    board.at(5, _game(GameStatus.PREGAME))
    board.feed.answer(_state(), 10)
    started = MatchupRecords(away=TeamRecord(wins=11, losses=5), home=TeamRecord(wins=5, losses=11))
    live = board.facts(10)
    assert live is not None and live.records == started
    board.details = details((12, 5), (5, 12))  # today's result, as the schedule now has it
    board.at(20, _game(GameStatus.PREGAME))  # the schedule's status flaps back for a pass
    board.feed.answer(_state(), 30)
    again = board.facts(30)
    assert again is not None and again.records == started  # frozen at the start
    board.details = None
    board.at(40, _game(GameStatus.PREGAME))  # details that fail to answer keep what was known
    board.feed.answer(_state(), 50)
    later = board.facts(50)
    assert later is not None and later.records == started
    joined_late = _Board(lag=0)
    joined_late.details = details((12, 5), (5, 12))
    joined_late.feed.answer(_state(), 0)
    facts = joined_late.facts(0)
    assert facts is not None and facts.records is None


def test_a_game_that_leaves_the_slate_is_forgotten() -> None:
    board = _Board(lag=0)
    board.feed.answer(_state(), 0)
    assert board.facts(0) is not None
    board.pipeline.refresh([], at=_seconds(10), fetch_feed=board.feed)
    board.feed.answer(_state(), 20)
    back = board.at(20)
    assert back.facts(GAME.id) is not None and back.offers == ()


# --- offers -----------------------------------------------------------------------------------------------------------


def test_a_scoring_big_play_offers_its_card_once_the_state_after_it_has_cleared_the_delay() -> None:
    board = _Board()
    tied = _state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM)
    before = _state(away=1, inning=7, phase=InningPhase.BOTTOM)
    earlier = _play("e0", away=1, home=0, at=-60, impact=BaseballScoringImpact(rbi=1, go_ahead=True))
    board.feed.answer(before, 0, events=(earlier,))  # the first look: its plays are from before the board tuned in
    homer = _play("e1", away=1, home=1, at=8, impact=BaseballScoringImpact(rbi=1, tying=True))
    board.feed.answer(tied, 10, events=(earlier, homer))  # the answer stood at 10: the play was 2 s old
    board.at(0)
    board.at(10)
    card = DedupeKey("mlb:mlb_statsapi:g1:bigplay:e1")
    assert board.at(38).offers == () and board.queue.card(card.raw) is not None  # the play is up; its state isn't
    assert board.at(39).offers == ()
    expected = PromotionCandidate(
        key=PromotionKey(GAME.id, "e1"),
        kind=PromotionKind.MAJOR,
        eligible=MonotonicTime(38.0),  # the card's release
        card=card,
        value=interrupt_value(InterruptClass.MAJOR, predecessor=baseball_proxy(before), stakes=Stakes()),
    )
    assert board.at(40).offers == (expected,)
    assert board.at(157.9).offers == (expected,)  # it stands until every promotion is gone
    assert board.at(158).offers == ()
    assert not any("e0" in offer.key.play for offer in board.at(160).offers)  # never the first look's


HOMER_CARD = "mlb:mlb_statsapi:g1:bigplay:e1"


def _offered_homer(board: _Board) -> tuple[BaseballGameEvent, PromotionCandidate[DedupeKey]]:
    """A tying home run in the bottom of the 7th, played at 8 s and reported at 10 s: its card is up from 38 s and
    its offer stands from 40 s, once the state after it has cleared the delay."""
    board.feed.answer(_state(away=1, inning=7, phase=InningPhase.BOTTOM), 0)
    homer = _play("e1", away=1, home=1, at=8, impact=BaseballScoringImpact(rbi=1, tying=True))
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 10, events=(homer,))
    board.at(0)
    board.at(10)
    [offer] = board.majors(40)
    return homer, offer


@pytest.mark.parametrize(
    "corrected",
    [
        pytest.param(lambda homer: (), id="the at-bat no longer a mapped play"),
        pytest.param(
            lambda homer: (
                dataclasses.replace(
                    homer,
                    event_type=BaseballGameEventType.DOUBLE,
                    importance=dataclasses.replace(homer.importance, priority=DisplayPriority.NORMAL),
                ),
            ),
            id="the at-bat now a double",
        ),
    ],
)
def test_a_big_play_a_correction_takes_away_is_withdrawn_at_once_and_its_card_comes_down_once_that_airs(
    corrected: Callable[[BaseballGameEvent], tuple[BaseballGameEvent, ...]],
) -> None:
    board = _Board()
    homer, _ = _offered_homer(board)
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 50, events=corrected(homer))
    assert board.majors(50) == ()  # the board knows the offer is stale, so it's no longer published
    assert board.queue.card(HOMER_CARD) is not None  # the card is as the broadcast still has it
    board.at(79)
    assert board.queue.card(HOMER_CARD) is not None
    assert board.majors(80) == () and board.queue.card(HOMER_CARD) is None  # the correction has aired
    assert board.majors(100) == ()


def test_a_big_play_a_correction_changes_is_offered_again_under_its_next_revision_with_its_card_revised() -> None:
    board = _Board()
    homer, offer = _offered_homer(board)
    rescored = dataclasses.replace(homer, payload=dataclasses.replace(homer.payload, description="a play, reviewed"))
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 50, events=(rescored,))
    assert board.majors(50) == ()  # the standing offer names content the board knows is stale
    card = board.queue.card(HOMER_CARD)
    assert card is not None and card.payload.description == "a play"  # the broadcast hasn't shown the change
    assert board.majors(79) == ()
    [revised] = board.majors(80)
    assert revised == dataclasses.replace(offer, eligible=MonotonicTime(80.0), revision=1)
    card = board.queue.card(HOMER_CARD)
    assert card is not None and card.payload.description == "a play, reviewed"
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 60, events=(rescored,))
    assert board.majors(90) == (revised,)  # the same version again revises nothing


def _reviewed(play: BaseballGameEvent, *, unscored: bool = False) -> BaseballGameEvent:
    """``play`` as a later answer has it: its description revised and, if ``unscored``, its run batted in taken away."""
    payload = dataclasses.replace(play.payload, description="a play, reviewed")
    if unscored:
        payload = dataclasses.replace(payload, rbi=0, scoring_impact=BaseballScoringImpact(rbi=0))
    return dataclasses.replace(play, payload=payload)


@pytest.mark.parametrize(
    ("play_log", "warnings"),
    [
        pytest.param(PlayLogCoverage.UNVERIFIED, (), id="a log that doesn't cover the game"),
        pytest.param(PlayLogCoverage.COMPLETE, ("play 12: AttributeError: a garbled entry",), id="a play it dropped"),
    ],
)
def test_an_answer_that_leaves_a_play_out_without_reading_its_whole_log_says_nothing_about_it(
    play_log: PlayLogCoverage, warnings: tuple[str, ...]
) -> None:
    board = _Board()
    _, offer = _offered_homer(board)
    answer = board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM, play_log=play_log), 50)
    board.feed.answers[-1] = dataclasses.replace(answer, warnings=warnings)
    assert board.majors(50) == (offer,)
    assert board.majors(80) == (offer,) and board.queue.card(HOMER_CARD) is not None


def test_a_big_play_corrected_before_it_is_offered_is_first_offered_under_its_revision() -> None:
    board = _Board()
    board.feed.answer(_state(away=1, inning=7, phase=InningPhase.BOTTOM), 0)
    homer = _play("e1", away=1, home=1, at=8, impact=BaseballScoringImpact(rbi=1, tying=True))
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 10, events=(homer,))
    board.at(0)
    board.at(10)
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 20, events=(_reviewed(homer),))
    board.at(20)
    assert board.at(38).offers == () and board.queue.card(HOMER_CARD) is not None  # up as the broadcast had it
    assert board.majors(40) == ()  # its successor has cleared, but the play it would name is out of date
    [revised] = board.majors(50)
    assert (revised.revision, revised.eligible) == (1, MonotonicTime(50.0))


def test_corrections_that_undo_each_other_before_either_airs_leave_the_play_offered_as_it_was() -> None:
    board = _Board()
    homer, offer = _offered_homer(board)
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 50, events=(_reviewed(homer),))
    assert board.majors(50) == ()
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 55, events=(homer,))
    assert board.majors(55) == ()
    assert board.majors(85) == (offer,)  # both have aired: the play stands as its card has shown it all along
    card = board.queue.card(HOMER_CARD)
    assert card is not None and card.payload.description == "a play"


def test_a_correction_that_leaves_a_big_play_no_longer_scoring_revises_its_card_and_offers_nothing() -> None:
    board = _Board()
    homer, _ = _offered_homer(board)
    no_rbi = _reviewed(homer, unscored=True)
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 50, events=(no_rbi,))
    assert board.majors(80) == ()
    card = board.queue.card(HOMER_CARD)
    assert card is not None and card.payload.description == "a play, reviewed"


def test_a_correction_to_a_big_play_after_its_game_s_final_has_aired_still_revises_its_card() -> None:
    board = _Board()
    final = _game(GameStatus.FINAL)
    board.feed.answer(_state(away=1, inning=7, phase=InningPhase.BOTTOM), 0)
    homer = _play("e1", away=1, home=1, at=8, impact=BaseballScoringImpact(rbi=1, tying=True))
    ended = _state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM)
    board.feed.answer(ended, 10, events=(homer,), status=GameStatus.FINAL)
    board.at(0)
    board.at(10, final)
    assert board.result(40, final).finals  # the home run aired at 38, the final at 40
    board.feed.answer(ended, 50, events=(_reviewed(homer),), status=GameStatus.FINAL)
    board.at(50, final)
    card = board.queue.card(HOMER_CARD)
    assert card is not None and card.payload.description == "a play"
    board.at(80, final)
    card = board.queue.card(HOMER_CARD)
    assert card is not None and card.payload.description == "a play, reviewed"


def test_a_big_play_whose_card_has_left_the_queue_is_forgotten_and_a_later_answer_changes_nothing() -> None:
    board = _Board()
    _offered_homer(board)
    board.queue.prune(_seconds(158))  # its window is over
    board.at(158)
    board.feed.answer(_state(away=1, home=1, inning=7, phase=InningPhase.BOTTOM), 170)  # one that no longer has it
    board.at(170)
    assert board.majors(200) == () and board.queue.card(HOMER_CARD) is None


def test_a_routine_big_play_offers_nothing() -> None:
    board = _Board(lag=0)
    board.feed.answer(_state(inning=7, phase=InningPhase.BOTTOM), 0)
    board.at(0)
    triple = _play("e1", away=0, home=0, at=9, event_type=BaseballGameEventType.TRIPLE)  # nobody scored
    board.feed.answer(_state(inning=7, phase=InningPhase.BOTTOM), 10, events=(triple,))
    view = board.at(10)
    assert board.queue.card("mlb:mlb_statsapi:g1:bigplay:e1") is not None and view.offers == ()


def test_a_big_play_an_outage_s_catch_up_brings_is_offered_flagged_so() -> None:
    board = _Board(lag=0)
    board.feed.answer(_state(inning=7, phase=InningPhase.BOTTOM), 0)
    board.at(0)
    board.feed.failing = True
    board.at(1)  # stale from here
    board.feed.failing = False
    homer = _play("e1", away=0, home=1, at=40, impact=BaseballScoringImpact(rbi=1, go_ahead=True))
    board.feed.answer(_state(home=1, inning=7, phase=InningPhase.BOTTOM), 50, events=(homer,))
    [offer] = board.majors(50)
    assert offer.origin is CandidateOrigin.CATCH_UP and offer.value is not None


def test_a_confirmed_no_hitter_offers_its_final_card_and_a_correction_offers_it_again_under_its_next_revision() -> None:
    board = _Board()
    board.feed.answer(_state(away=2, inning=9, phase=InningPhase.BOTTOM, home_hits=0), 0)
    no_hitter = _state(away=2, inning=9, phase=InningPhase.END, home_hits=0, home_reached_base=True)
    board.feed.answer(no_hitter, 10, status=GameStatus.FINAL)
    final = _game(GameStatus.FINAL)
    board.at(0)
    board.at(10, final)
    assert board.majors(39, final) == ()
    expected = PromotionCandidate(
        key=PromotionKey(GAME.id, "final"),
        kind=PromotionKind.MAJOR,
        eligible=MonotonicTime(40.0),  # the final's reveal
        card=DedupeKey(FINAL_KEY),
        value=interrupt_value(InterruptClass.NO_HITTER, predecessor=None, stakes=Stakes()),
    )
    assert board.majors(40, final) == (expected,)
    board.feed.answer(
        _state(away=3, inning=9, phase=InningPhase.END, home_hits=0, home_reached_base=True),
        50,
        status=GameStatus.FINAL,
    )
    board.at(50, final)
    [corrected] = board.majors(80, final)
    assert corrected.revision == 1 and corrected.eligible == MonotonicTime(80.0) and corrected.key == expected.key
    board.feed.answer(_state(away=3, inning=9, phase=InningPhase.END, home_hits=1), 90, status=GameStatus.FINAL)
    board.at(90, final)
    assert board.majors(120, final) == ()  # a scoring change took the no-hitter away: no feat, no offer


def test_a_correction_that_voids_a_perfect_game_withdraws_its_offer_though_the_final_card_reads_the_same() -> None:
    # The final reads as a perfect game; a correction finds a batter who reached on a dropped third strike. The
    # linescore is unchanged, so the final card is too, but the next view no longer offers a perfect game: it offers
    # the no-hitter under the next revision.
    board = _Board()
    board.feed.answer(_state(away=2, inning=9, phase=InningPhase.BOTTOM, home_hits=0, home_reached_base=False), 0)
    perfect = _state(away=2, inning=9, phase=InningPhase.END, home_hits=0, home_reached_base=False)
    board.feed.answer(perfect, 10, status=GameStatus.FINAL)
    final = _game(GameStatus.FINAL)
    board.at(0)
    board.at(10, final)
    [offer] = board.majors(40, final)
    assert offer.value == interrupt_value(InterruptClass.PERFECT_GAME, predecessor=None, stakes=Stakes())
    reached = _state(away=2, inning=9, phase=InningPhase.END, home_hits=0, home_reached_base=True)
    board.feed.answer(reached, 50, status=GameStatus.FINAL)
    board.at(50, final)
    [corrected] = board.majors(80, final)
    assert corrected.value == interrupt_value(InterruptClass.NO_HITTER, predecessor=None, stakes=Stakes())
    assert (corrected.key, corrected.revision, corrected.eligible) == (offer.key, 1, MonotonicTime(80.0))
    assert board.result(80, final).corrections == ()  # the card itself reads the same


def test_a_final_over_a_play_log_that_doesnt_cover_the_game_offers_no_feat_until_a_correction_covers_it() -> None:
    board = _Board()
    board.feed.answer(_state(away=2, inning=9, phase=InningPhase.BOTTOM, home_hits=0), 0)
    short = _state(away=2, inning=9, phase=InningPhase.END, home_hits=0, play_log=PlayLogCoverage.UNVERIFIED)
    board.feed.answer(short, 10, status=GameStatus.FINAL)
    final = _game(GameStatus.FINAL)
    board.at(0)
    board.at(10, final)
    assert board.result(40, final).finals and board.majors(41, final) == ()  # the card is up; no feat is offered
    covered = _state(away=2, inning=9, phase=InningPhase.END, home_hits=0, home_reached_base=False)
    board.feed.answer(covered, 50, status=GameStatus.FINAL)
    board.at(50, final)
    assert board.majors(79, final) == ()
    [offer] = board.majors(80, final)
    assert offer.value == interrupt_value(InterruptClass.PERFECT_GAME, predecessor=None, stakes=Stakes())
    assert (offer.revision, offer.eligible) == (1, MonotonicTime(80.0))
    assert board.majors(90, final) == (offer,)  # the same answer again revises nothing


def test_a_final_the_board_first_sees_already_over_offers_its_feat_only_as_a_baseline() -> None:
    board = _Board(lag=0)
    board.feed.answer(
        _state(away=2, inning=9, phase=InningPhase.END, home_hits=0, home_reached_base=True), 0, status=GameStatus.FINAL
    )
    [offer] = board.majors(0, _game(GameStatus.FINAL))
    assert offer.origin is CandidateOrigin.BASELINE


def test_an_active_bid_offers_its_no_hitter_card_as_a_reminder_each_inning_it_deepens_first_as_a_baseline() -> None:
    board = _Board(lag=0)
    board.feed.answer(_state(inning=7, away_hits=0), 0)  # tuned in to a bid through 6
    through_six = PromotionCandidate(
        key=PromotionKey(GAME.id, "home no_hitter through 6"),
        kind=PromotionKind.REMINDER,
        eligible=MonotonicTime(0.0),
        card=DedupeKey(NO_HITTER_KEY),
        origin=CandidateOrigin.BASELINE,
    )
    assert board.at(0).offers == (through_six,)
    board.feed.answer(_state(inning=7, away_hits=0), 5)
    assert board.at(5).offers == (through_six,)  # the same moment while the bid stands at that depth
    board.feed.answer(_state(inning=7, phase=InningPhase.BOTTOM, away_hits=0), 10)
    [through_seven] = board.at(10).offers
    assert through_seven.key.play == "home no_hitter through 7" and through_seven.eligible == MonotonicTime(10.0)
    assert through_seven.origin is CandidateOrigin.LIVE
    board.feed.answer(_state(inning=7, phase=InningPhase.BOTTOM, away_hits=1), 20)
    assert board.at(20).offers == ()  # broken: its card is gone, and so is the reminder


def test_a_bid_over_a_play_log_that_doesnt_cover_the_game_shows_its_card_but_is_offered_only_once_one_does() -> None:
    # Tuned in to a bid through 6 over a log that stops short; the card shows, the reminder waits. The first covering
    # answer is the bid's baseline, as the board's first look would have been.
    board = _Board(lag=0)
    board.feed.answer(_state(inning=7, away_hits=0, play_log=PlayLogCoverage.UNVERIFIED), 0)
    assert board.at(0).offers == () and board.queue.card(NO_HITTER_KEY) is not None
    board.feed.answer(_state(inning=7, away_hits=0), 5)
    [reminder] = board.at(5).offers
    assert (reminder.key.play, reminder.eligible, reminder.origin) == (
        "home no_hitter through 6",
        MonotonicTime(5.0),
        CandidateOrigin.BASELINE,
    )


def test_a_perfect_bid_read_once_over_a_log_that_stops_short_is_not_offered_then_and_keeps_its_moment() -> None:
    board = _Board(lag=0)
    board.feed.answer(_state(inning=7, away_hits=0, away_reached_base=False), 0)
    [perfect] = board.at(0).offers
    assert perfect.key.play == "home perfect_game through 6"
    # A log that stops short can't vouch for the clean sheet: the card reads a no-hitter, and nothing is offered.
    board.feed.answer(_state(inning=7, away_hits=0, play_log=PlayLogCoverage.UNVERIFIED), 5)
    assert board.at(5).offers == ()
    card = board.queue.card(NO_HITTER_KEY)
    assert card is not None and card.payload.perfect is False
    board.feed.answer(_state(inning=7, away_hits=0, away_reached_base=False), 10)
    assert board.at(10).offers == (perfect,)  # the same moment, from when it first cleared


def test_a_bid_that_passes_to_the_other_staff_at_the_same_depth_is_a_moment_of_its_own() -> None:
    # Both sides hitless into the top of the 7th: the home staff's bid through 6 shows. The away side's first hit
    # leaves the away staff's, also through 6: the card now names the other staff, and so does the moment.
    board = _Board(lag=0)
    board.feed.answer(_state(inning=7, away_hits=0, home_hits=0), 0)
    [home] = board.at(0).offers
    board.feed.answer(_state(inning=7, away_hits=1, home_hits=0), 10)
    [away] = board.at(10).offers
    assert (home.key.play, away.key.play) == ("home no_hitter through 6", "away no_hitter through 6")
    assert away.eligible == MonotonicTime(10.0)
    card = board.queue.card(NO_HITTER_KEY)
    assert card is not None and card.payload.pitching_side is HomeAway.AWAY
