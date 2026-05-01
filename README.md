# Chimera Betting Engine — FSU100

The live Betfair Exchange betting engine for Chimera Sports Trading. FSU100
maintains a streaming connection to the exchange, applies a JSON-driven
strategy plugin to incoming market updates, and (in `LIVE` mode) places lay
bets on the exchange.

FSU100 deliberately shares the strategy evaluator and plugin format with the
Chimera Backtest Tool. A plugin authored and validated against backtest
behaves identically when activated here — one strategy contract, two modes:

* **Backtest Tool** reads historic `.bz2` files from GCS or the Betfair
  Historic Data API.
* **FSU100** reads the live Betfair Exchange streaming API.

## Architecture

```
┌──────────────────────┐        ┌──────────────────────┐
│  AIM agent / portal  │ ─────► │   FastAPI service    │
└──────────────────────┘        │  (Cloud Run, Python  │
                                │       3.12)          │
                                └──────────────────────┘
                                  │     │             │
                ┌─────────────────┘     │             └──────────────┐
                ▼                       ▼                            ▼
   Betfair streaming socket    Betfair REST (orders)       gs://chiops-fsu100-results
   (live MarketBook updates)   (place / cancel / list)     (settled bets, daily summary)
```

* `main.py` — FastAPI app exposing the three endpoint sets.
* `engine.py` — the live engine: stream lifecycle, processing loop, bet
  placement, order tracking, settlement, daily persistence.
* `evaluator.py` — the pure strategy function. **Identical** to the
  Backtest Tool's `evaluator.py`.
* `services/` — `betfair_service.py` (live Betfair wrapper), `gcs_service.py`
  (results persistence), `secrets_service.py` (Secret Manager loader).
* `core/` — config, structured logging, in-process event bus, plugin store.
* `models/` — Pydantic schemas (`schemas.py`) and immutable decision
  dataclasses (`decisions.py`).
* `plugins/` — strategy JSON files; the registry refreshes them at startup.

The engine runs as a single Cloud Run instance with `min-instances=1`
during racing hours so the streaming socket stays open.

## Operating modes

| Mode | Stream | Bets placed | Purpose |
|------|--------|-------------|---------|
| `LIVE`    | Active | Yes — real money | Production betting |
| `DRY_RUN` | Active | No — decisions logged only | Validate strategy with live data, no risk |
| `STOPPED` | No stream | No | Engine idle |

**Default mode on first deploy is `STOPPED`.** The engine does not
auto-start. Operators must explicitly call
`POST /admin/control/start` (or `/dry-run`) after deploy. Always run
`DRY_RUN` for at least one full session before activating `LIVE`.

## API surface

### Set 1 — Parameters (admin)

| Method | Path                            | Purpose                                          |
| ------ | ------------------------------- | ------------------------------------------------ |
| GET    | `/admin/status`                 | Health, version, mode, active plugin, stream status. |
| GET    | `/admin/config`                 | Effective runtime configuration as structured JSON. |
| PUT    | `/admin/config`                 | Update runtime configuration in-process.         |
| GET    | `/admin/stats`                  | Aggregate metrics: bets placed, P&L, strike rate, etc. |
| GET    | `/admin/activity`               | Recent activity ring buffer (newest first).      |
| POST   | `/admin/control/{action}`       | `start`, `stop`, `dry-run`, `reset-stats`.       |
| GET    | `/admin/events`                 | Server-Sent Events stream of lifecycle updates.  |

### Set 2 — GUI (portal-facing)

| Method | Path                                       | Purpose                                       |
| ------ | ------------------------------------------ | --------------------------------------------- |
| GET    | `/api/markets`                             | Active markets currently being monitored.     |
| GET    | `/api/positions`                           | Open positions held on the exchange.          |
| GET    | `/api/results`                             | Today's settled bets and rolling summary.     |
| GET    | `/api/results/history`                     | Historical settled bets (date range, paginated). |
| GET    | `/api/strategies`                          | Installed strategy plugins.                   |
| GET    | `/api/strategies/{name}/schema`            | Editor schema for a specific plugin.          |
| GET    | `/api/account`                             | Account balance, exposure, available funds.   |

### Set 3 — Content (AIM agent)

| Method | Path                                                | Purpose                                  |
| ------ | --------------------------------------------------- | ---------------------------------------- |
| POST   | `/api/evaluate`                                     | Evaluate a market snapshot + plugin → decision (no placement). |
| POST   | `/api/place`                                        | Place a bet on Betfair (LIVE mode only). |
| POST   | `/api/cancel`                                       | Cancel an open order by `bet_id`.         |
| GET    | `/api/settled`                                      | Pull settled orders for a date range.    |

