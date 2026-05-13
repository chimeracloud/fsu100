"""Daily results persistence to GCS.

Track 1 writes a single JSON file per UTC trading day to
``gs://chiops-fsu100-results/track1/daily/{YYYY-MM-DD}.json``. The file
holds three lists:

* ``evaluations`` — one entry per market the evaluator processed
* ``placements``  — one entry per bet placed (or simulated in DRY_RUN)
* ``settlements`` — one entry per bet settled by Betfair

The file is rewritten in full on every update — small enough that
streaming-append isn't worth the complexity at this stage. Each daily
file caps at ~1MB even on a 100-bet day.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from google.cloud import storage  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


_LOCK = Lock()
_CLIENT: storage.Client | None = None


def _client() -> storage.Client:
    """Lazy-init the storage client. One client per process."""

    global _CLIENT
    if _CLIENT is None:
        _CLIENT = storage.Client()
    return _CLIENT


def _today_path() -> str:
    """Path to the current UTC trading day's results file."""

    return f"track1/daily/{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}.json"


class DailyResults:
    """In-memory cache of today's results, persisted to GCS on every write.

    Thread-safe via :class:`threading.Lock`. The portal reads this for the
    live feed and summary; GCS is the durable backing store so a Cloud
    Run restart doesn't lose the day's data.
    """

    def __init__(self, bucket: str = "chiops-fsu100-results") -> None:
        self._bucket_name = bucket
        self._evaluations: list[dict[str, Any]] = []
        self._placements: list[dict[str, Any]] = []
        self._settlements: list[dict[str, Any]] = []
        self._loaded_for_date: str | None = None
        self._load_today()

    def _load_today(self) -> None:
        """Hydrate today's data from GCS (called on startup + on date roll)."""

        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if self._loaded_for_date == today:
            return
        try:
            blob = _client().bucket(self._bucket_name).blob(_today_path())
            if blob.exists():
                data = json.loads(blob.download_as_text())
                self._evaluations = data.get("evaluations", [])
                self._placements = data.get("placements", [])
                self._settlements = data.get("settlements", [])
                logger.info(
                    "loaded daily results from GCS",
                    extra={
                        "evaluations": len(self._evaluations),
                        "placements": len(self._placements),
                        "settlements": len(self._settlements),
                    },
                )
            else:
                self._evaluations = []
                self._placements = []
                self._settlements = []
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not hydrate daily results: %s", exc)
            self._evaluations = []
            self._placements = []
            self._settlements = []
        self._loaded_for_date = today

    def _persist(self) -> None:
        """Write the current snapshot to GCS. Caller must hold the lock."""

        try:
            payload = {
                "date": self._loaded_for_date,
                "evaluations": self._evaluations,
                "placements": self._placements,
                "settlements": self._settlements,
            }
            blob = _client().bucket(self._bucket_name).blob(_today_path())
            blob.upload_from_string(
                json.dumps(payload, default=str),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist daily results: %s", exc)

    def append_evaluation(self, evaluation: dict[str, Any]) -> None:
        with _LOCK:
            self._load_today()
            self._evaluations.append(evaluation)
            self._persist()

    def append_placement(self, placement: dict[str, Any]) -> None:
        with _LOCK:
            self._load_today()
            self._placements.append(placement)
            self._persist()

    def append_settlement(self, settlement: dict[str, Any]) -> None:
        with _LOCK:
            self._load_today()
            self._settlements.append(settlement)
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        """Return a deep-enough copy of today's data for the portal."""

        with _LOCK:
            self._load_today()
            return {
                "date": self._loaded_for_date,
                "evaluations": list(self._evaluations),
                "placements": list(self._placements),
                "settlements": list(self._settlements),
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
                1
                for e in evals
                if "Rule 2A stake=0" in (e.get("skip_reason") or "")
            )
            no_bet_outside_bands = sum(
                1
                for e in evals
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


def load_results_for_date(
    date_str: str, bucket: str = "chiops-fsu100-results"
) -> dict[str, Any]:
    """Read results for an arbitrary historic day from GCS.

    Returns an empty payload if the day's file does not exist.
    """

    blob = _client().bucket(bucket).blob(f"track1/daily/{date_str}.json")
    if not blob.exists():
        return {
            "date": date_str,
            "evaluations": [],
            "placements": [],
            "settlements": [],
        }
    return json.loads(blob.download_as_text())
