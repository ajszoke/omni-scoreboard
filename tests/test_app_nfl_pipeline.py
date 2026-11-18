"""Tests for LiveFootballPipeline: NFL's answers into delay-safe cards on the shared board's queue.

Every test drives `LiveFootballPipeline.refresh` over a fake `NflFeeds` on a `FakeClock`, with a 45 s NFL delay
unless it says otherwise. Plays and states are synthetic.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.nfl_feeds import NflReading
from omni.app.nfl_pipeline import LiveFootballPipeline, NflCardPolicy
from omni.app.pipeline import PipelineResult
from omni.cards.base import CardId, CardKind, DedupeKey, EndBasis, GameEnd, ScoreboardCard
from omni.cards.football import LiveFootballCardPayload
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import DisplayPriority, GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone, SetPiece
from omni.events.football import FootballEvent, FootballEventKind
from omni.preferences.favorites import Favorites
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, FeedNote, FeedNoteKind, NflLiveSnapshot, NflPhase
from omni.queue.delay_policy import DelayPolicy
from omni.queue.scheduler import InterleavedCardQueue

T = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
LAG = 45
LONG = timedelta(minutes=10)  # how long a test's answer is current unless the test is about staleness
_TEAMS = NflTeamRegistry.default(source=NFLMETA_SOURCE)


def nfl_game(raw: str = "401", away: str = "DAL", home: str = "PHI", status: GameStatus = GameStatus.LIVE) -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.NFL, NFLMETA_SOURCE, raw),
        league=League.NFL,
        status=status,
        scheduled_start=T,
        away=_TEAMS.resolve(away),
        home=_TEAMS.resolve(home),
    )


def drive(
    away: int = 0,
    home: int = 0,
    *,
    period: int = 1,
    clock: int = 600,
    down: int = 1,
    to_go: int = 60,
    interval: FootballInterval | None = None,
) -> FootballGameState:
    """A moment of a game: AWAY's ball, ``down`` and 10 at ``to_go`` yards from the goal, or a break."""
    if interval is not None:
        return FootballGameState(period=period, clock_seconds=0, away_score=away, home_score=home, interval=interval)
    return FootballGameState(
        period=period,
        clock_seconds=clock,
        away_score=away,
        home_score=home,
        possession=HomeAway.AWAY,
        down=down,
        distance=10,
        yards_to_goal=to_go,
    )


KICKOFF = FootballGameState(
    period=1,
    clock_seconds=900,
    away_score=0,
    home_score=0,
    possession=HomeAway.HOME,
    yards_to_goal=65,
    set_piece=SetPiece.KICKOFF,
)


def play(key: str, written: datetime, *, major: bool = False, headline: str = "RUN +4") -> FootballEvent:
    """A synthetic play: a score is major, a run routine."""
    tone = PlayTone.SCORE if major else PlayTone.STANDARD
    return FootballEvent(
        key=key,
        kind=FootballEventKind.PLAY,
        summary=PlaySummary(tone=tone, headline=headline, detail="A synthetic play."),
        written_at=written,
    )


class FakeNflFeeds:
    """NFL feeds a test hands answers to: each taken once, oldest first; the newest stays readable."""

    def __init__(self) -> None:
        self.waiting: dict[LeagueScopedId, list[NflReading]] = defaultdict(list)
        self.newest: dict[LeagueScopedId, NflReading] = {}
        self.taken: list[LeagueScopedId] = []

    def answer(
        self,
        game: TeamGame,
        state: FootballGameState | None,
        arrived: ClockReading,
        *,
        events: tuple[FootballEvent, ...] = (),
        phase: NflPhase | None = None,
        observed: datetime | None = None,
        break_ends_at: datetime | None = None,
        current_for: timedelta | None = LONG,
        poll_every: int = 10,
        notes: tuple[FeedNote, ...] = (),
    ) -> None:
        snapshot = NflLiveSnapshot(
            game=game,
            phase=phase if phase is not None else (NflPhase.IN if state is not None else NflPhase.PRE),
            state=state,
            events=events,
            caught_up=True,
            break_ends_at=break_ends_at,
            observed_at=observed if observed is not None else arrived.utc,
            poll_every=DurationSeconds(poll_every),
            notes=notes,
        )
        reading = NflReading(snapshot, arrived, current_for)
        self.waiting[game.id].append(reading)
        self.newest[game.id] = reading

    def take(self, game: TeamGame) -> list[NflReading]:
        self.taken.append(game.id)
        return self.waiting.pop(game.id, [])

    def latest(self, game: TeamGame) -> NflReading | None:
        return self.newest.get(game.id)


