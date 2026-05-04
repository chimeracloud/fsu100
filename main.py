"""FastAPI application exposing the three FSU100 endpoint sets.

* ``/admin/*``         — health, config, stats, controls, SSE.
* ``/api/markets``,    — portal-facing snapshots.
  ``/api/positions``,
  ``/api/results*``,
  ``/api/strategies*``,
  ``/api/account``
* ``/api/evaluate``,   — content endpoints called by AIM agents.
  ``/api/place``,
  ``/api/cancel``,
  ``/api/settled``

The lifespan handler builds the shared services, registers the
:class:`LiveEngine` singleton, and binds the event bus to the running
asyncio loop. **Crucially**, no betting work begins here — the engine
boots in :class:`EngineMode.STOPPED` and only enters ``LIVE`` or
``DRY_RUN`` when an operator calls ``POST /admin/control/start`` or
``/dry-run``. This is the safety contract from the build spec: default
mode is ``STOPPED``; dry-run before live; no auto-bet on deploy.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Path as FastApiPath, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse

from core.config import AppSettings, get_settings
from core.events import EventBus, get_event_bus
from core.logging import configure_logging, get_logger
from core.plugin_store import (
    PluginNotFoundError,
    PluginStore,
    get_plugin_store,
)
from engine import LiveEngine, create_engine, get_engine
from models.schemas import (
    AccountResponse,
    ActivityResponse,
    AdminConfig,
    AdminStats,
    AdminStatus,
    BetDecisionView,
    CancelRequest,
    CancelReport,
    CancelResponse,
    ControlAction,
    ControlResponse,
    EngineFlags,
    FlagName,
    FlagPatch,
    CredentialSecretStatus,
    CredentialStatusResponse,
    VariablePatchAudit,
    VariablePatchRequest,
    VariablePatchResponse,
    EvaluateRequest,
    EvaluateResponse,
    HistoryResponse,
    MarketsResponse,
    NoBetView,
    PlaceRequest,
    PlaceReport,
    PlaceResponse,
    PositionsResponse,
    ResultsResponse,
    SettledResponse,
    StrategyInfo,
    StrategySchema,
)
from services.betfair_service import BetfairService, BetfairServiceError
from services.gcs_service import GcsService

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the shared singletons, wire the engine, and bring the stream up.

    The Betfair stream is started immediately on container boot — markets
    must always be visible to the portal regardless of which behaviour
    flags are on. The four flags (auto_betting, manual_betting, dry_run,
    recording) all default to ``False``: streaming is on, betting is off.
    """

    configure_logging()
    settings = get_settings()
    plugins = get_plugin_store()
    bus = get_event_bus()
    bus.bind_loop(asyncio.get_running_loop())
    betfair = BetfairService(max_latency_seconds=settings.stream_max_latency_seconds)
    gcs = GcsService()
    engine = create_engine(
        settings=settings,
        plugins=plugins,
        betfair=betfair,
        gcs=gcs,
        events=bus,
    )

    app.state.settings = settings
    app.state.plugins = plugins
    app.state.events = bus
    app.state.betfair = betfair
    app.state.gcs = gcs
    app.state.engine = engine

    # Rehydrate any plugin override the operator persisted in a previous
    # session so variable tunings survive container restarts.
    try:
        hydrated = await engine.hydrate_overrides_from_gcs()
        if hydrated:
            logger.info(
                "rehydrated plugin overrides from GCS",
                extra={"plugins": list(hydrated.keys())},
            )
    except Exception:  # noqa: BLE001 — startup must not fail because of overrides
        logger.exception("plugin override hydration raised; continuing with disk plugins")

    # Bring the stream up so /api/markets and /api/account work from the
    # first request. A failure here is logged but does not crash the
    # container — the engine will reconnect on the next operator action.
    try:
        await engine.ensure_streaming()
        logger.info("fsu100 stream started on boot")
    except Exception:  # noqa: BLE001 — stream may be unhealthy but service stays up
        logger.exception(
            "fsu100 stream failed to start on boot; will retry on next flag change"
        )

    logger.info(
        "fsu100 service started",
        extra={
            "service": settings.service_name,
            "version": settings.version,
            "environment": settings.environment,
        },
    )
    try:
        yield
    finally:
        logger.info("fsu100 service stopping; tearing down betfair session")
        try:
            await engine.stop()
        except Exception:
            logger.exception("error during engine stop on shutdown")
        try:
            betfair.logout()
        except Exception:
            logger.exception("error during betfair logout on shutdown")


