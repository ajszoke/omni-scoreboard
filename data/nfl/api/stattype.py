from enum import Enum


class StatType(Enum):
    RUSH_YARDS = 10
    PASS_YARDS = 15
    PASS_INTERCEPTED = 19  # todo confirm
    REC_YARDS = 21
    XP_GOOD = 72
    XP_NO_GOOD = 73
    XP_BLOCKED_KICKER = 74
    TACKLE = 79
    HALF_TACKLE = 82
    INTERCEPTION_CAUGHT = 85
    XP_BLOCKED_BLOCKER = 87
    TARGET = 115


