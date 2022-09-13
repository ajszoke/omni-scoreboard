import time
from datetime import datetime

import statsapi

import debug
from data.update import UpdateStatus

API_FIELDS = (
    "gameData,game,id,datetime,dateTime,officialDate,flags,noHitter,perfectGame,status,detailedState,abstractGameState,"
    + "reason,probablePitchers,teams,home,away,abbreviation,teamName,players,id,boxscoreName,fullName,liveData,plays,"
    + "currentPlay,result,eventType,playEvents,isPitch,pitchData,startSpeed,details,type,code,description,decisions,"
    + "winner,loser,save,id,linescore,outs,balls,strikes,note,inningState,currentInning,currentInningOrdinal,offense,"
    + "batter,inHole,onDeck,first,second,third,defense,pitcher,boxscore,teams,runs,players,battingOrder,stats,batting,pitching,fielding,seasonStats,batting,homeRuns,avg,ops,pitching,wins,"
    + "losses,saves,era,hits,errors,gameStats,battingOrder,weather,condition,temp,wind"
)

SCHEDULE_API_FIELDS = "dates,date,games,status,detailedState,abstractGameState,reason"

GAME_UPDATE_RATE = 10


class Game:
    @staticmethod
    def from_ID(game_id, date):
        game = Game(game_id, date)
        if game.update(True) == UpdateStatus.SUCCESS:
            return game
        return None

    def __init__(self, game_id, date):
        self.game_id = game_id
        self.date = date.strftime("%Y-%m-%d")
        self.starttime = time.time()
        self._data = {}
        self._status = {}
        self.win_probabilities = {}

    def update(self, force=False) -> UpdateStatus:
        if force or self.__should_update():
            self.starttime = time.time()
            try:
                debug.log("Fetching data for game %s", str(self.game_id))
                self._data = statsapi.get("game", {"gamePk": self.game_id})
                self.win_probabilities = statsapi.get("game_contextMetrics", {"gamePk": self.game_id})
                self._status = self._data["gameData"]["status"]
                if self._data["gameData"]["datetime"]["officialDate"] > self.date:
                    # this is odd, but if a game is postponed then the 'game' endpoint gets the rescheduled game
                    debug.log("Getting game status from schedule for game with strange date!")
                    try:
                        scheduled = statsapi.get(
                            "schedule", {"gamePk": self.game_id, "sportId": 1, "fields": SCHEDULE_API_FIELDS}
                        )
                        self._status = next(
                            g["games"][0]["status"] for g in scheduled["dates"] if g["date"] == self.date
                        )
                    except:
                        debug.error("Failed to get game status from schedule")

                return UpdateStatus.SUCCESS
            except:
                debug.exception("Networking Error while refreshing the current game data.")
                return UpdateStatus.FAIL
        return UpdateStatus.DEFERRED

    def datetime(self):
        time = self._data["gameData"]["datetime"]["dateTime"]
        return datetime.fromisoformat(time.replace("Z", "+00:00"))

    def home_name(self):
        return self._data["gameData"]["teams"]["home"]["teamName"]
    
    def home_abbreviation(self):
        return self._data["gameData"]["teams"]["home"]["abbreviation"]
    
    def pregame_weather(self):
        try:
            wx = self._data["gameData"]["weather"]["condition"] + " and " + self._data["gameData"]["weather"]["temp"] + u"\N{DEGREE SIGN}" + " wind " + self._data["gameData"]["weather"]["wind"]
        except KeyError:
            return None
        else:
            return wx 
    
    def away_name(self):
        return self._data["gameData"]["teams"]["away"]["teamName"]

    def away_abbreviation(self):
        return self._data["gameData"]["teams"]["away"]["abbreviation"]

    def status(self):
        return self._status["detailedState"]

    def home_score(self):
        return self._data["liveData"]["linescore"]["teams"]["home"].get("runs", 0)

    def away_score(self):
        return self._data["liveData"]["linescore"]["teams"]["away"].get("runs", 0)

    def home_hits(self):
        return self._data["liveData"]["linescore"]["teams"]["home"].get("hits", 0)

    def away_hits(self):
        return self._data["liveData"]["linescore"]["teams"]["away"].get("hits", 0)

    def home_errors(self):
        return self._data["liveData"]["linescore"]["teams"]["home"].get("errors", 0)

    def away_errors(self):
        return self._data["liveData"]["linescore"]["teams"]["away"].get("errors", 0)

    def winning_team(self):
        if self._status["abstractGameState"] == "Final":
            if self.home_score() > self.away_score():
                return "home"
            if self.home_score() < self.away_score():
                return "away"
        return None

    def losing_team(self):
        winner = self.winning_team()
        if winner is not None:
            if winner == "home":
                return "away"
            return "home"
        return None

    def inning_state(self):
        return self._data["liveData"]["linescore"].get("inningState", "Top")

    def inning_number(self):
        return self._data["liveData"]["linescore"].get("currentInning", 0)

    def inning_ordinal(self):
        return self._data["liveData"]["linescore"].get("currentInningOrdinal", 0)

    def win_probability(self):
        home_prob = self.win_probabilities["homeWinProbability"]
        away_prob = self.win_probabilities["awayWinProbability"]
        team_multiplier = -1 if home_prob > away_prob else 1
        return team_multiplier * max(int(home_prob), int(away_prob))

    def batting_team(self):
        return "away" if self._data["liveData"]["linescore"].get("inningState", 0) in ["Top", "End"] else "home"

    def pitching_team(self):
        return "home" if self._data["liveData"]["linescore"].get("inningState", 0) in ["Top", "End"] else "away"

    def features_team(self, team):
        return team in [
            self._data["gameData"]["teams"]["away"]["teamName"],
            self._data["gameData"]["teams"]["home"]["teamName"],
        ]

    def is_no_hitter(self):
        return self._data["gameData"]["flags"]["noHitter"]

    def is_perfect_game(self):
        return self._data["gameData"]["flags"]["perfectGame"]

    def man_on(self, base):
        try:
            id = self._data["liveData"]["linescore"]["offense"][base]["id"]
        except KeyError:
            return None
        else:
            return id

    def full_name(self, player):
        ID = Game._format_id(player)
        return self._data["gameData"]["players"][ID]["fullName"]

    def boxscore_name(self, player):
        ID = Game._format_id(player)
        name = self._data["gameData"]["players"][ID]["boxscoreName"]
        if "," in name:
            comma_idx = name.index(",")
            name = name[:comma_idx]
        return name

    def pitcher_stat(self, player, stat, team=None):
        ID = Game._format_id(player)

        if team is not None:
            stats = self._data["liveData"]["boxscore"]["teams"][team]["players"][ID]["seasonStats"]["pitching"]
        else:
            try:
                stats = self._data["liveData"]["boxscore"]["teams"]["home"]["players"][ID]["seasonStats"]["pitching"]
            except:
                try:
                    stats = self._data["liveData"]["boxscore"]["teams"]["away"]["players"][ID]["seasonStats"][
                        "pitching"
                    ]
                except:
                    return ""

        return stats[stat]

    def probable_pitcher_id(self, team):
        try:
            return self._data["gameData"]["probablePitchers"][team]["id"]
        except:
            return None

    def decision_pitcher_id(self, decision):
        try:
            return self._data["liveData"]["decisions"][decision]["id"]
        except:
            return None

    def batter(self):
        try:
            batter_id = self._data["liveData"]["linescore"]["offense"]["batter"]["id"]
            return self.boxscore_name(batter_id)
        except:
            return ""

    def batter_order_num(self):
        try:
            team = self.batting_team()
            batter_id = Game._format_id(self._data["liveData"]["linescore"]["offense"]["batter"]["id"])
            return int(
                int(self._data["liveData"]["boxscore"]["teams"][team]["players"][batter_id]["battingOrder"]) / 100)
        except:
            return ""

    def batter_stats(self):
        batter_id = Game._format_id(self._data["liveData"]["linescore"]["offense"]["batter"]["id"])
        if batter_id == "":
            return ""
        team = self.batting_team()
        box_root = self._data["liveData"]["boxscore"]["teams"][team]["players"][batter_id]["stats"]["batting"]
        season_root = self._data["liveData"]["boxscore"]["teams"][team]["players"][batter_id]["seasonStats"]["batting"]
        stats = {}

        if box_root != {}:
            stats = {
                "at_bats": box_root["atBats"],
                "hits": box_root["hits"],
                "hr": box_root["homeRuns"],
                "k": box_root["strikeOuts"],
                "bb": box_root["baseOnBalls"],
                "3b": box_root["triples"],
                "2b": box_root["doubles"],
                "sac": box_root["sacBunts"] + box_root["sacFlies"],
                "gitp": box_root["groundIntoTriplePlay"],
                "gidp": box_root["groundIntoDoublePlay"]
            }
        if season_root != {}:
            season_stats = {
                "season_hr": season_root["homeRuns"],
                "avg": season_root["avg"],
                "ops": season_root["ops"],
            }
            stats.update(season_stats)
        return stats

    def in_hole(self):
        try:
            batter_id = self._data["liveData"]["linescore"]["offense"]["inHole"]["id"]
            return self.boxscore_name(batter_id)
        except:
            return ""

    def on_deck(self):
        try:
            batter_id = self._data["liveData"]["linescore"]["offense"]["onDeck"]["id"]
            return self.boxscore_name(batter_id)
        except:
            return ""

    def pitcher(self):
        try:
            pitcher_id = self._data["liveData"]["linescore"]["defense"]["pitcher"]["id"]
            return self.boxscore_name(pitcher_id)
        except:
            return ""

    def pitcher_stats(self):
        pitcher_id = Game._format_id(self._data["liveData"]["linescore"]["defense"]["pitcher"]["id"])
        if pitcher_id == "":
            return ""
        team = self.pitching_team()
        box_root = self._data["liveData"]["boxscore"]["teams"][team]["players"][pitcher_id]["stats"]["pitching"]
        season_root = self._data["liveData"]["boxscore"]["teams"][team]["players"][pitcher_id]["seasonStats"]["pitching"]
        stats = {}
        if box_root != {}:
            stats = {
                "ip": box_root["inningsPitched"],
                "total_pitches": box_root["numberOfPitches"],
                "hr": box_root["homeRuns"],
                "er": box_root["earnedRuns"],
                "hits": box_root["hits"],
                "walks": box_root["baseOnBalls"],
                "strikeouts": box_root["strikeOuts"]
            }
        if season_root != {}:
            season_stats = {
                "era": season_root["era"],
                "whip": season_root["whip"]
            }
            stats.update(season_stats)
        return stats

    def balls(self):
        return self._data["liveData"]["linescore"].get("balls", 0)

    def strikes(self):
        return self._data["liveData"]["linescore"].get("strikes", 0)

    def outs(self):
        return self._data["liveData"]["linescore"].get("outs", 0)

    def last_pitch(self):
        try:
            play = self._data["liveData"]["plays"].get("currentPlay", {}).get("playEvents", [{}])[-1]
            if play.get("isPitch", False):
                return play["pitchData"].get("startSpeed", 0), play["details"]["type"]["code"], play["details"]["type"]["description"]
        except: 
            return None
    def note(self):
        try:
            return self._data["liveData"]["linescore"]["note"]
        except:
            return None

    def reason(self):
        try:
            return self._status["reason"]
        except:
            try:
                return self._status["detailedState"].split(":")[1].strip()
            except:
                return None

    def current_play_result(self):
        result = self._data["liveData"]["plays"].get("currentPlay", {}).get("result", {}).get("eventType", "")
        if result == "strikeout" and (
            "called" in self._data["liveData"]["plays"].get("currentPlay", {}).get("result", {}).get("description", "")
        ):
            result += "_looking"
        return result

    def __should_update(self):
        endtime = time.time()
        time_delta = endtime - self.starttime
        return time_delta >= GAME_UPDATE_RATE

    @staticmethod
    def _format_id(player):
        return player if "ID" in str(player) else "ID" + str(player)
