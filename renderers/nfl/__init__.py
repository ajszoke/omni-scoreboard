import time
from datetime import datetime

from typing import NoReturn

import debug
from data.nfl import NflManager
from util import canvashelper

try:
    from rgbmatrix import graphics
except ImportError:
    from RGBMatrixEmulator import graphics


class NflRenderer:
    def __init__(self, matrix, data: NflManager):
        self.matrix = matrix
        self.data = data
        self.canvas = matrix.CreateFrameCanvas()
        self.scrolling_text_pos = self.canvas.width
        self.animation_time = 0

    def render(self):
        while True:

            # Draw the current game
            self.__draw_game()

    def __draw_game(self) -> NoReturn:
        if len(self.data.boardStateQueue) >= 1 and self.data.boardStateQueue[0]['showTime'] < datetime.now():
            self.data.activeState = self.data.boardStateQueue.pop()
        game = self.data.activeState
        if game is None:
            return  # should only happen on init due to delay
        bgcolor = self.data.config.scoreboard_colors.color("default.background")
        self.canvas.Fill(bgcolor["r"], bgcolor["g"], bgcolor["b"])
        layout = self.data.config.layout
        colors = self.data.config.scoreboard_colors
        awayTeamIcon = self.data.awayTeamImg
        homeTeamIcon = self.data.homeTeamImg
        try:
            if self.data.gamePhase == 'END_GAME':
                pass  # todo
            elif self.data.gamePhase == 'INGAME':
                for team in ["away", "home"]:

                    # render team icons
                    x_offset = layout.coords("football.in_game." + team + "_team_logo.x")
                    y_offset = layout.coords("football.in_game." + team + "_team_logo.y")
                    icon_size = layout.coords("football.in_game." + team + "_team_logo.size")
                    for x in range(icon_size):
                        for y in range(icon_size):
                            color = awayTeamIcon.getpixel((x, y)) if team == "away" else homeTeamIcon.getpixel(
                                (x, y))
                            self.canvas.SetPixel(x + x_offset, y + y_offset, color[0], color[1], color[2])

                    # render scores
                    coords = {'x': layout.coords("football.in_game." + team + "_score.x"),
                              'y': layout.coords("football.in_game." + team + "_score.y")}
                    font = layout.font("football.in_game." + team + "_score")
                    color = colors.graphics_color("football.in_game.scores")
                    if team == 'away':
                        graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color,
                                          str(game['headData']['awayScore']))
                    else:
                        canvashelper.writeRightToLeft(self.canvas, font, coords, str(game['headData']['homeScore']),
                                                      color)

                # quarter
                font = layout.font("football.in_game.quarter_num")
                coords = {'x': layout.coords("football.in_game.quarter_num.x"),
                          'y': layout.coords("football.in_game.quarter_num.y")}
                color = colors.graphics_color("football.in_game.quarter")
                text = game['headData']['quarter_num']
                graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, str(text))
                if text in [1, 2, 3, 4]:
                    text = game['headData']['quarter_ordinal']
                    font = layout.font("football.in_game.quarter_ordinal")
                    coords = {'x': layout.coords("football.in_game.quarter_ordinal.x"),
                              'y': layout.coords("football.in_game.quarter_ordinal.y")}
                    graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, text)

                # play time
                font = layout.font("football.in_game.play_time_right")
                coords = {'x': layout.coords("football.in_game.play_time_right.x"),
                          'y': layout.coords("football.in_game.play_time_right.y")}
                color = colors.graphics_color("football.in_game.play_time")
                text = str(game['headData']['minutes']) + ":" + str(game['headData']['seconds'])
                canvashelper.writeRightToLeft(self.canvas, font, coords, text, color)

                # down & dist
                font = layout.font("football.in_game.down_and_distance")
                font_ordinal = layout.font("football.in_game.down_and_distance.ordinal")
                coords = {'x': layout.coords("football.in_game.play_time_right.x"),  # FIXME
                          'y': layout.coords("football.in_game.play_time_right.y")}
                color = colors.graphics_color("football.in_game.play_time")
                text = str(game['headData']['minutes']) + ":" + str(game['headData']['seconds'])
                canvashelper.writeRightToLeft(self.canvas, font, coords, text, color)

                # special image
                specialImg = self.data.specialBannerImg
                if specialImg is not None:
                    x_offset = layout.coords("football.special_banner.x")
                    y_offset = layout.coords("football.special_banner.y")
                    height = layout.coords("football.special_banner.height")
                    for x in range(128):  # fixme
                        for y in range(height):
                            color = specialImg.getpixel((x, y))
                            self.canvas.SetPixel(x + x_offset, y + y_offset, color[0], color[1], color[2])

        except Exception as e:
            debug.error('renderers/nfl/__init__: ' + str(e))

        self.canvas = self.matrix.SwapOnVSync(self.canvas)
        refresh_rate = self.data.config.scrolling_speed
        time.sleep(refresh_rate)
