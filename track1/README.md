# CLE V2 — Chimera Lay Engine v2

**Production lay engine for UK / IE horse racing WIN markets.**
Repo: `chimeracloud/fsu100` · Branch: `track1-production` ·
Cloud Run service: `fsu100-track1` (`europe-west2`).

V2 because the original engine was the `charles-ascot/lay-engine` repo
(now superseded). Strategy logic is descended from that codebase — the
four rule modules were carried over verbatim and refined here — but
the architecture is a clean re-start: streaming-first I/O, one
evaluator function, no plugins, no Acceptor, no AI generation.

---

## Operating principles

1. **One evaluator function — `evaluator.evaluate(snapshot, settings)`.**
   Same code path for live and (future) backtest. Cannot drift, because
   there is only one function. Settings flow through unchanged.
2. **betfairlightweight used as-is.** Auth, streaming, market cache,
   placement, settlement — direct calls. No wrappers, no framework.
3. **Three orthogonal modes:** `STOPPED` (default on boot), `DRY_RUN`
   (everything except real bet placement), `LIVE` (real money).
4. **Permanent per-market placement guard.** In LIVE mode, a market
   can never be bet on twice in a session, regardless of mode toggling.
   Prevents the duplicate-placement footgun.

---

## File layout

| File | Purpose |
|---|---|
| `rules.py` | Core rules (1, 2A/B/C, 3A/B) + spread control + JOFS + Mark Ceiling/Floor/Uplift. Independent stakes for 3A and 3B. |
| `signal_filters.py` | Four pre-bet filters: Overround, Field Size, Steam Gate, Band Performance. |
| `top2_concentration.py` | TOP2 risk overlay — block/suppress when top-2 prob is too concentrated. |
| `market_overlay.py` | Market Overlay Modifier (MOM) — scales stake by exchange overround. |
| `settings.py` | Engine config dataclasses. Maps 1:1 onto the Bet Settings UI panel. |
| `models.py` | `MarketSnapshot`, `EvaluationResult`, `PlacedBet`, `SettledBet`. |
| `evaluator.py` | The single 13-step pipeline. Pure function. |
| `engine.py` | betfairlightweight glue — auth, stream, drain, place, settle. |
| `gcs.py` | Daily results persistence to GCS. |
| `main.py` | FastAPI app entrypoint. |
| `Dockerfile` | Cloud Run build context. |

---

## The 13-step pipeline

Every market processed through the same sequence. Steps can short-circuit
downstream steps when they skip / block.

```
0   Fetch market               (engine or future backtest caller)
1   Spread Control             rules.check_spread
2   Core Rules                 rules.apply_rules
2.1 MAX_LAY_ODDS guard         inside apply_rules
2.2 Mark Ceiling               inside apply_rules
2.3 Mark Floor                 inside apply_rules
2.4 JOFS split                 inside apply_rules
2.5 Mark Uplift                inside apply_rules
3   Point Value multiplier     evaluator
4   Signal: Overround          signal_filters
5   Signal: Field Size         signal_filters
6   Signal: Steam Gate         signal_filters
7   Signal: Band Performance   signal_filters
8   TOP2 Concentration         top2_concentration
9   Market Overlay (MOM)       market_overlay
10  Settlement & P&L           engine._poll_settlement
```

---

## Default settings (cold boot)

| Group | Field | Default |
|---|---|---|
| General | `point_value` | £1 / pt |
| General | `countries` | GB, IE |
| General | `process_window_mins` | 5 |
| General | `mode` | `STOPPED` |
| Rules | Rule 1 (fav < 2.0) | enabled, 3 pts |
| Rules | Rule 2A (2.0–split1) | enabled, **0 pts** (skips band) |
| Rules | Rule 2B (split1–split2) | enabled, 1 pt |
| Rules | Rule 2C (split2–5.0) | enabled, 2 pts |
| Rules | Rule 3A (>5.0, gap < threshold) | enabled, 1 pt (per-instruction; lays fav + 2nd fav) |
| Rules | Rule 3B (>5.0, gap ≥ threshold) | enabled, 1 pt (lays fav only) |
| Rules | rule2_split1 / split2 | 3.0 / 4.0 |
| Rules | rule3_gap_threshold | 2.0 |
| Controls | Spread Control | ON |
| Controls | JOFS | ON (threshold 0.20) |
| Controls | Mark Ceiling / Floor / Uplift | OFF |
| Signals | Overround / Field Size / Steam Gate / Band Performance | OFF |
| Risk Overlay | TOP2 Concentration / Market Overlay (MOM) | OFF |

Settings persist to GCS at `gs://chiops-clev2-trading/settings/current.json`
on every successful `PUT /admin/config`. The lifespan handler restores
them on boot, so a redeploy or cold start no longer reverts your tuning.
Default values above are used only on first boot when the file doesn't
exist yet, or as a fallback if a persisted file fails validation.

---

## Endpoint surface

```
GET  /admin/status                          mode, stream, counters, balance, exposure
GET  /admin/config                          current Settings as JSON
PUT  /admin/config                          replace Settings (tolerates partial payloads)
POST /admin/control/{start|live|stop}       start = DRY_RUN, live = LIVE, stop = STOPPED
GET  /admin/events                          SSE — evaluations + placements + settlements + errors

GET  /api/results                           today's evaluations + placements + settlements
GET  /api/results/{YYYY-MM-DD}              historic day's persisted results
GET  /api/account                           Betfair balance + exposure (engine-cached)

GET  /health                                liveness probe
GET  /ready                                 readiness probe
```