### Request example — `POST /api/evaluate`

The plugin block is the complete strategy instruction set. FSU100 ignores
the historic-only fields (`source.type`, `source.bucket`, `parser.format`)
but it honours `source.filters.countries`, `source.filters.market_types`,
and `parser.time_before_off_seconds`.

```json
{
  "market_snapshot": {
    "market_id": "1.234567890",
    "publish_time": "2026-06-01T13:55:00Z",
    "market_definition": {
      "market_time": "2026-06-01T14:00:00Z",
      "venue": "Kempton",
      "country_code": "GB",
      "market_type": "WIN",
      "in_play": false,
      "runners": [
        { "selection_id": 101, "name": "Alpha", "status": "ACTIVE" },
        { "selection_id": 102, "name": "Bravo", "status": "ACTIVE" }
      ]
    },
    "runners": [
      { "selection_id": 101, "name": "Alpha", "last_price_traded": 1.80 },
      { "selection_id": 102, "name": "Bravo", "last_price_traded": 4.00 }
    ]
  },
  "plugin": {
    "name": "mark_4rule_lay_v1",
    "version": "1.0.0",
    "source": {
      "type": "gcs",
      "bucket": "gs://betfair-basic-historic/ADVANCED/",
      "date_range": { "start": "2025-01-01", "end": "2025-01-31" },
      "filters": { "countries": ["GB", "IE"], "market_types": ["WIN"] }
    },
    "parser": { "format": "betfair_mcm", "time_before_off_seconds": 300 },
    "strategy": {
      "rules": [
        { "name": "rule_1", "odds_band": [1.50, 2.00], "base_stake": 3 },
        { "name": "rule_2", "odds_band": [2.00, 5.00], "base_stake": 2 }
      ],
      "controls": {
        "hard_floor": 1.50,
        "hard_ceiling": 8.00,
        "jofs_enabled": true,
        "jofs_spread": 0.20,
        "mark_uplift": 2.0,
        "spread_control": true
      }
    },
    "staking": { "point_value": 7.50 }
  }
}
```

### Request example — `POST /api/place`

```json
{
  "market_id": "1.234567890",
  "decision": {
    "selection_id": 101,
    "runner_name": "Alpha",
    "side": "LAY",
    "price": 1.80,
    "stake": 22.50,
    "liability": 18.00,
    "rule_applied": "rule_1"
  },
  "persistence_type": "LAPSE",
  "customer_order_ref": "aim-2026-06-01-101"
}
```

`persistence_type` defaults to `LAPSE` (cancel at in-play); the engine never
takes positions into in-play. `POST /api/place` is rejected with `502` when
the engine is not in `LIVE` mode.

### Server-Sent Events (`GET /admin/events`)

| Event | Data |
|-------|------|
| `mode_changed`         | `old_mode`, `new_mode` |
| `stream_connected`     | `countries`, `market_types` |
| `stream_disconnected`  | `reason` |
| `evaluation`           | `market_id`, `decision` (`BET` / `NO_BET`), `rule`, `selection_id`, `runner_name`, `side`, `price`, `stake`, `liability`, `mode` (or `reason`/`detail` for `NO_BET`) |
| `bet_placed`           | `market_id`, `bet_id`, `rule`, `selection_id`, `runner_name`, `side`, `price`, `stake`, `liability` |
| `bet_settled`          | `bet_id`, `market_id`, `outcome` (`WON`/`LOST`/`VOID`), `pnl` |
| `positions_updated`    | `open_positions`, `total_exposure` |
| `stats_reset`          | (empty) |
| `error`                | `message`, plus context (`market_id`, `report`, …) |

## Adding a new strategy plugin

A plugin is a single JSON file in `plugins/` conforming to `PluginConfig`
(see `models/schemas.py`). FSU100 and the Backtest Tool consume the same
schema, so a plugin can be promoted from one to the other without
modification.

```bash
cp plugins/mark_4rule_lay_v1.json plugins/my_new_strategy_v1.json
# edit name, version, rules, controls
gcloud run deploy fsu100 ...
```

After deploy, switch the engine to the new plugin via the admin API:

```bash
curl -X PUT https://<service-url>/admin/config \
  -H 'Content-Type: application/json' \
  -d '{
    "log_level": "INFO",
    "activity_log_size": 200,
    "results_bucket": "chiops-fsu100-results",
    "active_plugin": "my_new_strategy_v1",
    "countries": ["GB", "IE"],
    "market_types": ["WIN"],
    "point_value": 7.5,
    "customer_strategy_ref": "fsu100"
  }'
```

