"""Betfairlightweight engine glue — auth, streaming, placement, settlement.

This is the I/O layer. Everything strategy-related lives in
``evaluator.py``. This module's only job is:

1. Authenticate with Betfair (certs from Secret Manager).
2. Subscribe to the GB+IE WIN market stream and maintain a market cache.
3. When a market enters the configured ``process_window_mins``, hand
   the snapshot to ``evaluator.evaluate`` and place / log the result.
4. Poll ``list_cleared_orders`` for settled bets, compute P&L, persist.

No abstractions over betfairlightweight — its classes are used
directly. The build spec is explicit on this: "Use it as-is."
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from queue import Queue
from typing import Any, Iterable, Optional

import betfairlightweight  # type: ignore[import-untyped]
from betfairlightweight import filters  # type: ignore[import-untyped]
from betfairlightweight.streaming import StreamListener  # type: ignore[import-untyped]
from google.cloud import secretmanager  # type: ignore[import-untyped]

from evaluator import evaluate
from gcs import DailyResults
from models import (
    EvaluationResult,
    MarketSnapshot,
    PlacedBet,
    SettledBet,
)
from rules import Runner  # type: ignore[import-not-found]
from settings import Mode, Settings

logger = logging.getLogger(__name__)


HORSE_RACING_EVENT_TYPE = "7"
PROJECT_ID = os.environ.get("GCP_PROJECT", "chiops")


# ──────────────────────────────────────────────────────────────────────────────
# Credentials
# ──────────────────────────────────────────────────────────────────────────────


def _read_secret(secret_id: str) -> str:
    """Read a secret value from Secret Manager."""

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def _build_trading() -> betfairlightweight.APIClient:
    """Build a betfairlightweight client with certs from Secret Manager.

    The certs live as PEM strings in Secret Manager and need to be on
    disk (file paths) for the underlying ``requests`` library.
    """

    username = _read_secret("betfair-username")
    password = _read_secret("betfair-password")
    app_key = _read_secret("betfair-app-key")
    cert_pem = _read_secret("betfair-cert-pem")
    key_pem = _read_secret("betfair-key-pem")

    # Write certs to a fresh temp dir per process.
    certs_dir = tempfile.mkdtemp(prefix="betfair-certs-")
    cert_path = os.path.join(certs_dir, "client-2048.crt")
    key_path = os.path.join(certs_dir, "client-2048.key")
    with open(cert_path, "w") as f:
        f.write(cert_pem)
    with open(key_path, "w") as f:
        f.write(key_pem)
    os.chmod(cert_path, 0o600)
    os.chmod(key_path, 0o600)

    return betfairlightweight.APIClient(
        username=username,
        password=password,
        app_key=app_key,
        certs=certs_dir,
        locale="uk",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Stream listener — receives MarketBook updates from Betfair
# ──────────────────────────────────────────────────────────────────────────────


class _MarketListener(StreamListener):
    """Drop-in StreamListener that calls ``on_market_book`` on every MCM.

    betfairlightweight's default listener maintains a ``MarketCache`` so
    we get fully-merged MarketBook objects on every update. We don't
    override that — we just hook on_process to feed the engine.
    """

    def __init__(self, on_market_book) -> None:
        super().__init__()
        self._on_market_book = on_market_book

    def on_process(self, output) -> None:  # type: ignore[override]
        """Called for each batch of stream events.

        ``output`` is whatever the stream emits — for the ``MARKET_SUB``
        request the cache yields ``MarketBook`` objects. We forward
        each one to the engine for windowing/evaluation.
        """

        super().on_process(output)
        for book in output or []:
            try:
                self._on_market_book(book)
            except Exception as exc:  # noqa: BLE001
                logger.warning("market book handler raised: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Engine — the orchestrator
# ──────────────────────────────────────────────────────────────────────────────


class Engine:
    """Single-process engine. One :class:`Engine` per Cloud Run container.

    Owns the betfairlightweight client, the stream thread, the
    settings, and the event queue the SSE endpoint reads from.
    """

    def __init__(self, results: DailyResults) -> None:
        self._results = results
        self._settings = Settings()
        self._trading: Optional[betfairlightweight.APIClient] = None
        self._stream = None
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_status = "DISCONNECTED"
        self._evaluated_markets: set[str] = set()
        # Event queue consumed by the SSE endpoint. Bounded so a slow
        # client cannot starve memory.
        self._events: Queue[dict[str, Any]] = Queue(maxsize=2000)
        self._settle_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._account_balance: float = 0.0
        self._account_exposure: float = 0.0
        self._placed: list[PlacedBet] = []
        self._started_at: Optional[str] = None
        # bet_id → {market_name, venue, race_time, runner_name, rule_applied}.
        # Populated in _place_real so _poll_settlement can hydrate the
        # SettledBet rows with full context (Betfair's list_cleared_orders
        # response only carries bet_id / selection_id / price / size / outcome
        # — not market_name, venue, race_time, or rule_applied).
        self._placement_context: dict[str, dict[str, Any]] = {}
        # Hydrate from today's persisted placements so the cache survives
        # a Cloud Run cold start (settlement can land hours after placement
        # for evening races).
        self._rehydrate_placement_context()

    # ── Public API used by main.py ─────────────────────────────────────

    @property
    def settings(self) -> Settings:
        return self._settings

    def replace_settings(self, new_settings: Settings) -> None:
        """Replace the current settings (e.g. from a PUT /admin/config)."""

        self._settings = new_settings

    def status(self) -> dict[str, Any]:
        return {
            "service": "fsu100-track1",
            "version": "1.0.0",
            "mode": self._settings.general.mode.value,
            "stream_status": self._stream_status,
            "markets_today": len(self._evaluated_markets),
            "bets_placed": len(self._placed),
            "pnl_today": self._results.summary().get("total_pnl", 0.0),
            "account_balance": self._account_balance,
            "account_exposure": self._account_exposure,
            "started_at": self._started_at,
            "timestamp": _iso_now(),
        }

    def control(self, action: str) -> dict[str, Any]:
        """Flip mode and (re)start/stop the stream.

        ``action`` is one of ``start`` (DRY_RUN), ``live``, ``stop``.
        """

        action = (action or "").lower()
        if action == "start":
            self._settings.general.mode = Mode.DRY_RUN
            self._ensure_stream_running()
        elif action == "live":
            self._settings.general.mode = Mode.LIVE
            self._ensure_stream_running()
        elif action == "stop":
            self._settings.general.mode = Mode.STOPPED
            self._stop_stream()
        else:
            raise ValueError(f"unknown action: {action!r}")
        return self.status()

    def events(self) -> Iterable[dict[str, Any]]:
        """Generator the SSE endpoint iterates over.

        Yields ``{type, data}`` dicts as soon as they arrive. Blocks
        on the queue when nothing to send; the caller times out and
        sends a heartbeat.
        """

        while not self._stop_flag.is_set():
            try:
                yield self._events.get(timeout=15)
            except Exception:  # noqa: BLE001
                yield {"type": "heartbeat", "data": _iso_now()}

    def shutdown(self) -> None:
        """Stop the stream + the settle thread cleanly on app shutdown."""

        self._stop_flag.set()
        self._stop_stream()

    # ── Stream lifecycle ───────────────────────────────────────────────

    def _ensure_stream_running(self) -> None:
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return
        self._stop_flag.clear()
        self._stream_thread = threading.Thread(
            target=self._run_stream, name="bf-stream", daemon=True
        )
        self._stream_thread.start()
        if self._settle_thread is None or not self._settle_thread.is_alive():
            self._settle_thread = threading.Thread(
                target=self._run_settle_loop, name="bf-settle", daemon=True
            )
            self._settle_thread.start()
        self._started_at = _iso_now()

    def _stop_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream.stop raised: %s", exc)
        self._stream_status = "DISCONNECTED"
        self._stream = None
        self._stream_thread = None

    def _run_stream(self) -> None:
        """Long-running thread — login, subscribe, listen until stopped."""

        try:
            self._stream_status = "CONNECTING"
            if self._trading is None:
                self._trading = _build_trading()
                self._trading.login()
            self._refresh_account()

            listener = _MarketListener(self._handle_market_book)
            market_filter = filters.streaming_market_filter(
                event_type_ids=[HORSE_RACING_EVENT_TYPE],
                country_codes=list(self._settings.general.countries),
                market_types=["WIN"],
            )
            data_filter = filters.streaming_market_data_filter(
                fields=[
                    "EX_BEST_OFFERS",
                    "EX_MARKET_DEF",
                    "EX_TRADED",
                    "EX_TRADED_VOL",
                    "SP_PROJECTED",
                ],
                ladder_levels=3,
            )
            self._stream = self._trading.streaming.create_stream(
                listener=listener
            )
            self._stream.subscribe_to_markets(
                market_filter=market_filter,
                market_data_filter=data_filter,
            )
            self._stream_status = "CONNECTED"
            self._stream.start()
        except Exception as exc:
            logger.exception("stream thread failed: %s", exc)
            self._stream_status = "ERROR"
            self._push_event(
                "error", {"detail": f"stream connect failed: {exc}"}
            )

    def _refresh_account(self) -> None:
        try:
            funds = self._trading.account.get_account_funds()
            self._account_balance = float(funds.available_to_bet_balance or 0)
            self._account_exposure = float(funds.exposure or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("account refresh failed: %s", exc)

    def _rehydrate_placement_context(self) -> None:
        """Rebuild the bet_id → context cache from today's persisted placements.

        Called on engine boot so settlement landing after a Cloud Run
        restart can still hydrate the rich SettledBet fields. Each
        placement row carries market_id + runner_name + rule_applied,
        and we look up market_name / venue / race_time from today's
        evaluations (keyed by market_id).
        """

        snapshot = self._results.snapshot()
        evals_by_market = {
            e.get("market_id"): e for e in snapshot.get("evaluations", [])
        }
        for placement in snapshot.get("placements", []):
            bet_id = placement.get("bet_id")
            if not bet_id:
                continue
            ev = evals_by_market.get(placement.get("market_id"), {})
            self._placement_context[bet_id] = {
                "market_id": placement.get("market_id", ""),
                "market_name": ev.get("market_name", ""),
                "venue": ev.get("venue", ""),
                "race_time": ev.get("race_time", ""),
                "runner_name": placement.get("runner_name", ""),
                "rule_applied": placement.get("rule_applied", ""),
            }

    # ── Per-market handling ────────────────────────────────────────────

    def _handle_market_book(self, book) -> None:  # type: ignore[no-untyped-def]
        """Called for each MarketBook update from the listener.

        Decides whether the market is inside the process window, builds
        a :class:`MarketSnapshot`, calls :func:`evaluator.evaluate`, and
        either places bets (LIVE) or logs them (DRY_RUN).
        """

        if self._settings.general.mode == Mode.STOPPED:
            return
        market_id = getattr(book, "market_id", None)
        if not market_id or market_id in self._evaluated_markets:
            return

        market_def = getattr(book, "market_definition", None)
        if market_def is None:
            return
        market_time = getattr(market_def, "market_time", None)
        if market_time is None:
            return

        if market_def.in_play or market_def.status != "OPEN":
            return

        # Window check. In LIVE the engine waits until the configured
        # process window (default 5 min before off) to take the snapshot
        # the strategy was designed around. In DRY_RUN we lift the upper
        # bound so the operator can validate the pipeline immediately
        # instead of waiting hours for races to enter the window — at
        # the cost that the prices the evaluator sees are whatever the
        # market shows on first sight, not the T-5min snapshot. Use this
        # for plumbing validation, not strategy-timing validation.
        # Lower bound (closed markets) is enforced in both modes.
        seconds_to_off = (
            market_time - datetime.now(tz=timezone.utc)
        ).total_seconds()
        if seconds_to_off < 0:
            return
        if self._settings.general.mode == Mode.LIVE:
            window = self._settings.general.process_window_mins * 60
            if seconds_to_off > window:
                return

        snapshot = _snapshot_from_book(book)
        result = evaluate(snapshot, self._settings)
        self._evaluated_markets.add(market_id)
        self._results.append_evaluation(result.to_dict())
        self._push_event("evaluation", result.to_dict())

        if result.skipped or not result.instructions:
            return

        if self._settings.general.mode == Mode.LIVE:
            self._place_real(result)
        else:
            self._place_simulated(result)

    def _place_real(self, result: EvaluationResult) -> None:
        for instr in result.instructions:
            try:
                report = self._trading.betting.place_orders(
                    market_id=result.market_id,
                    instructions=[instr.to_betfair_instruction()],
                    customer_strategy_ref="fsu100-track1",
                )
                bet_id = None
                if report.instruction_reports:
                    bet_id = report.instruction_reports[0].bet_id
                placed = PlacedBet(
                    market_id=result.market_id,
                    selection_id=instr.selection_id,
                    runner_name=instr.runner_name,
                    side="LAY",
                    price=instr.price,
                    stake=instr.size,
                    liability=instr.liability,
                    rule_applied=instr.rule_applied,
                    bet_id=bet_id,
                    simulated=False,
                )
                self._placed.append(placed)
                self._results.append_placement(placed.to_dict())
                self._push_event("placement", placed.to_dict())
                # Cache the rich context so _poll_settlement can hydrate
                # the SettledBet — Betfair's list_cleared_orders doesn't
                # carry market_name / venue / race_time / rule_applied.
                if bet_id:
                    self._placement_context[bet_id] = {
                        "market_id": result.market_id,
                        "market_name": result.market_name,
                        "venue": result.venue,
                        "race_time": result.race_time,
                        "runner_name": instr.runner_name,
                        "rule_applied": instr.rule_applied,
                    }
            except Exception as exc:
                logger.exception("place_orders failed: %s", exc)
                self._push_event(
                    "error",
                    {"market_id": result.market_id, "detail": str(exc)},
                )

    def _place_simulated(self, result: EvaluationResult) -> None:
        for instr in result.instructions:
            placed = PlacedBet(
                market_id=result.market_id,
                selection_id=instr.selection_id,
                runner_name=instr.runner_name,
                side="LAY",
                price=instr.price,
                stake=instr.size,
                liability=instr.liability,
                rule_applied=instr.rule_applied,
                bet_id=None,
                simulated=True,
            )
            self._placed.append(placed)
            self._results.append_placement(placed.to_dict())
            self._push_event("placement", placed.to_dict())

    # ── Settlement loop ────────────────────────────────────────────────

    def _run_settle_loop(self) -> None:
        """Poll list_cleared_orders every 5 minutes, mark settled bets."""

        while not self._stop_flag.is_set():
            try:
                if (
                    self._trading is not None
                    and self._settings.general.mode != Mode.STOPPED
                ):
                    self._poll_settlement()
                    self._refresh_account()
            except Exception as exc:  # noqa: BLE001
                logger.warning("settle loop raised: %s", exc)
            self._stop_flag.wait(timeout=300)  # 5 min

    def _poll_settlement(self) -> None:
        """Fetch settled bets from Betfair, append to daily results."""

        since = (datetime.now(tz=timezone.utc) - timedelta(hours=24)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            cleared = self._trading.betting.list_cleared_orders(
                bet_status="SETTLED",
                settled_date_range={"from": since},
                customer_strategy_refs=["fsu100-track1"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_cleared_orders failed: %s", exc)
            return

        existing_ids = {
            s.get("bet_id") for s in self._results.snapshot().get("settlements", [])
        }

        for order in cleared.cleared_orders:
            bet_id = order.bet_id
            if bet_id in existing_ids:
                continue
            # Betfair WIN_LOSE settles a LAY:
            # bet_outcome == "WON" → we won (runner LOST), profit = stake
            # bet_outcome == "LOST" → we lost (runner WON), loss = liability
            outcome = "VOID"
            pnl = 0.0
            stake = float(order.price_matched or 0) and float(order.size_settled or 0)
            if order.bet_outcome == "WON":
                outcome = "WON"
                pnl = float(order.profit or 0)
            elif order.bet_outcome == "LOST":
                outcome = "LOST"
                pnl = float(order.profit or 0)
            # Hydrate context from the placement cache (populated when the
            # bet was placed, or rehydrated from today's placements on boot).
            ctx = self._placement_context.get(bet_id, {})
            settled = SettledBet(
                market_id=ctx.get("market_id") or str(order.market_id),
                market_name=ctx.get("market_name", ""),
                venue=ctx.get("venue", ""),
                race_time=ctx.get("race_time", ""),
                selection_id=int(order.selection_id),
                runner_name=ctx.get("runner_name", ""),
                rule_applied=ctx.get("rule_applied", ""),
                side=order.side or "LAY",
                price=float(order.price_matched or 0),
                stake=float(order.size_settled or 0),
                liability=float(order.size_settled or 0)
                * (float(order.price_matched or 1) - 1),
                outcome=outcome,
                pnl=pnl,
                bet_id=bet_id,
                simulated=False,
            )
            self._results.append_settlement(settled.to_dict())
            self._push_event("settlement", settled.to_dict())

    # ── Event bus ──────────────────────────────────────────────────────

    def _push_event(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            self._events.put_nowait({"type": event_type, "data": data})
        except Exception:  # noqa: BLE001
            # Queue full; drop the event. SSE clients always have the
            # daily-results endpoint as a fallback.
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _snapshot_from_book(book) -> MarketSnapshot:  # type: ignore[no-untyped-def]
    """Convert a betfairlightweight ``MarketBook`` to a :class:`MarketSnapshot`."""

    md = book.market_definition
    runners_md = {r.selection_id: r for r in (md.runners or [])}

    runners: list[Runner] = []
    for r in book.runners:
        meta = runners_md.get(r.selection_id)
        name = (
            getattr(meta, "name", None)
            or getattr(meta, "runner_name", None)
            or f"selection_{r.selection_id}"
        )
        # betfairlightweight Runner has ex.available_to_back / available_to_lay
        # lists; first element is best of book.
        best_back = None
        best_lay = None
        ex = getattr(r, "ex", None)
        if ex is not None:
            atb = getattr(ex, "available_to_back", []) or []
            atl = getattr(ex, "available_to_lay", []) or []
            if atb:
                best_back = atb[0].price
            if atl:
                best_lay = atl[0].price
        runners.append(
            Runner(
                selection_id=r.selection_id,
                runner_name=name,
                best_available_to_lay=best_lay,
                best_available_to_back=best_back,
                status=getattr(r, "status", "ACTIVE") or "ACTIVE",
            )
        )

    venue = getattr(md, "venue", "") or ""
    country = getattr(md, "country_code", "") or ""
    race_time_dt = getattr(md, "market_time", None)
    race_time = race_time_dt.isoformat() if race_time_dt else ""
    name = getattr(md, "name", "") or ""

    return MarketSnapshot(
        market_id=str(book.market_id),
        market_name=name,
        venue=venue,
        country=country,
        race_time=race_time,
        snapshot_at=_iso_now(),
        runners=runners,
    )


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
