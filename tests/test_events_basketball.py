"""Basketball's captions: what each event accepts, and its words on the board."""

from __future__ import annotations

from typing import Any

import pytest

from omni.core.enum import HomeAway
from omni.domain.basketball import BySide, CallChange, PlayerCallChange
from omni.domain.football import PlayTone
from omni.events.basketball import (
    CallReversed,
    CaptionLines,
    CaptionWords,
    ChallengeWon,
    FieldGoalMade,
    FreeThrow,
    FreeThrows,
    PersonalFoul,
    ShotValue,
    TechnicalFoul,
    TimeoutTaken,
)

A, H = HomeAway.AWAY, HomeAway.HOME
CODES = BySide("POR", "MIN")


def test_a_made_basket_names_its_value_and_its_assister() -> None:
    three = FieldGoalMade(side=A, shooter="S.Sharpe", value=ShotValue.THREE, assister="D.Avdija")
    assert three.words(CODES) == CaptionWords(
        tone=PlayTone.SCORE, headline="3-POINT FG GOOD", detail="S.Sharpe (D.Avdija asst.)"
    )
    two = FieldGoalMade(side=H, shooter="A.Edwards", value=ShotValue.TWO)
    assert two.words(CODES) == CaptionWords(tone=PlayTone.SCORE, headline="2-POINT FG GOOD", detail="A.Edwards")


@pytest.mark.parametrize("names", [("", None), ("A.Edwards", "")])
def test_a_made_basket_names_real_players(names: tuple[str, str | None]) -> None:
    with pytest.raises(ValueError):
        FieldGoalMade(side=H, shooter=names[0], value=ShotValue.TWO, assister=names[1])


@pytest.mark.parametrize(
    "made, attempted, headline, tone",
    [
        (2, 2, "FREE THROWS GOOD", PlayTone.SCORE),
        (1, 1, "FREE THROW GOOD", PlayTone.SCORE),
        (1, 2, "1 OF 2 FREE THROWS", PlayTone.SCORE),
        (2, 3, "2 OF 3 FREE THROWS", PlayTone.SCORE),
        (0, 2, "FREE THROWS MISSED", PlayTone.STANDARD),
        (0, 1, "FREE THROW MISSED", PlayTone.STANDARD),
    ],
)
def test_free_throws_say_how_many_went_in(made: int, attempted: int, headline: str, tone: PlayTone) -> None:
    trip = FreeThrows(side=H, shooter="A.Edwards", made=made, attempted=attempted, game_made=6, game_attempted=9)
    assert trip.words(CODES) == CaptionWords(tone=tone, headline=headline, detail="A.Edwards (6/9 FT)")


_TRIP: dict[str, Any] = {
    "side": H,
    "shooter": "A.Edwards",
    "made": 2,
    "attempted": 2,
    "game_made": 6,
    "game_attempted": 6,
}


@pytest.mark.parametrize(
    "changes",
    [
        {"shooter": ""},
        {"attempted": 0, "made": 0},
        {"made": 3},
        {"made": -1},
        {"game_made": 1},  # fewer for the game than on the trip
        {"game_made": 7},  # more makes than attempts for the game
        {"made": 0, "game_made": 6},  # two misses on the trip, none for the game
    ],
    ids=str,
)
def test_free_throws_refuse_what_doesnt_add_up(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        FreeThrows(**{**_TRIP, **changes})


@pytest.mark.parametrize(
    "place, made, headline, result, tone",
    [
        (1, True, "FREE THROW 1 GOOD", "GOOD 6/7 FT", PlayTone.SCORE),
        (2, False, "FREE THROW 2 MISSED", "MISSED 6/7 FT", PlayTone.STANDARD),
        (3, True, "FREE THROW 3 GOOD", "GOOD 6/7 FT", PlayTone.SCORE),
    ],
)
def test_a_free_throw_says_its_place_and_result_but_never_the_trips_size(
    place: int, made: bool, headline: str, result: str, tone: PlayTone
) -> None:
    shot = FreeThrow(side=H, shooter="A.Edwards", place=place, made=made, game_made=6, game_attempted=7)
    assert shot.words(CODES) == CaptionWords(
        tone=tone,
        headline=headline,
        detail="A.Edwards (6/7 FT)",
        narrow=(CaptionLines(f"FREE THROW {place}", "A.Edwards"), CaptionLines(f"FREE THROW {place}", result)),
    )
    assert "OF" not in headline


_SHOT: dict[str, Any] = {
    "side": H,
    "shooter": "A.Edwards",
    "place": 1,
    "made": True,
    "game_made": 6,
    "game_attempted": 7,
}


@pytest.mark.parametrize(
    "changes",
    [
        {"shooter": ""},
        {"place": 0},
        {"game_attempted": 0, "game_made": 0},
        {"game_made": -1},
        {"game_made": 8},  # more makes than attempts for the game
        {"game_made": 0},  # it went in, so the game has a make
        {"made": False, "game_made": 7},  # it missed, so the game has a miss
    ],
    ids=str,
)
def test_a_free_throw_refuses_a_game_line_that_leaves_it_out(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        FreeThrow(**{**_SHOT, **changes})


@pytest.mark.parametrize("made, result", [(True, "GOOD"), (False, "MISSED")])
def test_a_technical_names_the_side_charged_and_the_shooter_but_no_one_charged(made: bool, result: str) -> None:
    technical = TechnicalFoul(side=A, shooter="A.Edwards", made=made, game_made=5, game_attempted=6)
    assert technical.words(CODES) == CaptionWords(
        tone=PlayTone.ALERT,
        headline="TECHNICAL: POR",
        detail=f"A.Edwards FT {result} (5/6)",
        narrow=(
            CaptionLines("TECHNICAL: POR", "A.Edwards FT"),
            CaptionLines("TECHNICAL: POR", f"{result} 5/6 FT"),
        ),
    )
    assert (
        TechnicalFoul(side=H, shooter="A.George", made=True, game_made=1, game_attempted=1)
        .words(BySide("UTA", "LAL"))
        .headline
        == "TECHNICAL: LAL"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"shooter": ""},
        {"game_attempted": 0, "game_made": 0},
        {"game_made": 7},
        {"game_made": 0},
        {"made": False, "game_made": 6},
    ],
    ids=str,
)
def test_a_technical_refuses_a_game_line_that_leaves_its_free_throw_out(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TechnicalFoul(
            **{"side": A, "shooter": "A.Edwards", "made": True, "game_made": 5, "game_attempted": 6, **changes}
        )


@pytest.mark.parametrize("count, detail", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (6, "6th")])
def test_a_foul_names_the_players_count(count: int, detail: str) -> None:
    foul = PersonalFoul(side=A, player="D.Clingan", count=count)
    assert foul.words(CODES) == CaptionWords(
        tone=PlayTone.ALERT, headline="PERSONAL FOUL", detail=f"D.Clingan ({detail} foul)"
    )


