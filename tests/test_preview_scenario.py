"""Tests for the preview scenario builder and CLI helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from omni.core.enum import PanelProfile
from omni.domain.baseball import InningPhase
from omni.panels.geometry import geometry_for
from omni.preview.cli import _configure_options, _parse_args
from omni.preview.scenario import build_card_from_scenario

CLOSE_GAME = Path(__file__).resolve().parents[1] / "fixtures" / "mlb" / "live-close-game.json"
NOW = datetime(2026, 6, 18, 23, 30, tzinfo=timezone.utc)


def test_build_card_from_close_game_scenario() -> None:
    card = build_card_from_scenario(CLOSE_GAME, now=NOW)
    assert card.contest.away.abbreviation == "NYY"  # type: ignore[attr-defined]
    assert card.contest.home.abbreviation == "BOS"  # type: ignore[attr-defined]

    p = card.payload
    assert (p.away_score, p.home_score) == (5, 5)
    assert p.inning == 9 and p.phase is InningPhase.BOTTOM
    assert (p.count.balls, p.count.strikes, p.count.outs) == (3, 2, 2)
    assert p.bases.first and p.bases.second and p.bases.third  # bases loaded


def test_scenario_without_team_games_raises(tmp_path: Path) -> None:
    scenario = {
        "schedule": [
            {
                "game_id": 1,
                "game_datetime": "2026-06-18T23:10:00Z",
                "status": "In Progress",
                "away_id": 999,  # unknown -> row skipped -> no contests
                "home_id": 111,
            }
        ],
        "game": {"liveData": {"linescore": {"currentInning": 9}}},
    }
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError):
        build_card_from_scenario(path, now=NOW)


@pytest.mark.parametrize("profile", list(PanelProfile))
def test_configure_options_sets_logical_geometry(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    options = _configure_options(SimpleNamespace(), profile)
    assert (options.cols, options.rows) == (width, height)
    assert options.chain_length == 1
    assert options.parallel == 1


def test_parse_args_reads_profile_fixture_and_duration() -> None:
    args = _parse_args(["--profile", "stack_64x64", "--fixture", "x.json", "--duration", "5"])
    assert args.profile == "stack_64x64"
    assert args.fixture == "x.json"
    assert args.duration == 5.0


def test_parse_args_duration_defaults_to_none() -> None:
    args = _parse_args(["--profile", "quad_128x64", "--fixture", "x.json"])
    assert args.duration is None


def test_parse_args_rejects_unknown_profile() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--profile", "nope", "--fixture", "x.json"])
