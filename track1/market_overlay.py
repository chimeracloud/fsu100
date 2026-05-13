"""
CHIMERA Lay Engine — Market Overlay Modifier (MARKET_OVERLAY_MODIFIER)
=======================================================================
Confidence-scaling modifier that adjusts stakes based on exchange market
efficiency at the time of evaluation.

NOT a decision rule — never creates or blocks a bet. Only scales the stake
multiplier applied after all other signals have been processed.

Priority position (per specification):
  1. SHORT_PRICE_CONTROL
  2. Core lay engine
  3. Joint favourite split logic (JOFS)
  4. RPR overlay / signal filters
  5. MARKET_OVERLAY_MODIFIER   ← this module
  6. Final stake sizing / bet placement

Rules:
  MOM_01 — COMPUTE_OVERROUND:  sum(1 / back_odds) across all active runners
  MOM_02 — HIGH_OVERROUND:     overround > 1.02  → multiplier = 1.15
  MOM_03 — NEUTRAL_BAND:       1.00 ≤ overround ≤ 1.02 → multiplier = 1.00
  MOM_04 — EFFICIENT_MARKET:   overround < 1.00  → multiplier = 0.80

Works in LIVE, DRY RUN, and BACKTEST contexts.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MarketOverlayResult:
    """Output from apply_market_overlay()."""
    market_overlay_state: str       # DISABLED | HIGH_OVERROUND | NEUTRAL | EFFICIENT_MARKET
    exchange_overround: float       # Sum of implied probs from back prices (0.0 if unavailable)
    overlay_multiplier: float       # 0.80, 1.00, or 1.15
    reason: str
    market_concentration_flag: bool = False   # True when top-of-market compression detected

    def to_dict(self) -> dict:
        return {
            "market_overlay_state": self.market_overlay_state,
            "exchange_overround": self.exchange_overround,
            "overlay_multiplier": self.overlay_multiplier,
            "reason": self.reason,
            "market_concentration_flag": self.market_concentration_flag,
        }


def compute_overround(runners) -> Optional[float]:
    """
    MOM_01 — COMPUTE_OVERROUND
    Compute exchange overround: sum(1 / back_odds) across all active runners.
    Uses best available back prices (higher back price = lower implied prob).
    Returns None if no valid back prices are available.
    """
    implied_probs = [
        1.0 / r.best_available_to_back
        for r in runners
        if getattr(r, "status", "ACTIVE") == "ACTIVE"
        and getattr(r, "best_available_to_back", None)
        and r.best_available_to_back > 1.0
    ]
    if not implied_probs:
        return None
    return round(sum(implied_probs), 4)


def _check_market_concentration(runners) -> bool:
    """
    Directional Exception — detect top-of-market compression.

    Fires when ALL of the following are true (overround > 1.02 already satisfied by caller):
      - gap between 1st and 2nd favourite back prices ≤ 0.30
      - gap between 2nd and 3rd favourite back prices ≥ 1.50

    Uses back prices sorted ascending (lowest back = shortest = favourite).
    This is a logging-only flag — no stake change. Feeds the future
    TOP_OF_MARKET_CONCENTRATION rule family.
    """
    active = sorted(
        [
            r for r in runners
            if getattr(r, "status", "ACTIVE") == "ACTIVE"
            and getattr(r, "best_available_to_back", None)
            and r.best_available_to_back > 1.0
        ],
        key=lambda r: r.best_available_to_back,
    )
    if len(active) < 3:
        return False

    gap_1_2 = round(active[1].best_available_to_back - active[0].best_available_to_back, 4)
    gap_2_3 = round(active[2].best_available_to_back - active[1].best_available_to_back, 4)
    return gap_1_2 <= 0.30 and gap_2_3 >= 1.50


def apply_market_overlay(runners, enabled: bool = True) -> MarketOverlayResult:
    """
    Apply the Market Overlay Modifier rules to a set of runners.

    Returns a MarketOverlayResult with the overlay_multiplier to apply to
    surviving instruction stakes after all other signal filters have run.
    When enabled=False returns multiplier=1.00 (no adjustment).

    Constraint: this multiplier is applied once per market evaluation.
    It is NOT stacked across multiple checkpoints.

    MOM_02: overround > 1.02  → 1.15  (HIGH_OVERROUND — market unsettled, trust signals more)
    MOM_03: 1.00–1.02         → 1.00  (NEUTRAL — proceed normally)
    MOM_04: < 1.00            → 0.80  (EFFICIENT_MARKET — edge already priced in, reduce)
    """
    if not enabled:
        return MarketOverlayResult(
            market_overlay_state="DISABLED",
            exchange_overround=0.0,
            overlay_multiplier=1.0,
            reason="Market Overlay Modifier disabled",
        )

    overround = compute_overround(runners)

    if overround is None:
        return MarketOverlayResult(
            market_overlay_state="NEUTRAL",
            exchange_overround=0.0,
            overlay_multiplier=1.0,
            reason="No back prices available — defaulting to NEUTRAL",
        )

    # MOM_02 — High Overround Amplifier
    if overround > 1.02:
        concentration = _check_market_concentration(runners)
        return MarketOverlayResult(
            market_overlay_state="HIGH_OVERROUND",
            exchange_overround=overround,
            overlay_multiplier=1.15,
            reason=f"Market unsettled (overround {overround:.4f}) — signals weighted higher",
            market_concentration_flag=concentration,
        )

    # MOM_04 — Efficient Market Dampener
    if overround < 1.00:
        return MarketOverlayResult(
            market_overlay_state="EFFICIENT_MARKET",
            exchange_overround=overround,
            overlay_multiplier=0.80,
            reason=f"Market is sharp (overround {overround:.4f}) — reducing aggression",
        )

    # MOM_03 — Neutral Band (1.00 ≤ overround ≤ 1.02)
    return MarketOverlayResult(
        market_overlay_state="NEUTRAL",
        exchange_overround=overround,
        overlay_multiplier=1.0,
        reason=f"Market normal (overround {overround:.4f}) — no adjustment",
    )
