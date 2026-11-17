# Market service

One AWS poller reads betting markets, and boards pull the latest stored value, so every board shares
one set of upstream requests and no board holds a market key.

```
board ── POST /v1/heartbeat (every 5 min) ──► API Lambda ──► DynamoDB (active_until)
board ◄─ GET /v1/latest?games=… (each tick) ─ API Lambda ◄── DynamoDB (newest row per game)
EventBridge (every minute) ─► Poller Lambda ─┬─► DynamoDB rows (TTL 1 hour)
   stream + REST quotes ─────────────────────┘└─► S3 archive (kept)
```

## How it behaves

- **Two gates.** A run calls no market unless a heartbeat arrived in the last 10 minutes *and* a game is
  in a window. An always-on board would otherwise mean polling all winter.
- **Windows.** Market prices run from 10 minutes before first pitch until the game ends (8 hours at most).
  Pregame lines, when enabled, run from 12 hours before first pitch.
- **The loop.** Lambda caps a run's length and stops self-invoking functions, so a schedule fires each
  minute and a run samples every 10 seconds for about 50. A crash costs at most a minute. A DynamoDB
  lease stops runs overlapping, because this account's concurrency limit (10) leaves no room to reserve any.
- **Freshness.** Every successful sample writes a row, even if the price is unchanged, so `observed_at`
  proves the poller is alive. A frozen poller leaves an old timestamp, and *boards must ignore old rows*.
- **No key needed to run.** With no key in SSM, the poller uses public quotes, cached for 30 seconds.
- **Archive.** Each run's rows also go to `s3://<bucket>/readings/dt=YYYY-MM-DD/hour=HH/*.jsonl.gz`, one
  self-describing JSON line per reading with a schema version. It's a plain archive, not a data lake.
  The bucket survives deleting the stack.

## API

Every route needs `Authorization: Bearer <token>`, with one token per board.

| Route | Body or query | Answer |
|---|---|---|
| `POST /v1/heartbeat` | optional `{"version": "..."}` | `{"active_until": "..."}` |
| `GET /v1/latest` | `?games=<id>,<id>` (up to 30) | `{"server_time": "...", "games": {"<id>": {"win": {...}, "lines": {...}}}}` |

A game id is `str(game.id)`, such as `mlb:mlb_statsapi:745123`. A `win` or `lines` value is null when there's
no unexpired row. A `win` row has `observed_at`, `away_pct`, `home_pct`, `bid`, `ask`, `source` and `slug`; its
percentages are null when the market is too thin to trust.

## Connecting a board

Issue the board a token. This writes `OMNI_CLOUD_URL` and `OMNI_CLOUD_TOKEN` into its `.env` and prints neither:

```bash
.venv/bin/python cloud/tools/issue_board_token.py BOARD_ID --env-file .env
```

With `--meter` (the live meter is off by default), `python -m omni.app` then reads the meter from the
service, sends a heartbeat every 5 minutes and says `meter market service` when it starts. Without those
two settings it reads Polymarket's public quotes as before. The board ignores a reading more than a minute old by the service's clock, so a stopped
poller or an unreachable service falls through to the pipeline's 3-minute hold and then MLB's model.

## Deploying

Needs `uv`, `make` and the AWS CLI, but not Docker: `make` fetches prebuilt wheels for Lambda's architecture.
Always use the named profile, since the machine's default may be another account.

```bash
aws login --profile omni                                 # a browser sign-in; WSL prints a URL to open
.venv/bin/python cloud/tools/load_polymarket_key.py      # .env -> SSM, printing neither half
make -C cloud validate
make -C cloud deploy                                     # shows the change set and asks first
.venv/bin/python cloud/tools/issue_board_token.py BOARD_ID --env-file .env   # stores only a hash
```

Run `make -C cloud lock` after changing `requirements.txt`. To revoke a board, delete its `board#<id>` row.

To exercise the service with no MLB games on, poll a league the exchange lists:

```bash
make -C cloud deploy DEPLOY_ARGS='--parameter-overrides SlateLeagues=nba'
```

## Bovada is off by default

Its terms apply to guests as well as players. They limit content to private personal use (8.1) and bar
storing it in another electronic retrieval system without written consent (9.3). A scheduled poller
writing odds to a table and an archive is hard to square with that, so `BovadaEnabled` defaults to `false`,
and a board keeps fetching its own pregame odds. See `docs/agent_context/SOURCES.md`.
