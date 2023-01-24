import re

from util import stringhelper


class NflBoardStateDto:

    def __init__(self, type=None, banner=None, topLine=None, bottomLine=None, playerOne=None, playerTwo=None):
        self.type = type
        self.banner = banner
        self.topLine = topLine
        self.bottomLine = bottomLine
        self.playerOne = playerOne
        self.playerTwo = playerTwo

    @staticmethod
    def createPenaltyDto(playDesc):
        team = re.search(r'PENALTY on (\w+)-', playDesc).group()
        player = re.search(r'PENALTY on \w+-([^\,]+),', playDesc).group()
        penaltyDesc = re.search(r'PENALTY on \w+-[^\,]+, ([^\,]+),', playDesc).group()
        yards = re.search(r'PENALTY on \w+-[^\,]+, [^\,]+, ([^\,]+)', playDesc).group()

        team = '' if stringhelper.isStringEmpty(team) else team + ' '
        name = '' if stringhelper.isStringEmpty(player) else player + ', '
        penaltyDesc = '' if stringhelper.isStringEmpty(penaltyDesc) else penaltyDesc + ' '
        yards = '' if stringhelper.isStringEmpty(yards) else '({} yards)'.format(yards)

        topLine = '{}PENALTY'.format(team)
        bottomLine = name + penaltyDesc + yards

        return NflBoardStateDto(type='PENALTY', topLine=topLine, bottomLine=bottomLine.strip())

    @staticmethod
    def createTouchdownDto():
        return NflBoardStateDto(type='TOUCHDOWN')

