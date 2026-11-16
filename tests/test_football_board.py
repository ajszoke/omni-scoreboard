"""The single-game football loop, its play strip, and the frame stream it can draw through."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from omni.core.clock import ClockReading, MonotonicTime
from omni.app.display import RecordingDisplaySink
from omni.app.football import _parse_args
from omni.app.football_feed import GameFeed
from omni.app.football_board import (
    CLOSING_WAIT,
    STRIP_DWELL,
    FRAME_MAX,
    FRAME_MIN,
    METER_MAX_AGE,
    UNCONFIRMED_NOTE,
    UNSHOWN_AFTER,
    FootballBoard,
)
from omni.app.frame_stream import DisplayLink, DisplayUnavailable, StreamDisplaySink, StreamFrame
from omni.cards.base import ScoreboardCard
from omni.cards.football import LiveFootballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.domain.football import FootballInterval, PlaySummary, PlayTone
from omni.renderers.context import RenderContext
from omni.renderers.text_lane import DEFAULT_MARQUEE, ScrollSpeed, TextLane
from omni.events.football import FootballEvent, FootballEventKind
from omni.providers.base import ProviderError
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import FINAL_DRAIN, HALFTIME_HOLD, NFLMETA_SOURCE, NflMetaLiveProvider
from omni.renderers.canvas import Canvas
from omni.tower import ChangeKind, EndReason, LaneChange, Provenance, Segment, SegmentKey, Surface
from tests.test_nflmeta import GAME, FakeFeed, _change, _page, _row, _scores, _synced

T0 = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


def _at(moment: datetime) -> ClockReading:
    """The clock's two readings at ``moment``; the monotonic one counts from T0."""
    return ClockReading(utc=moment, monotonic=MonotonicTime((moment - T0).total_seconds()))


def _board(
    feed: FakeFeed, *, sink: RecordingDisplaySink | None = None, **kwargs: Any
) -> tuple[FootballBoard, RecordingDisplaySink, list[str]]:
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=feed)
    lines: list[str] = []
    sink = sink if sink is not None else RecordingDisplaySink(PanelProfile.QUAD_128X64)
    source = GameFeed(provider, GAME, win_probability=kwargs.pop("win_probability", None))
    return FootballBoard(source, sink, log=lines.append, **kwargs), sink, lines


def test_polls_when_due_and_draws_every_step() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    feed.plays = [_page(_change("p1", 1, "C.Johnston punts 44 yards to CIN 18.", play_type="punt"))]
    board, sink, lines = _board(
        feed, win_probability=lambda game, now: WinProbability(home=83.0, away=17.0), meter_lag=timedelta(0)
    )
    board.step(_at(T0))
    board.step(_at(T0 + 5 * S))
    assert sum(path == "/api/v1/live-scores" for path, _ in feed.requests) == 1
    assert sink.committed == 2
    assert lines == [
        "PIT @ CIN 0-10 Q2 13:09 [in] | home ball, 2&6, 75 to go | TO 3-3 CH 2-2 | meter 17-83",
        "  > PUNT | C.Johnston punts 44 yards to CIN 18.",  # what arrived
        "  = PUNT | C.Johnston punts 44 yards to CIN 18.",  # and what the strip put up, apart
    ]
    board.step(_at(T0 + 10 * S))
    assert sum(path == "/api/v1/live-scores" for path, _ in feed.requests) == 2
    assert len(lines) == 3  # nothing changed, nothing logged
    assert board.showing is not None


def test_a_profile_without_a_strip_logs_plays_without_putting_them_up() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    feed.plays = [_page(_change("p1", 1, "C.Johnston punts 44 yards to CIN 18.", play_type="punt"))]
    board, sink, lines = _board(feed, sink=RecordingDisplaySink(PanelProfile.STACK_64X64))
    board.step(_at(T0))
    assert sink.committed == 1 and lines[1:] == [
        "  > PUNT | C.Johnston punts 44 yards to CIN 18."
    ]  # arrived, not shown
    assert board._strip.idle


class _FailingSink(RecordingDisplaySink):
    failing = True

    def commit(self, frame: Canvas) -> None:
        if self.failing:
            raise DisplayUnavailable("the display isn't taking frames")
        super().commit(frame)


