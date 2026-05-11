"""Discovery and validation of strategy plugins from the plugins directory.

Plugins are JSON documents that conform to :class:`PluginConfig`. Each one
sits in ``plugins/<name>.json`` and is loaded eagerly at startup; new
plugins added at runtime are picked up by re-calling
:meth:`PluginStore.refresh`.

The schema served by :meth:`PluginStore.schema_for` is identical to the
Backtest Tool's ``/api/plugins/{name}/schema`` so the same portal form
renders correctly against either service.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from core.config import get_settings
from core.logging import get_logger
from models.schemas import (
    PluginConfig,
    StrategyFieldSchema,
    StrategyInfo,
    StrategySchema,
)

logger = get_logger(__name__)


class PluginNotFoundError(LookupError):
    """Raised when a plugin name does not match any installed plugin."""


class PluginLoadError(RuntimeError):
    """Raised when a plugin file fails validation."""


class PluginStore:
    """In-memory registry of installed strategy plugins."""

    def __init__(self, plugins_dir: Path) -> None:
        self._dir = plugins_dir
        self._plugins: dict[str, PluginConfig] = {}
        self.refresh()

    def refresh(self) -> None:
        """Re-scan the plugins directory and rebuild the registry."""

        if not self._dir.exists():
            logger.warning("plugins directory missing", extra={"path": str(self._dir)})
            self._plugins = {}
            return

        loaded: dict[str, PluginConfig] = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                plugin = PluginConfig.model_validate(data)
            except json.JSONDecodeError as exc:
                logger.error(
                    "plugin file is not valid JSON",
                    extra={"path": str(path), "error": str(exc)},
                )
                continue
            except ValidationError as exc:
                logger.error(
                    "plugin file failed schema validation",
                    extra={"path": str(path), "errors": exc.errors()},
                )
                continue
            loaded[plugin.name] = plugin
        self._plugins = loaded
        logger.info("plugins loaded", extra={"count": len(loaded)})

    def list(self) -> list[StrategyInfo]:
        """Return a list of installed plugins for the GUI listing endpoint."""

        return [
            StrategyInfo(
                name=p.name,
                version=p.version,
                description=p.description,
                rule_count=len(p.strategy.rules),
            )
            for p in self._plugins.values()
        ]

    def get(self, name: str) -> PluginConfig:
        """Return the full plugin config for ``name``.

        Raises:
            PluginNotFoundError: if no plugin by that name is installed.
        """

        plugin = self._plugins.get(name)
        if plugin is None:
            raise PluginNotFoundError(name)
        return plugin

    def upsert(self, plugin: PluginConfig) -> None:
        """Insert or replace a plugin in the in-memory registry.

        Used by the Strategy-page SAVE flow: the API layer validates the
        payload, calls ``upsert`` to make the plugin immediately
        visible to GET endpoints, then mirrors it to GCS for durability.
        Re-running :meth:`refresh` afterwards would wipe this entry
        unless the GCS hydration step re-adds it — see engine startup.
        """

        self._plugins[plugin.name] = plugin

    def remove(self, name: str) -> bool:
        """Remove a plugin from the in-memory registry.

        Returns True if the plugin was present, False if no plugin by
        that name was registered (idempotent). The caller is responsible
        for deleting the GCS copy too.
        """

        return self._plugins.pop(name, None) is not None

    def schema_for(self, name: str) -> StrategySchema:
        """Return the editor schema served by GET /api/strategies/{name}/schema."""

        plugin = self.get(name)
        return StrategySchema(
            name=plugin.name,
            version=plugin.version,
            description=plugin.description,
            source_fields=_SOURCE_FIELDS,
            parser_fields=_PARSER_FIELDS,
            rule_fields=_RULE_FIELDS,
            control_fields=_CONTROL_FIELDS,
            staking_fields=_STAKING_FIELDS,
            defaults=plugin.model_dump(mode="json"),
        )


_SOURCE_FIELDS: list[StrategyFieldSchema] = [
    StrategyFieldSchema(
        name="type",
        type="string",
        required=True,
        enum=["gcs", "betfair_historic"],
        default="gcs",
        description=(
            "Backtest source mode. FSU100 ignores this field — it always "
            "reads the live Betfair stream — but it is preserved on the "
            "schema for cross-tool plugin compatibility."
        ),
    ),
    StrategyFieldSchema(
        name="filters.countries",
        type="array<string>",
        required=False,
        default=[],
        description=(
            "ISO country codes that drive the live streaming_market_filter, "
            "e.g. ['GB', 'IE']."
        ),
    ),
    StrategyFieldSchema(
        name="filters.market_types",
        type="array<string>",
        required=False,
        default=[],
        description=(
            "Betfair market types that drive the live streaming_market_filter, "
            "e.g. ['WIN', 'PLACE']."
        ),
    ),
    StrategyFieldSchema(
        name="filters.sport",
        type="string",
        required=False,
        default="Horse Racing",
        description="Sport name (kept for plugin compatibility, not used live).",
    ),
]


_PARSER_FIELDS: list[StrategyFieldSchema] = [
    StrategyFieldSchema(
        name="format",
        type="string",
        required=True,
        enum=["betfair_mcm"],
        default="betfair_mcm",
        description=(
            "Source data format — ignored by FSU100 because the live stream "
            "always supplies the same wire format."
        ),
    ),
    StrategyFieldSchema(
        name="time_before_off_seconds",
        type="integer",
        required=True,
        default=300,
        minimum=0,
        maximum=86_400,
        description="Seconds before market off-time at which to evaluate.",
    ),
    StrategyFieldSchema(
        name="price_field",
        type="string",
        required=True,
        enum=["ltp", "back", "lay"],
        default="ltp",
        description="Price field used to identify the favourite.",
    ),
    StrategyFieldSchema(
        name="extract_bsp",
        type="boolean",
        required=False,
        default=True,
        description="Settle bets using actual BSP when available.",
    ),
]


_RULE_FIELDS: list[StrategyFieldSchema] = [
    StrategyFieldSchema(name="name", type="string", required=True),
    StrategyFieldSchema(
        name="odds_band",
        type="array<float>",
        required=True,
        description="Two-element [lower, upper] odds bound.",
    ),
    StrategyFieldSchema(name="base_stake", type="number", required=False, minimum=0),
    StrategyFieldSchema(name="stake", type="number", required=False, minimum=0),
    StrategyFieldSchema(name="gap_lt", type="number", required=False, minimum=0),
    StrategyFieldSchema(name="gap_gte", type="number", required=False, minimum=0),
    StrategyFieldSchema(
        name="also_lay_2nd",
        type="boolean",
        required=False,
        default=False,
        description="When true, the rule places matching lays on the 2nd favourite.",
    ),
]


_CONTROL_FIELDS: list[StrategyFieldSchema] = [
    StrategyFieldSchema(
        name="hard_floor",
        type="number",
        required=True,
        default=1.01,
        minimum=1.0,
        description="Reject markets where favourite price is below this.",
    ),
    StrategyFieldSchema(
        name="hard_ceiling",
        type="number",
        required=True,
        default=1000.0,
        minimum=1.0,
        description="Reject markets where favourite price is above this.",
    ),
    StrategyFieldSchema(
        name="jofs_enabled",
        type="boolean",
        required=False,
        default=False,
        description="Enable Joint Odds Favourite Splitting.",
    ),
    StrategyFieldSchema(
        name="jofs_spread",
        type="number",
        required=False,
        default=0.20,
        minimum=0.0,
        description="Maximum gap (in price units) considered a joint favourite.",
    ),
    StrategyFieldSchema(
        name="mark_uplift",
        type="number",
        required=False,
        minimum=0,
        description=(
            "Stake multiplier applied to the matched rule's base_stake. "
            "When set, final_stake = base_stake * mark_uplift * point_value."
        ),
    ),
    StrategyFieldSchema(
        name="spread_control",
        type="boolean",
        required=False,
        default=False,
        description="Block markets when 1st/2nd favourite spread is too tight.",
    ),
]


_STAKING_FIELDS: list[StrategyFieldSchema] = [
    StrategyFieldSchema(
        name="point_value",
        type="number",
        required=True,
        default=1.0,
        minimum=0.0,
        description="Multiplier converting points (rule stakes) into currency.",
    ),
]


_STORE: PluginStore | None = None


def get_plugin_store() -> PluginStore:
    """Return the lazily-initialised process-wide :class:`PluginStore`."""

    global _STORE
    if _STORE is None:
        _STORE = PluginStore(get_settings().plugins_dir)
    return _STORE


def reset_plugin_store(plugins_dir: Path | None = None) -> PluginStore:
    """Force a refresh, optionally pointing at a different directory.

    Used by unit tests to load fixtures.
    """

    global _STORE
    target = plugins_dir or get_settings().plugins_dir
    _STORE = PluginStore(target)
    return _STORE