app = FastAPI(
    title="Chimera Betting Engine — FSU100",
    version=get_settings().version,
    description=(
        "Live Betfair betting engine with pluggable JSON-driven strategies. "
        "Default mode is STOPPED; operators must explicitly start LIVE or "
        "DRY_RUN via /admin/control."
    ),
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------


def _settings_dep() -> AppSettings:
    return get_settings()


def _engine_dep() -> LiveEngine:
    return get_engine()


def _bus_dep() -> EventBus:
    return get_event_bus()


def _plugins_dep() -> PluginStore:
    return get_plugin_store()


# ---------------------------------------------------------------------------
# SET 1 — PARAMETERS (admin)
# ---------------------------------------------------------------------------


@app.get(
    "/admin/status",
    response_model=AdminStatus,
    tags=["admin"],
    summary="Service health, version, mode, and stream status.",
)
async def admin_status(
    engine: LiveEngine = Depends(_engine_dep),
) -> AdminStatus:
    """Return the engine's current status snapshot."""

    return engine.status()


@app.get(
    "/admin/config",
    response_model=AdminConfig,
    tags=["admin"],
    summary="Effective runtime configuration as structured JSON.",
)
async def admin_config_get(
    engine: LiveEngine = Depends(_engine_dep),
) -> AdminConfig:
    """Return the editable runtime configuration."""

    return engine.get_runtime_config()


@app.put(
    "/admin/config",
    response_model=AdminConfig,
    tags=["admin"],
    summary="Update the runtime configuration in-process.",
)
async def admin_config_put(
    payload: AdminConfig,
    engine: LiveEngine = Depends(_engine_dep),
) -> AdminConfig:
    """Apply runtime configuration changes."""

    try:
        return engine.update_runtime_config(payload)
    except PluginNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"unknown plugin '{payload.active_plugin}'",
        ) from exc


@app.get(
    "/admin/stats",
    response_model=AdminStats,
    tags=["admin"],
    summary="Aggregate metrics for the current stats window.",
)
async def admin_stats(
    engine: LiveEngine = Depends(_engine_dep),
) -> AdminStats:
    """Return the rolling stats counter."""

    return engine.stats()


@app.get(
    "/admin/credentials/status",
    response_model=CredentialStatusResponse,
    tags=["admin"],
    summary="Per-engine credential bundle status (no values surfaced).",
)
async def admin_credentials_status(
    settings: AppSettings = Depends(_settings_dep),
) -> CredentialStatusResponse:
    """Return whether the engine's Secret Manager bundle is fully provisioned.

    Reads `secretmanager.versions.access` per required secret. Engine's
    bundle name is derived from ``customer_strategy_ref`` (per-engine,
    per-sport). Future: a config-driven explicit bundle name.
    """

    from services.secrets_service import SecretsService

    service = SecretsService()
    report = await asyncio.to_thread(service.credential_status)
    bundle_name = f"betfair-{settings.customer_strategy_ref}-creds"
    return CredentialStatusResponse(
        bundle_name=bundle_name,
        project=str(report.get("project", settings.gcp_project)),
        configured=bool(report.get("configured", False)),
        secrets=[
            CredentialSecretStatus(
                secret_id=str(s.get("secret_id", "")),
                configured=bool(s.get("configured", False)),
                error=(str(s["error"]) if s.get("error") else None),
            )
            for s in report.get("secrets", [])
        ],
        retrieved_at=datetime.now(timezone.utc),
    )


@app.get(
    "/admin/activity",
    response_model=ActivityResponse,
    tags=["admin"],
    summary="Recent activity log (newest first).",
)
async def admin_activity(
    bus: EventBus = Depends(_bus_dep),
) -> ActivityResponse:
    """Return the in-memory activity ring buffer."""

    events = await bus.recent_activity()
    return ActivityResponse(events=events)


