# Changelog — FSU100 Chimera Betting Engine

All notable changes to the live Betfair lay engine. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
semantic ([SemVer](https://semver.org/)) — major bumps reserve for
breaking changes to the operator-visible state model or HTTP contract.

The engine reports its running version on `GET /admin/status`.

---

## [Unreleased]

### Pending wire-up

- **Layered plugin pipeline.** Plugin roles (`rule` / `control` /
  `modifier`) are first-class on the schema and recognised by the
  Acceptor as of `1.2.0`, but the engine still pipelines every active
  plugin as a rule plugin in parallel. Controls (`cluster_concentration_v1`,
  `three_horse_cluster_guardrail`) need to run *before* rules to gate
  their bets; modifiers (`meeting_streak_dampener_v1`) need to run
  *after* rules to scale stake. Until this lands, mixing roles in
  `AdminConfig.active_plugins` produces duplicate bets — use the
  control plugin *instead of* the rule plugin as a workaround.
- **AI-on-API plugin generator.** Operator-facing endpoint that takes a
  natural-language brief plus `plugin_role` hint, calls a model, and
  returns Acceptor-validated JSON ready to save. Spec is being written
  by Claude Strategy.
- `chimera_may2026_v1` outstanding wire-ups (carried forward from
  `1.1.1`):
  - `mark_ceiling_enabled` / `mark_floor_enabled` / `mark_uplift_enabled`
    — currently enforced unconditionally via the existing controls; the
    new `*_enabled` toggles are observed but not consulted.
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
- Backtest worker durability — move from in-process to Cloud Tasks /
  Cloud Run Jobs so an in-flight backtest survives a deploy.

---

## [1.2.0] — 2026-05-12

### Added

- **Plugin Acceptor — `core/plugin_normaliser.py`.** Every plugin JSON
  arriving via `PUT /api/strategies/{name}` is now normalised before
  Pydantic validation. The Acceptor applies a fixed set of repair rules
  documented in `Chimera_Plugin_Template.json` v2.0 and returns the
  repair log in the response so the portal can surface what was
  auto-fixed:
  - Rewrites known wrong key names: `plugin_name` → `name`,
    `plugin_version` → `version`, `plugin_type` → `plugin_role`,
    `purpose` → `description`.
  - Hoists a top-level `rules` array under `strategy.rules`
    automatically (or under `strategy.guardrail_rules` when the plugin
    is a control / modifier and the rules lack `odds_band`).
  - Injects a `noop_passthrough` rule on `plugin_role: "control"` /
    `"modifier"` plugins missing rules — satisfies the schema's
    `min_length=1` without inventing a real bet.
  - Coerces malformed `odds_band` shapes — `{min, max}` objects,
    string-encoded numbers, inverted tuples, sub-1.01 lower bounds.
  - Parks rules with no `base_stake` or `stake`
    (`base_stake: 0, enabled: false`) rather than 422'ing.
  - Fills documented defaults for every optional field (`parser`,
    `staking.point_value`, `compatible_tools`, `author`, `sport`,
    `plugin_role`). Missing `name` is synthesised from author +
    description + epoch minutes or taken from the URL hint.
  - 28-test suite (`tests/test_plugin_normaliser.py`) covers every
    repair rule and includes an end-to-end test that Mark's
    ChatGPT-authored `three_horse_cluster` plugin (3 schema errors
    under the previous strict body) now loads cleanly with 5 repairs
    logged. ([`5c475f3`](https://github.com/chimeracloud/fsu100/commit/5c475f3))
- **Multi-plugin auto-betting.** `AdminConfig.active_plugins: list[str]`
  lets the operator run multiple plugins simultaneously. Legacy
  `active_plugin` (single string) is preserved as the head-of-list view
  for portal backwards compatibility. Each fired bet carries
  `plugin_name` so the portal can attribute decisions back to their
  source. ([`fcbb2d8`](https://github.com/chimeracloud/fsu100/commit/fcbb2d8))
- **`PUT` and `DELETE /api/strategies/{name}`.** Full plugin CRUD.
  PUT writes the validated JSON to GCS at
  `gs://chiops-fsu100-results/strategies/<name>.json` and updates the
  in-memory plugin store; DELETE removes both. Both are mode-locked —
  409 while either betting flag is on. The Strategy page in the portal
  was rewritten on top of these endpoints (rename from "Configurator",
  inline name / version / description editors, plugin list panel with
  edit / download / delete per row, LOAD PLUGIN file picker for
  external JSON). ([`bd392cd`](https://github.com/chimeracloud/fsu100/commit/bd392cd))
- **Settled-bets hydration on startup.** Engine reads the daily settled
  JSONL from GCS at boot and repopulates the in-memory `_recent_settled`
  ring buffer. Fixes settled bets disappearing from the portal after a
  Cloud Run cold start. ([`f13cd0d`](https://github.com/chimeracloud/fsu100/commit/f13cd0d))
- **Market status + winner on `MarketView`.** `status` (Betfair
  `market_definition.status` — `OPEN` / `SUSPENDED` / `CLOSED`) and
  `winner_selection_id` (populated when the market closes with a
  `WINNER` flag) are exposed on `/api/markets`. The portal uses these
  to render CLOSED banners and infer WON / LOST outcomes inline per
  row. `bet_placed` SSE events now also fire in `dry_run` mode
  (previously suppressed). ([`7638655`](https://github.com/chimeracloud/fsu100/commit/7638655))
- **Bundled plugin — `cluster_concentration_v1`.** Mark's 4-runner
  cluster suppression. Detects markets where 3–5 runners are tightly
  grouped at the top of the field and shifts the engine into
  cluster-mode behaviour. Authored as `plugin_role: "control"`.
  ([`5f74d54`](https://github.com/chimeracloud/fsu100/commit/5f74d54))
- **Bundled plugin — `meeting_streak_dampener_v1`.** Mark's stake
  modifier. When the first two consecutive races at a meeting are both
  won by the pre-off favourite, stake on every remaining race at that
  meeting is multiplied by 0.75× (acceptable range `[0.70, 0.75]`
  preserved for calibration sweeps). `plugin_role: "modifier"`.
  Loads and validates today; actual stake-dampening activates once the
  layered pipeline lands. ([`6b985c5`](https://github.com/chimeracloud/fsu100/commit/6b985c5))

### Changed

- **`PluginConfig.source` is now optional.** FSU100 never consults the
  historic `source` block (it always streams live from Betfair), and
  the country / market-type filters that used to live under
  `source.filters` are driven by `AdminConfig.countries` /
  `AdminConfig.market_types`. Plugins authored with no `source` block
  now load. ([`cb83e1b`](https://github.com/chimeracloud/fsu100/commit/cb83e1b))
- **`PluginConfig` and `StrategyConfig` allow extras.** Same
  `extra="allow"` relaxation already applied to `StrategyControls`,
  `StrategyRule`, and `StakingConfig` in `1.1.1` now extends to the
  top-level plugin shape and the strategy block. Plugins can carry
  sport-specific or experimental blocks (`cluster_detection`,
  `cluster_rules`, `modifier_logic`, `control_logic`, `_meta` bounds)
  alongside the canonical `rules` / `controls` pair without code churn.
  ([`5f74d54`](https://github.com/chimeracloud/fsu100/commit/5f74d54))
- **`PUT /api/strategies/{name}` response shape.** The endpoint now
  returns the `StrategyInfo` fields *plus* an `acceptor_repairs: list[str]`
  describing every auto-fix the normaliser applied. Clients reading
  only the legacy `StrategyInfo` fields are unaffected. ([`5c475f3`](https://github.com/chimeracloud/fsu100/commit/5c475f3))

### Schema additions (operator-visible, non-breaking)

- `AdminConfig.active_plugins: list[str]`
- `AdminStatus.active_plugins: list[str]`
- `MarketView.status: str | None`
- `MarketView.winner_selection_id: int | None`
- `plugin_role: "rule" | "control" | "modifier"` is recognised as a
  top-level plugin field via the schema's `extra="allow"`. Default
  filled by the Acceptor when absent is `"rule"`.

---

## [1.1.1] — 2026-05-08

### Added

- `chimera_may2026_v1` plugin now loads cleanly after the schema
  loosening below. The portal Configurator and the Lay Engine
  PluginCard render its full ruleset (Rule 1, 2a–c, 3A, 3B) and every
  control toggle.

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
