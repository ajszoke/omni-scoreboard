import json
import sys

if sys.version_info <= (3, 5):
    print("Error: Please run with python3")
    sys.exit(1)

import logging
import os
import threading
import time

from PIL import Image

import debug
from data import Data
from data.config import Config
from renderers.main import MainRenderer
from utils import args, led_matrix_options
from version import SCRIPT_NAME, SCRIPT_VERSION

import requests
import uuid
import re
from datetime import date

try:
    from rgbmatrix import RGBMatrix, __version__

    emulated = False
except ImportError:
    from RGBMatrixEmulator import RGBMatrix, version

    emulated = True


def main(matrix, config_base):

    # playList = []
    # favoriteTeam = "Vikings"
    # resp = requests.get(url="https://p.nfltags.com/nfl/NflUmdComponents.NFLToken.js",
    #                     headers={
    #                         "sec-fetch-dest": "empty",
    #                         "sec-fetch-mode": "no-cors",
    #                         "sec-fetch-site": "same-site",
    #                         "sec-gpc": "1",
    #                         "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36",
    #                         "Host": "p.nfltags.com",
    #                         "origin": "https://www.nfl.com",
    #                         "referer": "https://www.nfl.com/"
    #                     })
    # responseBody = resp.text
    # startIdx = responseBody.find("production:{", responseBody.find("desktop:{"))
    # endIdx = responseBody.find("},", startIdx)
    # authSubstring = responseBody[startIdx:endIdx]
    # clientKey = re.match(".*nflClientKey:\"([^\"]*)\".*", authSubstring).groups()[0]
    # clientSecret = re.match(".*nflClientSecret:\"([^\"]*)\".*", authSubstring).groups()[0]
    #
    # resp = requests.post(url="https://api.nfl.com/identity/v3/token",
    #                      headers={
    #                          "sec-fetch-dest": "empty",
    #                          "sec-fetch-mode": "cors",
    #                          "sec-fetch-site": "same-site",
    #                          "sec-gpc": "1",
    #                          "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36",
    #                          "Host": "api.nfl.com",
    #                          "path": "/identity/v3/token",
    #                          "origin": "https://www.nfl.com",
    #                          "referer": "https://www.nfl.com/"
    #                      },
    #                      data={
    #                          "clientKey": clientKey,
    #                          "clientSecret": clientSecret,
    #                          "deviceId": str(uuid.uuid4()),
    #                          "deviceInfo": "eyJtb2RlbCI6ImRlc2t0b3AiLCJ2ZXJzaW9uIjoiQ2hyb21lIiwib3NOYW1lIjoiV2luZG93cyIsIm9zVmVyc2lvbiI6IjEwIn0=",
    #                          "networkType": "other",
    #                          "useRefreshToken": "true"
    #                      })
    # # if (resp.status_code == 200):
    # responseBody = json.loads(resp.text)
    # refreshToken = responseBody["refreshToken"]
    # accessToken = responseBody["accessToken"]
    #
    # resp = requests.get(url="https://api.nfl.com/football/v2/weeks/date/{}".format(date.today().strftime("%Y-%m-%d")),
    #                     headers={
    #                         "Authorization": "Bearer " + accessToken
    #                     })
    # responseBody = json.loads(resp.text)
    # season = responseBody["season"]
    # seasonType = responseBody["seasonType"]
    # week = responseBody["week"]
    #
    # for week in range(1, 3):
    #     gameId = ""
    #     resp = requests.get(url="https://api.nfl.com/experience/v1/games?season={season}&seasonType={seasonType}&week={week}"
    #                             .format(season=season, seasonType=seasonType, week=week),
    #                         headers={
    #                             "Authorization": "Bearer " + accessToken
    #                         })
    #     responseBody = json.loads(resp.text)
    #     for game in responseBody["games"]:
    #         # if game["homeTeam"]["nickName"] == favoriteTeam or game["awayTeam"]["nickName"] == favoriteTeam:
    #         gameId = game["id"]
    #         resp = requests.get(url="https://api.nfl.com/experience/v1/gamedetails/{}".format(gameId),
    #                             headers={
    #                                 "Authorization": "Bearer " + accessToken
    #                             })
    #         responseBody = json.loads(resp.text)
    #         for play in responseBody["data"]["viewer"]["gameDetail"]["plays"]:
    #             playList.append(play["playDescription"])
    # with open('./playlist.txt', 'w') as f:
    #     for play in playList:
    #         f.write(f"{play}\n")


    # Read scoreboard options from config.json if it exists
    config = Config(config_base, matrix.width, matrix.height)
    logger = logging.getLogger("mlbled")
    if config.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.WARNING)

    # Print some basic info on startup
    debug.info("%s - v%s (%sx%s)", SCRIPT_NAME, SCRIPT_VERSION, matrix.width, matrix.height)

    if emulated:
        debug.log("rgbmatrix not installed, falling back to emulator!")
        debug.log("Using RGBMatrixEmulator version %s", version.__version__)
    else:
        debug.log("Using rgbmatrix version %s", __version__)

    # Draw startup screen
    logo = "assets/img/logo-w" + str(matrix.width) + "h" + str(matrix.height) + ".png"

    # MLB image disabled when using renderer, for now.
    # see: https://github.com/ty-porter/RGBMatrixEmulator/issues/9#issuecomment-922869679
    if os.path.exists(logo) and not emulated:
        logo = Image.open(logo)
        matrix.SetImage(logo.convert("RGB"))
        logo.close()

    # Create a new data object to manage the MLB data
    # This will fetch initial data from MLB
    data = Data(config)

    # create render thread
    render = threading.Thread(target=__render_main, args=[matrix, data], name="render_thread", daemon=True)
    time.sleep(1)
    render.start()

    screen = data.get_screen_type()
    if screen == "news":
        __refresh_offday(render, data)
    elif screen == "standings":
        __refresh_standings(render, data)
    else:
        __refresh_games(render, data)


