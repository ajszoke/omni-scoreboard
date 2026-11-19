"""The NBA pregame card's rules: the tip lines, the odds' spread, the standings, on each board."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from omni.cards.basketball import BasketballPregameCardPayload
from omni.core.enum import GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import WinLossRecord
from omni.domain.basketball import BasketballPhase, BySide, FavoriteLine, StandingRanks
from omni.domain.contest import GolfTournament, TeamGame
from omni.domain.teams import BasketballTeam
from omni.panels.geometry import geometry_for
from omni.renderers.basketball_pregame import BasketballPregameRenderer, Tip, TipLines, tip_lines
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from tests.basketball_fixtures import ZONE, nba_game, pregame_card

PROFILES = sorted(BasketballPregameRenderer.supported_profiles)
QUAD, STACK, SINGLE = PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32
TIP = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)  # 6:00 PM in Denver
NOON = datetime(2026, 11, 17, 19, 0, tzinfo=timezone.utc)  # 12:00 PM in Denver
_SIX = Tip("6:00 PM", time=True)


@pytest.mark.parametrize(
    "before, lower",
    [
        (timedelta(hours=1, minutes=23), "1h 23m"),
        (timedelta(hours=2), "2h 0m"),
        (timedelta(minutes=45, seconds=30), "45m"),
        (timedelta(minutes=1), "1m"),
        (timedelta(seconds=59), "SOON"),  # inside a minute
        (-timedelta(minutes=3), "SOON"),  # past the tip, not yet under way
    ],
)
def test_a_tip_later_today_reads_its_time_over_a_countdown(before: timedelta, lower: str) -> None:
    assert tip_lines(TIP - before, TIP, ZONE) == TipLines(_SIX, Tip(lower))


def test_a_tip_on_another_day_reads_its_date_over_its_time() -> None:
    now = datetime(2026, 11, 16, 23, 37, tzinfo=timezone.utc)  # 4:37 PM the day before, in Denver
    assert tip_lines(now, NOON, ZONE) == TipLines(Tip("11/17"), Tip("12:00 PM", time=True))
    assert tip_lines(TIP - timedelta(hours=1), TIP, timezone.utc).upper == Tip("1:00 AM", time=True)  # its own zone


def _payload(**changes: object) -> BasketballPregameCardPayload:
    values: dict[str, object] = {
        "phase": BasketballPhase.SCHEDULED,
        "tip": TIP,
        "records": BySide(WinLossRecord(wins=6, losses=8), WinLossRecord(wins=9, losses=5)),
        "line": FavoriteLine(favorite=HomeAway.HOME, moneyline=-265, spread=-6.5),
    }
    return BasketballPregameCardPayload(**{**values, **changes})  # type: ignore[arg-type]


def _texts(payload: BasketballPregameCardPayload, profile: PanelProfile, *, now: datetime = TIP) -> list[str]:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    context = RenderContext(profile=profile, now=now - timedelta(hours=1, minutes=23), timezone=ZONE)
    BasketballPregameRenderer().render(pregame_card(payload), context, canvas)
    return [op.text for op in canvas.texts()]


# The words each board shows for the tip and its label, label first; the single's scheduled tip has no label.
_TODAY = {
    QUAD: ["TIP", "6:00", "PM", "1h 23m"],
    STACK: ["TIP", "6:00", "PM", "1h 23m"],
    SINGLE: ["6:00", "PM", "1h 23m"],
}
_NEW_TIP = {
    QUAD: ["PPD", "11/17", "12:00", "PM"],
    STACK: ["PPD", "11/17", "12:00", "PM"],
    SINGLE: ["PPD", "11/17", "12:00", "PM"],
}
_NO_TIP = {QUAD: ["PPD", "TIP TBD"], STACK: ["PPD", "TIP TBD"], SINGLE: ["PPD", "TIP TBD"]}


@pytest.mark.parametrize("profile", PROFILES)
def test_each_board_reads_the_tip_today_and_a_postponed_game_with_and_without_a_new_one(profile: PanelProfile) -> None:
    def shown(texts: list[str], words: list[str]) -> bool:
        return [text for text in texts if text in words] == words

    assert shown(_texts(_payload(), profile), _TODAY[profile])
    day_before = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)  # 4:37 PM on the 16th, after the shift below
    moved = _payload(phase=BasketballPhase.POSTPONED, tip=NOON, line=None)
    assert shown(_texts(moved, profile, now=day_before), _NEW_TIP[profile])
    assert shown(_texts(_payload(phase=BasketballPhase.POSTPONED, tip=None, line=None), profile), _NO_TIP[profile])


_LONG_LINE = FavoriteLine(favorite=HomeAway.HOME, moneyline=-650, spread=-12.5)


def test_the_quads_odds_drop_their_spread_when_it_would_crowd_the_tip_beside_them() -> None:
    assert "MIN -265 (-6.5)" in _texts(_payload(), QUAD)  # one blank column before 6:00 PM
    assert "MIN -650 (-12.5)" in _texts(_payload(line=_LONG_LINE), QUAD)  # 6:00 PM leaves room
    noon = _texts(_payload(tip=NOON, line=_LONG_LINE), QUAD, now=NOON)
    assert "MIN -650" in noon and "MIN -650 (-12.5)" not in noon  # 12:00 PM doesn't
    no_tip = _payload(phase=BasketballPhase.POSTPONED, tip=None)
    assert "MIN -265 (-6.5)" in _texts(no_tip, QUAD)  # TIP TBD leaves it room
    assert "MIN -650" in _texts(dataclasses.replace(no_tip, line=_LONG_LINE), QUAD)  # but not the long one


@pytest.mark.parametrize("profile", PROFILES)
def test_with_no_records_or_standings_the_card_shows_neither(profile: PanelProfile) -> None:
    texts = _texts(_payload(records=BySide(None, None)), profile)
    assert "6-8" not in texts and "9-5" not in texts


def _standing(team: BasketballTeam, ranks: StandingRanks) -> list[str]:
    game = nba_game()
    contest = TeamGame(
        id=game.id,
        league=League.NBA,
        status=GameStatus.SCHEDULED,
        scheduled_start=TIP,
        away=team,
        home=game.home,
    )
    card = dataclasses.replace(pregame_card(_payload(ranks=BySide(ranks, None))), contest=contest)
    canvas = RecordingCanvas(128, 64)
    BasketballPregameRenderer().render(card, RenderContext(profile=QUAD, now=TIP, timezone=ZONE), canvas)
    return [op.text for op in canvas.texts() if op.font == "5x7"]


def test_a_standing_names_the_conference_and_the_division_as_far_as_theyre_known() -> None:
    portland = nba_game().away
    assert isinstance(portland, BasketballTeam)
    ranks = StandingRanks(conference=11, division=4)
    assert _standing(portland, ranks) == ["11th West, 4th NW"]
    boston = nba_game("ORL", "BOS").home
    assert isinstance(boston, BasketballTeam)
    assert _standing(boston, StandingRanks(conference=2, division=1)) == ["2nd East, 1st Atl."]  # not Atlanta
    assert _standing(dataclasses.replace(portland, division="Midwest"), ranks) == ["11th West"]
    assert _standing(dataclasses.replace(portland, conference=None, division=None), ranks) == ["11th"]


@pytest.mark.parametrize("profile", PROFILES)
def test_a_non_team_contest_is_refused(profile: PanelProfile) -> None:
    golf = GolfTournament(
        id=LeagueScopedId(League.PGA, SourceRef("x"), "t"),
        league=League.PGA,
        status=GameStatus.SCHEDULED,
        scheduled_start=TIP,
        tournament_name="Open",
    )
    card = dataclasses.replace(pregame_card(_payload()), contest=golf)
    with pytest.raises(TypeError):
        BasketballPregameRenderer().render(card, RenderContext(profile=profile, now=TIP), RecordingCanvas(128, 64))
