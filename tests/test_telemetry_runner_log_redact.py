"""A redacted run log: the NFL runner's log line for line in our own words, NFLMeta's ids numbered by first sight, and
the check (and its tool) that gates anything bundled or shared.

The logs are the synthetic ones the miner's tests read, one in each of the runner's three formats; their provider
words are a phrase, a fake player and a fake description, and their ids numbers no other part of a line has.
"""

from __future__ import annotations

import re
import zipfile
from datetime import date
from pathlib import Path

import pytest

from omni.telemetry import runner_log_redact
from omni.telemetry.runner_log_mine import mine_log, read_line
from omni.telemetry.runner_log_redact import (
    REDACTED_SUFFIX,
    RedactionLeak,
    lines_to_redact,
    redact,
    redact_nights,
    write_redacted_nights,
)
from tests.nflmeta_recording import PHRASE
from tests.test_telemetry_runner_log import (
    DESCRIPTION,
    DISPLAY,
    FIRST,
    FULL_LOG,
    GAME_ID,
    NIGHT_ONE,
    NIGHT_THREE,
    NIGHT_TWO,
    NOV_19,
    NOV_19_DATE,
    PLAY_IDS,
    PLAYER,
)
from tools import redact_run_log as tool

FORMATS = {
    "nfl runner 2026-11-15": NIGHT_ONE,
    "nfl runner 2026-11-16": NIGHT_TWO,
    "nfl runner 2026-11-19": NOV_19,
}
EVERY_NIGHT = NIGHT_ONE + NIGHT_TWO + NIGHT_THREE + NOV_19
# The provider's words and ids in the logs, each of which must be gone once redacted.
PROVIDER_TOKENS = (
    *PHRASE.split(),
    *PLAYER.split(),
    "Ignatius",
    "Brambleworth",
    DESCRIPTION,
    "J.Doe",
    "kicks 65 yards",
    "Touchback",
    "questionable",
    "Score update",
    "BLT #1",
    "HOLDING",
    "22920",
    "22921",
    str(GAME_ID),
    *(str(play) for play in PLAY_IDS),
    "/api/v1/",
    "omni-board",
    DISPLAY,
    FULL_LOG,
)
_STAMPED = re.compile(r"\d{2}:\d{2}:\d{2}  ")


def _stamp(line: str) -> str:
    stamped = _STAMPED.match(line)
    return stamped.group() if stamped is not None else ""


@pytest.mark.parametrize("written", sorted(FORMATS))
def test_each_format_is_redacted_line_for_line_under_the_same_stamps(written: str) -> None:
    log = FORMATS[written]
    redaction = redact(log)
    header, *body = redaction.lines
    assert header == f"# omni-redacted-run-log/1 | formats: {written} | no NFLMeta text or ids"
    assert len(body) == len(log)
    assert [_stamp(line) for line in body] == [_stamp(line) for line in log]
    assert redaction.kept + redaction.replaced + redaction.not_kept == len(log)
    assert lines_to_redact(log) > 0 and lines_to_redact(redaction.lines) == 0


def test_no_provider_word_or_id_survives_redaction() -> None:
    redacted = "\n".join(redact(EVERY_NIGHT, first_date=FIRST).lines)
    assert [token for token in PROVIDER_TOKENS if token in redacted] == []
    assert all(token in "\n".join(EVERY_NIGHT) for token in PROVIDER_TOKENS)  # each one was there to redact