class Board:
    """The pipeline, its feeds and its queue on a fake clock that starts at ``T``."""

    def __init__(
        self, *, favorites: Favorites = Favorites(), lag: int = LAG, policy: NflCardPolicy = NflCardPolicy()
    ) -> None:
        self.clock = FakeClock(T)
        self.feeds = FakeNflFeeds()
        self.queue = InterleavedCardQueue()
        self.pipeline = LiveFootballPipeline(
            feeds=self.feeds,
            delay_policy=DelayPolicy(broadcast_lag=DurationSeconds(lag)),
            queue=self.queue,
            favorites=favorites,
            policy=policy,
        )

    @property
    def now(self) -> ClockReading:
        return self.clock.read()

    def to(self, seconds: float) -> ClockReading:
        """Move the clock on to ``seconds`` past ``T``."""
        self.clock.advance(T + timedelta(seconds=seconds) - self.clock.now())
        return self.clock.read()

    def answer(self, game: TeamGame, state: FootballGameState | None, **kwargs: Any) -> None:
        self.feeds.answer(game, state, self.clock.read(), **kwargs)

    def refresh(self, *games: TeamGame) -> PipelineResult:
        return self.pipeline.refresh(games, at=self.clock.read())

    def at(self, seconds: float, *games: TeamGame) -> PipelineResult:
        """Move on to ``seconds`` and refresh."""
        self.to(seconds)
        return self.refresh(*games)

    def card(self, game: TeamGame, kind: str = "live") -> ScoreboardCard[Any] | None:
        return self.queue.card(f"{game.id}:{kind}")

    def payload(self, game: TeamGame, kind: str = "live") -> LiveFootballCardPayload:
        card = self.card(game, kind)
        assert card is not None
        assert isinstance(card.payload, LiveFootballCardPayload)
        return card.payload

    def caption(self, game: TeamGame) -> str | None:
        last = self.payload(game).last_play
        return last.headline if last is not None else None


def _seconds(instant: MonotonicTime | None) -> float | None:
    return instant.seconds if instant is not None else None


GAME = nfl_game()
LIVE_KEY = "nfl:nflmeta:401:live"
FINAL_KEY = "nfl:nflmeta:401:final"


# --- release ---------------------------------------------------------------------------------------------------------


def test_a_live_state_is_carded_once_it_clears_the_nfl_delay_and_not_before() -> None:
    board = Board()
    board.answer(GAME, KICKOFF)
    first = board.refresh(GAME)
    assert board.card(GAME) is None
    assert first.held == (GAME.id,) and first.releases == ((GAME.id, T + timedelta(seconds=LAG)),)
    assert _seconds(board.pipeline.next_deadline(board.now)) == LAG
    assert board.at(44.9, GAME).ingested == () and board.card(GAME) is None
    released = board.at(45, GAME)
    assert released.ingested == (CardId(LIVE_KEY),) and released.held == ()
    card = board.card(GAME)
    assert card is not None and card.kind is CardKind.LIVE_GAME and card.contest == GAME
    assert all(card.layout_support.supports(profile) for profile in PanelProfile)
    assert card.payload == LiveFootballCardPayload(state=KICKOFF)  # no caption, no meter
    assert card.priority.band is DisplayPriority.NORMAL


def test_the_card_shows_the_newest_state_to_have_cleared_and_isn_t_restaged_when_nothing_changed() -> None:
    board = Board()
    board.answer(GAME, KICKOFF)
    board.at(10)
    board.answer(GAME, drive(clock=880))
    board.at(20)
    board.answer(GAME, drive(clock=870))
    board.at(60, GAME)
    assert board.payload(GAME).state == drive(clock=880)  # the answer at 20 s is still in the delay
    board.answer(GAME, drive(clock=870))  # the same state again
    assert board.at(65, GAME).ingested == (CardId(LIVE_KEY),)
    assert board.payload(GAME).state == drive(clock=870)
    assert board.at(105, GAME).ingested == ()  # its repeat cleared, but nothing on the card changed


