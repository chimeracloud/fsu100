# Changelog — FSU100 Chimera Betting Engine

All notable changes to the live Betfair lay engine. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
semantic ([SemVer](https://semver.org/)) — major bumps reserve for
breaking changes to the operator-visible state model or HTTP contract.

The engine reports its running version on `GET /admin/status`.

---

## [Unreleased]

### Added

- `chimera_may2026_v1` plugin now loads cleanly after the schema
  loosening in `1.1.1`. The portal Configurator and the Lay Engine
  PluginCard render its full ruleset (Rule 1, 2a–c, 3A, 3B) and every
  control toggle.

### Pending wire-up

The `chimera_may2026_v1` plugin declares several controls the evaluator
does not yet read. They round-trip through the schema and persist on
the plugin (audit-safe) but have no effect on placement until each is
wired into `evaluator.py`:

- `mark_ceiling_enabled` / `mark_floor_enabled` / `mark_uplift_enabled`
  — currently enforced unconditionally via the existing `hard_ceiling`,
  `hard_floor`, and `mark_uplift` controls. The new `*_enabled` toggles
  are observed but not consulted.
- `signal_overround` / `signal_field_size` / `signal_steam_gate` /
  `signal_band_perf` / `market_overlay_modifier` — flagged on the
  configurator but not connected to any pre-bet gate yet.
- `top2_concentration_enabled` and the `top2_06_*` / `top2_07_*` /
  `top2_08_*` blocks — not yet integrated with the rule selector.

### Known follow-ups

- Per-engine service account + dedicated `betfair-lay-fsu100-creds`
  bundle (per platform brief §6). The engine currently runs as the
  default Compute SA on shared `betfair-*` secrets.
- `/api/account` and `/api/results/summary` should return a structured
  `STOPPED` payload (200) rather than a 5xx when the engine has
  not yet finished its first authentication on a cold start.

---

## [1.1.1] — 2026-05-08

### Fixed

- **Plugin loader rejected operator metadata.** `PluginConfig` and
  `StakingConfig` had `extra="forbid"`, so any plugin that carried
  `author` / `sport` / `compatible_tools` at the top level — or a
  `staking._meta` block declaring per-variable bounds for the
  Configurator — failed validation and never made it into the
  registry. The full Mark ruleset was on disk but the engine only
  loaded the older 4-rule plugin. Both schemas now allow extras (same
  pattern already applied to `StrategyControls` and `StrategyRule`);
  required fields are still validated. ([`98a7e45`](https://github.com/chimeracloud/fsu100/commit/98a7e45))

---

## [1.1.0] — 2026-05-04

### Added

- **Runner names from `list_market_catalogue`.** Streaming market
  definitions don't include human-readable runner names for live
  data, so every runner used to surface as `selection_98071709`. The
  engine now fires a one-shot `list_market_catalogue` per market on
  first sight and caches the names. ([`7d3c91d`](https://github.com/chimeracloud/fsu100/commit/7d3c91d))
- **Top-3 back/lay ladder per runner.** `RunnerSnapshot` now carries
  `back_ladder` and `lay_ladder` arrays (price + size for each rung,
  best-first per Betfair convention). The portal renders the
  Betfair-style `3 BACK | BSP | 3 LAY` grid in the market detail view.
  ([`239ca67`](https://github.com/chimeracloud/fsu100/commit/239ca67))

---

## [1.0.1] — 2026-05-04

### Fixed

- **`/api/markets` 500 — naive vs aware datetime.** `market_time`
  arrived from `betfairlightweight` as a naive UTC datetime; the
  service compared it against an aware `datetime.now(timezone.utc)`,
  raising `TypeError: can't subtract offset-naive and offset-aware
  datetimes` on every request once the cache had any market.
  Coerce naive values to aware UTC before subtraction.
  ([`2656094`](https://github.com/chimeracloud/fsu100/commit/2656094))
- **`/api/markets` 500 — NaN in price fields.** Betfair encodes "no
  projection" as `float('nan')` rather than `None` for SP fields;
  the JSON encoder rejected NaN and the response 500'd. Strip NaN
  and ±inf from every price/size field before serialisation.
  ([`e17e71d`](https://github.com/chimeracloud/fsu100/commit/e17e71d))

---

## [1.0.0] — 2026-05-04

### Changed

**Always-on stream + four independent behaviour flags.** Replaces the
single `EngineMode` enum (`STOPPED` / `DRY_RUN` / `LIVE`) with four
orthogonal toggles describing what the engine does, on top of an
always-on Betfair stream. ([`8483130`](https://github.com/chimeracloud/fsu100/commit/8483130))

- The Betfair session is established on container boot and stays open
  regardless of operator intent — markets populate the cache and
  `/api/markets` serves data from the first request, not after a
  manual `START`.
- The four flags (`auto_betting`, `manual_betting`, `dry_run`,
  `recording`) are independent. Every combination is valid; `dry_run`
  is an override that simulates rather than transmits any bet that
  would otherwise fire.
- Legacy `EngineMode` is now derived from the flags and exposed on
  `/admin/status` for backwards compatibility:
  - `STOPPED` — no flag is on
  - `DRY_RUN` — `dry_run` is on
  - `LIVE` — at least one of `auto_betting` / `manual_betting` is on,
    with `dry_run` off
- Emergency stop now cancels every open order and forces all four
  flags off **without** tearing the stream down — the operator
  resumes from a clean state without losing market visibility.
- Variable patches (live tuning) are blocked while either betting
  flag is on; `dry_run` and `recording` do not lock.

### Added

- `PUT /admin/control/flags/{flag}` — flip one flag at a time, body
  is `{"enabled": bool}`. Returns the updated flag set and the
  derived legacy mode.
- `flag_changed` SSE event carries `{flag, previous, current, flags,
  mode}` so the portal can keep the four pills in sync without
  re-polling.
- `EngineFlags` block on `/admin/status`.

### Compatibility

- `POST /admin/control/{action}` with `start` / `stop` / `dry-run` /
  `emergency-stop` / `reset-stats` is preserved as a flag-flip shim
  so existing automation continues to work.

---

## [0.3.0] — 2026-05-04

### Added

- **Daily spend cap** (`AdminConfig.daily_max_stake_enabled` /
  `daily_max_stake`). Server enforces a hard ceiling on cumulative
  stake placed by the engine in a single trading day; the engine
  refuses any bet that would push `stats.total_stake` over the cap.
  Auto-resets at 00:00 UTC. ([`334dbf3`](https://github.com/chimeracloud/fsu100/commit/334dbf3))
- **Kill switch** — `POST /admin/control/emergency-stop` issues a
  Betfair-side `cancelOrders` across every market the engine has
  ever touched, then tears the session down. Distinct from `STOP`
  (orderly): unmatched orders cannot lapse to fill while shutting
  down. ([`2c492ad`](https://github.com/chimeracloud/fsu100/commit/2c492ad))

---

## [0.2.0] — 2026-05-02 → 2026-05-03

### Added

- **Plugin override hydration.** On startup the engine loads any
  per-plugin variable overrides the operator persisted in a previous
  session from `gs://chiops-fsu100-results/overrides/<plugin>.json`
  so live tunings survive container restarts. ([`e5f9792`](https://github.com/chimeracloud/fsu100/commit/e5f9792))
- **Per-variable APPLY endpoint** for the Lay Engine PluginCard.
  Operators tune individual variables (stakes, spreads, gaps) within
  the bounds declared by each variable's `_meta` block; changes are
  persisted to GCS and audit-logged with timestamp, actor, before
  and after values. ([`ae247ff`](https://github.com/chimeracloud/fsu100/commit/ae247ff))
- **Per-runner BSP + best back/lay** on `MarketView` — the portal's
  active markets list now shows top-of-book and projected SP for
  every runner without a second API call. ([`2464e67`](https://github.com/chimeracloud/fsu100/commit/2464e67))
- **`GET /admin/credentials/status`** — reports which secrets in the
  engine's bundle are configured. Status only; secret values are
  never read into the response. ([`ef5a89e`](https://github.com/chimeracloud/fsu100/commit/ef5a89e))
- **`chimera_may2026_v1` plugin** bundled — Mark's full 6-rule
  ruleset with `_meta` bounds for every tunable. ([`5259a9e`](https://github.com/chimeracloud/fsu100/commit/5259a9e))
- **CORS middleware** allowlisting `chimerasportstrading.com` and
  `www.chimerasportstrading.com`. ([`c8d9bdb`](https://github.com/chimeracloud/fsu100/commit/c8d9bdb))

---

## [0.1.0] — 2026-05-02

### Added

- **Initial release of FSU100** — Chimera live Betfair lay engine.
  FastAPI service on Cloud Run, betfairlightweight stream, JSON-
  driven strategy plugins, audit log to GCS, three endpoint sets
  (`/admin`, GUI, content). ([`5f2220d`](https://github.com/chimeracloud/fsu100/commit/5f2220d))

### Fixed

- Container build was missing `engine.py` — the Dockerfile copied
  routes and models but not the orchestrator module. ([`e62d009`](https://github.com/chimeracloud/fsu100/commit/e62d009))
