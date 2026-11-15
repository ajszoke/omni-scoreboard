"""What a reading says, in the table and the archive."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cloud.market_service.readings import format_instant, lines_reading, win_reading
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BettingLine, GameLines, WinProbability
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.providers.polymarket_us import MarketQuote, MarketSample

REG = MlbTeamRegistry.from_color_file()
START = datetime(2026, 9, 17, 1, 40, tzinfo=timezone.utc)
AT = START + timedelta(hours=1)
GAME = TeamGame(
    id=LeagueScopedId(League.MLB, SourceRef("mlb_statsapi"), "745123"),
    league=League.MLB,
    status=GameStatus.LIVE,
    scheduled_start=START,
    away=REG.resolve(115),
    home=REG.resolve(119),
)


def _sample(*, probability: WinProbability | None, streamed: bool = True) -> MarketSample:
    return MarketSample(
        slug="aec-mlb-col-lad-2026-09-16",
        quote=MarketQuote(bid=0.30, ask=0.32),
        probability=probability,
        streamed=streamed,
    )


def test_a_market_reading_keeps_the_raw_price_and_the_percentages() -> None:
    reading = win_reading(GAME, _sample(probability=WinProbability(home=69.0, away=31.0)), AT)
    assert reading.game_id == "mlb:mlb_statsapi:745123"
    assert reading.observed_at == AT
    assert reading.body == {
        "league": "mlb",
        "away": "COL",
        "home": "LAD",
        "start": "2026-09-17T01:40:00.000Z",
        "slug": "aec-mlb-col-lad-2026-09-16",
        "bid": 0.30,
        "ask": 0.32,
        "source": "stream",
        "away_pct": 31.0,
        "home_pct": 69.0,
    }


def test_a_market_too_thin_to_read_records_its_price_with_no_percentages() -> None:
    body = win_reading(GAME, _sample(probability=None, streamed=False), AT).body
    assert body["source"] == "rest"
    assert body["bid"] == 0.30
    assert body["away_pct"] is None and body["home_pct"] is None


def test_a_lines_reading_keeps_each_side_and_says_which_is_missing() -> None:
    lines = GameLines(away=BettingLine(moneyline=120, runline=1.5, runline_price=-150), home=None)
    body = lines_reading(GAME, lines, AT).body
    assert body["lines"] == {"away": {"moneyline": 120, "runline": 1.5, "runline_price": -150}, "home": None}
    assert body["start"] == "2026-09-17T01:40:00.000Z"


def test_an_archive_line_stands_on_its_own() -> None:
    line = win_reading(GAME, _sample(probability=None), AT).archive_line()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["v"] == 1
    assert parsed["kind"] == "win"
    assert parsed["game_id"] == "mlb:mlb_statsapi:745123"
    assert parsed["observed_at"] == "2026-09-17T02:40:00.000Z"
    assert parsed["home"] == "LAD"


def test_instants_are_written_in_utc_whatever_zone_they_arrive_in() -> None:
    denver = timezone(timedelta(hours=-6))
    assert format_instant(datetime(2026, 9, 16, 19, 40, 0, 250000, tzinfo=denver)) == "2026-09-17T01:40:00.250Z"
