import tzlocal

from data import Odds
from data.game import Game

PITCHER_TBD = "TBD"


class Pregame:
    def __init__(self, game: Game, time_format, odds: Odds):
        self.home_team = game.home_abbreviation()
        self.away_team = game.away_abbreviation()
        self.pregame_weather = game.pregame_weather()
        self.time_format = time_format
        if odds is not None:
            self.teamOdds = {
                "away": odds.retrieve_team_odds_str(game.away_longname()),
                "home": odds.retrieve_team_odds_str(game.home_longname())
            }

        try:
            self.start_time = self.__convert_time(game.datetime())
        except:
            self.start_time = "TBD"

        self.status = game.status()

        away_id = game.probable_pitcher_id("away")
        if away_id is not None:
            self.away_starter = {
                "name": game.boxscore_name(away_id),
                "wins": game.pitcher_stat(away_id, "wins", "away"),
                "losses": game.pitcher_stat(away_id, "losses", "away"),
                "era": game.pitcher_stat(away_id, "era", "away")
            }
            self.away_record = game.away_record()
        else:
            self.away_starter = PITCHER_TBD

        home_id = game.probable_pitcher_id("home")
        if home_id is not None:
            self.home_starter = {
                "name": game.boxscore_name(home_id),
                "wins": game.pitcher_stat(home_id, "wins", "home"),
                "losses": game.pitcher_stat(home_id, "losses", "home"),
                "era": game.pitcher_stat(home_id, "era", "home")
            }
            self.home_record = game.home_record()
        else:
            self.home_starter = PITCHER_TBD

    def get_away_starter(self):
        return self.away_starter

    def get_away_odds(self):
        if self.teamOdds is not None:
            return self.teamOdds["away"]
        else:
            return ""

    def get_home_starter(self):
        return self.home_starter

    def get_home_odds(self):
        if self.teamOdds is not None:
            return self.teamOdds["home"]
        else:
            return ""

    def __convert_time(self, game_time_utc):
        """Converts MLB's pregame times (UTC) into the local time zone"""
        time_str = "{}:%M".format(self.time_format)
        if self.time_format == "%-I":
            time_str += " %p"
        return game_time_utc.astimezone(tzlocal.get_localzone()).strftime(time_str)

    def __str__(self):
        s = "<{} {}> {} @ {}; {}; {} vs {}".format(
            self.__class__.__name__,
            hex(id(self)),
            self.away_team,
            self.home_team,
            self.start_time,
            self.away_starter,
            self.home_starter,
        )
        return s
