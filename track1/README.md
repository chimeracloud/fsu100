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

## Strategic position (read this first)

CLE V2 is **not** the primary profit centre. It's the **discipline machine**.

Modern UK racing markets are too efficient for lay-the-favourite to be a
standalone business. A 30-day backtest of the production strategy
delivered +0.9% ROI — statistically positive but not enough margin to
justify scaling exposure. Academic literature (Cain/Law/Peel 2000;
Smith/Williams 2019) confirms the favourite-longshot bias has been
substantially eroded by algorithmic pricing over the past decade.

CLE V2's role going forward:
- **Capital preservation** through disciplined sizing + multi-layer risk control
- **Audit-grade operational track record** — every decision traced,
  every bet recorded, every settlement reconciled
- **Daily small positive expectation** at low exposure — sized to
  generate operational data, not large P&L
- **Production-grade observability** — the same patterns reused for
  the arbitrage engine and future FSUs

The primary profit centre is the **Arbitrage Engine** (separate FSU,
implementation begins 2026-05-15). See
`~/Downloads/Chimera_Arbitrage_Architecture_Plan.md` for that
specification. CLE V2 and the arb engine run in parallel; revenue
diversification across two structurally different strategy classes.

---

## Operating principles

1. **One evaluator function — `evaluator.evaluate(snapshot, settings)`.**
   Same code path for live and (future) backtest. Cannot drift, because
   there is only one function. Settings flow through unchanged.
2. **betfairlightweight used as-is.** Auth, streaming, market cache,
   placement, settlement — direct calls. No wrappers, no framework.
3. **Three orthogonal modes:** `STOPPED` (default on boot — including
   after every redeploy or restart), `DRY_RUN` (everything except
   real bet placement), `LIVE` (real money).
4. **Permanent per-market placement guard.** In LIVE mode, a market
   can never be bet on twice in a session, regardless of mode toggling.
   The guard is set BEFORE the Betfair HTTP call so even a mid-call
   exception cannot leave a market open to re-betting.
5. **Post-placement verification.** After every successful
   `place_orders`, the engine immediately calls `list_current_orders`
   to confirm Betfair has the order. Unverified bets fire an error
   event and surface on the status panel.
6. **Cross-source market integrity check.** The engine pulls today's
   GB/IE WIN market count from Betfair's catalogue every 5 minutes
   and compares to its own evaluated-market count. Divergence > 2
   surfaces a `⚠ MARKETS MISMATCH` warning on the portal.

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
| `engine.py` | betfairlightweight glue — auth, stream, drain, place, settle, verify, catalogue counts. |
| `gcs.py` | TradingStore — all GCS persistence (daily, settled, errors, snapshots, markets, settings). |
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
10  Place + verify             engine._place_real → list_current_orders
11  Settlement & P&L           engine._poll_settlement
```

---

## Default settings (cold boot — fallback if no persisted file)

These are the dataclass defaults. They apply only on the first ever boot
or when the persisted settings file fails validation. Once an operator
hits SAVE in the Bet Settings panel, those values become the persisted
defaults across all subsequent restarts.

| Group | Field | Default |
|---|---|---|
| General | `point_value` | £1 / pt |
| General | `countries` | GB, IE |
| General | `process_window_mins` | 5 |
| General | `mode` | `STOPPED` (always — see safety note below) |
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

### Safety: mode is always STOPPED on boot

The persisted settings file carries `general.mode`, but the lifespan
handler **forces it to STOPPED on every container start**, regardless
of the persisted value. This means:

- Every redeploy / restart / scale event lands the engine in STOPPED
- Operator MUST explicitly hit START or GO LIVE after every boot
- Prevents the engine silently resuming mid-race after a redeploy

This patch landed 2026-05-14 after an incident where mid-session
redeploys were observed to retain LIVE mode in memory.

---

## Production-tuned settings (current operator profile)

This is the operator's actual saved configuration as of 2026-05-15.
Persisted at `gs://chiops-clev2-trading/settings/current.json`.
Diverges from the defaults above based on the May 2026 strategy review
and AI-report-driven calibration.

| Group | Field | Production value | Default | Δ |
|---|---|---|---|---|
| General | `point_value` | £10 / pt | £1 / pt | × 10 |
| Rules | Rule 1 stake | 2 pts | 3 pts | reduced — Rule 1 is the highest-frequency lowest-edge band |
| Rules | Rule 2A stake | 0 (skip) | 0 (skip) | unchanged — short-priced 2.0–3.0 band has thin edge |
| Rules | Rule 2B stake | 2 pts | 1 pt | pushed — AI report identifies 3.0–5.0 as the strategy's sweet spot |
| Rules | Rule 2C stake | 2 pts | 2 pts | unchanged |
| Rules | Rule 3A stake | 1 pt | 1 pt | unchanged — correlated 2-bet risk |
| Rules | Rule 3B stake | 1.5 pts | 1 pt | pushed — single-bet variant, cleaner edge |
| Rules | `rule3_gap_threshold` | 1.5 | 2.0 | tightened — reduces Rule 3A frequency |
| Controls | Mark Ceiling | ON, 8.0 | OFF | hard cap on Rule 3 territory |
| Controls | Mark Floor | ON, 1.5 | OFF | blocks sub-1.5 favourites (near-zero edge) |
| Signals | Overround | ON, soft 1.15 / hard 1.20 | OFF | market-integrity check |
| Signals | Field Size | ON, max 10 / odds ≥ 3.0 / cap £10 | OFF | variance protection on big NH fields |
| Risk Overlay | TOP2 Concentration | ON | OFF | blocks two-horse races |

