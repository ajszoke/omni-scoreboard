"""Typed renderers: a hardware-agnostic `Canvas` plus per-card renderers.

Renderers depend only on the `Canvas` protocol (not rgbmatrix or Pillow directly),
so the same renderer draws to a real panel, a Pillow image for golden-image tests,
or a recording double for layout assertions.
"""
