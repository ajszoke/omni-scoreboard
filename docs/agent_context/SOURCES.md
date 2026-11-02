# Research basis

This context pack was prepared on 2026-06-14.

Primary repositories and references:

- Current legacy repo: https://github.com/ajszoke/omni-scoreboard
- Upstream source repo: https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard
- Upstream compare against legacy: https://github.com/ajszoke/omni-scoreboard/compare/master...MLB-LED-Scoreboard:mlb-led-scoreboard:master
- Upstream releases: https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard/releases
- Upstream plugin API docs: https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard/blob/master/bullpen/README.md
- Upstream config example: https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard/blob/master/config.example.json
- GitHub CLI fork docs: https://cli.github.com/manual/gh_repo_fork
- GitHub CLI rename docs: https://cli.github.com/manual/gh_repo_rename
- GitHub double-fork caveat: https://github.com/cli/cli/issues/6329
- ESPN NFL scoreboard JSON endpoint: https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
- ESPN NBA scoreboard JSON endpoint: https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard
- ESPN PGA scoreboard JSON endpoint: https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard
- DataGolf API: https://datagolf.com/api-access
- SportsDataIO Golf API: https://sportsdata.io/pga-golf-api
- Tidbyt product reference: https://tidbyt.com/
- Tidbyt/Pixlet tooling: https://github.com/tidbyt/pixlet
- Gidger Raspberry Pi sports scoreboard reference: https://github.com/gidger/rpi-led-nhl-scoreboard
- Ty Porter LED scoreboard hardware/dev-env writeup: https://blog.ty-porter.dev/development/raspberry%20pi/emulation/2021/11/11/building-raspberry-pi-led-scoreboards.html

Odds and win probability (measured 2026-11-01):

- Bovada coupon, no key, undocumented: `https://www.bovada.lv/services/sports/event/coupon/events/A/description/baseball/mlb`. Served with `Cache-Control: max-age=600`; the copy refreshes only about every 10 minutes (two cycles observed), and a `no-cache` request is ignored. No 429s at up to 1 request/second against that cached copy. Fine for pregame, too stale for live.
- Polymarket US market data, no key: `https://gateway.polymarket.us` (`/v1/market/slug/{slug}`, `/v1/markets/{slug}/bbo`). Docs: https://docs.polymarket.us/ . 25 requests/second per IP (https://docs.polymarket.us/api-reference/rate-limits.md). Public responses are cached for 30 seconds; real-time needs the keyed WebSocket (https://docs.polymarket.us/api-reference/websocket/markets.md). Moneyline slugs are `aec-mlb-{away}-{home}-{local date}`: all 15 games matched on 2026-09-16 and 2026-06-10, but doubleheader games matched none of the variants tried.
- Polymarket US participant agreement (binds account holders, so anyone with a key), clause 22: data "may not be redistributed except as PMUS expressly permits": https://www.polymarketexchange.com/files/legal/latest/participant-agreement
- Kalshi Developer Agreement v1.1, section 3: API use "is expressly limited to facilitating a members own trading"; section 1: accessing the API counts as accepting it. Ruled out: https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf
- polymarket.com terms, sections 1 and 4.2: US residents are limited to content features, and scraping is barred. Ruled out from the US: https://polymarket.com/tos

Local execution note:

- Browser-based GitHub access worked.
- Shell-level DNS inside the sandbox did not work: `getent hosts github.com` returned no host and `git ls-remote https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard.git` failed with `Could not resolve host: github.com`.
- Local Claude Code should retry all git commands on the user's machine; the sandbox DNS failure should not be assumed to apply locally.