@pytest.mark.parametrize(
    ("written", "release"),
    [
        (-10, 35),  # written 10 s before the answer that brought it: shown 10 s sooner
        (-180, 45),  # written longer ago than a play reaches a board: its time isn't believed
        (30, 45),  # written after the answer that brought it: one of the clocks is off
    ],
)
def test_a_play_s_own_write_time_shortens_its_hold_only_when_credible(written: int, release: float) -> None:
    board = Board()
    board.answer(GAME, KICKOFF)
    board.at(45, GAME)
    board.to(50)
    board.answer(GAME, drive(clock=880), events=(play("p1", board.now.utc + timedelta(seconds=written)),))
    board.at(50 + release - 0.1, GAME)
    assert board.caption(GAME) is None
    board.at(50 + release, GAME)
    assert board.caption(GAME) == "RUN +4"


def test_the_first_answer_s_plays_are_from_before_the_board_tuned_in_and_never_caption_it() -> None:
    board = Board()
    board.answer(GAME, drive(), events=(play("old", T, major=True),))
    board.at(1, GAME)
    board.answer(GAME, drive(), events=(play("old", T, major=True), play("new", board.now.utc)))
    board.at(46, GAME)
    assert board.caption(GAME) == "RUN +4"
    assert board.payload(GAME).last_play == play("new", T).summary


def test_an_older_play_released_after_a_newer_one_never_takes_its_place() -> None:
    board = Board()
    board.answer(GAME, drive())
    board.at(10, GAME)
    late = play("older", T - timedelta(minutes=5), headline="OLDER")  # not credible: the full delay
    fresh = play("newer", T + timedelta(seconds=8), headline="NEWER")  # credible: 2 s sooner
    board.answer(GAME, drive(), events=(late, fresh))
    board.at(53, GAME)
    assert board.caption(GAME) == "NEWER"
    board.at(55, GAME)
    assert board.caption(GAME) == "NEWER"


# --- the caption's lifetime ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("major", "gone"), [(False, 90), (True, 150)])
def test_a_caption_is_gone_by_its_hard_end(major: bool, gone: int) -> None:
    board = Board()
    board.answer(GAME, drive())
    board.at(5, GAME)
    board.answer(GAME, drive(clock=590), events=(play("p", board.now.utc, major=major),))
    board.at(50, GAME)
    assert board.caption(GAME) == "RUN +4"
    assert _seconds(board.pipeline.next_deadline(board.now)) == 50 + gone
    board.at(50 + gone - 0.1, GAME)
    assert board.caption(GAME) == "RUN +4"
    assert board.at(50 + gone, GAME).ingested == (CardId(LIVE_KEY),)
    assert board.caption(GAME) is None


@pytest.mark.parametrize(("major", "start_within"), [(False, 60), (True, 120)])
def test_a_caption_that_can_t_start_within_its_bound_never_goes_up(major: bool, start_within: int) -> None:
    board = Board(policy=NflCardPolicy(stale_for=timedelta(minutes=5)))  # a quiet card stays up, dimmed
    board.answer(GAME, drive(), current_for=timedelta(seconds=20))
    board.at(5, GAME)
    board.answer(GAME, drive(), events=(play("p", board.now.utc, major=major),), current_for=timedelta(seconds=20))
    board.at(50, GAME)  # released, but the feed went quiet at 25 s: the card is dimmed, the field alone
    assert board.caption(GAME) is None and board.pipeline.next_deadline(board.now) == MonotonicTime(50 + start_within)
    board.at(50 + start_within, GAME)
    board.answer(GAME, drive(), current_for=LONG)  # current again, but too late to start
    board.refresh(GAME)
    assert board.caption(GAME) is None


