# Edge Engine

Prices every PrizePicks and Underdog line against sharp books and betting exchanges, blends in its own
player projections, logs every pick to a public, tamper-evident record, and sizes each day's entries
together with uncertainty-aware Kelly staking. It replaced the original `prizepicks_edge.py` script (still in the git history) and keeps the Edge
Finder app working.

```
 The Odds API ──► poller (credit budget) ──► SQLite: price history, pick'em lines
 ESPN / MLB   ──► injuries, rosters, box scores ──┘          │
                                                             ▼
          market price (vig removed) + projection ──► blended fair % ± uncertainty
                                                             │
                         pick log (hash chain, 20% holdout) ◄┤
                                                             ▼
                  candidate entries ──► portfolio optimizer ──► board ──► web app, alerts, Edge Finder JSON
                                                             │
          grading ──► box scores ──► nightly refit: blend weights, correlations ──► holdout backtest
```

## Quick start (your computer)

```bash
pip install ".[fast]"                 # numpy is optional but much faster
export ODDS_API_KEY=your_key SPORTS="nba nhl"
edge init
edge backfill --days 60               # past box scores so projections work on day one
edge once                             # one full pass; writes site/index.json and site/results.html
edge serve --port 8080                # web app at http://localhost:8080
edge create-user you@example.com --admin
```

`edge run` is the always-on mode for a server. `edge -h` lists every command.

## Run it on a server (Fly.io, about $5 to $10 a month)

```bash
fly launch --copy-config --no-deploy
fly volumes create edge_data --size 3
fly secrets set ODDS_API_KEY=... SECRET_KEY=$(openssl rand -hex 32) INVITE_CODE=pick-a-code \
               NTFY_TOPIC=edge-yourname-7f3k DAILY_CREDIT_BUDGET=1500 PUBLIC_URL=https://edge-engine.fly.dev/results
fly deploy
fly ssh console -C "edge create-user you@example.com --admin"
fly ssh console -C "edge backfill --days 60"
```

One container runs the engine loop and the web app together, sharing `/data/edge.db`. Back the volume
up (`fly volumes snapshots` runs daily by default; for more, add Litestream).

## No server yet: scheduled mode

`.github/workflows/engine.yml` runs a full pass four times a day, keeps the database in the repo, and
publishes the results page to GitHub Pages. It replaces `daily.yml` (disable that one). Same secrets
and variables as before. Switch to the server when you want real-time prices; then disable this workflow.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `ODDS_API_KEY` | (required) | The Odds API key |
| `SPORTS` | `nba nhl` | Any of `nba wnba nfl cfb nhl mlb` |
| `BOOKS` | 10 books | Sharp books and exchanges (Pinnacle, BetOnline, Novig, ProphetX), pick'em apps, US books. 10 books bill as one region |
| `DFS_APPS` | `prizepicks underdog` | Pick'em apps to price; add `pick6`, `dabble_us_dfs` |
| `DAILY_CREDIT_BUDGET` | `600` | Credits per day the poller may spend; it sets the polling interval to fit |
| `MIN_POLL_MINUTES` | `3` | Fastest polling, used near game time |
| `ALT_LINES` | `0` | `1` prices goblins and demons (more credits) |
| `LOG_EDGE` / `ALERT_EDGE` | `0` / `0.03` | Log picks at break-even or better; alert at 3+ points |
| `KELLY_MULT`, `MAX_STAKE_PCT`, `DAY_CAP_PCT`, `PLAYER_CAP_PCT` | `0.25`, `0.02`, `0.08`, `0.04` | Staking limits for the model portfolio |
| `NTFY_TOPIC`, `DISCORD_WEBHOOK` | | Phone and Discord alerts |
| `INVITE_CODE`, `OPEN_SIGNUPS` | | Sign-ups need the invite code unless `OPEN_SIGNUPS=1` |
| `APPS_FILE` | | JSON file overriding payout tables (Pick6, Dabble, promos) |

**Credits.** Each event poll costs (markets x regions). Five markets with 10 books is 5 credits.
Ten NBA games polled every 10 minutes for the 6 hours before tip is about 1,800 credits a day. The
poller polls far-off games every few hours and close games often, and stretches intervals to stay on
budget. Pick an Odds API plan that covers your budget times 30.

## The track record (step 1)

* **Every pick is logged** when the model first sees it at break-even or better, before the game, with
  the model version. Only the closing price and result are filled in later.
* **Hash chain.** Each pick stores `SHA-256(previous hash + JSON of its core fields)`. Core fields:
  `id, created_at, model_version, sport, game_id, app, player, market, line, side, tier, start,
  fair_market, fair_proj, fair, edge` (floats rounded to 6 places, keys sorted). Editing or deleting
  any pick breaks every hash after it. Anyone can check a download with `edge verify --csv picks.csv`.
  Publish the latest hash somewhere public now and then (a social post, a commit) so the whole chain
  can't be silently rebuilt.
* **Holdout.** One pick in five, chosen by its hash, is never used to fit models. `edge backtest`
  scores market vs projection vs final price on those picks. Run `edge backtest --strict` before
  shipping a model change; it fails if the new price is worse than the market alone.
* **Closing line value** is re-taken every poll until the game starts.
* **Model portfolio.** The entries the model recommends are logged and hash-chained too, so the results
  page shows real recommended stakes, not hand-picked wins.

Old picks from `history/picks.csv` were never hashed, so `edge import-csv` keeps them in a separate,
labeled table rather than mixing them into the verified record.

## Models

* **Market price.** Vig removed (power method) from each book, weighted toward sharp books and
  exchanges, shifted to the pick'em line with a stat model when needed, with an uncertainty estimate
  from book disagreement and data quality.
* **Projection.** Recency-weighted player average from stored box scores, pulled toward the longer
  average for small samples, adjusted for the opponent's defense, plus a usage bump when regular
  teammates are ruled out. Spread from the player's own variance.
* **Blend.** `logit(p) = c + a·logit(market) + b·logit(projection)`, fit nightly on training picks
  with a ridge toward market-only, so the projection only gains weight when it proves useful. Starts at
  85% market and 15% projection.
* **Correlations.** Learned nightly from box scores (teammates vs opponents, by stat pair), blended
  with hand-set priors by sample size.
* **Portfolio.** All legs simulated jointly (Gaussian copula). Entries are added one at a time, each
  choosing the one that most improves expected log growth of the whole day, then scaled by the Kelly
  fraction and by `edge² / (edge² + uncertainty²)`, with caps per entry, per player and per day.
* **Best app.** When a prop is on several apps, the board shows which line has the most edge.
* **Lineup reaction.** Injuries are checked every 10 minutes; a newly ruled-out player triggers an
  immediate repoll of that game, a projection update for teammates, and an alert.

## Edge Finder app

The engine writes the app's data file (`site/index.json`, and `data/latest.json` in scheduled mode).
With the server, the app's data is also at `/api/latest.json?token=...` (token on the Account page).

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

Tests run against fake API responses, including a full cycle: polling, pricing, logging, the
portfolio, alerts, grading, settlement, the results page, chain verification and the web app.

## Not yet verified against live services

The Odds API, ESPN and MLB responses are modeled on their documented and observed shapes, and parsing
is defensive, but the first live run is the real test. Watch the log of the first `edge once` and
check that legs are priced, injuries load, and games grade the next day.

See `LEGAL_CHECKLIST.md` before charging anyone.
