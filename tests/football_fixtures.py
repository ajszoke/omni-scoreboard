"""NFL teams and live-card builders shared by the football tests, from the committed registry and tiles."""

from __future__ import annotations

from datetime import datetime, timezone

from omni.cards.base import CardId, CardKind, CardPriority, DedupeKey, DisplayTiming, LayoutSupport, ScoreboardCard
from omni.cards.football import LiveFootballCardPayload
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState, PlaySummary
from omni.domain.teams import Team
from omni.providers.nfl_teams import NflTeamRegistry

SOURCE = SourceRef("nflmeta")
T = datetime(2026, 11, 16, 2, 30, tzinfo=timezone.utc)
ALL_PROFILES = (PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64)

_REGISTRY = NflTeamRegistry.default(source=SOURCE)


def nfl_team(abbreviation: str) -> Team:
    return _REGISTRY.resolve(abbreviation)


def nfl_game(away: str = "PIT", home: str = "CIN", raw: str = "22920") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.NFL, SOURCE, raw),
        league=League.NFL,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=nfl_team(away),
        home=nfl_team(home),
    )


def live_football_card(
    state: FootballGameState,
    *,
    last_play: PlaySummary | None = None,
    win_probability: WinProbability | None = None,
    game: TeamGame | None = None,
    break_ends_at: datetime | None = None,
) -> ScoreboardCard[LiveFootballCardPayload]:
    return ScoreboardCard(
        id=CardId("nfl-g1:live"),
        kind=CardKind.LIVE_GAME,
        contest=game if game is not None else nfl_game(),
        timing=DisplayTiming(available_at=T, min_display=DurationSeconds(5), max_display=DurationSeconds(30)),
        priority=CardPriority(band=DisplayPriority.FAVORITE, score=50.0),
        layout_support=LayoutSupport(profiles=frozenset(ALL_PROFILES)),
        dedupe_key=DedupeKey("nfl-g1:live"),
        payload=LiveFootballCardPayload(
            state=state, last_play=last_play, win_probability=win_probability, break_ends_at=break_ends_at
        ),
    )