Settings persist via `PUT /admin/config` with confirmed GCS write.
Response includes `persisted: true` only after successful write. The
portal surfaces a "✓ SAVED HH:MM:SS" indicator next to the SAVE button
once persistence is confirmed.

---

## Endpoint surface

```
GET  /admin/status                          mode, stream, counters, balance, exposure,
                                            betfair_markets_today, chimera_markets_today,
                                            bets_placed, bets_verified, bets_unverified
GET  /admin/config                          current Settings as JSON
PUT  /admin/config                          replace Settings (GCS-confirmed write or 500)
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
| `daily/{YYYY-MM-DD}.json` | every evaluation, placement, settlement | Full timeline of the day's session |
| `settled/{YYYY-MM-DD}.json` | every Betfair-confirmed settlement | Subset of daily — only `list_cleared_orders` confirmations |
| `errors/{YYYY-MM-DD}.json` | every error event from `_push_event("error", ...)` | Post-session error log |
| `snapshots/{YYYY-MM-DD}_start.json` | first STOPPED → DRY_RUN/LIVE transition | Engine status at session start |
| `snapshots/{YYYY-MM-DD}_end.json` | active → STOPPED transition | Engine status at session end |
| `markets/{YYYY-MM-DD}_catalogue.json` | flushed on session end | Per-market runner names + race_time |
| `settings/current.json` | every successful `PUT /admin/config` | Latest operator settings, single file, latest-wins |

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

## Portal layout (CST)

Operator surface lives at `chimerasportstrading.com/betfair/horse-racing`.
Frontend: `chimeracloud/cst` (React, deployed via Cloudflare Pages).
The portal communicates with the engine via the cst-api proxy
(`/api/proxy/fsu100-track1/...`).

### Sections (top → bottom)

1. **ENGINE STATUS panel** — merged status + daily summary. Controls
   (START / GO LIVE / STOP) in the top-right corner. 8-column grid of
   tiles:
   - MODE (STOPPED / DRY_RUN / LIVE)
   - STREAM (CONNECTED / DISCONNECTED)
   - MARKETS (split tile: Betfair count vs Chimera count — flags
     mismatch in red)
   - BETS (split tile: Local count vs Verified count — gold when
     mismatched)
   - STRIKE rate
   - WINS/LOSS ratio
   - P&L (today)
   - ROI
   - STAKE TOTAL
   - LIABILITY
   - VOID
   - SKIP 2A count
   - BALANCE
   - EXPOSURE
   - UNVERIFIED (only appears if count > 0)
2. **LIVE FEED panel** — real-time event stream with three filter
   toggles (LAY / DRY / SKIP). Always-visible outcomes:
   PLACED / WON / LOST / VOID / ERROR.
3. **PLACEMENTS panel** — today's placements table. MARKET column
   shows venue + race time (HH:MM) on top, market_id as small mono
   sub-line. RUNNER / RULE / PRICE / STAKE / LIABILITY / BET_ID /
   TYPE columns. TYPE distinguishes DRY (simulated) vs LIVE (real).
4. **BET SETTINGS panel** (collapsible) — five-group form:
   GENERAL / BASE RULES / CONTROLS / SIGNALS / RISK OVERLAY.
   Save persists to GCS with "✓ SAVED HH:MM:SS" confirmation.

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
| LIVE | connected | runs on every market book | real `place_orders` + verify call | polled every 5 min | enforced — only evaluates inside `process_window_mins` of off |

`STOP` is always safe — it closes the stream, halts evaluation, leaves
open Betfair positions untouched (they settle naturally via Betfair).

---

## First live production session — 2026-05-14

The engine ran its first LIVE session under the production-tuned
settings on 2026-05-14. Headline numbers:

| Metric | Value |
|---|---|
| Settled bets | 35 |
| Wins | 27 |
| Losses | 8 |
| Voids | 0 |
| Strike rate | 77.1% |
| Total P&L | +£152.12 |

Operational notes from the session:
- An early-session duplicate-placement incident (Rule 3A fired twice on
  the same Punchestown 19:50 market, leading to four total lay bets
  where two were expected). Root cause: `_placed_markets` was being
  added to AFTER the HTTP call, leaving a race window where an
  in-flight exception could leave the market open to re-betting. Fixed
  same day with the pre-HTTP marking patch.
- A `place_orders` response-parsing bug surfaced the same morning —
  bets were placed on Betfair but local tracking failed because the
  code read `instruction_reports` instead of `place_instruction_reports`.
  Fixed; defensive fallback added to read either attribute.
- Cloud Build redeployed the service mid-session four times (each
  bug-fix push triggered a deploy). The new "boot in STOPPED" safety
  patch landed at end of session — future mid-session deploys will
  require explicit START on the new container.
- Operator-side cash-out via Betfair UI was used to manage the
  duplicate exposure. Net position remained profitable.

77% strike rate is meaningfully above the 67–70% break-even for laying
favourites at these stakes. **One session is not a statistical proof**
— the strategy needs 10+ sessions before any of these numbers carry
weight. But the directional result is consistent with the strategy
review's predictions.

---

## Known gaps (track before scaling exposure)

1. **Steam Gate** & **Band Performance** signals require session
   history CLE V2 doesn't yet collect. They silently no-op rather
   than function. Leave both OFF until wired.
2. **No backtest harness inside CLE V2.** Tuning currently happens in
   the legacy `charles-ascot/lay-engine` backtest UI. Strategy modules
   are no longer byte-identical between the two (independent 3A/3B
   stakes added to CLE V2 only) — port if needed.
3. **Mark Uplift** (2.5–3.5 stake override) — code present but OFF.
   No published data justifies the band-specific override; needs
   per-band backtest evidence before enabling.
4. **Pre-existing bets without local context** (e.g. errored placements
   from before the response-parsing fix on 2026-05-14) will settle on
   Betfair with correct P&L, but their `SettledBet` row in the daily
   JSON will show blank `market_name` / `venue` / `runner_name`.

### Recently closed (since 2026-05-13)

- ✅ **Settings persistence** to `gs://chiops-clev2-trading/settings/current.json` with confirmed GCS write
- ✅ **Visible SAVE confirmation** ("✓ SAVED HH:MM:SS" pill)
- ✅ **Bucket switch** from `chiops-fsu100-results/track1/` to `chiops-clev2-trading/`
- ✅ **Permanent per-market placement guard** (no duplicate bets on mode toggle)
- ✅ **`_placed_markets` marked BEFORE HTTP** (closes race window for in-flight exception)
- ✅ **Boot in STOPPED mode** regardless of persisted setting
- ✅ **`place_orders` response parsing fix** (correct `place_instruction_reports` attribute, defensive fallback)
- ✅ **Independent Rule 3A / 3B stakes**
- ✅ **Post-placement bet verification** via `list_current_orders`
- ✅ **Betfair markets-count cross-check** with `⚠ MARKETS MISMATCH` warning
- ✅ **Merged ENGINE STATUS + DAILY SUMMARY panel** with controls inline
- ✅ **LAY / DRY / SKIP filter toggles** on the Live Feed
- ✅ **Placements panel** moved to bottom, with venue + race time stacked over market_id
- ✅ **Runner names** via `list_market_catalogue` (replaces `selection_<id>` placeholders)
- ✅ **Race time as HH:MM** in feed (UTC display, race-time not event-time)
- ✅ **Naive→aware UTC coercion** on `market_time` (fixes datetime subtraction TypeError)
- ✅ **Datetime-filter MARKETS TODAY counter** (excludes tomorrow's races from today's count)

