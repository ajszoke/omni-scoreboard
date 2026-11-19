"""Mining the NFL runner's log: each night's runs, strip, feed, display and clock, counted without their words.

The logs here are synthetic, written line for line in the shapes the runner printed on each night: the first
night's (`omni/app/football.py` and `football_board.py` at 176fd69) and the second's (the same files with
`football_feed.py` and `frame_stream.py` at bfd123a). Every free-text part carries `PHRASE`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from omni.telemetry.digest import check_digest
from omni.telemetry.runner_log_mine import MEASURES, allowed, mine_log, mine_runner_log, summary
from tests.nflmeta_recording import PHRASE

FIRST = date(2026, 11, 15)
DISPLAY = "ssh omni-board sudo python3 omni/board_frame_sink.py"

NIGHT_ONE = [
    f"a line before any run: {PHRASE}",
    "omni.app.football: PIT @ CIN (game 22920), quad_128x64, delay 0s",
    f"display: starting {DISPLAY}",
    "18:20:00  PIT @ CIN: pre",
    "18:25:10  PIT @ CIN 0-0 Q1 15:00 [in] | away ball, 1&10, 75 to go | TO 3-3 CH 2-2 | meter 40-60",
    f"18:25:10    > KICKOFF | {PHRASE} kicks 65 yards",
    f"18:25:11    = KICKOFF | {PHRASE} kicks 65 yards",
    "18:26:00  PIT @ CIN 0-0 Q1 14:30 [in] | away ball, 2&6, 71 to go | TO 3-3 CH 2-2",
    "18:26:00    > TIMEOUT CIN | CIN #1 at 14:30",
    "18:26:01    = TIMEOUT CIN | CIN #1 at 14:30",
    "18:28:00    > TV TIMEOUT",
    "18:28:01    = TV TIMEOUT",
    f"18:30:00    ! late play: 4 YARD RUN | {PHRASE}",
    f"18:31:00  feed: NFLMeta fetch failed for /api/v1/games/22920/play-changes: HTTP Error 503: {PHRASE}",
    f"18:31:30  market: {PHRASE}",
    "18:35:00  PIT @ CIN 0-0 Q1 0:00 [in] | end_of_quarter | TO 3-3 CH 2-2",
    f"18:35:00    - 4 YARD RUN | {PHRASE} (J.Doe) (unseen: the break began)",
    "18:38:00  PIT @ CIN 7-0 Q2 0:00 [in] | halftime",
    f"18:38:00    - XP GOOD | {PHRASE} (unseen: during a break)",
    "18:38:31  halftime: holding off the feed",
    "18:51:31  halftime hold over: polling again",
    "18:51:31  PIT @ CIN 7-0 Q3 15:00 [in] | kickoff, 65 to go",
    "display: lost the display process; restarting shortly",
    f"display: starting {DISPLAY}",
    "Traceback (most recent call last):",
    f'  File "{PHRASE}.py", line 1, in <module>',
    "KeyboardInterrupt",
    "omni.app.football: PIT @ CIN (game 22920), quad_128x64, delay 0s",
    f"display: starting {DISPLAY}",
    "19:05:00  PIT @ CIN 7-0 Q3 10:00 [in]",
    "19:06:00  PIT @ CIN 7-3 Q4 0:00 [post] | final",
    "19:08:00  final: no more polling",
]
NIGHT_TWO = [
    "omni.app.football: LAC @ BAL (game 22921), stack_64x64, delay 30s",
    f"18:10:00  display: starting {DISPLAY}",
    "18:10:02  LAC @ BAL: pre",
    "18:15:00  LAC @ BAL 0-0 Q1 15:00 [in]",
    "18:15:00    > KICKOFF",
    "18:15:01    = KICKOFF (unconfirmed)",
    "18:15:01  display: no frame acknowledgements; showing frames unconfirmed (update board_frame_sink.py on the board)",
    "18:16:00    > TIMEOUT BLT | BLT #1 at 14:00",
    "18:16:00    = TIMEOUT BLT | BLT #1 at 14:00",
    "18:16:20  display: the display process ended; restarting shortly",
    f"18:16:25  display: starting {DISPLAY}",
    "18:16:26    ~ TIMEOUT BLT | BLT #1 at 14:00 (blanked: the display went away)",
    "18:16:27    = TIMEOUT BLT | BLT #1 at 14:00 (again, on the new display)",
    f"18:17:00    > 3 YARD RUN | {PHRASE}",
    f"18:17:01    = 3 YARD RUN | {PHRASE}",
    f"18:17:30    x 3 YARD RUN | {PHRASE} (cleared: the field moved on without its play)",
    f"18:18:00    > LAC CHALLENGE: STANDS | {PHRASE}",
    f"18:18:00    - LAC CHALLENGE: STANDS | {PHRASE} (unseen: too late to start)",
    "18:18:30    > LAC +7 | Score update; play not reported",
    "18:18:31    = LAC +7 | Score update; play not reported",
    "18:19:00  feed stale: no answer; dimming the card until it answers",
    "18:19:00    x LAC +7 | Score update; play not reported (cleared: its source went stale)",
    "18:20:10  feed answering again",
    "18:20:10  LAC @ BAL 7-0 Q1 10:00 [in]",
    "18:20:20  feed: quota: NFLMeta 20 of 20 a minute used, focus waiting",
    "18:20:30  feed: NFLMeta no longer lists game 22921",
    f"18:20:31  feed: NFLMeta sent an unreadable game 22921: {PHRASE}",
    f"18:20:32  feed: TimeoutError: {PHRASE}",
    f"18:20:33  feed: NFLMeta fetch failed for /api/v1/live-scores: <urlopen error {PHRASE}>",
    f"18:21:00  paint failed: ValueError: {PHRASE}",
    "18:21:01  paint recovered",
    f"18:21:02  strip: a caption failed to draw: ValueError: {PHRASE}",
    f"step failed: RuntimeError: {PHRASE}",
    f"18:21:05  display: couldn't start it (OSError: {PHRASE}); trying again shortly",
    f"18:21:06  display: a frame write failed: BrokenPipeError: {PHRASE}",
    "18:22:00    ! matched play missing: not in the feed yet: reading again next poll",
    f"18:22:01    ! a note a later runner adds: {PHRASE}",
    f"18:22:02    ! final plays incomplete: {PHRASE}",
    f"18:22:03    > INJURY BAL | {PHRASE}, questionable to return",
    f"18:22:04    > XP NO GOOD | {PHRASE}",
    "18:22:05    > TWO-MINUTE WARNING",
    f"18:22:06    > REVIEW: REVERSED | {PHRASE}",
    f"18:22:07    - FLAG BAL: HOLDING | {PHRASE} (unseen: {PHRASE})",
    "18:22:07    ~ TIMEOUT BLT",
    f"18:22:08    - 2-PT FAILS | {PHRASE}",
    "18:30:00  LAC @ BAL 7-0 Q1 9:00 [in]",  # nearly eight minutes with nothing logged, mid-game
    "18:29:00  LAC @ BAL 7-0 Q1 8:30 [in]",  # the wall clock stepped back a minute
    "18:31:00  LAC @ BAL 7-0 Q4 0:00 [post] | final",
    "18:33:00  final: no more polling",
]
NIGHT_THREE = [  # a run across midnight
    "omni.app.football: CAR @ ATL (game 3), single_64x32, delay 0s",
    "23:59:00  CAR @ ATL: pre",
    "23:59:30  halftime: holding off the feed",
    "00:00:30  halftime hold over: polling again",
]


def _spread(count: int, *values: float) -> dict[str, Any]:
    least, p25, median, p75, most = values if values else (None,) * 5
    return {"count": count, "min": least, "p25": p25, "median": median, "p75": p75, "max": most}


def _same(value: float) -> dict[str, Any]:
    return _spread(1, value, value, value, value, value)


def _counts(*rows: tuple[Any, ...], keys: tuple[str, ...] = ("kind",)) -> list[dict[str, Any]]:
    return [{**dict(zip(keys, row[:-1])), "count": row[-1]} for row in rows]


@pytest.fixture
def nights() -> list[dict[str, Any]]:
    return mine_log(NIGHT_ONE + NIGHT_TWO + NIGHT_THREE, FIRST)


def test_runs_are_grouped_into_nights_and_dated_from_the_first(nights: list[dict[str, Any]]) -> None:
    assert [(night["date"], night["away"], night["home"], night["runs"]) for night in nights] == [
        ("2026-11-15", "PIT", "CIN", 2),
        ("2026-11-16", "LAC", "BAL", 1),
        ("2026-11-17", "CAR", "ATL", 1),
    ]
    assert [night["formats"] for night in nights] == [
        ["nfl runner 2026-11-15"],
        ["nfl runner 2026-11-16"],
        ["nfl runner 2026-11-15"],
    ]
    one, two, _ = nights
    assert one["run_rows"] == [
        {"away": "PIT", "home": "CIN", "profile": "quad_128x64", "delay": 0, "wall": 1891.0, "lines": 26},
        {"away": "PIT", "home": "CIN", "profile": "quad_128x64", "delay": 0, "wall": 180.0, "lines": 5},
    ]
    assert one["lines"] == {"parsed": 28, "unparsed": 4}  # the line before any run, and a traceback
    assert two["run_rows"] == [
        {"away": "LAC", "home": "BAL", "profile": "stack_64x64", "delay": 30, "wall": 1380.0, "lines": len(NIGHT_TWO)}
    ]
    assert two["lines"] == {"parsed": len(NIGHT_TWO), "unparsed": 0}


def test_the_first_nights_strip_feed_and_halftime(nights: list[dict[str, Any]]) -> None:
    one = nights[0]
    assert one["strip"] == {
        "arrived": _counts(("official_timeout", 1), ("play", 1), ("timeout", 1)),
        "shown": _counts(("official_timeout", 1), ("play", 1), ("timeout", 1)),
        "shown_again": [],
        "dropped": _counts(
            ("conversion", "during a break", 1), ("play", "the break began", 1), keys=("kind", "reason")
        ),
        "cleared": [],
        "blanked": [],
    }
    assert one["frames"] == {"logged": False, "shown_confirmed": 3, "shown_unconfirmed": 0, "unconfirmed_notes": 0}
    assert one["feed"] == {
        "answer_lines": 8,
        "answer_gap": _spread(6, 50.0, 90.0, 245.0, 482.5, 811.0),
        "errors": _counts(("fetch failed", "play-changes", 503, 1), keys=("kind", "endpoint", "status")),
        "refusals": 0,
        "notes": _counts(("late play", 1)),
        "other_notes": 0,
        "stale_spells": 0,
        "stale": _spread(0),
        "market_errors": 1,
    }
    assert one["display"] == {"starts": 3, "start_failures": 0, "drops": 1, "write_failures": 0}
    assert one["halftime"] == {"holds": [{"start": 67111, "end": 67891, "length": 780.0}]}
    assert one["clock"] == {"backward_steps": 0, "backward": _spread(0), "silences": 0, "silence": _spread(0)}
    assert one["final"] == {"parked": 1, "drain": _same(120.0)}
    assert one["failures"] == {"step": 0, "paint": 0, "paint_recovered": 0, "caption": 0}


def test_the_second_nights_acknowledgements_clears_failures_and_clock(nights: list[dict[str, Any]]) -> None:
    two = nights[1]
    assert two["strip"] == {
        "arrived": _counts(
            ("conversion", 1),
            ("injury", 1),
            ("play", 2),
            ("review", 2),
            ("score", 1),
            ("timeout", 1),
            ("two_minute_warning", 1),
        ),
        "shown": _counts(("play", 2), ("score", 1), ("timeout", 1)),
        "shown_again": _counts(("timeout", 1)),
        "dropped": _counts(
            ("conversion", "other", 1),
            ("play", "other", 1),
            ("review", "too late to start", 1),
            keys=("kind", "reason"),
        ),
        "cleared": _counts(
            ("play", "the field moved on without its play", 1),
            ("score", "its source went stale", 1),
            keys=("kind", "reason"),
        ),
        "blanked": _counts(("timeout", 2)),
    }
    assert two["frames"] == {"logged": False, "shown_confirmed": 4, "shown_unconfirmed": 1, "unconfirmed_notes": 1}
    feed = two["feed"]
    assert feed["answer_lines"] == 6
    assert feed["answer_gap"] == _spread(4, 120.0, 253.5, 304.0, 380.0, 590.0)  # a step back isn't a gap
    assert feed["errors"] == _counts(
        ("fetch failed", "live-scores", None, 1),
        ("game not listed", None, None, 1),
        ("other", None, None, 1),
        ("refused by the quota", None, None, 1),
        ("unreadable game", None, None, 1),
        keys=("kind", "endpoint", "status"),
    )
    assert feed["refusals"] == 1
    assert feed["notes"] == _counts(("final plays incomplete", 1), ("matched play missing", 1))
    assert feed["other_notes"] == 1
    assert (feed["stale_spells"], feed["stale"]) == (1, _same(70.0))
    assert two["display"] == {"starts": 2, "start_failures": 1, "drops": 1, "write_failures": 1}
    assert two["halftime"] == {"holds": []}
    assert two["clock"] == {
        "backward_steps": 1,
        "backward": _same(60.0),
        "silences": 1,
        "silence": _same(472.0),
    }
    assert two["final"] == {"parked": 1, "drain": _same(120.0)}
    assert two["failures"] == {"step": 1, "paint": 1, "paint_recovered": 1, "caption": 1}


def test_a_run_across_midnight_keeps_its_hold_whole(nights: list[dict[str, Any]]) -> None:
    three = nights[2]
    assert three["halftime"] == {"holds": [{"start": 86370, "end": 86430, "length": 60.0}]}
    assert three["run_rows"][0]["wall"] == 90.0
    assert three["clock"]["backward_steps"] == 0


def test_a_restart_of_the_same_game_the_next_evening_is_the_next_night() -> None:
    log = [
        "omni.app.football: PIT @ CIN (game 1), quad_128x64, delay 0s",
        "20:00:00  PIT @ CIN: pre",
        "omni.app.football: PIT @ CIN (game 1), quad_128x64, delay 0s",
        "20:30:00  PIT @ CIN: pre",
        "omni.app.football: PIT @ CIN (game 1), quad_128x64, delay 0s",
        "19:00:00  PIT @ CIN: pre",
    ]
    assert [(night["date"], night["runs"]) for night in mine_log(log, FIRST)] == [
        ("2026-11-15", 2),
        ("2026-11-16", 1),
    ]


def test_a_start_line_naming_no_club_or_panel_keeps_neither() -> None:
    log = ["omni.app.football: XYZ @ CIN (game 1), giant, delay 5s", "20:00:00  XYZ @ CIN: pre"]
    (night,) = mine_log(log, FIRST)
    assert (night["away"], night["home"]) == (None, None)
    assert night["run_rows"] == [{"away": None, "home": None, "profile": None, "delay": 5, "wall": None, "lines": 2}]
    assert "- @ -" in summary(night)


def test_a_log_with_no_run_is_one_night_of_unread_lines() -> None:
    (night,) = mine_log([f"{PHRASE}", "12:00:00  something else"], FIRST)
    assert (night["runs"], night["lines"], night["away"]) == (0, {"parsed": 0, "unparsed": 2}, None)


def test_no_log_words_reach_a_digest_or_its_summary(tmp_path: Path) -> None:
    log = tmp_path / "football-live.log"
    log.write_text("\n".join(NIGHT_ONE + NIGHT_TWO) + "\n")
    paths = mine_runner_log(log, tmp_path / "out", FIRST)
    assert paths == [tmp_path / "out" / "2026-11-15.json", tmp_path / "out" / "2026-11-16.json"]
    for path in paths:
        written = path.read_text()
        words = path.with_suffix(".md").read_text()
        for text in (written, words):
            assert PHRASE not in text and "Quokka" not in text and DISPLAY not in text and "J.Doe" not in text
        check_digest(json.loads(written), measures=MEASURES, values=allowed(json.loads(written)))
    assert "Halftime holds: 780" in paths[0].with_suffix(".md").read_text()


def test_a_night_already_mined_is_not_mined_again(tmp_path: Path) -> None:
    log = tmp_path / "football-live.log"
    log.write_text("\n".join(NIGHT_ONE) + "\n")
    (path,) = mine_runner_log(log, tmp_path / "out", FIRST)
    path.write_text("kept as it was\n")
    assert mine_runner_log(log, tmp_path / "out", FIRST) == [path]
    assert path.read_text() == "kept as it was\n"


def test_a_long_drain_after_the_final_is_measured_as_the_drain_not_a_silence() -> None:
    log = [
        "omni.app.football: LAC @ BAL (game 1), quad_128x64, delay 0s",
        "21:00:00  LAC @ BAL 20-17 Q4 0:05 [in]",
        "21:01:00  LAC @ BAL 20-17 Q4 0:00 [post] | final",
        "21:16:00  final: no more polling",
    ]
    (night,) = mine_log(log, FIRST)
    assert night["clock"]["silences"] == 0
    assert night["final"]["drain"] == _same(900.0)