def test_a_frame_the_display_did_not_take_spends_no_strip_time() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    feed.plays = [_page(_change("p1", 1, "C.Johnston punts 44 yards to CIN 18.", play_type="punt"))]
    sink = _FailingSink(PanelProfile.QUAD_128X64)
    board, _, lines = _board(feed, sink=sink)
    board.step(_at(T0))
    board.step(_at(T0 + 5 * S))  # the same failure is logged once
    assert lines[-1] == "paint failed: DisplayUnavailable: the display isn't taking frames"
    assert not any(line.startswith("  =") for line in lines)  # nothing went up
    sink.failing = False
    board.step(_at(T0 + 8 * S))
    assert lines[-2:] == ["paint recovered", "  = PUNT | C.Johnston punts 44 yards to CIN 18."]
    assert board.next_deadline(_at(T0 + 8 * S)) == MonotonicTime(28)  # its 20 s count from the frame that landed


def test_holds_each_reading_back_by_the_broadcast_delay() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(clock="13:09")), _scores(_row(clock="12:40"))]
    board, sink, _ = _board(feed, delay=timedelta(seconds=15))
    board.step(_at(T0))
    assert board.showing is None and sink.committed == 0
    board.step(_at(T0 + 10 * S))
    board.step(_at(T0 + 15 * S))
    assert board.showing is not None and board.showing.state is not None
    assert board.showing.state.clock_seconds == 789
    board.step(_at(T0 + 25 * S))
    assert board.showing.state is not None and board.showing.state.clock_seconds == 760


def test_clears_the_strip_when_the_field_moves_before_its_play_arrives() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_synced("p1")),
        _scores(
            _synced(
                "p2",
                "awaiting_play",
                situation={"possession_abbr": "CIN", "down": 3, "distance": 2, "yardline_100": 71},
            )
        ),
    ]
    feed.plays = [_page(_change("p1", 1, "C.Brown up the middle to CIN 29 for 4 yards.", yards_gained=4))]
    board, _, lines = _board(feed)
    for second in range(0, 31):
        board.step(_at(T0 + second * S))
    assert board._strip.cue(_at(T0 + 30 * S)) is None  # the run is stale beside 3rd & 2
    assert (
        "  x 4 YARD RUN | C.Brown up the middle to CIN 29 for 4 yards. (cleared: the field moved on without its play)"
        in lines
    )


def test_draws_halftime_then_holds_the_feed() -> None:
    feed = FakeFeed()
    halftime = _synced("p0", status="unknown", status_detail="Halftime", clock="0:00")
    feed.scores = [_scores(halftime), _scores(_row(period=3))]
    board, _, lines = _board(feed)
    board.step(_at(T0))
    showing = board.showing
    assert showing is not None and showing.state is not None and showing.state.interval is not None
    board.step(_at(T0 + timedelta(minutes=5)))
    assert board.showing is showing
    board.step(_at(T0 + timedelta(minutes=15)))
    assert board.showing is not None and board.showing.state is not None and board.showing.state.period == 3
    assert lines[1:3] == ["halftime: holding off the feed", "halftime hold over: polling again"]
    assert sum(path == "/api/v1/live-scores" for path, _ in feed.requests) == 2


def test_a_feed_failure_is_retried_later_and_a_market_failure_keeps_the_last_reading() -> None:
    feed = FakeFeed()
    feed.scores = [RuntimeError("down"), _scores(_row()), _scores(_row(clock="12:00"))]
    readings: list[Any] = [WinProbability(home=60.0, away=40.0), ProviderError("market down")]

    def market(game: TeamGame, now: datetime) -> WinProbability | None:
        answer = readings.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]

    board, _, lines = _board(feed, win_probability=market, meter_lag=timedelta(0))
    board.step(_at(T0))
    board.step(_at(T0 + 10 * S))
    assert len(feed.requests) == 1  # waits out the retry gap
    board.step(_at(T0 + 30 * S))
    board.step(_at(T0 + 40 * S))
    assert lines[0].startswith("feed: NFLMeta fetch failed")
    assert lines[2] == "market: market down"
    assert lines[3].endswith("meter 40-60")


