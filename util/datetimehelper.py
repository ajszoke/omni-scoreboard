import datetime
from data.config import Config


def getFormattedDate(config: Config, format: str=None):
    if format is None:
        format = '%Y-%m-%d'
    curTime = datetime.datetime.now()
    endOfDay = datetime.datetime.strptime(config.end_of_day, '%H:%M').time()
    isEndOfDayPm = 1 if endOfDay > datetime.datetime.strptime('12:00', '%H:%M').time() else -1
    dayDelta = 1 if (curTime.time() > endOfDay) and isEndOfDayPm else -1
    curTime += datetime.timedelta(days=dayDelta)
    return curTime.strftime(format)
