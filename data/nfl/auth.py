import requests

def get_bearer_token():
    resp = requests.post(url="https://api.nfl.com/identity/v3/token",
                         headers={
                             "sec-fetch-dest": "empty",
                             "sec-fetch-mode": "cors",
                             "sec-fetch-site": "same-site",
                             "sec-gpc": "1",
                             "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36",
                             "Host": "api.nfl.com",
                             "path": "/identity/v3/token",
                             "origin": "https://www.nfl.com",
                             "referer": "https://www.nfl.com/"
                         },
                         data={
                             "clientKey": "4cFUW6DmwJpzT9L7LrG3qRAcABG5s04g",
                             "clientSecret": "CZuvCL49d9OwfGsR",
                             "deviceId": "d42972d1-2969-434e-bd7f-0e7d1b4f2a7a",
                             "deviceInfo": "eyJtb2RlbCI6ImRlc2t0b3AiLCJ2ZXJzaW9uIjoiQ2hyb21lIiwib3NOYW1lIjoiV2luZG93cyIsIm9zVmVyc2lvbiI6IjEwIn0=",
                             "networkType": "other",
                             "useRefreshToken": "true"
                         })
    print(resp)