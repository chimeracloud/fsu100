"""GCS persistence for FSU100 results.

Writes a JSON document per settled bet plus a daily summary to the configured
results bucket. The daily JSONL ``settled-YYYY-MM-DD.jsonl`` file lets the
portal scan a date range without rehydrating the engine state from memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from google.api_core.exceptions import NotFound, PermissionDenied
from google.cloud import storage

from core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GcsLocation:
    """A bucket name + blob name pair."""

    bucket: str
    blob_name: str


class GcsService:
    """Synchronous GCS access — wrap calls in ``asyncio.to_thread`` from async code."""

    def __init__(self, client: storage.Client | None = None) -> None:
        self._client = client or storage.Client()

    def upload_text(
        self,
        bucket: str,
        blob_name: str,
        text: str,
        content_type: str = "application/json",
    ) -> None:
        """Upload a text payload, overwriting any existing blob."""

        try:
            blob = self._client.bucket(bucket).blob(blob_name)
            blob.upload_from_string(text, content_type=content_type)
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account cannot write gs://{bucket}/{blob_name}"
            ) from exc

    def append_jsonl(
        self,
        bucket: str,
        blob_name: str,
        line: str,
    ) -> None:
        """Append a single newline-terminated line to a JSONL blob.

        Reads the existing object (if any), appends the line, and rewrites
        it. Adequate for the engine's expected throughput (well under one
        write per second); not safe for concurrent writers.
        """

        try:
            blob = self._client.bucket(bucket).blob(blob_name)
            try:
                existing = blob.download_as_bytes().decode("utf-8")
            except NotFound:
                existing = ""
            if existing and not existing.endswith("\n"):
                existing += "\n"
            payload = (existing + line).rstrip("\n") + "\n"
            blob.upload_from_string(payload, content_type="application/x-ndjson")
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account cannot write gs://{bucket}/{blob_name}"
            ) from exc

    def download_text(self, bucket: str, blob_name: str) -> str | None:
        """Return blob contents as a string, or ``None`` if missing."""

        try:
            blob = self._client.bucket(bucket).blob(blob_name)
            return blob.download_as_text()
        except NotFound:
            return None
        except PermissionDenied as exc:
            raise RuntimeError(
                f"service account cannot read gs://{bucket}/{blob_name}"
            ) from exc

    def list_blob_names(self, bucket: str, prefix: str) -> list[str]:
        """List blob names within a bucket under ``prefix``."""

        try:
            iterator = self._client.list_blobs(bucket, prefix=prefix)
        except (NotFound, PermissionDenied) as exc:
            raise RuntimeError(
                f"cannot list gs://{bucket}/{prefix}: {exc}"
            ) from exc
        return [b.name for b in iterator]

    @staticmethod
    def settled_blob_name(day: date) -> str:
        """Return the canonical blob name for a day's settled bets."""

        return f"settled/{day.isoformat()}.jsonl"

    @staticmethod
    def daily_summary_blob_name(day: date) -> str:
        """Return the canonical blob name for a day's summary stats."""

        return f"summary/{day.isoformat()}.json"

    @staticmethod
    def activity_blob_name(day: date) -> str:
        """Return the canonical blob name for a day's activity log."""

        return f"activity/{day.isoformat()}.jsonl"


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    """Parse a JSONL text payload into a list of objects.

    Defensive against trailing newlines and blank lines.
    """

    import json

    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            out.append(json.loads(stripped))
        except json.JSONDecodeError:
            logger.warning("skipping malformed JSONL line", extra={"line": stripped[:200]})
    return out
