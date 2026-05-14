"""GCS-backed persistence for CLE V2 trading data.

All data lives under a single bucket — ``gs://chiops-clev2-trading/`` —
with the layout SC fixed for the V2 start:

    daily/{YYYY-MM-DD}.json       evaluations + placements + settlements
    settled/{YYYY-MM-DD}.json     only confirmed Betfair settled orders
    errors/{YYYY-MM-DD}.json      error events from the session
    snapshots/{YYYY-MM-DD}_start.json   engine status at session start
    snapshots/{YYYY-MM-DD}_end.json     engine status at session end
    markets/{YYYY-MM-DD}_catalogue.json runner names + venue per market
    settings/current.json         operator settings — single file, latest wins

The :class:`TradingStore` is the only writer for any of these. The
engine and the FastAPI layer interact with the store; the store talks
to GCS.

Atomicity model: each writer rewrites the whole file in full on every
call (it's small enough — a 100-bet day fits in <1MB). No append-only
streaming. The cost is a few extra bytes on the wire; the benefit is
zero risk of partial writes corrupting historic data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from google.cloud import storage  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


BUCKET_NAME = "chiops-clev2-trading"
SETTINGS_PATH = "settings/current.json"


# ──────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────────────────────────────


_LOCK = Lock()
_CLIENT: Optional[storage.Client] = None


def _client() -> storage.Client:
    """Lazy-init the storage client. One per process."""

    global _CLIENT
    if _CLIENT is None:
        _CLIENT = storage.Client()
    return _CLIENT


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────────────────
# TradingStore — the single GCS facade for all CLE V2 trading data
# ──────────────────────────────────────────────────────────────────────────────


class TradingStore:
    """All session data writes go through this object.

    Thread-safe: every method holds :data:`_LOCK` while touching the
    in-memory cache or GCS. Designed to be a single shared instance
    per process.
    """

    def __init__(self, bucket: str = BUCKET_NAME) -> None:
        self._bucket_name = bucket
        # In-memory cache for the daily file (the hottest read path —
        # the portal polls /api/results every 5s).
        self._evaluations: list[dict[str, Any]] = []
        self._placements: list[dict[str, Any]] = []
        self._settlements: list[dict[str, Any]] = []
        self._errors: list[dict[str, Any]] = []
        self._loaded_for_date: Optional[str] = None
        self._load_today()

    # ── Internal load / persist ────────────────────────────────────────

    def _load_today(self) -> None:
        """Hydrate today's cache from GCS on first access or date roll."""

        today = _today()
        if self._loaded_for_date == today:
            return
        # Reset everything before re-loading — date has rolled.
        self._evaluations = []
        self._placements = []
        self._settlements = []
        self._errors = []
        try:
            bucket = _client().bucket(self._bucket_name)
            daily = bucket.blob(f"daily/{today}.json")
            if daily.exists():
                data = json.loads(daily.download_as_text())
                self._evaluations = data.get("evaluations", []) or []
                self._placements = data.get("placements", []) or []
                self._settlements = data.get("settlements", []) or []
            errors = bucket.blob(f"errors/{today}.json")
            if errors.exists():
                edata = json.loads(errors.download_as_text())
                self._errors = edata.get("errors", []) or []
            logger.info(
                "hydrated TradingStore for %s — %d eval, %d placed, %d settled, %d errors",
                today,
                len(self._evaluations),
                len(self._placements),
                len(self._settlements),
                len(self._errors),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not hydrate TradingStore: %s", exc)
        self._loaded_for_date = today

    def _persist_daily(self) -> None:
        """Write the daily file (evals + placements + settlements). Caller holds the lock."""

        try:
            payload = {
                "date": self._loaded_for_date,
                "evaluations": self._evaluations,
                "placements": self._placements,
                "settlements": self._settlements,
            }
            blob = _client().bucket(self._bucket_name).blob(
                f"daily/{self._loaded_for_date}.json"
            )
            blob.upload_from_string(
                json.dumps(payload, default=str),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily persist failed: %s", exc)

    def _persist_settled(self) -> None:
        """Write the settled-only file. Mirror of daily.settlements."""

        try:
            payload = {
                "date": self._loaded_for_date,
                "settlements": self._settlements,
            }
            blob = _client().bucket(self._bucket_name).blob(
                f"settled/{self._loaded_for_date}.json"
            )
            blob.upload_from_string(
                json.dumps(payload, default=str),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("settled persist failed: %s", exc)

    def _persist_errors(self) -> None:
        try:
            payload = {
                "date": self._loaded_for_date,
                "errors": self._errors,
            }
            blob = _client().bucket(self._bucket_name).blob(
                f"errors/{self._loaded_for_date}.json"
            )
            blob.upload_from_string(
                json.dumps(payload, default=str),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("errors persist failed: %s", exc)

    # ── Public writers ─────────────────────────────────────────────────

    def append_evaluation(self, evaluation: dict[str, Any]) -> None:
        with _LOCK:
            self._load_today()
            self._evaluations.append(evaluation)
            self._persist_daily()

    def append_placement(self, placement: dict[str, Any]) -> None:
        with _LOCK:
            self._load_today()
            self._placements.append(placement)
            self._persist_daily()

    def append_settlement(self, settlement: dict[str, Any]) -> None:
        with _LOCK:
            self._load_today()
            self._settlements.append(settlement)
            # Both the daily file (full timeline) and the settled-only
            # file get the new row.
            self._persist_daily()
            self._persist_settled()

    def append_error(self, error: dict[str, Any]) -> None:
        """Persist any error event to the errors/{date}.json file."""

        with _LOCK:
            self._load_today()
            # Stamp the error so we can timeline it later.
            if "timestamp" not in error:
                error = dict(error)
                error["timestamp"] = datetime.now(tz=timezone.utc).isoformat()
            self._errors.append(error)
            self._persist_errors()

    def write_snapshot(self, status: dict[str, Any], kind: str) -> None:
        """Write the engine's status snapshot at session start or end.

        ``kind`` must be ``"start"`` or ``"end"`` — used in the filename.
        """

        if kind not in ("start", "end"):
            raise ValueError(f"snapshot kind must be 'start' or 'end', got {kind!r}")
        try:
            payload = {
                "date": _today(),
                "kind": kind,
                "captured_at": datetime.now(tz=timezone.utc).isoformat(),
                "status": status,
            }
            blob = _client().bucket(self._bucket_name).blob(
                f"snapshots/{_today()}_{kind}.json"
            )
            blob.upload_from_string(
                json.dumps(payload, default=str),
                content_type="application/json",
            )
            logger.info("wrote %s snapshot for %s", kind, _today())
        except Exception as exc:  # noqa: BLE001
            logger.warning("snapshot persist failed (%s): %s", kind, exc)

    def write_catalogue(self, catalogue: dict[str, Any]) -> None:
        """Write the market catalogue (runner names + venue per market) for today.

        ``catalogue`` shape: ``{market_id: {venue, race_time, runners: {selection_id: name}}}``.
        Called periodically and at shutdown.
        """

        try:
            payload = {
                "date": _today(),
                "captured_at": datetime.now(tz=timezone.utc).isoformat(),
                "markets": catalogue,
            }
            blob = _client().bucket(self._bucket_name).blob(
                f"markets/{_today()}_catalogue.json"
            )
            blob.upload_from_string(
                json.dumps(payload, default=str),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalogue persist failed: %s", exc)

    # ── Public readers ─────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of today's state for the portal's /api/results."""

        with _LOCK:
            self._load_today()
            return {
                "date": self._loaded_for_date,
                "evaluations": list(self._evaluations),
                "placements": list(self._placements),
                "settlements": list(self._settlements),
                "errors": list(self._errors),
            }

    def summary(self) -> dict[str, Any]:
        """Aggregate counts + totals for the portal summary panel."""

        with _LOCK:
            self._load_today()
            evals = self._evaluations
            placements = self._placements
            settled = self._settlements

            total_races = len({e.get("market_id") for e in evals})
            bets_placed = len(placements)
            skipped_rule_2a = sum(
                1 for e in evals
                if "Rule 2A stake=0" in (e.get("skip_reason") or "")
            )
            no_bet_outside_bands = sum(
                1 for e in evals
                if e.get("skipped") and "exceed" in (e.get("skip_reason") or "").lower()
            )
            won = sum(1 for s in settled if s.get("outcome") == "WON")
            lost = sum(1 for s in settled if s.get("outcome") == "LOST")
            void = sum(1 for s in settled if s.get("outcome") == "VOID")
            total_stake = sum(float(p.get("stake", 0)) for p in placements)
            total_liability = sum(float(p.get("liability", 0)) for p in placements)
            total_pnl = sum(float(s.get("pnl", 0)) for s in settled)
            strike = (won / (won + lost)) if (won + lost) > 0 else None
            roi = (total_pnl / total_stake) if total_stake > 0 else None

            return {
                "date": self._loaded_for_date,
                "total_races": total_races,
                "bets_placed": bets_placed,
                "skipped_rule_2a": skipped_rule_2a,
                "no_bet_outside_bands": no_bet_outside_bands,
                "won": won,
                "lost": lost,
                "void": void,
                "total_stake": round(total_stake, 2),
                "total_liability": round(total_liability, 2),
                "total_pnl": round(total_pnl, 2),
                "strike_rate": round(strike * 100, 1) if strike is not None else None,
                "roi": round(roi * 100, 1) if roi is not None else None,
            }


# ──────────────────────────────────────────────────────────────────────────────
# Settings persistence — independent of the trading store
# ──────────────────────────────────────────────────────────────────────────────


def save_settings(settings_dict: dict[str, Any], bucket: str = BUCKET_NAME) -> bool:
    """Persist operator settings to ``settings/current.json``.

    Returns ``True`` if the write succeeded; ``False`` on any failure.
    The PUT /admin/config handler should refuse to return 200 if this
    returns False — operator must know the change didn't persist.
    """

    try:
        payload = {
            "saved_at": datetime.now(tz=timezone.utc).isoformat(),
            "settings": settings_dict,
        }
        blob = _client().bucket(bucket).blob(SETTINGS_PATH)
        blob.upload_from_string(
            json.dumps(payload, default=str),
            content_type="application/json",
        )
        logger.info("settings persisted to gs://%s/%s", bucket, SETTINGS_PATH)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("settings persist FAILED: %s", exc)
        return False


def load_settings(bucket: str = BUCKET_NAME) -> Optional[dict[str, Any]]:
    """Read persisted settings. Returns the inner ``settings`` dict or None.

    None means either (a) the file doesn't exist (first boot) or (b)
    GCS was unreachable. The lifespan handler treats both as "use
    defaults" — we'd rather boot than block on settings.
    """

    try:
        blob = _client().bucket(bucket).blob(SETTINGS_PATH)
        if not blob.exists():
            logger.info("no persisted settings — first boot, using defaults")
            return None
        data = json.loads(blob.download_as_text())
        return data.get("settings")
    except Exception as exc:  # noqa: BLE001
        logger.warning("settings load failed (will use defaults): %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Backwards-compat alias — DailyResults was the old class name.
# Kept so anything still importing it gets the new behaviour.
# ──────────────────────────────────────────────────────────────────────────────


DailyResults = TradingStore


def load_results_for_date(
    date_str: str, bucket: str = BUCKET_NAME
) -> dict[str, Any]:
    """Read the daily file for an arbitrary historic day from GCS."""

    blob = _client().bucket(bucket).blob(f"daily/{date_str}.json")
    if not blob.exists():
        return {
            "date": date_str,
            "evaluations": [],
            "placements": [],
            "settlements": [],
        }
    return json.loads(blob.download_as_text())
