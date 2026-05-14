"""Track 1 production lay engine — FastAPI entrypoint.

Minimal endpoints, deliberately. Everything the portal page needs and
nothing else. No plugins, no Acceptor, no AI — just the engine.

Routes:

  GET  /admin/status              engine + stream state, counters
  GET  /admin/config              current Settings as JSON
  PUT  /admin/config              update Settings
  POST /admin/control/{action}    start (DRY_RUN) | live (LIVE) | stop
  GET  /admin/events              SSE feed of evaluations / placements / settlements

  GET  /api/results               today's evaluations + placements + settlements
  GET  /api/results/{date}        results for a historic date (YYYY-MM-DD)
  GET  /api/account               Betfair account balance + exposure

  GET  /health                    liveness
  GET  /ready                     readiness
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from engine import Engine
from gcs import TradingStore, load_results_for_date, load_settings, save_settings
from settings import Mode, Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the engine on startup. It stays in STOPPED mode until the
    # operator hits POST /admin/control/start.
    results = TradingStore()
    engine = Engine(results)
    app.state.engine = engine
    app.state.results = results

    # Restore persisted settings if they exist (settings/current.json).
    # First boot has no file → use the Settings dataclass defaults.
    persisted = load_settings()
    if persisted:
        try:
            restored = Settings.from_dict(persisted)
            # SAFETY: never auto-resume an active mode after a restart.
            # If the previous session was running DRY_RUN or LIVE and
            # the container restarts (deploy, scale event, crash), the
            # operator MUST explicitly hit START or GO LIVE again. This
            # prevents the engine from silently picking up where it
            # left off mid-race — a real footgun observed 2026-05-14.
            restored.general.mode = Mode.STOPPED
            engine.replace_settings(restored)
            logger.info(
                "CLE V2 engine ready — settings restored from GCS, mode forced to STOPPED"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "persisted settings failed validation — using defaults: %s", exc,
            )
    else:
        logger.info("CLE V2 engine ready — using default settings (no persisted file)")

    try:
        yield
    finally:
        engine.shutdown()
        logger.info("CLE V2 engine shut down")


app = FastAPI(
    title="Chimera FSU100 — Track 1 Production",
    description=(
        "Pure rule-based Betfair lay engine for UK/IE horse racing. "
        "Mark's 6 rules + controls + signals + risk overlay. "
        "Same evaluator function runs in DRY_RUN and LIVE."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chimerasportstrading.com",
        "https://www.chimerasportstrading.com",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# Admin
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/admin/status", tags=["admin"])
async def get_status() -> dict[str, Any]:
    return app.state.engine.status()


@app.get("/admin/config", tags=["admin"])
async def get_config() -> dict[str, Any]:
    return app.state.engine.settings.to_dict()


@app.put("/admin/config", tags=["admin"])
async def put_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Replace the current settings.

    Persists to ``gs://chiops-clev2-trading/settings/current.json``
    before updating the in-memory engine. If GCS write fails we
    return 500 and DO NOT update the engine — operator must see
    that the change didn't stick. Survives redeploys via the
    lifespan loader.
    """

    try:
        new_settings = Settings.from_dict(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"invalid settings: {exc}",
        ) from exc

    settings_dict = new_settings.to_dict()
    if not save_settings(settings_dict):
        raise HTTPException(
            status_code=500,
            detail=(
                "settings did not persist to GCS — engine NOT updated. "
                "Retry; if the error persists, check GCS health and SA "
                "permissions on gs://chiops-clev2-trading/."
            ),
        )

    app.state.engine.replace_settings(new_settings)
    return {"settings": settings_dict, "persisted": True}


@app.post("/admin/control/{action}", tags=["admin"])
async def post_control(action: str) -> dict[str, Any]:
    """Flip mode. ``action`` ∈ {start, live, stop}."""

    try:
        return app.state.engine.control(action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/admin/events", tags=["admin"])
async def get_events():
    """SSE — evaluations, placements, settlements as they happen."""

    async def event_iter():
        loop = asyncio.get_event_loop()
        # Run the blocking queue.get in a thread, yield to FastAPI.
        gen = app.state.engine.events()
        while True:
            event = await loop.run_in_executor(None, lambda: next(gen, None))
            if event is None:
                break
            yield {
                "event": event["type"],
                "data": json.dumps(event["data"], default=str),
            }

    return EventSourceResponse(event_iter())


# ──────────────────────────────────────────────────────────────────────────────
# API
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/api/results", tags=["api"])
async def get_results_today() -> dict[str, Any]:
    snap = app.state.results.snapshot()
    snap["summary"] = app.state.results.summary()
    return snap


@app.get("/api/results/{date}", tags=["api"])
async def get_results_for_date(date: str) -> dict[str, Any]:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return load_results_for_date(date)


@app.get("/api/account", tags=["api"])
async def get_account() -> dict[str, Any]:
    status = app.state.engine.status()
    return {
        "balance": status.get("account_balance", 0.0),
        "exposure": status.get("account_exposure", 0.0),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["meta"])
async def ready() -> dict[str, str]:
    return {"status": "ok"}
