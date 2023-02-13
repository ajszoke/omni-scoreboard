import json

from PIL import Image

import debug
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
        self.boardStateQueueMutex = False
        self.boardStateQueue = []
        self.activeState = None
        # self.prevState = None
        self.gamePhase = None
        self.awayTeamImg = Image.open('assets/img/team-icons/nfl/20/KC.png')
        self.homeTeamImg = Image.open('assets/img/team-icons/nfl/20/PHI.png')
        self.specialBannerImg = Image.open('assets/img/sb-lvii-banner.png')
        self.sblviiGameId = '70e5ba20-9aaa-11ed-99e5-6b206f74937e'
        self.sblviiPregameImg = Image.open('assets/img/sblvii-pregame.png')
        self.debugInputLine = 18
        # self.config.NFL_IS_DEBUG = False  # TODO remove
        if self.config.NFL_IS_DEBUG:
            gameData = self.config.NFL_DEBUG_GAME
            with open('data/nfl/api/archive/' + gameData['filename'], mode='r') as f:
                self.lines = f.readlines()

    def update(self):
        if self.config.NFL_IS_DEBUG:
            apiData = self.lines[self.debugInputLine]
            apiData = apiData.strip()
            if apiData[-1] == ',':
                apiData = apiData[:-1]
            apiData = json.loads(apiData)
            debug.info('Debug: row ' + str(self.debugInputLine))
            self.debugInputLine += 1
        else:
            apiData = self.nflApi.getGameData(self.sblviiGameId)

        self.gamePhase = apiData['phase']
        newStates = NflProcessor.process(apiData, self.activeState)
        if newStates == 'END_GAME':
            pass  # TODO
        if newStates is not None and newStates['newCenterDtos'] is not None:
            self.boardStateQueueMutex = True
            showTime = datetime.now() + timedelta(seconds=self.config.nfl_display_delay)
            truncateIdx = 0
            for i in range(len(self.boardStateQueue)):
                if self.boardStateQueue[i]['showTime'] > showTime:
                    truncateIdx = i
                    break
            self.boardStateQueue = self.boardStateQueue[:truncateIdx]
            for state in newStates['newCenterDtos']:
                self.boardStateQueue.append({'headData': newStates['headData'], 'state': state, 'showTime': showTime})
                showTime += timedelta(seconds=self.DEFAULT_STATE_EXPIRE_SEC)
            self.boardStateQueueMutex = False
