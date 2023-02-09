try:
    from rgbmatrix import RGBMatrix, graphics
except ImportError:
    from RGBMatrixEmulator import graphics


def writeRightToLeft(canvas, font, coords, string, text_colors):
    font_width = font["size"]["width"]
    for i, c in enumerate(string[::-1]):
        char_draw_x = coords["x"] - font_width * (i + 1)  # Determine character position
        graphics.DrawText(canvas, font["font"], char_draw_x, coords["y"], text_colors, c)
        coords["x"] += 1
