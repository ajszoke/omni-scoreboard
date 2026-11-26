# NFLMeta budget replay: November 2026

A synthetic month of NFL games read through the board's quota ledger (`omni.app.quota`) at each read strategy, from `omni/replay/nfl_budget.py`. Regenerate with `tools/nflmeta_budget.py`.

- **Plans:** Free is 20 requests a minute, 5,000 requests and 25,000 rows a month; Builder ($12 a month) is 120, 75,000 and 250,000. The month is the UTC calendar month.
- **Schedule:** 72 games kick off in November (UTC): Sundays of 11, 11, 14, 14, 12 games, four Thursdays (Thanksgiving's three games included), and four Monday nights (Nov 30's bills to December). The favorite (CAR) plays at 13:00 ET every Sunday. The typical Sunday is Nov 15: 14 games.
- **Billing model:** one row per game a live-scores read lists plus one per injury (up to 4 a game by its final); an unfiltered read lists the whole day, finals included. A followed game's plays bill 280 rows over 274 reads on change. A followed game is read every 10 s outside its 13-minute halftime hold, through a 120 s drain after its final.
- **Classes:** the favorite's score and plays, and each other final's drain or read (B to E), are focus; other games' own score reads (F's, their drains included) and their plays normal; the slate, the per-game and live-games reads background; the day's game list backfill.
- **NFLMeta's docs** (https://nflmeta.org/api-docs/core-endpoints, https://nflmeta.org/openapi.yaml): live-scores takes `phase` (pre, in, post), `game_id`, and `game_ids` (up to 100), and has no way to leave injuries out. They say each game listed bills a row; on PIT@CIN each injury entry billed one too, which this model follows. Responses carry `X-Monthly-Quota-Remaining` and, on plans with a row ceiling, `X-Monthly-Row-Remaining`, both for the current UTC month.

## Strategies

- **A:** today's solo favorite: CAR filtered at 10 s, its plays on change.
- **B30:** an unfiltered slate read every 30 s while any game is live; the favorite at 10 s with its plays; a drain for each final.
- **B45:** B with the slate read every 45 s.
- **C30:** B with each live game read filtered every 30 s instead of the slate.
- **C45:** C with each game read every 45 s.
- **D30:** B30 with plays on change for every live game.
- **E:** one phase=in read every 45 s while another game is live, the favorite left out by id; the day's game ids from one unfiltered read before its first kickoff; the favorite at 10 s with its plays; one filtered read for each other final.
- **F:** E with every other live game read by id every 30 s at normal, its plays on change, through its final's drain at that pace; with none left to read together, no phase=in read.

## Headline

Demand is what the strategy asks with nothing held back. A plan carries a strategy when the month's requests, rows and busiest minute all fit; the cells say when the ledger starts holding reads back on that plan, and when the month runs out. Games in one window kick off together and, in this model, end together, so their final drains bunch into one minute: that sets B, C and D's busiest minute.

| Strategy | Peak a minute | Sunday requests | Sunday rows | Month requests | Month rows | Free | Builder |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | 8 | 1,349 | 3,000 | 6,745 | 15,000 | no: exhausted Nov 22 | fits (8% of requests, 6% of rows) |
| B30 | 57 | 2,688 | 63,045 | 17,266 | 292,495 | no: sheds from Nov 1, exhausted Nov 8, 47 reads wait for the minute | no: sheds from Nov 22 |
| B45 | 56 | 2,299 | 43,359 | 14,061 | 201,687 | no: sheds from Nov 1, exhausted Nov 8, 55 reads wait for the minute | fits (18% of requests, 80% of rows); sheds from Nov 29 |
| C30 | 59 | 6,458 | 16,195 | 33,076 | 83,005 | no: sheds from Nov 1, exhausted Nov 8, 2,728 reads wait for the minute | fits (44% of requests, 33% of rows) |
| C45 | 58 | 4,820 | 12,100 | 24,634 | 61,900 | no: sheds from Nov 1, exhausted Nov 8, 125 reads wait for the minute | fits (32% of requests, 24% of rows) |
| D30 | 69 | 6,250 | 66,685 | 35,624 | 311,255 | no: sheds from Nov 1, exhausted Nov 8, 459 reads wait for the minute | no: sheds from Nov 22 |
| E | 17 | 2,144 | 11,324 | 13,270 | 57,902 | no: sheds from Nov 8, exhausted Nov 15 | fits (17% of requests, 23% of rows) |
| F | 48 | 9,579 | 18,484 | 49,169 | 94,802 | no: sheds from Nov 1, exhausted Nov 8, 6,308 reads wait for the minute | fits (65% of requests, 37% of rows); sheds from Nov 23 |

## Which plan carries which strategy

