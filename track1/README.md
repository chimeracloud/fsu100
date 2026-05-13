# FSU100 — Track 1 Production Lay Engine

Branch: `track1-production` · Repo: `chimeracloud/fsu100`

Pure rule-based Betfair lay engine for UK/IE horse racing. Mark's 6 rules
plus controls, signals, and the risk overlay (TOP2 + MOM). Designed to be
deployed this week as a working production engine.

No plugins. No Acceptor. No AI generation. No strategy stacks. Just the
strategy, the streaming connection, and the portal page.

## What this is

* **One evaluator function** in `evaluator.py` — same code path for live
  and backtest. Both call `evaluate(snapshot, settings)`. Cannot drift.
* **betfairlightweight used as-is** — auth, streaming, market cache,
  place orders, settlement. No wrappers, no abstractions.
* **Three modes** — `STOPPED` (default), `DRY_RUN` (everything except
  real bets), `LIVE` (real money).
* **Daily results to GCS** — `gs://chiops-fsu100-results/track1/daily/`.
* **One portal page** at `/betfair/horse-racing` — status, live feed,
  daily summary, settings.

## File layout

| File | Purpose |
|---|---|
| `rules.py` | Core rules + spread control (copied verbatim from `charles-ascot/lay-engine`). |
| `signal_filters.py` | Four signal filters (copied verbatim). |
| `top2_concentration.py` | TOP2 risk overlay (copied verbatim). |
| `market_overlay.py` | Market Overlay Modifier (copied verbatim). |
| `settings.py` | Engine config dataclasses. Maps 1:1 onto the Bet Settings UI. |
| `models.py` | `MarketSnapshot`, `EvaluationResult`, `PlacedBet`, `SettledBet`. |
| `evaluator.py` | The single 13-step pipeline. Pure function. |
| `engine.py` | betfairlightweight glue — auth, stream, place, settle. |
| `gcs.py` | Daily results persistence. |
| `main.py` | FastAPI app. |
| `Dockerfile` | Build context for Cloud Run. |

## The pipeline (matches `docs/STRATEGY_SPECIFICATION.md` byte-for-byte)

```
0   Fetch market           (caller — engine or backtest)
1   Spread Control         rules.check_spread
2   Core Rules             rules.apply_rules
2.1 MAX_LAY_ODDS guard     inside apply_rules
2.2 Mark Ceiling           inside apply_rules
2.3 Mark Floor             inside apply_rules
2.4 JOFS split             inside apply_rules
2.5 Mark Uplift            inside apply_rules
3   Point Value × stake    evaluator
4   Signal: Overround      signal_filters
5   Signal: Field Size     signal_filters
6   Signal: Steam Gate     signal_filters
7   Signal: Band Perf      signal_filters
8   TOP2 Concentration     top2_concentration
9   Market Overlay (MOM)   market_overlay
10  Settlement & P&L       engine.poll_settlement
```

## Default settings (matches current live engine)

* Mode: STOPPED on boot
* Countries: GB, IE
* Market types: WIN
* Process window: 5 minutes before off
* Point value: £1/pt
* Spread Control: ON
* JOFS: ON
* Mark Ceiling / Floor / Uplift: OFF
* All 4 signal filters: OFF
* TOP2 / MOM: OFF

## Endpoints

```
GET  /admin/status                  mode, stream, counters
GET  /admin/config                  current settings
PUT  /admin/config                  update settings
POST /admin/control/{start|live|stop}
GET  /admin/events                  SSE — evaluations + placements + settlements

GET  /api/results                   today's results
GET  /api/results/{YYYY-MM-DD}      historic day
GET  /api/account                   Betfair balance + exposure

GET  /health                        liveness
GET  /ready                         readiness
```

## Build + deploy

```bash
# From this directory:
docker build -t fsu100-track1 .
gcloud run deploy fsu100-track1 \
  --image fsu100-track1 \
  --region europe-west2 \
  --service-account fsu100@chiops.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --memory 1Gi --cpu 2 --min-instances 1 --no-cpu-throttling
```

Cloud Build trigger (set up separately) should target this directory
and the `track1-production` branch.

## Secrets in Secret Manager (project `chiops`)

| Secret | Contents |
|---|---|
| `betfair-username` | Betfair login email |
| `betfair-password` | Betfair password |
| `betfair-app-key` | Application key |
| `betfair-cert-pem` | Client cert PEM body |
| `betfair-key-pem` | Client key PEM body |

## Why not the existing `main` branch engine?

`main` has accumulated the plugin system, the Acceptor, the strategy
stack — none of which Mark's production needs this week. Track 1 ships
the engine cleanly so it runs, then Track 2 layers complexity back on
top deliberately.

This branch is intentionally minimal. Resist the urge to abstract.
