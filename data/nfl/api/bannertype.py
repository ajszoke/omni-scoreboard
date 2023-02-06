from enum import Enum


class BannerType(Enum):
    STANDARD = 0
    TOUCHDOWN = 1
    OTHER_SCORE = 2
    ALERT = 3
    TURNOVER = 4
    SAFETY = 5
    EMPTY = 6
