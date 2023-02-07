import json
import time

from data.nfl.api.nflapi import NflApi
from data.nfl.nflprocessor import NflProcessor
from datetime import datetime, timedelta


class NflManager:

    def __init__(self, config):
        DEFAULT_STATE_EXPIRE_SEC = 5

        # Save the parsed config
        self.config = config
        self.nflApi = NflApi(config)
        # self.gameStates = []  # gameID, [dto, expire]  todo
        self.boardStateQueue = []

        if self.config.NFL_IS_DEBUG:  # FIXME
            gameData = self.config.NFL_DEBUG_GAME

            with open('data/nfl/api/archive/' + gameData['filename'], mode='r') as f:
                for demoResponseLine in f.readlines():
                    demoResponseLine = demoResponseLine.strip()
                    if demoResponseLine[-1] == ',':
                        demoResponseLine = demoResponseLine[:-1]
                    demoResponseLine = json.loads(demoResponseLine)
                    curState = self.boardStateQueue[0] if len(self.boardStateQueue) > 0 else None
                    newStates = NflProcessor.process(demoResponseLine, curState)
                    if newStates is not None:
                        expTime = datetime.now() + timedelta(
                            seconds=DEFAULT_STATE_EXPIRE_SEC + config.nfl_display_delay)
                        for state in newStates['newCenterDtos']:
                            self.boardStateQueue.append({'headData': newStates['headData'], 'state': state, 'expTime': expTime})
                            expTime += timedelta(seconds=DEFAULT_STATE_EXPIRE_SEC)
                    if len(self.boardStateQueue) > 1 and self.boardStateQueue[0]['expTime'] < datetime.now():
                        self.boardStateQueue.pop()
                    time.sleep(12)

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
