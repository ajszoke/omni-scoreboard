"""Football plays: reading the league's play text, summarizing plays, and the ledger built from them."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from omni.core.enum import HomeAway
from omni.domain.football import PlayTone, TeamChallenges, TeamTimeouts
from omni.events.football import (
    ConversionResult,
    FieldGoalResult,
    FootballEventKind,
    FootballPlay,
    FootballPlayType,
    PlayText,
    Review,
    ReviewVerdict,
    TimeoutCall,
    conversion_headline,
    review_headline,
    summarize_play,
)
from omni.events.football_clubs import PLAY_BY_PLAY_CODES, canonical_play_text, club_code
from omni.events.football_ledger import PlayerLine, PlayLedger
from omni.providers.nfl_teams import NflTeamRegistry

_TEAMS = NflTeamRegistry.default()
CHALLENGE_UPHELD = (
    "(Shotgun) J.Burrow pass short middle to S.Perine to CIN 45 for 5 yards (N.Herbig; P.Queen).Cincinnati challenged"
    " the short of the line to gain ruling, and the play was Upheld. The ruling on the field stands. (Timeout #1.)"
)
BOOTH_REVERSED = (
    "(Shotgun) A.Rodgers pass deep left intended for D.Washington INTERCEPTED by J.Battle at CIN 36. J.Battle to"
    " CIN 36 for no gain (D.Washington).The Replay Official reviewed the interception ruling, and the play was"
    " REVERSED.(Shotgun) A.Rodgers pass incomplete deep left to D.Washington."
)


def play(number: int, description: str, play_type: str = "rush", revision: int = 1, **fields: Any) -> FootballPlay:
    return FootballPlay(
        number=number,
        revision=revision,
        play_type=FootballPlayType(play_type),
        quarter=fields.pop("quarter", 1),
        description=description,
        **fields,
    )


def test_reads_a_ruling_and_the_play_as_it_stands() -> None:
    upheld = PlayText(CHALLENGE_UPHELD)
    assert upheld.standing == "J.Burrow pass short middle to S.Perine to CIN 45 for 5 yards (N.Herbig; P.Queen)."
    assert upheld.review == Review("Cincinnati", "short of the line to gain", ReviewVerdict.STANDS, 1)
    reversed_ = PlayText(BOOTH_REVERSED)
    assert reversed_.standing == "A.Rodgers pass incomplete deep left to D.Washington."
    assert reversed_.review == Review(None, "interception", ReviewVerdict.REVERSED, None)
    plain = PlayText("(No Huddle, Shotgun) C.Brown up the middle to CIN 41 for 9 yards (J.Ramsey).")
    assert plain.standing == "C.Brown up the middle to CIN 41 for 9 yards (J.Ramsey)."
    assert plain.review is None and plain.timeout is None and plain.penalty is None and plain.conversion is None
    cut_short = PlayText(
        "A.B pass to C.D for 5 yards.The Replay Official reviewed the catch ruling, and the play was REVERSED."
    )
    assert cut_short.standing == cut_short.raw  # nothing after the ruling: the whole text stands


def test_reads_timeouts_flags_and_tries() -> None:
    assert PlayText("Timeout #2 by CIN at 01:52.").timeout == TimeoutCall("CIN", 2, "1:52")
    assert PlayText("(Timeout #3 by PIT.)").timeout == TimeoutCall("PIT", 3)
    assert TimeoutCall("CIN", 3, "12:34").describe() == "CIN #3 at 12:34"
    assert TimeoutCall("PIT", 3).describe() == "PIT #3"
    flagged = PlayText("A.B pass incomplete.PENALTY on PIT-A.Samuel, Defensive Pass Interference, 27 yards - No Play.")
    assert flagged.penalty == ("PIT", "Defensive Pass Interference")
    assert flagged.nullified
    scored = PlayText(
        "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN. E.McPherson extra point is GOOD, Center"
    )
    assert scored.conversion == "E.McPherson extra point is GOOD"


def test_unknown_feed_words_read_as_other() -> None:
    assert FootballPlayType("lateral_fiesta") is FootballPlayType.OTHER
    assert FieldGoalResult("doinked") is FieldGoalResult.OTHER
    assert ConversionResult("good").scored and ConversionResult("success").scored
    assert not ConversionResult("failure").scored
    assert ConversionResult("odd") is ConversionResult.OTHER


@pytest.mark.parametrize(
    ("given", "kind", "tone", "headline", "detail"),
    [
        (
            play(1, "D", "pass_reception", yards_gained=23, first_down=True),
            "play",
            PlayTone.STANDARD,
            "23 YARD PASS, 1ST DOWN",
            "D",
        ),
        (play(1, "D", "rush", yards_gained=0), "play", PlayTone.STANDARD, "RUN, NO GAIN", "D"),
        (play(1, "D", "rush", yards_gained=-2), "play", PlayTone.STANDARD, "RUN, 2 YARD LOSS", "D"),
        (play(1, "D", "rush", yards_gained=1, first_down=True), "play", PlayTone.STANDARD, "1 YARD RUN, 1ST DOWN", "D"),
        (play(1, "D", "rush"), "play", PlayTone.STANDARD, "RUN", "D"),
        (play(1, "D", "pass_reception", yards_gained=0), "play", PlayTone.STANDARD, "PASS, NO GAIN", "D"),
        (play(1, "D", "sack", yards_gained=-7), "play", PlayTone.STANDARD, "7 YARD SACK", "D"),
        (play(1, "D", "sack", yards_gained=0), "play", PlayTone.STANDARD, "SACK", "D"),
        (
            play(1, "A.Rodgers kneels to PIT 21 for -1 yards.", "rush", yards_gained=-1),
            "play",
            PlayTone.STANDARD,
            "KNEEL",
            "A.Rodgers kneels to PIT 21 for -1 yards.",
        ),
        (
            play(1, "J.Burrow spiked the ball to stop the clock.", "pass_incompletion"),
            "play",
            PlayTone.STANDARD,
            "SPIKE",
            "J.Burrow spiked the ball to stop the clock.",
        ),
        (play(1, "D", "sack"), "play", PlayTone.STANDARD, "SACK", "D"),
        (
            play(1, "D", "passing_touchdown", offense="CIN", touchdown=True),
            "play",
            PlayTone.SCORE,
            "TOUCHDOWN CIN",
            "D",
        ),
        (play(1, "D", "rush", touchdown=True, interception=True), "play", PlayTone.TURNOVER, "PICK SIX", "D"),
        (play(1, "D", "rush", touchdown=True, fumble_lost=True), "play", PlayTone.TURNOVER, "FUMBLE RETURN TD", "D"),
        (play(1, "D", "pass_reception", interception=True), "play", PlayTone.TURNOVER, "INTERCEPTION", "D"),
        (play(1, "D", "rush", fumble_lost=True), "play", PlayTone.TURNOVER, "FUMBLE LOST", "D"),
        (
            play(1, "D", "field_goal_good", field_goal=FieldGoalResult.MADE),
            "play",
            PlayTone.SCORE,
            "FIELD GOAL GOOD",
            "D",
        ),
        (play(1, "D", "other", field_goal=FieldGoalResult.MISSED), "play", PlayTone.TURNOVER, "FG NO GOOD", "D"),
        (play(1, "D", "other", field_goal=FieldGoalResult.BLOCKED), "play", PlayTone.TURNOVER, "FG BLOCKED", "D"),
        (
            play(1, "D", "field_goal_missed", field_goal=FieldGoalResult.OTHER),
            "play",
            PlayTone.TURNOVER,
            "FG NO GOOD",
            "D",
        ),
        (play(1, "D", "kickoff"), "play", PlayTone.STANDARD, "KICKOFF", "D"),
        (play(1, "D", "lateral_fiesta"), "play", PlayTone.STANDARD, "PLAY", "D"),
        (
            play(
                1,
                "A.B pass incomplete.PENALTY on PIT-A.C, Offensive Offside, 5 yards - No Play.",
                "penalty",
                penalty=True,
            ),
            "play",
            PlayTone.ALERT,
            "FLAG PIT: OFFENSIVE OFFSIDE",
            "A.B pass incomplete.PENALTY on PIT-A.C, Offensive Offside, 5 yards - No Play.",
        ),
        (
            play(1, "A.B pass incomplete. PENALTY on CIN-J.B, Taunting, 15 yards.", "pass_incompletion", penalty=True),
            "play",
            PlayTone.STANDARD,
            "INCOMPLETE + FLAG",
            "A.B pass incomplete. PENALTY on CIN-J.B, Taunting, 15 yards.",
        ),
        (play(1, "D", "penalty", penalty=True), "play", PlayTone.ALERT, "PENALTY", "D"),
        (play(1, "Timeout #1 by PIT at 01:27.", "timeout"), "timeout", PlayTone.ALERT, "TIMEOUT PIT", "PIT #1 at 1:27"),
        (
            play(1, "Official Timeout at 08:26.", "official_timeout"),
            "tv_timeout",
            PlayTone.STANDARD,
            "TV TIMEOUT",
            None,
        ),
        (
            play(1, "Two-Minute Warning", "two_minute_warning"),
            "two_minute_warning",
            PlayTone.ALERT,
            "TWO-MINUTE WARNING",
            None,
        ),
        (play(1, CHALLENGE_UPHELD, "pass_reception"), "play", PlayTone.ALERT, "CIN CHALLENGE: STANDS", None),
        (play(1, BOOTH_REVERSED, "pass_incompletion"), "play", PlayTone.ALERT, "REVIEW: REVERSED", None),
    ],
)
def test_summarizes_a_play(given: FootballPlay, kind: str, tone: PlayTone, headline: str, detail: str | None) -> None:
    summarized = summarize_play(given, teams={"Cincinnati": "CIN"})
    assert summarized is not None
    event_kind, summary = summarized
    assert (event_kind, summary.tone, summary.headline) == (FootballEventKind(kind), tone, headline)
    if detail is not None or event_kind is not FootballEventKind.PLAY:
        assert summary.detail == detail


def test_feed_markers_are_not_summarized() -> None:
    assert summarize_play(play(1, "END QUARTER 1", "end_period")) is None


def test_names_tries_and_rulings() -> None:
    assert conversion_headline(play(1, "D", extra_point=ConversionResult.GOOD)) == "XP GOOD"
    assert conversion_headline(play(1, "D", extra_point=ConversionResult.FAILED)) == "XP NO GOOD"
    assert conversion_headline(play(1, "D", two_point=ConversionResult.SUCCESS)) == "2-PT GOOD"
    assert conversion_headline(play(1, "D", two_point=ConversionResult.FAILURE)) == "2-PT FAILS"
    assert review_headline(Review("Gotham", "catch", ReviewVerdict.STANDS, None), {}) == "GOTHAM CHALLENGE: STANDS"


def _ledger() -> PlayLedger:
    return PlayLedger(_TEAMS.resolve("PIT"), _TEAMS.resolve("CIN"))


def test_events_arrive_in_play_order_and_a_late_starter_sees_only_the_last() -> None:
    plays = [
        play(3, "S.Perine right guard to PIT 2 for 1 yard (K.Benton).", yards_gained=1),
        play(1, "C.Brown right tackle to CIN 31 for 11 yards (J.Brisker).", yards_gained=11),
        play(2, "END QUARTER 1", "end_period"),
    ]
    caught_up = _ledger().absorb(plays, announce=False)
    assert [event.key for event in caught_up] == ["3:play"]
    events = _ledger().absorb(plays)
    assert [event.key for event in events] == ["1:play", "3:play"]
    assert events[0].players_line == "C.Brown 11 YDS 1 CAR | J.Brisker 1 TKL"
    assert events[0].summary.headline == "11 YARD RUN"


def test_a_correction_is_announced_only_when_it_changes_what_happened() -> None:
    ledger = _ledger()
    ledger.absorb(
        [
            play(
                1,
                "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN.",
                "passing_touchdown",
                touchdown=True,
                yards_gained=2,
            ),
            play(2, BOOTH_REVERSED.split("The Replay")[0], "pass_interception_return", interception=True),
            play(3, "C.Brown up the middle to CIN 41 for 9 yards.", yards_gained=9),
        ]
    )
    assert ledger.line("J.Battle") is not None
    events = ledger.absorb(
        [
            play(
                1,
                "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN. E.McPherson extra point is GOOD, Center-W.Wagner.",
                "passing_touchdown",
                revision=2,
                touchdown=True,
                yards_gained=2,
                extra_point=ConversionResult.GOOD,
            ),
            play(2, BOOTH_REVERSED, "pass_incompletion", revision=2),
            play(3, "C.Brown up the middle to CIN 42 for 10 yards.", revision=2, yards_gained=10),
            play(3, "a stale revision", revision=1),
            play(3, "the same revision again", revision=2),
        ]
    )
    assert [(event.kind, event.summary.headline, event.summary.tone) for event in events] == [
        (FootballEventKind.CONVERSION, "XP GOOD", PlayTone.SCORE),
        (FootballEventKind.REVIEW, "REVIEW: REVERSED", PlayTone.ALERT),
    ]
    assert events[0].summary.detail == "E.McPherson extra point is GOOD"
    assert events[1].players_line == "A.Rodgers 0/1 0 YDS"  # the interception is gone from his line
    assert ledger.line("J.Battle") is None
    line = ledger.line("C.Brown")
    assert line is not None and line.rushing_yards == 10
    missed = ledger.absorb(
        [
            play(4, "D", "passing_touchdown"),
            play(4, "D", "passing_touchdown", revision=2, two_point=ConversionResult.FAILURE),
        ]
    )
    assert [event.kind for event in missed] == [FootballEventKind.PLAY]
    ledger.absorb([play(5, "D", "passing_touchdown"), play(5, "D", revision=2, extra_point=ConversionResult.FAILED)])
    twice = ledger.absorb([play(6, "D"), play(6, "D", revision=2), play(6, "D", revision=3)])
    assert [event.key for event in twice] == ["6:play"]  # a new play corrected in the same read is just new
    again = ledger.absorb([play(1, CHALLENGE_UPHELD, revision=3), play(1, CHALLENGE_UPHELD + " More.", revision=4)])
    assert [event.kind for event in again] == [FootballEventKind.REVIEW]  # corrected twice in a read: once
    assert ledger.absorb([play(5, "D", revision=3, extra_point=ConversionResult.FAILED)]) == []


def test_a_score_a_turnover_or_a_ruling_is_major_and_the_rest_is_routine() -> None:
    ledger = _ledger()
    first = ledger.absorb(
        [
            play(1, "D, TOUCHDOWN.", "rushing_touchdown", touchdown=True),
            play(2, "D", "rush", touchdown=True, interception=True),  # a pick six, toned as a turnover
            play(3, "D", "safety"),
            play(4, "D", "field_goal_good", field_goal=FieldGoalResult.MADE),
            play(5, "D", "other", field_goal=FieldGoalResult.MISSED),
            play(6, "D", "rush", fumble_lost=True),
            play(7, "D", "rush", yards_gained=3),
            play(8, "A.B pass incomplete.PENALTY on PIT-A.C, Offensive Offside, 5 yards - No Play.", "penalty"),
            play(9, "Timeout #1 by PIT at 01:27.", "timeout"),
            play(10, "Official Timeout at 08:26.", "official_timeout"),
            play(11, "Two-Minute Warning", "two_minute_warning"),
            play(12, "D, TOUCHDOWN.", "rushing_touchdown", touchdown=True),
            play(13, BOOTH_REVERSED.split("The Replay")[0], "pass_interception_return", interception=True),
        ]
    )
    assert [event.major for event in first] == [True] * 6 + [False] * 5 + [True] * 2
    assert [event.play for event in first] == list(range(1, 14))  # each names the play it's about
    corrections = ledger.absorb(
        [
            play(
                1,
                "D, TOUCHDOWN. X.K extra point is GOOD.",
                revision=2,
                touchdown=True,
                extra_point=ConversionResult.GOOD,
            ),
            play(
                12,
                "D, TOUCHDOWN. X.K extra point is No Good.",
                revision=2,
                touchdown=True,
                extra_point=ConversionResult.FAILED,
            ),
            play(13, BOOTH_REVERSED, "pass_incompletion", revision=2),
        ]
    )
    assert [(event.kind, event.major, event.play) for event in corrections] == [
        (FootballEventKind.CONVERSION, True, 1),  # points
        (FootballEventKind.CONVERSION, False, 12),  # a missed try changes nothing on the board
        (FootballEventKind.REVIEW, True, 13),  # a ruling
    ]
    assert not dataclasses.replace(first[6], kind=FootballEventKind.INJURY).major
    assert dataclasses.replace(first[6], kind=FootballEventKind.SCORE).major  # points the scoreboard showed


def test_a_play_first_seen_with_a_ruling_is_major_when_it_scored_or_turned_the_ball_over() -> None:
    upheld_touchdown = (
        "C.Brown up the middle for 2 yards, TOUCHDOWN. Pittsburgh challenged the touchdown ruling, and the play"
        " was Upheld. The ruling on the field stands. (Timeout #1.)"
    )
    events = _ledger().absorb(
        [
            play(1, upheld_touchdown, "rushing_touchdown", touchdown=True),
            play(2, BOOTH_REVERSED, "pass_incompletion"),  # an interception, reversed: an incompletion now
            play(3, CHALLENGE_UPHELD, "pass_reception", yards_gained=5),
        ]
    )
    assert [(event.summary.tone, event.summary.headline) for event in events] == [
        (PlayTone.ALERT, "PIT CHALLENGE: STANDS"),  # worded and toned as the ruling
        (PlayTone.ALERT, "REVIEW: REVERSED"),
        (PlayTone.ALERT, "CIN CHALLENGE: STANDS"),
    ]
    assert [event.major for event in events] == [True, False, False]  # only the touchdown stands as a score
    assert not play(4, "END QUARTER 1", "end_period").scored_or_turned_over


def test_each_play_by_play_code_reads_as_the_boards_abbreviation_and_any_other_code_as_written() -> None:
    assert PLAY_BY_PLAY_CODES == {"BLT": "BAL", "CLV": "CLE", "ARZ": "ARI", "HST": "HOU", "LA": "LAR"}
    assert [club_code(code) for code in ("BLT", "CLV", "ARZ", "HST", "LA")] == ["BAL", "CLE", "ARI", "HOU", "LAR"]
    assert [club_code(code) for code in ("CIN", "XYZ")] == ["CIN", "XYZ"]
    text = "A.B pass to C.D to BLT 12 for 3 yards. PENALTY on CLV-E.F, Holding, enforced at ARZ 40 (HST), LA 20."
    assert canonical_play_text(text) == (
        "A.B pass to C.D to BAL 12 for 3 yards. PENALTY on CLE-E.F, Holding, enforced at ARI 40 (HOU), LAR 20."
    )
    untouched = "blt BLTS ABLT HSTN la LAC 20, LAS 5, WSH 30"  # not a whole word in capitals, or not one of the five
    assert canonical_play_text(untouched) == untouched


def test_play_text_names_clubs_by_the_boards_abbreviations_in_headlines_details_and_counts() -> None:
    timeout = summarize_play(play(1, "Timeout #3 by BLT at 01:47.", "timeout"))
    assert timeout is not None and (timeout[1].headline, timeout[1].detail) == ("TIMEOUT BAL", "BAL #3 at 1:47")
    flag = summarize_play(play(2, "A.B pass incomplete.PENALTY on CLV-A.C, Offside, 5 yards - No Play.", "penalty"))
    assert flag is not None and flag[1].headline == "FLAG CLE: OFFSIDE"
    assert flag[1].detail == "A.B pass incomplete.PENALTY on CLE-A.C, Offside, 5 yards - No Play."
    run = summarize_play(play(3, "A.Back up the middle to HST 12 for 3 yards (C.D).", yards_gained=3))
    assert run is not None and run[1].detail == "A.Back up the middle to HOU 12 for 3 yards (C.D)."
    challenged = PlayText(CHALLENGE_UPHELD.replace("Cincinnati", "ARZ")).review
    assert challenged is not None and challenged.challenger == "ARI"

    ledger = PlayLedger(_TEAMS.resolve("BAL"), _TEAMS.resolve("CLE"))
    ledger.absorb(
        [
            play(n, f"Timeout #{n} by BLT at 0{n}:00.", "timeout", quarter=2)
            for n in range(1, 4)  # Baltimore spends all three
        ]
        + [play(4, "Timeout #1 by CLV at 00:40.", "timeout", quarter=2)]
    )
    assert ledger.timeouts(2) == TeamTimeouts(away=0, home=2)
    rekick = play(
        5,
        "X.K kicks 65 yards from BLT 35 to CLV 0. PENALTY on BLT-X.Y, Illegal Formation, 5 yards, enforced at"
        " BLT 35 - No Play.",
        "kickoff",
        offense="CLE",
        quarter=2,
    )
    ledger.absorb([rekick])
    assert ledger.next_kickoff(2) == (HomeAway.AWAY, 30)  # Baltimore kicks again, five yards back


def test_a_failed_try_arriving_late_is_announced_plainly() -> None:
    ledger = _ledger()
    ledger.absorb([play(1, "D, TOUCHDOWN.", "rushing_touchdown")])
    events = ledger.absorb(
        [
            play(
                1,
                "D, TOUCHDOWN. X.Kicker extra point is No Good, Wide Left.",
                "rushing_touchdown",
                revision=2,
                extra_point=ConversionResult.FAILED,
            )
        ]
    )
    assert [(event.summary.headline, event.summary.tone, event.summary.detail) for event in events] == [
        ("XP NO GOOD", PlayTone.STANDARD, "X.Kicker extra point is No Good")
    ]


@pytest.mark.parametrize(
    ("description", "fields", "lines"),
    [
        (
            "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN.",
            {"play_type": "passing_touchdown", "touchdown": True, "yards_gained": 2},
            {"J.Burrow": "J.Burrow 1/1 2 YDS 1 TD", "C.Brown": "C.Brown 2 YDS 1 TD 1 REC"},
        ),
        (
            "A.Rodgers pass incomplete deep right to D.Metcalf (J.Battle).",
            {"play_type": "pass_incompletion"},
            {"A.Rodgers": "A.Rodgers 0/1 0 YDS", "J.Battle": None},
        ),
        (
            "A.Rodgers pass deep left intended for D.Washington INTERCEPTED by J.Battle at CIN 36. J.Battle to CIN 36"
            " for no gain (D.Washington).",
            {"play_type": "pass_interception_return", "interception": True},
            {"A.Rodgers": "A.Rodgers 0/1 0 YDS 1 INT", "J.Battle": "J.Battle 1 INT", "D.Washington": None},
        ),
        (
            "J.Burrow sacked at PIT 45 for -3 yards (P.Queen; T.Watt).",
            {"play_type": "sack", "yards_gained": -3},
            {"P.Queen": "P.Queen 0.5 SCK", "J.Burrow": None},
        ),
        (
            "E.McPherson kicks 61 yards from CIN 35 to PIT 4. K.Wetjen to PIT 31 for 27 yards (T.Brooks).",
            {"play_type": "kickoff"},
            {"K.Wetjen": "K.Wetjen 27 YDS", "T.Brooks": "T.Brooks 1 TKL"},
        ),
        (
            "C.Johnston punts 44 yards to CIN 18. D.Meyers to CIN 18 for no gain (C.Kuntz).",
            {"play_type": "punt"},
            {"D.Meyers": "D.Meyers 0 YDS", "C.Kuntz": "C.Kuntz 1 TKL"},
        ),
        (
            "C.Johnston punts 35 yards to CIN 20, Center-C.Kuntz, fair catch by D.Meyers.",
            {"play_type": "punt"},
            {"D.Meyers": None},
        ),
        (
            "X.Returner punts 50 yards to PIT 10. K.Wetjen to CIN 0 for 90 yards, TOUCHDOWN.",
            {"play_type": "punt_return_touchdown", "touchdown": True},
            {"K.Wetjen": "K.Wetjen 90 YDS 1 TD"},
        ),
        (
            "J.Warren right guard for 4 yards, TOUCHDOWN.",
            {"play_type": "rushing_touchdown", "touchdown": True, "yards_gained": 4},
            {"J.Warren": "J.Warren 4 YDS 1 TD 1 CAR"},
        ),
        (
            "J.Warren right guard to PIT 31 for 2 yards (D.Hill). J.Warren FUMBLES (D.Hill), recovered by PIT-A.B.",
            {"play_type": "fumble_recovery_own", "yards_gained": 2},
            {"J.Warren": "J.Warren 2 YDS 1 CAR 1 FUM", "D.Hill": None},
        ),
        (
            "A.Rodgers pass incomplete.PENALTY on PIT-D.Metcalf, Offensive Offside, 5 yards - No Play.",
            {"play_type": "penalty", "penalty": True},
            {"A.Rodgers": None},
        ),
        ("Timeout #1 by PIT at 01:27.", {"play_type": "timeout"}, {"PIT": None}),
        ("E.McPherson 43 yard field goal is No Good.", {"play_type": "field_goal_missed"}, {"E.McPherson": None}),
    ],
)
def test_credits_players_from_the_play_text(
    description: str, fields: dict[str, Any], lines: dict[str, str | None]
) -> None:
    ledger = _ledger()
    play_type = fields.pop("play_type")
    ledger.absorb([play(1, description, play_type, **fields)])
    for name, expected in lines.items():
        line = ledger.line(name)
        assert (line.describe() if line is not None else None) == expected


def test_a_line_leads_with_what_a_player_mostly_did() -> None:
    passer = PlayerLine(
        "J.Burrow", completions=21, attempts=26, passing_yards=245, passing_touchdowns=2, carries=3, rushing_yards=8
    )
    assert passer.describe() == "J.Burrow 21/26 245 YDS 2 TD 8 RUSH"
    defender = PlayerLine("P.Queen", tackles=9, sacks=1.5, interceptions=1, fumbles=1)
    assert defender.describe() == "P.Queen 9 TKL 1.5 SCK 1 INT 1 FUM"
    assert PlayerLine("N.Body").describe() == "N.Body"


def test_counts_timeouts_by_half_including_one_lost_on_a_challenge() -> None:
    ledger = _ledger()
    ledger.absorb(
        [
            play(1, CHALLENGE_UPHELD, "pass_reception"),
            play(2, "Timeout #2 by CIN at 01:52.", "timeout", quarter=2),
            play(3, "Timeout #1 by PIT at 01:27.", "timeout", quarter=2),
            play(4, "Timeout #3 by XYZ at 01:00.", "timeout", quarter=2),
            play(5, "Gotham challenged the catch ruling, and the play was Upheld. (Timeout #2.)", quarter=2),
            play(6, "Timeout #1 by CIN at 09:00.", "timeout", quarter=3),
            play(7, "Timeout #1 by PIT at 09:00.", "timeout", quarter=5),
        ]
    )
    assert ledger.timeouts(2) == TeamTimeouts(away=2, home=1)
    assert ledger.timeouts(4) == TeamTimeouts(away=3, home=2)
    assert ledger.timeouts(5) == TeamTimeouts(away=1, home=2)  # two apiece in overtime


def test_counts_challenges_and_the_third_earned_by_winning_either_of_the_first_two() -> None:
    ledger = _ledger()
    assert ledger.challenges() == TeamChallenges(away=2, home=2)
    ledger.absorb(
        [
            play(1, CHALLENGE_UPHELD, "pass_reception"),
            play(2, "X. Pittsburgh challenged the catch ruling, and the play was REVERSED.Y.", "pass_incompletion"),
            play(3, "X. Steelers challenged the spot ruling, and the play was REVERSED.Y.", "rush"),
            play(4, BOOTH_REVERSED, "pass_incompletion"),
            play(5, "X. Gotham challenged the spot ruling, and the play was REVERSED.Y.", "rush"),
            play(6, "X. Cincinnati Bengals challenged the catch ruling, and the play was REVERSED.Y.", "rush"),
        ]
    )
    # PIT won both of its first two and has its third left. CIN lost one and won one, which since 2024
    # also earns a third.
    assert ledger.challenges() == TeamChallenges(away=1, home=1)


def test_names_a_club_by_how_play_text_might_say_it() -> None:
    ledger = PlayLedger(_TEAMS.resolve("LAR"), _TEAMS.resolve("SF"))
    ledger.absorb([play(1, "X. LA Rams challenged the catch ruling, and the play was Upheld. (Timeout #1.)")])
    assert ledger.challenges() == TeamChallenges(away=1, home=2)
    assert ledger.timeouts(1) == TeamTimeouts(away=2, home=3)


def _kickoff(number: int, kicker: str, receiver: str, quarter: int = 1) -> FootballPlay:
    description = f"X.Kicker kicks 65 yards from {kicker} 35 to {receiver} 0. Touchback."
    return play(number, description, "kickoff", offense=receiver, quarter=quarter)


def test_the_team_that_kicked_off_the_game_receives_the_second_half() -> None:
    ledger = _ledger()
    assert ledger.second_half_receiver() is None  # no kickoff yet
    ledger.absorb([_kickoff(5, "CIN", "PIT", quarter=2), _kickoff(1, "PIT", "CIN")])
    assert ledger.second_half_receiver() is HomeAway.AWAY  # PIT kicked off the game
    unknown = _ledger()
    unknown.absorb([play(1, "X.Kicker kicks 65 yards.", "kickoff")])
    assert unknown.second_half_receiver() is None


def _scored(play_type: str, **fields: Any) -> FootballPlay:
    return play(1, "D, TOUCHDOWN.", play_type, touchdown=True, **fields)


@pytest.mark.parametrize(
    ("plays", "period", "expected"),
    [
        ([], 1, (None, None)),  # the toss decides the opening kickoff
        ([], 5, (None, None)),  # and overtime's
        ([], 2, None),  # the second quarter carries on from the first
        ([_kickoff(1, "PIT", "CIN")], 3, (HomeAway.HOME, 35)),  # PIT kicked the game off, so CIN kicks the second half
        ([], 3, (None, None)),
        ([_scored("passing_touchdown", offense="CIN")], 1, None),  # the try comes first
        ([_scored("passing_touchdown", offense="CIN", extra_point=ConversionResult.GOOD)], 1, (HomeAway.HOME, 35)),
        (
            [_scored("pass_interception_return", offense="CIN", interception=True, extra_point=ConversionResult.GOOD)],
            1,
            (HomeAway.AWAY, 35),  # PIT's defense scored, so PIT kicks
        ),
        ([_scored("punt_return_touchdown", offense="CIN", extra_point=ConversionResult.GOOD)], 1, (HomeAway.AWAY, 35)),
        ([_scored("rushing_touchdown", extra_point=ConversionResult.GOOD)], 1, (None, None)),
        ([play(1, "D", "field_goal_good", offense="PIT", field_goal=FieldGoalResult.MADE)], 2, (HomeAway.AWAY, 35)),
        ([play(1, "D", "field_goal_good", offense="PIT")], 2, (HomeAway.AWAY, 35)),
        (
            [play(1, "D", "safety", offense="PIT")],
            4,
            (HomeAway.AWAY, 20),
        ),  # PIT, caught in its end zone, kicks from its 20
        ([play(1, "D", "safety")], 4, (None, None)),
        ([play(1, "D", "rush", offense="PIT"), play(2, "Timeout #1 by PIT.", "timeout")], 4, None),
        ([_kickoff(1, "PIT", "CIN")], 1, None),  # the kickoff has happened
    ],
)
def test_knows_when_a_kickoff_is_next_and_who_kicks_it(
    plays: list[FootballPlay], period: int, expected: tuple[HomeAway | None, int | None] | None
) -> None:
    ledger = _ledger()
    in_period = [
        p if p.play_type is FootballPlayType.KICKOFF else dataclasses.replace(p, quarter=period) for p in plays
    ]
    ledger.absorb(in_period)
    assert ledger.next_kickoff(period) == expected


@pytest.mark.parametrize(
    ("scoring", "points"),
    [
        (
            play(
                1,
                "D, TOUCHDOWN.",
                "passing_touchdown",
                offense="CIN",
                touchdown=True,
                extra_point=ConversionResult.GOOD,
            ),
            (0, 7),
        ),
        (
            play(
                1,
                "D, TOUCHDOWN.",
                "rushing_touchdown",
                offense="CIN",
                touchdown=True,
                two_point=ConversionResult.SUCCESS,
            ),
            (0, 8),
        ),
        (
            play(1, "D, TOUCHDOWN.", "pass_interception_return", offense="CIN", touchdown=True, interception=True),
            (6, 0),
        ),
        (play(1, "D", "field_goal_good", offense="PIT", field_goal=FieldGoalResult.MADE), (3, 0)),
        (play(1, "D", "safety", offense="PIT"), (0, 2)),  # the defense's points
        (play(1, "X.Kicker extra point is GOOD.", "other", offense="CIN", extra_point=ConversionResult.GOOD), (0, 1)),
        (
            play(
                1,
                "D, TOUCHDOWN. PENALTY on CIN-X, Holding, 10 yards - No Play.",
                "penalty",
                offense="CIN",
                touchdown=True,
            ),
            (0, 0),
        ),
        (play(1, "D", "rush", offense="CIN", yards_gained=4), (0, 0)),
        (play(1, "D, TOUCHDOWN.", "rushing_touchdown", touchdown=True), (0, 0)),  # no team named
    ],
)
def test_counts_the_points_each_sides_plays_account_for(scoring: FootballPlay, points: tuple[int, int]) -> None:
    ledger = _ledger()
    assert ledger.latest == -1
    ledger.absorb([scoring])
    assert (ledger.points(HomeAway.AWAY), ledger.points(HomeAway.HOME)) == points
    assert ledger.latest == 1


def test_knows_when_a_touchdown_still_owes_its_try() -> None:
    ledger = _ledger()
    assert not ledger.try_pending(HomeAway.HOME)
    ledger.absorb([play(1, "D, TOUCHDOWN.", "passing_touchdown", offense="CIN", touchdown=True)])
    assert ledger.try_pending(HomeAway.HOME) and not ledger.try_pending(HomeAway.AWAY)
    ledger.absorb(
        [
            play(
                1,
                "D, TOUCHDOWN.",
                "passing_touchdown",
                revision=2,
                offense="CIN",
                touchdown=True,
                extra_point=ConversionResult.GOOD,
            )
        ]
    )
    assert not ledger.try_pending(HomeAway.HOME)


def test_an_eligibility_report_before_a_play_is_not_the_play() -> None:
    text = "J.Bayer reported in as eligible.  (Shotgun) S.Perine right tackle for 1 yard, TOUCHDOWN."
    assert PlayText(text).standing == "S.Perine right tackle for 1 yard, TOUCHDOWN."
    ledger = _ledger()
    ledger.absorb([play(1, text, "rushing_touchdown", offense="CIN", touchdown=True, yards_gained=1)])
    line = ledger.line("S.Perine")
    assert line is not None and line.describe() == "S.Perine 1 YDS 1 TD 1 CAR"
    assert ledger.line("J.Bayer") is None


_KICKED = "E.McPherson kicks 65 yards from CIN 35 to PIT 0."
_SCORED_WITH = "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN. E.McPherson extra point is GOOD. "


@pytest.mark.parametrize(
    ("last", "expected"),
    [
        (
            play(
                1,
                _KICKED + " PENALTY on CIN-X.Y, Offside on Free Kick, 5 yards, enforced at CIN 35 - No Play.",
                "kickoff",
                offense="PIT",
            ),
            (HomeAway.HOME, 30),
        ),
        (
            play(
                1,
                _KICKED + " PENALTY on PIT-X.Y, Illegal Formation, 5 yards, enforced at CIN 35 - No Play.",
                "kickoff",
                offense="PIT",
            ),
            (HomeAway.HOME, 40),
        ),
        (
            play(
                1,
                _KICKED + " PENALTY on PIT-X.Y, Illegal Formation, 5 yards, enforced at PIT 45 - No Play.",
                "kickoff",
                offense="PIT",
            ),
            (HomeAway.HOME, 60),
        ),
        (
            play(
                1, _KICKED + " PENALTY on CIN-X.Y, Offside on Free Kick, 5 yards - No Play.", "kickoff", offense="PIT"
            ),
            (HomeAway.HOME, None),  # a penalty moved it, but from where isn't said: no spot rather than a guess
        ),
        (
            play(
                1,
                "X kicks. PENALTY on CIN-X.Y, Offside on Free Kick, 5 yards, enforced at CIN 35 - No Play.",
                "kickoff",
            ),
            (None, None),
        ),
        (
            play(
                1,
                _SCORED_WITH + "PENALTY on PIT-Z.Z, Unnecessary Roughness, 15 yards, enforced on the kickoff.",
                "passing_touchdown",
                offense="CIN",
                touchdown=True,
                extra_point=ConversionResult.GOOD,
            ),
            (HomeAway.HOME, 50),
        ),
        (
            play(
                1,
                _SCORED_WITH + "PENALTY on CIN-W.W, Unsportsmanlike Conduct, 15 yards, enforced on the kickoff.",
                "passing_touchdown",
                offense="CIN",
                touchdown=True,
                extra_point=ConversionResult.GOOD,
            ),
            (HomeAway.HOME, 20),
        ),
        (
            play(
                1,
                _SCORED_WITH + "PENALTY on PIT-Z.Z, Defensive Holding, 5 yards, enforced at PIT 2.",
                "passing_touchdown",
                offense="CIN",
                touchdown=True,
                extra_point=ConversionResult.GOOD,
            ),
            (HomeAway.HOME, 35),
        ),
        (
            play(
                1,
                "D. PENALTY on CIN-W.W, Unsportsmanlike Conduct, 15 yards, enforced on the kickoff.",
                "safety",
                offense="CIN",
            ),
            (HomeAway.HOME, 5),
        ),
    ],
)
def test_a_penalty_moves_the_kickoff(last: FootballPlay, expected: tuple[HomeAway | None, int | None]) -> None:
    ledger = _ledger()
    ledger.absorb([dataclasses.replace(last, quarter=2)])
    assert ledger.next_kickoff(2) == expected