@pytest.mark.parametrize("changes", [{"count": 0}, {"count": 7}, {"player": ""}], ids=str)
def test_a_foul_refuses_an_impossible_count(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PersonalFoul(**{"side": A, "player": "D.Clingan", "count": 2, **changes})


def test_a_timeout_names_the_team_and_whats_left() -> None:
    assert TimeoutTaken(side=A, left=4).words(CODES) == CaptionWords(
        tone=PlayTone.ALERT, headline="TIMEOUT POR", detail="4 LEFT"
    )
    assert TimeoutTaken(side=H, left=0).words(CODES).headline == "TIMEOUT MIN"


@pytest.mark.parametrize("left", [-1, 7])
def test_a_timeout_leaves_no_more_than_six(left: int) -> None:
    with pytest.raises(ValueError):
        TimeoutTaken(side=A, left=left)


def test_a_won_challenge_reverses_the_call_and_names_the_change_when_a_line_shows_one() -> None:
    plain = ChallengeWon(side=A).words(CODES)
    first = CaptionLines("POR CHALLENGE", "CALL REVERSED")
    assert plain == CaptionWords(
        tone=PlayTone.ALERT, headline="POR CHALLENGE WON", detail="CALL REVERSED", narrow=(first, first)
    )
    block = PlayerCallChange(change=CallChange.FOUL_TO_BLOCK, player="T.Camara")
    assert ChallengeWon(side=A, change=block).words(CODES) == CaptionWords(
        tone=PlayTone.ALERT,
        headline="POR CHALLENGE WON",
        detail="BLOCK, NOT FOUL: T.Camara",
        narrow=(first, CaptionLines("BLOCK, NOT FOUL", "T.Camara")),
    )


@pytest.mark.parametrize(
    "change, detail, words",
    [
        (CallChange.FOUL_OFF, "FOUL OFF J.Randle", "FOUL OFF"),
        (CallChange.FOUL_TO_BLOCK, "BLOCK, NOT FOUL: J.Randle", "BLOCK, NOT FOUL"),
        (CallChange.OFFENSIVE_FOUL_OFF, "OFFENSIVE FOUL OFF: J.Randle", "OFFENSIVE FOUL OFF"),
    ],
)
def test_a_reversal_with_no_challenge_names_no_one_who_asked(change: CallChange, detail: str, words: str) -> None:
    reversal = CallReversed(change=PlayerCallChange(change=change, player="J.Randle"))
    assert reversal.side is None
    assert reversal.words(CODES) == CaptionWords(
        tone=PlayTone.ALERT,
        headline="CALL REVERSED",
        detail=detail,
        narrow=(CaptionLines("CALL REVERSED", words), CaptionLines("CALL REVERSED", "J.Randle")),
    )


@pytest.mark.parametrize("value, points", [(ShotValue.TWO, 2), (ShotValue.THREE, 3)])
def test_a_shot_counts_its_points(value: ShotValue, points: int) -> None:
    assert value.points == points
