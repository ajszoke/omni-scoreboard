"""Mining an NFLMeta recording: our own telemetry about the feed and its breaks, and none of its words."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from omni.providers.rights import DataSource, stamp_recording
from omni.telemetry.digest import DigestLeak, check_digest
from omni.telemetry.nflmeta_mine import MEASURES, allowed, mine, mine_recording, summary
from tests.nflmeta_recording import PHRASE, Recording, change, game_row, injury

FOLDER = "2026-11-15-PIT-CIN"
NOW = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)


def _spread(count: int, *values: float) -> dict[str, Any]:
    """A spread's record: its count, then its min, p25, median, p75 and max (none when it's empty)."""
    least, p25, median, p75, most = values if values else (None,) * 5
    return {"count": count, "min": least, "p25": p25, "median": median, "p75": p75, "max": most}


def _same(value: float) -> dict[str, Any]:
    return _spread(1, value, value, value, value, value)


C = {
    1: change(1, 100, written=100, quarter=1, play_type="kickoff"),
    2: change(2, 110, written=130, quarter=1),
    3: change(
        3, 120, written=140, quarter=1, play_type="timeout", offense="CIN", description=f"Timeout #1 by CIN. {PHRASE}"
    ),
    4: change(4, 110, written=200, quarter=1, revision=2, yards_gained=5),
    5: change(5, 130, written=230, quarter=1, play_type="pass_reception"),
    6: change(6, 140, written=250, quarter=1, play_type="official_timeout"),
    7: change(7, 150, written=420, quarter=1),
    8: change(8, 155, written=425, quarter=1),
    9: change(9, 160, written=430, quarter=1, play_type="end_period"),
    10: change(10, 170, written=530, quarter=2, play_type="pass_reception"),
    11: change(11, 180, written=600, quarter=2, play_type="passing_touchdown", touchdown=True),
    12: change(
        12,
        180,
        written=640,
        quarter=2,
        play_type="passing_touchdown",
        revision=2,
        touchdown=True,
        extra_point_result="good",
    ),
    13: change(13, 190, written=700, quarter=2, play_type="kickoff", offense="CIN"),
    14: change(14, 200, written=800, quarter=2, play_type="two_minute_warning"),
    15: change(15, 210, written=900, quarter=2, play_type="pass_reception", offense="CIN"),
    16: change(
        16,
        210,
        written=960,
        quarter=2,
        play_type="pass_incompletion",
        offense="CIN",
        revision=2,
        description=f"The Replay Official reviewed the pass completion ruling, and the play was REVERSED. {PHRASE}",
    ),
    17: change(17, 220, written=1000, quarter=2, offense="CIN"),
    18: change(18, 230, written=1010, quarter=2, play_type="end_of_half"),
    19: change(19, 240, written=1800, quarter=3, play_type="kickoff"),
    20: change(20, 260, written=1860, quarter=3, play_type="pass_reception"),
    21: change(21, 250, written=1870, quarter=3),  # written into the feed after 260: late
    22: change(22, 270, written=1900, quarter=3, play_type="end_period"),
    23: change(23, 280, written=2000, quarter=4),
    24: change(24, 290, written=2100, quarter=4),
    25: change(25, 300, written=2200, quarter=4, play_type="end_of_game"),
}


def _game(root: Path) -> Path:
    """A whole (short) game, recorded as the board does: a scoreboard, then the play feed half a second later.

    Its facts, in seconds from the recording's start: each play row arrives 6.5 s after it was written, but for
    the 155 (1.5 s) and the touchdown (16.5 s, ten after the scoreboard showed it). A team timeout, an official
    timeout, the two-minute warning and a review's ruling each come 6.5 s after they were written, then the next
    snap 90, 170, 100 and 40 s later. The quarter breaks and halftime reach us 5 s after the scoreboard row was
    written. The board restarts over halftime and reads the whole feed again, which has lost the 155.
    """
    rec = Recording(root / FOLDER)

    def beat(at: float, *rows: dict[str, Any], billed: str = "1", **row: Any) -> None:
        rec.scoreboard(at, game_row(**row), billed=billed)
        rec.plays(at + 0.5, *rows)

    rec.scoreboard(0, game_row(phase="pre", period=0))
    rec.plays(0.5, cursor=None)  # the board's first read, from the feed's start
    beat(106, C[1])
    beat(136, C[2], clock="14:30")
    beat(146, C[3], clock="14:00")
    beat(206, C[4], clock="13:00")
    beat(236, C[5], clock="12:30")
    beat(256, C[6], clock="12:00")
    beat(426, C[7], C[8], clock="11:00")
    beat(436, C[9], clock="0:00", detail=f"End of 1st {PHRASE}", changed=431)
    beat(536, C[10], period=2)
    beat(606, period=2, clock="13:00", scores=(6, 0))
    beat(616, C[11], period=2, clock="13:00", scores=(6, 0))
    beat(646, C[12], period=2, clock="13:00", scores=(6, 0))
    beat(656, period=2, clock="13:00", scores=(7, 0))
    beat(706, C[13], period=2, clock="12:50", scores=(7, 0))
    beat(806, C[14], period=2, clock="2:00", scores=(7, 0))
    beat(906, C[15], period=2, clock="1:30", scores=(7, 0))
    beat(966, C[16], period=2, clock="1:30", scores=(7, 0))
    beat(1006, C[17], period=2, clock="1:00", scores=(7, 0))
    halftime: dict[str, Any] = {"period": 2, "clock": "0:00", "scores": (7, 0), "detail": f"Halftime {PHRASE}"}
    halftime["situation"] = False
    beat(1016, C[18], changed=1011, **halftime)
    # A restart over halftime: an unfiltered read to find the game, then the whole feed again, in two pages.
    rec.scoreboard(1100, game_row(game_id=999, away="DAL", home="NYG"), game_row(**halftime), billed="2", query={})
    rec.scoreboard(1100.2, game_row(**halftime))
    page_two = f"{PHRASE} page two"
    rec.plays(1100.5, C[1], C[4], C[3], C[5], C[6], C[7], C[9], cursor=None, more=True, next_cursor=page_two)
    rec.plays(1100.8, C[10], C[12], C[13], C[14], C[16], C[17], C[18], cursor=page_two)
    beat(1806, C[19], period=3, scores=(7, 0))
    beat(1866, C[20], period=3, clock="14:00", scores=(7, 0))
    beat(1876, C[21], period=3, clock="13:30", scores=(7, 0))
    beat(1906, C[22], period=3, clock="0:00", scores=(7, 0), detail=f"End of 3rd {PHRASE}", changed=1901)
    beat(2006, C[23], period=4, scores=(7, 0))
    # The recorder tool's lines carry no query.
    rec.scoreboard(2016, game_row(period=4, scores=(7, 0), injuries=(injury(2010),)), billed="2", recorder=True)
    rec.plays(2016.5, recorder=True)
    beat(2106, C[24], period=4, clock="13:00", scores=(7, 0), injuries=(injury(2010),), billed="2")
    final: dict[str, Any] = {"phase": "post", "period": 4, "clock": "0:00", "scores": (7, 0), "detail": PHRASE}
    beat(2206, C[25], **final)
    beat(2336, **final)
    return root / FOLDER


@pytest.fixture
def digest(tmp_path: Path) -> dict[str, Any]:
    return mine(_game(tmp_path))


def test_a_recording_is_named_by_its_folder_and_its_game(digest: dict[str, Any]) -> None:
    assert (digest["schema"], digest["kind"], digest["folder"], digest["date"]) == (
        "omni-mined/1",
        "nflmeta recording",
        FOLDER,
        "2026-11-15",
    )
    assert (digest["away"], digest["home"]) == ("PIT", "CIN")
    assert digest["replay"] == {"scoreboards": 31, "looks_failed": 0, "unreadable_lines": 0, "unreadable_rows": 0}


def test_each_break_is_measured_from_its_onset_to_its_receipt_and_the_next_snap(digest: dict[str, Any]) -> None:
    def row(kind: str, quarter: int, *spans: float | None) -> dict[str, Any]:
        onset, residual, receipt = spans
        return {
            "kind": kind,
            "quarter": quarter,
            "onset_to_receipt": onset,
            "onset_to_next_action": residual,
            "receipt_to_next_action_receipt": receipt,
        }

    assert digest["break_rows"] == [
        row("nfl halftime", 2, 5.0, 789.0, 790.5),
        row("nfl injury timeout", 4, None, None, 90.5),  # a report: no onset, and the next snap after it
        row("nfl official timeout", 1, 6.5, 170.0, 170.0),
        row("nfl quarter break", 1, 5.0, 99.0, 100.5),
        row("nfl quarter break", 3, 5.0, 99.0, 100.5),
        row("nfl review", 2, 6.5, 40.0, 40.0),  # from the ruling's own write
        row("nfl team timeout", 1, 6.5, 90.0, 90.0),
        row("nfl two-minute warning", 2, 6.5, 100.0, 100.0),
    ]
    quarters = next(kind for kind in digest["breaks"] if kind["kind"] == "nfl quarter break")
    assert quarters == {
        "kind": "nfl quarter break",
        "count": 2,
        "onsets_observed": 0,
        "onset_to_receipt": _spread(2, 5.0, 5.0, 5.0, 5.0, 5.0),
        "onset_to_next_action": _spread(2, 99.0, 99.0, 99.0, 99.0, 99.0),
        "receipt_to_next_action_receipt": _spread(2, 100.5, 100.5, 100.5, 100.5, 100.5),
    }
    injuries = next(kind for kind in digest["breaks"] if kind["kind"] == "nfl injury timeout")
    assert injuries["onset_to_receipt"] == _spread(0) and injuries["receipt_to_next_action_receipt"] == _same(90.5)
    assert [kind["kind"] for kind in digest["breaks"]] == sorted({row["kind"] for row in digest["break_rows"]})


def test_feed_latency_and_whether_the_scoreboard_moved_before_its_play(digest: dict[str, Any]) -> None:
    assert digest["latency"] == {
        "plays": 22,
        "write_to_receipt": _spread(22, 1.5, 6.5, 6.5, 6.5, 16.5),
        "score_moves": 2,
        "scoreboard_first": 1,  # the touchdown: its row came 10.5 s after the scoreboard showed it
        "plays_first": 1,  # the extra point: its revision came 9.5 s before the scoreboard
        "together": 0,
        "never_explained": 0,
        "scoreboard_lead": _spread(2, -9.5, -4.5, 0.5, 5.5, 10.5),
    }
    assert digest["score_rows"] == [{"quarter": 2, "scoreboard_lead": -9.5}, {"quarter": 2, "scoreboard_lead": 10.5}]


def test_late_plays_revisions_and_a_play_gone_from_a_later_whole_read(digest: dict[str, Any]) -> None:
    assert digest["order"] == {
        "late_plays": 1,
        "late_lag": _same(10.0),
        "revised_plays": 3,
        "revisions": 3,
        "revision_delay": _spread(3, 40.0, 50.0, 60.0, 65.0, 70.0),
        "history_reads": 2,
        "vanished_plays": 1,
        "returned_plays": 0,
    }


def test_cadence_by_quarter_half_and_game(digest: dict[str, Any]) -> None:
    def quarter(number: int, wall: float, ratio: float, gaps: dict[str, Any]) -> dict[str, Any]:
        return {"quarter": number, "wall": wall, "game_clock": 900, "wall_per_clock_second": ratio, "snap_gap": gaps}

    assert digest["cadence"] == {
        "quarters": [
            quarter(1, 330.0, 0.367, _spread(4, 5.0, 23.75, 65.0, 122.5, 190.0)),
            quarter(2, 480.0, 0.533, _spread(4, 70.0, 92.5, 100.0, 125.0, 200.0)),
            quarter(3, 100.0, 0.111, _spread(2, 10.0, 22.5, 35.0, 47.5, 60.0)),
            quarter(4, 200.0, 0.222, _same(100.0)),
        ],
        "halves": [{"half": 1, "wall": 910.0}, {"half": 2, "wall": 400.0}],
        "game_wall": 2100.0,
    }


def test_the_event_mix_notes_and_scoring_plays(digest: dict[str, Any]) -> None:
    events = {(row["quarter"], row["kind"]): row["count"] for row in digest["events"]["events"]}
    assert events == {
        (1, "official_timeout"): 1,
        (1, "play"): 5,
        (1, "timeout"): 1,
        (2, "conversion"): 1,
        (2, "play"): 5,
        (2, "review"): 1,
        (2, "two_minute_warning"): 1,
        (3, "play"): 2,  # the late play is held back
        (4, "injury"): 1,
        (4, "play"): 2,
    }
    assert digest["events"]["notes"] == [{"kind": "late play", "count": 1}]
    assert digest["events"]["scoring"] == [{"kind": "touchdown", "count": 1}, {"kind": "extra point", "count": 1}]


def test_billing_and_polling_per_endpoint(digest: dict[str, Any]) -> None:
    assert digest["billing"] == [
        {
            "endpoint": "live-scores",
            "requests": 31,
            "billed_rows": 34,
            "billed_per_read": _spread(31, 1, 1, 1, 1, 2),
            "rows_per_read": _spread(31, 1, 1, 1, 1, 2),
            "empty_reads": 0,
        },
        {
            "endpoint": "play-changes",
            "requests": 31,
            "billed_rows": 39,
            "billed_per_read": _spread(31, 0, 1, 1, 1, 7),
            "rows_per_read": _spread(31, 0, 1, 1, 1, 7),
            "empty_reads": 5,
        },
    ]
    assert digest["polling"] == {
        "endpoints": [
            {"endpoint": "live-scores", "answer_gap": _spread(30, 0.2, 12.5, 55.0, 100.0, 705.8)},
            {"endpoint": "play-changes", "answer_gap": _spread(30, 0.3, 12.5, 55.0, 100.0, 705.7)},
        ],
        "recommended": [{"seconds": 10, "answers": 31}],
    }


def test_coverage_counts_every_snapshot_with_a_state(digest: dict[str, Any]) -> None:
    coverage = digest["coverage"]
    assert coverage["snapshots"] == 30  # all but the pregame look
    period = next(field for field in coverage["fields"] if field["field"] == "period")
    assert period == {
        "field": "period",
        "reported": 1.0,
        "inferred": 0.0,
        "defaulted": 0.0,
        "unavailable": 0.0,
        "not_applicable": 0.0,
    }


def test_no_provider_words_reach_the_digest_or_its_summary(tmp_path: Path) -> None:
    path = mine_recording(_game(tmp_path), tmp_path / "out")
    assert path == tmp_path / "out" / f"{FOLDER}.json"
    written = path.read_text()
    words = path.with_suffix(".md").read_text()
    for text in (written, words):
        assert PHRASE not in text and "Velvet" not in text and "Quokka" not in text
    assert "Breaks" in words and "nfl team timeout" in words
    check_digest(json.loads(written), measures=MEASURES, values=allowed(json.loads(written)))


def test_a_mined_folder_is_not_mined_again(tmp_path: Path) -> None:
    folder = _game(tmp_path)
    path = mine_recording(folder, tmp_path / "out")
    assert path is not None
    path.write_text("kept as it was\n")
    assert mine_recording(folder, tmp_path / "out") == path
    assert path.read_text() == "kept as it was\n"


def test_only_an_nflmeta_recording_is_mined(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert mine_recording(tmp_path / "missing", out) is None
    empty = tmp_path / "2026-11-15-PIT-CIN"
    empty.mkdir()
    assert mine_recording(empty, out) is None  # no scoreboards recorded
    other = Recording(tmp_path / "2026-11-16-LAL-BOS")
    other.scoreboard(0, game_row())
    stamp_recording(other.folder, DataSource.BALLDONTLIE, NOW)
    assert mine_recording(other.folder, out) is None
    assert not out.exists()


def test_a_stamped_recording_is_mined_and_an_unreadable_stamp_is_judged_by_its_files(tmp_path: Path) -> None:
    stamped = _game(tmp_path / "stamped")
    stamp_recording(stamped, DataSource.NFLMETA, NOW)
    assert mine_recording(stamped, tmp_path / "out1") is not None
    garbled = _game(tmp_path / "garbled")
    (garbled / "RETENTION.json").write_text("{")
    assert mine_recording(garbled, tmp_path / "out2") is not None


@pytest.mark.parametrize("name", ["PIT-CIN", "2026-11-15-PIT-XYZ", "2026-13-45-PIT-CIN"])
def test_a_folder_not_named_for_a_date_and_two_clubs_cant_be_mined(tmp_path: Path, name: str) -> None:
    rec = Recording(tmp_path / name)
    rec.scoreboard(0, game_row())
    with pytest.raises(ValueError, match="named"):
        mine_recording(rec.folder, tmp_path / "out")


def test_a_recording_whose_scoreboards_never_list_the_game_cant_be_mined(tmp_path: Path) -> None:
    rec = Recording(tmp_path / "2026-11-15-DAL-NYG")
    rec.scoreboard(0, game_row())
    with pytest.raises(ValueError, match="no recorded scoreboard lists DAL @ NYG"):
        mine(rec.folder)


def _small(root: Path) -> Path:
    """Three readable looks (live, halftime, final), one the provider can't read, and some broken lines."""
    rec = Recording(root / "2026-11-16-LAC-BAL")
    teams: dict[str, Any] = {"away": "LAC", "home": "BAL", "game_id": 7}
    rec.scoreboard(0, game_row(**teams), query={"game_id": 7})
    rec.plays(0.5, recorder=True, billed="0")
    rec.scoreboard(10, game_row(game_id=8, away="DAL", home="NYG"), recorder=True)  # the game isn't listed
    rec.scoreboard(20, game_row(**teams, period=2, clock="0:00", detail="Halftime", situation=False), poll=15)
    rec.plays(20.5, {"change_id": 5, "play": None}, {"play": {"play_number": 9}}, recorder=True, billed="x")
    rec.plays(20.7, {"change_id": 6, "play": {"play_number": 9, "quarter": "second"}}, recorder=True)
    rec.scoreboard(35, game_row(**teams, phase="post", period=4, clock="0:00", detail="Final"), billed=None, poll=0)
    rec.raw("live-scores.jsonl", "{not json")
    rec.raw("live-scores.jsonl", "[1, 2]")
    rec.raw("live-scores.jsonl", json.dumps({"received_at": "2026-11-16T01:00:00", "body": {}}))  # no offset
    rec.raw("play-changes.jsonl", json.dumps({"received_at": 5, "body": {}}))
    rec.raw("play-changes.jsonl", json.dumps({"received_at": "yesterday", "body": {"data": "x", "meta": "y"}}))
    return rec.folder