def test_the_2026_11_19_log_reads_in_our_own_words() -> None:
    redaction = redact(NOV_19, first_date=NOV_19_DATE)
    versions = NOV_19[3].removeprefix("20:10:00  ")
    config = NOV_19[4].removeprefix("20:10:00  ")
    usage = NOV_19[0], NOV_19[29], NOV_19[-1]
    assert redaction.lines == (
        "# omni-redacted-run-log/1 | night 2026-11-19 | formats: nfl runner 2026-11-19 | no NFLMeta text or ids",
        usage[0],
        "omni.app.football: NYJ @ NE (game G1), quad_128x64, delay 0s",
        "omni.app.football: the full log is [path], deleted by 2026-11-26",
        f"20:10:00  {versions}",
        f"20:10:00  {config}",
        "20:10:01  display: starting [command]",
        "20:10:02  NYJ @ NE: pre",
        "20:15:00  NYJ @ NE 0-0 Q1 15:00 [in] | [detail]",
        "20:15:00    > play",
        "20:15:01    = play (unconfirmed)",
        "20:16:00    > routine timeout #P001",
        "20:16:01    = routine timeout #P001",
        "20:16:30    ! late play: [detail]",
        "20:16:31    ! matched play missing: routine play #P002: [detail]",
        "20:17:00    x routine timeout #P001 (cleared: past its hard end)",
        "20:17:01    - an entry (unseen: the field moved on)",
        "20:17:02    > major score",
        "20:18:00  clock: the wall clock jumped +12m against the monotonic clock (the host slept?)",
        "20:18:00    - major score (unseen: its source went stale)",
        "20:18:00  woke: dimming the card until an answer from after the sleep is up",
        "20:18:05  woke: an answer from after the sleep is up",
        "20:19:00  feed: NFLMeta no longer lists game G1",
        "20:19:01  feed: NFLMeta fetch failed for /play-changes: HTTP Error 429: [detail]",
        "20:19:02  feed stale: [detail]",
        "20:19:30  feed: the ask failed",
        "20:19:31  market: the ask failed",
        "[line not kept]",
        "22:40:00  NYJ @ NE 13-27 Q4 0:00 [post] | final",
        "22:42:00  final: no more polling",
        usage[1],
        usage[1],
        "omni.app.football: NYJ @ NE (game G1), quad_128x64, delay 0s",
        "omni.app.football: the full log is [path], deleted by 2026-11-26",
        f"22:50:00  {versions}",
        f"22:50:00  {config}",
        "22:50:01    ! late play: [detail]",
        "22:50:02    - routine play #P003 (unseen: arrived late)",
        "22:50:05  final: no more polling",
        usage[2],
    )
    assert (redaction.kept, redaction.replaced, redaction.not_kept) == (20, 18, 1)


def test_the_first_two_nights_keep_their_reasons_markers_and_breaks() -> None:
    body = redact(NIGHT_ONE + NIGHT_TWO).lines[1:]
    assert body[:3] == (
        "[line not kept]",
        "omni.app.football: PIT @ CIN (game G1), quad_128x64, delay 0s",
        "display: starting [command]",
    )
    for line in (
        "18:25:10    > play",
        "18:28:01    = official_timeout",
        "18:30:00    ! late play: [detail]",
        "18:31:00  feed: NFLMeta fetch failed for /play-changes: HTTP Error 503: [detail]",
        "18:35:00  PIT @ CIN 0-0 Q1 0:00 [in] | end_of_quarter | [detail]",
        "18:35:00    - play (unseen: the break began)",
        "18:38:00  PIT @ CIN 7-0 Q2 0:00 [in] | halftime",
        "display: lost the display process; restarting shortly",
        "omni.app.football: LAC @ BAL (game G2), stack_64x64, delay 30s",
        "18:16:26    ~ timeout (blanked: the display went away)",
        "18:16:27    = timeout (again, on the new display)",
        "18:17:30    x play (cleared: the field moved on without its play)",
        "18:20:20  feed: quota: [detail]",
        "18:20:31  feed: NFLMeta sent an unreadable game G2: [detail]",
        "18:20:32  feed: [detail]",
        "18:20:33  feed: NFLMeta fetch failed for /live-scores: [detail]",
        "step failed: [detail]",
        "18:21:05  display: couldn't start it ([detail]); trying again shortly",
        "18:22:01    ! [note]: [detail]",
        "18:22:06    > review",
        "18:22:07    - play (unseen: [detail])",
        "18:22:07    ~ timeout",
    ):
        assert line in body


