"""The three recorded games of 2026-11-18 through the shared board's NBA path, ALL-STAR, under the board policy's
settings with the Lakers the favorite: the decisions it makes (the fixture), UTA at LAL from its pregame card to its
final with its timeouts' breaks (eight long, two short), the play queue, the requests within the ledger's minute, every
card drawable on every profile, and the same night on Free, on the slate alone."""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from omni.app.quota import BalldontliePlan
from omni.cards.base import CardKind, ScoreboardCard
from omni.core.enum import League, PanelProfile
from omni.core.time import DurationSeconds
from omni.panels.geometry import geometry_for
from omni.policy.settings import PolicySettings
from omni.preferences.favorites import Favorites
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import default_registry
from omni.replay.basketball import NbaRun, nba_decisions, run_nba
from tests.balldontlie_replay import RecordedNight

DECISIONS = Path(__file__).parent / "fixtures" / "replay" / "nba-2026-11-18.decisions.txt"
LAKERS = Favorites({League.NBA: ["LAL"]})
DELAY = DurationSeconds(30)
SETTINGS = PolicySettings()  # omni-board-policy/2
UTA_LAL = "nba:balldontlie:21718141"
DET_NOP = "nba:balldontlie:21718137"
HOU_IND = "nba:balldontlie:21718132"


def _until(night: RecordedNight) -> Any:
    return night.end + timedelta(minutes=2)


@cache
def _decisions() -> tuple[str, ...]:
    night = RecordedNight()
    lines = nba_decisions(
        night.fetch, night.clock, until=_until(night), delay=DELAY, favorites=LAKERS, settings=SETTINGS
    )
    return tuple(lines)


@cache
def _night(tier: BalldontliePlan) -> tuple[NbaRun, RecordedNight, tuple[ScoreboardCard[Any], ...]]:
    night = RecordedNight()
    drawn: dict[tuple[object, ...], ScoreboardCard[Any]] = {}

    def watch(card: ScoreboardCard[Any]) -> None:
        payload = card.payload
        bottom = getattr(payload, "bottom", None)
        state = getattr(payload, "state", None)
        place = getattr(bottom, "place", None)
        kind = (card.kind, type(bottom).__name__, getattr(state, "phase", None), place is not None)
        drawn.setdefault(kind, card)

    run = run_nba(
        night.fetch,
        night.clock,
        until=_until(night),
        tier=tier,
        delay=DELAY,
        favorites=LAKERS,
        settings=SETTINGS,
        watch=watch,
    )
    return run, night, tuple(drawn.values())


def _busiest_minute(night: RecordedNight) -> int:
    """The most requests the night got in any 60 s."""
    times = sorted(at for at, _, _ in night.asked)
    busiest, start = 0, 0
    for end, at in enumerate(times):
        while times[start] <= at - 60:
            start += 1
        busiest = max(busiest, end - start + 1)
    return busiest


def _lines(kind: str, game: str | None = None) -> list[str]:
    return [line for line in _decisions() if line[10:18].strip() == kind and (game is None or game in line)]


def test_the_night_replays_to_its_recorded_decisions() -> None:
    header, *decided = _decisions()
    recorded, *expected = DECISIONS.read_text().splitlines()
    assert decided == expected
    assert header == recorded and "nba_delay=30s;nba_plan=all-star" not in header  # the digest names them


def test_uta_at_lal_runs_from_its_pregame_card_to_its_final() -> None:
    ups = _lines("up", UTA_LAL)
    assert ups[0].endswith(f"{UTA_LAL}:pregame  (rotation)")
    assert any(f"{UTA_LAL}:live" in line for line in ups)
    (final,) = _lines("final", UTA_LAL)
    assert final.split()[2] == f"{UTA_LAL}:final"
    assert any(f"{UTA_LAL}:final" in line for line in ups)
    assert len(_lines("final")) == 3  # every game's final, each revealed once


def test_uta_at_lal_s_timeouts_lend_eight_long_breaks_and_two_short_and_its_period_breaks_too() -> None:
    begun = [line for line in _lines("break", UTA_LAL) if not line.endswith("(ended)")]
    kinds = Counter(line.split(" @ ")[0].split(f"{UTA_LAL} ")[1] for line in begun)
    assert kinds["nba tv timeout"] == 8 and kinds["nba team timeout"] == 2
    assert kinds["nba halftime"] == 1 and kinds["nba quarter break"] == 2
    opened = [line for line in _lines("lease", UTA_LAL) if " opened " in line]
    assert len(opened) == 13  # every break of the favorite's lent
    assert all("provisional" not in line for line in opened if "timeout" in line)  # aged from the inferred onset


def _caption_holds() -> Counter[tuple[float, str]]:
    """How long each caption line held its game's panel, by what came next: another ``caption``, a ``stat`` panel, or
    the card going ``down``."""
    lines = _decisions()[1:]
    holds: Counter[tuple[float, str]] = Counter()
    for i, line in enumerate(lines):
        game, words = line[18:].split("  ") if line[10:18].strip() == "panel" else ("", "")
        if not words[:1].isupper():
            continue  # not a panel line, or a stat panel's
        for later in lines[i + 1 :]:
            if later[10:18].strip() == "panel" and later[18:].startswith(f"{game}  "):
                after = "caption" if later.split("  ")[-1][:1].isupper() else "stat"
                break
            if later[10:18].strip() == "down" and later[18:].startswith(f"{game}:live"):
                after = "down"
                break
        holds[round(float(later[:8]) - float(line[:8]), 1), after] += 1
    return holds


def test_the_play_queue_runs_its_plays_6_s_each_and_a_lone_one_a_whole_turn_with_no_count() -> None:
    panels = _lines("panel")
    assert not [line for line in panels if re.search(r"\d+/\d+|alone", line)]
    holds = _caption_holds()
    assert holds[6.0, "caption"] and holds[12.0, "stat"]  # a run's plays, and one alone before the rotation
    stats = {line.split("  ")[-1] for line in panels if not line.split("  ")[-1][:1].isupper()}
    assert {"line score", "team box", "players #1"} <= stats
    assert any("unseen (too late to start)" in line for line in _lines("skip"))  # never shown late


def test_every_request_keeps_to_the_ledger_s_minute() -> None:
    run, night, _ = _night(BalldontliePlan.ALL_STAR)
    assert _busiest_minute(night) <= 55
    assert dict(run.requests)["season"] == 2  # once each Eastern day: the night runs past midnight there


@pytest.mark.parametrize("profile", list(PanelProfile))
def test_every_kind_of_card_the_night_put_up_draws_on_every_profile(profile: PanelProfile) -> None:
    _, _, cards = _night(BalldontliePlan.ALL_STAR)
    assert {card.kind for card in cards} == {CardKind.PREGAME, CardKind.LIVE_GAME, CardKind.FINAL}
    registry = default_registry()
    geometry = geometry_for(profile)
    for card in cards:
        canvas = RecordingCanvas(geometry.width, geometry.height)
        registry.render(card, RenderContext(profile=profile, now=card.timing.available_at), canvas)
        assert canvas.ops


def test_on_free_the_night_runs_on_the_slate_alone() -> None:
    run, night, cards = _night(BalldontliePlan.FREE)
    assert "/v1/stats" not in dict(run.requests)
    bottoms = {type(card.payload.bottom).__name__ for card in cards if card.kind is CardKind.LIVE_GAME}
    assert bottoms == {"LineScorePanel"}
    assert _busiest_minute(night) <= 5
