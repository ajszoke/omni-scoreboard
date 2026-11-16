"""The single-game football loop, its play strip, and the frame stream it can draw through."""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.display import RecordingDisplaySink
from omni.app.football import _parse_args
from omni.app.football_feed import GameFeed
from omni.app.football_board import (
    CLOSING_WAIT,
    FRAME_MAX,
    FRAME_MIN,
    METER_MAX_AGE,
    FootballBoard,
    PlayStrip,
)
from omni.app.frame_stream import FRAME_MAGIC, StreamDisplaySink, encode_frame
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.domain.football import FootballInterval, PlaySummary, PlayTone
from omni.renderers.text_lane import DEFAULT_MARQUEE, ScrollSpeed
from omni.events.football import FootballEvent, FootballEventKind
from omni.providers.base import ProviderError
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import FINAL_DRAIN, HALFTIME_HOLD, NFLMETA_SOURCE, NflMetaLiveProvider
from omni.renderers.canvas import RecordingCanvas
from tests.test_nflmeta import GAME, FakeFeed, _change, _page, _row, _scores, _synced

T0 = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


def _event(key: str, headline: str = "RUN +4", players: str | None = "C.Brown 4 YDS 1 CAR") -> FootballEvent:
    summary = PlaySummary(tone=PlayTone.STANDARD, headline=headline, detail=f"{headline} detail")
    return FootballEvent(key=key, kind=FootballEventKind.PLAY, summary=summary, players_line=players)


def _shown(strip: PlayStrip, at: datetime) -> tuple[str, str | None] | None:
    summary = strip.showing(at)
    return None if summary is None else (summary.headline, summary.detail)


def test_a_lone_event_shows_its_description_then_its_players_then_nothing() -> None:
    strip = PlayStrip()
    assert strip.showing(T0) is None
    strip.add([_event("1")])
    assert strip.showing(T0) == _event("1").summary
    assert _shown(strip, T0 + FRAME_MAX - S) == ("RUN +4", "RUN +4 detail")
    assert _shown(strip, T0 + FRAME_MAX) == ("RUN +4", "C.Brown 4 YDS 1 CAR")
    assert strip.showing(T0 + FRAME_MAX + FRAME_MAX) is None


def test_an_event_without_players_goes_blank_after_its_description() -> None:
    strip = PlayStrip()
    strip.add([_event("1", "TV TIMEOUT", players=None)])
    strip.showing(T0)
    assert strip.showing(T0 + FRAME_MAX) is None


def test_events_that_arrive_together_each_get_their_turn() -> None:
    strip = PlayStrip()
    strip.add([_event("1", "RUN +11"), _event("2", "PASS +53"), _event("1", "RUN +11")])
    assert _shown(strip, T0) == ("RUN +11", "RUN +11 detail")
    assert _shown(strip, T0 + FRAME_MIN - S) == ("RUN +11", "RUN +11 detail")
    later = T0 + FRAME_MIN
    assert _shown(strip, later) == ("PASS +53", "PASS +53 detail")  # one waiting: the minimum, then on
    assert _shown(strip, later + FRAME_MAX - S) == ("PASS +53", "PASS +53 detail")  # the last gets it all
    strip.add([_event("1", "RUN +11")])  # already shown once
    assert _shown(strip, later + FRAME_MAX + FRAME_MAX) is None


def test_a_field_change_clears_the_strip_once_its_event_has_had_its_minimum() -> None:
    strip = PlayStrip()
    strip.add([_event("1")])
    strip.showing(T0)
    strip.field_changed()
    assert _shown(strip, T0 + FRAME_MIN - S) == ("RUN +4", "RUN +4 detail")
    assert strip.showing(T0 + FRAME_MIN) is None
    strip.add([_event("2", "PASS +9")])
    assert _shown(strip, T0 + FRAME_MIN + S) == ("PASS +9", "PASS +9 detail")  # a fresh event isn't stale
    assert _shown(strip, T0 + FRAME_MIN + S + FRAME_MAX) == ("PASS +9", "C.Brown 4 YDS 1 CAR")


def _board(feed: FakeFeed, **kwargs: Any) -> tuple[FootballBoard, RecordingDisplaySink, list[str]]:
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=feed)
    lines: list[str] = []
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    source = GameFeed(provider, GAME, win_probability=kwargs.pop("win_probability", None))
    return FootballBoard(source, sink, log=lines.append, **kwargs), sink, lines