@pytest.mark.parametrize(
    ("line", "redacted"),
    [
        (
            "omni.app.football: XYZ @ CIN (game 1), giant, delay 5s",
            "omni.app.football: [team] @ CIN (game G1), [profile], delay 5s",
        ),
        ("XYZ @ QQQ 3-0 Q1 9:00 [in] |  a field", "[team] @ [team] 3-0 Q1 9:00 [in] | [detail]"),
        ("PIT @ CIN 3-0 Q1 9:00 [in]trailing", "PIT @ CIN 3-0 Q1 9:00 [in] | [detail]"),
        ("display: lost the display process (EOF in a description); restarting shortly", None),
        ("display: the receiver said something; restarting shortly", "display: [detail]; restarting shortly"),
        ("feed: NFLMeta no longer lists game", "feed: NFLMeta no longer lists game [game]: [detail]"),
        ("feed: NFLMeta no longer lists game 12 again", "feed: NFLMeta no longer lists game G1: [detail]"),
        ("feed: quota: HTTP Error 418 in a teapot", "feed: quota: HTTP Error 418: [detail]"),
        ("feed: HTTP Error 500: Internal", "feed: HTTP Error 500: [detail]"),
        ("  ! late play", None),
        ("  ! late play: ", "  ! late play"),
        ("  ! late play: major review #7", "  ! late play: major review #P001"),
        ("  ! late play: routine play", None),
        ("  - XP GOOD | words (unseen: a reason nobody wrote)", "  - conversion (unseen: [detail])"),
        ("  ~ KICKOFF (blanked: the panel caught fire)", "  ~ play (blanked: [detail])"),
        ("  x routine play #4 (cleared: arrived late)", "  x routine play #P001 (cleared: arrived late)"),
        ("[line not kept]", None),
    ],
)
def test_each_shape_says_what_it_can_in_our_own_words(line: str, redacted: str | None) -> None:
    expected = redacted if redacted is not None else line.replace("(EOF in a description)", "([detail])")
    _, written = redact([line]).lines
    assert written == expected
    assert redact([written]).lines[1] == written  # and reads the same again


def test_ordinals_are_numbered_by_first_sight_and_stable() -> None:
    log = [
        "omni.app.football: PIT @ CIN (game 700), quad_128x64, delay 0s",
        "20:00:00    > routine play #31",
        "20:00:01    > routine timeout #30",
        "20:00:02    = routine play #31",
        "20:00:03  feed: NFLMeta no longer lists game 800",
        "20:00:04  feed: NFLMeta sent an unreadable game 700: boom",
        "20:00:05    ! late play: major score #29: words",
    ]
    redaction = redact(log)
    assert redaction.lines[1:] == (
        "omni.app.football: PIT @ CIN (game G1), quad_128x64, delay 0s",
        "20:00:00    > routine play #P001",
        "20:00:01    > routine timeout #P002",
        "20:00:02    = routine play #P001",
        "20:00:03  feed: NFLMeta no longer lists game G2",
        "20:00:04  feed: NFLMeta sent an unreadable game G1: [detail]",
        "20:00:05    ! late play: major score #P003: [detail]",
    )
    assert redact(list(redaction.lines[1:])).lines == redaction.lines  # numbered again, the same
    swapped = [redaction.lines[0], *(line.replace("G1", "G9") for line in redaction.lines[1:])]
    assert lines_to_redact(swapped) == 2  # an ordinal out of its order is renumbered


