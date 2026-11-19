"""The team lanes against the scores: every lane element at its widest never touches a score's ink.

Each check draws a live card's away row the way the board does, with the widest NBA code, a five-character record
ending in the widest digit, seven timeouts and the bonus, against a score whose ink starts as far left as a score
can (a two-digit score with the leading digit that reaches furthest left, or a 1xx), at both halves of the turn.
An element on the score's rows must leave a blank column before its ink; one under the score must sit wholly
below it. The blank columns are the locked design's.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from omni.cards.basketball import BasketballGameCardPayload
from omni.core.clock import MonotonicTime
from omni.core.enum import PanelProfile
from omni.domain.baseball import WinLossRecord
from omni.panels.geometry import geometry_for
from omni.providers.nba_teams import NBA_TEAMS
from omni.renderers.canvas import DrawOp, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.font import rasterize
from omni.renderers.live_basketball import _RED as RED
from omni.renderers.live_basketball import LiveBasketballRenderer
from omni.renderers.text_lane import LaneClock, Marquee
from tests.basketball_fixtures import T, game_card, live, side

QUAD, STACK, SINGLE = PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32
_ROW_HEIGHT = {QUAD: 20, STACK: 20, SINGLE: 16}
_MODULE_LEFT = {QUAD: 80, STACK: 64, SINGLE: 44}  # where the clock and pips begin, right of the scores
_DIGITS = "0123456789"


@dataclass(frozen=True)
class _Ink:
    """The board columns and rows an element's ink (or box) covers, inclusive."""

    left: int
    right: int
    top: int
    bottom: int

    def clear_of(self, score: _Ink) -> int | None:
        """Blank columns between this and the score, or None when it sits wholly above or below the score's ink."""
        if self.bottom < score.top or self.top > score.bottom:
            return None
        return score.left - self.right - 1


def _glyph_ink(font: str, text: str) -> _Ink:
    lit = [(x, y) for y, row in enumerate(rasterize(font, text)) for x, on in enumerate(row) if on]
    return _Ink(min(x for x, _ in lit), max(x for x, _ in lit), min(y for _, y in lit), max(y for _, y in lit))


def _text_ink(op: DrawOp) -> _Ink:
    ink = _glyph_ink(op.font, op.text)
    return _Ink(op.x + ink.left, op.x + ink.right, op.y + ink.top, op.y + ink.bottom)


def _widest(font: str) -> tuple[str, str]:
    """The NBA code whose ink reaches furthest right, and the digit that does."""
    code = max(NBA_TEAMS, key=lambda code: _glyph_ink(font, code).right)
    digit = max(_DIGITS, key=lambda digit: _glyph_ink(font, digit).right)
    return code, digit


def _leading(font: str) -> str:
    """The leading digit whose ink starts furthest left."""
    return min("123456789", key=lambda digit: _glyph_ink(font, digit).left)


def _draw(profile: PanelProfile, three: bool, up: float) -> list[DrawOp]:
    """The away row of a live card at its widest: ``up`` seconds into the turn."""
    code, digit = _widest("6x10")  # the faces agree: ATL and 0 are the widest in every face used here
    score = (100 if three else 40) + int(digit)  # the score's leftmost ink is worked out from its face, below
    record = WinLossRecord(wins=40 + int(digit), losses=40 + int(digit))  # five characters, ending in the digit
    payload: BasketballGameCardPayload = live(
        side(score, 7, True, (score,)), side(12, 7, True, (12,)), 1, 3000, home_win=None, records=(record, None)
    )
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    lanes = LaneClock()
    lanes.frame("card", MonotonicTime(0.0)).commit()
    context = RenderContext(profile=profile, now=T, marquee=Marquee(clock=lanes.frame("card", MonotonicTime(up))))
    LiveBasketballRenderer().render(game_card(payload, away=code), context, canvas)
    return [op for op in canvas.ops if op.y < _ROW_HEIGHT[profile] and op.x < _MODULE_LEFT[profile]]


