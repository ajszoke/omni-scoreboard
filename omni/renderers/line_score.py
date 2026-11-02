"""The quad's R/H/E line score: the columns every card showing a game's full line draws into.

Each column is right-aligned at a fixed edge so it holds steady as a value widens. Each face gets its
own columns, placed so the widest cell it can hold — one 9x18B digit, two 6x13B ones — still starts
past the live card's ABS lane.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from omni.core.colors import RGBColor
from omni.renderers.canvas import Canvas
from omni.renderers.font import char_size
from omni.renderers.text import draw_right_aligned

__all__ = ["LINE_SCORE_RIGHT", "RHE_BIG_FONT", "LineScoreFace", "draw_line_score_row", "line_score_face"]

RHE_BIG_FONT = "9x18B"  # the headline line score — the largest face we carry
_RHE_SMALL_FONT = "6x13B"  # stepped down to once a value reaches two digits, to keep the columns fitting
_RHE_LEFT = 30  # where a column's widest cell begins
_RHE_PITCH = 14
_ROW_HEIGHT = 20


@dataclass(frozen=True, slots=True)
class LineScoreFace:
    """A font and the right edges of the columns laid out for it."""

    font: str
    cols: tuple[int, ...]


_BIG = LineScoreFace(RHE_BIG_FONT, tuple(_RHE_LEFT + 9 + i * _RHE_PITCH for i in range(3)))
_SMALL = LineScoreFace(_RHE_SMALL_FONT, tuple(_RHE_LEFT + 12 + i * _RHE_PITCH for i in range(3)))
LINE_SCORE_RIGHT = _BIG.cols[-1]


def line_score_face(values: Iterable[int]) -> LineScoreFace:
    """The face both rows share: the big one, until any value reaches two digits and would spill."""
    return _SMALL if max(values) >= 10 else _BIG


def draw_line_score_row(
    canvas: Canvas, values: Sequence[int], face: LineScoreFace, *, row_top: int, colors: Sequence[RGBColor]
) -> None:
    """One team's columns, right-aligned and vertically centered in its 20px row."""
    top = row_top + (_ROW_HEIGHT - char_size(face.font)[1]) // 2
    for value, right_x, color in zip(values, face.cols, colors):
        draw_right_aligned(canvas, right_x, top, str(value), color, face.font)