## Strategy contract

`evaluator.evaluate(market_book, strategy, *, point_value, filters_country,
filters_market_type)` is a **pure function** — the same bytes as the
Backtest Tool's evaluator. There are zero hardcoded rules and zero
rule-name string matches. Rule names are labels carried into events and
results for reporting only — a plugin with rules named `banana_1`,
`banana_2`, … runs through the same code path as `rule_1`, `rule_2`, ….

```
final_stake = base_stake * mark_uplift * point_value
```

* `base_stake` (or `stake`) — read from the matched rule.
* `mark_uplift` — read from `strategy.controls`. Defaults to `1.0` when
  unset.
* `point_value` — read from `staking`.

When JOFS splits the bet across the joint favourite and 2nd favourite,
each leg gets `final_stake / 2`. When `also_lay_2nd` is set on the rule,
both the favourite and 2nd favourite receive a full-stake lay.

## Safety contract

This is a **live betting engine handling real money.** The build follows a
non-negotiable safety contract:

1. Default mode on deploy is `STOPPED`. The engine does not auto-start.
2. `DRY_RUN` must be exercised before `LIVE` is ever activated.
3. Every bet placed is logged with the full instruction trail and emitted
   via SSE.
4. Every error is caught, logged, and emitted via SSE — no silent
   failures.
5. If the streaming socket disconnects, the engine stops placing bets and
   emits a `stream_disconnected` event.
6. Bet placement failures are logged but do not crash the engine.
7. Daily settled bets and summaries are persisted to GCS so state
   survives container restarts.

## Deployment

The repo deploys to Cloud Run via `gcloud run deploy`. Charles handles
deploys manually via the GCP Console after pushing to GitHub:

```bash
gcloud run deploy fsu100 \
  --source . \
  --region europe-west2 \
  --project chiops \
  --service-account fsu100@chiops.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --min-instances 1
```

Secrets (`betfair-username`, `betfair-password`, `betfair-app-key`,
`betfair-cert-pem`, `betfair-key-pem`) are read at runtime from Secret
Manager via the bound service account; nothing is set as an environment
variable.

### Required IAM bindings

Run once when the service account is created:

```bash
gcloud iam service-accounts create fsu100 \
  --display-name="FSU100 Betting Engine SA" \
  --project=chiops

gcloud storage buckets create gs://chiops-fsu100-results \
  --location=europe-west2 \
  --project=chiops \
  --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding gs://chiops-fsu100-results \
  --member="serviceAccount:fsu100@chiops.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

for SECRET in betfair-username betfair-password betfair-app-key betfair-cert-pem betfair-key-pem; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:fsu100@chiops.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" \
    --project=chiops
done
```

### Environment

| Item                    | Value                                         |
| ----------------------- | --------------------------------------------- |
| GCP project             | `chiops`                                      |
| Region                  | `europe-west2` (London — required for Betfair) |
| Service account         | `fsu100@chiops.iam.gserviceaccount.com`       |
| Results bucket          | `gs://chiops-fsu100-results/`                 |
| Repo                    | `https://github.com/chimeracloud/fsu100.git`  |
| Local path              | `/Users/charles/Projects/fsu100/`             |

The service reads optional configuration from environment variables
prefixed `CHIMERA_` (see `core/config.py`); deploy without overrides for
production defaults. Set `min-instances=1` during racing hours so the
streaming session is not lost to scale-to-zero.

## Tests

```bash
pip install -r requirements.txt
pip install pytest
pytest tests/
```

The evaluator suite mirrors the Backtest Tool's exactly, covering each
rule in `mark_4rule_lay_v1`, the JOFS split, the spread control gate,
floor/ceiling guards, and idempotency. The schema suite exercises the
admin / evaluate / place / cancel contracts.

## Changelog

### 1.0.0 — initial release
* Three endpoint sets (`/admin`, GUI, content).
* Live Betfair streaming integration via `betfairlightweight`.
* `LIVE`, `DRY_RUN`, `STOPPED` operating modes — defaults to `STOPPED`.
* Pure-function strategy evaluator shared verbatim with the Backtest Tool.
* Order tracking and settlement pollers with daily GCS persistence.
* Server-Sent Events stream covering every lifecycle transition.
* Bundled `mark_4rule_lay_v1` plugin (identical to the Backtest Tool's).