def test_coverage_shares_per_field_across_live_halftime_and_final(tmp_path: Path) -> None:
    fields = {field.pop("field"): field for field in mine(_small(tmp_path))["coverage"]["fields"]}
    third, two_thirds = 0.3333, 0.6667

    def shares(**levels: float) -> dict[str, float]:
        return {
            level: levels.get(level, 0.0)
            for level in ("reported", "inferred", "defaulted", "unavailable", "not_applicable")
        }

    assert fields["score"] == fields["period"] == fields["clock"] == fields["interval"] == shares(reported=1.0)
    assert fields["possession"] == shares(reported=third, unavailable=third, not_applicable=third)
    assert fields["down_distance"] == fields["spot"] == shares(reported=third, not_applicable=two_thirds)
    assert fields["timeouts"] == shares(inferred=third, not_applicable=two_thirds)
    assert fields["challenges"] == shares(inferred=two_thirds, not_applicable=third)


def test_failed_looks_unreadable_lines_and_rows_and_a_recording_without_whole_reads(tmp_path: Path) -> None:
    digest = mine(_small(tmp_path))
    assert digest["replay"] == {"scoreboards": 4, "looks_failed": 1, "unreadable_lines": 5, "unreadable_rows": 3}
    assert digest["order"]["history_reads"] == 0
    assert digest["order"]["vanished_plays"] is None and digest["order"]["returned_plays"] is None
    assert digest["billing"][0]["billed_rows"] == 3 and digest["billing"][0]["billed_per_read"]["count"] == 3
    assert digest["billing"][1] == {
        "endpoint": "play-changes",
        "requests": 3,
        "billed_rows": 1,  # an unreadable count isn't billed
        "billed_per_read": _spread(2, 0, 0.25, 0.5, 0.75, 1),
        "rows_per_read": _spread(3, 0, 0.5, 1, 1.5, 2),
        "empty_reads": 1,
    }
    assert digest["polling"]["endpoints"][0]["answer_gap"] == _spread(3, 10.0, 10.0, 10.0, 12.5, 15.0)
    assert digest["polling"]["recommended"] == [{"seconds": 10, "answers": 2}, {"seconds": 15, "answers": 1}]
    assert digest["breaks"] == [  # the halftime row doesn't say when it was written: NFLMeta's observation dates it
        {
            "kind": "nfl halftime",
            "count": 1,
            "onsets_observed": 1,
            "onset_to_receipt": _same(20.0),
            "onset_to_next_action": _spread(0),
            "receipt_to_next_action_receipt": _spread(0),
        }
    ]
    assert digest["latency"]["plays"] == 0 and digest["latency"]["score_moves"] == 0
    assert digest["cadence"] == {
        "quarters": [],
        "halves": [{"half": 1, "wall": None}, {"half": 2, "wall": None}],
        "game_wall": None,
    }
    assert "plays gone from one: -" in summary(digest)


