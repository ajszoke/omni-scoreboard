"""Font metrics and the custom glyphs ported from the legacy matrix set.

These guard two things the anchored-layout primitives now depend on: that ``advance``
reports a glyph's real ``DWIDTH`` (so a string mixing the thin space with full cells
measures correctly), and that the hand-made compact ½ replaced upstream's stock glyph.
A named ``THIN_SPACE`` constant isolates U+2009 so the rest of the source stays clear.
"""

from __future__ import annotations

import pytest

from omni.renderers.font import FONTS, advance, char_size, rasterize

THIN_SPACE = " "
ONE_HALF = "½"


@pytest.mark.parametrize("name", sorted(FONTS))
def test_every_registered_font_loads_and_reports_sane_metrics(name: str) -> None:
    # Each committed font must load through bdfparser and rasterize without error and report a
    # positive cell + advance, so a registry typo or a missing .bdf fails loudly right here.
    width, height = char_size(name)
    assert width > 0 and height > 0
    assert len(rasterize(name, "Ag5 0")) > 0
    assert advance(name, "0") > 0


def test_advance_is_the_cell_width_for_ordinary_glyphs() -> None:
    assert advance("4x6", "A") == 4
    assert advance("6x10", "0") == 6


def test_thin_space_advances_half_a_4x6_cell() -> None:
    # U+2009, ported from the legacy set, is a 2px "close quarters" space.
    assert advance("4x6", THIN_SPACE) == 2


def test_advance_falls_back_to_the_cell_for_an_unmapped_char() -> None:
    # An emoji has no glyph in a 4x6 bitmap font; assume a full cell rather than raise.
    assert advance("4x6", "\U0001f600") == 4


def test_thin_space_narrows_the_rasterized_row() -> None:
    # Drawing already advances by DWIDTH, so the thin space packs the row tighter —
    # which is exactly why text_width has to sum advances to stay in lockstep.
    normal = len(rasterize("4x6", "a b")[0])  # 4 + 4 + 4
    tight = len(rasterize("4x6", "a" + THIN_SPACE + "b")[0])  # 4 + 2 + 4
    assert (normal, tight) == (12, 10)


def test_custom_one_half_is_the_compact_legacy_glyph() -> None:
    # The user's hand-made compact ½ replaced upstream's taller stock glyph: a '1'
    # stroke over a small '2', sitting on the baseline with no descender.
    rows = rasterize("4x6", ONE_HALF)
    on = {(x, y) for y, row in enumerate(rows) for x, px in enumerate(row) if px}
    assert on == {(0, 0), (0, 1), (0, 3), (1, 3), (1, 4), (2, 4)}
