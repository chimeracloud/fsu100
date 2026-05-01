"""Pydantic schemas for every public request and response payload.

Every shape that crosses the API boundary lives here so validation is
centralised and the OpenAPI document is complete. All models forbid
unknown fields so silent typos in user-supplied JSON fail fast.

The plugin schema is **byte-for-byte identical** to the Backtest Tool's
plugin schema. A plugin file authored for the Backtest Tool validates and
runs in FSU100 without modification. FSU100 ignores ``plugin.source.type``
and ``plugin.parser.format`` (it always reads the live Betfair stream), but
it honours ``plugin.source.filters.countries`` and
``plugin.source.filters.market_types`` (these become the live
``streaming_market_filter``) plus ``plugin.parser.time_before_off_seconds``
(when to evaluate inside the pre-off window).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    """Base model that rejects unknown fields and trims strings."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
    )


# ---------------------------------------------------------------------------
# Plugin schema — identical to the Backtest Tool
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    """Where a Backtest Tool plugin would read historic data from.

    Retained on the FSU100 schema so plugins authored for the Backtest Tool
    validate without changes; FSU100 itself ignores the source type because
    it always streams live from the Betfair Exchange.
    """

    GCS = "gcs"
    BETFAIR_HISTORIC = "betfair_historic"


class FileType(str, Enum):
    """Betfair Historic Data API file types — kept for plugin compatibility."""

    MARKET = "M"
    EVENT = "E"
    ITEMS = "I"


