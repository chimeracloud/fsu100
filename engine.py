"""Live betting engine — the heart of FSU100.

Manages the Betfair streaming session, applies the active strategy plugin
to incoming market updates, and (when the appropriate behaviour flag is
on) places bets on the exchange. Also tracks open orders, settles cleared
bets, and persists daily results to GCS.

State model
-----------
Operator behaviour is described by four independent boolean flags:

* ``auto_betting`` — engine fires bets autonomously from streamed market
  updates.
* ``manual_betting`` — operators may place bets via ``POST /api/place``.
* ``dry_run`` — both auto and manual bets are simulated, never sent to
  Betfair. Stats and audit log still record the would-be bet.
* ``recording`` — raw market change messages are persisted to GCS for
  later replay by the backtest tool.

The Betfair stream is **always-on**: the engine logs in and connects on
boot and reconnects automatically on failure, regardless of flag state.
Markets always populate the cache so the portal can render them as soon
as the page loads.

The legacy :class:`EngineMode` enum is derived from the flags:

* ``LIVE``    — at least one of ``auto_betting`` / ``manual_betting`` is
  on, and ``dry_run`` is off.
* ``DRY_RUN`` — ``dry_run`` is on (regardless of betting toggles).
* ``STOPPED`` — none of the four flags is on.

Threading model
---------------
The engine combines async (FastAPI / event bus) with sync (Betfair stream
socket, periodic orders/settlement pollers). Mutable state lives behind
``threading.RLock`` so the streaming worker thread and asyncio tasks can
read/write it safely. Sync code publishes events back to the async bus via
:meth:`core.events.EventBus.publish_threadsafe`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from core.config import AppSettings, get_settings
from core.events import EventBus
from core.logging import get_logger
from core.plugin_store import PluginNotFoundError, PluginStore
from evaluator import evaluate
from models.decisions import BetDecision, NoBet, Side
from models.schemas import (
    AccountResponse,
    AdminConfig,
    AdminStats,
    AdminStatus,
    BetDecisionView,
    EngineFlags,
    EngineMode,
    FlagName,
    HistoryResponse,
    MarketsResponse,
    MarketView,
    PluginConfig,
    PositionView,
    PositionsResponse,
    PriceSize,
    ResultsResponse,
    RunnerSnapshot,
    SettledBet,
    SettledResponse,
    StreamStatus,
)
from services.betfair_service import BetfairService, BetfairServiceError
from services.gcs_service import GcsService, parse_jsonl

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class EngineRuntimeConfig:
    """Operator-configurable runtime parameters held in memory.

    Distinct from :class:`core.config.AppSettings` (process-level, mostly
    immutable). This is the block edited via ``PUT /admin/config`` and read
    by the engine on every market update.
    """

    log_level: str
    activity_log_size: int
    results_bucket: str
    active_plugin: str
    countries: list[str]
    market_types: list[str]
    point_value: float
    customer_strategy_ref: str
    daily_max_stake_enabled: bool = False
    daily_max_stake: float = 0.0

    def to_admin_config(self) -> AdminConfig:
        """Produce the :class:`AdminConfig` view served by the admin API."""

        return AdminConfig(
            log_level=self.log_level,  # type: ignore[arg-type]
            activity_log_size=self.activity_log_size,
            results_bucket=self.results_bucket,
            active_plugin=self.active_plugin,
            countries=list(self.countries),
            market_types=list(self.market_types),
            point_value=self.point_value,
            customer_strategy_ref=self.customer_strategy_ref,
            daily_max_stake_enabled=self.daily_max_stake_enabled,
            daily_max_stake=self.daily_max_stake,
        )


@dataclass
class _OpenOrder:
    """In-memory record of one currently-open Betfair order."""

    bet_id: str
    market_id: str
    selection_id: int
    runner_name: str
    side: str
    price: float
    stake: float
    liability: float
    rule_applied: str | None
    placed_at: datetime
    matched_size: float
    unmatched_size: float


@dataclass
class _SettledBetRecord:
    """In-memory record of one settled bet."""

    bet_id: str
    market_id: str
    selection_id: int
    runner_name: str
    side: str
    price: float
    stake: float
    liability: float
    rule_applied: str | None
    outcome: str
    pnl: float
    settled_at: datetime

    def to_view(self) -> SettledBet:
        return SettledBet(
            bet_id=self.bet_id,
            market_id=self.market_id,
            selection_id=self.selection_id,
            runner_name=self.runner_name,
            side=self.side,  # type: ignore[arg-type]
            price=self.price,
            stake=self.stake,
            liability=self.liability,
            rule_applied=self.rule_applied,
            outcome=self.outcome,  # type: ignore[arg-type]
            pnl=self.pnl,
            settled_at=self.settled_at,
        )


@dataclass
class _MarketCacheEntry:
    """Cached snapshot of the latest streamed ``MarketBook`` for one market.

    Runner names are sourced from the betting REST catalogue (the streaming
    MCM feed does not include them) and cached here keyed by selection_id.
    ``catalogue_fetched`` is set once we've made a catalogue call for the
    market — even if it failed — to avoid retry storms on inaccessible
    markets.
    """

    market_id: str
    venue: str | None
    country: str | None
    market_type: str | None
    market_time: datetime | None
    in_play: bool
    runners: list[RunnerSnapshot]
    last_update: datetime
    evaluated: bool = False
    # Betfair market_definition.status (OPEN/SUSPENDED/CLOSED/INACTIVE).
    # Populated from each book update so the portal can show CLOSED
    # once a race finishes.
    status: str | None = None
    # Set when the market closes and Betfair flags a winning runner.
    winner_selection_id: int | None = None
    runner_names: dict[int, str] = field(default_factory=dict)
    market_name: str | None = None
    event_name: str | None = None
    catalogue_fetched: bool = False


@dataclass
class _Stats:
    """Aggregated counters maintained for ``GET /admin/stats``."""

    bets_placed: int = 0
    bets_won: int = 0
    bets_lost: int = 0
    bets_void: int = 0
    bets_pending: int = 0
    markets_processed: int = 0
    total_stake: float = 0.0
    total_liability: float = 0.0
    total_pnl: float = 0.0
    open_exposure: float = 0.0
    stats_window_start: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def reset(self) -> None:
        """Zero every counter and reset the window start to ``now``."""

        self.bets_placed = 0
        self.bets_won = 0
        self.bets_lost = 0
        self.bets_void = 0
        self.bets_pending = 0
        self.markets_processed = 0
        self.total_stake = 0.0
        self.total_liability = 0.0
        self.total_pnl = 0.0
        self.open_exposure = 0.0
        self.stats_window_start = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Live engine
# ---------------------------------------------------------------------------


class LiveEngine:
    """Runs the live betting workflow.

    The engine is a long-lived singleton owned by the FastAPI lifespan. It
    reads strategy configuration from the supplied :class:`PluginStore`,
    talks to Betfair via :class:`BetfairService`, persists results via
    :class:`GcsService`, and emits lifecycle events via :class:`EventBus`.
    """

    _MAX_RECENT_SETTLED = 1_000

    def __init__(
        self,
        *,
        settings: AppSettings,
        plugins: PluginStore,
        betfair: BetfairService,
        gcs: GcsService,
        events: EventBus,
    ) -> None:
        self._settings = settings
        self._plugins = plugins
        self._betfair = betfair
        self._gcs = gcs
        self._events = events

        self._lock = threading.RLock()
        self._auto_betting = False
        self._manual_betting = False
        self._dry_run = False
        self._recording = False
        self._stream_status = StreamStatus.DISCONNECTED
        self._streaming_requested = False
        self._started_at: datetime | None = None

        default_plugin = self._resolve_default_plugin()
        # The live streaming filter (countries + market_types) is owned by
        # AdminConfig — plugins no longer pin a source. Seed from legacy
        # plugin.source.filters when present (older plugin JSONs), otherwise
        # start with broad GB/IE WIN defaults which the operator can refine
        # via PUT /admin/config.
        legacy_filters = default_plugin.source.filters if default_plugin.source else None
        seed_countries = list(legacy_filters.countries) if legacy_filters else ["GB", "IE"]
        seed_market_types = list(legacy_filters.market_types) if legacy_filters else ["WIN"]
        self._runtime_config = EngineRuntimeConfig(
            log_level=settings.log_level,
            activity_log_size=settings.activity_log_size,
            results_bucket=settings.results_bucket,
            active_plugin=default_plugin.name,
            countries=seed_countries,
            market_types=seed_market_types,
            point_value=default_plugin.staking.point_value,
            customer_strategy_ref=settings.customer_strategy_ref,
            daily_max_stake_enabled=False,
            daily_max_stake=0.0,
        )

        self._market_cache: dict[str, _MarketCacheEntry] = {}
        self._open_orders: dict[str, _OpenOrder] = {}
        self._recent_settled: list[_SettledBetRecord] = []
        self._known_settled_bet_ids: set[str] = set()
        self._stats = _Stats()

        self._processing_thread: threading.Thread | None = None
        self._processing_stop = threading.Event()
        self._poll_orders_task: asyncio.Task[None] | None = None
        self._poll_settlement_task: asyncio.Task[None] | None = None
        self._daily_reset_task: asyncio.Task[None] | None = None
        self._poll_catalogues_task: asyncio.Task[None] | None = None
        # market_id -> {selection_id: runner_name}, hydrated from
        # list_market_catalogue. Lookups are read-mostly under self._lock.
        self._runner_names: dict[str, dict[int, str]] = {}

    # ------------------------------------------------------------------
    # Status accessors
    # ------------------------------------------------------------------

    def flags(self) -> EngineFlags:
        """Return a snapshot of the four behaviour flags."""

        with self._lock:
            return EngineFlags(
                auto_betting=self._auto_betting,
                manual_betting=self._manual_betting,
                dry_run=self._dry_run,
                recording=self._recording,
            )

    @property
    def _mode(self) -> EngineMode:
        """Derive the legacy mode from the current flag combination.

        Held under the engine lock at every read site. Pure function of
        the four bools, kept as a property so the rest of the engine can
        continue to read ``self._mode`` without churn.
        """

        if self._dry_run:
            return EngineMode.DRY_RUN
        if self._auto_betting or self._manual_betting:
            return EngineMode.LIVE
        return EngineMode.STOPPED

    def status(self) -> AdminStatus:
        """Return the snapshot served by ``GET /admin/status``."""

        with self._lock:
            uptime = 0.0
            if self._started_at is not None:
                uptime = (
                    datetime.now(timezone.utc) - self._started_at
                ).total_seconds()
            try:
                active_plugin = self._plugins.get(self._runtime_config.active_plugin)
                plugin_version: str | None = active_plugin.version
                plugin_name: str | None = active_plugin.name
            except PluginNotFoundError:
                plugin_version = None
                plugin_name = None
            return AdminStatus(
                service=self._settings.service_name,
                version=self._settings.version,
                environment=self._settings.environment,
                uptime_seconds=uptime,
                timestamp=datetime.now(timezone.utc),
                mode=self._mode,
                flags=EngineFlags(
                    auto_betting=self._auto_betting,
                    manual_betting=self._manual_betting,
                    dry_run=self._dry_run,
                    recording=self._recording,
                ),
                active_plugin=plugin_name,
                active_plugin_version=plugin_version,
                stream_status=self._stream_status,
                markets_in_cache=len(self._market_cache),
            )

    def get_runtime_config(self) -> AdminConfig:
        with self._lock:
            return self._runtime_config.to_admin_config()

    def update_runtime_config(self, payload: AdminConfig) -> AdminConfig:
        """Apply ``PUT /admin/config`` and return the resulting config."""

        with self._lock:
            self._plugins.get(payload.active_plugin)
            self._runtime_config = EngineRuntimeConfig(
                log_level=payload.log_level,
                activity_log_size=payload.activity_log_size,
                results_bucket=payload.results_bucket,
                active_plugin=payload.active_plugin,
                countries=list(payload.countries),
                market_types=list(payload.market_types),
                point_value=payload.point_value,
                customer_strategy_ref=(
                    payload.customer_strategy_ref
                    or self._settings.customer_strategy_ref
                ),
                daily_max_stake_enabled=payload.daily_max_stake_enabled,
                daily_max_stake=payload.daily_max_stake,
            )
            return self._runtime_config.to_admin_config()

    def stats(self) -> AdminStats:
        with self._lock:
            stats = self._stats
            decisive = stats.bets_won + stats.bets_lost
            strike_rate = (
                round(stats.bets_won / decisive, 4) if decisive else 0.0
            )
            return AdminStats(
                bets_placed=stats.bets_placed,
                bets_won=stats.bets_won,
                bets_lost=stats.bets_lost,
                bets_void=stats.bets_void,
                bets_pending=stats.bets_pending,
                strike_rate=strike_rate,
                markets_processed=stats.markets_processed,
                total_stake=round(stats.total_stake, 2),
                total_liability=round(stats.total_liability, 2),
                total_pnl=round(stats.total_pnl, 2),
                open_exposure=round(stats.open_exposure, 2),
                stats_window_start=stats.stats_window_start,
            )

    def reset_stats(self) -> None:
        with self._lock:
            self._stats.reset()
            self._recent_settled.clear()
            self._known_settled_bet_ids.clear()

    # ------------------------------------------------------------------
    # GUI views
    # ------------------------------------------------------------------

    def markets(self) -> MarketsResponse:
        """Return the snapshot served by ``GET /api/markets``.

        ``market_time`` arrives from betfairlightweight as a naive UTC
        datetime; ``now`` is timezone-aware. Coercing both to aware UTC
        before subtraction prevents ``TypeError: can't subtract
        offset-naive and offset-aware datetimes``.
        """

        now = datetime.now(timezone.utc)
        out: list[MarketView] = []
        with self._lock:
            for entry in self._market_cache.values():
                seconds_to_off: float | None = None
                market_time = entry.market_time
                if market_time is not None:
                    if market_time.tzinfo is None:
                        market_time = market_time.replace(tzinfo=timezone.utc)
                    seconds_to_off = (market_time - now).total_seconds()
                out.append(
                    MarketView(
                        market_id=entry.market_id,
                        venue=entry.venue,
                        country=entry.country,
                        market_type=entry.market_type,
                        market_time=market_time,
                        seconds_to_off=seconds_to_off,
                        in_play=entry.in_play,
                        evaluated=entry.evaluated,
                        status=entry.status,
                        winner_selection_id=entry.winner_selection_id,
                        runners=list(entry.runners),
                    )
                )
        out.sort(
            key=lambda m: m.market_time or datetime.max.replace(tzinfo=timezone.utc)
        )
        return MarketsResponse(markets=out)

    def positions(self) -> PositionsResponse:
        """Return the snapshot served by ``GET /api/positions``."""

        with self._lock:
            views = [
                PositionView(
                    market_id=o.market_id,
                    selection_id=o.selection_id,
                    runner_name=o.runner_name,
                    side=o.side,  # type: ignore[arg-type]
                    price=o.price,
                    stake=o.stake,
                    liability=o.liability,
                    matched_size=o.matched_size,
                    unmatched_size=o.unmatched_size,
                    rule_applied=o.rule_applied,
                    bet_id=o.bet_id,
                    placed_at=o.placed_at,
                    pnl_if_settled_now=None,
                )
                for o in self._open_orders.values()
            ]
            total_exposure = round(self._stats.open_exposure, 2)
        return PositionsResponse(positions=views, total_exposure=total_exposure)

    def today_results(self) -> ResultsResponse:
        """Return the snapshot served by ``GET /api/results``."""

        with self._lock:
            bets = [r.to_view() for r in self._recent_settled]
        return ResultsResponse(bets=bets, summary=self.stats())

    def history(
        self,
        *,
        range_start: date,
        range_end: date,
        page: int,
        page_size: int,
    ) -> HistoryResponse:
        """Return paginated settled-bet history loaded from GCS."""

        if range_end < range_start:
            raise ValueError("range_end must be on or after range_start")
        bets: list[SettledBet] = []
        cursor = range_start
        while cursor <= range_end:
            blob_name = self._gcs.settled_blob_name(cursor)
            text = self._gcs.download_text(self._runtime_config.results_bucket, blob_name)
            if text:
                for entry in parse_jsonl(text):
                    try:
                        bets.append(SettledBet.model_validate(entry))
                    except Exception:
                        logger.warning(
                            "skipping unparseable settled-bet entry",
                            extra={"blob": blob_name},
                        )
            cursor = date.fromordinal(cursor.toordinal() + 1)

        bets.sort(key=lambda b: b.settled_at, reverse=True)
        total = len(bets)
        start = (page - 1) * page_size
        end = start + page_size
        return HistoryResponse(
            bets=bets[start:end],
            page=page,
            page_size=page_size,
            total=total,
            range_start=range_start,
            range_end=range_end,
        )

    def settled(self, *, range_start: date, range_end: date) -> SettledResponse:
        """Return the response served by ``GET /api/settled``.

        When the engine is authenticated, prefers a fresh pull from
        Betfair so the AIM agent gets the canonical view; falls back to
        the GCS log otherwise.
        """

        if self._betfair.is_authenticated:
            try:
                bets = self._fetch_cleared_bets(range_start, range_end)
                return SettledResponse(
                    bets=bets,
                    range_start=range_start,
                    range_end=range_end,
                    total=len(bets),
                )
            except BetfairServiceError:
                logger.exception("falling back to GCS for settled bets")

        history = self.history(
            range_start=range_start,
            range_end=range_end,
            page=1,
            page_size=10_000,
        )
        return SettledResponse(
            bets=list(history.bets),
            range_start=range_start,
            range_end=range_end,
            total=history.total,
        )

    def account(self) -> AccountResponse:
        """Return the snapshot served by ``GET /api/account``."""

        if not self._betfair.is_authenticated:
            raise BetfairServiceError(
                "engine is not authenticated; start LIVE or DRY_RUN mode first"
            )
        funds = self._betfair.get_account_funds()
        return AccountResponse(
            available_to_bet=float(getattr(funds, "available_to_bet_balance", 0.0)),
            exposure=float(getattr(funds, "exposure", 0.0)),
            points_balance=float(getattr(funds, "points_balance", 0.0) or 0.0),
            wallet=getattr(funds, "wallet", "UK") or "UK",
            retrieved_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Mode transitions
    # ------------------------------------------------------------------

    async def ensure_streaming(self) -> AdminStatus:
        """Bring the Betfair stream up if it isn't already.

        Called from the FastAPI lifespan on container startup so the
        engine is authenticated and streaming markets the moment the
        service is ready, without waiting for an operator action. The
        method is idempotent — if the stream is already connected it
        is a no-op.
        """

        with self._lock:
            already_up = self._stream_status is StreamStatus.CONNECTED
            self._streaming_requested = True
        if already_up:
            return self.status()

        try:
            await asyncio.to_thread(self._spin_up_session)
        except Exception as exc:
            with self._lock:
                self._stream_status = StreamStatus.ERROR
            await self._events.publish(
                "error",
                {"message": str(exc)},
                detail=f"stream failed to come up: {exc}",
            )
            raise

        with self._lock:
            if self._started_at is None:
                self._started_at = datetime.now(timezone.utc)
            self._start_async_pollers()

        await self._events.publish(
            "stream_ready",
            {},
            detail="engine is streaming markets and ready for flag changes",
        )
        return self.status()

    async def set_flag(
        self,
        flag: FlagName,
        enabled: bool,
    ) -> AdminStatus:
        """Set a single behaviour flag and emit a ``flag_changed`` event.

        The stream is not touched — toggling betting / dry-run / recording
        does not connect or disconnect the Betfair session. ``manual_betting``
        and ``auto_betting`` may be on simultaneously; ``dry_run`` is an
        override that simulates rather than transmits any bet that would
        otherwise be placed.
        """

        with self._lock:
            attr = f"_{flag.value}"
            previous = getattr(self, attr)
            if previous == enabled:
                return self.status()
            setattr(self, attr, enabled)
            new_flags = EngineFlags(
                auto_betting=self._auto_betting,
                manual_betting=self._manual_betting,
                dry_run=self._dry_run,
                recording=self._recording,
            )
            new_mode = self._mode

        await self._events.publish(
            "flag_changed",
            {
                "flag": flag.value,
                "previous": previous,
                "current": enabled,
                "flags": new_flags.model_dump(),
                "mode": new_mode.value,
            },
            detail=f"{flag.value} → {enabled}",
        )

        if self._streaming_requested:
            try:
                await self.ensure_streaming()
            except Exception:
                logger.warning("ensure_streaming after flag change failed")

        return self.status()

    async def start_live(self) -> AdminStatus:
        """Legacy compat: turn ``auto_betting`` on, ``dry_run`` off."""

        await self.set_flag(FlagName.DRY_RUN, False)
        return await self.set_flag(FlagName.AUTO_BETTING, True)

    async def start_dry_run(self) -> AdminStatus:
        """Legacy compat: turn ``auto_betting`` and ``dry_run`` on."""

        await self.set_flag(FlagName.AUTO_BETTING, True)
        return await self.set_flag(FlagName.DRY_RUN, True)

    async def stop(self) -> AdminStatus:
        """Legacy compat: turn the betting flags off, leave the stream up."""

        await self.set_flag(FlagName.AUTO_BETTING, False)
        await self.set_flag(FlagName.MANUAL_BETTING, False)
        return await self.set_flag(FlagName.DRY_RUN, False)

    async def emergency_stop(self) -> dict[str, Any]:
        """Kill switch — cancel every open order and force all flags off.

        The Betfair stream is **not** torn down: keeping it up ensures
        the portal continues to render market data and the operator can
        immediately resume from a clean state. Distinct from :meth:`stop`,
        this issues a platform-wide cancel on Betfair so unmatched orders
        can't lapse to fill during the freeze.
        """

        with self._lock:
            previous_flags = EngineFlags(
                auto_betting=self._auto_betting,
                manual_betting=self._manual_betting,
                dry_run=self._dry_run,
                recording=self._recording,
            )
            self._auto_betting = False
            self._manual_betting = False
            self._dry_run = False
            self._recording = False

        cancel_report: dict[str, Any] = {"attempted": False}
        if self._betfair.is_authenticated:
            try:
                response = await asyncio.to_thread(
                    self._betfair.cancel_all_orders
                )
                reports = (
                    getattr(response, "cancel_instruction_reports", None) or []
                )
                cancel_report = {
                    "attempted": True,
                    "status": getattr(response, "status", None),
                    "instruction_count": len(reports),
                    "error_code": getattr(response, "error_code", None),
                }
            except BetfairServiceError as exc:
                logger.exception("emergency_stop: cancel_all_orders raised")
                cancel_report = {
                    "attempted": True,
                    "status": "FAILURE",
                    "error_code": str(exc),
                }

        await self._events.publish(
            "emergency_stop",
            {
                "previous_flags": previous_flags.model_dump(),
                "cancel_report": cancel_report,
            },
            detail=(
                f"EMERGENCY STOP: all flags off; "
                f"cancel status={cancel_report.get('status', 'n/a')}"
            ),
        )

        return {
            "previous_flags": previous_flags.model_dump(),
            "cancel_report": cancel_report,
            "status": self.status().model_dump(mode="json"),
        }

    def _spin_up_session(self) -> None:
        """Login, start the stream, and launch the processing thread."""

        with self._lock:
            self._stream_status = StreamStatus.CONNECTING
            countries = list(self._runtime_config.countries)
            market_types = list(self._runtime_config.market_types)

        if not self._betfair.is_authenticated:
            self._betfair.login()

        output_queue = self._betfair.start_stream(
            event_type_id=self._settings.event_type_id,
            countries=countries,
            market_types=market_types,
            conflate_ms=self._settings.stream_conflate_ms,
            heartbeat_ms=self._settings.stream_heartbeat_ms,
        )

        with self._lock:
            self._stream_status = StreamStatus.CONNECTED
            self._processing_stop.clear()
            self._processing_thread = threading.Thread(
                target=self._processing_loop,
                args=(output_queue,),
                name="fsu100-processing",
                daemon=True,
            )
            self._processing_thread.start()

        self._events.publish_threadsafe(
            "stream_connected",
            {"countries": countries, "market_types": market_types},
            detail=f"stream connected; {len(countries)} countries, {len(market_types)} market types",
        )

    async def _teardown_session(self, *, reason: str) -> None:
        """Stop the stream, processing thread, and pollers, then logout."""

        with self._lock:
            self._processing_stop.set()
            thread = self._processing_thread
            self._processing_thread = None
            tasks = [self._poll_orders_task, self._poll_settlement_task, self._daily_reset_task]
            self._poll_orders_task = None
            self._poll_settlement_task = None
            self._daily_reset_task = None

        for task in tasks:
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self._betfair.stop_stream()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._betfair.logout()

        with self._lock:
            self._stream_status = StreamStatus.DISCONNECTED
            self._market_cache.clear()

        await self._events.publish(
            "stream_disconnected",
            {"reason": reason},
            detail=reason,
        )

    def _start_async_pollers(self) -> None:
        """Schedule the order and settlement polling tasks if not already running.

        Pollers run as long as the stream is up, regardless of which
        behaviour flags are on — outstanding orders and settlements are
        worth tracking even when the engine isn't placing new bets.
        """

        loop = asyncio.get_running_loop()
        with self._lock:
            if self._stream_status is not StreamStatus.CONNECTED:
                return
            if self._poll_orders_task is None or self._poll_orders_task.done():
                self._poll_orders_task = loop.create_task(
                    self._poll_orders(), name="fsu100-poll-orders"
                )
            if (
                self._poll_settlement_task is None
                or self._poll_settlement_task.done()
            ):
                self._poll_settlement_task = loop.create_task(
                    self._poll_settlement(), name="fsu100-poll-settlement"
                )
            if self._daily_reset_task is None or self._daily_reset_task.done():
                self._daily_reset_task = loop.create_task(
                    self._daily_reset_loop(), name="fsu100-daily-reset"
                )
            if (
                self._poll_catalogues_task is None
                or self._poll_catalogues_task.done()
            ):
                self._poll_catalogues_task = loop.create_task(
                    self._poll_catalogues(), name="fsu100-poll-catalogues"
                )

    async def _poll_catalogues(self) -> None:
        """Hydrate runner names from the betting REST catalogue.

        The MCM stream only carries selection_id; runner names come from
        ``list_market_catalogue``. The poller batches up to 100 markets
        per call and runs every few seconds while the stream is up. Once
        a market's names are cached they stick for the lifetime of the
        cache entry — markets that close are evicted from
        ``_market_cache`` and a fresh fetch happens for any that re-open.
        """

        interval = 5.0
        batch_size = 100
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                if self._stream_status is not StreamStatus.CONNECTED:
                    return
                missing = [
                    mid
                    for mid in self._market_cache.keys()
                    if mid not in self._runner_names
                ]
            if not missing:
                continue
            for chunk_start in range(0, len(missing), batch_size):
                chunk = missing[chunk_start : chunk_start + batch_size]
                try:
                    catalogue = await asyncio.to_thread(
                        self._betfair.list_market_catalogue, chunk
                    )
                except BetfairServiceError:
                    logger.exception("list_market_catalogue failed; will retry")
                    continue
                resolved: dict[str, dict[int, str]] = {}
                for entry in catalogue or []:
                    market_id = getattr(entry, "market_id", None) or (
                        entry.get("marketId") if isinstance(entry, dict) else None
                    )
                    if market_id is None:
                        continue
                    runners = getattr(entry, "runners", None)
                    if runners is None and isinstance(entry, dict):
                        runners = entry.get("runners") or []
                    names: dict[int, str] = {}
                    for runner in runners or []:
                        sid = getattr(runner, "selection_id", None)
                        if sid is None and isinstance(runner, dict):
                            sid = runner.get("selectionId")
                        name = getattr(runner, "runner_name", None)
                        if name is None:
                            name = getattr(runner, "name", None)
                        if name is None and isinstance(runner, dict):
                            name = runner.get("runnerName") or runner.get("name")
                        if sid is not None and name:
                            names[int(sid)] = str(name)
                    resolved[str(market_id)] = names
                with self._lock:
                    for mid, names in resolved.items():
                        self._runner_names[mid] = names
                        cache_entry = self._market_cache.get(mid)
                        if cache_entry is not None:
                            cache_entry.runner_names = names
                            cache_entry.runners = [
                                snapshot.model_copy(
                                    update={
                                        "name": names.get(
                                            snapshot.selection_id, snapshot.name
                                        )
                                    }
                                )
                                for snapshot in cache_entry.runners
                            ]
                            cache_entry.catalogue_fetched = True

    async def _daily_reset_loop(self) -> None:
        """Auto-reset stats at midnight UTC so the daily cap resets cleanly.

        The daily spend cap is enforced against ``stats.total_stake`` —
        without this loop, the counter would accumulate indefinitely and
        the cap would behave like a session cap rather than a daily one.
        """

        from datetime import timedelta as _td  # noqa: PLC0415 — local to keep top of file tidy
        while True:
            now = datetime.now(timezone.utc)
            tomorrow = (now + _td(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            await asyncio.sleep((tomorrow - now).total_seconds())
            with self._lock:
                if self._stream_status is not StreamStatus.CONNECTED:
                    return
                self._stats.reset()
                self._recent_settled.clear()
                self._known_settled_bet_ids.clear()
            await self._events.publish(
                "stats_reset",
                {"trigger": "daily-midnight-utc"},
                detail="stats auto-reset at midnight UTC",
            )

    # ------------------------------------------------------------------
    # Streaming & evaluation
    # ------------------------------------------------------------------

    def _processing_loop(self, output_queue: Any) -> None:
        """Consume MarketBooks from the stream and apply the strategy."""

        while not self._processing_stop.is_set():
            try:
                market_books = output_queue.get(timeout=1.0)
            except Exception:
                continue
            try:
                for market_book in market_books:
                    self._handle_market_book(market_book)
            except Exception:
                logger.exception("processing loop raised on market_book")

    def _handle_market_book(self, market_book: Any) -> None:
        """Update the cache and, if eligible, evaluate the market."""

        market_id = getattr(market_book, "market_id", None)
        if market_id is None:
            return
        md = getattr(market_book, "market_definition", None)
        publish_time = getattr(market_book, "publish_time", None)
        in_play = bool(
            getattr(market_book, "inplay", False)
            or getattr(md, "in_play", False)
        )

        runner_views = self._build_runner_views(market_book)
        market_time = getattr(md, "market_time", None) if md is not None else None
        venue = getattr(md, "venue", None) if md is not None else None
        country = getattr(md, "country_code", None) if md is not None else None
        market_type = getattr(md, "market_type", None) if md is not None else None
        # market_definition.status is one of OPEN / SUSPENDED / CLOSED /
        # INACTIVE — feed it through to MarketView so the portal can
        # distinguish "race finished" (CLOSED) from "race running"
        # (in_play=True, status=OPEN).
        status = getattr(md, "status", None) if md is not None else None
        # If the market has closed, look for the winning runner so the
        # operator can see who won at a glance. Runner status comes from
        # market_definition.runners[*].status — "WINNER" / "LOSER" /
        # "REMOVED" / "ACTIVE".
        winner_selection_id: int | None = None
        if status == "CLOSED" and md is not None:
            for rd in getattr(md, "runners", None) or []:
                if getattr(rd, "status", None) == "WINNER":
                    winner_selection_id = getattr(rd, "selection_id", None)
                    break

        with self._lock:
            cache_entry = self._market_cache.get(market_id)
            already_evaluated = bool(cache_entry and cache_entry.evaluated)
            self._market_cache[market_id] = _MarketCacheEntry(
                market_id=market_id,
                venue=venue,
                country=country,
                market_type=market_type,
                market_time=market_time,
                in_play=in_play,
                status=status,
                winner_selection_id=winner_selection_id,
                runners=runner_views,
                last_update=datetime.now(timezone.utc),
                evaluated=already_evaluated,
            )

        if already_evaluated:
            return
        if md is None or market_time is None or publish_time is None:
            return
        if in_play:
            return

        try:
            plugin = self._active_plugin()
        except PluginNotFoundError:
            return
        threshold = plugin.parser.time_before_off_seconds
        seconds_to_off = (market_time - publish_time).total_seconds()
        if seconds_to_off > threshold:
            return
        if seconds_to_off <= 0:
            with self._lock:
                entry = self._market_cache.get(market_id)
                if entry is not None:
                    entry.evaluated = True
            return

        with self._lock:
            countries = tuple(self._runtime_config.countries)
            market_types = tuple(self._runtime_config.market_types)
            point_value = self._runtime_config.point_value
            mode = self._mode
            auto_betting = self._auto_betting
            dry_run = self._dry_run
            customer_strategy_ref = self._runtime_config.customer_strategy_ref

        results = evaluate(
            market_book,
            plugin.strategy,
            point_value=point_value,
            filters_country=countries,
            filters_market_type=market_types,
        )
        bets: list[BetDecision] = [r for r in results if isinstance(r, BetDecision)]
        skipped: list[NoBet] = [r for r in results if isinstance(r, NoBet)]

        with self._lock:
            entry = self._market_cache.get(market_id)
            if entry is not None:
                entry.evaluated = True
            self._stats.markets_processed += 1

        if not bets:
            reason = skipped[0].reason if skipped else "no_decision"
            detail = skipped[0].detail if skipped else "evaluator returned no decisions"
            self._events.publish_threadsafe(
                "evaluation",
                {
                    "market_id": market_id,
                    "decision": "NO_BET",
                    "reason": reason,
                    "detail": detail,
                },
                market_id=market_id,
                detail=f"NO_BET: {reason}",
            )
            return

        for bet in bets:
            self._events.publish_threadsafe(
                "evaluation",
                {
                    "market_id": market_id,
                    "decision": "BET",
                    "rule": bet.rule_applied,
                    "selection_id": bet.selection_id,
                    "runner_name": bet.runner_name,
                    "side": bet.side.value,
                    "price": bet.price,
                    "stake": bet.stake,
                    "liability": bet.liability,
                    "mode": mode.value,
                },
                market_id=market_id,
                detail=(
                    f"{mode.value}: {bet.rule_applied} "
                    f"{bet.side.value} {bet.runner_name} "
                    f"@ {bet.price} for {bet.stake}"
                ),
            )
            if auto_betting:
                if dry_run:
                    # Simulate the bet — no Betfair call, no _OpenOrder
                    # entry (we don't want to skew real exposure stats).
                    # Emit the bet_placed event with dry_run=true so the
                    # portal can surface the dry-run bet on the markets
                    # table just like a real one.
                    self._events.publish_threadsafe(
                        "bet_placed",
                        {
                            "market_id": market_id,
                            "bet_id": None,
                            "rule": bet.rule_applied,
                            "selection_id": bet.selection_id,
                            "runner_name": bet.runner_name,
                            "side": bet.side.value,
                            "price": bet.price,
                            "stake": bet.stake,
                            "liability": bet.liability,
                            "dry_run": True,
                        },
                        market_id=market_id,
                        detail=(
                            f"DRY: {bet.rule_applied} "
                            f"{bet.side.value} {bet.runner_name} "
                            f"@ {bet.price} for {bet.stake}"
                        ),
                    )
                else:
                    self._place_bet_sync(
                        market_id=market_id,
                        bet=bet,
                        customer_strategy_ref=customer_strategy_ref,
                    )

    def _build_runner_views(self, market_book: Any) -> list[RunnerSnapshot]:
        """Construct :class:`RunnerSnapshot` rows for cache and GUI display.

        Extracts the full per-runner price set the portal needs — last
        traded, best back / lay (top of book), and SP projected / actual.
        Missing fields are left as ``None`` rather than raising. Betfair
        emits ``nan`` (not ``None``) for SP fields with no projection
        yet, which the standard JSON encoder rejects; the helper below
        coerces both ``nan`` and ``inf`` to ``None`` so the API response
        always serialises.
        """

        def _safe(v: Any) -> float | None:
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if f != f or f in (float("inf"), float("-inf")):
                return None
            return f

        market_id = getattr(market_book, "market_id", None)
        md = getattr(market_book, "market_definition", None)
        names: dict[int, str] = {}
        # Streaming MCM only carries runner names in historic data; for live
        # data we fall back to the catalogue cache populated by
        # ``_poll_catalogues``.
        with self._lock:
            cached = self._runner_names.get(str(market_id), {}) if market_id else {}
        if md is not None:
            for definition_runner in getattr(md, "runners", []) or []:
                sid = getattr(definition_runner, "selection_id", None)
                if sid is None:
                    continue
                sid_int = int(sid)
                from_definition = getattr(definition_runner, "name", None)
                resolved = (
                    cached.get(sid_int)
                    or from_definition
                    or f"selection_{sid_int}"
                )
                names[sid_int] = resolved
        for sid_int, name in cached.items():
            names.setdefault(int(sid_int), name)
        out: list[RunnerSnapshot] = []
        for runner in getattr(market_book, "runners", []) or []:
            sid = getattr(runner, "selection_id", None)
            if sid is None:
                continue
            sid_int = int(sid)

            best_back: float | None = None
            best_lay: float | None = None
            back_ladder: list[PriceSize] = []
            lay_ladder: list[PriceSize] = []
            ex = getattr(runner, "ex", None)
            if ex is not None:
                # Betfair returns the lists in best-first order — back is
                # sorted by descending price, lay by ascending price. Keep
                # that order; the portal reverses ``back_ladder`` at
                # render time so the best back sits next to BSP.
                back_list = getattr(ex, "available_to_back", None) or []
                lay_list = getattr(ex, "available_to_lay", None) or []
                for ps in back_list[:3]:
                    back_ladder.append(
                        PriceSize(
                            price=_safe(getattr(ps, "price", None)),
                            size=_safe(getattr(ps, "size", None)),
                        )
                    )
                for ps in lay_list[:3]:
                    lay_ladder.append(
                        PriceSize(
                            price=_safe(getattr(ps, "price", None)),
                            size=_safe(getattr(ps, "size", None)),
                        )
                    )
                if back_ladder:
                    best_back = back_ladder[0].price
                if lay_ladder:
                    best_lay = lay_ladder[0].price

            near_price: float | None = None
            far_price: float | None = None
            actual_sp: float | None = None
            sp = getattr(runner, "sp", None)
            if sp is not None:
                near_price = getattr(sp, "near_price", None)
                far_price = getattr(sp, "far_price", None)
                actual_sp = getattr(sp, "actual_sp", None)

            out.append(
                RunnerSnapshot(
                    selection_id=sid_int,
                    name=names.get(sid_int, f"selection_{sid_int}"),
                    status=getattr(runner, "status", None) or "ACTIVE",
                    last_price_traded=_safe(getattr(runner, "last_price_traded", None)),
                    best_back=_safe(best_back),
                    best_lay=_safe(best_lay),
                    back_ladder=back_ladder,
                    lay_ladder=lay_ladder,
                    near_price=_safe(near_price),
                    far_price=_safe(far_price),
                    actual_sp=_safe(actual_sp),
                )
            )
        return out

    def _active_plugin(self) -> PluginConfig:
        """Return the plugin matching the current ``runtime_config.active_plugin``."""

        with self._lock:
            name = self._runtime_config.active_plugin
        return self._plugins.get(name)

    def _resolve_default_plugin(self) -> PluginConfig:
        """Return the default plugin configured in :class:`AppSettings`."""

        try:
            return self._plugins.get(self._settings.default_active_plugin)
        except PluginNotFoundError:
            available = [info.name for info in self._plugins.list()]
            if not available:
                raise PluginNotFoundError(
                    "no plugins installed; cannot determine a default"
                )
            logger.warning(
                "default plugin missing, falling back to first installed",
                extra={
                    "configured": self._settings.default_active_plugin,
                    "fallback": available[0],
                },
            )
            return self._plugins.get(available[0])

    # ------------------------------------------------------------------
    # Bet placement
    # ------------------------------------------------------------------

    def _check_daily_cap(self, stake: float) -> str | None:
        """Return ``None`` if ``stake`` is OK, else a human-readable reason.

        Compares cumulative stake (today's session) + the proposed stake
        against ``daily_max_stake`` from runtime config. Reset at midnight
        UTC by :meth:`_daily_reset_loop`.
        """

        with self._lock:
            if not self._runtime_config.daily_max_stake_enabled:
                return None
            cap = float(self._runtime_config.daily_max_stake or 0.0)
            if cap <= 0:
                return None
            already = float(self._stats.total_stake)
        if already + stake > cap:
            return (
                f"daily spend cap £{cap:.2f} would be exceeded — "
                f"already £{already:.2f} placed; bet stake £{stake:.2f}"
            )
        return None

    def _place_bet_sync(
        self,
        *,
        market_id: str,
        bet: BetDecision,
        customer_strategy_ref: str,
    ) -> None:
        """Place a bet on Betfair from the streaming thread."""

        cap_block = self._check_daily_cap(bet.stake)
        if cap_block is not None:
            logger.warning(
                "bet refused by daily spend cap",
                extra={"market_id": market_id, "reason": cap_block},
            )
            self._events.publish_threadsafe(
                "spend_cap_blocked",
                {"market_id": market_id, "stake": bet.stake, "reason": cap_block},
                market_id=market_id,
                detail=cap_block,
            )
            return

        try:
            response = self._betfair.place_lay_order(
                market_id=market_id,
                selection_id=bet.selection_id,
                price=bet.price,
                size=bet.stake,
                customer_strategy_ref=customer_strategy_ref,
            )
        except BetfairServiceError as exc:
            logger.error(
                "place_orders failed",
                extra={"market_id": market_id, "error": str(exc)},
            )
            self._events.publish_threadsafe(
                "error",
                {"market_id": market_id, "message": str(exc)},
                market_id=market_id,
                detail=f"place_orders failed: {exc}",
            )
            return

        report = self._first_instruction_report(response)
        bet_id = report.get("bet_id") if report else None
        status = report.get("status") if report else None
        if not bet_id or status != "SUCCESS":
            logger.error(
                "place_orders returned non-success",
                extra={"market_id": market_id, "report": report},
            )
            self._events.publish_threadsafe(
                "error",
                {"market_id": market_id, "report": report},
                market_id=market_id,
                detail=f"place_orders status={status}",
            )
            return

        order = _OpenOrder(
            bet_id=str(bet_id),
            market_id=market_id,
            selection_id=bet.selection_id,
            runner_name=bet.runner_name,
            side=bet.side.value,
            price=bet.price,
            stake=bet.stake,
            liability=bet.liability,
            rule_applied=bet.rule_applied,
            placed_at=datetime.now(timezone.utc),
            matched_size=float(report.get("size_matched", 0.0) or 0.0),
            unmatched_size=max(
                bet.stake - float(report.get("size_matched", 0.0) or 0.0),
                0.0,
            ),
        )
        with self._lock:
            self._open_orders[order.bet_id] = order
            self._stats.bets_placed += 1
            self._stats.bets_pending += 1
            self._stats.total_stake += order.stake
            self._stats.total_liability += order.liability
            self._stats.open_exposure = self._compute_exposure_locked()

        self._events.publish_threadsafe(
            "bet_placed",
            {
                "market_id": market_id,
                "bet_id": order.bet_id,
                "rule": bet.rule_applied,
                "selection_id": bet.selection_id,
                "runner_name": bet.runner_name,
                "side": bet.side.value,
                "price": bet.price,
                "stake": bet.stake,
                "liability": bet.liability,
            },
            market_id=market_id,
            detail=(
                f"placed {bet.side.value} {bet.runner_name} @ {bet.price} "
                f"for {bet.stake}, bet_id={order.bet_id}"
            ),
        )

    def place_bet_external(
        self,
        *,
        market_id: str,
        decision: BetDecisionView,
        persistence_type: str,
        customer_order_ref: str | None,
    ) -> Any:
        """Place a bet requested by the AIM agent via ``POST /api/place``.

        Returns the raw betfairlightweight response so the caller can map
        instruction reports onto the API schema.
        """

        if not self._betfair.is_authenticated:
            raise BetfairServiceError(
                "engine is not authenticated; the always-on stream has not "
                "yet logged in"
            )
        with self._lock:
            manual_betting = self._manual_betting
            dry_run = self._dry_run
            customer_strategy_ref = self._runtime_config.customer_strategy_ref
        if not manual_betting:
            raise BetfairServiceError(
                "refusing to place bet: manual_betting flag is off"
            )
        cap_block = self._check_daily_cap(decision.stake)
        if cap_block is not None:
            raise BetfairServiceError(cap_block)
        if dry_run:
            return {
                "status": "SUCCESS",
                "instruction_reports": [
                    {
                        "status": "SUCCESS",
                        "bet_id": None,
                        "instruction": {
                            "selection_id": decision.selection_id,
                            "limit_order": {
                                "size": decision.stake,
                                "price": decision.price,
                                "persistence_type": persistence_type,
                            },
                        },
                        "size_matched": 0.0,
                        "average_price_matched": 0.0,
                        "dry_run": True,
                    }
                ],
            }
        response = self._betfair.place_lay_order(
            market_id=market_id,
            selection_id=decision.selection_id,
            price=decision.price,
            size=decision.stake,
            persistence_type=persistence_type,
            customer_strategy_ref=customer_strategy_ref,
            customer_order_ref=customer_order_ref,
        )
        report = self._first_instruction_report(response)
        bet_id = report.get("bet_id") if report else None
        if bet_id and report and report.get("status") == "SUCCESS":
            order = _OpenOrder(
                bet_id=str(bet_id),
                market_id=market_id,
                selection_id=decision.selection_id,
                runner_name=decision.runner_name,
                side=decision.side,
                price=decision.price,
                stake=decision.stake,
                liability=decision.liability,
                rule_applied=decision.rule_applied,
                placed_at=datetime.now(timezone.utc),
                matched_size=float(report.get("size_matched", 0.0) or 0.0),
                unmatched_size=max(
                    decision.stake - float(report.get("size_matched", 0.0) or 0.0),
                    0.0,
                ),
            )
            with self._lock:
                self._open_orders[order.bet_id] = order
                self._stats.bets_placed += 1
                self._stats.bets_pending += 1
                self._stats.total_stake += order.stake
                self._stats.total_liability += order.liability
                self._stats.open_exposure = self._compute_exposure_locked()
        return response

    def cancel_bet_external(
        self,
        *,
        market_id: str,
        bet_id: str,
        size_reduction: float | None,
    ) -> Any:
        """Handle ``POST /api/cancel``."""

        if not self._betfair.is_authenticated:
            raise BetfairServiceError(
                "engine is not authenticated; start LIVE or DRY_RUN mode first"
            )
        return self._betfair.cancel_order(
            market_id=market_id,
            bet_id=bet_id,
            size_reduction=size_reduction,
        )

    @staticmethod
    def _first_instruction_report(response: Any) -> dict[str, Any] | None:
        """Extract the first instruction report from a place/cancel response."""

        if response is None:
            return None
        reports = getattr(response, "place_instruction_reports", None) or getattr(
            response, "cancel_instruction_reports", None
        )
        if not reports:
            return None
        first = reports[0]
        return {
            "status": getattr(first, "status", None),
            "bet_id": getattr(first, "bet_id", None),
            "placed_date": getattr(first, "placed_date", None),
            "average_price_matched": getattr(first, "average_price_matched", None),
            "size_matched": getattr(first, "size_matched", None),
            "size_cancelled": getattr(first, "size_cancelled", None),
            "cancelled_date": getattr(first, "cancelled_date", None),
            "error_code": getattr(first, "error_code", None),
        }

    def _compute_exposure_locked(self) -> float:
        """Sum liabilities across every open order. Caller must hold the lock."""

        return sum(o.liability for o in self._open_orders.values())

    # ------------------------------------------------------------------
    # Order tracking & settlement
    # ------------------------------------------------------------------

    async def _poll_orders(self) -> None:
        """Periodic refresh of in-memory open orders from Betfair."""

        interval = self._settings.order_polling_seconds
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                if self._mode is EngineMode.STOPPED:
                    return
                strategy_ref = self._runtime_config.customer_strategy_ref
            try:
                report = await asyncio.to_thread(
                    self._betfair.list_current_orders,
                    customer_strategy_refs=[strategy_ref] if strategy_ref else None,
                )
            except BetfairServiceError:
                logger.exception("list_current_orders failed; will retry")
                self._events.publish_threadsafe(
                    "error",
                    {"message": "list_current_orders failed"},
                    detail="list_current_orders failed",
                )
                continue
            self._apply_orders_report(report)

    def _apply_orders_report(self, report: Any) -> None:
        """Update :attr:`_open_orders` and emit ``positions_updated``."""

        if report is None:
            return
        with self._lock:
            current_ids = {o.bet_id for o in self._open_orders.values()}
            seen_ids: set[str] = set()
            for current in getattr(report, "orders", []) or []:
                bet_id = str(getattr(current, "bet_id", ""))
                if not bet_id:
                    continue
                seen_ids.add(bet_id)
                existing = self._open_orders.get(bet_id)
                price_size = getattr(current, "price_size", None)
                price = float(
                    getattr(price_size, "price", None)
                    or (existing.price if existing else 0.0)
                )
                size = float(
                    getattr(price_size, "size", None)
                    or (existing.stake if existing else 0.0)
                )
                size_matched = float(getattr(current, "size_matched", 0.0) or 0.0)
                size_remaining = float(
                    getattr(current, "size_remaining", size - size_matched) or 0.0
                )
                status = getattr(current, "status", "EXECUTABLE")
                if status == "EXECUTION_COMPLETE" and size_remaining <= 0:
                    if existing is not None:
                        del self._open_orders[bet_id]
                        if self._stats.bets_pending > 0:
                            self._stats.bets_pending -= 1
                    continue
                placed_at = getattr(current, "placed_date", None) or (
                    existing.placed_at if existing else datetime.now(timezone.utc)
                )
                runner_name = (
                    existing.runner_name
                    if existing
                    else f"selection_{getattr(current, 'selection_id', 0)}"
                )
                rule_applied = existing.rule_applied if existing else None
                liability = price * size_remaining * (price - 1.0) if price > 1.0 else 0.0
                self._open_orders[bet_id] = _OpenOrder(
                    bet_id=bet_id,
                    market_id=str(getattr(current, "market_id", "")),
                    selection_id=int(getattr(current, "selection_id", 0)),
                    runner_name=runner_name,
                    side=getattr(current, "side", "LAY"),
                    price=price,
                    stake=size,
                    liability=existing.liability if existing else liability,
                    rule_applied=rule_applied,
                    placed_at=placed_at,
                    matched_size=size_matched,
                    unmatched_size=size_remaining,
                )
            for missing in current_ids - seen_ids:
                self._open_orders.pop(missing, None)
            self._stats.open_exposure = self._compute_exposure_locked()
            count = len(self._open_orders)
            exposure = self._stats.open_exposure

        self._events.publish_threadsafe(
            "positions_updated",
            {"open_positions": count, "total_exposure": round(exposure, 2)},
            detail=f"{count} open positions, exposure {exposure:.2f}",
        )

    async def _poll_settlement(self) -> None:
        """Periodic pull of cleared orders, persistence, and stat updates."""

        interval = self._settings.settlement_polling_seconds
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                if self._mode is EngineMode.STOPPED:
                    return
            try:
                today = datetime.now(timezone.utc).date()
                bets = await asyncio.to_thread(
                    self._fetch_cleared_bets, today, today
                )
            except BetfairServiceError:
                logger.exception("list_cleared_orders failed; will retry")
                continue
            await self._record_settlements(bets)

    def _fetch_cleared_bets(
        self, range_start: date, range_end: date
    ) -> list[SettledBet]:
        """Pull cleared orders for the supplied range and convert to views."""

        with self._lock:
            strategy_ref = self._runtime_config.customer_strategy_ref
        report = self._betfair.list_cleared_orders(
            from_day=range_start,
            to_day=range_end,
            customer_strategy_refs=[strategy_ref] if strategy_ref else None,
        )
        return self._cleared_orders_to_views(report)

    def _cleared_orders_to_views(self, report: Any) -> list[SettledBet]:
        """Translate a betfairlightweight cleared-orders report into views."""

        out: list[SettledBet] = []
        if report is None:
            return out
        for order in getattr(report, "orders", []) or []:
            bet_id = str(getattr(order, "bet_id", ""))
            if not bet_id:
                continue
            with self._lock:
                existing_open = self._open_orders.get(bet_id)
            price = float(getattr(order, "price_matched", 0.0) or 0.0)
            stake = float(
                getattr(order, "size_settled", None)
                or getattr(order, "size", None)
                or (existing_open.stake if existing_open else 0.0)
            )
            profit = float(getattr(order, "profit", 0.0) or 0.0)
            outcome = self._infer_outcome(order, profit)
            settled_at = (
                getattr(order, "settled_date", None)
                or datetime.now(timezone.utc)
            )
            side = getattr(order, "side", "LAY")
            liability = (
                existing_open.liability
                if existing_open is not None
                else round(stake * max(price - 1.0, 0.0), 2)
            )
            runner_name = (
                existing_open.runner_name
                if existing_open is not None
                else getattr(order, "item_description", None)
                and getattr(order.item_description, "runner_desc", None)
                or f"selection_{getattr(order, 'selection_id', 0)}"
            )
            rule_applied = (
                existing_open.rule_applied if existing_open is not None else None
            )
            out.append(
                SettledBet(
                    bet_id=bet_id,
                    market_id=str(getattr(order, "market_id", "")),
                    selection_id=int(getattr(order, "selection_id", 0)),
                    runner_name=runner_name,
                    side=side,
                    price=price,
                    stake=round(stake, 2),
                    liability=round(liability, 2),
                    rule_applied=rule_applied,
                    outcome=outcome,
                    pnl=round(profit, 2),
                    settled_at=settled_at,
                )
            )
        return out

    @staticmethod
    def _infer_outcome(order: Any, profit: float) -> str:
        """Infer ``WON`` / ``LOST`` / ``VOID`` from a cleared order."""

        if getattr(order, "size_cancelled", 0):
            return "VOID"
        if profit > 0:
            return "WON"
        if profit < 0:
            return "LOST"
        return "VOID"

    async def _record_settlements(self, bets: Iterable[SettledBet]) -> None:
        """Update stats, drop matching open orders, and persist to GCS."""

        new_records: list[_SettledBetRecord] = []
        with self._lock:
            for bet in bets:
                if bet.bet_id in self._known_settled_bet_ids:
                    continue
                self._known_settled_bet_ids.add(bet.bet_id)
                record = _SettledBetRecord(
                    bet_id=bet.bet_id,
                    market_id=bet.market_id,
                    selection_id=bet.selection_id,
                    runner_name=bet.runner_name,
                    side=bet.side,
                    price=bet.price,
                    stake=bet.stake,
                    liability=bet.liability,
                    rule_applied=bet.rule_applied,
                    outcome=bet.outcome,
                    pnl=bet.pnl,
                    settled_at=bet.settled_at,
                )
                new_records.append(record)
                self._recent_settled.insert(0, record)
                if len(self._recent_settled) > self._MAX_RECENT_SETTLED:
                    del self._recent_settled[self._MAX_RECENT_SETTLED :]
                if record.outcome == "WON":
                    self._stats.bets_won += 1
                elif record.outcome == "LOST":
                    self._stats.bets_lost += 1
                else:
                    self._stats.bets_void += 1
                if self._stats.bets_pending > 0:
                    self._stats.bets_pending -= 1
                self._stats.total_pnl += record.pnl
                self._open_orders.pop(record.bet_id, None)
            self._stats.open_exposure = self._compute_exposure_locked()

        if not new_records:
            return

        bucket = self._runtime_config.results_bucket
        for record in new_records:
            await self._persist_settled_bet(bucket, record)
            await self._events.publish(
                "bet_settled",
                {
                    "bet_id": record.bet_id,
                    "market_id": record.market_id,
                    "outcome": record.outcome,
                    "pnl": record.pnl,
                },
                market_id=record.market_id,
                detail=(
                    f"{record.outcome} {record.runner_name} pnl={record.pnl}"
                ),
            )

        await self._publish_daily_summary(bucket)

    async def _persist_settled_bet(
        self, bucket: str, record: _SettledBetRecord
    ) -> None:
        day = record.settled_at.astimezone(timezone.utc).date()
        blob_name = self._gcs.settled_blob_name(day)
        line = json.dumps(record.to_view().model_dump(mode="json"))
        try:
            await asyncio.to_thread(
                self._gcs.append_jsonl, bucket, blob_name, line
            )
        except RuntimeError:
            logger.exception(
                "failed to append settled bet to GCS",
                extra={"bucket": bucket, "blob_name": blob_name},
            )

    async def _publish_daily_summary(self, bucket: str) -> None:
        """Mirror today's stats to ``summary/<date>.json`` for portal consumption."""

        today = datetime.now(timezone.utc).date()
        snapshot = self.stats().model_dump(mode="json")
        blob_name = self._gcs.daily_summary_blob_name(today)
        try:
            await asyncio.to_thread(
                self._gcs.upload_text,
                bucket,
                blob_name,
                json.dumps(snapshot, separators=(",", ":")),
            )
        except RuntimeError:
            logger.exception(
                "failed to publish daily summary",
                extra={"bucket": bucket, "blob_name": blob_name},
            )

    # ------------------------------------------------------------------
    # Per-variable APPLY (Lay Engine PluginCard wiring)
    # ------------------------------------------------------------------

    async def hydrate_overrides_from_gcs(self) -> dict[str, int]:
        """Replace any installed plugin with its GCS-mirrored override.

        Called from the FastAPI lifespan after the plugin store loads from
        disk. For each plugin we attempt to read
        ``overrides/<plugin_name>.json`` from the results bucket; if
        present and valid, we replace the in-memory ``PluginConfig`` with
        the persisted one. This is what makes operator tunings survive
        container restarts.

        Returns a small report mapping plugin name → field count of the
        loaded override (for logging).
        """

        bucket = self._runtime_config.results_bucket
        report: dict[str, int] = {}
        for info in self._plugins.list():
            blob_name = f"overrides/{info.name}.json"
            try:
                text = await asyncio.to_thread(
                    self._gcs.download_text, bucket, blob_name
                )
            except RuntimeError:
                logger.warning(
                    "could not fetch plugin override (permission?)",
                    extra={"bucket": bucket, "blob_name": blob_name},
                )
                continue
            if not text:
                continue
            try:
                payload = json.loads(text)
                hydrated = PluginConfig.model_validate(payload)
            except Exception:
                logger.exception(
                    "plugin override on GCS failed validation; ignoring",
                    extra={"plugin": info.name},
                )
                continue
            self._plugins._plugins[info.name] = hydrated  # noqa: SLF001 — controlled mutation
            report[info.name] = sum(1 for _ in payload.get("strategy", {}).get("rules", []))
            logger.info(
                "plugin override hydrated from GCS",
                extra={"plugin": info.name, "rules": report[info.name]},
            )
        return report

    async def apply_variable_patches(
        self,
        plugin_name: str,
        variables: dict[str, Any],
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Apply a flat map of dotted-path → value patches to a plugin.

        Locked while either betting flag is on: changing strategy values
        underneath an engine that is actively placing bets is unsafe.
        ``dry_run`` and ``recording`` do not block patches. Patches are
        validated against ``_meta`` bounds (when declared) before being
        applied. The plugin object held in :class:`PluginStore` is mutated
        in place, the full updated plugin is mirrored to GCS at
        ``overrides/<plugin>.json``, and one audit row per applied change
        is appended to ``audit/<YYYY-MM-DD>.jsonl``. An SSE
        ``plugin_variables_applied`` event fires on success.

        Returns:
            ``{"plugin": <updated plugin dict>, "applied": [...], "rejected": {...}}``
            — ``applied`` lists the audit rows for accepted changes;
            ``rejected`` maps path → reason for any patch we declined.
        """

        with self._lock:
            betting_active = self._auto_betting or self._manual_betting
        if betting_active:
            raise PermissionError(
                "refusing to apply variable patches while a betting flag is on; "
                "turn off auto_betting and manual_betting first"
            )

        try:
            plugin = self._plugins.get(plugin_name)
        except PluginNotFoundError:
            raise

        applied: list[dict[str, Any]] = []
        rejected: dict[str, str] = {}
        now = datetime.now(timezone.utc)

        for path, value in variables.items():
            try:
                before, after = self._apply_single_patch(plugin, path, value)
            except (KeyError, TypeError, ValueError) as exc:
                rejected[path] = f"{type(exc).__name__}: {exc}"
                continue
            applied.append(
                {
                    "timestamp": now.isoformat(),
                    "plugin_name": plugin.name,
                    "plugin_version": plugin.version,
                    "actor": actor,
                    "path": path,
                    "before": before,
                    "after": after,
                }
            )

        if applied:
            await self._persist_plugin_overrides(plugin)
            await self._persist_audit_rows(applied)
            await self._events.publish(
                "plugin_variables_applied",
                {
                    "plugin_name": plugin.name,
                    "plugin_version": plugin.version,
                    "applied": applied,
                    "actor": actor,
                },
                detail=(
                    f"{len(applied)} variable patch(es) applied to "
                    f"{plugin.name} by {actor or 'operator'}"
                ),
            )

        return {
            "plugin": plugin.model_dump(mode="json"),
            "applied": applied,
            "rejected": rejected,
        }

    def _apply_single_patch(
        self, plugin: PluginConfig, path: str, value: Any
    ) -> tuple[Any, Any]:
        """Mutate ``plugin`` at ``path`` and return ``(before, after)``.

        Path forms accepted:
            * ``<rule_name>.<field>``       — e.g. ``rule_2b.base_stake``
            * ``controls.<field>``          — e.g. ``controls.jofs_spread``
            * ``staking.<field>``           — e.g. ``staking.point_value``

        Bounds are checked against the matching ``_meta`` block when
        declared on the plugin; out-of-bounds values raise ``ValueError``.
        """

        parts = path.split(".")
        if len(parts) != 2:
            raise ValueError(f"unsupported path '{path}'")
        head, field = parts

        if head == "controls":
            target = plugin.strategy.controls
            meta = getattr(target, "_meta", None) or {}
        elif head == "staking":
            target = plugin.staking
            meta = getattr(target, "_meta", None) or {}
        else:
            target = next(
                (r for r in plugin.strategy.rules if r.name == head), None
            )
            if target is None:
                raise KeyError(f"unknown rule '{head}'")
            meta = getattr(target, "_meta", None) or {}

        if not hasattr(target, field):
            raise KeyError(f"unknown field '{field}' on '{head}'")

        coerced = self._coerce_with_meta(value, meta.get(field))
        before = getattr(target, field, None)
        try:
            setattr(target, field, coerced)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"could not assign {coerced!r} to {path}: {exc}") from exc
        return before, coerced

    @staticmethod
    def _coerce_with_meta(value: Any, meta: Any) -> Any:
        """Coerce ``value`` to the right primitive and check bounds.

        ``meta`` is the per-field ``_meta`` block from the plugin
        (``{"min": 0, "max": 5, "step": 0.5, "default": 1}`` or similar).
        Booleans pass through. Numerics are coerced to float and bounded
        if min/max are declared.
        """

        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            num = float(value)
        elif isinstance(value, str):
            try:
                num = float(value)
            except ValueError:
                return value
        else:
            return value
        if meta:
            if "min" in meta and num < float(meta["min"]):
                raise ValueError(
                    f"value {num} below declared min {meta['min']}"
                )
            if "max" in meta and num > float(meta["max"]):
                raise ValueError(
                    f"value {num} above declared max {meta['max']}"
                )
        return num

    async def _persist_plugin_overrides(self, plugin: PluginConfig) -> None:
        """Mirror the full updated plugin to GCS under ``overrides/<name>.json``.

        Allows future container restarts to rehydrate operator tunings.
        Hydration on startup is a follow-up — the file is written today
        so it can be consumed when that lands.
        """

        bucket = self._runtime_config.results_bucket
        blob_name = f"overrides/{plugin.name}.json"
        payload = json.dumps(plugin.model_dump(mode="json"), indent=2)
        try:
            await asyncio.to_thread(
                self._gcs.upload_text, bucket, blob_name, payload
            )
        except RuntimeError:
            logger.exception(
                "failed to persist plugin overrides",
                extra={"bucket": bucket, "blob_name": blob_name},
            )

    async def _persist_audit_rows(
        self, rows: list[dict[str, Any]]
    ) -> None:
        """Append audit rows to today's JSONL log in the results bucket."""

        if not rows:
            return
        bucket = self._runtime_config.results_bucket
        today = datetime.now(timezone.utc).date()
        blob_name = f"audit/{today.isoformat()}.jsonl"
        for row in rows:
            try:
                await asyncio.to_thread(
                    self._gcs.append_jsonl,
                    bucket,
                    blob_name,
                    json.dumps(row),
                )
            except RuntimeError:
                logger.exception(
                    "failed to append audit row",
                    extra={"bucket": bucket, "blob_name": blob_name},
                )

    # ------------------------------------------------------------------
    # Stateless helpers used by /api/evaluate
    # ------------------------------------------------------------------

    def evaluate_snapshot(
        self,
        snapshot: Any,
        plugin: PluginConfig,
        *,
        point_value_override: float | None = None,
    ) -> tuple[list[BetDecision], list[NoBet]]:
        """Run the evaluator against a caller-supplied market snapshot.

        Pure delegation to :func:`evaluator.evaluate` — kept on the engine
        so the FastAPI layer doesn't import the evaluator directly and so
        a future change to defaults flows through one entry point.
        """

        point_value = (
            point_value_override
            if point_value_override is not None
            else plugin.staking.point_value
        )
        # Live streaming filters live on the admin runtime config, not on
        # the plugin. Fall back to plugin.source.filters only when an older
        # plugin payload still embeds them (otherwise empty = no extra
        # parse-time filtering on top of the streaming filter).
        legacy_filters = plugin.source.filters if plugin.source else None
        results = evaluate(
            snapshot,
            plugin.strategy,
            point_value=point_value,
            filters_country=(
                legacy_filters.countries if legacy_filters
                else self._runtime_config.countries
            ),
            filters_market_type=(
                legacy_filters.market_types if legacy_filters
                else self._runtime_config.market_types
            ),
        )
        bets = [r for r in results if isinstance(r, BetDecision)]
        skipped = [r for r in results if isinstance(r, NoBet)]
        return bets, skipped


_ENGINE: LiveEngine | None = None


def get_engine() -> LiveEngine:
    """Return the lazily-created singleton engine instance.

    Created during the FastAPI lifespan via :func:`create_engine`. Calling
    this before lifespan startup is a programming error.
    """

    if _ENGINE is None:
        raise RuntimeError("engine has not been created yet")
    return _ENGINE


def create_engine(
    *,
    settings: AppSettings,
    plugins: PluginStore,
    betfair: BetfairService,
    gcs: GcsService,
    events: EventBus,
) -> LiveEngine:
    """Create and register the process-wide :class:`LiveEngine`."""

    global _ENGINE
    _ENGINE = LiveEngine(
        settings=settings,
        plugins=plugins,
        betfair=betfair,
        gcs=gcs,
        events=events,
    )
    return _ENGINE


__all__ = [
    "EngineRuntimeConfig",
    "LiveEngine",
    "create_engine",
    "get_engine",
]
