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
- ESPN scoreboard JSON endpoints (reference only since 2026-11-15: ESPN's terms bar automated access):
  - NFL: https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
  - NBA: https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard
  - PGA: https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard
- DataGolf API: https://datagolf.com/api-access
- SportsDataIO Golf API: https://sportsdata.io/pga-golf-api
- Tidbyt product reference: https://tidbyt.com/
- Tidbyt/Pixlet tooling: https://github.com/tidbyt/pixlet
- Gidger Raspberry Pi sports scoreboard reference: https://github.com/gidger/rpi-led-nhl-scoreboard
- Ty Porter LED scoreboard hardware/dev-env writeup: https://blog.ty-porter.dev/development/raspberry%20pi/emulation/2021/11/11/building-raspberry-pi-led-scoreboards.html

Odds and win probability (measured 2026-11-01):

- Bovada coupon, no key, undocumented: `https://www.bovada.lv/services/sports/event/coupon/events/A/description/baseball/mlb`. Served with `Cache-Control: max-age=600`; the copy refreshes only about every 10 minutes (two cycles observed), and a `no-cache` request is ignored. No 429s at up to 1 request/second against that cached copy. Fine for pregame, too stale for live.
- Polymarket US market data, no key: `https://gateway.polymarket.us` (`/v1/market/slug/{slug}`, `/v1/markets/{slug}/bbo`). Docs: https://docs.polymarket.us/ . 25 requests/second per IP (https://docs.polymarket.us/api-reference/rate-limits.md). Public responses are cached for 30 seconds; real-time needs the keyed WebSocket (https://docs.polymarket.us/api-reference/websocket/markets.md). Moneyline slugs are `aec-mlb-{away}-{home}-{local date}`: all 15 games matched on 2026-09-16 and 2026-06-10, but doubleheader games matched none of the variants tried.
- Polymarket US market stream, API key required: `wss://api.polymarket.us/v1/ws/markets` (https://docs.polymarket.us/api-reference/websocket/overview.md). Its light feed carries prices but no market state, so REST stays the source of truth for whether a market is open. Refuses an unsigned client with HTTP 401. Measured with a real key over 90 seconds on three live games: first quote 0.1s after subscribing, about 20 messages/second in total, bid/ask changes up to several per second on a busy market and tens of seconds apart on a quiet one, and no heartbeat messages despite the docs.
- Polymarket US league listing, no key: `/v2/leagues/{league}/events`, 10 events by default, with `?limit=` and `?offset=` working (measured 2026-11-02). Each event gives `live`, `ended`, `closed`, `startTime` and its markets, each with `slug`, `gameStartTime` and `marketSides`.
- NFL team colors and logo files: nflverse's team table (CC BY 4.0), https://github.com/nflverse/nflverse-data/releases/download/teams/teams_colors_logos.csv . Its Wikipedia logo links point at thumbnails; the tiles use the original SVGs, located through Wikipedia's API by file name because files get renamed (Buffalo's was, and the Rams changed logos in 2020). Wikimedia's image host throttles quick bursts (a 429 after four files), so the fetch tool paces itself. The logos are team trademarks.
- NFLMeta (https://nflmeta.org), checked 2026-11-15. Builder is $12/month: 120 requests/minute, 75,000 requests and 250,000 rows a month; Free is 20/minute, 5,000 and 25,000, for "personal, non-commercial learning, research, and private evaluation". Paid terms allow "displaying individual scores and statistics in your own application". Any plan may do "normal application caching", but no standing copy of the database and no systematic accumulation, scripted paging included. Termination doesn't entitle anyone to keep data obtained before it; a breach of the restrictions requires deleting any accumulated copy; cancelling a paid plan ends commercial use (terms re-read 2026-11-15 for review round 4). So raw recordings (`--record`) are opt-in, kept to what a diagnosis needs, never committed or shared, and deleted after. `GET /api/v1/live-scores` returns every live game with the situation (possession, down, distance, `yardline_100`), billing a row per game; a per-game cursor feed (`/api/v1/games/{id}/play-changes`) carries new and corrected plays, whose rows follow nflverse's play-by-play columns. Recommended poll is 10 seconds, with a target of 15-20 seconds behind its source. Its live play IDs look like ESPN's (each `source_play_id` starts with ESPN's game id), which suggests ESPN as its live source; unverified. `play_sync.status` is `matched`, `awaiting_play`, `stale` ("cannot be treated as current") or `unknown` ("no usable reference or a game that is not in progress": before kickoff and after the final). An unavailable cursor answers 410 and a malformed or wrong-game one 400; the guide says to record a gap before starting over. Rows re-read later keep their original `changed_at`. Rows carry `season_year` and `week`; the board takes a week past 18 as the postseason, unverified until a playoff game. Live behavior, PIT@CIN 2026-11-15 (first half, plus whole-game traces where marked):
  - The scoreboard changes once per play, a median of 30s apart, and its clock only moves at plays.
  - A reading reached us a median of 6s after NFLMeta took it (p90 10s).
  - A play's row lands a median of 10s after the scoreboard shows the play; `play_sync.status` turns `matched` when it has.
  - Every live row is `provisional`, and 46 of 51 plays were corrected, up to five times. The last correction came a median of 4.4 minutes later, mostly filling in `drive_result` or the description, sometimes the yards. A review can move the scoreboard backward.
  - Live rows have 27 columns, with no timeouts, win probability or wall-clock time. Play types are ESPN's (`pass_reception`, `rush`, `sack`, `official_timeout`, `end_period`).
  - Cost: about 490 requests an hour at the recommended pace, so roughly 1,500 for a game.
  - Kickoff rows list the receiving team in `posteam_abbr`, and punt rows the punting team.
  - Club timeouts appear only in play text ("Timeout #2 by CIN at 01:52."). So does one charged for a lost challenge ("...was Upheld. ... (Timeout #1.)").
  - Over halftime, the scoreboard row has `situation: null`.
  - The scoreboard row also carries an `injuries` list: player, team, injury, `state` such as `questionable_to_return`, and `reported_at`.
  - **Scoring plays, whole game,** traced from our receipts and each row's `changed_at` (times are Mountain):
    - **Q2, nothing skipped:** the TD (play 164200) was written at 19:26:22 and received 6 s later, and its XP came as revision 2 at 19:27:02. The miss the user saw was the board's old logic.
    - **Q3, the TD written into the wrong row:** NFLMeta first put the TD's points into the previous touchdown's row (play 219100's revision 3 carried the new score, 31, at 20:25:06). The TD's own row (279400) came at 20:28:26, about 4 minutes after the scoreboard moved and after the next kickoff's row. Its extra point never got a try result; only the score column shows it. The old row was put back at 20:30:26. This was the only one of the game's 170 plays first seen after a later play.
    - **Otherwise:** a try arrives as its touchdown row's second revision, 20 to 60 s after the first, a two-point "success" included.
    - **Down line:** the scoreboard's down line stayed on "1st & Goal" after the Q3 score.
  - **The final, whole game:** `play_sync` turns `unknown` and names no last play. The old board parked at the first final row, and the snap after 0:20 was never read.
  - **Cadence, whole game:**
    - NFLMeta re-observes a game every 10.1 s (median and p90 of `observed_at`), so polling faster than its recommended 10 s gains a few seconds at most.
    - Reading the play feed only when the scoreboard flags a new play left a quarter of rows (98 of 390) waiting 15 s or more before we read them, worst 274 s. `--play-feed every-poll` reads it every tick instead.
  - Polymarket US lists NFL moneylines as `aec-nfl-{away}-{home}-{date}`. That night's market was liquid (a 0.25-cent spread), and it moved on an interception at least 20s before the feed reported it.
- Polymarket US participant agreement (binds account holders, so anyone with a key), clause 22: data "may not be redistributed except as PMUS expressly permits": https://www.polymarketexchange.com/files/legal/latest/participant-agreement
- Bovada terms of service (read 2026-11-02, https://www.bovada.lv/terms-of-service ; the page text was read through a summarizing fetch and cut off for length, so check the wording before relying on it): they cover use "whether as a registered user or as a guest". 8.1: content licensed for private personal use. 9.3: no copying or storing of content in another website or electronic retrieval system without prior written consent. 9.9: no commercial use without a license. 6.4, on bots: "All actions on the Website must be executed by players via the supplied user interface." `robots.txt` allows everything. None of it mentions odds data specifically. The cloud poller leaves Bovada off by default for this reason.
- The deployed poller (2026-11-15, five live NBA games): a run samples for about 52 seconds and uses about 120 MB of its 256, so a season of 10 game-hours a day is about 240,000 GB-seconds a month, inside Lambda's 400,000 free. The keyed stream answers from AWS: after each run's first REST sample, every price came off it.
- Reachability from AWS (one run from Lambda in us-east-2, 2026-11-02): Polymarket REST and listing, Bovada and StatsAPI all answer as they do from home, and the unsigned stream handshake gets Polymarket's own 401. One run says nothing about sustained polling.
- Kalshi Developer Agreement v1.1, section 3: API use "is expressly limited to facilitating a members own trading"; section 1: accessing the API counts as accepting it. Ruled out: https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf
- polymarket.com terms, sections 1 and 4.2: US residents are limited to content features, and scraping is barred. Ruled out from the US: https://polymarket.com/tos

Local execution note:

- Browser-based GitHub access worked.
- Shell-level DNS inside the sandbox did not work: `getent hosts github.com` returned no host and `git ls-remote https://github.com/MLB-LED-Scoreboard/mlb-led-scoreboard.git` failed with `Could not resolve host: github.com`.
- Local Claude Code should retry all git commands on the user's machine; the sandbox DNS failure should not be assumed to apply locally.
- Highlightly NFL API (https://highlightly.net), checked 2026-11-15:
  - Plans: Basic free (100 requests a day) and Pro $7.99 a month.
  - `GET /matches` refreshes once a minute; `/matches/{id}` (drives and plays, each play's down, distance and `possessionText` before and after) once every two minutes.
  - The game clock is to the minute only, and there are no timeouts. The key goes in the `x-rapidapi-key` header.
  - Too slow to drive a board; at most a backfill source.
- BALLDONTLIE (https://www.balldontlie.io), checked 2026-11-15:
  - Coverage: 40 leagues across basketball, NFL, MLB, NHL, NCAAF, MMA, golf, soccer, tennis, F1 and esports.
  - Pricing is per league: Free (5 a minute), ALL-STAR $9.99 a month, GOAT $39.99 a month, with a 7-day free trial. ALL-ACCESS is $299.99 a month for all leagues.
  - NFL `/games` (scores, quarter scores, status; no clock or situation) is on every tier. `/nfl/v1/plays` (`start_`/`end_` down, distance and yard line, `end_possession_text`, `clock_display`, `scoring_play`), ordered by wall-clock time, needs GOAT.
  - Its terms allow storing, archiving and serving your own boards.

NBA providers (review round 4's research thread):
- Sources: the head's report of 2026-11-15, kept at `~/omni-review/research/nba-providers-research.md`. BALLDONTLIE's docs and terms were re-checked 2026-11-16; the other providers are as the head reported them.
- What no feed at a family budget gives:
  - a running-clock flag or a shot clock. Only Sportradar Push (under contract), TheRundown at $399 a month and SportsAPI.app at $199 have one. Clocks are sampled, so the NBA game clock's colon doesn't blink either.
  - possession, or the coach's challenge count. Under the NBA rule, a successful first challenge earns a second.
- BALLDONTLIE NBA (re-checked 2026-11-16):
  - Tiers are per league:
    - Free: teams, players and games, at 5 requests a minute.
    - ALL-STAR, $9.99 a month: adds game player stats, active players and injuries, at 60 a minute.
    - GOAT, $39.99 a month: adds box scores (`GET /v1/box_scores/live` among them), plays, lineups and odds, at 600 a minute. A 7-day GOAT trial runs at 5 a minute.
  - The game object (the games endpoint is on every tier):
    - `status_state`: `scheduled`, `in_progress`, `final`, `postponed`, `canceled`, `delayed`, `suspended`, `abandoned` or `unknown`.
    - `status`: "1st Qtr" through "Halftime" to "Final", or a start time before tip-off.
    - `period`: 0 before tip-off and 5+ in overtime. Some old overtime finals read 4.
    - `time`: a clock string, such as "3:44".
    - Scores by quarter and overtime.
    - `home_`/`visitor_timeouts_remaining` and `home_`/`visitor_in_bonus`. Quarter, bonus and timeout data start with the 2023 season.
    - No possession, team fouls or running flag.
  - "Game data is realtime for in-progress games"; no refresh interval is published. One `/games?dates[]=` request covers a slate, so the Free tier's 5 a minute allows a full-slate read every 12 seconds.
  - Terms, last updated 2026-10-20:
    - §6 allows any lawful personal or commercial use, including "backend APIs that support those products, provided those APIs are not offered to third parties".
    - Copying, caching, storing and archiving are allowed, with no retention deadline.
    - Data may be kept after access ends (§17).
    - No attribution is required.
    - Barred: a substitute sports-data API or feed (§6), sharing API keys (§5, §7(b)), and automation outside the official interfaces (§7(d)).
    - There are no separate Free-tier terms.
    - So one key in a central poller can serve every Omni board; a key on each friend's board would be sharing it.
- Realtime Sports API Starter, $29 a month, with a 7-day no-card trial:
  - Live NBA and NHL play-by-play and sampled clocks, plus NFL, MLB and NCAA.
  - Its FAQ allows display to your own users, backend caching, and keeping data after cancelling; it claims no league license.
  - Its 10,000 units a month are pooled across every league and delivery type, and one full NBA night polled every 10 seconds nearly uses them up. It needs push, with ingest focused on favorite games.
- PropLine Hobby, $9 a month (Pro $19):
  - NBA and NHL score, period, sampled clock, last play and player stats, with game state refreshed about every 5 seconds.
  - Individual values in an end-user product are allowed; archive and post-cancellation rights are unverified.
  - Full NBA plays need Streaming Lite, at $39.
- Too slow or too thin: Highlightly All Sports Pro ($12.49) refreshes the NBA scoreboard every 60 seconds and details every 120. API-Sports has no NBA play-by-play, and its terms grant no publication license.
- Technically best: Sportradar NBA v8, with Push Game Clock (running state, tenths and shot clock) and edits and deletes. It needs a negotiated license, and its standard terms require destroying data after termination.
- Excluded:
  - NBA.com's CDN and `nba_api`: NBA.com's terms (2026-07-13) restrict live and archived play-by-play products, and the wrapper saw 403s in 2026.
  - ESPN: its terms bar automated access.
- Cost, by the head's model: a 10–15-game night needs about 900 slate requests at a 10-second poll, and plays polled every 10 seconds add 900 per game (9,900–14,400 calls a night).
- Trials the head suggests:
  - BALLDONTLIE NBA ALL-STAR on a night with NBA and NHL games;
  - Realtime's 7-day trial on one NBA and one NHL game;
  - PropLine Hobby against the full slate.
  Measure each against one broadcast:
  - receipt age, p50 and p95;
  - lead changes missed between samples;
  - clock nulls and placeholders;
  - when the last play arrives relative to the final.
