import re

from data.nfl.nflboardstatedto import NflBoardStateDto


class NflProcessor:

    @staticmethod
    def process(data, prevPlay):

        win_probabilities = {}  # TODO
        player_stats = []
        newBoardStates = []

        curPlay = data['plays'][-1]
        lastPlayIdx = len(data['plays']) - 1
        if prevPlay is not None and prevPlay['idx'] != lastPlayIdx:
            if prevPlay['desc'] == curPlay['playDescription']:
                return []
            # fix conditionals

        posTeam = curPlay['possessionTeam']['abbreviation']
        playDesc = curPlay['playDescription']
        penaltyDto = None
        touchdownDto = None

        if 'PENALTY' in playDesc:
            penaltyDto = NflBoardStateDto.createPenaltyDto(playDesc)
            if 'No play.' in playDesc:
                return [penaltyDto]
        if 'TOUCHDOWN' in playDesc:
            touchdownDto = NflBoardStateDto.createTouchdownDto()
        if curPlay['playType'] == 'KICK_OFF':
            pass  # TODO


    # def update(self, oldState):
    #     pass
    #
    # def oldStateIs(self, oldState):
    #     pass
    #
    # def getLastPLayIdx(self):
    #     return len(self._data["plays"])
    #
    # def get_most_recent_play(self):
    #     return self._data["plays"][-1]
    #
    # def getEventsInPlay(self, playIdx):
    #     try:
    #         play = self._data["plays"][playIdx]
    #         return play.split("\r\n")
    #     except:
    #         return None
    #
    # def home_name(self):
    #     return self._data["homeTeam"]["nickName"]
    #
    # def home_abbreviation(self):
    #     return self._data["homeTeam"]["abbreviation"]
    #
    # def away_name(self):
    #     return self._data["visitorTeam"]["nickName"]
    #
    # def away_abbreviation(self):
    #     return self._data["visitorTeam"]["abbreviation"]
    #
    # def status(self):
    #     return self._data['phase']
    #
    # def home_score(self):
    #     return self._data["homePointsTotal"]
    #
    # def away_score(self):
    #     return self._data["visitorPointsTotal"]
    #
    # def home_timeouts(self):
    #     return self._data["homeTimeoutsRemaining"]
    #
    # def away_timeouts(self):
    #     return self._data["visitorTimeoutsRemaining"]
    #
    # def possession(self):
    #     return 'HOME' if self._data['homeTeam']['id'] == self._data['possessionTeam']['id'] else 'AWAY'
    #
    # def quarter(self):
    #     return self._data["period"]
    #
    # def get_split_time(self):
    #     time = self._data['gameClock']
    #     mins = time[:time.find(':')].lstrip('0')
    #     secs = time[time.find(':')+1:]
    #     return [mins, secs]
    #
    # def blinking_time(self):
    #     pass
    #
    # def down_and_distance(self):
    #     dist = 'GOAL' if self._data['goalToGoal'] else self._data['distance']
    #     return self._data['down'] + ' & ' + dist
    #
    # def ball_spot(self):
    #     return self._data['yardLine']
    #
    # def upper_banner(self):
    #     pass
    #
    # def upper_top_line(self):
    #     pass
    #
    # def upper_bottom_line(self):
    #     pass
    #
    # def lower_first_player_name(self):
    #     pass
    #
    # def lower_first_player_stats(self):
    #     pass
    #
    # def lower_second_player_name(self):
    #     pass
    #
    # def lower_second_player_stats(self):
    #     pass

    @staticmethod
    def parse_play(play):
        playParts = []