@app.put(
    "/admin/control/flags/{flag}",
    response_model=ControlResponse,
    tags=["admin"],
    summary="Toggle a single behaviour flag (auto/manual betting, dry-run, recording).",
)
async def admin_set_flag(
    payload: FlagPatch,
    flag: FlagName = FastApiPath(..., description="Flag to update."),
    engine: LiveEngine = Depends(_engine_dep),
) -> ControlResponse:
    """Flip one of the four engine behaviour flags.

    The four flags are independent. Stream connectivity is unaffected by
    flag changes — markets continue to populate regardless. ``dry_run``
    is an override: while on, both auto-fired and manually-placed bets
    are simulated rather than sent to Betfair.
    """

    try:
        status = await engine.set_flag(flag, payload.enabled)
    except BetfairServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ControlResponse(
        action=f"flag.{flag.value}",
        accepted=True,
        mode=status.mode,
        flags=status.flags,
        detail=f"{flag.value} → {payload.enabled}",
    )


@app.post(
    "/admin/control/{action}",
    response_model=ControlResponse,
    tags=["admin"],
    summary="Legacy engine controls (start, stop, dry-run, reset-stats, emergency-stop).",
)
async def admin_control(
    action: ControlAction = FastApiPath(..., description="Control action."),
    engine: LiveEngine = Depends(_engine_dep),
    bus: EventBus = Depends(_bus_dep),
) -> ControlResponse:
    """Backwards-compatible mode controls.

    Each action maps onto one or more flag flips. The new portal should
    use ``PUT /admin/control/flags/{flag}`` directly; this endpoint is
    retained so existing automation continues to work.
    """

    try:
        if action is ControlAction.EMERGENCY_STOP:
            report = await engine.emergency_stop()
            await bus.publish(
                "stats_reset",
                {},
                detail="EMERGENCY STOP triggered by operator",
            )
            cancel_status = report.get("cancel_report", {}).get("status") or "n/a"
            status = engine.status()
            return ControlResponse(
                action=action.value,
                accepted=True,
                mode=status.mode,
                flags=status.flags,
                detail=(
                    f"emergency stop completed; cancel_orders status={cancel_status}"
                ),
            )
        if action is ControlAction.START:
            status = await engine.start_live()
            return ControlResponse(
                action=action.value,
                accepted=True,
                mode=status.mode,
                flags=status.flags,
                detail="auto_betting=on, dry_run=off",
            )
        if action is ControlAction.DRY_RUN:
            status = await engine.start_dry_run()
            return ControlResponse(
                action=action.value,
                accepted=True,
                mode=status.mode,
                flags=status.flags,
                detail="auto_betting=on, dry_run=on",
            )
        if action is ControlAction.STOP:
            status = await engine.stop()
            return ControlResponse(
                action=action.value,
                accepted=True,
                mode=status.mode,
                flags=status.flags,
                detail="all betting flags off; stream stays up",
            )
        if action is ControlAction.RESET_STATS:
            engine.reset_stats()
            await bus.publish(
                "stats_reset",
                {},
                detail="stats reset by operator",
            )
            status = engine.status()
            return ControlResponse(
                action=action.value,
                accepted=True,
                mode=status.mode,
                flags=status.flags,
                detail="stats reset",
            )
    except BetfairServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=f"unknown action: {action}")


@app.get(
    "/admin/events",
    tags=["admin"],
    summary="Server-Sent Events stream of engine lifecycle updates.",
)
async def admin_events(
    bus: EventBus = Depends(_bus_dep),
) -> EventSourceResponse:
    """Open an SSE connection that emits one event per state change."""

    async def _gen() -> AsyncIterator[dict[str, Any]]:
        async for record in bus.subscribe():
            yield {
                "event": record["event"],
                "data": json.dumps(
                    {
                        "timestamp": record["timestamp"],
                        "market_id": record.get("market_id"),
                        "data": record.get("data", {}),
                    }
                ),
            }

    return EventSourceResponse(_gen())


# ---------------------------------------------------------------------------
# SET 2 — GUI (portal-facing)
# ---------------------------------------------------------------------------


@app.get(
    "/api/markets",
    response_model=MarketsResponse,
    tags=["gui"],
    summary="Active markets currently being monitored.",
)
async def list_markets(
    engine: LiveEngine = Depends(_engine_dep),
) -> MarketsResponse:
    """Return the engine's view of active markets."""

    return engine.markets()