def __refresh_offday(render_thread, data):  # type: (threading.Thread, Data) -> None
    debug.log("Main has selected the offday information to refresh")
    while render_thread.is_alive():
        time.sleep(30)
        data.refresh_weather()
        data.refresh_news_ticker()


def __refresh_standings(render_thread, data):  # type: (threading.Thread, Data) -> None
    if data.standings.populated():
        debug.log("Main has selected the standings to refresh")
        while render_thread.is_alive():
            time.sleep(30)
            data.refresh_standings()
    else:
        __refresh_offday(render_thread, data)


def __refresh_games(render_thread, data):  # type: (threading.Thread, Data) -> None
    debug.log("Main has selected the game and schedule information to refresh")

    starttime = time.time()
    promise_game = data.schedule.games_live()

    while render_thread.is_alive():
        time.sleep(0.5)
        data.refresh_schedule()
        if data.config.standings_no_games:
            if not data.schedule.games_live():
                data.refresh_standings()
                continue
            # make sure a game is poulated
            elif not promise_game:
                promise_game = True
                data.advance_to_next_game()

        rotate = data.should_rotate_to_next_game()
        if data.schedule.games_live() and not rotate:
            data.refresh_game()

        endtime = time.time()
        time_delta = endtime - starttime
        rotate_rate = data.config.rotate_rate_for_status(data.current_game.status())

        if time_delta >= rotate_rate and data.scrolling_finished:
            starttime = time.time()
            if rotate:
                data.advance_to_next_game()


def __render_main(matrix, data):
    MainRenderer(matrix, data).render()


if __name__ == "__main__":
    # Check for led configuration arguments
    command_line_args = args()
    matrixOptions = led_matrix_options(command_line_args)

    # Initialize the matrix
    matrix = RGBMatrix(options=matrixOptions)
    try:
        config, _ = os.path.splitext(command_line_args.config)
        main(matrix, config)
    except:
        debug.exception("Untrapped error in main!")
        sys.exit(1)
    finally:
        matrix.Clear()
