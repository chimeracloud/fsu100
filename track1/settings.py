"""Engine settings — every toggle and variable that drives the pipeline.

One Settings object flows through the entire pipeline (evaluator + engine
+ portal). Defaults match the current live engine exactly: spread control
on, JOFS on, everything else off. The portal's Bet Settings page is just
a CRUD form for this object — every field in this file maps to a UI
control on that page.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any


class Mode(str, Enum):
    """Three operating modes — orthogonal to stream state.

    STOPPED  → no streaming, no evaluation. Default on cold start.
    DRY_RUN  → streaming active, evaluation active, decisions logged,
               no bets placed on Betfair.
    LIVE     → streaming active, evaluation active, bets placed with
               real money on Betfair.
    """

    STOPPED = "STOPPED"
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


@dataclass
class GeneralSettings:
    """Group 1 in the Bet Settings panel — general engine config."""

    point_value: float = 1.0
    countries: list[str] = field(default_factory=lambda: ["GB", "IE"])
    process_window_mins: int = 5
    mode: Mode = Mode.STOPPED


@dataclass
class BaseRules:
    """Group 2 — the 6 core rules. Each rule has an enabled toggle and a
    configurable stake (in points). Stake of 0 means SKIP the band.
    """

    rule1_enabled: bool = True
    rule1_stake: float = 3.0

    rule2a_enabled: bool = True
    rule2a_stake: float = 0.0  # default 0 = skip band

    rule2b_enabled: bool = True
    rule2b_stake: float = 1.0

    rule2c_enabled: bool = True
    rule2c_stake: float = 2.0

    rule3a_enabled: bool = True
    rule3b_enabled: bool = True
    rule3_stake: float = 1.0  # shared between 3A and 3B

    rule2_split1: float = 3.0  # 2a / 2b boundary
    rule2_split2: float = 4.0  # 2b / 2c boundary
    rule3_gap_threshold: float = 2.0


@dataclass
class Controls:
    """Group 3 — cross-rule controls (ISO 31000 sense: measures that
    modify risk). Spread Control + JOFS on by default to match current
    live engine; Mark Ceiling / Floor / Uplift off by default.
    """

    spread_control_enabled: bool = True
    jofs_enabled: bool = True

    mark_ceiling_enabled: bool = False
    mark_ceiling_value: float = 8.0

    mark_floor_enabled: bool = False
    mark_floor_value: float = 1.5

    mark_uplift_enabled: bool = False
    mark_uplift_stake: float = 3.0


@dataclass
class SignalToggles:
    """Group 4 — four independent signal filters (off by default to match
    current live engine). Threshold values match the lay-engine defaults.
    """

    signal_overround_enabled: bool = False
    overround_soft_threshold: float = 1.15
    overround_hard_threshold: float = 1.20

    signal_field_size_enabled: bool = False
    field_size_max_runners: int = 10
    field_size_odds_min: float = 3.0
    field_size_stake_cap: float = 10.0

    signal_steam_gate_enabled: bool = False
    steam_gate_odds_min: float = 3.0
    steam_shortening_pct: float = 0.03

    signal_band_perf_enabled: bool = False
    band_perf_lookback_days: int = 5
    band_perf_min_win_rate: float = 0.50
    band_perf_min_sample: int = 10
    band_perf_reduced_stake: float = 10.0


@dataclass
class RiskOverlay:
    """Group 5 — post-decision risk layer (TOP2 + MOM). Off by default
    to match current live engine. Thresholds are not user-configurable
    per spec — they are fixed per the lay-engine specification.
    """

    top2_concentration_enabled: bool = False
    market_overlay_enabled: bool = False


@dataclass
class Settings:
    """Top-level engine settings.

    Flat composition of five groups so the portal can render each as a
    panel without surgery. The same object instance is read by:

    * The evaluator (every field is consumed at decision time).
    * The engine (mode + countries + process_window drive the stream).
    * The portal (renders the form, PUTs back to ``/admin/config``).
    """

    general: GeneralSettings = field(default_factory=GeneralSettings)
    rules: BaseRules = field(default_factory=BaseRules)
    controls: Controls = field(default_factory=Controls)
    signals: SignalToggles = field(default_factory=SignalToggles)
    risk: RiskOverlay = field(default_factory=RiskOverlay)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly view of every setting."""

        d = asdict(self)
        d["general"]["mode"] = self.general.mode.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        """Inverse of :meth:`to_dict`. Tolerates partial dicts — any
        missing field falls back to the dataclass default so the portal
        can PUT only the fields the operator changed."""

        def _fill(group_cls, group_data):
            kwargs = {}
            for f in fields(group_cls):
                if f.name in group_data:
                    kwargs[f.name] = group_data[f.name]
            return group_cls(**kwargs)

        general_data = dict(data.get("general") or {})
        if "mode" in general_data and isinstance(general_data["mode"], str):
            general_data["mode"] = Mode(general_data["mode"])

        return cls(
            general=_fill(GeneralSettings, general_data),
            rules=_fill(BaseRules, data.get("rules") or {}),
            controls=_fill(Controls, data.get("controls") or {}),
            signals=_fill(SignalToggles, data.get("signals") or {}),
            risk=_fill(RiskOverlay, data.get("risk") or {}),
        )
