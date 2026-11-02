"""Bovada's pregame odds: reading the coupon, matching it to a game, and refreshing at its own pace."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BettingLine, GameLines
from omni.domain.contest import TeamGame
from omni.providers.base import ProviderError
from omni.providers.bovada_odds import BovadaOddsProvider, _american, _handicap, _parse_coupon
from omni.providers.mlb_teams import MlbTeamRegistry

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