class DateRange(_Strict):
    """Inclusive date range carried in plugin source blocks."""

    start: date
    end: date

    @model_validator(mode="after")
    def _check_order(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("date_range.end must be on or after date_range.start")
        return self


class SourceFilters(_Strict):
    """Filters applied when listing or downloading historic files.

    FSU100 maps ``countries`` and ``market_types`` onto the live
    ``streaming_market_filter`` when a strategy is activated.
    """

    countries: list[str] = Field(
        default_factory=list,
        description="ISO country codes (e.g. ['GB', 'IE']).",
    )
    market_types: list[str] = Field(
        default_factory=list,
        description="Betfair market types (e.g. ['WIN', 'PLACE']).",
    )
    sport: str = Field(
        default="Horse Racing",
        description="Betfair sport name (kept for plugin compatibility).",
    )
    plan: Literal["Basic Plan", "Advanced Plan", "Pro Plan"] = Field(
        default="Basic Plan",
        description="Historic Data API plan (kept for plugin compatibility).",
    )
    file_types: list[FileType] = Field(
        default_factory=lambda: [FileType.MARKET],
        description="Historic Data API file types (kept for plugin compatibility).",
    )


class GcsSourceConfig(_Strict):
    """Plugin source pointer — read .bz2 files from a GCS bucket."""

    type: Literal[SourceType.GCS] = SourceType.GCS
    bucket: str = Field(
        ...,
        description="GCS path including any prefix, e.g. gs://bucket/PATH/",
    )
    date_range: DateRange
    filters: SourceFilters = Field(default_factory=SourceFilters)


class BetfairHistoricSourceConfig(_Strict):
    """Plugin source pointer — download files via the Historic Data API."""

    type: Literal[SourceType.BETFAIR_HISTORIC] = SourceType.BETFAIR_HISTORIC
    date_range: DateRange
    filters: SourceFilters = Field(default_factory=SourceFilters)
    persist_to_bucket: str | None = Field(
        default=None,
        description=(
            "Optional gs://... path used by the Backtest Tool to mirror "
            "downloads back to GCS — ignored by FSU100."
        ),
    )


SourceConfig = Annotated[
    GcsSourceConfig | BetfairHistoricSourceConfig,
    Field(discriminator="type"),
]


class ParserConfig(_Strict):
    """Controls how raw market updates are converted into evaluator input.

    FSU100 honours ``time_before_off_seconds`` (the entry window relative to
    market off-time) but ignores ``format`` and ``price_field`` because the
    live stream always supplies a fully-formed ``MarketBook`` with
    ``last_price_traded`` populated.
    """

    format: Literal["betfair_mcm"] = "betfair_mcm"
    time_before_off_seconds: int = Field(
        default=300,
        ge=0,
        le=86_400,
        description="Seconds before market_time at which to evaluate.",
    )
    price_field: Literal["ltp", "back", "lay"] = Field(
        default="ltp",
        description="Price field used to identify the favourite.",
    )
    extract_bsp: bool = Field(
        default=True,
        description="Settle bets using SP when available.",
    )


class StrategyRule(_Strict):
    """One element of the ``strategy.rules`` array.

    The evaluator iterates rules in order and applies the first whose
    ``odds_band`` contains the favourite price. ``extra="allow"`` is the
    only relaxation from :class:`_Strict` — rules accept additional,
    plugin-specific fields so future strategies can encode bespoke
    parameters without changing this schema.
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    name: str
    odds_band: tuple[float, float] = Field(
        ...,
        description="Inclusive lower, exclusive upper odds bound.",
    )
    base_stake: float | None = Field(default=None, ge=0)
    stake: float | None = Field(default=None, ge=0)
    gap_lt: float | None = Field(default=None, ge=0)
    gap_gte: float | None = Field(default=None, ge=0)
    also_lay_2nd: bool = False

    @model_validator(mode="after")
    def _has_stake(self) -> "StrategyRule":
        if self.base_stake is None and self.stake is None:
            raise ValueError(
                f"rule '{self.name}' must define either base_stake or stake"
            )
        return self


class StrategyControls(BaseModel):
    """Cross-rule guards and modifiers.

    ``extra="allow"`` lets plugins introduce new controls without code
    changes — the evaluator applies the controls it understands and
    silently passes the rest through to the result document for
    auditability.
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    hard_floor: float = Field(default=1.01, ge=1.0)
    hard_ceiling: float = Field(default=1000.0, gt=1.0)
    jofs_enabled: bool = False
    jofs_spread: float = Field(default=0.20, ge=0.0)
    mark_uplift: float | None = Field(default=None, ge=0.0)
    spread_control: bool = False

    @model_validator(mode="after")
    def _check_floor_ceiling(self) -> "StrategyControls":
        if self.hard_ceiling <= self.hard_floor:
            raise ValueError("hard_ceiling must be greater than hard_floor")
        return self


class StrategyConfig(_Strict):
    """The decisive part of a plugin: what to bet and when."""

    rules: list[StrategyRule] = Field(..., min_length=1)
    controls: StrategyControls = Field(default_factory=StrategyControls)


class StakingConfig(_Strict):
    """How stake numbers in rules translate to currency."""

    point_value: float = Field(
        default=1.0,
        gt=0,
        description="Multiplier applied to base_stake / stake to get currency.",
    )


class PluginConfig(_Strict):
    """Full plugin payload.

    Identical to the Backtest Tool's plugin schema so a JSON file authored
    for one tool runs in the other without changes.
    """

    name: str
    version: str
    description: str | None = None
    source: SourceConfig
    parser: ParserConfig = Field(default_factory=ParserConfig)
    strategy: StrategyConfig
    staking: StakingConfig = Field(default_factory=StakingConfig)


# ---------------------------------------------------------------------------
# Engine mode and control
# ---------------------------------------------------------------------------


class EngineMode(str, Enum):
    """Live engine operating mode.

    * ``LIVE``   — stream is active and bets are placed on the exchange.
    * ``DRY_RUN`` — stream is active, decisions are logged but no bets are
      sent to Betfair.
    * ``STOPPED`` — stream is closed and no bets are placed. Default on
      startup.
    """

    LIVE = "LIVE"
    DRY_RUN = "DRY_RUN"
    STOPPED = "STOPPED"


class StreamStatus(str, Enum):
    """Reported state of the Betfair streaming socket."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class ControlAction(str, Enum):
    """Recognised values for ``POST /admin/control/{action}``."""

    START = "start"
    STOP = "stop"
    DRY_RUN = "dry-run"
    RESET_STATS = "reset-stats"


class ControlResponse(_Strict):
    """Body of ``POST /admin/control/{action}``."""

    action: ControlAction
    accepted: bool
    mode: EngineMode
    detail: str | None = None


# ---------------------------------------------------------------------------
# Admin (Set 1)
# ---------------------------------------------------------------------------


class AdminStatus(_Strict):
    """Body of ``GET /admin/status``."""

    service: str
    version: str
    environment: str
    uptime_seconds: float
    timestamp: datetime
    mode: EngineMode
    active_plugin: str | None
    active_plugin_version: str | None
    stream_status: StreamStatus
    markets_in_cache: int


class AdminConfig(_Strict):
    """Body of ``GET /admin/config`` and ``PUT /admin/config``.

    Mutable fields control how the engine runs without changing the strategy
    itself — to swap rules, change ``active_plugin``.
    """

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    activity_log_size: int = Field(ge=10, le=1000)
    results_bucket: str
    active_plugin: str
    countries: list[str] = Field(
        description=(
            "ISO country codes used as the streaming filter. "
            "Overrides plugin.source.filters.countries."
        )
    )
    market_types: list[str] = Field(
        description=(
            "Market types used as the streaming filter. "
            "Overrides plugin.source.filters.market_types."
        )
    )
    point_value: float = Field(
        gt=0,
        description="Multiplier from rule stake to currency stake.",
    )
    customer_strategy_ref: str | None = Field(
        default=None,
        max_length=15,
        description="String tagged onto every order for back-office tracking.",
    )


class AdminStats(_Strict):
    """Body of ``GET /admin/stats``."""

    bets_placed: int
    bets_won: int
    bets_lost: int
    bets_void: int
    bets_pending: int
    strike_rate: float
    markets_processed: int
    total_stake: float
    total_liability: float
    total_pnl: float
    open_exposure: float
    stats_window_start: datetime


class ActivityEvent(_Strict):
    """One entry in the recent-activity ring buffer."""

    timestamp: datetime
    event: str
    market_id: str | None = None
    detail: str | None = None


class ActivityResponse(_Strict):
    """Body of ``GET /admin/activity``."""

    events: list[ActivityEvent]


# ---------------------------------------------------------------------------
# GUI / portal-facing (Set 2)
# ---------------------------------------------------------------------------


class RunnerSnapshot(_Strict):
    """One runner row inside :class:`MarketSnapshot` and :class:`MarketView`."""

    selection_id: int
    name: str
    status: str = "ACTIVE"
    last_price_traded: float | None = None


class MarketView(_Strict):
    """Active market currently being monitored by the engine."""

    market_id: str
    venue: str | None
    country: str | None
    market_type: str | None
    market_time: datetime | None
    seconds_to_off: float | None
    in_play: bool
    evaluated: bool
    runners: list[RunnerSnapshot]


class MarketsResponse(_Strict):
    """Body of ``GET /api/markets``."""

    markets: list[MarketView]


class PositionView(_Strict):
    """One open position currently held on the exchange."""

    market_id: str
    selection_id: int
    runner_name: str
    side: Literal["LAY", "BACK"]
    price: float
    stake: float
    liability: float
    matched_size: float
    unmatched_size: float
    rule_applied: str | None
    bet_id: str
    placed_at: datetime
    pnl_if_settled_now: float | None = None


class PositionsResponse(_Strict):
    """Body of ``GET /api/positions``."""

    positions: list[PositionView]
    total_exposure: float


class SettledBet(_Strict):
    """One settled bet row included in results responses."""

    bet_id: str
    market_id: str
    selection_id: int
    runner_name: str
    side: Literal["LAY", "BACK"]
    price: float
    stake: float
    liability: float
    rule_applied: str | None
    outcome: Literal["WON", "LOST", "VOID"]
    pnl: float
    settled_at: datetime


class ResultsResponse(_Strict):
    """Body of ``GET /api/results``."""

    bets: list[SettledBet]
    summary: AdminStats


class HistoryResponse(_Strict):
    """Body of ``GET /api/results/history``."""

    bets: list[SettledBet]
    page: int
    page_size: int
    total: int
    range_start: date
    range_end: date


class StrategyInfo(_Strict):
    """Listing entry returned by ``GET /api/strategies``."""

    name: str
    version: str
    description: str | None = None
    rule_count: int


class StrategyFieldSchema(_Strict):
    """Subset of a JSON schema used to render a strategy editor form."""

    name: str
    type: str
    required: bool = False
    description: str | None = None
    default: Any = None
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None


class StrategySchema(_Strict):
    """Body of ``GET /api/strategies/{name}/schema``."""

    name: str
    version: str
    description: str | None = None
    source_fields: list[StrategyFieldSchema]
    parser_fields: list[StrategyFieldSchema]
    rule_fields: list[StrategyFieldSchema]
    control_fields: list[StrategyFieldSchema]
    staking_fields: list[StrategyFieldSchema]
    defaults: dict[str, Any]


class AccountResponse(_Strict):
    """Body of ``GET /api/account``."""

    available_to_bet: float
    exposure: float
    points_balance: float | None = None
    wallet: str = "UK"
    retrieved_at: datetime


# ---------------------------------------------------------------------------
# Content (Set 3) — the AIM agent calls these
# ---------------------------------------------------------------------------


class MarketDefinitionInput(_Strict):
    """Subset of a Betfair ``marketDefinition`` accepted by ``/api/evaluate``."""

    market_time: datetime | None = None
    venue: str | None = None
    country_code: str | None = None
    market_type: str | None = None
    in_play: bool = False
    runners: list[RunnerSnapshot] = Field(default_factory=list)


class MarketSnapshot(_Strict):
    """Generic market snapshot accepted by ``POST /api/evaluate``.

    The shape mirrors a betfairlightweight ``MarketBook`` closely enough
    that the evaluator can consume it directly via duck-typing — no
    conversion layer is required.
    """

    market_id: str
    publish_time: datetime | None = None
    inplay: bool = False
    market_definition: MarketDefinitionInput
    runners: list[RunnerSnapshot]


class EvaluateRequest(_Strict):
    """Body of ``POST /api/evaluate``.

    The full plugin block must be supplied — the engine does not assume the
    caller wants the active plugin. The optional overrides match the keyword
    arguments to :func:`evaluator.evaluate`.
    """

    market_snapshot: MarketSnapshot
    plugin: PluginConfig
    point_value_override: float | None = Field(default=None, gt=0)


class BetDecisionView(_Strict):
    """JSON-friendly view of a :class:`models.decisions.BetDecision`."""

    selection_id: int
    runner_name: str
    side: Literal["LAY", "BACK"]
    price: float
    stake: float
    liability: float
    rule_applied: str
    notes: str = ""


class NoBetView(_Strict):
    """JSON-friendly view of a :class:`models.decisions.NoBet`."""

    reason: str
    detail: str = ""


class EvaluateResponse(_Strict):
    """Body returned by ``POST /api/evaluate``."""

    market_id: str
    decisions: list[BetDecisionView]
    skipped: list[NoBetView]


class PlaceRequest(_Strict):
    """Body of ``POST /api/place``."""

    market_id: str
    decision: BetDecisionView
    persistence_type: Literal["LAPSE", "PERSIST", "MARKET_ON_CLOSE"] = "LAPSE"
    customer_order_ref: str | None = Field(default=None, max_length=32)


class PlaceReport(_Strict):
    """One instruction report inside :class:`PlaceResponse`."""

    status: str
    bet_id: str | None = None
    placed_date: datetime | None = None
    average_price_matched: float | None = None
    size_matched: float | None = None
    error_code: str | None = None


class PlaceResponse(_Strict):
    """Body returned by ``POST /api/place``."""

    market_id: str
    customer_ref: str
    status: str
    error_code: str | None = None
    instruction_reports: list[PlaceReport]


class CancelRequest(_Strict):
    """Body of ``POST /api/cancel``."""

    market_id: str
    bet_id: str
    size_reduction: float | None = Field(default=None, gt=0)


class CancelReport(_Strict):
    """One instruction report inside :class:`CancelResponse`."""

    status: str
    bet_id: str
    cancelled_date: datetime | None = None
    size_cancelled: float | None = None
    error_code: str | None = None


class CancelResponse(_Strict):
    """Body returned by ``POST /api/cancel``."""

    market_id: str
    status: str
    error_code: str | None = None
    instruction_reports: list[CancelReport]


class SettledResponse(_Strict):
    """Body returned by ``GET /api/settled``."""

    bets: list[SettledBet]
    range_start: date
    range_end: date
    total: int
