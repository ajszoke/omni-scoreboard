"""Stitch rendered panels into one contact sheet for eyeball review.

The committed goldens are 1 LED = 1 px so they diff exactly, but that makes a 128x64
PNG miserable to review — you end up zooming an image viewer into each one. This
composes a directory of them into a single sheet at a review scale (default 6x,
nearest-neighbor so the LEDs stay crisp squares), each panel labeled, so the whole
batch reads at a glance. It is a *review* artifact only; the committed goldens stay
1:1 for exact comparison.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

__all__ = ["build_contact_sheet", "write_contact_sheet"]

_BG = (28, 28, 30)  # a dark sheet so even all-black panels sit on a visible field
_LABEL_COLOR = (220, 220, 220)
_BORDER = (90, 90, 95)  # frames each panel so a black-background card is still bounded
_PAD = 8
_LABEL_H = 14  # headroom above each panel for its name


def build_contact_sheet(panels: list[tuple[str, Image.Image]], *, scale: int = 6, columns: int = 4) -> Image.Image:
    """Compose ``(label, image)`` panels into a labeled grid, each image scaled ``scale`` x.

    Cells are uniform (sized to the largest panel) and images are centered within them, so a
    mix of profiles (128x64, 64x64, 64x32) lines up. Nearest-neighbor keeps every LED a crisp
    square at the review scale.
    """
    if not panels:
        raise ValueError("no panels to compose")
    scaled = [
        (label, img.resize((img.width * scale, img.height * scale), Image.Resampling.NEAREST)) for label, img in panels
    ]
    cell_w = max(img.width for _, img in scaled)
    cell_h = max(img.height for _, img in scaled)
    col_w, row_h = cell_w + 2 * _PAD, cell_h + _LABEL_H + 2 * _PAD
    rows = -(-len(scaled) // columns)  # ceil division
    sheet = Image.new("RGB", (columns * col_w, rows * row_h), _BG)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, (label, img) in enumerate(scaled):
        row, col = divmod(i, columns)
        ox, oy = col * col_w + _PAD, row * row_h + _PAD
        draw.text((ox, oy), label, fill=_LABEL_COLOR, font=font)
        ix = ox + (cell_w - img.width) // 2  # center a narrower panel in its cell
        iy = oy + _LABEL_H
        sheet.paste(img, (ix, iy))
        draw.rectangle((ix - 1, iy - 1, ix + img.width, iy + img.height), outline=_BORDER)
    return sheet


def write_contact_sheet(
    src_dir: Path | str, out_path: Path | str, *, scale: int = 6, columns: int = 4, pattern: str = "*.png"
) -> int:
    """Load every ``pattern`` image under ``src_dir`` (skipping ``_``-prefixed names), stitch, and save.

    Returns the panel count. ``_``-prefixed files are skipped so a sheet written into the same
    directory is never folded back into the next one.
    """
    src, out = Path(src_dir), Path(out_path)
    paths = sorted(p for p in src.glob(pattern) if not p.name.startswith("_"))
    if not paths:
        raise FileNotFoundError(f"no {pattern} images under {src}")
    panels = []
    for path in paths:
        with Image.open(path) as handle:  # close each source handle; the converted copy lives on
            panels.append((path.stem, handle.convert("RGB")))
    sheet = build_contact_sheet(panels, scale=scale, columns=columns)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return len(panels)
