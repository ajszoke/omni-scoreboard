"""How a game can end, by league, stage and season, so a decision or a replay can say which rules it assumed.

`RULES` names this table by version: a rule changed or a mode added moves it. Nothing reads these modes to
decide yet. They're here so that what a board decided can be read beside the rules it took for granted.

- **MLB extra innings** (`ExtraInnings`): since 2020 a regular-season game starts each extra half-inning
  with a runner on second base. A postseason game never has. Read 2026-11-17 on MLB.com
  (https://www.mlb.com/news/mlb-extra-innings-rules-for-playoffs-2024-automatic-runners-and-pitch-clock):
  in the regular season "both teams start each extra inning with a runner on second base", and "There is
  no runner starting on second base during extra innings for postseason games" [verified]. That it began
  in 2020 and was made permanent from 2023 is from MLB.com's search listing of its glossary and its
  "Automatic runner permanent" story, whose pages refused the read [reported].
- **NFL overtime** (`Overtime`): a regular-season game plays one 10-minute period (since 2017) and can end
  tied; a postseason game plays 15-minute periods until someone wins. Since 2022 in the postseason, and
  2025 in the regular season, each team gets a possession whatever the first one brings. Read 2026-11-17:
  the 2025 change at https://www.therams.com/news/2025-nfl-rule-bylaw-resolution-changes-owners-approve-expanded-replay-alignment-of-regular-season-and-overtime-rules-permanent-dynamic-kickoff-with-touchbacks-to-the-35-yard-line
  (an NFL club's site, 2025-04-01: "both teams an opportunity to possess the ball regardless of the outcome
  of the first possession", the period "amended to 10") and the 2022 one at
  https://www.nfl.com/news/nfl-owners-approve-modified-overtime-rule-ensuring-possession-for-both-teams-in-
  ("will ensure both teams will receive a possession in overtime", postseason only) [verified]. That a
  safety on the first possession still ends it, the 15-minute postseason periods and the 2017 move to 10
  minutes are as the NFL's own overtime page (https://operations.nfl.com/the-rules/nfl-overtime-rules)
  reads in search listings, and as rules sites report; that page wouldn't render for a read [reported].

A stage the table doesn't cover, or a season older than its record, gives UNKNOWN, never a guess.
"""

from __future__ import annotations

from datetime import date, timezone
from enum import Enum
from typing import TypeAlias

from omni.core.enum import League, SeasonStage, StrEnumMixin
from omni.domain.contest import Contest

__all__ = [
    "RULES",
    "ExtraInnings",
    "Overtime",
    "RuleMode",
    "RulesVersion",
    "extra_innings",
    "overtime",
    "rule_mode",
    "season_of",
]


class RulesVersion(StrEnumMixin, str, Enum):
    """A version of this rule table, by its published name."""

    V2026_1 = "omni-rules/2026.1"


RULES = RulesVersion.V2026_1  # the table in force


class ExtraInnings(StrEnumMixin, str, Enum):
    """How an MLB game's extra half-innings start."""

    AUTOMATIC_RUNNER = "automatic_runner"  # with a runner on second base
    BASES_EMPTY = "bases_empty"  # as any other half-inning does
    UNKNOWN = "unknown"


class Overtime(StrEnumMixin, str, Enum):
    """How an NFL game's overtime is played."""

    # One 10-minute period; a touchdown on the first possession wins; it can end tied. Regular season, 2017-2024.
    TEN_MINUTES_FIRST_TOUCHDOWN_WINS = "10min_first_touchdown_wins"
    # One 10-minute period; each team gets a possession unless the first ends in a safety; it can end tied.
    # Regular season, from 2025.
    TEN_MINUTES_BOTH_POSSESS = "10min_both_possess"
    # 15-minute periods until someone wins; each team gets a possession unless the first ends in a safety.
    # Postseason, from 2022.
    FIFTEEN_MINUTE_PERIODS_BOTH_POSSESS = "15min_periods_both_possess"
    UNKNOWN = "unknown"


RuleMode: TypeAlias = ExtraInnings | Overtime

_AUTOMATIC_RUNNER_SINCE = 2020
_TEN_MINUTE_OVERTIME_SINCE = 2017
_REGULAR_SEASON_BOTH_POSSESS_SINCE = 2025
_POSTSEASON_BOTH_POSSESS_SINCE = 2022
_NFL_SEASON_OPENS_IN = 8  # August: a game from January through July belongs to the season before


def season_of(contest: Contest) -> int:
    """The season a contest belongs to, by the year it opened: an NFL game in January is the last year's."""
    day = contest.provider_date or _utc_date(contest)
    if contest.league is League.NFL and day.month < _NFL_SEASON_OPENS_IN:
        return day.year - 1
    return day.year


def extra_innings(contest: Contest) -> ExtraInnings:
    """How an MLB game's extra innings start, from its stage and season; UNKNOWN when they don't say."""
    if contest.league is not League.MLB:
        raise ValueError(f"{contest.id} isn't an MLB game")
    if contest.stage is SeasonStage.POSTSEASON:
        return ExtraInnings.BASES_EMPTY
    if contest.stage is SeasonStage.REGULAR:
        if season_of(contest) >= _AUTOMATIC_RUNNER_SINCE:
            return ExtraInnings.AUTOMATIC_RUNNER
        return ExtraInnings.BASES_EMPTY
    return ExtraInnings.UNKNOWN


def overtime(contest: Contest) -> Overtime:
    """How an NFL game's overtime is played, from its stage and season; UNKNOWN when they don't say."""
    if contest.league is not League.NFL:
        raise ValueError(f"{contest.id} isn't an NFL game")
    season = season_of(contest)
    if contest.stage is SeasonStage.REGULAR:
        if season >= _REGULAR_SEASON_BOTH_POSSESS_SINCE:
            return Overtime.TEN_MINUTES_BOTH_POSSESS
        if season >= _TEN_MINUTE_OVERTIME_SINCE:
            return Overtime.TEN_MINUTES_FIRST_TOUCHDOWN_WINS
    if contest.stage is SeasonStage.POSTSEASON and season >= _POSTSEASON_BOTH_POSSESS_SINCE:
        return Overtime.FIFTEEN_MINUTE_PERIODS_BOTH_POSSESS
    return Overtime.UNKNOWN


def rule_mode(contest: Contest) -> RuleMode | None:
    """The mode a contest's end follows, or None for a league this table has no modes for yet."""
    if contest.league is League.MLB:
        return extra_innings(contest)
    if contest.league is League.NFL:
        return overtime(contest)
    return None


def _utc_date(contest: Contest) -> date:
    return contest.scheduled_start.astimezone(timezone.utc).date()