def test_polls_when_due_and_draws_every_step() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    feed.plays = [_page(_change("p1", 1, "C.Johnston punts 44 yards to CIN 18.", play_type="punt"))]
    board, sink, lines = _board(
        feed, win_probability=lambda game, now: WinProbability(home=83.0, away=17.0), meter_lag=timedelta(0)
    )
    board.step(T0)
    board.step(T0 + 5 * S)
    assert sum(path == "/api/v1/live-scores" for path, _ in feed.requests) == 1
    assert sink.committed == 2
    assert lines == [
        "PIT @ CIN 0-10 Q2 13:09 [in] | home ball, 2&6, 75 to go | TO 3-3 CH 2-2 | meter 17-83",
        "  > PUNT | C.Johnston punts 44 yards to CIN 18.",  # what arrived
        "  = PUNT | C.Johnston punts 44 yards to CIN 18.",  # and what the strip put up, apart
    ]
    board.step(T0 + 10 * S)
    assert sum(path == "/api/v1/live-scores" for path, _ in feed.requests) == 2
    assert len(lines) == 3  # nothing changed, nothing logged
    assert board.showing is not None


def test_holds_each_reading_back_by_the_broadcast_delay() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(clock="13:09")), _scores(_row(clock="12:40"))]
    board, sink, _ = _board(feed, delay=timedelta(seconds=15))
    board.step(T0)
    assert board.showing is None and sink.committed == 0
    board.step(T0 + 10 * S)
    board.step(T0 + 15 * S)
    assert board.showing is not None and board.showing.state is not None
    assert board.showing.state.clock_seconds == 789
    board.step(T0 + 25 * S)
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
    board, _, _ = _board(feed)
    for second in range(0, 31):
        board.step(T0 + second * S)
    assert board._strip.showing(T0 + 30 * S) is None  # the run is stale beside 3rd & 2


def test_draws_halftime_then_holds_the_feed() -> None:
    feed = FakeFeed()
    halftime = _synced("p0", status="unknown", status_detail="Halftime", clock="0:00")
    feed.scores = [_scores(halftime), _scores(_row(period=3))]
    board, _, lines = _board(feed)
    board.step(T0)
    showing = board.showing
    assert showing is not None and showing.state is not None and showing.state.interval is not None
    board.step(T0 + timedelta(minutes=5))
    assert board.showing is showing
    board.step(T0 + timedelta(minutes=15))
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
    board.step(T0)
    board.step(T0 + 10 * S)
    assert len(feed.requests) == 1  # waits out the retry gap
    board.step(T0 + 30 * S)
    board.step(T0 + 40 * S)
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
        board.step(T0 + seconds * S)
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
    board.step(T0)
    assert sink.committed == 0
    board.step(T0 + 10 * S)
    assert lines == ["PIT @ CIN: pre", "PIT @ CIN 0-10 Q1 13:09 [in] | end_of_quarter | TO 3-3 CH 2-2"]


def test_logs_a_live_game_with_no_down_to_describe() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(situation=None))]
    board, _, lines = _board(feed)
    board.step(T0)
    assert lines == ["PIT @ CIN 0-10 Q2 13:09 [in] | TO 3-3 CH 2-2"]


def test_streams_each_new_frame_once() -> None:
    sent: list[bytes] = []
    delivered = [True, False, True]

    def write(frame: bytes) -> bool:
        sent.append(frame)
        return delivered.pop(0)

    sink = StreamDisplaySink(PanelProfile.SINGLE_64X32, write)
    assert sink.profile is PanelProfile.SINGLE_64X32
    for _ in range(2):
        sink.commit(sink.new_frame())
    assert len(sent) == 1
    assert sent[0][:4] == FRAME_MAGIC and struct.unpack("<HH", sent[0][4:8]) == (64, 32)
    assert len(sent[0]) == 8 + 64 * 32 * 3
    lit = sink.new_frame()
    lit.set_pixel(0, 0, RGBColor(255, 0, 0))
    sink.commit(lit)  # not delivered, so the same frame goes again
    sink.commit(lit)
    assert len(sent) == 3 and sent[1] == sent[2]
    with pytest.raises(TypeError):
        sink.commit(RecordingCanvas(64, 32))
    with pytest.raises(ValueError):
        encode_frame(2, 2, b"\x00")


