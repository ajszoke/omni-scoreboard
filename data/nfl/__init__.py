import json

from PIL import Image

from data.nfl.api.nflapi import NflApi
from data.nfl.nflprocessor import NflProcessor
from datetime import datetime, timedelta


class NflManager:
    DEFAULT_STATE_EXPIRE_SEC = 5

    def __init__(self, config):
        # Save the parsed config
        self.config = config
        self.nflApi = NflApi(config)
        # self.gameStates = []  # gameID, [dto, expire]  todo
        self.boardStateQueue = []
        self.activeState = None
        self.gamePhase = None
        self.awayTeamImg = Image.open('assets/img/team-icons/nfl/20/KC.png')
        self.homeTeamImg = Image.open('assets/img/team-icons/nfl/20/PHI.png')
        self.specialBannerImg = Image.open('assets/img/sb-lvii-banner.png')
        self.debugInputLine = 0
        if self.config.NFL_IS_DEBUG:
            gameData = self.config.NFL_DEBUG_GAME
            with open('data/nfl/api/archive/' + gameData['filename'], mode='r') as f:
                self.lines = f.readlines()

    def update(self):
        if self.config.NFL_IS_DEBUG:  # FIXME
            demoResponseLine = self.lines[self.debugInputLine]
            demoResponseLine = demoResponseLine.strip()
            if demoResponseLine[-1] == ',':
                demoResponseLine = demoResponseLine[:-1]
            demoResponseLine = json.loads(demoResponseLine)
            self.gamePhase = demoResponseLine['phase']
            newStates = NflProcessor.process(demoResponseLine, self.activeState)
            if newStates['newCenterDtos'] is not None:
                showTime = datetime.now() + timedelta(
                    seconds=self.DEFAULT_STATE_EXPIRE_SEC + self.config.nfl_display_delay)
                for state in newStates['newCenterDtos']:
                    self.boardStateQueue.append({'headData': newStates['headData'], 'state': state, 'showTime': showTime})
                    showTime += timedelta(seconds=self.DEFAULT_STATE_EXPIRE_SEC)
            self.debugInputLine += 1

        else:
            self.activeGames = self.nflApi.getGamesForWeek()
            for game in self.activeGames:
                if game['detail']['phase'] == 'INGAME':
                    gameId = game['id']
                    # self.gameStates[gameId] = {}
                    # self.gameStates[gameId]['last_play'] = None

            # while True:
            #     for game in self.gameStates:
            #         newData = self.nflApi.getGameData(game)
            #         gameProcessor = NflProcessor.process(newData, self.gameStates[game]['last_play'])