def test_a_caption_that_started_in_time_stays_until_its_hard_end_through_a_quiet_spell() -> None:
    board = Board()
    board.answer(GAME, drive(), current_for=timedelta(seconds=20))
    board.at(5, GAME)
    board.answer(GAME, drive(), events=(play("p", board.now.utc),), current_for=timedelta(seconds=20))
    board.at(50, GAME)
    board.answer(GAME, drive(), current_for=LONG)
    board.refresh(GAME)
    assert board.caption(GAME) == "RUN +4"  # current again within its 60 s
    board.answer(GAME, drive(), current_for=timedelta(seconds=1))
    board.at(60, GAME)
    assert board.caption(GAME) is None  # quiet: the field alone, but its time still runs
    board.answer(GAME, drive(), current_for=LONG)
    board.at(130, GAME)
    assert board.caption(GAME) == "RUN +4"  # past its start bound, but it had started


def test_a_caption_goes_once_a_newer_answer_s_field_moves_on_and_stays_while_only_the_clock_runs() -> None:
    board = Board()
    board.answer(GAME, drive())
    board.at(10, GAME)
    board.answer(GAME, drive(down=2, to_go=56), events=(play("p", board.now.utc),))
    board.at(20, GAME)
    board.answer(GAME, drive(down=2, to_go=56, clock=560))  # the clock alone ran
    board.at(30, GAME)
    board.answer(GAME, drive(down=3, to_go=50))  # the next snap, its play not in yet
    board.at(55, GAME)
    assert board.caption(GAME) == "RUN +4"
    board.at(65, GAME)
    assert board.caption(GAME) == "RUN +4"
    board.at(75, GAME)
    assert board.caption(GAME) is None and board.payload(GAME).state == drive(down=3, to_go=50)


def test_in_a_break_the_card_shows_the_field_alone_and_halftime_counts_down_on_the_viewer_s_clock() -> None:
    board = Board()
    board.answer(GAME, drive(period=2, clock=20))
    board.at(10, GAME)
    ends = board.now.utc + timedelta(minutes=13)
    halftime = drive(7, 3, period=2, interval=FootballInterval.HALFTIME)
    board.answer(GAME, halftime, events=(play("p", board.now.utc),), break_ends_at=ends)
    board.at(55, GAME)
    payload = board.payload(GAME)
    assert payload.state == halftime and payload.last_play is None
    assert payload.break_ends_at == ends + timedelta(seconds=LAG)


# --- the final ----------------------------------------------------------------------------------------------------


def _live_at_45(board: Board, game: TeamGame = GAME) -> None:
    board.answer(game, drive(14, 10, period=4))
    board.at(45, game)
    assert board.card(game) is not None


FINAL_STATE = drive(17, 10, period=4, interval=FootballInterval.FINAL)


def test_a_final_reveals_from_the_feed_s_own_answer_after_the_delay_and_replaces_the_live_card() -> None:
    board = Board()
    _live_at_45(board)
    board.to(60)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    held = board.refresh(GAME)
    assert board.card(GAME, "final") is None and held.held == (GAME.id,)
    assert held.releases == ((GAME.id, T + timedelta(seconds=105)),)
    assert board.at(104.9, GAME).finals == () and board.card(GAME) is not None  # the live card holds meanwhile
    revealed = board.at(105, GAME)
    ended = GameEnd(T + timedelta(seconds=60), EndBasis.FIRST_SEEN)
    assert revealed.finals == (CardId(FINAL_KEY),) and revealed.ends == ((CardId(FINAL_KEY), ended),)
    assert revealed.removed == (GAME.id,) and board.card(GAME) is None
    final = board.card(GAME, "final")
    assert final is not None and final.kind is CardKind.FINAL
    assert final.replaces == DedupeKey(LIVE_KEY) and final.ended == ended
    assert final.payload == LiveFootballCardPayload(state=FINAL_STATE)
    assert final.timing.available_at == T + timedelta(seconds=105)
    assert final.timing.expires_at == T + timedelta(seconds=105, minutes=30)  # the post-game window


