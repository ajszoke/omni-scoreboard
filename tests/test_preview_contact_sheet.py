"""The golden contact sheet: grid sizing, row wrap, and directory stitching."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from omni.preview.contact_sheet import build_contact_sheet, write_contact_sheet

_PAD, _LABEL_H = 8, 14  # mirror the module's cell padding so the size math is checkable here


def test_grid_is_sized_to_the_largest_panel() -> None:
    # A mix of profiles shares one uniform cell sized to the widest/tallest, so they line up.
    panels = [("wide", Image.new("RGB", (128, 64))), ("narrow", Image.new("RGB", (64, 32), (9, 9, 9)))]
    sheet = build_contact_sheet(panels, scale=6, columns=2)
    assert sheet.size == (2 * (128 * 6 + 2 * _PAD), 64 * 6 + _LABEL_H + 2 * _PAD)


def test_panels_wrap_onto_multiple_rows() -> None:
    panels = [(f"p{i}", Image.new("RGB", (64, 32))) for i in range(3)]
    sheet = build_contact_sheet(panels, scale=2, columns=2)  # 3 panels, 2 columns -> 2 rows
    assert sheet.height == 2 * (32 * 2 + _LABEL_H + 2 * _PAD)


def test_empty_panel_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="no panels"):
        build_contact_sheet([], scale=6)


def test_write_stitches_a_directory_and_skips_underscored(tmp_path: Path) -> None:
    golden = tmp_path / "golden"
    golden.mkdir()
    Image.new("RGB", (128, 64), (1, 2, 3)).save(golden / "b.png")
    Image.new("RGB", (64, 64), (4, 5, 6)).save(golden / "a.png")
    Image.new("RGB", (8, 8)).save(golden / "_prior_sheet.png")  # underscored -> never folded back in
    out = tmp_path / "build" / "sheet.png"
    count = write_contact_sheet(golden, out, scale=3, columns=2)
    assert count == 2  # the two goldens, not the underscored prior sheet
    assert out.exists()
    with Image.open(out) as sheet:
        assert sheet.size[0] > 0


def test_write_raises_when_no_images_match(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no"):
        write_contact_sheet(tmp_path, tmp_path / "out.png")