def test_the_launcher_takes_a_game_and_a_display() -> None:
    args = _parse_args(["--profile", "quad_128x64", "--teams", "PIT,CIN", "--sink-command", "cat"])
    assert (args.teams, args.delay, args.meter_lag, args.fps, args.emulated) == ("PIT,CIN", 0, 15, 10.0, False)
    assert args.scroll_speed == "slow" and args.halftime_hold is None
    assert (args.odds, args.record, args.play_feed) == (False, False, "on-change")  # meter and recording opt in


def test_closing_out_gives_each_event_its_minimum_and_no_players_lines() -> None:
    strip = PlayStrip()
    strip.add([_event("1", "KNEEL"), _event("2", "TV TIMEOUT")])
    strip.showing(T0)
    strip.close_out()
    assert _shown(strip, T0 + FRAME_MIN - S) == ("KNEEL", "KNEEL detail")
    assert _shown(strip, T0 + FRAME_MIN) == ("TV TIMEOUT", "TV TIMEOUT detail")
    assert not strip.idle
    assert strip.showing(T0 + 2 * FRAME_MIN) is None and strip.idle
    strip.resume()
    strip.add([_event("3")])
    strip.showing(T0 + 3 * FRAME_MIN)
    assert _shown(strip, T0 + 3 * FRAME_MIN + FRAME_MAX) == ("RUN +4", "C.Brown 4 YDS 1 CAR")
    strip.clear()
    assert strip.showing(T0 + 4 * FRAME_MIN) is None


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
    board.step(T0)
    board.step(T0 + 10 * S)  # the scoreboard says final, and the feed, read at once, has the kick
    assert board._closing is not None
    assert board.showing is not None and board.showing.state is not None and board.showing.state.interval is None
    lines = board._strip.showing(T0 + 10 * S)
    assert lines is not None and lines.headline == "FIELD GOAL GOOD"
    for second in range(11, 20):
        board.step(T0 + second * S)
    assert board._closing is not None  # 10 seconds of the kick, still before FINAL
    board.step(T0 + 20 * S)
    assert board._closing is None
    assert board.showing is not None and board.showing.state is not None
    assert board.showing.state.interval is FootballInterval.FINAL
    assert board._strip.showing(T0 + 21 * S) is None


def test_a_break_whose_play_never_lands_shows_after_a_wait() -> None:
    feed = FakeFeed()
    board = _period_ends("awaiting_play", feed)
    feed.plays = [feed.plays[0]]
    board.step(T0)
    for second in range(10, 10 + int(CLOSING_WAIT.total_seconds())):
        board.step(T0 + second * S)
    assert board._closing is not None
    board.step(T0 + (10 + int(CLOSING_WAIT.total_seconds())) * S)
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
        board.step(T0 + second * S)
    assert board._closing is None
    assert board.showing is not None and board.showing.state is not None and board.showing.state.period == 2


def test_a_board_starting_in_a_break_keeps_the_strip_empty() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", status_detail="Halftime", clock="0:00"))]
    feed.plays = [_page(_change("p1", 1, "A.Rodgers kneels to PIT 21 for -1 yards.", yards_gained=-1))]
    board, _, lines = _board(feed)
    board.step(T0)
    assert lines[1] == "  > KNEEL | A.Rodgers kneels to PIT 21 for -1 yards."  # the feed's latest, logged
    assert board._strip.showing(T0) is None  # but not shown over halftime


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
    board.step(T0)
    assert lines[0] == "PIT @ CIN 0-10 Q2 13:09 [in] | kickoff, 65 to go | TO 3-3 CH 2-2"


def test_stops_polling_once_the_final_and_its_last_play_are_in() -> None:
    feed = FakeFeed()
    board = _period_ends("matched", feed)
    lines: list[str] = []
    board._log = lines.append
    for second in range(0, 10 + int(FINAL_DRAIN.total_seconds())):
        board.step(T0 + second * S)
    assert "final: no more polling" not in lines  # still reading the feed for the final's last plays
    board.step(T0 + 10 * S + FINAL_DRAIN)
    asked = len(feed.requests)
    assert "final: no more polling" in lines
    board.step(T0 + timedelta(hours=1))
    assert len(feed.requests) == asked


def _stoppage(key: str, kind: FootballEventKind, headline: str) -> FootballEvent:
    return FootballEvent(key=key, kind=kind, summary=PlaySummary(tone=PlayTone.STANDARD, headline=headline))


