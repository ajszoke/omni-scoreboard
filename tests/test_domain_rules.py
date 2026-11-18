"""Rule modes: how a game can end, from its league, stage and season; UNKNOWN when they don't say."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from omni.core.enum import GameStatus, League, SeasonStage
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import Contest
from omni.domain.rules import (
    RULES,
    ExtraInnings,
    Overtime,
    RulesVersion,
    extra_innings,
    overtime,
    rule_mode,
    season_of,
)

SOURCE = SourceRef("test")


def _contest(league: League, stage: SeasonStage, start: datetime, *, filed: date | None = None) -> Contest:
    return Contest(
        id=LeagueScopedId(league, SOURCE, "g1"),
        league=league,
        status=GameStatus.SCHEDULED,
        scheduled_start=start,
        stage=stage,
        provider_date=filed,
    )


def _on(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 23, 0, tzinfo=timezone.utc)


def test_the_rule_table_is_versioned() -> None:
    assert RULES is RulesVersion.V2026_1 and str(RULES) == "omni-rules/2026.1"


@pytest.mark.parametrize(
    ("stage", "year", "mode"),
    [
        (SeasonStage.REGULAR, 2026, ExtraInnings.AUTOMATIC_RUNNER),
        (SeasonStage.REGULAR, 2020, ExtraInnings.AUTOMATIC_RUNNER),
        (SeasonStage.REGULAR, 2019, ExtraInnings.BASES_EMPTY),
        (SeasonStage.POSTSEASON, 2026, ExtraInnings.BASES_EMPTY),
        (SeasonStage.POSTSEASON, 2019, ExtraInnings.BASES_EMPTY),
        (SeasonStage.PRESEASON, 2026, ExtraInnings.UNKNOWN),
        (SeasonStage.ALL_STAR, 2026, ExtraInnings.UNKNOWN),
        (SeasonStage.EXHIBITION, 2026, ExtraInnings.UNKNOWN),
        (SeasonStage.UNKNOWN, 2026, ExtraInnings.UNKNOWN),
    ],
)
def test_an_mlb_game_s_extra_innings_follow_its_stage_and_season(
    stage: SeasonStage, year: int, mode: ExtraInnings
) -> None:
    game = _contest(League.MLB, stage, _on(year, 7, 4))
    assert extra_innings(game) is mode and rule_mode(game) is mode


@pytest.mark.parametrize(
    ("stage", "start", "mode"),
    [
        (SeasonStage.REGULAR, _on(2026, 11, 15), Overtime.TEN_MINUTES_BOTH_POSSESS),
        (SeasonStage.REGULAR, _on(2025, 9, 7), Overtime.TEN_MINUTES_BOTH_POSSESS),
        (SeasonStage.REGULAR, _on(2026, 1, 4), Overtime.TEN_MINUTES_BOTH_POSSESS),  # the 2025 season's last week
        (SeasonStage.REGULAR, _on(2025, 1, 5), Overtime.TEN_MINUTES_FIRST_TOUCHDOWN_WINS),  # the 2024 season's
        (SeasonStage.REGULAR, _on(2017, 9, 10), Overtime.TEN_MINUTES_FIRST_TOUCHDOWN_WINS),
        (SeasonStage.REGULAR, _on(2016, 9, 11), Overtime.UNKNOWN),  # 15-minute periods then: before the table
        (SeasonStage.POSTSEASON, _on(2027, 1, 16), Overtime.FIFTEEN_MINUTE_PERIODS_BOTH_POSSESS),
        (SeasonStage.POSTSEASON, _on(2023, 1, 14), Overtime.FIFTEEN_MINUTE_PERIODS_BOTH_POSSESS),  # 2022 season
        (SeasonStage.POSTSEASON, _on(2022, 1, 23), Overtime.UNKNOWN),  # the 2021 season's, before the change
        (SeasonStage.PRESEASON, _on(2026, 8, 13), Overtime.UNKNOWN),
        (SeasonStage.UNKNOWN, _on(2026, 11, 15), Overtime.UNKNOWN),
    ],
)
def test_an_nfl_game_s_overtime_follows_its_stage_and_season(
    stage: SeasonStage, start: datetime, mode: Overtime
) -> None:
    game = _contest(League.NFL, stage, start)
    assert overtime(game) is mode and rule_mode(game) is mode


def test_a_season_is_counted_from_the_date_the_source_files_the_game_under_when_it_gives_one() -> None:
    late = _contest(League.NFL, SeasonStage.REGULAR, _on(2026, 1, 1), filed=date(2025, 12, 31))
    assert season_of(late) == 2025
    assert season_of(_contest(League.NFL, SeasonStage.REGULAR, _on(2026, 8, 13))) == 2026
    assert season_of(_contest(League.MLB, SeasonStage.REGULAR, _on(2026, 3, 26), filed=date(2026, 3, 26))) == 2026


def test_a_league_without_modes_has_none_and_a_mode_is_only_asked_of_its_own_league() -> None:
    assert rule_mode(_contest(League.NBA, SeasonStage.REGULAR, _on(2026, 11, 16))) is None
    with pytest.raises(ValueError, match="isn't an MLB game"):
        extra_innings(_contest(League.NFL, SeasonStage.REGULAR, _on(2026, 11, 15)))
    with pytest.raises(ValueError, match="isn't an NFL game"):
        overtime(_contest(League.MLB, SeasonStage.REGULAR, _on(2026, 7, 4)))
