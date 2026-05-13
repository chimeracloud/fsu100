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
from gcs import DailyResults, load_results_for_date
from settings import Settings

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
    results = DailyResults()
    engine = Engine(results)
    app.state.engine = engine
    app.state.results = results
    logger.info("track1 engine ready (mode=STOPPED)")
    try:
        yield
    finally:
        engine.shutdown()
        logger.info("track1 engine shut down")


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
    """Replace the current settings. Tolerates partial dicts — anything
    not supplied falls back to the dataclass default."""

    try:
        new_settings = Settings.from_dict(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid settings: {exc}") from exc
    app.state.engine.replace_settings(new_settings)
    return new_settings.to_dict()


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