- **Free** carries none of them.
- **Builder** carries A, B45, C30, C45, E, F.

## What each plan does to the rest of the month

### Free

- **A** (5,000 requests and 10,657 rows read): from Nov 22, the month is spent: nothing more is read, the favorite included; refused for the month: 353 favorite play reads, 1,392 favorite score reads.
- **B30** (3,270 requests and 25,000 rows read): from Nov 1, slate reads every 60 s (background slowed); from Nov 1, no slate reads (background shed); from Nov 8, the month is spent: nothing more is read, the favorite included; refused for the month: 863 favorite play reads, 3,407 favorite score reads, 715 final drain reads, 8,894 slate reads.
- **B45** (3,210 requests and 25,000 rows read): from Nov 1, slate reads every 90 s (background slowed); from Nov 1, no slate reads (background shed); from Nov 8, the month is spent: nothing more is read, the favorite included; refused for the month: 857 favorite play reads, 3,379 favorite score reads, 715 final drain reads, 5,794 slate reads.
- **C30** (5,000 requests and 11,398 rows read): from Nov 1, other games' score reads every 60 s (background slowed); from Nov 2, no other games' score reads (background shed); from Nov 8, the month is spent: nothing more is read, the favorite included; refused for the month: 999 favorite play reads, 3,922 favorite score reads, 715 final drain reads, 21,438 other games' score reads.
- **C45** (5,000 requests and 11,497 rows read): from Nov 1, other games' score reads every 90 s (background slowed); from Nov 8, no other games' score reads (background shed); from Nov 8, the month is spent: nothing more is read, the favorite included; refused for the month: 944 favorite play reads, 3,710 favorite score reads, 715 final drain reads, 13,750 other games' score reads.
- **D30** (5,000 requests and 23,403 rows read): from Nov 1, slate reads every 60 s (background slowed); from Nov 1, no slate reads (background shed); from Nov 2, no other games' play reads (normal shed); from Nov 8, the month is spent: nothing more is read, the favorite included; refused for the month: 1,051 favorite play reads, 4,122 favorite score reads, 715 final drain reads, 15,618 other games' play reads, 8,896 slate reads.
- **E** (5,000 requests and 18,405 rows read): from Nov 8, no day's game list reads (backfill shed); from Nov 8, live-games reads every 90 s (background slowed); from Nov 9, no live-games reads (background shed); from Nov 15, the month is spent: nothing more is read, the favorite included; refused for the month: 10 day's game list reads, 720 favorite play reads, 2,828 favorite score reads, 43 final drain reads, 4,363 live-games reads.
- **F** (5,000 requests and 8,638 rows read): from Nov 1, no other games' own score reads (normal shed); from Nov 1, no other games' play reads (normal shed); from Nov 2, no day's game list reads (backfill shed); from Nov 8, the month is spent: nothing more is read, the favorite included; refused for the month: 12 day's game list reads, 1,045 favorite play reads, 4,100 favorite score reads, 21,739 other games' own score reads, 16,591 other games' play reads.

### Builder

- **A** (6,745 requests and 15,000 rows read): all of it.
- **B30** (15,296 requests and 225,293 rows read): from Nov 22, slate reads every 60 s (background slowed); from Nov 29, no slate reads (background shed); refused for the month: 618 slate reads.
- **B45** (13,938 requests and 194,608 rows read): from Nov 29, slate reads every 90 s (background slowed).
- **C30** (33,076 requests and 83,005 rows read): all of it.
- **C45** (24,634 requests and 61,900 rows read): all of it.
- **D30** (33,302 requests and 231,122 rows read): from Nov 22, slate reads every 60 s (background slowed); from Nov 29, no slate reads (background shed); refused for the month: 1,090 slate reads.
- **E** (13,270 requests and 57,902 rows read): all of it.
- **F** (49,166 requests and 94,786 rows read): from Nov 23, no day's game list reads (backfill shed); refused for the month: 3 day's game list reads.

## A board restart

The play cursor lives only in the process, so a restarted board reads each followed game's play feed again from its start, in pages of 100. On PIT@CIN, 19 restarts were most of the night's 4,737 play rows. A persisted cursor (with the play ledger it feeds) resumes where the board stopped: one request, billing only the changes since.

| Restart | Requests | Rows | With a persisted cursor |
| --- | --- | --- | --- |
| one followed game, 30 min after kickoff | 1 | 47 | 1 request, new changes only |
| one followed game, at halftime | 2 | 140 | 1 request, new changes only |
| one followed game, an hour after halftime | 3 | 234 | 1 request, new changes only |
| one followed game, at the final | 3 | 280 | 1 request, new changes only |
| D's 9 followed games, 15:00 ET on Nov 15 | 18 | 1,512 | 9 requests, new changes only |
