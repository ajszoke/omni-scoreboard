"""The NFLMeta budget replay: its synthetic month, its billing model, and the committed report it writes."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from omni.app.quota import NFLMETA_FREE, UNLIMITED
from omni.providers.nflmeta import HALFTIME_HOLD
from omni.replay.nfl_budget import (
    EASTERN,
    GAME_LENGTH,
    PLAY_READS,
    PLAY_ROWS,
    STRATEGIES,
    TYPICAL_SUNDAY,
    Game,
    budget_report,
    month_games,
    replay,
    restart_cost,
)

REPORT = Path(__file__).parent / "fixtures" / "replay" / "nflmeta-budget.md"
KICKOFF = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)


def _eastern(day: date, at: time) -> datetime:
    return datetime.combine(day, at, EASTERN).astimezone(timezone.utc)


def test_the_committed_report_is_what_the_replay_says_at_the_defaults() -> None:
    """Regenerate with `tools/nflmeta_budget.py > tests/fixtures/replay/nflmeta-budget.md`, and explain the diff."""
    assert budget_report() == REPORT.read_text()


def test_november_is_the_games_that_kick_off_in_its_utc_month_with_the_favorite_on_sundays() -> None:
    games = month_games()
    assert len(games) == 72
    assert all(game.kickoff.month == 11 for game in games)
    assert _eastern(date(2026, 10, 29), time(20, 15)) not in {game.kickoff for game in games}  # October's
    assert _eastern(date(2026, 11, 30), time(20, 15)) not in {game.kickoff for game in games}  # bills to December
    favorites = [game for game in games if game.favorite]
    assert [game.day.day for game in favorites] == [1, 8, 15, 22, 29]
    assert {game.kickoff.astimezone(EASTERN).time() for game in favorites} == {time(13, 0)}
    assert sum(1 for game in games if game.day == TYPICAL_SUNDAY) == 14
    assert sum(1 for game in games if game.day == date(2026, 11, 26)) == 3  # Thanksgiving


def test_a_game_s_rows_and_plays_follow_the_measured_model() -> None:
    game = Game(day=TYPICAL_SUNDAY, kickoff=KICKOFF)
    assert [game.row(KICKOFF - timedelta(minutes=1)), game.row(KICKOFF)] == [1, 1]
    assert game.row(KICKOFF + GAME_LENGTH / 2) == 3  # two injury entries by the middle of the game
    assert game.row(game.final) == game.row(game.final + timedelta(hours=5)) == 5  # four by the final, and after
    plays = game.plays()
    assert len(plays) == PLAY_READS and sum(rows for _, rows in plays) == PLAY_ROWS
    start, end = game.halftime
    assert end - start == HALFTIME_HOLD and not any(start < at < end for at, _ in plays)
    assert plays[-1][0] == game.final


def test_an_unfiltered_read_late_on_the_typical_sunday_bills_what_the_measured_one_did() -> None:
    sunday = [game for game in month_games() if game.day == TYPICAL_SUNDAY]
    late = _eastern(TYPICAL_SUNDAY, time(23, 0))
    rows = sum(game.row(late) for game in sunday)
    assert 68 <= rows <= 70  # measured: 14 games listed, 68 to 70 rows


def test_a_restart_rereads_a_game_s_whole_play_history_in_pages() -> None:
    game = Game(day=TYPICAL_SUNDAY, kickoff=KICKOFF)
    assert restart_cost(game, KICKOFF) == (1, 0)
    assert restart_cost(game, game.final) == (3, PLAY_ROWS)


def test_on_free_no_minute_ever_goes_past_its_limit_and_what_it_holds_back_waits() -> None:
    sunday = [game for game in month_games() if game.day == TYPICAL_SUNDAY]
    full = next(strategy for strategy in STRATEGIES if strategy.key == "D30")
    asked = replay(sunday, full, UNLIMITED)
    held = replay(sunday, full, NFLMETA_FREE)
    assert asked.peak_minute > 20 and asked.waits == 0
    assert held.peak_minute <= 20 and held.waits > 0
