from enum import Enum


class BannerType(Enum):
    STANDARD = 'STANDARD'
    TOUCHDOWN = 'TOUCHDOWN'
    OTHER_SCORE = 'OTHER_SCORE'
    ALERT = 'ALERT'
    TURNOVER = 'TURNOVER'
    SAFETY = 'SAFETY'
    EMPTY = 'EMPTY'