def test_the_guard_refuses_a_digest_holding_anything_off_its_lists(tmp_path: Path) -> None:
    digest = mine(_game(tmp_path))
    values = allowed(digest)
    check_digest(digest, measures=MEASURES, values=values)
    with pytest.raises(DigestLeak, match=r"string at digest\.away"):
        check_digest({**digest, "away": PHRASE}, measures=MEASURES, values=values)
    with pytest.raises(DigestLeak, match="a key in digest.latency isn't a declared measure"):
        check_digest({**digest, "latency": {PHRASE: 1}}, measures=MEASURES, values=values)
    with pytest.raises(DigestLeak, match=r"value at digest\.breaks\[0\]"):
        check_digest({**digest, "breaks": [object()]}, measures=MEASURES, values=values)


def test_every_kind_of_score_and_a_play_a_penalty_wiped_out(tmp_path: Path) -> None:
    """NE scores a safety, a field goal and a touchdown with a two-point try; NYJ's touchdown is wiped out by a
    penalty, and a field goal of theirs shows on the scoreboard with no play behind it."""
    rec = Recording(tmp_path / "2026-11-17-NYJ-NE")
    teams: dict[str, Any] = {"away": "NYJ", "home": "NE"}
    rec.scoreboard(0, game_row(**teams))
    rec.plays(0.5, cursor=None, more=True, next_cursor=f"{PHRASE} never read")  # a whole read left unfinished
    plays = [
        change(1, 10, written=10, quarter=1, play_type="safety", offense="NYJ"),
        change(2, 20, written=30, quarter=1, play_type="field_goal_good", offense="NE", field_goal_result="made"),
        change(
            3,
            30,
            written=50,
            quarter=1,
            play_type="rushing_touchdown",
            offense="NE",
            touchdown=True,
            two_point_conv_result="success",
        ),
        change(
            4,
            40,
            written=60,
            quarter=1,
            play_type="passing_touchdown",
            offense="NYJ",
            touchdown=True,
            description=f"{PHRASE} TOUCHDOWN. PENALTY on NYJ-{PHRASE}, Holding, 10 yards, enforced. - No Play.",
        ),
    ]
    for at, row, scores in zip((20, 40, 60, 70), plays, ((0, 2), (0, 5), (0, 13), (0, 13))):
        rec.scoreboard(at, game_row(**teams, scores=scores))
        rec.plays(at + 0.5, row)
    unwritten = [change(5, 50, written=0, quarter=1), change(6, 50, written=0, quarter=1, revision=2)]
    for row in unwritten:
        del row["changed_at"]  # rows that don't say when the feed wrote them
    for at, row, state in zip((80, 100), unwritten, ("out", "questionable_to_return")):
        rec.scoreboard(at, game_row(**teams, scores=(0, 13), injuries=(injury(at - 5, state),)))
        rec.plays(at + 0.5, row)
    rec.scoreboard(75, game_row(**teams, scores=(0, 13), injuries=(injury(74),)))
    rec.scoreboard(110, game_row(**teams, scores=(3, 13)))
    digest = mine(rec.folder)
    assert digest["events"]["scoring"] == [
        {"kind": "touchdown", "count": 1},
        {"kind": "field goal", "count": 1},
        {"kind": "safety", "count": 1},
        {"kind": "two-point try", "count": 1},
    ]
    latency = digest["latency"]
    assert (latency["score_moves"], latency["scoreboard_first"], latency["never_explained"]) == (4, 3, 1)
    assert latency["scoreboard_lead"] == _spread(3, 0.5, 0.5, 0.5, 0.5, 0.5)
    assert digest["score_rows"][0] == {"quarter": 1, "scoreboard_lead": None}
    assert digest["order"]["revision_delay"] == _same(20.0)  # by arrival, the rows not saying when written
    assert digest["order"]["history_reads"] == 0
    injuries = [row for row in digest["break_rows"] if row["kind"] == "nfl injury timeout"]
    assert len(injuries) == 2  # questionable, out, then questionable again: the same report twice


def test_a_recording_the_provider_cant_read_cant_be_mined(tmp_path: Path) -> None:
    rec = Recording(tmp_path / FOLDER)
    rec.scoreboard(0, game_row(phase="halfway"))
    with pytest.raises(ValueError, match="the provider read none of the recorded scoreboards"):
        mine(rec.folder)


def test_a_change_id_is_read_as_the_feed_writes_it_a_string_of_digits() -> None:
    from omni.telemetry.nflmeta_mine import _change_id

    assert _change_id("40213") == 40213
    assert _change_id(40213) == 40213
    assert _change_id("c-7") is None
    assert _change_id(None) is None