def test_a_stoppage_stays_up_until_play_resumes() -> None:
    strip = PlayStrip()
    strip.add([_stoppage("t", FootballEventKind.TV_TIMEOUT, "TV TIMEOUT")])
    strip.showing(T0)
    assert _shown(strip, T0 + timedelta(minutes=3)) == ("TV TIMEOUT", None)  # however long the break runs
    strip.field_changed()  # the next snap
    assert strip.showing(T0 + timedelta(minutes=3)) is None
    strip.add([_stoppage("i", FootballEventKind.INJURY, "INJURY CIN"), _event("2")])
    strip.showing(T0 + timedelta(minutes=4))
    assert _shown(strip, T0 + timedelta(minutes=4) + FRAME_MIN) == ("RUN +4", "RUN +4 detail")  # superseded
    assert FootballEventKind.TV_TIMEOUT.spans and not FootballEventKind.PLAY.spans


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
    board.step(T0)
    board.step(T0 + 30 * S)
    showing = board.showing
    assert showing is not None and showing.break_ends_at == half_ended + HALFTIME_HOLD
    assert cards.cards[-1].payload.break_ends_at == half_ended + HALFTIME_HOLD + timedelta(seconds=30)
    assert sink.committed == 1
    assert "  - KNEEL | A.Rodgers kneels to PIT 21 for -1 yards. (unseen: during a break)" in lines


def test_each_frame_gets_its_own_minimum() -> None:
    strip = PlayStrip()
    strip.add([_event("1")])
    strip.showing(T0)
    assert _shown(strip, T0 + FRAME_MAX) == ("RUN +4", "C.Brown 4 YDS 1 CAR")  # the players' frame starts
    strip.field_changed()  # a second later the field moves on
    assert _shown(strip, T0 + FRAME_MAX + FRAME_MIN - S) == ("RUN +4", "C.Brown 4 YDS 1 CAR")  # its own 10s
    assert strip.showing(T0 + FRAME_MAX + FRAME_MIN) is None  # then blank, waiting for the play


def test_a_play_waiting_when_a_description_has_had_its_minimum_skips_the_players_frame() -> None:
    strip = PlayStrip()
    strip.add([_event("1")])
    strip.showing(T0)
    strip.add([_event("2", "PASS +9")])
    assert _shown(strip, T0 + FRAME_MIN) == ("PASS +9", "PASS +9 detail")  # straight up, no blank


def test_a_market_that_goes_quiet_takes_the_meter_away() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row())]
    answers = [WinProbability(home=60.0, away=40.0)]

    def market(game: TeamGame, now: datetime) -> WinProbability:
        if answers:
            return answers.pop(0)
        raise ProviderError("market down")

    board, _, lines = _board(feed, win_probability=market, meter_lag=timedelta(0))
    board.step(T0)
    assert lines[0].endswith("meter 40-60")
    board.step(T0 + METER_MAX_AGE)
    assert [line for line in lines if not line.startswith("market")][-1].endswith("meter 40-60")  # recent enough
    board.step(T0 + METER_MAX_AGE + 10 * S)
    assert lines[-1] == "PIT @ CIN 0-10 Q2 13:09 [in] | home ball, 2&6, 75 to go | TO 3-3 CH 2-2"


def test_logs_the_feeds_own_trouble() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0")), _scores(_synced("p1"))]
    feed.plays = [_page(cursor="c1"), RuntimeError("timed out")]
    board, _, lines = _board(feed)
    board.step(T0)
    board.step(T0 + 10 * S)
    assert lines[-1] == "  ! play read failed: NFLMeta fetch failed for /api/v1/games/22920/play-changes: timed out"


def test_a_new_display_process_gets_the_frame_again() -> None:
    sent: list[bytes] = []
    generation = [1]

    def write(frame: bytes) -> bool:
        sent.append(frame)
        return True

    sink = StreamDisplaySink(PanelProfile.SINGLE_64X32, write, lambda: generation[0])
    sink.commit(sink.new_frame())
    sink.commit(sink.new_frame())
    assert len(sent) == 1
    generation[0] = 2  # the display process was restarted, and its panel starts blank
    sink.commit(sink.new_frame())
    sink.commit(sink.new_frame())
    assert len(sent) == 2