---

## Architectural relationship to the broader Chimera stack

CLE V2 is one of several Fractional Service Units (FSUs) in the Chimera
production stack. Each FSU has a single responsibility and a clean API
boundary. Relevant siblings:

| FSU | Role |
|---|---|
| **FSU1X** (`charles-ascot/beta-fsu1x`) | The Odds API wrapper — normalised odds across 40+ bookmakers, 70+ sports |
| **FSU1Y** (`charles-ascot/beta-fsu1y`) | The Racing API wrapper — UK/IE/HK racing, 20+ bookie odds per runner |
| **FSU100 / CLE V2** (this engine) | Betfair lay engine for UK/IE horse racing WIN markets |
| **Arbitrage Engine** (planned, scaffold begins 2026-05-15) | Distributed-agent arb detection + execution — see architectural plan |
| **cst-api** | Portal API proxy — JWT auth, routes browser → FSU services |
| **CST** (`chimeracloud/cst`) | Operator portal — React + Cloudflare Pages |

CLE V2 deliberately does not depend on FSU1X or FSU1Y (it streams
directly from Betfair). The arbitrage engine WILL consume both
FSU1X / FSU1Y feeds plus CLE V2's Betfair stream as price sources.

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

The strategy itself, after objective review, is structurally sound but
not a primary profit centre at retail scale. CLE V2 will continue to
operate as the **discipline machine** while Chimera builds the
arbitrage engine as the primary revenue driver. Two products,
diversified across structurally different strategy classes.

If you're tempted to add a wrapper, a framework, or an abstraction —
read it twice and probably don't.