def test_the_meter_trails_the_market_by_its_lag() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row())]
    prices = iter(range(50, 60))

    def market(game: TeamGame, now: datetime) -> WinProbability:
        price = float(next(prices))
        return WinProbability(home=price, away=100.0 - price)

    board, _, lines = _board(feed, win_probability=market, meter_lag=timedelta(seconds=15))
    for seconds in (0, 10, 20, 30, 40):
        board.step(_at(T0 + seconds * S))
    assert [line.rsplit("|", 1)[-1].strip() for line in lines] == [
        "TO 3-3 CH 2-2",  # nothing 15 seconds old yet
        "meter 50-50",  # at 20s: the price read at 0s
        "meter 49-51",  # at 30s: the price read at 10s
        "meter 48-52",
    ]


def test_logs_a_game_before_kickoff_and_at_a_break_without_drawing_the_first() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(phase="pre", period=0)), _scores(_row(status_detail="End of 1st", period=1))]
    board, sink, lines = _board(feed)
    board.step(_at(T0))
    assert sink.committed == 0
    board.step(_at(T0 + 10 * S))
    assert lines == ["PIT @ CIN: pre", "PIT @ CIN 0-10 Q1 13:09 [in] | end_of_quarter | TO 3-3 CH 2-2"]


def test_logs_a_live_game_with_no_down_to_describe() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(situation=None))]
    board, _, lines = _board(feed)
    board.step(_at(T0))
    assert lines == ["PIT @ CIN 0-10 Q2 13:09 [in] | TO 3-3 CH 2-2"]


def test_the_launcher_takes_a_game_and_a_display() -> None:
    args = _parse_args(["--profile", "quad_128x64", "--teams", "PIT,CIN", "--sink-command", "cat"])
    assert (args.teams, args.delay, args.meter_lag, args.fps, args.emulated) == ("PIT,CIN", 0, 15, 10.0, False)
    assert args.scroll_speed == "slow" and args.halftime_hold is None
    assert (args.odds, args.record, args.play_feed) == (False, False, "on-change")  # meter and recording opt in


def _period_ends(final_status: str, feed: FakeFeed) -> FootballBoard:
    walk_off = "C.Boswell 45 yard field goal is GOOD, Center-C.Kuntz."
    feed.scores = [
        _scores(_synced("p1", away_team={"abbr": "PIT", "score": 10})),
        _scores(
            _synced("p2", "awaiting_play", phase="post", status_detail="Final", away_team={"abbr": "PIT", "score": 13})
        ),
        _scores(
            _synced("p2", final_status, phase="post", status_detail="Final", away_team={"abbr": "PIT", "score": 13})
        ),
    ]
    feed.plays = [
        _page(
            _change(
                "p1",
                1,
                "A.Rodgers pass short right to J.Warren for 4 yards.",
                play_type="pass_reception",
                yards_gained=4,
            )
        ),
        _page(_change("p2", 2, walk_off, play_type="field_goal_good", field_goal_result="made", posteam_abbr="PIT")),
    ]
    board, _, _ = _board(feed)
    return board


def test_a_walk_off_shows_its_play_before_the_final() -> None:
    feed = FakeFeed()
    board = _period_ends("matched", feed)
    board.step(_at(T0))
    board.step(_at(T0 + 10 * S))  # the scoreboard says final, and the feed, read at once, has the kick
    assert board._closing is not None
    assert board.showing is not None and board.showing.state is not None and board.showing.state.interval is None
    cue = board._strip.cue(_at(T0 + 10 * S))
    assert cue is not None and cue.payload.headline == "FIELD GOAL GOOD"
    for second in range(11, 20):
        board.step(_at(T0 + second * S))
    assert board._closing is not None  # 10 seconds of the kick, still before FINAL
    board.step(_at(T0 + 20 * S))
    assert board._closing is None
    assert board.showing is not None and board.showing.state is not None
    assert board.showing.state.interval is FootballInterval.FINAL
    assert board._strip.cue(_at(T0 + 21 * S)) is None


