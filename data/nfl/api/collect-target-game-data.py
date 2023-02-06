import json
import time

import uuid
# import debug
import requests
from datetime import date
from datetime import datetime


class NflApi:

    def __init__(self):
        self.token = self.getToken()
        self.weekData = self.getWeekData()
        # self.api = api

    def getToken(self):
        url = 'https://api.nfl.com/identity/v3/token'
        data = {
            "clientKey": "4cFUW6DmwJpzT9L7LrG3qRAcABG5s04g",
            "clientSecret": "CZuvCL49d9OwfGsR",
            "deviceId": uuid.uuid4(),
            "deviceInfo": '{"model":"desktop","version":"Chrome","osName":"Windows","osVersion":"10"}'.encode('ascii'),
            "networkType": "other",
            "useRefreshToken": 'true'
        }
        api_res = self.doCall('POST', url, data)
        return 'Bearer ' + api_res.json()['accessToken']

    def getWeekData(self):
        url = 'https://api.nfl.com/football/v2/weeks/date/' + str(date.today())
        headers = {'Authorization': self.token}
        api_res = self.doCall('GET', url, headers=headers).json()
        return api_res

    def getGamesForWeek(self):
        url = "https://api.nfl.com/experience/v1/games?season={season}&seasonType={seasonType}&week={week}"\
            .format(season=self.weekData["season"], seasonType=self.weekData["seasonType"], week=self.weekData["week"])
        headers = {'Authorization': self.token}
        api_res = self.doCall("GET", url, headers=headers).json()
        return api_res["games"]

    def getGameData(self, gameId):
        url = "https://api.nfl.com/experience/v1/gamedetails/" + gameId
        headers = {'Authorization': self.token}
        api_res = self.doCall("GET", url, headers=headers).json()
        return api_res["data"]["viewer"]["gameDetail"]

    def getEspnGameData(self, gameId):
        url = "http://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{}/competitions/{}/plays".format(gameId, gameId)
        api_res = self.doCall("GET", url).json()
        return api_res

    def doCall(self, verb: str, url: str, data=None, headers=None, isRetry=False):
        verb = verb.upper()
        assert verb in ['GET', 'POST']
        api_res = None

        try:
            if verb == 'GET':
                api_res = requests.get(url=url, headers=headers)
            elif verb == 'POST':
                api_res = requests.post(url=url, data=data, headers=headers)

            if api_res.status_code == 401:
                if isRetry:
                    # debug.error('API FAILURE: Failed twice' +
                    #             '\n\tVerb: ' + verb +
                    #             '\n\tURL: ' + url +
                    #             '\n\tData: ' + data +
                    #             '\n\tHeaders: ' + headers +
                    #             '\n\tResponse: ' + str(api_res.status_code) + str(api_res.content))
                    return None
                if 'Authorization' in headers:
                    headers['Authorization'] = self.getToken()  # refresh
                api_res = self.doCall(verb=verb, url=url, data=data, headers=headers, isRetry=True)
        except Exception as e:
            # debug.error(e)
            pass

        return api_res

nflApi = NflApi()

gameId = '401492629'
filename = 'archive/230205_NFC_VS_AFC.json'
gameState = None

with open(filename, mode='w') as f:
    f.write("")


while True:
    with open(filename, mode='a') as f:
        try:
            start = time.time()
            data = nflApi.getEspnGameData(gameId)
            end = time.time()
            diff = int((end - start)*100)
            print("({MS} ms)".format(MS=diff))
            json.dump(data, f)
            f.write('\n')
        except Exception as e:
            print("{TS} {E}".format(TS=datetime.now().strftime("%H:%M:%S"), E=e))
        time.sleep(15)