def test_a_second_final_answer_before_the_reveal_waits_its_own_delay_then_corrects_the_card() -> None:
    board = Board()
    _live_at_45(board)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    board.to(46)
    newer = drive(17, 13, period=4, interval=FootballInterval.FINAL)
    board.answer(GAME, newer, phase=NflPhase.POST)
    assert board.at(90, GAME).finals == (CardId(FINAL_KEY),)
    assert board.payload(GAME, "final").state == FINAL_STATE  # the newer answer is still in the delay
    assert board.at(91, GAME).corrections == (CardId(FINAL_KEY),)
    assert board.payload(GAME, "final").state == newer


def test_the_schedule_calling_a_game_final_reveals_nothing_until_its_feed_does() -> None:
    board = Board()
    _live_at_45(board)
    over = nfl_game(status=GameStatus.FINAL)
    board.answer(over, drive(14, 10, period=4, clock=5))
    result = board.at(50, over)
    assert result.unconfirmed == (over.id,) and result.finals == () and board.card(over, "final") is None
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(80)  # its feed has 30 s to agree
    stale = board.at(80, over)
    assert stale.stale == (over.id,) and "stale: the schedule says final" in stale.warnings[0]
    card = board.card(over)
    assert card is not None and card.stale_since == T + timedelta(seconds=80)
    board.answer(over, FINAL_STATE, phase=NflPhase.POST)
    agreed = board.at(90, over)
    assert agreed.unconfirmed == () and agreed.stale == ()
    assert board.at(135, over).finals == (CardId(FINAL_KEY),)


def test_a_final_by_the_schedule_with_no_answer_at_all_is_unfed_and_shows_nothing() -> None:
    board = Board()
    over = nfl_game(status=GameStatus.FINAL)
    result = board.at(0, over)
    assert result.skipped == (f"{over.id}: no answer yet",) and result.stale == (over.id,)
    assert board.card(over) is None and board.card(over, "final") is None


def test_finals_are_ordered_by_when_their_games_ended() -> None:
    board = Board()
    later, sooner = nfl_game("402", "MIN", "GB"), nfl_game("403", "SF", "SEA")
    board.answer(later, drive(period=4))
    board.answer(sooner, drive(period=4))
    board.at(45, later, sooner)
    board.answer(sooner, FINAL_STATE, phase=NflPhase.POST)
    board.to(50)
    board.answer(later, FINAL_STATE, phase=NflPhase.POST)
    revealed = board.at(95, later, sooner)
    assert revealed.ends == (
        (CardId("nfl:nflmeta:402:final"), GameEnd(T + timedelta(seconds=50), EndBasis.FIRST_SEEN)),
        (CardId("nfl:nflmeta:403:final"), GameEnd(T + timedelta(seconds=45), EndBasis.FIRST_SEEN)),
    )
    view = board.queue.publish()
    pick = board.queue.peek(board.now, PanelProfile.QUAD_128X64, view=view)
    assert pick is not None and pick.card.id == CardId("nfl:nflmeta:403:final")  # the game that ended first


def test_a_later_final_answer_with_a_different_result_corrects_the_card_after_its_own_delay() -> None:
    board = Board()
    _live_at_45(board)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    board.at(105, GAME)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)  # the same result again
    board.answer(GAME, drive(20, 10, period=4))  # and a live answer, which a final never takes
    assert board.at(150, GAME).corrections == ()
    corrected = drive(17, 13, period=4, interval=FootballInterval.FINAL)
    board.answer(GAME, corrected, phase=NflPhase.POST)
    assert board.at(194.9, GAME).corrections == () and board.payload(GAME, "final").state == FINAL_STATE
    before = board.card(GAME, "final")
    assert board.at(195, GAME).corrections == (CardId(FINAL_KEY),)
    after = board.card(GAME, "final")
    assert after is not None and before is not None and after.payload.state == corrected
    assert after.timing == before.timing  # corrected in place: its window runs on


def test_a_final_whose_window_is_over_stays_gone_whatever_comes_later() -> None:
    board = Board()
    _live_at_45(board)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    board.at(105, GAME)
    board.to(105 + 1800)
    board.queue.prune(board.now)
    board.answer(GAME, drive(20, 13, period=4, interval=FootballInterval.FINAL), phase=NflPhase.POST)
    later = board.at(105 + 1900, GAME)
    assert later.corrections == () and later.finals == () and board.card(GAME, "final") is None