def test_a_break_whose_play_never_lands_shows_after_a_wait() -> None:
    feed = FakeFeed()
    board = _period_ends("awaiting_play", feed)
    feed.plays = [feed.plays[0]]
    board.step(_at(T0))
    for second in range(10, 10 + int(CLOSING_WAIT.total_seconds())):
        board.step(_at(T0 + second * S))
    assert board._closing is not None
    board.step(_at(T0 + (10 + int(CLOSING_WAIT.total_seconds())) * S))
    assert board._closing is None


def test_a_break_that_ends_before_its_plays_are_shown_just_goes_on() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_synced("p1")),
        _scores(_synced("p2", "awaiting_play", status_detail="End of 1st", period=1)),
        _scores(_synced("p2", "awaiting_play", status_detail="End of 1st", period=1)),
        _scores(_synced("p3", "awaiting_play", period=2)),
    ]
    board, _, _ = _board(feed)
    for second in (0, 10, 20, 30):
        board.step(_at(T0 + second * S))
    assert board._closing is None
    assert board.showing is not None and board.showing.state is not None and board.showing.state.period == 2


def test_a_board_starting_in_a_break_keeps_the_strip_empty() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", status_detail="Halftime", clock="0:00"))]
    feed.plays = [_page(_change("p1", 1, "A.Rodgers kneels to PIT 21 for -1 yards.", yards_gained=-1))]
    board, _, lines = _board(feed)
    board.step(_at(T0))
    assert lines[1] == "  > KNEEL | A.Rodgers kneels to PIT 21 for -1 yards."  # the feed's latest, logged
    assert board._strip.cue(_at(T0)) is None  # but not shown over halftime


def test_logs_a_kickoff_with_its_spot() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", situation=None))]
    scored = "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN. E.McPherson extra point is GOOD."
    feed.plays = [
        _page(
            _change(
                "p1",
                1,
                scored,
                play_type="passing_touchdown",
                touchdown=True,
                extra_point_result="good",
                posteam_abbr="CIN",
            )
        )
    ]
    board, _, lines = _board(feed)
    board.step(_at(T0))
    assert lines[0] == "PIT @ CIN 0-10 Q2 13:09 [in] | kickoff, 65 to go | TO 3-3 CH 2-2"


def test_stops_polling_once_the_final_and_its_last_play_are_in() -> None:
    feed = FakeFeed()
    board = _period_ends("matched", feed)
    lines: list[str] = []
    board._log = lines.append
    for second in range(0, 10 + int(FINAL_DRAIN.total_seconds())):
        board.step(_at(T0 + second * S))
    assert "final: no more polling" not in lines  # still reading the feed for the final's last plays
    board.step(_at(T0 + 10 * S + FINAL_DRAIN))
    asked = len(feed.requests)
    assert "final: no more polling" in lines
    board.step(_at(T0 + timedelta(hours=1)))
    assert len(feed.requests) == asked


class _Cards:
    """Stands in for the renderer, keeping each card it's asked to draw."""

    def __init__(self) -> None:
        self.cards: list[Any] = []

    def render(self, card: Any, context: Any, canvas: Any) -> None:
        self.cards.append(card)


def test_a_halftime_hold_counts_down_on_the_card_on_the_broadcasts_clock() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0", status="unknown", status_detail="Halftime", clock="0:00"))]
    half_ended = T0 - timedelta(minutes=2)
    kneel = _change("p0", 1, "A.Rodgers kneels to PIT 21 for -1 yards.")
    feed.plays = [_page({**kneel, "changed_at": half_ended.isoformat()})]
    board, sink, lines = _board(feed, marquee=DEFAULT_MARQUEE.at(ScrollSpeed.FAST), delay=timedelta(seconds=30))
    cards = _Cards()
    board._renderer = cards  # type: ignore[assignment]
    board.step(_at(T0))
    board.step(_at(T0 + 30 * S))
    showing = board.showing
    assert showing is not None and showing.break_ends_at == half_ended + HALFTIME_HOLD
    assert cards.cards[-1].payload.break_ends_at == half_ended + HALFTIME_HOLD + timedelta(seconds=30)
    assert sink.committed == 1
    assert "  - KNEEL | A.Rodgers kneels to PIT 21 for -1 yards. (unseen: during a break)" in lines


