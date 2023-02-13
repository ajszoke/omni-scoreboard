import copy
import re
import debug
from data.nfl.api.stattype import StatType
from data.nfl.api.bannertype import BannerType

from util import stringhelper


class NflBoardCenterDto:

    def __init__(self, type=None, isRequired=True, banner=None, topLine=None, bottomLine=None, playerOne=None, playerTwo=None):
        self.type = type
        self.isRequired = isRequired
        self.banner = banner
        self.topLine = topLine
        self.bottomLine = bottomLine
        self.playerOne = playerOne  # todo
        self.playerTwo = playerTwo  # todo

    @staticmethod
    def createPenaltyDtos(playDesc):
        if 'offsetting' in playDesc:
            return NflBoardCenterDto(type=BannerType.ALERT, topLine='PENALTIES', bottomLine='Offset (No Play)')

        startIdx = re.search(r'(Penalty|PENALTY)', playDesc)
        searchString = copy.copy(playDesc)
        penalties = []
        penaltyIdxes = []

        # chunk the play description into individual enforced penalties
        while startIdx is not None:
            declineIdx = re.search(r', declined', searchString)
            enforcedIdx = re.search(r', enforced', searchString)
            declineIdx = declineIdx.start() if declineIdx is not None else None
            enforcedIdx = enforcedIdx.start() if enforcedIdx is not None else None
            if declineIdx is None:
                if enforcedIdx is None:
                    debug.error("Parsing error on penalty (couldn't find end idx): " + searchString)
                    return None
                penaltyIdxes.append([startIdx, enforcedIdx])
                searchString = searchString[enforcedIdx + len('enforced ') + 1:]
            elif enforcedIdx is None:
                searchString = searchString[declineIdx + len('declined ') + 1:]
            elif enforcedIdx < declineIdx:
                penaltyIdxes.append([startIdx, enforcedIdx])
                searchString = searchString[enforcedIdx + len('enforced ') + 1:]
            else:
                searchString = searchString[declineIdx + len('declined ') + 1:]
            startIdx = re.search(r'(Penalty|PENALTY)', searchString)

        for [startIdx, endIdx] in penaltyIdxes:
            searchString = playDesc[startIdx:endIdx]

            if re.search(r'PENALTY ON (\w+)-', searchString).group() != '':
                # player penalty
                team = re.findall('PENALTY on (\w+)-', playDesc)
                player = re.findall('PENALTY on \w+-([^,]+),', playDesc)
                penaltyDesc = re.findall('PENALTY on \w+-[^,]+, ([^,]+),', playDesc)
                yards = re.findall('PENALTY on \w+-[^,]+, [^,]+, ([^,]+)', playDesc)

                team = '' if len(team) == 0 else team[0] + ' '
                player = '' if len(player) == 0 else player[0] + ', '
                penaltyDesc = '' if len(penaltyDesc) == 0 else penaltyDesc[0] + ' '
                yards = '' if len(yards) == 0 else '({})'.format(yards[0])

                topLine = '{}PENALTY'.format(team)
                bottomLine = player + penaltyDesc + yards
                penalties.append(NflBoardCenterDto(type=BannerType.ALERT, topLine=topLine,
                                                   bottomLine=bottomLine.strip()))

            elif re.search(r'PENALTY ON (\w+), ', searchString).group != '':
                # team penalty
                team = re.findall(r'PENALTY on (\w+),', playDesc)
                penaltyDesc = re.findall(r'PENALTY on \w+, ([^,]+),', playDesc)
                yards = re.findall(r'PENALTY on \w+, [^,]+, ([^,]+),', playDesc)

                team = '' if len(team) == 0 else team[0] + ' '
                penaltyDesc = '' if len(penaltyDesc) == 0 else penaltyDesc[0] + ' '
                yards = '' if len(yards) == 0 else '({})'.format(yards[0])

                topLine = '{}PENALTY'.format(team)
                bottomLine = penaltyDesc + yards
                penalties.append(NflBoardCenterDto(type=BannerType.ALERT, topLine=topLine,
                                                   bottomLine=bottomLine.strip()))

            else:
                debug.error("Parsing error on penalty (couldn't determine type): " + searchString)
                continue

            return penalties

    @staticmethod
    def createFumbleDto(playDesc, posTeam, playType):  # TODO
        recoverTeamArr = re.findall(r'RECOVERED by (\w+)-', playDesc)
        if len(recoverTeamArr) == 0 or recoverTeamArr[0] != posTeam:
            return None

        fumbler = ''
        fumblerArr = []
        if playType == 'KICK_OFF':
            fumblerArr = re.findall(r'from \w+ \d+ to \w+ \d+\. (.*) to', playDesc)
            # todo
        elif playType == 'PUNT':
            pass
        if len(fumblerArr) > 0:
            fumbler = fumblerArr[0]
        pass

    @staticmethod
    def createTouchdownDto():
        return NflBoardCenterDto(type=BannerType.TOUCHDOWN)

    @staticmethod
    def createSafetyDto():
        return NflBoardCenterDto(type=BannerType.SAFETY)

    @staticmethod
    def createKickoffDto(playDesc, kickTeam):
        topLine = kickTeam + ' Kick-off'
        bottomLine = ''

        if 'kicks onside' in playDesc:
            if 'RECOVERED ' in playDesc:
                return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine='Onside, {} RECOVERED'
                                         .format(kickTeam))
            parts = re.findall(r'from \w+ \d+ to \w+ \d+\. ([^ ]*).* to (\w+ \d+)', playDesc)  # player recovery
            if len(parts) == 2:
                bottomLine = '{} to {}'.format(parts[0], parts[1])
            else:
                parts = re.findall(r'.* to (\w+ \d+).*', playDesc)  # team recovery
                if len(parts) == 1:
                    bottomLine = parts[0]
        if 'Touchback.' in playDesc:
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine="Touchback")

        recoveryStr = re.findall(r'RECOVERED by (\w+)-', playDesc)
        if len(recoveryStr) == 1:
            bottomLine = recoveryStr[0] + ' RECOVERS'

        parts = re.findall(r'yards from \w+ \d+ to \w+ -?\d+\. ([a-zA-z.]+) to (\w+ \d+)', playDesc)
        if len(parts) == 2:
            bottomLine = "{}, to {}".format(parts[0], parts[1])
        elif len(parts) == 1 and any(char.isdigit() for char in parts[0]):
            bottomLine = 'to ' + parts[0]

        return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine=bottomLine.strip())

    @staticmethod
    def createFieldGoalDto(playDesc):
        # good
        parts = re.findall(r'\(\d*:\d{2}\) ([^( \d)]*) (\d+ yard) field goal is GOOD', playDesc)
        if len(parts) == 2:
            return NflBoardCenterDto(type=BannerType.OTHER_SCORE, topLine='FIELD GOAL GOOD',
                                     bottomLine='{} ({}s)'.format(parts[0], parts[1]))
        # no good
        parts = re.findall(r'\(\d*:\d{2}\) ([^( \d)]*) (\d+ yard) field goal is No Good', playDesc)
        if len(parts) == 2:
            return NflBoardCenterDto(type=BannerType.TURNOVER, topLine='FIELD GOAL MISSED',
                                     bottomLine='{} ({}s)'.format(parts[0], parts[1]))
        # blocked
        parts = re.findall(r'field goal is BLOCKED .* recovered by (\w+)-.*', playDesc)
        if len(parts) == 1:
            return NflBoardCenterDto(type=BannerType.TURNOVER, topLine='FIELD GOAL BLOCKED',
                                     bottomLine='Recovered by ' + parts[0])
        # unknown
        return NflBoardCenterDto(type=BannerType.EMPTY)

    @staticmethod
    def createInterceptionDto(playDesc):
        # standard
        parts = re.findall(r'\(\d*:\d{2}\) \([^)]*\) (.*) pass .* INTERCEPTED by (.*) at .* to (\w+ \d+)', playDesc)
        if len(parts) == 3:
            return NflBoardCenterDto(type=BannerType.TURNOVER, topLine='INTERCEPTION',
                                     bottomLine='{}, by {} to {}'.format(parts[0], parts[1], parts[2]))
        # pick six?
        parts = re.findall(r'\(\d*:\d{2}\) \([^)]*\) (.*) pass .* INTERCEPTED by (.*) at ', playDesc)
        if len(parts) == 2:
            return NflBoardCenterDto(type=BannerType.TURNOVER, topLine='PICK SIX',
                                     bottomLine='{}, by {}'.format(parts[0], parts[1]))
        # unknown
        parts = re.findall(r'\(\d*:\d{2}\) \([^)]*\) (.*) pass .* INTERCEPTED', playDesc)
        bottomLine = parts[0] if len(parts) == 1 else ''
        return NflBoardCenterDto(type=BannerType.TURNOVER, topLine='INTERCEPTION', bottomLine=bottomLine)

    @staticmethod
    def createEndQuarterDto(playDesc):
        quarterNum = playDesc.strip()[-1]
        return NflBoardCenterDto(type=BannerType.STANDARD, topLine='End', bottomLine='Quarter ' + quarterNum)

    @staticmethod
    def createPuntDto(playDesc):
        # standard
        parts = re.findall(r'\(\d*:\d{2}\) (.*) punts -?\d+ yards .*(to|at) (\w+ \d+).*', playDesc)
        if len(parts) == 3:
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine='Punt',
                                     bottomLine='{}, to {}'.format(parts[0], parts[2]))
        #touchback
        parts = re.findall(r'\(\d*:\d{2}\) (.*) punts -?\d+ yards .*Touchback\.', playDesc)
        if len(parts) == 1:
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine='Punt',
                                     bottomLine='{}, Touchback'.format(parts[0]))
        #safety
        if 'SAFETY' in playDesc:
            return NflBoardCenterDto(type=BannerType.TURNOVER, topLine='SAFETY',
                                     bottomLine='Punt touched out of end zone')
        # block
        parts = re.findall(r'\(\d*:\d{2}\) (.*) punt is BLOCKED .*(recovered|RECOVERED) by (\w+)-', playDesc)
        if len(parts) == 3:
            return NflBoardCenterDto(type=BannerType.TURNOVER, topLine='Punt BLOCKED',
                                     bottomLine='Recovered by ' + parts[2])
        # unknown
        return NflBoardCenterDto(type=BannerType.STANDARD, topLine='Punt', bottomLine='')

    @staticmethod
    def createRushDto(playData):
        for stat in playData['playStats']:
            if stat['statId'] == StatType.RUSH_YARDS.value:
                topLine = 'Kneel-down' if 'kneels' in playData['playDescription'] else str(stat['yards']) + ' yard rush'
                bottomLine = stat['playerName']
                return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine=bottomLine)
        return NflBoardCenterDto(type=BannerType.EMPTY)

    @staticmethod
    def createPassDto(playData):
        qbName = None
        recName = None
        yards = None
        for stat in playData:
            if stat['statId'] == StatType.PASS_YARDS.value:
                yards = stat['yards']
                qbName = stat['playerName']
            elif stat['statId'] == StatType.REC_YARDS.value:
                recName = stat['playerName']

        topLine = str(yards) + ' yard pass' if yards is not None else 'Pass'
        qbName = '' if qbName is None else qbName
        recName = '' if recName is None else ' to ' + recName
        return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine=qbName + recName)

    @staticmethod
    def createXpKickDto(playData):
        playDesc = playData['playDescription']
        topLine = ''
        bottomLine = ''
        type = BannerType.STANDARD
        if 'extra point is GOOD' in playDesc:
            topLine = 'PAT Good'
            for stat in playData['playStats']:
                if stat['statId'] == StatType.XP_GOOD.value:
                    bottomLine = stat['playerName']
                    break
        elif 'extra point is No Good' in playDesc:
            topLine = 'PAT NO GOOD'
            type = BannerType.TURNOVER
            for stat in playData['playStats']:
                if stat['statId'] == StatType.XP_NO_GOOD.value:
                    bottomLine = stat['playerName']
                    break
        elif 'extra point is Blocked' in playDesc:
            topLine = 'PAT BLOCKED'
            type = BannerType.TURNOVER
            kicker = ''
            blocker = ''
            for stat in playData['playStats']:
                if stat['statId'] == StatType.XP_BLOCKED_KICKER.value:
                    kicker = stat['playerName']
                elif stat['statId'] == StatType.XP_BLOCKED_BLOCKER.value:
                    blocker = stat['playerName']
            bottomLine = '{} (by {})'.format(kicker, blocker)
        else:
            return NflBoardCenterDto(type=BannerType.EMPTY)
        return NflBoardCenterDto(type=type, topLine=topLine, bottomLine=bottomLine)

    @staticmethod
    def createPat2Dto(playData):
        playDesc = playData['playDescription']
        topLine = ''
        bottomLine = ''
        if 'ATTEMPT SUCCEEDS' in playDesc:
            topLine = '2-pt GOOD'
        elif 'ATTEMPT FAILS' in playDesc:
            topLine = '2-pt NO GOOD'
        else:
            return NflBoardCenterDto(type=BannerType.EMPTY)

        parts = re.findall(r'.*(\w+\..*) rushes', playDesc)
        if len(parts) == 1:
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine=parts[0] + ' rush')
        parts = re.findall(r'.*(\w+\..*) pass to (\w+\..*) is', playDesc)
        if len(parts) == 2:
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine=parts[0] + ' to ' + parts[1])
        parts = re.findall(r'.*(\w+\..*) is sacked', playDesc)
        if len(parts) == 1:
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine=parts[0] + ' sacked')
        return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine='')

    @staticmethod
    def createSackDto(playData):
        playDesc = playData['playDescription']
        topLine = ''
        parts = re.findall(r'.*(\w+\..*) sacked.*for -?(\d+) yards.*\(([^\)]*)\)', playDesc)
        if len(parts) == 3:
            topLine = parts[1] + ' yard sack'
            if 'sack split' in parts[2]:
                parts[2] = parts[2][len('sack split by '):].replace(' and ', ', ')
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine=topLine, bottomLine='{} ({})'
                                     .format(parts[1], parts[2]))
        return NflBoardCenterDto(type=BannerType.EMPTY)

    @staticmethod
    def createTimeoutDto(playDesc):
        if playDesc.strip() == 'Two-Minute Warning':
            return NflBoardCenterDto(type=BannerType.STANDARD, topLine='Two-Minute', bottomLine='Warning')
        bottomLine = ''
        parts = re.findall(r'Timeout #(\d) by (\w+) at', playDesc)
        if len(parts) == 2:
            timeoutNum = stringhelper.ordinalize(parts[0])
            bottomLine = '{} ({})'.format(parts[1], timeoutNum)
        return NflBoardCenterDto(type=BannerType.STANDARD, topLine='Timeout', bottomLine=bottomLine)

    @staticmethod
    def createEmptyDto():
        return NflBoardCenterDto(type=BannerType.EMPTY, isRequired=False)

    def __json__(self):
       return self.__dict__