def test_a_redacted_log_mines_as_the_log_did() -> None:
    redaction = redact(EVERY_NIGHT, first_date=FIRST)
    assert redaction.lines[0] == (
        "# omni-redacted-run-log/1 | nights 2026-11-15 to 2026-11-18 | formats: nfl runner 2026-11-15,"
        " nfl runner 2026-11-16, nfl runner 2026-11-19 | no NFLMeta text or ids"
    )
    assert mine_log(redaction.lines[1:], FIRST) == mine_log(EVERY_NIGHT, FIRST)


def test_each_night_is_redacted_as_a_log_of_its_own() -> None:
    nights = redact_nights(EVERY_NIGHT, FIRST)
    assert [day for day, _ in nights] == [date(2026, 11, day) for day in (15, 16, 17, 18)]
    assert sum(len(night.lines) - 1 for _, night in nights) == len(EVERY_NIGHT)
    assert [night.lines[0] for _, night in nights] == [
        f"# omni-redacted-run-log/1 | night 2026-11-{day} | formats: nfl runner {written} | no NFLMeta text or ids"
        for day, written in (("15", "2026-11-15"), ("16", "2026-11-16"), ("17", "2026-11-15"), ("18", "2026-11-19"))
    ]
    two, four = nights[1][1], nights[3][1]
    assert two.lines[1] == "omni.app.football: LAC @ BAL (game G1), stack_64x64, delay 30s"  # numbered night by night
    assert four.lines[1] == NOV_19[0]  # the month's usage goes with the run it opens
    assert all(lines_to_redact(night.lines) == 0 for _, night in nights)


def test_a_log_with_no_run_or_no_line_has_a_header_all_the_same() -> None:
    assert redact([], first_date=FIRST).lines == ("# omni-redacted-run-log/1 | formats: none | no NFLMeta text or ids",)
    assert redact_nights([], FIRST) == []
    header, line = redact([PHRASE], first_date=FIRST).lines
    assert header == "# omni-redacted-run-log/1 | night 2026-11-15 | formats: none | no NFLMeta text or ids"
    assert line == "[line not kept]"


@pytest.mark.parametrize(
    ("lines", "changed"),
    [
        ([], 0),
        (["20:00:00  final: no more polling"], 0),  # our own words, without a header
        (["# omni-redacted-run-log/1 | formats: none | no NFLMeta text or ids"], 0),
        (["# omni-redacted-run-log/1 | night 2026-11-15 | formats: nfl runner 2026-11-15 | no NFLMeta text or ids"], 0),
        (["# omni-redacted-run-log/1 | formats: nfl runner 2026-11-15 | no NFLMeta text or ids", "x"], 1),
        ([f"# omni-redacted-run-log/1 | formats: {PHRASE} | no NFLMeta text or ids"], 1),
        (["# omni-redacted-run-log/2 | formats: none | no NFLMeta text or ids"], 1),
        (
            [
                "# omni-redacted-run-log/1 | formats: none | no NFLMeta text or ids",
                f"18:30:00    ! late play: {PHRASE}",
            ],
            1,
        ),
        ([f"# a note: {PHRASE}"], 1),
    ],
)
def test_lines_to_redact_counts_what_a_log_still_carries(lines: list[str], changed: int) -> None:
    assert lines_to_redact(lines) == changed


def test_a_redaction_that_would_change_again_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def leaky(lines: list[str]) -> list[tuple[object, str]]:
        return [(read_line(line), line + "!") for line in lines]

    monkeypatch.setattr(runner_log_redact, "_redacted", leaky)
    with pytest.raises(RedactionLeak, match="would change if redacted again"):
        redact(NIGHT_ONE)


def test_each_night_is_written_beside_its_digest_once(tmp_path: Path) -> None:
    log = tmp_path / "runner.log"
    log.write_text("\n".join(NIGHT_ONE + NIGHT_TWO) + "\n")
    out = tmp_path / "out"
    paths = write_redacted_nights(log, out, FIRST)
    assert paths == [out / f"2026-11-15{REDACTED_SUFFIX}", out / f"2026-11-16{REDACTED_SUFFIX}"]
    for path, (_, night) in zip(paths, redact_nights(NIGHT_ONE + NIGHT_TWO, FIRST)):
        assert path.read_text() == "\n".join(night.lines) + "\n"
    assert sorted(path.name for path in out.iterdir()) == [path.name for path in paths]  # nothing partial left
    paths[0].write_text("kept as it was\n")
    assert write_redacted_nights(log, out, FIRST) == paths
    assert paths[0].read_text() == "kept as it was\n"


