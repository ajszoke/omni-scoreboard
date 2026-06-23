"""Tests for the big-play card: event lineage, BURST attention, layouts, goldens.

Regenerate goldens intentionally with: OMNI_REGEN_GOLDEN=1 pytest -k big_play
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from omni.cards.attention import AttentionMode
from omni.cards.baseball import BigPlayCardPayload
from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.enum import DisplayPriority, GameStatus, League, PanelProfile, UpdateUrgency
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import Contest, TeamGame
from omni.events.base import EventImportance
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.big_play import BigPlayRenderer, headline_for
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.pillow_canvas import PillowCanvas
from omni.renderers.registry import default_registry

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
ALL_PROFILES = list(PanelProfile)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
_REG = MlbTeamRegistry.from_color_file()
AWAY = _REG.resolve(115)
HOME = _REG.resolve(119)


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
        away=AWAY,
        home=HOME,
    )


def _event(
    *,
    event_type: BaseballGameEventType = BaseballGameEventType.HOME_RUN,
    description: str = "Betts homers",
    eid: str = "e1",
) -> BaseballGameEvent:
    return BaseballGameEvent(
        id=LeagueScopedId(League.MLB, SOURCE, eid),
        contest=_game(),
        event_type=event_type,
        source=SOURCE,
        source_time=NOW,
        observed_at=NOW,
        importance=EventImportance(
            priority=DisplayPriority.ALERT,
            urgency=UpdateUrgency.HIGH,
            leverage=0.9,
            rarity=0.7,
            favorite_relevance=0.0,
        ),
        payload=BaseballPlayPayload(inning=7, phase=InningPhase.BOTTOM, description=description, rbi=1),
    )


def _state(away: int = 3, home: int = 5) -> BaseballGameState:
    return BaseballGameState(
        away_score=away,
        home_score=home,
        inning=7,
        phase=InningPhase.BOTTOM,
        count=BaseballCount(balls=0, strikes=0, outs=1),
        bases=BaseballBaseState(),
    )


def _card(
    *,
    event_type: BaseballGameEventType = BaseballGameEventType.HOME_RUN,
    description: str = "Betts homers",
    away: int = 3,
    home: int = 5,
) -> ScoreboardCard[BigPlayCardPayload]:
    event = _event(event_type=event_type, description=description)
    return CardFactory().big_play(_game(), event, _state(away, home), now=NOW)


def _render(card: ScoreboardCard[BigPlayCardPayload], profile: PanelProfile) -> RecordingCanvas:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    BigPlayRenderer().render(card, RenderContext(profile=profile, now=NOW), canvas)
    return canvas


# --- lineage + attention (the High #1 fix) ----------------------------------------


def test_big_play_card_carries_event_lineage() -> None:
    # An event-derived card retains its source event id(s) — dedupable + auditable.
    event = _event(eid="play-42")
    card = CardFactory().big_play(_game(), event, _state(), now=NOW)
    assert card.kind is CardKind.BIG_PLAY
    assert card.source_event_ids == (event.id,)  # <- lineage populated (round-1 High #1)
    assert event.id.raw in card.dedupe_key.raw  # dedupe keyed on the specific play


def test_big_play_uses_bounded_burst_attention() -> None:
    card = CardFactory().big_play(_game(), _event(), _state(), now=NOW)
    assert card.attention.mode is AttentionMode.BURST
    assert card.attention.takeover_for.value > 0  # a bounded takeover, never permanent
    assert card.priority.band is DisplayPriority.ALERT


def test_big_play_has_a_finite_window() -> None:
    card = CardFactory().big_play(_game(), _event(), _state(), now=NOW)
    expires = card.timing.expires_at
    assert expires is not None and not card.timing.is_available(expires)


# --- headline + layouts -----------------------------------------------------------


def test_headline_derives_from_event_type() -> None:
    assert headline_for(BaseballGameEventType.HOME_RUN) == "HOME RUN"
    assert headline_for(BaseballGameEventType.DOUBLE_PLAY) == "DOUBLE PLAY"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_headline_and_score_on_every_profile(profile: PanelProfile) -> None:
    texts = {t.text for t in _render(_card(away=3, home=5), profile).texts()}
    assert "HOME RUN" in texts
    assert f"{AWAY.abbreviation} 3  {HOME.abbreviation} 5" in texts  # score line with both sides


def test_description_shown_on_large_panels() -> None:
    for profile in (PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64):
        texts = {t.text for t in _render(_card(description="Betts homers"), profile).texts()}
        assert "Betts homers" in texts


def test_single_profile_compromise_drops_the_description() -> None:
    texts = {t.text for t in _render(_card(description="Betts homers"), PanelProfile.SINGLE_64X32).texts()}
    assert not any("Betts" in t for t in texts)  # description dropped at 64x32
    assert "HOME RUN" in texts  # but the headline + score remain


def test_long_description_is_truncated_to_panel_width() -> None:
    long = "Mookie Betts homers (12) on a fly ball to deep right field"
    rendered = [t.text for t in _render(_card(description=long), PanelProfile.STACK_64X64).texts()]
    desc = next(t for t in rendered if t.startswith("Mookie"))
    assert len(desc) <= 15  # truncated to the stack's character budget


def test_compromise_is_declared_on_the_card() -> None:
    assert any("single_64x32" in note for note in _card().layout_support.compromise_notes)


def test_default_registry_routes_big_play_to_its_renderer() -> None:
    # A BIG_PLAY card dispatches by (sport, kind) to the BigPlayRenderer.
    canvas = RecordingCanvas(128, 64)
    default_registry().render(_card(), RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), canvas)
    assert "HOME RUN" in {t.text for t in canvas.texts()}


def test_renderer_rejects_non_teamgame_contest() -> None:
    import dataclasses

    bad = Contest(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
    )
    card = dataclasses.replace(_card(), contest=bad)
    with pytest.raises(TypeError):
        BigPlayRenderer().render(
            card, RenderContext(profile=PanelProfile.QUAD_128X64, now=NOW), RecordingCanvas(128, 64)
        )


# --- goldens ----------------------------------------------------------------------


def _assert_matches_golden(image: Image.Image, name: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("OMNI_REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return
    assert path.exists(), f"missing golden {name}; regenerate with OMNI_REGEN_GOLDEN=1"
    expected = Image.open(path).convert("RGB")
    assert image.convert("RGB").tobytes() == expected.tobytes(), f"render differs from golden {name}"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_golden_image_per_profile(profile: PanelProfile) -> None:
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    BigPlayRenderer().render(_card(description="Betts homers"), RenderContext(profile=profile, now=NOW), canvas)
    _assert_matches_golden(canvas.image(), f"big_play_{profile.to_json_value()}.png")
