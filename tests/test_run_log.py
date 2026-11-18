"""A run's log: our own telemetry to stdout, and the provider's words only in a run folder stamped to expire."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omni.app.display import RecordingDisplaySink
from omni.app.football import _LOG_DIR, _parse_args, open_run_log
from omni.app.football_board import FootballBoard
from omni.app.football_feed import GameFeed
from omni.app.run_log import RUN_LOG_FILE, LogLine, RunLog, print_telemetry
from omni.core.clock import FakeClock
from omni.core.enum import PanelProfile
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from omni.providers.rights import RETENTION_FILE, DataSource, read_stamp
from tests.test_nflmeta import GAME, T0, FakeFeed, _change, _page, _scores, _synced

S = timedelta(seconds=1)
PHRASE = "ZEBRA CROSSING"  # a phrase only the feed words, which stdout must never carry


def test_a_line_s_telemetry_is_printed_and_the_whole_line_kept_in_the_stamped_run_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "2026-11-16-PIT-CIN"
    printed: list[str] = []
    log = RunLog(folder, DataSource.NFLMETA, T0, out=printed.append)
    log(LogLine("  = routine play #7", f"  = RUSH +4 | J.Doe {PHRASE}"))
    log("feed answering again", at=T0)
    log.close()
    stamp = f"{T0.astimezone():%H:%M:%S}  "
    assert printed == ["  = routine play #7", f"{stamp}feed answering again"]
    assert (folder / RUN_LOG_FILE).read_text().splitlines() == [
        f"  = RUSH +4 | J.Doe {PHRASE}",
        f"{stamp}feed answering again",
    ]
    assert read_stamp(folder) == log.stamp and log.stamp.delete_by == T0 + timedelta(days=7)
    assert log.path == folder / RUN_LOG_FILE
    print_telemetry(LogLine("  ! late play", f"  ! late play: {PHRASE}"))
    assert capsys.readouterr().out == "  ! late play\n"
    assert LogLine("paint recovered").full == "paint recovered"


def test_the_runner_keeps_its_logs_under_build_logs_nflmeta_unless_told_otherwise() -> None:
    teams = ["--profile", "quad_128x64", "--teams", "PIT,CIN", "--emulated"]
    assert _parse_args(teams).log_dir == _LOG_DIR == Path(__file__).resolve().parents[1] / "build" / "logs" / "nflmeta"
    assert _parse_args(teams + ["--log-dir", "/srv/logs"]).log_dir == Path("/srv/logs")


def _worded_feed() -> FakeFeed:
    """A game whose feed words its plays, an injury, a failed read and a failed scoreboard with ``PHRASE``."""
    hurt = {"player_name": "A. Player", "team_abbr": "PIT", "injury": f"{PHRASE} knee"}
    feed = FakeFeed()
    feed.scores = [
        _scores(_synced("p1", injuries=[{**hurt, "state": "questionable_to_return"}])),
        _scores(_synced("p2", injuries=[{**hurt, "state": "out"}])),  # the injury's news: its status changed
        RuntimeError(f"{PHRASE} scoreboard"),
        _scores(_synced("p2", injuries=[{**hurt, "state": "out"}])),
    ]
    feed.plays = [
        _page(_change("p1", 1, f"C.Johnston punts 44 yards past the {PHRASE}.", play_type="punt")),
        RuntimeError(f"{PHRASE} in a failed read"),  # a note from the feed, quoting what failed
        _page(_change("p2", 2, f"J.Doe runs to the {PHRASE} for 4 yards.")),
    ]
    return feed


def test_the_runner_s_board_never_prints_the_feed_s_words_and_keeps_them_in_its_stamped_run_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _parse_args(["--profile", "quad_128x64", "--teams", "PIT,CIN", "--emulated", "--log-dir", str(tmp_path)])
    log = open_run_log(args.log_dir, "PIT", "CIN", T0)  # telemetry to the real stdout
    clock = FakeClock(T0)
    provider = NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=_worded_feed())
    board = FootballBoard(GameFeed(provider, GAME), RecordingDisplaySink(PanelProfile.QUAD_128X64), log=log)
    for _ in range(90):
        board.step(clock.read())
        clock.advance(S)
    log.close()
    printed = capsys.readouterr().out
    folder = tmp_path / f"{T0.astimezone():%Y-%m-%d}-PIT-CIN"
    kept = (folder / RUN_LOG_FILE).read_text()
    assert PHRASE not in printed
    for told in ("  = routine play #1", "  > routine injury", "  ! play read failed", "feed: the ask failed"):
        assert told in printed.splitlines()  # what kind of thing each line was, in its place
    for quoted in (
        f"  = PUNT | C.Johnston punts 44 yards past the {PHRASE}.",
        f"  > INJURY PIT | A. Player ({PHRASE} knee), out",
        f"{PHRASE} in a failed read",
        f"feed: NFLMeta fetch failed for /api/v1/live-scores: {PHRASE} scoreboard",
        f"J.Doe runs to the {PHRASE} for 4 yards.",
    ):
        assert quoted in kept
    assert json.loads((folder / RETENTION_FILE).read_text())["source"] == "nflmeta"
    assert [path.name for path in tmp_path.iterdir()] == [folder.name]  # nothing lands outside the stamped folder
