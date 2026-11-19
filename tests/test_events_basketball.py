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
    FreeThrows,
    PersonalFoul,
    ShotValue,
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
