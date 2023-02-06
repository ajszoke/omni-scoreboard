import json

from data.nfl.api.nflapi import NflApi
from data.nfl.nflprocessor import NflProcessor


class NflManager:

    def __init__(self, config):
        # Save the parsed config
        self.config = config
        self.nflApi = NflApi(config)
        self.boardStateQueue = []
        self.gameStates = {}

        if self.config.NFL_IS_DEBUG:  # FIXME
            for gameData in self.config.NFL_DEBUG_GAMES:
                self.gameStates[gameData['gameId']] = None

                with open('data/nfl/api/archive/' + gameData['filename'], mode='r') as f:
                    for demoResponseLine in f.readlines():
                        demoResponseLine = demoResponseLine.strip()
                        if demoResponseLine[-1] == ',':
                            demoResponseLine = demoResponseLine[:-1]
                        demoResponseLine = json.loads(demoResponseLine)
                        curState = self.boardStateQueue[0] if len(self.boardStateQueue) > 0 else None
                        newStates = NflProcessor.process(demoResponseLine, self.gameStates[gameData['gameId']], curState)
                        if len(newStates) > 0:
                            self.boardStateQueue.pop()
                            self.boardStateQueue.append(newStates)
        else:
            self.activeGames = self.nflApi.getGamesForWeek()
        for game in self.activeGames:
            if game['detail']['phase'] == 'INGAME':
                gameId = game['id']
                self.gameStates[gameId] = {}
                self.gameStates[gameId]['last_play'] = None

        while True:
            for game in self.gameStates:
                newData = self.nflApi.getGameData(game)
                gameProcessor = NflProcessor.process(newData, self.gameStates[game]['last_play'])
                newBoardStates = gameProcessor