def test_a_live_game_whose_feed_says_it_s_over_reveals_its_final_though_the_schedule_still_says_live() -> None:
    board = Board()
    _live_at_45(board)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    board.at(50, GAME)
    board.answer(GAME, drive(14, 10, period=4, clock=1))  # a live answer after the final changes nothing
    assert board.at(95, GAME).finals == (CardId(FINAL_KEY),)
    assert board.at(140, GAME).ingested == () and board.card(GAME) is None


def test_the_live_card_carries_on_through_the_final_s_embargo_from_states_already_in_the_delay() -> None:
    board = Board()
    _live_at_45(board)
    board.answer(GAME, drive(17, 10, period=4, clock=2))
    board.to(50)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    board.at(60, GAME)
    assert board.payload(GAME).state == drive(14, 10, period=4)
    board.at(90, GAME)
    assert board.payload(GAME).state == drive(17, 10, period=4, clock=2)  # cleared while the final is held
    assert board.at(95, GAME).finals == (CardId(FINAL_KEY),)


def test_a_final_with_its_live_card_already_down_replaces_nothing() -> None:
    board = Board()
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    board.at(45, GAME)
    final = board.card(GAME, "final")
    assert final is not None and final.replaces is None


# --- staleness and health -----------------------------------------------------------------------------------------


def test_a_live_card_dims_two_cadences_after_its_last_answer_and_comes_down_two_minutes_later() -> None:
    board = Board(lag=0)
    board.answer(GAME, drive(), current_for=None, poll_every=10)
    assert board.at(0, GAME).ingested == (CardId(LIVE_KEY),)
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(20)
    stale = board.at(20, GAME)
    assert stale.stale == (GAME.id,) and stale.warnings == (
        f"{GAME.id}: stale: its feed's newest answer is out of date",
    )
    card = board.card(GAME)
    assert card is not None and card.stale_since == T + timedelta(seconds=20)
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(140)
    assert board.at(139.9, GAME).removed == () and board.card(GAME) is not None
    board.at(140, GAME)
    assert board.card(GAME) is None
    board.answer(GAME, drive(clock=500), current_for=None)
    back = board.at(150, GAME)
    assert back.stale == () and board.payload(GAME).state == drive(clock=500)
    card = board.card(GAME)
    assert card is not None and card.stale_since is None


def test_an_answer_that_says_how_long_it_s_current_is_taken_at_its_word() -> None:
    board = Board(lag=0)
    board.answer(GAME, drive(), current_for=timedelta(seconds=90), poll_every=10)
    board.at(0, GAME)
    assert board.at(89.9, GAME).stale == ()
    assert board.at(90, GAME).stale == (GAME.id,)


def test_a_dimmed_card_brightens_once_a_newer_state_clears_the_delay() -> None:
    board = Board()
    board.answer(GAME, drive(), current_for=timedelta(seconds=60))
    board.at(45, GAME)
    board.at(70, GAME)
    dimmed = board.card(GAME)
    assert dimmed is not None and dimmed.stale_since == T + timedelta(seconds=60)
    board.answer(GAME, drive())  # the same field, current again
    board.refresh(GAME)
    card = board.card(GAME)
    assert card is not None and card.stale_since is not None  # what's on it is still the old answer's
    board.at(115, GAME)
    card = board.card(GAME)
    assert card is not None and card.stale_since is None


def test_a_halftime_hold_keeps_the_card_current_until_the_break_it_names_is_over() -> None:
    board = Board(lag=0)
    ends = T + timedelta(minutes=13)
    halftime = drive(7, 3, period=2, interval=FootballInterval.HALFTIME)
    board.answer(GAME, halftime, break_ends_at=ends, current_for=None, poll_every=10)
    board.at(0, GAME)
    assert board.pipeline.next_deadline(board.now) == MonotonicTime(13 * 60 + 20)
    assert board.at(13 * 60 + 19.9, GAME).stale == ()
    assert board.at(13 * 60 + 20, GAME).stale == (GAME.id,)


def test_a_live_game_with_no_answer_at_all_is_unfed_at_once() -> None:
    board = Board()
    result = board.at(0, GAME)
    assert result.skipped == (f"{GAME.id}: no answer yet",) and result.stale == (GAME.id,)
    assert result.held == () and board.card(GAME) is None


