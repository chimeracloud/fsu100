"""Runtime configuration for the FSU100 betting engine.

Settings are loaded from environment variables and validated via Pydantic.
A single :class:`AppSettings` instance is exposed via :func:`get_settings`
and is intended to be cached for the lifetime of the process.

Settings here govern infrastructure (logging, bucket names, plugin
directory). Strategy parameters are read from the active plugin and from
the runtime config managed by :mod:`core.engine_config`, not from this
module.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Top-level application settings.

    All values are optional with sensible defaults so the service can boot
    in any environment that has the required GCP IAM bindings in place.
    """

    model_config = SettingsConfigDict(
        env_prefix="CHIMERA_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = Field(
        default="chimera-fsu100",
        description="Logical name of the service, used for logs and traces.",
    )
    version: str = Field(
        default="1.2.0",
        description="Semantic version of the deployed service.",
    )
    environment: Literal["development", "staging", "production"] = Field(
        default="production",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")

    gcp_project: str = Field(
        default="chiops",
        description="GCP project that hosts secrets and result storage.",
    )
    gcp_region: str = Field(default="europe-west2")

    results_bucket: str = Field(
        default="chiops-fsu100-results",
        description="GCS bucket where settled bets and daily summaries are persisted.",
    )

    plugins_dir: Path = Field(
        default=Path(__file__).resolve().parent.parent / "plugins",
        description="Directory scanned for strategy plugin JSON files.",
    )
    work_dir: Path = Field(
        default=Path("/tmp/chimera-fsu100"),
        description="Scratch directory used for the Betfair cert/key bundle.",
    )

    activity_log_size: int = Field(
        default=200,
        ge=10,
        le=1000,
        description="Maximum number of activity events held in memory.",
    )
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "https://chimerasportstrading.com",
            "https://www.chimerasportstrading.com",
        ],
        description=(
            "Origins permitted to issue cross-origin requests against the API. "
            "Set CHIMERA_CORS_ALLOWED_ORIGINS to a JSON array to override."
        ),
    )
    default_active_plugin: str = Field(
        default="mark_4rule_lay_v1",
        description="Plugin loaded into the engine at boot.",
    )
    customer_strategy_ref: str = Field(
        default="fsu100",
        max_length=15,
        description="String tagged onto every order for downstream tracking.",
    )
    event_type_id: str = Field(
        default="7",
        description="Betfair event type id for the live stream filter (7 = Horse Racing).",
    )
    order_polling_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description="Interval between current-order refreshes from Betfair.",
    )
    settlement_polling_seconds: float = Field(
        default=300.0,
        gt=0.0,
        description="Interval between cleared-order pulls from Betfair.",
    )
    stream_max_latency_seconds: float = Field(
        default=2.0,
        gt=0.0,
        description="Latency threshold above which warnings are logged.",
    )
    stream_conflate_ms: int = Field(
        default=250,
        ge=0,
        le=120_000,
        description="Conflation interval requested from the streaming API.",
    )
    stream_heartbeat_ms: int = Field(
        default=5_000,
        ge=500,
        le=5_000,
        description="Heartbeat interval requested from the streaming API.",
    )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return the process-wide :class:`AppSettings` singleton."""

    settings = AppSettings()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return settings