# --- the tool ----------------------------------------------------------------------------------------------------


def test_the_tool_writes_a_redacted_log_printing_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, out = tmp_path / "runner.log", tmp_path / "kept" / "2026-11-19.redacted.log"
    log.write_text("\n".join(NOV_19) + "\n")
    assert tool.main([str(log), "--out", str(out), "--first-date", "2026-11-19"]) == 0
    assert out.read_text() == "\n".join(redact(NOV_19, first_date=NOV_19_DATE).lines) + "\n"
    printed = capsys.readouterr()
    assert printed.out == f"{out}: 20 lines kept, 18 replaced, 1 not kept\n" and printed.err == ""
    assert tool.main([str(log), "--out", str(out)]) == 1  # never over a file that's there
    printed = capsys.readouterr()
    assert printed.out == "" and printed.err == f"{out} is already there; not overwritten\n"
    assert out.read_text().startswith("# omni-redacted-run-log/1 | night 2026-11-19 |")


def _zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


def test_the_check_passes_redacted_logs_and_fails_raw_ones_in_folders_and_zips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = "\n".join(NIGHT_TWO) + "\n"
    redacted = "\n".join(redact(NIGHT_TWO).lines) + "\n"
    sources = tmp_path / "sources"
    (sources / "logs").mkdir(parents=True)
    (sources / "ok.redacted.log").write_text(redacted)
    (sources / "notes.md").write_text(f"{PHRASE}\n")  # not a log: not checked
    assert tool.main(["--check", str(sources)]) == 0
    assert capsys.readouterr().out == "checked 1 logs: 0 carry NFLMeta text or ids, or can't be read\n"

    (sources / "logs" / "runner.log").write_text(raw)
    bundle = _zip(tmp_path / "bundle.zip", {"sources/ok.log": redacted, "sources/runner.log": raw, "README.md": PHRASE})
    _zip(sources / "inner.zip", {"runner.log": raw})
    assert tool.main(["--check", str(sources), str(bundle)]) == 1
    changed = lines_to_redact(NIGHT_TWO)
    printed = capsys.readouterr().out
    assert printed.splitlines() == [
        f"{sources / 'inner.zip'}:runner.log: {changed} lines would change under redaction",
        f"{sources / 'logs' / 'runner.log'}: {changed} lines would change under redaction",
        f"{bundle}:sources/runner.log: {changed} lines would change under redaction",
        "checked 5 logs: 3 carry NFLMeta text or ids, or can't be read",
    ]
    assert not any(token in printed for token in PROVIDER_TOKENS)
    clean = _zip(tmp_path / "clean.zip", {"sources/ok.log": redacted})
    assert tool.main(["--check", str(clean), str(sources / "ok.redacted.log")]) == 0
    capsys.readouterr()


def test_the_check_fails_what_it_can_t_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.zip"
    broken.write_text("not a zip")
    missing = tmp_path / "missing.log"
    assert tool.main(["--check", str(broken), str(missing)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{broken}: can't be read",
        f"{missing}: can't be read",
        "checked 2 logs: 2 carry NFLMeta text or ids, or can't be read",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["runner.log"],
        ["--out", "out.log"],
        ["--check", "sources", "--out", "out.log"],
        ["--check", "a", "--first-date", "2026-11-19"],
    ],
)
def test_the_tool_wants_a_log_and_out_or_only_paths_to_check(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        tool.main(argv)
    assert stopped.value.code == 2
