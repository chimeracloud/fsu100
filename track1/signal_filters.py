"""
CHIMERA Lay Engine — Signal Filters
=====================================
Four pre-bet market intelligence signals derived from the dickreuter
methodology analysis (Day 33 post-mortem, March 2026).

These sit BETWEEN rules evaluation and execution. They never modify
the rules themselves — only adjust stakes or block individual bets.

All signals are OFF by default and independently switchable.

SIGNALS:
  1. Market Overround   — high book % = illiquid / unreliable market
  2. Field Size         — large field + mid-high odds = high variance
  3. Steam Gate         — favourite shortening = backed money, don't lay
  4. Rolling Band Perf  — recent win rate < threshold → reduce stake

Works in both LIVE and BACKTEST contexts.
"""

from dataclasses import dataclass
from typing import Optional


# ─── Odds band helper ─────────────────────────────────────────────────────────

def get_odds_band(odds: float) -> str:
    """Map a price to a named odds band (mirrors AI report logic)."""
    if odds < 2.0:
        return "under_2"
    elif odds < 3.0:
        return "2_to_3"
    elif odds < 4.0:
        return "3_to_4"
    elif odds < 5.0:
        return "4_to_5"
    else:
        return "5_plus"


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class SignalConfig:
    """
    Configuration for all four signal filters.
    All disabled by default — must be explicitly enabled.
    """

    # Signal 1: Market Overround
    overround_enabled: bool = False
    overround_soft_threshold: float = 1.15    # >115% book → halve stake
    overround_hard_threshold: float = 1.20    # >120% book → skip entirely

    # Signal 2: Field Size
    field_size_enabled: bool = False
    field_size_max_runners: int = 10          # trigger when field > this
    field_size_odds_min: float = 3.0          # only trigger when fav odds >= this
    field_size_stake_cap: float = 10.0        # cap stake to this value

    # Signal 3: Steam Gate
    steam_gate_enabled: bool = False
    steam_gate_odds_min: float = 3.0          # only trigger when fav odds >= this
    steam_shortening_pct: float = 0.03        # >3% price drop = shortening

    # Signal 4: Rolling Band Performance
    band_perf_enabled: bool = False
    band_perf_lookback_days: int = 5
    band_perf_min_win_rate: float = 0.50      # <50% win rate → reduce stake
    band_perf_min_sample: int = 10            # minimum bets before acting
    band_perf_reduced_stake: float = 10.0     # cap stake to this when triggered


# ─── Individual signal verdict ────────────────────────────────────────────────

@dataclass
class SignalVerdict:
    """The verdict from a single signal check."""
    signal: str
    fired: bool
    action: str = "NONE"          # NONE | HALVE_STAKE | CAP_STAKE | SKIP
    cap_value: Optional[float] = None
    reason: str = ""


# ─── Master filter result ─────────────────────────────────────────────────────

@dataclass
class SignalFilterResult:
    """
    Consolidated result after running all enabled signals against one bet.
    Contains the final stake to use and a full audit trail of what fired.
    """
    allowed: bool
    original_stake: float
    final_stake: float
    verdicts: list            # List[SignalVerdict]
    skip_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "original_stake": self.original_stake,
            "final_stake": self.final_stake,
            "signals_fired": [
                {"signal": v.signal, "action": v.action, "reason": v.reason}
                for v in self.verdicts if v.fired
            ],
            "skip_reason": self.skip_reason,
        }


# ─── Signal 1: Market Overround ───────────────────────────────────────────────