def test_a_feed_s_notes_are_passed_on_as_warnings() -> None:
    board = Board()
    note = FeedNote(FeedNoteKind.PLAY_READ_FAILED, "page 2")
    board.answer(GAME, drive(), notes=(note,))
    assert board.at(0, GAME).warnings == (f"{GAME.id}: play read failed: page 2",)


# --- priority, phases and teardown ------------------------------------------------------------------------------


def test_a_favorite_s_game_is_in_the_favorite_band_and_an_mlb_favorite_s_namesake_is_not() -> None:
    board = Board(favorites=Favorites({League.MLB: ("MIN",), League.NFL: ("GB", "CAR")}))
    vikings, packers = nfl_game("402", "MIN", "CHI"), nfl_game("403", "CAR", "GB")
    board.answer(vikings, drive())
    board.answer(packers, drive())
    board.at(45, vikings, packers)
    favorite, namesake = board.card(packers), board.card(vikings)
    assert favorite is not None and namesake is not None
    assert favorite.priority.band is DisplayPriority.FAVORITE and favorite.priority.reasons == ("favorite #1 GB",)
    assert namesake.priority.band is DisplayPriority.NORMAL


def test_an_upcoming_game_gets_no_card_and_its_answers_wait_until_the_slate_says_it_s_live() -> None:
    board = Board()
    upcoming = nfl_game(status=GameStatus.SCHEDULED)
    board.answer(upcoming, None)
    board.answer(upcoming, KICKOFF)  # the feed has it under way before the slate does
    result = board.at(30, upcoming)
    assert board.feeds.taken == [] and len(board.feeds.waiting[upcoming.id]) == 2
    assert result.ingested == () and result.held == () and result.stale == () and len(board.queue) == 0
    board.at(31, GAME)
    assert board.at(45, GAME).ingested == (CardId(LIVE_KEY),)  # from its first answer, released as it would be
    assert board.payload(GAME).state == KICKOFF


def test_a_game_that_leaves_the_live_phase_loses_its_live_card() -> None:
    board = Board()
    _live_at_45(board)
    gone = board.at(50)
    assert gone.removed == (GAME.id,) and board.card(GAME) is None and len(board.queue) == 0


def test_a_game_that_leaves_the_slate_loses_its_final_revealed_or_not() -> None:
    board = Board()
    _live_at_45(board)
    board.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    board.at(90, GAME)
    assert board.card(GAME, "final") is not None
    board.at(100)
    assert board.card(GAME, "final") is None and board.pipeline.next_deadline(board.now) is None
    held = Board()
    _live_at_45(held)
    held.answer(GAME, FINAL_STATE, phase=NflPhase.POST)
    held.at(50, GAME)
    held.at(60)  # gone while its result was still in the delay
    assert held.at(90).finals == () and len(held.queue) == 0 and held.pipeline.next_deadline(held.now) is None


def test_a_game_off_the_slate_before_its_feed_agrees_is_forgotten_and_held_for_nothing() -> None:
    board = Board()
    _live_at_45(board)
    over = nfl_game(status=GameStatus.FINAL)
    board.at(50, over)
    assert board.at(80, over).stale == (over.id,)  # unconfirmed, and its card dimmed
    gone = board.at(85)
    assert gone.removed == (over.id,) and gone.stale == () and gone.unconfirmed == ()
    assert board.pipeline.next_deadline(board.now) is None and len(board.queue) == 0


def test_a_live_game_whose_answer_has_no_state_yet_gets_no_card() -> None:
    board = Board()
    board.answer(GAME, None)
    result = board.at(60, GAME)
    assert result.ingested == () and result.held == () and result.stale == () and board.card(GAME) is None


def test_the_card_policy_s_defaults_are_the_documented_bounds() -> None:
    policy = NflCardPolicy()
    assert policy.caption_bounds(False) == (timedelta(seconds=60), timedelta(seconds=90))
    assert policy.caption_bounds(True) == (timedelta(seconds=120), timedelta(seconds=150))
    assert policy.postgame_window == timedelta(minutes=30) and policy.stale_for == timedelta(minutes=2)
