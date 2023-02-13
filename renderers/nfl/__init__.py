import json
import time
import traceback
from datetime import datetime

from typing import NoReturn

from PIL import Image


import debug
import utils
from data.nfl import NflManager
from data.nfl.api.bannertype import BannerType
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
        self.stateShowtime = None
        self.scrollRate = self.data.config.nfl_scroll_rate
        self.possessionImg = Image.open('assets/img/possess-indicator.png')
        self.delayImg = Image.open('assets/img/delay.png')
        self.scrollX0 = self.canvas.width // 8
        self.centerX = self.canvas.width // 2

    def render(self):
        while True:

            # Draw the current game
            self.__draw_game()

    def __draw_game(self) -> NoReturn:
        try:

            if self.data.gamePhase == 'PREGAME':
                img = self.data.sblviiPregameImg
                for x in range(img.width):
                    for y in range(img.height):
                        color = img.getpixel((x, y))
                        self.canvas.SetPixel(x, y, color[0], color[1], color[2])
                self.canvas = self.matrix.SwapOnVSync(self.canvas)
                time.sleep(30)
                return

            print(datetime.now().strftime("%H:%M:%S"))
            print('BoardStateQueue START')
            for i in range(len(self.data.boardStateQueue)):
                item = self.data.boardStateQueue[i]
                print("| State: " + str(item['state'].type.name) + "/"
                      + str(item['state'].topLine) + "/" + str(item['state'].bottomLine) + "/" + str(item['headData']['minutes']) + ":" +str(item['headData']['seconds'])
                      + ' | showTime: ' + item['showTime'].strftime("%H:%M:%S"))
            print('BoardStateQueue END')
            if len(self.data.boardStateQueue) >= 1 and self.data.boardStateQueue[0]['showTime'] < datetime.now() and not self.data.boardStateQueueMutex:
                if self.data.activeState is None \
                        or self.data.activeState['state'].topLine != self.data.boardStateQueue[0]['state'].topLine \
                        or self.data.activeState['state'].bottomLine != self.data.boardStateQueue[0]['state'].bottomLine:
                    self.stateShowtime = datetime.now()  # TODO move all this shit to the manager
                # self.data.prevState = self.data.activeState.copy()
                self.data.activeState = self.data.boardStateQueue[0]
                self.data.boardStateQueue = self.data.boardStateQueue[1:len(self.data.boardStateQueue)]
                print('QUEUE POP')
            game = self.data.activeState
            if game is None:  # should only happen on init due to delay
                for x in range(0, self.canvas.width):
                    for y in range(0, self.canvas.height):
                        color = self.delayImg.getpixel((x, y))
                        self.canvas.SetPixel(x, y, color[0], color[1], color[2])
                self.canvas = self.matrix.SwapOnVSync(self.canvas)
                return
            bgcolor = self.data.config.scoreboard_colors.color("default.background")
            self.canvas.Fill(bgcolor["r"], bgcolor["g"], bgcolor["b"])
            layout = self.data.config.layout
            colors = self.data.config.scoreboard_colors
            awayTeamIcon = self.data.awayTeamImg
            homeTeamIcon = self.data.homeTeamImg

            if self.data.gamePhase == 'END_GAME':
                pass  # todo
            elif self.data.gamePhase == 'INGAME':
                for team in ["away", "home"]:

                    # render team icons
                    x_offset = layout.coords("football.in_game." + team + "_team_logo.x")
                    y_offset = layout.coords("football.in_game." + team + "_team_logo.y")
                    icon_size = layout.coords("football.in_game." + team + "_team_logo.size")
                    for awayX in range(icon_size):
                        for y in range(icon_size):
                            color = awayTeamIcon.getpixel((awayX, y)) if team == "away" else homeTeamIcon.getpixel(
                                (awayX, y))
                            self.canvas.SetPixel(awayX + x_offset, y + y_offset, color[0], color[1], color[2])

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
                down = game['headData']['down']
                distance = '&' + str(game['headData']['distance'])
                width = 2 * font_ordinal['size']['width'] + (len(str(distance)) + 1) * font['size']['width']
                startX = abs(64 - (width // 2))
                coords = {'x': startX,
                          'y': layout.coords("football.in_game.down_and_distance.y")}
                color = colors.graphics_color("football.in_game.down_and_distance")
                graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, down[0])
                coords = {'x': startX + font['size']['width'],
                          'y': layout.coords("football.in_game.down_and_distance.ordinal.y")}
                graphics.DrawText(self.canvas, font_ordinal["font"], coords["x"], coords["y"], color, down[1:])
                coords = {'x': startX + font['size']['width'] + 2*font['size']['width'],
                          'y': layout.coords("football.in_game.down_and_distance.y")}
                graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, distance)

                # line of scrimmage
                font = layout.font("football.in_game.line_of_scrimmage")
                text = game['headData']['lineOfScrimmage']

                coords = {'x': utils.center_text_position(text, self.centerX, font['size']['width']),
                          'y': layout.coords("football.in_game.line_of_scrimmage.y")}
                color = colors.graphics_color("football.in_game.line_of_scrimmage")
                graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, text)

                # timeouts
                awayTimeouts = game['headData']['awayTimeoutsLeft']
                homeTimeouts = game['headData']['homeTimeoutsLeft']
                width = layout.coords("football.in_game.timeouts.width")
                height = layout.coords("football.in_game.timeouts.height")
                yBottom = layout.coords("football.in_game.timeouts.y")
                yTop = yBottom + height
                color = colors.graphics_color("football.in_game.timeouts")
                awayX = layout.coords("football.in_game.timeouts.away_x")
                homeX = layout.coords("football.in_game.timeouts.home_x")
                for t in range(1, 4):
                    if t > awayTimeouts:  # timeout spent
                        graphics.DrawLine(self.canvas, awayX, yBottom, awayX + width, yBottom, color)
                        graphics.DrawLine(self.canvas, awayX, yTop, awayX + width, yTop, color)
                        graphics.DrawLine(self.canvas, awayX, yBottom, awayX, yTop, color)
                        graphics.DrawLine(self.canvas, awayX + width, yBottom, awayX + width, yTop, color)
                    else:  # timeout unspent
                        for y in range(yBottom, yTop):
                            graphics.DrawLine(self.canvas, awayX, y, awayX + width, y, color)
                    if t > homeTimeouts:
                        graphics.DrawLine(self.canvas, homeX, yBottom, homeX - width, yBottom, color)
                        graphics.DrawLine(self.canvas, homeX, yTop, homeX - width, yTop, color)
                        graphics.DrawLine(self.canvas, homeX, yBottom, homeX, yTop, color)
                        graphics.DrawLine(self.canvas, homeX - width, yBottom, homeX - width, yTop, color)
                    else:
                        for y in range(yBottom, yTop):
                            graphics.DrawLine(self.canvas, homeX, y, homeX - width, y, color)
                    awayX += (width + 2)
                    homeX -= (width + 2)

                # possession
                possessionImgWidth = self.possessionImg.width
                possessionImgHeight = self.possessionImg.height
                possessTeam = game['headData']['possessingTeam']
                yBottom = layout.coords("football.in_game.possession.y")
                if possessTeam == 'AWAY':
                    xLeft = layout.coords("football.in_game.possession.away_x")
                elif possessTeam == 'HOME':
                    xLeft = layout.coords("football.in_game.possession.home_x")
                else:
                    debug.error('Unknown possession team: ' + possessTeam)
                    xLeft = -1
                for x in range(0, possessionImgWidth):
                    for y in range(0, possessionImgHeight):
                        color = self.possessionImg.getpixel((x, y))
                        self.canvas.SetPixel(xLeft + x, yBottom + y, color[0], color[1], color[2])

                # center text
                stateType = game['state'].type.name
                if stateType in [BannerType.TOUCHDOWN.name, BannerType.SAFETY.name]:
                    bannerBottom = layout.coords("football.in_game.upper_banner.bottom_y")
                    bannerHeight = layout.coords("football.in_game.upper_banner.touchdown.height")
                    if stateType == BannerType.TOUCHDOWN.name:
                        text = 'TOUCHDOWN'
                        color = colors.graphics_color("football.in_game.upper_banner_touchdown_bg")
                    else:
                        text = 'SAFETY'
                        color = colors.graphics_color("football.in_game.upper_banner_safety_bg")
                    for y in range(bannerBottom, bannerBottom + bannerHeight):
                        graphics.DrawLine(self.canvas, 0, y, self.canvas.width, y, color)
                    font = layout.font("football.in_game.upper_banner.touchdown")
                    color = colors.graphics_color("football.in_game.upper_banner_touchdown_text")
                    coords = {'x': utils.center_text_position(text, self.centerX, font['size']['width']),
                              'y': layout.coords("football.in_game.down_and_distance.y")}
                    graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, text)
                elif stateType != BannerType.EMPTY.name:
                    if stateType == BannerType.STANDARD.name:
                        color = colors.graphics_color("football.in_game.upper_top_line")
                    elif stateType == BannerType.OTHER_SCORE.name:
                        color = colors.graphics_color("football.in_game.upper_banner_other_score")
                    elif stateType == BannerType.ALERT.name:
                        color = colors.graphics_color("football.in_game.upper_banner_alert")
                    elif stateType == BannerType.TURNOVER.name:
                        color = colors.graphics_color("football.in_game.upper_banner_turnover")
                    else:
                        color = None
                        debug.error('NflRenderer: unknown state ' + stateType)
                    font = layout.font("football.in_game.upper_banner.top")
                    text = game['state'].topLine
                    coords = {'x': utils.center_text_position(text, self.centerX, font['size']['width']),
                              'y': layout.coords("football.in_game.upper_banner.top.y")}
                    self.drawText(text, font, layout.coords("football.in_game.upper_banner.top.y"), color)
                    # graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, text)
                    font = layout.font("football.in_game.upper_banner.bottom")
                    text = game['state'].bottomLine
                    coords = {'x': utils.center_text_position(text, self.centerX, font['size']['width']),
                              'y': layout.coords("football.in_game.upper_banner.bottom.y")}
                    self.drawText(text, font, layout.coords("football.in_game.upper_banner.bottom.y"), color)
                    # graphics.DrawText(self.canvas, font["font"], coords["x"], coords["y"], color, text)

                # banner image
                bannerImg = self.data.specialBannerImg
                if bannerImg is not None:
                    x_offset = layout.coords("football.special_banner.x")
                    y_offset = layout.coords("football.special_banner.y")
                    height = layout.coords("football.special_banner.height")
                    for awayX in range(self.canvas.width):
                        for y in range(height):
                            color = bannerImg.getpixel((awayX, y))
                            self.canvas.SetPixel(awayX + x_offset, y + y_offset, color[0], color[1], color[2])

        except Exception:
            debug.error(traceback.format_exc())

        self.canvas = self.matrix.SwapOnVSync(self.canvas)
        refresh_rate = self.data.config.scrolling_speed
        time.sleep(refresh_rate)

    def drawText(self, text, font, y, color):
        textWidth = font['size']['width'] * len(text)  # fixme
        if textWidth > self.canvas.width:
            delta = datetime.now() - self.stateShowtime
            xOffset = round((delta.seconds * 1000000 + delta.microseconds)/1000000 * self.scrollRate)
            x = self.scrollX0 - xOffset
        else:
            x = utils.center_text_position(text, self.canvas.width // 2, font['size']['width'])

        graphics.DrawText(self.canvas, font["font"], x, y, color, text)