def check_overround(all_runners, config: SignalConfig) -> SignalVerdict:
    """
    Compute the market book percentage (sum of implied win probabilities
    from back prices). A high overround signals an illiquid or poorly
    priced market where the favourite's odds are less reliable.

    >115% (soft)  → halve stake
    >120% (hard)  → skip entirely
    """
    if not config.overround_enabled:
        return SignalVerdict(signal="OVERROUND", fired=False)

    implied_probs = [
        1.0 / r.best_available_to_back
        for r in all_runners
        if getattr(r, "best_available_to_back", None) and r.best_available_to_back > 1.0
    ]

    if not implied_probs:
        return SignalVerdict(
            signal="OVERROUND", fired=False,
            reason="No back prices available for overround calc",
        )

    overround = round(sum(implied_probs), 4)

    if overround > config.overround_hard_threshold:
        return SignalVerdict(
            signal="OVERROUND",
            fired=True,
            action="SKIP",
            reason=(
                f"Book {overround:.1%} exceeds hard threshold "
                f"{config.overround_hard_threshold:.0%} — market too illiquid"
            ),
        )

    if overround > config.overround_soft_threshold:
        return SignalVerdict(
            signal="OVERROUND",
            fired=True,
            action="HALVE_STAKE",
            reason=(
                f"Book {overround:.1%} exceeds soft threshold "
                f"{config.overround_soft_threshold:.0%} — stake halved"
            ),
        )

    return SignalVerdict(
        signal="OVERROUND", fired=False,
        reason=f"Book {overround:.1%} — within threshold",
    )


# ─── Signal 2: Field Size ─────────────────────────────────────────────────────

def check_field_size(all_runners, fav_odds: float, config: SignalConfig) -> SignalVerdict:
    """
    Large NH fields at mid-to-high odds significantly increase variance.
    When field > max_runners AND fav odds >= odds_min, cap the stake.
    """
    if not config.field_size_enabled:
        return SignalVerdict(signal="FIELD_SIZE", fired=False)

    active_count = sum(
        1 for r in all_runners
        if getattr(r, "status", "ACTIVE") == "ACTIVE"
    )

    if active_count > config.field_size_max_runners and fav_odds >= config.field_size_odds_min:
        return SignalVerdict(
            signal="FIELD_SIZE",
            fired=True,
            action="CAP_STAKE",
            cap_value=config.field_size_stake_cap,
            reason=(
                f"Field of {active_count} runners with fav @ {fav_odds:.2f} "
                f"(>{config.field_size_max_runners} + odds ≥ {config.field_size_odds_min}) "
                f"→ stake capped at £{config.field_size_stake_cap}"
            ),
        )

    return SignalVerdict(
        signal="FIELD_SIZE", fired=False,
        reason=f"Field: {active_count} runners @ {fav_odds:.2f}",
    )


# ─── Signal 3: Steam Gate ─────────────────────────────────────────────────────

def check_steam(
    selection_id: int,
    current_price: float,
    previous_prices: dict,     # {selection_id: float} — earliest monitoring snapshot
    fav_odds: float,
    config: SignalConfig,
) -> SignalVerdict:
    """
    If the favourite has shortened significantly since the first monitoring
    snapshot, money is backing it (steam). Laying into a steaming horse
    is betting against the live information flow.

    Only applies when fav odds >= steam_gate_odds_min (mid-high bands).
    """
    if not config.steam_gate_enabled:
        return SignalVerdict(signal="STEAM_GATE", fired=False)

    if fav_odds < config.steam_gate_odds_min:
        return SignalVerdict(
            signal="STEAM_GATE", fired=False,
            reason=f"Fav {fav_odds:.2f} below steam odds threshold {config.steam_gate_odds_min}",
        )

    prev_price = previous_prices.get(selection_id)
    if prev_price is None or prev_price <= 1.0:
        return SignalVerdict(
            signal="STEAM_GATE", fired=False,
            reason="No previous price snapshot — steam check skipped",
        )

    price_change_pct = (prev_price - current_price) / prev_price

    if price_change_pct >= config.steam_shortening_pct:
        return SignalVerdict(
            signal="STEAM_GATE",
            fired=True,
            action="SKIP",
            reason=(
                f"Favourite SHORTENING: {prev_price:.2f} → {current_price:.2f} "
                f"({price_change_pct:.1%} drop) — laying into steam, skipped"
            ),
        )

    direction = (
        "SHORTENING" if price_change_pct > 0
        else ("DRIFTING" if price_change_pct < 0 else "STABLE")
    )
    return SignalVerdict(
        signal="STEAM_GATE", fired=False,
        reason=f"Price {direction}: {prev_price:.2f} → {current_price:.2f}",
    )


