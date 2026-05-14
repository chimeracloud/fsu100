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

**Operating gotcha — settings do not persist across redeploy yet.** A
Cloud Run cold start reverts every setting to the defaults above. This
is on the immediate fix list. Until then, re-apply any custom settings
after every deploy or container restart.

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

## Data storage — open for review

**Current state — single GCS bucket, single prefix.**

| What | Where |
|---|---|
| Daily results (evaluations + placements + settlements) | `gs://chiops-fsu100-results/track1/daily/{YYYY-MM-DD}.json` |
| Cloud Run application logs | Cloud Logging (project `chiops`, service `fsu100-track1`) |
| Settings | **in-memory only** (lost on redeploy or restart) |
| Placement context cache | **in-memory only**, rehydrated from today's daily JSON on boot |
| Runner-name catalogue cache | **in-memory only**, rebuilt on demand via `list_market_catalogue` |
| Betfair credentials | Secret Manager (`chiops` project) — `betfair-{username, password, app-key, cert-pem, key-pem}` |

**Decisions pending before CLE V2 production starts:**

1. **Bucket prefix.** Current `track1/` reflects the Track-1 branding from
   the initial brief. Options to consolidate under CLE V2:
   - Keep current: `gs://chiops-fsu100-results/track1/...` (zero migration cost)
   - Rename prefix: `gs://chiops-fsu100-results/clev2/...` (cosmetic, requires code patch + Cloud Build rebuild)
   - New bucket: `gs://chiops-cle-v2-results/...` (cleanest separation; needs SA permissions + lifecycle policy)
2. **Settings persistence.** Should land before any other repair work
   so operator tunings survive deploys. Proposed location:
   `gs://chiops-fsu100-results/clev2/settings/current.json`. Persist
   on every `PUT /admin/config`; hydrate on lifespan startup.
3. **Audit log of bet placements.** Currently embedded in the daily JSON;
   may want a separate immutable append-only log for compliance, e.g.
   `gs://chiops-fsu100-results/clev2/audit/{date}.jsonl`.

Charles will confirm location strategy before the next round of changes.
Until then the engine continues writing to `track1/daily/`.

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

1. **Settings persistence.** Top priority. Currently in-memory only.
2. **Visible SAVE confirmation.** UI shows the save attempt but doesn't
   confirm round-trip success; operators have been bitten by silent
   reverts on refresh.
3. **Steam Gate** & **Band Performance** signals require session
   history Track 1 doesn't yet collect. They silently no-op rather
   than function. Leave both OFF until wired.
4. **No backtest harness inside CLE V2 yet.** Tuning is happening in
   the legacy `charles-ascot/lay-engine` backtest UI. Strategy modules
   are no longer byte-identical between the two (independent 3A/3B
   stakes added to CLE V2 only) — port if needed.
5. **Pre-existing bets without local context** (e.g. errored placements
   from before the response-parsing fix) will settle on Betfair with
   correct P&L, but their `SettledBet` row in the daily JSON will show
   blank `market_name` / `venue` / `runner_name`.

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
