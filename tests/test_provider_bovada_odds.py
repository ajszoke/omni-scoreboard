"""Bovada's pregame odds: reading the coupon, matching it to a game, and refreshing at its own pace."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.core.enum import GameStatus, HomeAway, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BettingLine, GameLines
from omni.domain.basketball import FavoriteLine
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.bovada_odds import (
    BovadaCoupon,
    BovadaOddsProvider,
    _american,
    _handicap,
    _parse_coupon,
    favorite_line,
)
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.nba_teams import NbaTeamRegistry

REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi")
START = datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc)
NOW = START - timedelta(hours=2)
AWAY_NAME, HOME_NAME = "Colorado Rockies", "Los Angeles Dodgers"


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.SCHEDULED,
        scheduled_start=START,
        away=REG.resolve(115, full_name=AWAY_NAME),
        home=REG.resolve(119, full_name=HOME_NAME),
    )


def _outcome(side: str, american: str, handicap: str | None = None, status: str = "O") -> dict[str, Any]:
    price = {"american": american}
    if handicap is not None:
        price["handicap"] = handicap
    return {"type": side, "status": status, "price": price}


def _event(
    *,
    away: str = AWAY_NAME,
    home: str = HOME_NAME,
    start: datetime = START,
    away_ml: str = "+165",
    home_ml: str = "-195",
    away_status: str = "O",
    market_status: str = "O",
) -> dict[str, Any]:
    # The shape of a real coupon's event, as served for a live baseball board.
    return {
        "startTime": int(start.timestamp() * 1000),
        "competitors": [{"name": home, "home": True}, {"name": away, "home": False}],
        "displayGroups": [
            {
                "markets": [
                    {
                        "descriptionKey": "Main Dynamic Asian Runline",
                        "status": "O",
                        "outcomes": [_outcome("A", "-130", "1.5"), _outcome("H", "+110", "-1.5")],
                    },
                    {
                        "descriptionKey": "Head To Head",
                        "status": market_status,
                        "outcomes": [_outcome("A", away_ml, status=away_status), _outcome("H", home_ml)],
                    },
                    {"descriptionKey": "Main Dynamic Over/Under", "status": "O", "outcomes": []},
                ]
            }
        ],
    }


def _coupon(*events: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"events": list(events)}]


class _Fetch:
    def __init__(self, coupon: Any) -> None:
        self.coupon = coupon
        self.calls = 0
        self.down = False

    def __call__(self) -> Any:
        self.calls += 1
        if self.down:
            raise OSError("connection refused")
        return self.coupon


def _lines(*events: dict[str, Any]) -> GameLines | None:
    return BovadaOddsProvider(fetch=lambda: _coupon(*events)).lines_for(_game(), NOW)


def test_each_side_gets_its_moneyline_and_run_line() -> None:
    assert _lines(_event()) == GameLines(
        away=BettingLine(moneyline=165, runline=1.5, runline_price=-130),
        home=BettingLine(moneyline=-195, runline=-1.5, runline_price=110),
    )


def test_a_game_not_on_the_board_has_no_lines() -> None:
    assert _lines() is None
    assert _lines(_event(away="New York Yankees")) is None


def test_clubs_meeting_again_tomorrow_arent_todays_game() -> None:
    assert _lines(_event(start=START + timedelta(days=1))) is None


def test_a_doubleheader_takes_the_game_nearest_its_start() -> None:
    assert _lines(_event(start=START + timedelta(hours=5), away_ml="+200"), _event(away_ml="+165")) is not None
    near = _lines(_event(start=START + timedelta(hours=5), away_ml="+200"), _event(away_ml="+165"))
    assert near is not None and near.away is not None and near.away.moneyline == 165


def test_names_match_past_case_and_punctuation_and_fall_back_to_the_nickname() -> None:
    assert _lines(_event(away="COLORADO ROCKIES")) is not None
    assert _lines(_event(home="L.A. Dodgers")) is not None


def test_a_suspended_price_is_no_price() -> None:
    line = _lines(_event(away_status="S"))
    assert line is not None and line.away is None and line.home is not None
    both = _lines(_event(market_status="S"))
    assert both is not None and both.away is None and both.home is None


def test_even_money_reads_as_plus_100() -> None:
    line = _lines(_event(away_ml="EVEN"))
    assert line is not None and line.away is not None and line.away.moneyline == 100


def test_a_moneyline_without_a_run_line_is_still_a_line() -> None:
    event = _event()
    event["displayGroups"][0]["markets"].pop(0)
    line = _lines(event)
    assert line is not None and line.away == BettingLine(moneyline=165)


def test_an_unreadable_game_is_left_off_rather_than_failing_the_rest() -> None:
    assert _parse_coupon(_coupon({"competitors": []}, {"competitors": [{"name": "x", "home": True}]})) == ()
    no_groups = _event()
    no_groups["displayGroups"] = []
    parsed = _parse_coupon(_coupon(no_groups))
    assert len(parsed) == 1 and parsed[0].lines == GameLines()


def test_an_empty_coupon_is_an_empty_board() -> None:
    # What the endpoint serves when no games are posted, such as all winter.
    assert BovadaOddsProvider(fetch=lambda: []).lines_for(_game(), NOW) is None


def test_it_refreshes_at_its_own_pace() -> None:
    fetch = _Fetch(_coupon(_event()))
    provider = BovadaOddsProvider(fetch=fetch, refresh_every=DurationSeconds(300))
    provider.lines_for(_game(), NOW)
    provider.lines_for(_game(), NOW + timedelta(seconds=299))
    assert fetch.calls == 1
    provider.lines_for(_game(), NOW + timedelta(seconds=300))
    assert fetch.calls == 2


def test_a_failed_refresh_raises_keeps_the_last_board_and_waits_out_the_interval() -> None:
    fetch = _Fetch(_coupon(_event()))
    provider = BovadaOddsProvider(fetch=fetch, refresh_every=DurationSeconds(300))
    assert provider.lines_for(_game(), NOW) is not None
    fetch.down = True
    with pytest.raises(ProviderError, match="Bovada odds fetch failed"):
        provider.lines_for(_game(), NOW + timedelta(seconds=300))
    assert provider.lines_for(_game(), NOW + timedelta(seconds=301)) is not None
    assert fetch.calls == 2


def test_a_coupon_of_the_wrong_shape_is_a_failed_refresh() -> None:
    with pytest.raises(ProviderError):
        BovadaOddsProvider(fetch=lambda: {"unexpected": True}).lines_for(_game(), NOW)


@pytest.mark.parametrize("raw, value", [("-120", -120), ("+110", 110), ("EVEN", 100), ("n/a", None), (None, None)])
def test_american_odds_parse(raw: Any, value: int | None) -> None:
    assert _american(raw) == value


@pytest.mark.parametrize("raw, value", [("1.5", 1.5), ("-1.5", -1.5), ("n/a", None), (None, None)])
def test_handicaps_parse(raw: Any, value: float | None) -> None:
    assert _handicap(raw) == value


# --- NBA: its own coupon, its point spread, and the favorite's line -------------------------------------------------

_NBA = NbaTeamRegistry.default()


def _nba_game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.NBA, SourceRef("balldontlie"), "21718141"),
        league=League.NBA,
        status=GameStatus.SCHEDULED,
        scheduled_start=START,
        away=_NBA.resolve("UTA"),
        home=_NBA.resolve("LAL"),
    )


def _nba_event(
    away_ml: str = "+210", home_ml: str = "-260", spreads: tuple[str, str] = ("6.5", "-6.5")
) -> dict[str, Any]:
    """A synthetic NBA event shaped like MLB's coupon, the point spread where MLB's run line is."""
    event = _event(away="Utah Jazz", home="Los Angeles Lakers", away_ml=away_ml, home_ml=home_ml)
    spread, moneyline, total = event["displayGroups"][0]["markets"]
    spread["descriptionKey"] = "Main Dynamic Asian Handicap"
    spread["outcomes"] = [_outcome("A", "-110", spreads[0]), _outcome("H", "-110", spreads[1])]
    return event


def test_nba_s_coupon_is_its_own_and_its_point_spread_reads_where_mlb_s_run_line_does() -> None:
    assert BovadaCoupon.NBA.url.startswith(
        "https://www.bovada.lv/services/sports/event/coupon/events/A/description/basketball/nba?"
    )
    assert "/baseball/mlb?" in BovadaCoupon.MLB.url
    provider = BovadaOddsProvider(fetch=_Fetch(_coupon(_nba_event())), coupon=BovadaCoupon.NBA)
    lines = provider.lines_for(_nba_game(), NOW)
    assert lines == GameLines(
        away=BettingLine(moneyline=210, runline=6.5, runline_price=-110),
        home=BettingLine(moneyline=-260, runline=-6.5, runline_price=-110),
    )
    assert favorite_line(lines) == FavoriteLine(favorite=HomeAway.HOME, moneyline=-260, spread=-6.5)
    mlb_reading = BovadaOddsProvider(fetch=_Fetch(_coupon(_nba_event())))  # MLB's key finds no point spread
    assert mlb_reading.lines_for(_nba_game(), NOW) == GameLines(
        away=BettingLine(moneyline=210), home=BettingLine(moneyline=-260)
    )


def test_an_empty_answer_is_a_coupon_with_no_games_and_no_error() -> None:
    for coupon in BovadaCoupon:
        assert BovadaOddsProvider(fetch=_Fetch({}), coupon=coupon).lines_for(_nba_game(), NOW) is None
    assert _parse_coupon({}) == ()


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            GameLines(away=BettingLine(moneyline=-150, runline=-3.5), home=BettingLine(moneyline=130, runline=3.5)),
            (HomeAway.AWAY, -150, -3.5),
        ),
        (
            GameLines(away=BettingLine(moneyline=-110, runline=0.0), home=BettingLine(moneyline=-110, runline=-1.0)),
            (HomeAway.HOME, -110, -1.0),
        ),
        (GameLines(away=BettingLine(moneyline=-110, runline=0.0), home=BettingLine(moneyline=-110, runline=0.0)), None),
        (GameLines(away=BettingLine(moneyline=-150), home=BettingLine(moneyline=130, runline=3.5)), None),
        (GameLines(away=BettingLine(moneyline=-150, runline=2.5), home=BettingLine(moneyline=130, runline=-2.5)), None),
        (GameLines(away=BettingLine(moneyline=-150, runline=-3.5)), None),
        (GameLines(home=BettingLine(moneyline=-150, runline=-3.5)), None),
    ],
)
def test_the_favorite_s_line_is_the_shorter_moneyline_and_the_points_it_gives(
    lines: GameLines, expected: tuple[HomeAway, int, float] | None
) -> None:
    line = favorite_line(lines)
    if expected is None:
        assert line is None
    else:
        assert line == FavoriteLine(favorite=expected[0], moneyline=expected[1], spread=expected[2])
