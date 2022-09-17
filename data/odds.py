import time

import json
from datetime import datetime

import requests

import debug
from data.update import UpdateStatus

ODDS_UPDATE_RATE = 5 * 60  # 10 minutes between odds updates


class Odds:
    def __init__(self):
        self.api_url = "https://www.bovada.lv/services/sports/event/coupon/events/A/description/baseball/mlb?marketFilterId=def&preMatchOnly=true&eventsLimit=5000&lang=en"

        self.games = {}


        # Force an update for our initial data
        self.update(True)

    # Make a call to the open weather maps API and update our instance variables
    # Pass True if you need to ignore the update rate (like for our first update)
    def update(self, force=False) -> UpdateStatus:
        if force or self.__should_update():
            debug.log("Odds should update!")
            self.starttime = time.time()
            try:
                response = requests.get(self.api_url)
                if response.status_code == 200:
                    response_body = json.loads(response.text)
                    for game in response_body[0]["events"]:
                        if datetime.now().strftime("%Y%m%d") in game["link"]:
                            new_game = {}
                            for team in game["competitors"]:
                                new_game[team["name"]] = {}
                            for market in game["displayGroups"][0]["markets"]:
                                if market["descriptionKey"] == "Main Dynamic Asian Runline":
                                    for team in market["outcomes"]:
                                        new_game[team["description"]]["runline"] = team["price"]["handicap"]
                                        new_game[team["description"]]["runline_odds"] = team["price"]["american"]
                                elif market["descriptionKey"] == "Head To Head":
                                    for team in market["outcomes"]:
                                        new_game[team["description"]]["moneyline"] = team["price"]["american"]
                                else:
                                    continue

                        self.games.update(new_game)
                    return UpdateStatus.SUCCESS
                else:
                    debug.error(response.text)
                    return UpdateStatus.FAIL

            except BaseException as e:
                debug.error(e)
                return UpdateStatus.FAIL

    def retrieve_team_odds_str(self, long_team_name):
        try:
            odds = self.games[long_team_name]
            runline = odds["runline"]
            if not runline.startswith('-'):
                runline = "+" + runline
            return "{} ({} {})".format(odds["moneyline"], runline, odds["runline_odds"])
        except KeyError:
            return ""

    def __should_update(self):
        endtime = time.time()
        time_delta = endtime - self.starttime
        return time_delta >= ODDS_UPDATE_RATE