---

## Data storage

All CLE V2 trading data lives under one bucket:
**`gs://chiops-clev2-trading/`** (region `europe-west2`). The fsu100
service account has `roles/storage.objectAdmin` on it.

| Path | Written by | Contents |
|---|---|---|
| `daily/{YYYY-MM-DD}.json` | every evaluation, placement, settlement | Full timeline of the day's session — what the engine saw, what it decided, what it placed, what settled |
| `settled/{YYYY-MM-DD}.json` | every Betfair-confirmed settlement | Subset of daily — only the rows that came back from `list_cleared_orders`. Useful for audit / P&L reconciliation |
| `errors/{YYYY-MM-DD}.json` | every error event from `_push_event("error", ...)` | Post-session error log. Placement failures, stream issues, anything that fired an error |
| `snapshots/{YYYY-MM-DD}_start.json` | first STOPPED → DRY_RUN/LIVE transition | Engine status at session start (balance, settings, mode) |
| `snapshots/{YYYY-MM-DD}_end.json` | active → STOPPED transition | Engine status at session end. Lets you diff against `_start.json` |
| `markets/{YYYY-MM-DD}_catalogue.json` | flushed on session end | Per-market runner names + race_time. Built from the `list_market_catalogue` cache |
| `settings/current.json` | every successful `PUT /admin/config` | Latest operator settings. Single file, latest-wins. Restored on lifespan startup. |

Each writer rewrites the whole file in full on every call — no
streaming append. The cost is a few bytes per write; the benefit is
zero risk of partial-write corruption.

### Other persistence

| Item | Where |
|---|---|
| Cloud Run application logs | Cloud Logging (project `chiops`, service `fsu100-track1`) |
| Placement context cache | In-memory, rehydrated from today's daily JSON on boot |
| Runner-name catalogue cache | In-memory, written to `markets/{date}_catalogue.json` on session end |
| `_placed_markets` permanent dedup | In-memory, rehydrated from today's daily JSON on boot (any `placement` row where `simulated=False`) |
| Betfair credentials | Secret Manager (`chiops` project) — `betfair-{username, password, app-key, cert-pem, key-pem}` |

---

## Build + deploy

Auto-deploy on push to `track1-production` via Cloud Build trigger
`track1-production-fsu100-track1`. Trigger watches `track1/**` paths,
builds `track1/Dockerfile`, deploys to Cloud Run service `fsu100-track1`.

Manual deploy from this directory if needed:

```bash
gcloud run deploy fsu100-track1 \
  --source . \
  --region europe-west2 \
  --service-account fsu100@chiops.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --memory 1Gi --cpu 2 \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --set-env-vars GCP_PROJECT=chiops
```

`max-instances=1` is **required** — the engine is stateful (in-memory
caches, single Betfair stream subscription, placement guard). Multiple
instances would create duplicate streams and concurrent bet placement.

---

## Mode semantics

| Mode | Stream | Evaluator | Placement | Settlement polling | Window upper bound |
|---|---|---|---|---|---|
| STOPPED | disconnected | not running | n/a | not polling | n/a |
| DRY_RUN | connected | runs on every market book | simulated (no Betfair call) | polled (no-op for sim bets) | **lifted** — evaluates every OPEN market on first sight |
| LIVE | connected | runs on every market book | real `place_orders` call | polled every 5 min | enforced — only evaluates inside `process_window_mins` of off |

`STOP` is always safe — it closes the stream, halts evaluation, leaves
open Betfair positions untouched (they settle naturally via Betfair).

---

## Known gaps (track on these before scaling exposure)

1. **Steam Gate** & **Band Performance** signals require session
   history CLE V2 doesn't yet collect. They silently no-op rather
   than function. Leave both OFF until wired.
2. **No backtest harness inside CLE V2 yet.** Tuning is happening in
   the legacy `charles-ascot/lay-engine` backtest UI. Strategy modules
   are no longer byte-identical between the two (independent 3A/3B
   stakes added to CLE V2 only) — port if needed.
3. **Pre-existing bets without local context** (e.g. errored placements
   from before the response-parsing fix) will settle on Betfair with
   correct P&L, but their `SettledBet` row in the daily JSON will show
   blank `market_name` / `venue` / `runner_name`.

### Recently closed

- ✅ Settings persistence (gs://chiops-clev2-trading/settings/current.json)
- ✅ Visible SAVE confirmation (transient "✓ SAVED HH:MM:SS" pill in the panel)
- ✅ Bucket switch from `chiops-fsu100-results/track1/` to `chiops-clev2-trading/`
- ✅ Permanent per-market placement guard (no duplicate bets on mode toggle)
- ✅ `place_orders` response parsing fix (correct `place_instruction_reports` attribute)
- ✅ Independent Rule 3A / 3B stakes

---

## Why this exists

Mark's strategy is a serious, well-thought-out lay-the-favourite system
with multiple risk-management layers. The previous engine
(`charles-ascot/lay-engine`) had architectural problems that made it
hard to trust live results — backtest and live applied the same
strategy modules differently, settings flowed through different paths,
and the polled data source missed price moves at decision time.

CLE V2 takes the same strategy modules and runs them through a
**single evaluator** against a **streaming data feed**. Tighter,
cleaner, smaller surface. Built to be operated by one person, not
maintained by a team.

If you're tempted to add a wrapper, a framework, or an abstraction —
read it twice and probably don't.
