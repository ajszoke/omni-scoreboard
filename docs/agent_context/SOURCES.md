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
- NBA team colors and logo files (2026-11-16). Colors: Wikipedia's `Module:Sports color/basketball` (CC BY-SA 4.0, Wikipedia contributors), pinned in `assets/raw/nba/manifest.json` at revision 1386095329 with its sha256; Miami's and Brooklyn's alts use the third color from their club articles' color lists. Logos: the NBA's own CDN, `https://cdn.nba.com/logos/nba/<NBA team id>/primary/L/logo.svg` (on light) and `.../primary/D/logo.svg` (on dark), keyed by the 16106127xx team ids. Its `global` set is the full logos with lettering, and `secondary`, `alternate` and `partial` answer 403. These primary marks are the clean ones ESPN's scoreboard shows: the Spurs' spur, the 76ers' "76", the Suns' sun-ball, with no lettered rings. Wikipedia and Commons carry only the clubs' full logos. Boston's shamrock roundel comes from ESPN's scoreboard logos (`a.espncdn.com/i/teamlogos/nba/500/scoreboard/bos.png` and `500-dark/...`), since the CDN has Lucky; Charlotte's side-profile hornet is a file the user picked (`curated` in the manifest, with its URL). The logos are the teams' trademarks wherever they're hosted, so the user accepted an ESPN-hosted logo file as a source (2026-11-16); ESPN is still not a data feed. The CDN stalls on a User-Agent carrying a comment, so the fetch tool sends a bare generic one.
- NFLMeta (https://nflmeta.org), checked 2026-11-15. Builder is $12/month: 120 requests/minute, 75,000 requests and 250,000 rows a month; Free is 20/minute, 5,000 and 25,000, for "personal, non-commercial learning, research, and private evaluation". Paid terms allow "displaying individual scores and statistics in your own application". Any plan may do "normal application caching", but no standing copy of the database and no systematic accumulation, scripted paging included. Termination doesn't entitle anyone to keep data obtained before it; a breach of the restrictions requires deleting any accumulated copy; cancelling a paid plan ends commercial use (terms re-read 2026-11-15 for review round 4). So raw recordings (`--record`) are opt-in, kept to what a diagnosis needs, never committed or shared, and deleted after. `GET /api/v1/live-scores` returns every live game with the situation (possession, down, distance, `yardline_100`), billing a row per game; a per-game cursor feed (`/api/v1/games/{id}/play-changes`) carries new and corrected plays, whose rows follow nflverse's play-by-play columns. Recommended poll is 10 seconds, with a target of 15-20 seconds behind its source. Its live play IDs look like ESPN's (each `source_play_id` starts with ESPN's game id), which suggests ESPN as its live source; unverified. `play_sync.status` is `matched`, `awaiting_play`, `stale` ("cannot be treated as current") or `unknown` ("no usable reference or a game that is not in progress": before kickoff and after the final). An unavailable cursor answers 410 and a malformed or wrong-game one 400; the guide says to record a gap before starting over. Rows re-read later keep their original `changed_at`. Rows carry `season_year` and `week`; the board takes a week past 18 as the postseason, unverified until a playoff game. Live behavior, PIT@CIN 2026-11-15 (first half, plus whole-game traces where marked):
  - The scoreboard changes once per play, a median of 30s apart, and its clock only moves at plays.
  - A reading reached us a median of 6s after NFLMeta took it (p90 10s).
  - A play's row lands a median of 10s after the scoreboard shows the play; `play_sync.status` turns `matched` when it has.
  - Every live row is `provisional`, and 46 of 51 plays were corrected, up to five times. The last correction came a median of 4.4 minutes later, mostly filling in `drive_result` or the description, sometimes the yards. A review can move the scoreboard backward.
  - Live rows have 27 columns, with no timeouts, win probability or wall-clock time. Play types are ESPN's (`pass_reception`, `rush`, `sack`, `official_timeout`, `end_period`).
  - Play text names clubs by the league's play-by-play codes. Five differ from the board's abbreviations: BLT (BAL), CLV (CLE), ARZ (ARI), HST (HOU) and LA (LAR). The board reads them through `omni/events/football_clubs.py` (seen as "TIMEOUT BLT" on LAC @ BAL, 2026-11-16).
  - Once no game is live, `recommended_poll_seconds` runs long: after LAC @ BAL's final (2026-11-16) the next recommended poll came about 15 minutes later.
  - Cost: about 490 requests an hour at the recommended pace, so roughly 1,500 for a game.
  - **Billing, from our own counts on PIT@CIN** (the two headers we kept, no content):
    - `live-scores` billed one row per game listed **plus one per injury entry listed**. Filtered to one game, that was 1 to 3 rows over the night.
    - Unfiltered, late that Sunday, it listed the day's whole window: 14 games, 13 of them final. That billed 68–70 rows, 54–56 of them injury entries.
    - `play-changes` billed one row per change returned. A read with nothing new billed no rows but still cost a request. One game's whole history, read from no cursor at the end, was 277 rows.
    - `X-Monthly-Quota-Remaining` fell by one per request on both endpoints, so it counts requests.
    - Each restart of the board re-read its game's plays from the start, because the cursor lives only in the process. That night's 19 restarts were most of the 4,737 play rows billed.
  - **The docs** (https://nflmeta.org/api-docs/core-endpoints and `openapi.yaml`, read 2026-11-17):
    - `live-scores` takes `phase` (`pre`, `in`, `post`; `in` includes halftime and excludes finals), `game_id`, and `game_ids` (up to 100). There's no way to leave injuries out.
    - The docs say a game listed bills one row; the injury rows above are measured.
    - Responses carry `X-NFLMeta-Response-Rows`, `X-Monthly-Quota-Remaining` and, on a plan with a row ceiling, `X-Monthly-Row-Remaining`, all for the current UTC month.
    - A 429 covers the minute and both monthly limits; a per-minute 429 doesn't use monthly quota. `play-changes` takes `limit` up to 1000 and `start=latest`. `/api/v1/usage` costs a request and no rows.
  - **The budget replay** (`tools/nflmeta_budget.py`; its report is `tests/fixtures/replay/nflmeta-budget.md`): a synthetic November (72 games, five Sundays) through the board's quota ledger.
    - **Free carries no strategy for a whole month,** not even today's solo favorite at 10 s (about 1,350 requests a Sunday, so the month's 5,000 run out on the fourth Sunday).
    - **The cheapest shared-board strategy** is one `phase=in` read every 45 s for the other live games, the favorite read by id at 10 s with its plays, and one read for each other final. That's about 13,300 requests and 58,000 rows for the month: 17% and 23% of Builder.
    - **An unfiltered slate read is the costly one.** It bills every game of the day, finals and their injuries included, so reading it every 30 s comes to about 290,000 rows a month, more than Builder carries.
    - **The user's plan (2026-11-17): Builder, once Free's month is spent.** The board runs Free (`--nflmeta-plan free`, the default) until NFLMeta says the month is spent. Then the log says so once, and the league's health reads "NFLMeta Free spent for <month>: switch to Builder". A restart with `--nflmeta-plan builder` keeps the month's counts, and the first answer's headers reconcile them; a month spent under Free no longer stays spent under Builder.
  - **Stoppages, PIT@CIN (minimized trace, arrival to the next play's arrival, 10 s polls):** an official timeout (its reason unknown; most look like commercial breaks) 171 s median (p90 181 s); a team timeout 81 s; the two-minute warning 141 s; after a score, through the try and kickoff, 201 s; an injury or a review 111 s; an ordinary snap 40 s.
  - **Mined from the whole PIT@CIN recording** before it expires (`build/telemetry/nflmeta/2026-11-15-PIT-CIN.json`, our own measurements only; `tools/nflmeta_mine.py`). Spans run from the feed's write of a stoppage's row to the next snap's, p25 / median / p75, one game only:
    - official timeout (12): 168 / 171 / 180 s; team timeout (3): 65 / 80 / 145 s; two-minute warning (2): 113 / 145 / 178 s; quarter break (2): 225 / 237 / 249 s; review ruling (4): 80 / 101 / 124 s; halftime: 860 s.
    - **Against policy v2's lease ceilings** (`BOARD_POLICY.md` §5: an official timeout 30 s, the two-minute warning and a quarter break 90 s each), every measured break ran longer, so the ceilings are conservative, as intended. Retune them only from many games, and never by treating an official timeout as a commercial: nothing in the feed says which one it was.
    - **Latency:** a play reached us 4.8 / 8.3 / 10.5 s after the feed wrote it. In 15 of 16 score changes the scoreboard moved first, by 14 / 20 / 44 s, before the play that explained it.
    - **Revisions:** 142 of 170 plays were revised (220 revisions, 90 / 200 / 340 s after first sight); one play arrived 211 s after a later one.
    - **Pace:** the median gap between snaps was 40–50 s in each quarter; quarters took 29, 57, 32 and 41 minutes of wall time; the game took 3 h 1 min.
    - **Billing:** 1,148 scoreboard reads billed 3,538 rows (one 70-row unfiltered read); 367 play-feed reads billed 4,737 rows, 133 of them empty.
    - The live feed never gave timeout or challenge counts: they were inferred from play text in every snapshot.
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
- **The user's ruling on display (2026-11-17): showing a source's data on the family's own boards, from a short-lived copy that expires, is display, not redistribution.** Friends and family see it briefly on their board, and it expires; they don't receive the data. `omni/providers/rights.py` records it (`ProjectUse.FAMILY_BOARD_DISPLAY`) beside each source's terms, which stay as written. Revisit it before any commercial use.
  - **Its condition is quick expiry,** checked in code: the market service's rows expire in an hour and its S3 archive in a day (the archive was kept indefinitely before; deploying the new template deletes what it held); a board ignores a reading over a minute old; NFLMeta's recordings and run logs expire 7 days after they're made.
  - Bovada stays off: its terms bar storing its content elsewhere at all (9.3), not only redistribution.
  - It doesn't reopen ESPN, whose terms bar automated access itself.
- **NFLMeta's wording expires wherever it lands.** The NFL runner's stdout carries our own telemetry only (kinds, ids, reasons, timings); its full lines, quoting the feed, go to a stamped `build/logs/nflmeta/<run>/runner.log`. The expiry sweep (`tools/recordings_due.py --expire`, and the NFL runner at start and exit) mines each overdue stamped folder into `build/telemetry/`, then deletes it. It never deletes an unstamped folder or a source's recording whose terms allow keeping it.
- Bovada terms of service (read 2026-11-02, https://www.bovada.lv/terms-of-service ; the page text was read through a summarizing fetch and cut off for length, so check the wording before relying on it): they cover use "whether as a registered user or as a guest". 8.1: content licensed for private personal use. 9.3: no copying or storing of content in another website or electronic retrieval system without prior written consent. 9.9: no commercial use without a license. 6.4, on bots: "All actions on the Website must be executed by players via the supplied user interface." `robots.txt` allows everything. None of it mentions odds data specifically. The cloud poller leaves Bovada off by default for this reason.
- The deployed poller (2026-11-15, five live NBA games): a run samples for about 52 seconds and uses about 120 MB of its 256, so a season of 10 game-hours a day is about 240,000 GB-seconds a month, inside Lambda's 400,000 free. The keyed stream answers from AWS: after each run's first REST sample, every price came off it.
- MLB StatsAPI signals for the board policy (checked 2026-11-16, free):
  - `game_contextMetrics` carries `inningBreakLength`, each game's scheduled inning break: 120 s for 13 of 15 games on 2026-09-16 and 145 s for 2, and 175 s in the World Series. It also carries home and away win probability (no leverage field), the series fields (`seriesDescription`, `gamesInSeries`, `seriesGameNumber`) and each team's `leagueRecord`.
  - `schedule?hydrate=broadcasts` marks national TV (`broadcasts[].isNational`; two ESPN games on 2026-09-16).
  - The live feed's `playEvents` type each stoppage by `details.eventType`. Over four games on 2026-09-16 (start to the next event's start): `mound_visit` 48 s median (p90 70 s), `pitching_substitution` 52 s (p90 107 s), `batter_timeout` 14 s, `offensive_substitution` 16 s, `pickoff` 22 s, and an inning break 2:12 (p90 2:36). Reviews come as `reviewDetails` (`inProgress`, `isOverturned`), which the provider's field list doesn't request yet.
  - A replayed feed (`timecode`) shows both sides' `runs`, `hits` and `errors` as explicit zeros from before the first pitch, so a missing count means a partial answer, not an unplayed side.
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
    - `time`: the game clock with its period, "Q2 1:17", with tenths under a minute ("Q1 :50.9"); "END Q1", "Half", "END Q3" and "END Q4" at breaks (seen live, 2026-11-16).
    - Scores by quarter and overtime.
    - `home_`/`visitor_timeouts_remaining` and `home_`/`visitor_in_bonus`. Quarter, bonus and timeout data start with the 2023 season.
    - No possession, team fouls or running flag.
  - "Game data is realtime for in-progress games"; no refresh interval is published. One `/games?dates[]=` request covers a slate, so the Free tier's 5 a minute allows a full-slate read every 12 seconds.
  - Probed with the user's Free key, 2026-11-16 (`tools/balldontlie_record.py --probe`):
    - The key goes bare in an `Authorization` header. A slate answered in 0.12–0.18 s, uncached (`cf-cache-status: DYNAMIC`, with a weak `ETag`).
    - Rate-limit headers: `x-ratelimit-limit` (5), `x-ratelimit-remaining`, and `x-ratelimit-reset`, the epoch second the minute's window ends. The window opened with the first request.
    - Endpoints above the tier (`/v1/stats`, `/v1/box_scores/live`) answer 401 "Unauthorized", as a refused key would, with no rate-limit headers.
    - Before tip-off: `status` is the tip time (UTC), `status_state` is `scheduled`, `period` is 0 and the scores are 0. `time`, quarter scores, timeouts and bonus are null.
    - At the final, `status` and `time` both read "Final". An overtime final has `period` 5 and `*_ot1` scores. Timeouts and bonus keep their last values.
    - Every game also has `date` (the US Eastern date the API files it under), `datetime` (tip-off, UTC), `season` (its starting year), `postseason`, `postponed` and `ist_stage` (the NBA Cup stage; null on these games).
    - A team has an id, conference, division, city, name, full name and abbreviation (`BKN`, `GSW`, `NOP`, `PHX`, `SAS`, `UTA`, `WAS`).
  - Recorded live, 2026-11-16 (8 games, a slate read every ~13 s from 16:50 to 21:26 Mountain; analysis in `~/omni-review/sources/evidence/live-2026-11-16.md`):
    - 1,084 answers, all 200: p50 0.17 s, p95 0.61 s, max 6.3 s. The 5-a-minute limit was fully used (`x-ratelimit-remaining` spread evenly over 0-4) with no 429, so 13 s is the edge; a board-side poller should back off on 429.
    - The `time` clock is sampled live: while play runs it advances about one game second per wall second (p50), so the feed is current to within a poll. It has no possession or running flag.
    - About 60% of reads change nothing (stoppages). A game's clock moves 13-26 s apart and its score about 39 s apart (medians).
    - A timeout shows as a decrement of `*_timeouts_remaining` (85 over 8 games; the NBA charges its mandatory timeouts to a team).
    - **Break lengths** (monotonic poll times, the sleep excluded): a timeout's decrement to the clock running again, 195 s median (p90 247 s, n 77), since most are TV timeouts; `END Q1`/`END Q3` to the next period's clock, 156 s (p90 209 s); `Half` to Q3's clock, 15:43.
    - `status_state` turns `final` 13-26 s after the clock reads Q4 :00.0, sometimes through "END Q4"; no score changed after a final.
    - The clock can step back: official corrections (Q2 2:12 to 2:13) and period-boundary transients ("Q4 :00.0" under period 4 before "Q4 11:45"; "Q2 12:00" under period 2 just before "Half"). Treat a boundary reading as provisional.
  - **ALL-STAR since 2026-11-18** (the user's plan; $9.99 a month, 60 requests a minute). Probed that night
    (`tools/balldontlie_record.py --probe`, `capabilities.json`), each documented endpoint asked once:
    - answered: `/v1/stats` (live player lines: `min`, `pts`, `fgm`/`fga`, `fg3m`/`fg3a`, `ftm`/`fta`, `oreb`/`dreb`/`reb`,
      `ast`, `stl`, `blk`, `turnover`, `pf`, `plus_minus`, with the player, team and game), `/v1/players/active`,
      `/v1/player_injuries` (`status`, `return_date`, `description`), and Free's games, teams and players;
    - 401 for every GOAT endpoint: play-by-play, box scores (live and by date), standings, leaders, season and team
      averages, advanced stats, win probabilities, lineups, shot charts, odds, player props, DFS and contracts.
    - So NBA captions can come only from changes in player lines between reads (a made 2- or 3-point basket, a free
      throw, an assist in the same read, a foul with its count, a steal, a block, a turnover), not from plays; and
      standings would have to be computed from results.
    - The recorder reads the slate and every running game's player lines every ~6 s within 55 a minute: on 2026-11-18
      (11 games) every answer was 200 with at least 3 requests left in each window.
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

A running game clock, by league (checked 2026-11-18; nothing is built on it):

Whether a feed can say, without guessing, that a game clock is running right now. Without that, a board shows the
clock as sampled: no blinking colon, no countdown between reads.

| League | Our source | A running flag? | What can be known instead | Feasible? |
|---|---|---|---|---|
| MLB | StatsAPI | No game clock. We found no pitch-timer state in the live feed (unverified: a field search wasn't possible) | Nothing to tick | Not applicable |
| NFL | NFLMeta | No: each play row has the period and the clock, no running state | NFL Rule 4 fixes whether the clock stops after a play (an incompletion, out of bounds, a score, a change of possession, a timeout, a penalty, an injury, the two-minute warning, a period's end) and whether it restarts at the snap or at the referee's ready-for-play signal (out of bounds restarts on the signal, except late in a half). So "stopped" can be derived from the last released play, and "stopped until the snap" for most stoppages. The ready-for-play signal and any 10-second runoff aren't in the feed. Sportradar's NFL push feeds show no running field in their samples | Partial, from the rules: stopped can be known; running, and from when, can't |
| NBA | BALLDONTLIE (ALL-STAR) | No, on any tier: `/v1/games` has a sampled `time`; GOAT's `/v1/plays` gives each play's `clock` | Two reads with a moving clock show it ran between them. Stoppages could be read from GOAT's play types (fouls, timeouts, violations, free throws, a made basket in the last 2:00), but the restart (the inbound touch) is no event. Sportradar's Push Game Clock has `clocks.running` (Realtime customers only, by contract) | No, at our tiers |
| NHL | Not chosen | **Yes, in the NHL's own web API:** `api-web.nhle.com/v1/gamecenter/{id}/play-by-play` carries `clock.running` and `clock.inIntermission`. Seen only on a finished game, so a live `true` is unverified; the terms for automated use are unverified. Sportradar's NHL Push Clock has `clocks.running` (contract) | Hockey's clock stops on every whistle, which the flag reports directly | Likely, if the NHL's API terms allow it |
| PGA | Not chosen | No game clock | Tee times and holes | Not applicable |
