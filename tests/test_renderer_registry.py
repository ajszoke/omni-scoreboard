"""Tests for RendererRegistry: routing + the three-way render-contract check."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

import pytest

from omni.cards.base import CardKind, LayoutSupport, ScoreboardCard
from omni.cards.factory import CardFactory
from omni.core.enum import GameStatus, League, PanelProfile, Sport
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RenderDispatchError, RendererRegistry, default_registry

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)
QUAD = PanelProfile.QUAD_128X64
CTX = RenderContext(profile=QUAD, now=T)


def _live_card() -> ScoreboardCard[Any]:
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )
    state = BaseballGameState(
        away_score=1,
        home_score=2,
        inning=5,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=1, strikes=2, outs=1),
        bases=BaseballBaseState(first=True),
    )
    return CardFactory().live_baseball(game, state, now=T)


class _RecordingRenderer:
    """A minimal `Renderer` that records the calls routed to it."""

    def __init__(self, profiles: set[PanelProfile]) -> None:
        self.supported_profiles = frozenset(profiles)
        self.calls: list[tuple[str, PanelProfile]] = []

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self.calls.append((card.id.raw, context.profile))


def test_register_and_lookup_by_sport_and_kind() -> None:
    renderer = _RecordingRenderer({QUAD})
    registry = RendererRegistry()
    registry.register(Sport.BASEBALL, CardKind.LIVE_GAME, renderer)
    assert registry.renderer_for(_live_card()) is renderer


def test_unregistered_card_raises() -> None:
    with pytest.raises(RenderDispatchError, match="no renderer registered"):
        RendererRegistry().renderer_for(_live_card())


def test_render_routes_to_the_registered_renderer() -> None:
    renderer = _RecordingRenderer({QUAD})
    registry = RendererRegistry()
    registry.register(Sport.BASEBALL, CardKind.LIVE_GAME, renderer)
    registry.render(_live_card(), CTX, RecordingCanvas(128, 64))
    assert renderer.calls == [("g1:live", QUAD)]


def test_render_rejects_card_that_does_not_support_the_profile() -> None:
    registry = RendererRegistry()
    registry.register(Sport.BASEBALL, CardKind.LIVE_GAME, _RecordingRenderer({QUAD}))
    card = dataclasses.replace(
        _live_card(), layout_support=LayoutSupport(profiles=frozenset({PanelProfile.SINGLE_64X32}))
    )
    with pytest.raises(RenderDispatchError, match="card .* does not support"):
        registry.render(card, CTX, RecordingCanvas(128, 64))


def test_render_rejects_renderer_that_does_not_support_the_profile() -> None:
    registry = RendererRegistry()
    registry.register(Sport.BASEBALL, CardKind.LIVE_GAME, _RecordingRenderer({PanelProfile.SINGLE_64X32}))
    with pytest.raises(RenderDispatchError, match="renderer .* does not support"):
        registry.render(_live_card(), CTX, RecordingCanvas(128, 64))


def test_render_rejects_canvas_geometry_mismatch() -> None:
    registry = RendererRegistry()
    registry.register(Sport.BASEBALL, CardKind.LIVE_GAME, _RecordingRenderer({QUAD}))
    with pytest.raises(RenderDispatchError, match="does not match"):
        registry.render(_live_card(), CTX, RecordingCanvas(64, 32))  # wrong size for QUAD


def test_default_registry_renders_a_live_baseball_card() -> None:
    canvas = RecordingCanvas(128, 64)
    default_registry().render(_live_card(), CTX, canvas)
    assert len(canvas.ops) > 0  # the real LiveBaseballRenderer drew something


def _pregame_card() -> ScoreboardCard[Any]:
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.PREGAME,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )
    return CardFactory().pregame(game, now=T)


def test_default_registry_routes_pregame_to_its_renderer() -> None:
    # A PREGAME card dispatches by (sport, kind) to the PregameRenderer, not the live one.
    canvas = RecordingCanvas(128, 64)
    default_registry().render(_pregame_card(), CTX, canvas)
    drawn = {t.text for t in canvas.texts()}
    assert "FIRST PITCH" in drawn  # proof the pregame renderer (not live) handled it


def _final_card() -> ScoreboardCard[Any]:
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "g1"),
        league=League.MLB,
        status=GameStatus.FINAL,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )
    state = BaseballGameState(
        away_score=3,
        home_score=5,
        inning=9,
        phase=InningPhase.BOTTOM,
        count=BaseballCount(balls=0, strikes=0, outs=3),
        bases=BaseballBaseState(),
    )
    return CardFactory().final(game, state, now=T)


def test_default_registry_routes_final_to_its_renderer() -> None:
    # A FINAL card dispatches to the FinalRenderer (its "FINAL" marker proves it).
    canvas = RecordingCanvas(128, 64)
    default_registry().render(_final_card(), CTX, canvas)
    assert "FINAL" in {t.text for t in canvas.texts()}
