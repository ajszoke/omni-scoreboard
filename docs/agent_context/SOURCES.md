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

Local execution note:

- Browser-based GitHub access worked.
- Shell-level DNS inside the sandbox did not work: `getent hosts github.com` returned no host and `git ls-remote https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard.git` failed with `Could not resolve host: github.com`.
- Local Claude Code should retry all git commands on the user's machine; the sandbox DNS failure should not be assumed to apply locally.