# ─── Signal 4: Rolling Band Performance ──────────────────────────────────────

def check_band_performance(
    fav_odds: float,
    band_stats: dict,    # {band_name: {"wins": int, "total": int, "win_rate": float}}
    config: SignalConfig,
) -> SignalVerdict:
    """
    If the rolling win rate for this odds band (over the last N days) is
    below the threshold, reduce the stake automatically rather than
    betting at full size into a band that has been losing.

    Requires a minimum sample of bets before acting (avoids premature
    reductions after only 1-2 losses).
    """
    if not config.band_perf_enabled:
        return SignalVerdict(signal="BAND_PERF", fired=False)

    band = get_odds_band(fav_odds)
    stats = band_stats.get(band, {})
    total = stats.get("total", 0)
    win_rate = stats.get("win_rate", None)

    if total < config.band_perf_min_sample or win_rate is None:
        return SignalVerdict(
            signal="BAND_PERF", fired=False,
            reason=f"Band {band}: {total} bets — below min sample ({config.band_perf_min_sample}), no action",
        )

    if win_rate < config.band_perf_min_win_rate:
        return SignalVerdict(
            signal="BAND_PERF",
            fired=True,
            action="CAP_STAKE",
            cap_value=config.band_perf_reduced_stake,
            reason=(
                f"Band {band}: {win_rate:.0%} win rate over last "
                f"{config.band_perf_lookback_days}d "
                f"(threshold {config.band_perf_min_win_rate:.0%}, {total} bets) "
                f"→ stake capped at £{config.band_perf_reduced_stake}"
            ),
        )

    return SignalVerdict(
        signal="BAND_PERF", fired=False,
        reason=f"Band {band}: {win_rate:.0%} win rate ({total} bets) — OK",
    )


# ─── Master filter ────────────────────────────────────────────────────────────

def apply_signal_filters(
    selection_id: int,
    current_price: float,
    original_stake: float,
    all_runners,           # list[Runner] — full active field
    previous_prices: dict, # {selection_id: float} — from monitoring snapshots
    band_stats: dict,      # {band_name: {"wins", "total", "win_rate"}}
    config: SignalConfig,
) -> SignalFilterResult:
    """
    Run all enabled signals against a single bet instruction.
    Returns a SignalFilterResult with the final stake and audit trail.

    Priority:
      1. Any SKIP verdict      → block the bet entirely
      2. HALVE_STAKE verdicts  → halve the current stake
      3. CAP_STAKE verdicts    → apply the most restrictive cap
      4. No verdicts fired     → stake unchanged
    """
    fav_odds = current_price

    verdicts = [
        check_overround(all_runners, config),
        check_field_size(all_runners, fav_odds, config),
        check_steam(selection_id, current_price, previous_prices, fav_odds, config),
        check_band_performance(fav_odds, band_stats, config),
    ]

    # ── Any SKIP → block the bet ──
    skip_verdicts = [v for v in verdicts if v.fired and v.action == "SKIP"]
    if skip_verdicts:
        return SignalFilterResult(
            allowed=False,
            original_stake=original_stake,
            final_stake=0.0,
            verdicts=verdicts,
            skip_reason=" | ".join(v.reason for v in skip_verdicts),
        )

    # ── Apply stake modifications ──
    final_stake = original_stake

    # HALVE_STAKE (compounding — each HALVE halves the current value)
    for v in verdicts:
        if v.fired and v.action == "HALVE_STAKE":
            final_stake = round(final_stake / 2, 2)

    # CAP_STAKE — take the most restrictive cap across all fired signals
    caps = [
        v.cap_value for v in verdicts
        if v.fired and v.action == "CAP_STAKE" and v.cap_value is not None
    ]
    if caps:
        final_stake = min(final_stake, min(caps))

    # Never drop below Betfair minimum (£2) when we started above it
    if original_stake >= 2.0:
        final_stake = max(final_stake, 2.0)

    return SignalFilterResult(
        allowed=True,
        original_stake=original_stake,
        final_stake=round(final_stake, 2),
        verdicts=verdicts,
    )
