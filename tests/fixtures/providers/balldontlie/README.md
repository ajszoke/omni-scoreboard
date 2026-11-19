# BALLDONTLIE NBA replay fixtures

Three games from the night of 2026-11-18, read at the ALL-STAR tier (the slate and the stat rows about every 5 s
each) and cut down to one game per file. They're in the recorder's own format, one JSON record per line, gzipped.

| Files | Game | What it covers |
|---|---|---|
| `nba-2026-11-18-uta-lal.*` | UTA @ LAL | The whole game, from before the tip to the final. Timeouts in every quarter; free throws, including two that land in one read |
| `nba-2026-11-18-det-nop.*` | DET @ NOP | From the first quarter to the final. A won challenge: NOP's count drops at Q2 1:58, then comes back 114 s later with the clock still stopped. DET's count drops at Q4 12:00, a full clock, so that decrement falls in the break. DET drops by two at Q4 2:57 in one read. Two baskets are taken back (`fgm` −1), one credited to a teammate in the same read |
| `nba-2026-11-18-hou-ind.*` | HOU @ IND | From the third quarter to the final. Two calls taken back: a foul (Thompson, Q3 8:17) and an offensive foul (Siakam, Q4 6:35, `pf` −1 and `turnover` −1). Each comes a few minutes after the same team's count dropped in the same stoppage, and neither count comes back. A technical in a trip: at Q3 1:04, after one IND foul, HOU shoots three free throws by two shooters (Thompson, Durant, Thompson) |

## Format

**`*.games.jsonl.gz`.** Reads of `GET /v1/games?dates[]=…`:
- Each record is `{received_at, monotonic, took_s, status, headers, query, body: {data: [game], meta}}`.
- `data` holds this game's row only. The rows are exactly as the feed sent them.
- Only reads where the row changed are kept. A read identical to the one before it is dropped, so the gaps between
  records aren't the poll interval.

**`*.stats.jsonl.gz`.** Reads of `GET /v1/stats?game_ids[]=…`:
- Each record is `{received_at, monotonic, took_s, status, headers, query, changed: [row]}`.
- The recorder kept only the rows that changed since the previous read, so `changed` is never a full page. A
  player's first row is in the first read they appear in. Rebuild a full page by laying each read's rows over the
  ones before.
- Most reads include every player on the court, because their `min` ticks.
- Each row keeps its stat fields as sent: `min`, `fgm`, `fga`, `fg_pct`, `fg3m`, `fg3a`, `fg3_pct`, `ftm`, `fta`,
  `ft_pct`, `oreb`, `dreb`, `reb`, `ast`, `stl`, `blk`, `turnover`, `pf`, `pts`, `plus_minus`.
- The nested objects are trimmed:
  - `player`: `id`, `first_name`, `last_name`, `team_id`;
  - `team`: `id`, `abbreviation`;
  - `game`: `id`, `status`, `period`, `time`, `home_team_score`, `visitor_team_score`. These are the stat rows' own
    view of the game, which can lag or lead the slate's.

**Both kinds of record:**
- `monotonic` is the recorder's clock in seconds, shared by both files. Merge the two files on it to replay the
  night in arrival order.
- `headers` keeps `date` and the rate-limit headers (`x-ratelimit-limit`, `x-ratelimit-remaining`,
  `x-ratelimit-reset`).
- `query` names this game only.

The data is BALLDONTLIE's. Its terms allow keeping it, but not serving it as a feed. These files exist to test the
board's own reading of it.