@app.get(
    "/api/positions",
    response_model=PositionsResponse,
    tags=["gui"],
    summary="Open positions currently held on the exchange.",
)
async def list_positions(
    engine: LiveEngine = Depends(_engine_dep),
) -> PositionsResponse:
    """Return open orders cached from the most recent poll."""

    return engine.positions()


@app.get(
    "/api/results",
    response_model=ResultsResponse,
    tags=["gui"],
    summary="Today's settled bets and rolling summary stats.",
)
async def list_results_today(
    engine: LiveEngine = Depends(_engine_dep),
) -> ResultsResponse:
    """Return today's results from in-memory state."""

    return engine.today_results()


@app.get(
    "/api/results/history",
    response_model=HistoryResponse,
    tags=["gui"],
    summary="Historical settled bets (date range, paginated).",
)
async def list_results_history(
    range_start: date = Query(..., description="Inclusive start date."),
    range_end: date = Query(..., description="Inclusive end date."),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    engine: LiveEngine = Depends(_engine_dep),
) -> HistoryResponse:
    """Return paginated historical settled bets loaded from GCS."""

    try:
        return engine.history(
            range_start=range_start,
            range_end=range_end,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/strategies",
    response_model=list[StrategyInfo],
    tags=["gui"],
    summary="Installed strategy plugins.",
)
async def list_strategies(
    plugins: PluginStore = Depends(_plugins_dep),
) -> list[StrategyInfo]:
    """Return one summary entry per installed plugin."""

    return plugins.list()


@app.get(
    "/api/strategies/{name}/schema",
    response_model=StrategySchema,
    tags=["gui"],
    summary="Editor schema for a specific strategy plugin.",
)
async def get_strategy_schema(
    name: str,
    plugins: PluginStore = Depends(_plugins_dep),
) -> StrategySchema:
    """Return the schema used by the portal to render a config form."""

    try:
        return plugins.schema_for(name)
    except PluginNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"plugin '{name}' not installed",
        ) from exc


@app.post(
    "/api/strategies/{name}/variables",
    response_model=VariablePatchResponse,
    tags=["gui"],
    summary="Tune plugin variable values (mode-locked, audited).",
)
async def post_strategy_variables(
    name: str,
    payload: VariablePatchRequest,
    engine: LiveEngine = Depends(_engine_dep),
) -> VariablePatchResponse:
    """Apply per-variable patches to a plugin's values.

    Mode-locked: 409 if engine is not ``STOPPED``. Patches are validated
    against ``_meta`` bounds when declared on the plugin. Successful
    changes are audited to GCS and broadcast as a
    ``plugin_variables_applied`` SSE event.
    """

    try:
        result = await engine.apply_variable_patches(
            name, payload.variables, actor=payload.actor
        )
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PluginNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"plugin '{name}' not installed"
        ) from exc

    return VariablePatchResponse(
        plugin=result["plugin"],
        applied=[VariablePatchAudit(**row) for row in result["applied"]],
        rejected=result["rejected"],
    )


@app.get(
    "/api/account",
    response_model=AccountResponse,
    tags=["gui"],
    summary="Account balance, exposure, and available funds.",
)
async def get_account(
    engine: LiveEngine = Depends(_engine_dep),
) -> AccountResponse:
    """Return the current account snapshot from Betfair."""

    try:
        return engine.account()
    except BetfairServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# SET 3 — CONTENT (AIM agent calls)
# ---------------------------------------------------------------------------


@app.post(
    "/api/evaluate",
    response_model=EvaluateResponse,
    tags=["content"],
    summary="Evaluate a market snapshot against a strategy and return the decision.",
)
async def post_evaluate(
    request: EvaluateRequest,
    engine: LiveEngine = Depends(_engine_dep),
) -> EvaluateResponse:
    """Run the strategy evaluator against a caller-supplied snapshot."""

    bets, skipped = engine.evaluate_snapshot(
        request.market_snapshot,
        request.plugin,
        point_value_override=request.point_value_override,
    )
    return EvaluateResponse(
        market_id=request.market_snapshot.market_id,
        decisions=[
            BetDecisionView(
                selection_id=b.selection_id,
                runner_name=b.runner_name,
                side=b.side.value,
                price=b.price,
                stake=b.stake,
                liability=b.liability,
                rule_applied=b.rule_applied,
                notes=b.notes,
            )
            for b in bets
        ],
        skipped=[NoBetView(reason=s.reason, detail=s.detail) for s in skipped],
    )


