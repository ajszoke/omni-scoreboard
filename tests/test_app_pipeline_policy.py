"""What the MLB pipeline tells the board's policy: `LiveBaseballPipeline.refresh`'s ``policy``, from released state only.

Every test drives `LiveBaseballPipeline.refresh` over a fake game feed whose answers arrive when the test says: a read
hands the newest answer to have arrived by then, again until a newer one does, as the poll plan does. Each test
refreshes as each answer arrives, on a 30 s MLB delay unless it says otherwise. States, plays and break signals are
synthetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.pipeline import LiveBaseballPipeline
from omni.cards.base import DedupeKey
from omni.cards.factory import CardFactory
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, League, UpdateUrgency
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
    home_reached_base: bool | None = None,
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
        home_reached_base=home_reached_base,
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

    def at(self, seconds: float, game: TeamGame = GAME) -> PolicyView:
        result = self.pipeline.refresh(
            [game], at=_seconds(seconds), fetch_feed=self.feed, fetch_pregame_details=lambda _: self.details
        )
        return result.policy

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
        key=PromotionKey(GAME.id, "no_hitter through 6"),
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
    assert through_seven.key.play == "no_hitter through 7" and through_seven.eligible == MonotonicTime(10.0)
    assert through_seven.origin is CandidateOrigin.LIVE
    board.feed.answer(_state(inning=7, phase=InningPhase.BOTTOM, away_hits=1), 20)
    assert board.at(20).offers == ()  # broken: its card is gone, and so is the reminder
