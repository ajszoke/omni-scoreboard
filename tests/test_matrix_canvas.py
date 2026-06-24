"""Tests for MatrixCanvas: the Canvas -> LED matrix adapter.

The headline test renders a real card through both MatrixCanvas (over a fake
matrix) and PillowCanvas, and asserts the result is pixel-identical — so the
emulator/hardware output matches the golden-image snapshots.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omni.cards.factory import CardFactory
from omni.core.colors import RGBColor
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase
from omni.domain.contest import TeamGame
from omni.panels.geometry import geometry_for
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoImage, LogoStore
from omni.renderers.live_baseball import LiveBaseballRenderer
from omni.renderers.matrix_canvas import MatrixCanvas, MatrixSurface
from omni.renderers.pillow_canvas import PillowCanvas

NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")


class FakeMatrix:
    """A minimal `MatrixSurface` that records SetPixel writes into a dict."""

    def __init__(self) -> None:
        self.pixels: dict[tuple[int, int], tuple[int, int, int]] = {}

    def SetPixel(self, x: int, y: int, r: int, g: int, b: int) -> None:
        self.pixels[(x, y)] = (r, g, b)


def _card() -> object:
    reg = MlbTeamRegistry.from_color_file()
    game = TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "700001"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
        away=reg.resolve(115, full_name="Colorado Rockies"),
        home=reg.resolve(119, full_name="Los Angeles Dodgers"),
    )
    state = BaseballGameState(
        away_score=3,
        home_score=5,
        inning=7,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=BaseballBaseState(first=True, third=True),
    )
    return CardFactory().live_baseball(game, state, now=NOW)


def test_fake_matrix_satisfies_surface_protocol() -> None:
    assert isinstance(FakeMatrix(), MatrixSurface)
    assert not isinstance(object(), MatrixSurface)


def test_set_pixel_clips_to_bounds() -> None:
    fake = FakeMatrix()
    canvas = MatrixCanvas(fake, 4, 4)
    assert (canvas.width, canvas.height) == (4, 4)
    canvas.set_pixel(1, 2, RGBColor(10, 20, 30))
    canvas.set_pixel(-1, 0, RGBColor(1, 1, 1))
    canvas.set_pixel(4, 0, RGBColor(2, 2, 2))
    canvas.set_pixel(0, 4, RGBColor(3, 3, 3))
    assert fake.pixels == {(1, 2): (10, 20, 30)}


def test_fill_and_fill_rect() -> None:
    fake = FakeMatrix()
    canvas = MatrixCanvas(fake, 3, 2)
    canvas.fill(RGBColor(0, 0, 0))
    assert len(fake.pixels) == 6  # every pixel written
    canvas.fill_rect(1, 0, 10, 1, RGBColor(255, 0, 0))  # width clipped to bounds
    assert fake.pixels[(1, 0)] == (255, 0, 0)
    assert fake.pixels[(2, 0)] == (255, 0, 0)
    assert fake.pixels[(0, 0)] == (0, 0, 0)


def test_text_draws_some_pixels() -> None:
    fake = FakeMatrix()
    MatrixCanvas(fake, 64, 32).text(0, 0, "A", RGBColor(255, 255, 255), font="4x6")
    assert any(color == (255, 255, 255) for color in fake.pixels.values())


def test_draw_image_blits_each_pixel_and_clips() -> None:
    fake = FakeMatrix()
    canvas = MatrixCanvas(fake, 8, 4)
    tile = LogoImage(
        key="t",
        width=2,
        height=2,
        pixels=(RGBColor(10, 0, 0), RGBColor(0, 20, 0), RGBColor(0, 0, 30), RGBColor(40, 40, 40)),
    )
    canvas.draw_image(2, 1, tile)
    assert fake.pixels[(2, 1)] == (10, 0, 0)
    assert fake.pixels[(3, 2)] == (40, 40, 40)
    canvas.draw_image(7, 3, tile)  # only the top-left pixel is in bounds
    assert fake.pixels[(7, 3)] == (10, 0, 0)
    assert (8, 3) not in fake.pixels  # the rest is clipped, no error


@pytest.mark.parametrize("logos", [None, LogoStore()], ids=["bar", "logo"])
def test_matrix_canvas_matches_pillow_pixel_for_pixel(logos: LogoStore | None) -> None:
    card = _card()
    renderer = LiveBaseballRenderer()
    for profile in PanelProfile:
        width, height = geometry_for(profile).size
        context = RenderContext(profile=profile, now=NOW, logos=logos)

        fake = FakeMatrix()
        renderer.render(card, context, MatrixCanvas(fake, width, height))  # type: ignore[arg-type]

        pillow = PillowCanvas(width, height)
        renderer.render(card, context, pillow)  # type: ignore[arg-type]
        image = pillow.image().convert("RGB")

        for y in range(height):
            for x in range(width):
                expected = image.getpixel((x, y))
                actual = fake.pixels.get((x, y), (0, 0, 0))
                assert actual == expected, f"{profile.value} pixel ({x},{y}): {actual} != {expected}"
