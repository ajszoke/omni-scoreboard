import re

from data.nfl.nflboardcenterdto import NflBoardCenterDto
from util import stringhelper


class NflProcessor:

    @staticmethod
    def process(data, prevPlay):

        win_probabilities = {}  # TODO
        player_stats = []
        newCenterDtos = []
        newBoardStates = []

        headData = {}

        curPlay = data['plays'][-1]
        lastPlayIdx = len(data['plays']) - 1
        if prevPlay is not None and prevPlay['idx'] != lastPlayIdx:
            if prevPlay['desc'] == curPlay['playDescription']:
                return []
            # fix conditionals, check for stats and head data changes

        # head data
        headData['awayScore'] = data['visitorPointsTotal']
        headData['awayProb'] = None  # todo
        headData['awayTimeoutsLeft'] = data['visitorTimeoutsRemaining']
        headData['homeScore'] = data['homePointsTotal']
        headData['homeProb'] = None  # todo
        headData['homeTimeoutsLeft'] = data['homeTimeoutsRemaining']
        headData['quarter'] = data['period']  # todo overtime, ordinalize
        headData['lineOfScrimmage'] = data['yardLine']
        # game clock
        gameClock = re.findall(r'(\d+):(\d+)', data['gameClock'])
        headData['minutes'] = gameClock[0]
        headData['seconds'] = gameClock[1]
        # down and distance
        down = stringhelper.ordinalize(data['down'])
        distance = data['distance']
        headData['downAndDistance'] = down + ' & ' + distance
        # possession
        possessionTeam = data['possessionTeam']['abbreviation']
        if possessionTeam is None:
            headData['possessingTeam'] = 'NONE'
        elif possessionTeam == data['visitorTeam']['abbreviation']:
            headData['possessingTeam'] = 'AWAY'
        else:
            headData['possessingTeam'] = 'HOME'
        # center data
        posTeam = curPlay['possessionTeam']['abbreviation']
        playDesc = curPlay['playDescription']
        playStats = curPlay['playStats']
        penaltyDtos = []
        touchdownDto = None
        fumbleDto = None
        safetyDto = None

        REVERSAL_STRING = 'the play was REVERSED.\r\n'
        reversalIdx = playDesc.find(REVERSAL_STRING)
        if reversalIdx != -1:
            playDesc = playDesc[reversalIdx + len(REVERSAL_STRING):]

        if 'PENALTY' in playDesc:
            penaltyDtos = NflBoardCenterDto.createPenaltyDtos(playDesc)
            if 'No play.' in playDesc:
                return penaltyDtos
        if 'TOUCHDOWN' in playDesc:
            touchdownDto = NflBoardCenterDto.createTouchdownDto()
        if 'FUMBLES' in playDesc or 'MUFFS' in playDesc:
            isKick = curPlay['playType'] in ['KICK_OFF', 'PUNT', 'FREE_KICK']
            fumbleDto = NflBoardCenterDto.createFumbleDto(playDesc, posTeam, isKick)  # fixme
        if 'SAFETY' in playDesc:
            safetyDto = NflBoardCenterDto.createSafetyDto()
        if '*** play under review ***' in playDesc:
            pass  # todo

        if curPlay['playType'] == 'KICK_OFF':
            dto = NflBoardCenterDto.createKickoffDto(playDesc, posTeam)
            newCenterDtos.append(dto)
        elif curPlay['playType'] == 'RUSH':
            dto = NflBoardCenterDto.createRushDto(playStats)
            newCenterDtos.append(dto)