def test_a_market_that_goes_quiet_takes_the_meter_away() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row())]
    answers = [WinProbability(home=60.0, away=40.0)]

    def market(game: TeamGame, now: datetime) -> WinProbability:
        if answers:
            return answers.pop(0)
        raise ProviderError("market down")

    board, _, lines = _board(feed, win_probability=market, meter_lag=timedelta(0))
    board.step(_at(T0))
    assert lines[0].endswith("meter 40-60")
    board.step(_at(T0 + METER_MAX_AGE))
    assert [line for line in lines if not line.startswith("market")][-1].endswith("meter 40-60")  # recent enough
    board.step(_at(T0 + METER_MAX_AGE + 10 * S))
    assert lines[-1] == "PIT @ CIN 0-10 Q2 13:09 [in] | home ball, 2&6, 75 to go | TO 3-3 CH 2-2"


def test_logs_the_feeds_own_trouble() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0")), _scores(_synced("p1"))]
    feed.plays = [_page(cursor="c1"), RuntimeError("timed out")]
    board, _, lines = _board(feed)
    board.step(_at(T0))
    board.step(_at(T0 + 10 * S))
    assert lines[-1] == "  ! play read failed: NFLMeta fetch failed for /api/v1/games/22920/play-changes: timed out"


def test_the_next_deadline_is_the_soonest_thing_the_board_would_do_by_itself() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(clock="13:09"))]
    delayed, _, _ = _board(feed, delay=timedelta(seconds=15))
    delayed.step(_at(T0))
    assert delayed.next_deadline(_at(T0)) == MonotonicTime(15.0)  # a reading clears the broadcast delay
    closing = _period_ends("matched", FakeFeed())
    closing.step(_at(T0))
    closing.step(_at(T0 + 10 * S))  # the final, with the walk-off kick on the strip
    assert closing._closing is not None
    assert closing.next_deadline(_at(T0 + 10 * S)) == MonotonicTime(20.0)  # the kick's minimum comes first
    feed = FakeFeed()
    waiting = _period_ends("awaiting_play", feed)
    feed.plays = [feed.plays[0]]  # the walk-off never lands
    waiting.step(_at(T0))
    waiting.step(_at(T0 + 10 * S))  # the final, its last play not in yet: the strip has nothing to show
    assert waiting._closing is not None and waiting._strip.idle
    assert waiting.next_deadline(_at(T0 + 10 * S)) == MonotonicTime(10.0) + CLOSING_WAIT


def test_the_log_names_an_entry_the_strip_dropped_unseen() -> None:
    board, _, lines = _board(FakeFeed())
    punt = Segment(
        key=SegmentKey(Surface.PLAY_STRIP, "x"),
        frames=(PlaySummary(tone=PlayTone.STANDARD, headline="PUNT"),),
        dwell=STRIP_DWELL,
        provenance=Provenance(NFLMETA_SOURCE, received_at=T0),
        available_at=T0,
    )
    board._trace(
        [
            LaneChange(ChangeKind.DROPPED, punt, _at(T0), reason=EndReason.EXPIRED),
            LaneChange(ChangeKind.DROPPED, punt, _at(T0)),
        ]
    )
    assert lines[-2:] == ["  - PUNT (unseen: expired)", "  - PUNT (unseen: dropped)"]


# Drawing through the frame stream: the strip counts a frame as shown only on the display's word.

PUNT = "PUNT | C.Johnston punts 44 yards to CIN 18."
_STRIP_LANE = TextLane(x=0, y=56, width=128, font="5x7")