def _lane(profile: PanelProfile, three: bool) -> tuple[dict[str, _Ink], _Ink]:
    """Each team-lane element's ink in the away row, by name, and the score's at its leftmost."""
    names = _draw(profile, three, up=1.0)
    counts = _draw(profile, three, up=7.0)
    score_op = next(op for op in names if op.op == "text" and op.text.isdigit())
    font = score_op.font
    lead = "1" if three else _leading(font)
    full = _glyph_ink(font, _DIGITS)
    score = _Ink(
        score_op.x + _glyph_ink(font, lead).left,
        score_op.x + _glyph_ink(font, score_op.text).right,
        score_op.y + full.top,
        score_op.y + full.bottom,
    )
    code, record = [op for op in names if op.op == "text" and not op.text.isdigit()]
    timeouts = next(op for op in counts if op.op == "text" and op.text.endswith("TO"))
    box = next(op for op in counts if op.op == "fill_rect" and op.color == RED and op.x > 0)  # not a color bar
    elements = {
        "code": _text_ink(code),
        "record": _text_ink(record),
        "timeouts": _text_ink(timeouts),
        "bonus": _Ink(box.x, box.x + box.w - 1, box.y, box.y + box.h - 1),
    }
    return elements, score


# Blank columns before the score's ink for each element on the score's rows; None for one wholly below it.
_CLEAR: dict[tuple[PanelProfile, bool], dict[str, int | None]] = {
    (QUAD, False): {"code": 20, "record": 8, "timeouts": 14, "bonus": 11},
    (QUAD, True): {"code": 11, "record": 4, "timeouts": 5, "bonus": 2},
    (STACK, False): {"code": 7, "record": None, "timeouts": 5, "bonus": None},
    (STACK, True): {"code": 3, "record": None, "timeouts": 2, "bonus": None},
    (SINGLE, False): {"code": 8, "record": None, "timeouts": 11, "bonus": 9},
    (SINGLE, True): {"code": 5, "record": None, "timeouts": 5, "bonus": 3},
}
_CASES = [
    pytest.param(profile, three, id=f"{profile.to_json_value()}-{'1xx' if three else 'two-digit'}")
    for profile in sorted(LiveBasketballRenderer.supported_profiles)
    for three in (False, True)
]


@pytest.mark.parametrize("profile, three", _CASES)
def test_no_lane_element_touches_the_score(profile: PanelProfile, three: bool) -> None:
    elements, score = _lane(profile, three)
    for name, ink in elements.items():
        clear = ink.clear_of(score)
        assert clear is None or clear >= 1, f"{name} touches the score"
        if clear is None:
            assert ink.top > score.bottom, f"{name} runs over the score"
    assert {name: ink.clear_of(score) for name, ink in elements.items()} == _CLEAR[(profile, three)]


def test_the_quads_record_alone_drops_to_5x7_from_100() -> None:
    for three, faces in ((False, ["6x10", "6x10"]), (True, ["6x10", "5x7"])):
        names = _draw(QUAD, three, up=1.0)
        assert [op.font for op in names if op.op == "text" and not op.text.isdigit()] == faces


def test_the_stacks_record_passes_under_a_1xx_score_two_columns_with_two_blank_rows_between() -> None:
    elements, score = _lane(STACK, three=True)
    record = elements["record"]
    assert record.right - score.left + 1 == 2
    assert record.top - score.bottom - 1 == 2


def test_two_blank_rows_part_the_stacks_record_from_a_two_digit_score() -> None:
    elements, score = _lane(STACK, three=False)
    assert elements["record"].top - score.bottom - 1 == 2 and elements["record"].right < score.left


def test_the_widest_code_and_digit() -> None:
    assert _widest("6x10") == _widest("5x7") == _widest("4x6") == ("ATL", "0")
