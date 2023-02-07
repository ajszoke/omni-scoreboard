import re

import debug
from data.nfl.nflboardcenterdto import NflBoardCenterDto
from util import stringhelper


class NflProcessor:

    @staticmethod
    def process(data, prevState):

        win_probabilities = {}  # TODO
        player_stats = []
        newCenterDtos = []

        headData = {}

        curPlay = data['plays'][-1]
        playIdx = len(data['plays']) - 1

        # head data
        headData['awayScore'] = data['visitorPointsTotal']
        headData['awayProb'] = None  # todo
        headData['awayTimeoutsLeft'] = data['visitorTimeoutsRemaining']
        headData['homeScore'] = data['homePointsTotal']
        headData['homeProb'] = None  # todo
        headData['homeTimeoutsLeft'] = data['homeTimeoutsRemaining']
        headData['lineOfScrimmage'] = data['yardLine']
        # period
        headData['quarter'] = data['period']
        if headData['quarter'] in [1, 2, 3, 4]:
            headData['quarter'] = stringhelper.ordinalize(headData['quarter'])
        # game clock
        gameClock = re.findall(r'(\d+):(\d+)', data['gameClock'])
        headData['minutes'] = gameClock[0][0]
        headData['seconds'] = gameClock[0][1]
        # down and distance
        down = stringhelper.ordinalize(data['down'])
        distance = data['distance']
        headData['downAndDistance'] = down + ' & ' + str(distance)
        # possession
        possessionTeam = data['possessionTeam']['abbreviation']
        if possessionTeam is None:
            headData['possessingTeam'] = 'NONE'
        elif possessionTeam == data['visitorTeam']['abbreviation']:
            headData['possessingTeam'] = 'AWAY'
        else:
            headData['possessingTeam'] = 'HOME'

        if prevState is not None and prevState['playIdx'] == playIdx:
            if prevState['desc'] == curPlay['playDescription']:
                return []
            # fix conditionals, check for stats and head data changes

        # center data
        posTeam = curPlay['possessionTeam']['abbreviation']
        playDesc = curPlay['playDescription']
        playData = curPlay['playStats']
        penaltyDtos = []
        touchdownDto = None
        fumbleDto = None
        safetyDto = None
        standardDto = None

        REVERSAL_STRING = 'the play was REVERSED.\r\n'
        reversalIdx = playDesc.find(REVERSAL_STRING)
        if reversalIdx != -1:
            playDesc = playDesc[reversalIdx + len(REVERSAL_STRING):]

        if 'PENALTY' in playDesc:
            penaltyDtos = NflBoardCenterDto.createPenaltyDtos(playDesc)
            if 'No play.' in playDesc:
                return {'headData': headData, 'newCenterDtos': penaltyDtos, 'playIdx': playIdx}
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
            standardDto = dto
        elif curPlay['playType'] == 'PASS':
            dto = NflBoardCenterDto.createPassDto(playData)
            standardDto = dto
        elif curPlay['playType'] == 'RUSH':
            dto = NflBoardCenterDto.createRushDto(playData)
            standardDto = dto
        elif curPlay['playType'] == 'XP_KICK':
            dto = NflBoardCenterDto.createXpKickDto(playData)
            standardDto = dto
        elif curPlay['playType'] == 'SACK':
            dto = NflBoardCenterDto.createSackDto(playData)
            standardDto = dto
        elif curPlay['playType'] == 'PUNT':
            dto = NflBoardCenterDto.createPuntDto(playDesc)
            standardDto = dto
        elif curPlay['playType'] == 'INTERCEPTION':
            dto = NflBoardCenterDto.createInterceptionDto(playDesc)
            standardDto = dto
        elif curPlay['playType'] == 'END_QUARTER':
            dto = NflBoardCenterDto.createEndQuarterDto(playDesc)
            standardDto = dto
        elif curPlay['playType'] == 'FIELD_GOAL':
            dto = NflBoardCenterDto.createFieldGoalDto(playDesc)
            standardDto = dto
        elif curPlay['playType'] == 'TIMEOUT':
            dto = NflBoardCenterDto.createTimeoutDto(playDesc)
            standardDto = dto
        elif curPlay['playType'] == 'END_GAME':
            pass  # todo
        elif curPlay['playType'] == 'PAT2':
            dto = NflBoardCenterDto.createPat2Dto(playData)
            standardDto = dto
        elif curPlay['playType'] == 'INTERCEPTION':
            dto = NflBoardCenterDto.createInterceptionDto(playDesc)
            standardDto = dto
        else:
            debug.error('Cannot process play:' + playData)

        if touchdownDto is not None:
            newCenterDtos.append(touchdownDto)
        if safetyDto is not None:
            newCenterDtos.append(safetyDto)
        if fumbleDto is not None:
            newCenterDtos.append(fumbleDto)
        if standardDto is not None:
            newCenterDtos.append(standardDto)
        if len(penaltyDtos) > 0:
            newCenterDtos += penaltyDtos

        return {'headData': headData, 'newCenterDtos': newCenterDtos, 'playIdx': playIdx}