class _Stream:
    """The frame stream to a display process, with the process played by the test.

    The board draws through a real `StreamDisplaySink` and `DisplayLink`. The writer keeps each frame
    handed to it as written to the current display process; the test says when a display process starts
    (and whether it acknowledges frames), which frame it shows, and when it goes away.
    """

    def __init__(self, *, now: Callable[[], float] = lambda: 0.0) -> None:
        self.link = DisplayLink(monotonic=now)
        self.frames: list[StreamFrame] = []
        self.sink = StreamDisplaySink(PanelProfile.QUAD_128X64, self._write, self.link)
        self.generation = 0

    def _write(self, frame: StreamFrame) -> bool:
        self.frames.append(frame)
        self.link.written(self.generation, frame.id)
        return True

    def start(self, *, acknowledges: bool = True) -> None:
        self.generation += 1
        self.link.started(self.generation)
        if acknowledges:
            self.link.ready(self.generation)

    def show_newest(self) -> None:
        self.link.shown(self.generation, self.frames[-1].id)

    def go_away(self) -> None:
        self.link.ended(self.generation)


class _Ticking:
    """Stands in for the renderer: each frame it draws differs from the last, and it notes how long the
    strip's line had been up in each one."""

    def __init__(self) -> None:
        self._drawn = 0
        self.strip_up: list[float | None] = []

    @property
    def supported_profiles(self) -> frozenset[PanelProfile]:
        return frozenset(PanelProfile)

    def render(self, card: ScoreboardCard[LiveFootballCardPayload], context: RenderContext, canvas: Canvas) -> None:
        canvas.set_pixel(self._drawn % canvas.width, self._drawn // canvas.width % canvas.height, RGBColor(255, 0, 0))
        self._drawn += 1
        play, clock = card.payload.last_play, context.marquee.clock
        if play is not None and clock is not None:
            self.strip_up.append(clock.elapsed(_STRIP_LANE, play.headline, overflows=True))


def _streamed(feed: FakeFeed, stream: _Stream) -> tuple[FootballBoard, _Ticking, list[str]]:
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=feed)
    lines: list[str] = []
    renderer = _Ticking()
    board = FootballBoard(GameFeed(provider, GAME), stream.sink, log=lines.append, renderer=renderer)
    return board, renderer, lines


def _punt() -> FakeFeed:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    feed.plays = [_page(_change("p1", 1, "C.Johnston punts 44 yards to CIN 18.", play_type="punt"))]
    return feed


def test_a_strip_frame_counts_its_time_from_when_the_display_shows_it_not_from_when_it_was_drawn() -> None:
    stream = _Stream()
    stream.start()
    board, _, lines = _streamed(_punt(), stream)
    board.step(_at(T0))  # drawn and handed over
    assert board.next_deadline(_at(T0)) is None  # the strip waits for the display's word
    first = stream.frames[0].id
    for second in range(1, 5):
        board.step(_at(T0 + second * S))
    assert not any(line.startswith("  =") for line in lines)  # handed over is not shown
    stream.link.shown(1, first)  # the display's word on the first frame comes 5 s after it was drawn
    board.step(_at(T0 + 5 * S))
    assert lines[-1] == f"  = {PUNT}"
    assert board.next_deadline(_at(T0 + 5 * S)) == MonotonicTime(25)  # its 20 s from the word, not the draw
    stream.link.shown(1, stream.frames[2].id)  # word on a frame drawn before: the dwell already runs
    board.step(_at(T0 + 6 * S))
    assert board.next_deadline(_at(T0 + 6 * S)) == MonotonicTime(25)


def test_the_strips_marquee_starts_when_the_display_shows_its_frame() -> None:
    stream = _Stream()
    stream.start()
    board, renderer, _ = _streamed(_punt(), stream)
    for second in range(0, 5):  # five seconds of frames the display doesn't confirm: each starts at the head
        board.step(_at(T0 + second * S))
    stream.link.shown(1, stream.frames[0].id)
    for second in range(5, 8):
        board.step(_at(T0 + second * S))
        stream.show_newest()
    assert renderer.strip_up == [0.0] * 6 + [1.0, 2.0]  # counted from when it was shown (5 s), not drawn (0 s)


def _two_plays() -> FakeFeed:
    feed = _punt()
    feed.scores = [_scores(_synced("p1")), _scores(_synced("p2"))]
    pass_play = _change("p2", 2, "J.Burrow pass short right to J.Chase for 9 yards.", play_type="pass_reception")
    feed.plays = [feed.plays[0], _page(pass_play)]
    return feed