@app.post(
    "/api/place",
    response_model=PlaceResponse,
    tags=["content"],
    summary="Place a bet on Betfair (LIVE mode only).",
)
async def post_place(
    request: PlaceRequest,
    engine: LiveEngine = Depends(_engine_dep),
) -> PlaceResponse:
    """Place a single bet and return the Betfair instruction reports."""

    try:
        response = await asyncio.to_thread(
            engine.place_bet_external,
            market_id=request.market_id,
            decision=request.decision,
            persistence_type=request.persistence_type,
            customer_order_ref=request.customer_order_ref,
        )
    except BetfairServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return PlaceResponse(
        market_id=request.market_id,
        customer_ref=getattr(response, "customer_ref", "") or "",
        status=getattr(response, "status", "UNKNOWN") or "UNKNOWN",
        error_code=getattr(response, "error_code", None),
        instruction_reports=_translate_place_reports(response),
    )


@app.post(
    "/api/cancel",
    response_model=CancelResponse,
    tags=["content"],
    summary="Cancel an open order by bet_id.",
)
async def post_cancel(
    request: CancelRequest,
    engine: LiveEngine = Depends(_engine_dep),
) -> CancelResponse:
    """Cancel an open order and return the Betfair instruction reports."""

    try:
        response = await asyncio.to_thread(
            engine.cancel_bet_external,
            market_id=request.market_id,
            bet_id=request.bet_id,
            size_reduction=request.size_reduction,
        )
    except BetfairServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return CancelResponse(
        market_id=request.market_id,
        status=getattr(response, "status", "UNKNOWN") or "UNKNOWN",
        error_code=getattr(response, "error_code", None),
        instruction_reports=_translate_cancel_reports(response),
    )


@app.get(
    "/api/settled",
    response_model=SettledResponse,
    tags=["content"],
    summary="Pull settled orders for a date range.",
)
async def get_settled(
    range_start: date = Query(..., description="Inclusive start date."),
    range_end: date = Query(..., description="Inclusive end date."),
    engine: LiveEngine = Depends(_engine_dep),
) -> SettledResponse:
    """Return cleared orders, preferring a fresh Betfair pull when authenticated."""

    try:
        return await asyncio.to_thread(
            engine.settled,
            range_start=range_start,
            range_end=range_end,
        )
    except BetfairServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _translate_place_reports(response: Any) -> list[PlaceReport]:
    """Convert betfairlightweight place reports into Pydantic views."""

    out: list[PlaceReport] = []
    for report in getattr(response, "place_instruction_reports", None) or []:
        out.append(
            PlaceReport(
                status=getattr(report, "status", "UNKNOWN") or "UNKNOWN",
                bet_id=_optional_str(getattr(report, "bet_id", None)),
                placed_date=_optional_datetime(
                    getattr(report, "placed_date", None)
                ),
                average_price_matched=_optional_float(
                    getattr(report, "average_price_matched", None)
                ),
                size_matched=_optional_float(
                    getattr(report, "size_matched", None)
                ),
                error_code=_optional_str(getattr(report, "error_code", None)),
            )
        )
    return out


def _translate_cancel_reports(response: Any) -> list[CancelReport]:
    """Convert betfairlightweight cancel reports into Pydantic views."""

    out: list[CancelReport] = []
    for report in getattr(response, "cancel_instruction_reports", None) or []:
        instruction = getattr(report, "instruction", None)
        bet_id = (
            getattr(instruction, "bet_id", None)
            if instruction is not None
            else None
        )
        out.append(
            CancelReport(
                status=getattr(report, "status", "UNKNOWN") or "UNKNOWN",
                bet_id=str(bet_id) if bet_id is not None else "",
                cancelled_date=_optional_datetime(
                    getattr(report, "cancelled_date", None)
                ),
                size_cancelled=_optional_float(
                    getattr(report, "size_cancelled", None)
                ),
                error_code=_optional_str(getattr(report, "error_code", None)),
            )
        )
    return out


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@app.exception_handler(ValidationError)
async def _validation_handler(_request, exc: ValidationError) -> JSONResponse:  # type: ignore[no-untyped-def]
    """Return a 422 with structured error detail rather than a 500."""

    return JSONResponse(status_code=422, content={"errors": exc.errors()})
