"""Wrapper over the betfairlightweight client for live operations.

Owns the Betfair session for the lifetime of the engine: cert-based login,
the streaming connection, REST calls for orders, and account access. Every
call into ``betfairlightweight`` goes through this module so that error
handling, structured logging, and cleanup are consistent.

The service is intentionally thin — the engine in :mod:`engine` decides
what to subscribe to, when to place a bet, and how to settle results;
this module simply translates those decisions into HTTP/socket calls.
"""

from __future__ import annotations

import queue
import shutil
import tempfile
import threading
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Iterable

import betfairlightweight
from betfairlightweight import StreamListener
from betfairlightweight.exceptions import BetfairError
from betfairlightweight.filters import (
    cancel_instruction,
    limit_order,
    place_instruction,
    streaming_market_data_filter,
    streaming_market_filter,
    time_range,
)
from betfairlightweight.streaming.betfairstream import BetfairStream

from core.logging import get_logger
from services.secrets_service import BetfairCredentials, SecretsService

logger = get_logger(__name__)


class BetfairServiceError(RuntimeError):
    """Raised when a Betfair API call returns a usable error or fails outright."""


class StreamNotRunningError(BetfairServiceError):
    """Raised when a caller expects an active stream but none is running."""


class BetfairService:
    """Owns the live Betfair connection and exposes typed helpers."""

    def __init__(
        self,
        secrets: SecretsService | None = None,
        max_latency_seconds: float = 2.0,
    ) -> None:
        self._secrets = secrets or SecretsService()
        self._max_latency = max_latency_seconds
        self._lock = threading.RLock()
        self._trading: betfairlightweight.APIClient | None = None
        self._cert_dir: Path | None = None
        self._stream: BetfairStream | None = None
        self._stream_thread: threading.Thread | None = None
        self._listener: StreamListener | None = None
        self._output_queue: queue.Queue[list[Any]] | None = None
        self._stream_unique_id: int | None = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def login(self) -> None:
        """Fetch credentials, write the cert bundle to disk, and log in.

        Idempotent: a second call while already logged in is a no-op.
        """

        with self._lock:
            if self._trading is not None:
                logger.debug("login() called while already authenticated")
                return

            creds: BetfairCredentials = self._secrets.get_betfair_credentials()
            cert_dir = Path(tempfile.mkdtemp(prefix="bf_certs_"))
            cert_path = cert_dir / "client.crt"
            key_path = cert_dir / "client.key"
            try:
                cert_path.write_text(creds.cert_pem)
                key_path.write_text(creds.key_pem)
                cert_path.chmod(0o600)
                key_path.chmod(0o600)
                trading = betfairlightweight.APIClient(
                    username=creds.username,
                    password=creds.password,
                    app_key=creds.app_key,
                    certs=str(cert_dir),
                )
                trading.login()
            except Exception:
                shutil.rmtree(cert_dir, ignore_errors=True)
                raise
            self._trading = trading
            self._cert_dir = cert_dir
            logger.info("betfair session established")

    def logout(self) -> None:
        """Close the session and remove the on-disk credentials.

        Stops the stream first if it is running. Errors during logout are
        logged but not propagated — the caller has already decided to tear
        the session down.
        """

        with self._lock:
            self.stop_stream()
            if self._trading is not None:
                try:
                    self._trading.logout()
                except BetfairError:
                    logger.warning("betfair logout returned an error", exc_info=True)
                self._trading = None
            if self._cert_dir is not None:
                shutil.rmtree(self._cert_dir, ignore_errors=True)
                self._cert_dir = None
            logger.info("betfair session closed")

    @property
    def is_authenticated(self) -> bool:
        """True when a Betfair session is open."""

        with self._lock:
            return self._trading is not None

    def _require_trading(self) -> betfairlightweight.APIClient:
        with self._lock:
            if self._trading is None:
                raise BetfairServiceError("not authenticated; call login() first")
            return self._trading

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def start_stream(
        self,
        *,
        event_type_id: str,
        countries: Iterable[str],
        market_types: Iterable[str],
        conflate_ms: int = 250,
        heartbeat_ms: int = 5_000,
    ) -> queue.Queue[list[Any]]:
        """Open the live market stream and return its output queue.

        The stream runs on a background daemon thread. ``MarketBook`` lists
        are pushed to the returned queue as updates arrive — the engine
        consumes them in :meth:`engine.LiveEngine._processing_loop`.
        """

        trading = self._require_trading()
        with self._lock:
            if self._stream is not None and self._stream.running:
                logger.debug("start_stream() called while stream already running")
                assert self._output_queue is not None
                return self._output_queue

            output_queue: queue.Queue[list[Any]] = queue.Queue()
            listener = StreamListener(
                output_queue=output_queue,
                max_latency=self._max_latency,
            )
            stream = trading.streaming.create_stream(listener=listener)

            market_filter = streaming_market_filter(
                event_type_ids=[event_type_id],
                country_codes=list(countries) or None,
                market_types=list(market_types) or None,
            )
            data_filter = streaming_market_data_filter(
                fields=[
                    "EX_MARKET_DEF",
                    "EX_BEST_OFFERS",
                    "EX_TRADED",
                    "EX_LTP",
                    "SP_PROJECTED",
                    "SP_TRADED",
                ],
                ladder_levels=3,
            )
            unique_id = stream.subscribe_to_markets(
                market_filter=market_filter,
                market_data_filter=data_filter,
                conflate_ms=conflate_ms,
                heartbeat_ms=heartbeat_ms,
            )

            thread = threading.Thread(
                target=self._stream_runner,
                args=(stream,),
                name="betfair-stream",
                daemon=True,
            )
            thread.start()

            self._stream = stream
            self._stream_thread = thread
            self._listener = listener
            self._output_queue = output_queue
            self._stream_unique_id = unique_id
            logger.info(
                "betfair stream started",
                extra={
                    "event_type_id": event_type_id,
                    "countries": list(countries),
                    "market_types": list(market_types),
                    "unique_id": unique_id,
                },
            )
            return output_queue

    def _stream_runner(self, stream: BetfairStream) -> None:
        """Background thread target — runs the stream's blocking read loop."""

        try:
            stream.start()
        except Exception:
            logger.exception("betfair stream terminated unexpectedly")

    def stop_stream(self) -> None:
        """Stop the streaming socket and join the worker thread.

        Safe to call multiple times. The output queue is intentionally
        retained so the consumer can drain any remaining updates.
        """

        with self._lock:
            stream = self._stream
            thread = self._stream_thread
            self._stream = None
            self._stream_thread = None
            self._listener = None
            self._stream_unique_id = None

        if stream is not None and stream.running:
            try:
                stream.stop()
            except Exception:
                logger.warning("error stopping betfair stream", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.info("betfair stream stopped")

    @property
    def stream_running(self) -> bool:
        """True when the streaming socket is connected and running."""

        with self._lock:
            return self._stream is not None and self._stream.running

    @property
    def listener(self) -> StreamListener | None:
        """Return the active :class:`StreamListener`, if any."""

        with self._lock:
            return self._listener

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_lay_order(
        self,
        *,
        market_id: str,
        selection_id: int,
        price: float,
        size: float,
        persistence_type: str = "LAPSE",
        customer_strategy_ref: str | None = None,
        customer_order_ref: str | None = None,
        customer_ref: str | None = None,
    ) -> Any:
        """Place a single LAY limit order on Betfair."""

        trading = self._require_trading()
        instruction = place_instruction(
            order_type="LIMIT",
            selection_id=selection_id,
            side="LAY",
            limit_order=limit_order(
                price=price,
                size=size,
                persistence_type=persistence_type,
            ),
            customer_order_ref=customer_order_ref,
        )
        try:
            return trading.betting.place_orders(
                market_id=market_id,
                instructions=[instruction],
                customer_ref=customer_ref,
                customer_strategy_ref=customer_strategy_ref,
            )
        except BetfairError as exc:
            raise BetfairServiceError(
                f"place_orders failed for market {market_id}: {exc}"
            ) from exc

    def cancel_all_orders(self, customer_ref: str | None = None) -> Any:
        """Cancel every open bet across every market.

        ``trading.betting.cancel_orders()`` with no market_id and no
        instructions issues a platform-wide cancel — the kill-switch
        primitive. Returns the betfairlightweight CancelOrders response
        so the caller can audit what was actioned.
        """

        trading = self._require_trading()
        try:
            return trading.betting.cancel_orders(customer_ref=customer_ref)
        except BetfairError as exc:
            raise BetfairServiceError(
                f"cancel_all_orders failed: {exc}"
            ) from exc

    def cancel_order(
        self,
        *,
        market_id: str,
        bet_id: str,
        size_reduction: float | None = None,
        customer_ref: str | None = None,
    ) -> Any:
        """Cancel (or partially cancel) an open order."""

        trading = self._require_trading()
        instruction = cancel_instruction(
            bet_id=bet_id,
            size_reduction=size_reduction,
        )
        try:
            return trading.betting.cancel_orders(
                market_id=market_id,
                instructions=[instruction],
                customer_ref=customer_ref,
            )
        except BetfairError as exc:
            raise BetfairServiceError(
                f"cancel_orders failed for bet {bet_id}: {exc}"
            ) from exc

    def list_current_orders(
        self,
        *,
        market_ids: Iterable[str] | None = None,
        customer_strategy_refs: Iterable[str] | None = None,
    ) -> Any:
        """Return the live current-orders report."""

        trading = self._require_trading()
        try:
            return trading.betting.list_current_orders(
                market_ids=list(market_ids) if market_ids else None,
                customer_strategy_refs=(
                    list(customer_strategy_refs) if customer_strategy_refs else None
                ),
            )
        except BetfairError as exc:
            raise BetfairServiceError(
                f"list_current_orders failed: {exc}"
            ) from exc

    def list_cleared_orders(
        self,
        *,
        from_day: date,
        to_day: date,
        customer_strategy_refs: Iterable[str] | None = None,
        from_record: int = 0,
        record_count: int = 1_000,
    ) -> Any:
        """Return cleared orders between ``from_day`` and ``to_day`` (inclusive)."""

        trading = self._require_trading()
        start_dt = datetime.combine(from_day, dt_time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(to_day, dt_time.max, tzinfo=timezone.utc)
        date_range = time_range(from_=start_dt, to=end_dt)
        try:
            return trading.betting.list_cleared_orders(
                bet_status="SETTLED",
                settled_date_range=date_range,
                customer_strategy_refs=(
                    list(customer_strategy_refs) if customer_strategy_refs else None
                ),
                from_record=from_record,
                record_count=record_count,
            )
        except BetfairError as exc:
            raise BetfairServiceError(
                f"list_cleared_orders failed: {exc}"
            ) from exc

    def list_market_profit_and_loss(self, market_ids: Iterable[str]) -> Any:
        """Return live profit and loss per OPEN market."""

        trading = self._require_trading()
        ids = list(market_ids)
        if not ids:
            return []
        try:
            return trading.betting.list_market_profit_and_loss(
                market_ids=ids,
                include_settled_bets=True,
                include_bsp_bets=True,
                net_of_commission=False,
            )
        except BetfairError as exc:
            raise BetfairServiceError(
                f"list_market_profit_and_loss failed: {exc}"
            ) from exc

    def list_market_book(self, market_ids: Iterable[str]) -> Any:
        """Return a current snapshot of one or more markets."""

        trading = self._require_trading()
        ids = list(market_ids)
        if not ids:
            return []
        try:
            return trading.betting.list_market_book(market_ids=ids)
        except BetfairError as exc:
            raise BetfairServiceError(
                f"list_market_book failed: {exc}"
            ) from exc

    def list_market_catalogue(
        self,
        market_ids: Iterable[str],
        max_results: int = 100,
    ) -> Any:
        """Return market catalogue entries (event, market name, runner names).

        The streaming MCM feed does not include runner names, so we fetch
        the catalogue once per market via the betting REST API and cache
        the names for the lifetime of the cache entry. ``max_results`` is
        the Betfair API per-call limit.
        """

        trading = self._require_trading()
        ids = list(market_ids)
        if not ids:
            return []
        from betfairlightweight.filters import market_filter as _mf  # noqa: PLC0415

        try:
            return trading.betting.list_market_catalogue(
                filter=_mf(market_ids=ids),
                market_projection=[
                    "MARKET_DESCRIPTION",
                    "RUNNER_DESCRIPTION",
                    "EVENT",
                    "EVENT_TYPE",
                ],
                max_results=max_results,
            )
        except BetfairError as exc:
            raise BetfairServiceError(
                f"list_market_catalogue failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account_funds(self, wallet: str | None = None) -> Any:
        """Return the available-to-bet snapshot for ``wallet``."""

        trading = self._require_trading()
        try:
            return trading.account.get_account_funds(wallet=wallet)
        except BetfairError as exc:
            raise BetfairServiceError(
                f"get_account_funds failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def keepalive(self) -> None:
        """Best-effort keepalive — silently ignored if not authenticated."""

        with self._lock:
            trading = self._trading
        if trading is None:
            return
        try:
            trading.keep_alive()
        except BetfairError:
            logger.warning("betfair keep_alive failed", exc_info=True)


