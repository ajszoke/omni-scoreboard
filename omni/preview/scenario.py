"""Build a renderable card from a self-contained preview scenario fixture.

A scenario JSON bundles a schedule row and its game feed::

    {"schedule": [ <one statsapi.schedule row> ], "game": <statsapi game feed>}

It is run through the real provider + `CardFactory`, so a preview exercises the
same typed pipeline as production — only the fetchers are swapped for the fixture.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from omni.cards.base import ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.cards.factory import CardFactory
from omni.domain.contest import TeamGame
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from omni.providers.mlb_teams import MlbTeamRegistry

__all__ = ["build_card_from_scenario"]


def build_card_from_scenario(
    path: str | Path,
    *,
    now: datetime,
    registry: MlbTeamRegistry | None = None,
) -> ScoreboardCard[LiveBaseballCardPayload]:
    """Assemble a live MLB card from the scenario at `path`.

    The scenario's schedule must contain exactly the game its `game` feed
    describes; the first parsed `TeamGame` is used.
    """
    data: dict[str, Any] = json.loads(Path(path).read_text())
    schedule = data["schedule"]
    game_feed = data["game"]

    reg = registry if registry is not None else MlbTeamRegistry.from_color_file()
    provider = MlbStatsApiProvider(
        reg,
        fetch_schedule=lambda game_date, sport_ids: schedule,
        fetch_game=lambda game_pk: game_feed,
    )

    contests = [c for c in provider.refresh(now).contests if isinstance(c, TeamGame)]
    if not contests:
        raise ValueError(f"scenario {path} produced no team games")
    game = contests[0]
    state = provider.fetch_game_state(game.id.raw)
    return CardFactory().live_baseball(game, state, now=now)