def test_a_short_stall_keeps_the_caption_on_the_panel_on_its_time_and_credits_nothing_new() -> None:
    stream = _Stream()
    stream.start()
    board, _, lines = _streamed(_two_plays(), stream)
    board.step(_at(T0))
    stream.show_newest()
    board.step(_at(T0 + S))  # the punt is up, from now
    for second in range(2, 16):  # the display goes quiet: frames are handed over, and none is confirmed
        board.step(_at(T0 + second * S))
        if second == 10:  # the pass has arrived: the punt goes once it has had 10 s from when it went up
            assert board.next_deadline(_at(T0 + 10 * S)) == MonotonicTime(11)
    assert [line for line in lines if line.startswith("  =")] == [f"  = {PUNT}"]  # the pass isn't up yet
    assert board.next_deadline(_at(T0 + 15 * S)) is None  # it waits for the display
    stream.show_newest()
    board.step(_at(T0 + 16 * S))
    assert lines[-1] == "  = PASS | J.Burrow pass short right to J.Chase for 9 yards."
    assert board.next_deadline(_at(T0 + 16 * S)) == MonotonicTime(36)


def test_a_lost_display_takes_the_captions_time_back_and_a_new_one_gives_it_all_again() -> None:
    stream = _Stream()
    stream.start()
    board, _, lines = _streamed(_punt(), stream)
    board.step(_at(T0))
    stream.show_newest()
    board.step(_at(T0 + S))
    assert board.next_deadline(_at(T0 + S)) == MonotonicTime(21)
    board.step(_at(T0 + 5 * S))
    in_flight = stream.frames[-1].id  # handed over to the display ...
    stream.go_away()  # ... when it went away
    board.step(_at(T0 + 6 * S))
    assert lines[-1] == f"  ~ {PUNT} (blanked: the display went away)"
    assert board.next_deadline(_at(T0 + 6 * S)) is None  # no time runs on a blank panel
    stream.link.shown(1, in_flight)  # the old display's word, late: dropped
    stream.start()  # a new display process, its panel blank
    stream.link.shown(2, in_flight)  # it shows the old frame after all, but that frame was drawn for the old one
    board.step(_at(T0 + 8 * S))
    assert lines[-1] == f"  ~ {PUNT} (blanked: the display went away)" and board.next_deadline(_at(T0 + 8 * S)) is None
    stream.show_newest()  # a frame drawn since: current and complete
    board.step(_at(T0 + 9 * S))
    assert lines[-1] == f"  = {PUNT} (again, on the new display)"
    assert board.next_deadline(_at(T0 + 9 * S)) == MonotonicTime(29)  # the whole 20 s again, from the new word


def test_a_display_that_never_answers_is_credited_unconfirmed_after_a_few_seconds() -> None:
    now = [0.0]
    stream = _Stream(now=lambda: now[0])
    stream.start(acknowledges=False)  # a display process from before the acknowledgements
    board, _, lines = _streamed(_punt(), stream)
    for second in range(0, 5):
        now[0] = float(second)
        board.step(_at(T0 + second * S))
    assert not any(line.startswith("  =") or line.startswith("display:") for line in lines)
    for second in range(5, 7):
        now[0] = float(second)
        board.step(_at(T0 + second * S))
    assert lines[-2:] == [UNCONFIRMED_NOTE, f"  = {PUNT} (unconfirmed)"]  # the note, once
    assert board.next_deadline(_at(T0 + 6 * S)) == MonotonicTime(25)


def test_frames_the_display_never_shows_are_forgotten_after_a_while() -> None:
    stream = _Stream()
    stream.start()
    board, _, lines = _streamed(_punt(), stream)
    board.step(_at(T0))
    stale = stream.frames[0].id
    board.step(_at(T0 + UNSHOWN_AFTER))  # drawn too long ago to credit now
    stream.link.shown(1, stale)
    board.step(_at(T0 + UNSHOWN_AFTER + S))
    assert not any(line.startswith("  =") for line in lines)
    stream.show_newest()
    board.step(_at(T0 + UNSHOWN_AFTER + 2 * S))
    assert lines[-1] == f"  = {PUNT}"
