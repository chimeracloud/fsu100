"""
CHIMERA Lay Engine — Core Rules
================================
Pure rule-based lay betting. No intelligence. No ML. Just IF/WHEN logic.

RULES:
  1. All bets are LAY bets on the favourite (lowest odds runner)
  2. If favourite odds < 2.0  → £3 lay on favourite
     JOINT/CLOSE (gap ≤ 0.2): split as £1.50 fav + £1.50 2nd fav
  3. If favourite odds 2.0–5.0 → £2 lay on favourite
     JOINT/CLOSE (gap ≤ 0.2): split as £1.00 fav + £1.00 2nd fav
  4. If favourite odds > 5.0:
     a. If gap to 2nd favourite < 2 → £1 lay on fav + £1 lay on 2nd fav
     b. If gap to 2nd favourite ≥ 2 → £1 lay on favourite only

JOINT/CLOSE FAVOURITE RULE (applies to Rules 1–3):
  When the gap between 1st and 2nd favourite is ≤ CLOSE_ODDS_THRESHOLD (0.2),
  the market is treated as a joint-favourite race.  The full stake for the
  applicable rule is split evenly across both runners rather than laid on
  the favourite alone.  This protects against laying a horse that isn't
  actually the dominant favourite at race time.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

# Maximum lay odds — skip markets where the favourite exceeds this.
# Odds like 560.00 indicate an illiquid market with no real trading.
MAX_LAY_ODDS = 50.0

# Close-odds / joint-favourite threshold.
# When the gap between 1st and 2nd favourite is at or below this value,
# the stake is split evenly across both runners.  Applies to all rules.
CLOSE_ODDS_THRESHOLD = 0.2


@dataclass
class Runner:
    """A runner in a race with current market data."""
    selection_id: int
    runner_name: str
    handicap: float = 0.0
    best_available_to_lay: Optional[float] = None  # lowest lay price = best odds
    best_available_to_back: Optional[float] = None  # highest back price
    status: str = "ACTIVE"


@dataclass
class LayInstruction:
    """A single lay bet instruction to send to Betfair."""
    market_id: str
    selection_id: int
    runner_name: str
    price: float      # The lay odds
    size: float        # The stake (backer's stake we're accepting)
    rule_applied: str  # Which rule triggered this bet

    @property
    def liability(self) -> float:
        """What we lose if the horse wins."""
        return round(self.size * (self.price - 1), 2)

    def to_betfair_instruction(self) -> dict:
        """Format as Betfair placeOrders instruction."""
        return {
            "selectionId": str(self.selection_id),
            "handicap": "0",
            "side": "LAY",
            "orderType": "LIMIT",
            "limitOrder": {
                "size": str(self.size),
                "price": str(self.price),
                "persistenceType": "LAPSE"
            }
        }

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "runner_name": self.runner_name,
            "price": self.price,
            "size": self.size,
            "liability": self.liability,
            "rule_applied": self.rule_applied,
        }


@dataclass
class RuleResult:
    """The output of applying rules to a market."""
    market_id: str
    market_name: str
    venue: str
    race_time: str
    instructions: list  # List[LayInstruction]
    favourite: Optional[Runner] = None
    second_favourite: Optional[Runner] = None
    skipped: bool = False
    skip_reason: str = ""
    rule_applied: str = ""
    evaluated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "market_name": self.market_name,
            "venue": self.venue,
            "race_time": self.race_time,
            "favourite": {
                "name": self.favourite.runner_name,
                "odds": self.favourite.best_available_to_lay,
                "selection_id": self.favourite.selection_id,
            } if self.favourite else None,
            "second_favourite": {
                "name": self.second_favourite.runner_name,
                "odds": self.second_favourite.best_available_to_lay,
                "selection_id": self.second_favourite.selection_id,
            } if self.second_favourite else None,
            "instructions": [i.to_dict() for i in self.instructions],
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "rule_applied": self.rule_applied,
            "evaluated_at": self.evaluated_at,
            "total_stake": sum(i.size for i in self.instructions),
            "total_liability": sum(i.liability for i in self.instructions),
        }


def identify_favourites(runners: list[Runner]) -> tuple[Optional[Runner], Optional[Runner]]:
    """
    Identify the favourite (lowest lay odds) and second favourite.
    Only considers ACTIVE runners with available lay prices.
    """
    active = [
        r for r in runners
        if r.status == "ACTIVE" and r.best_available_to_lay is not None
    ]

    if len(active) < 1:
        return None, None

    # Sort by best available to lay (lowest = favourite)
    active.sort(key=lambda r: r.best_available_to_lay)

    favourite = active[0]
    second_favourite = active[1] if len(active) > 1 else None

    return favourite, second_favourite


def apply_rules(
    market_id: str,
    market_name: str,
    venue: str,
    race_time: str,
    runners: list[Runner],
    jofs_enabled: bool = True,
    mark_ceiling_enabled: bool = False,
    mark_floor_enabled: bool = False,
    mark_uplift_enabled: bool = False,
    mark_uplift_stake: float = 3.0,
    # Per-rule toggles and configurable stakes (backtest only; live always uses defaults)
    rule1_enabled: bool = True,
    rule3a_enabled: bool = True,
    rule3b_enabled: bool = True,
    rule1_stake: float = 3.0,
    rule3_stake: float = 1.0,
    # Rule 2 sub-bands (replaces single Rule 2)
    rule2a_enabled: bool = True,   # 2.0 – split1
    rule2b_enabled: bool = True,   # split1 – split2
    rule2c_enabled: bool = True,   # split2 – 5.0
    rule2a_stake: float = 2.0,     # £2 lay — matches historical Rule 2 across 2.0–5.0
    rule2b_stake: float = 2.0,     # £2 lay — matches historical Rule 2 across 2.0–5.0
    rule2c_stake: float = 2.0,     # £2 lay — matches historical Rule 2 across 2.0–5.0
    rule2_split1: float = 3.0,     # boundary between 2a and 2b
    rule2_split2: float = 4.0,     # boundary between 2b and 2c
    # Rule 3 gap threshold (splits 3A from 3B)
    rule3_gap_threshold: float = 2.0,
) -> RuleResult:
    """
    Apply the lay betting rules to a market.
    Returns a RuleResult with zero or more LayInstructions.

    THE RULES (exhaustive):
      - Fav odds < 2.0  → £3 lay on fav
        JOINT/CLOSE (gap ≤ 0.2): £1.50 fav + £1.50 2nd fav
      - Fav odds 2.0–5.0 → £2 lay on fav
        JOINT/CLOSE (gap ≤ 0.2): £1.00 fav + £1.00 2nd fav
      - Fav odds > 5.0 AND gap to 2nd fav < 2 → £1 lay fav + £1 lay 2nd fav
      - Fav odds > 5.0 AND gap to 2nd fav ≥ 2 → £1 lay fav only
    """
    result = RuleResult(
        market_id=market_id,
        market_name=market_name,
        venue=venue,
        race_time=race_time,
        instructions=[],
    )

    # Step 1: Identify favourite and second favourite
    fav, second_fav = identify_favourites(runners)
    result.favourite = fav
    result.second_favourite = second_fav

    if fav is None:
        result.skipped = True
        result.skip_reason = "No active runners with available lay prices"
        return result

    odds = fav.best_available_to_lay

    # ─── Guard: Skip illiquid markets with absurd odds ───
    if odds > MAX_LAY_ODDS:
        result.skipped = True
        result.skip_reason = f"Favourite odds {odds} exceed max threshold ({MAX_LAY_ODDS})"
        return result

    # ─── Mark Rule: Hard ceiling — no lays above 8.0 ───
    if mark_ceiling_enabled and odds > 8.0:
        result.skipped = True
        result.skip_reason = f"Favourite odds {odds} exceed hard ceiling of 8.0 (Mark Rule)"
        return result

    # ─── Mark Rule: Hard floor — no lays below 1.5 ───
    if mark_floor_enabled and odds < 1.5:
        result.skipped = True
        result.skip_reason = f"Favourite odds {odds} below hard floor of 1.5 (Mark Rule)"
        return result

    # ─── Joint/Close-odds detection ───
    # Compute gap between 1st and 2nd favourite up front.
    # When jofs_enabled=False the close_odds flag stays False, reverting
    # Rules 1 & 2 to single-favourite behaviour.
    fav_gap = None
    close_odds = False
    if second_fav is not None:
        fav_gap = round(second_fav.best_available_to_lay - odds, 4)
        close_odds = jofs_enabled and (fav_gap <= CLOSE_ODDS_THRESHOLD)

    # ─── RULE 1: Favourite odds < 2.0 ───
    if odds < 2.0:
        if not rule1_enabled:
            result.skipped = True
            result.skip_reason = f"Rule 1 disabled (fav odds {odds} < 2.0)"
            return result
        joint_size = round(rule1_stake / 2, 2)
        if close_odds:
            # Joint/close favourite — split stake evenly
            result.rule_applied = (
                f"RULE_1_JOINT: Fav {odds} < 2.0, 2nd fav {second_fav.best_available_to_lay} "
                f"(gap {fav_gap:.2f} ≤ {CLOSE_ODDS_THRESHOLD}) → £{joint_size} fav + £{joint_size} 2nd fav"
            )
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=fav.selection_id,
                runner_name=fav.runner_name,
                price=odds,
                size=joint_size,
                rule_applied="RULE_1_JOINT_FAV",
            ))
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=second_fav.selection_id,
                runner_name=second_fav.runner_name,
                price=second_fav.best_available_to_lay,
                size=joint_size,
                rule_applied="RULE_1_JOINT_2ND",
            ))
        else:
            result.rule_applied = f"RULE_1: Fav odds {odds} < 2.0 → £{rule1_stake} lay"
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=fav.selection_id,
                runner_name=fav.runner_name,
                price=odds,
                size=rule1_stake,
                rule_applied="RULE_1_ODDS_UNDER_2",
            ))
        return result

    # ─── RULE 2a/2b/2c: Favourite odds 2.0–5.0 (split into 3 configurable sub-bands) ───
    if 2.0 <= odds <= 5.0:
        # Determine sub-band
        if odds < rule2_split1:
            sub_enabled, sub_stake, sub_label, band_str = rule2a_enabled, rule2a_stake, "2A", f"2.0–{rule2_split1}"
        elif odds < rule2_split2:
            sub_enabled, sub_stake, sub_label, band_str = rule2b_enabled, rule2b_stake, "2B", f"{rule2_split1}–{rule2_split2}"
        else:
            sub_enabled, sub_stake, sub_label, band_str = rule2c_enabled, rule2c_stake, "2C", f"{rule2_split2}–5.0"

        if not sub_enabled or sub_stake <= 0:
            reason = "disabled" if not sub_enabled else "stake=0 (skip band)"
            result.skipped = True
            result.skip_reason = f"Rule {sub_label} {reason} (fav odds {odds} in {band_str})"
            return result

        # Mark Rule: 2.5–3.5 band uplift
        in_uplift_band = mark_uplift_enabled and 2.5 <= odds <= 3.5
        if close_odds:
            half = (mark_uplift_stake / 2) if in_uplift_band else round(sub_stake / 2, 2)
            uplift_tag = " [UPLIFT]" if in_uplift_band else ""
            result.rule_applied = (
                f"RULE_{sub_label}_JOINT: Fav {odds} in {band_str}, 2nd fav {second_fav.best_available_to_lay} "
                f"(gap {fav_gap:.2f} ≤ {CLOSE_ODDS_THRESHOLD}) → £{half} fav + £{half} 2nd fav{uplift_tag}"
            )
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=fav.selection_id,
                runner_name=fav.runner_name,
                price=odds,
                size=half,
                rule_applied=f"RULE_{sub_label}_JOINT_FAV",
            ))
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=second_fav.selection_id,
                runner_name=second_fav.runner_name,
                price=second_fav.best_available_to_lay,
                size=half,
                rule_applied=f"RULE_{sub_label}_JOINT_2ND",
            ))
        else:
            stake = mark_uplift_stake if in_uplift_band else sub_stake
            uplift_tag = " [UPLIFT]" if in_uplift_band else ""
            result.rule_applied = f"RULE_{sub_label}: Fav odds {odds} in {band_str} → £{stake} lay{uplift_tag}"
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=fav.selection_id,
                runner_name=fav.runner_name,
                price=odds,
                size=stake,
                rule_applied=f"RULE_{sub_label}",
            ))
        return result

    # ─── RULE 3: Favourite odds > 5.0 ───
    if odds > 5.0:
        # Need second favourite to calculate gap
        if second_fav is None:
            # No second favourite — treat as 3B (single lay on favourite)
            if not rule3b_enabled:
                result.skipped = True
                result.skip_reason = f"Rule 3B disabled (fav odds {odds} > 5.0, no 2nd fav)"
                return result
            result.rule_applied = f"RULE_3B: Fav odds {odds} > 5.0, no 2nd fav → £{rule3_stake} lay fav only"
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=fav.selection_id,
                runner_name=fav.runner_name,
                price=odds,
                size=rule3_stake,
                rule_applied="RULE_3B_NO_SECOND_FAV",
            ))
            return result

        # fav_gap already computed (not None since second_fav is not None)
        if fav_gap < rule3_gap_threshold:
            # Gap < threshold → stake on fav + stake on 2nd fav (Rule 3A)
            if not rule3a_enabled:
                result.skipped = True
                result.skip_reason = f"Rule 3A disabled (fav odds {odds} > 5.0, gap {fav_gap:.2f} < {rule3_gap_threshold})"
                return result
            rule_tag = "RULE_3_JOINT" if close_odds else "RULE_3A"
            result.rule_applied = (
                f"{rule_tag}: Fav odds {odds} > 5.0, gap {fav_gap:.2f} < {rule3_gap_threshold} "
                f"→ £{rule3_stake} fav + £{rule3_stake} 2nd fav"
            )
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=fav.selection_id,
                runner_name=fav.runner_name,
                price=odds,
                size=rule3_stake,
                rule_applied=f"{rule_tag}_FAV",
            ))
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=second_fav.selection_id,
                runner_name=second_fav.runner_name,
                price=second_fav.best_available_to_lay,
                size=rule3_stake,
                rule_applied=f"{rule_tag}_2ND",
            ))
            return result

        else:
            # Gap ≥ threshold → stake on fav only (Rule 3B)
            if not rule3b_enabled:
                result.skipped = True
                result.skip_reason = f"Rule 3B disabled (fav odds {odds} > 5.0, gap {fav_gap:.2f} ≥ {rule3_gap_threshold})"
                return result
            result.rule_applied = (
                f"RULE_3B: Fav odds {odds} > 5.0, gap {fav_gap:.2f} ≥ {rule3_gap_threshold} "
                f"→ £{rule3_stake} fav only"
            )
            result.instructions.append(LayInstruction(
                market_id=market_id,
                selection_id=fav.selection_id,
                runner_name=fav.runner_name,
                price=odds,
                size=rule3_stake,
                rule_applied="RULE_3B_WIDE_GAP",
            ))
            return result

    # Should never reach here
    result.skipped = True
    result.skip_reason = f"Unexpected odds value: {odds}"
    return result


# ──────────────────────────────────────────────
#  SPREAD CONTROL — Dynamic Back/Lay Spread Validation
# ──────────────────────────────────────────────
#
#  Validates that the back-lay spread is within acceptable
#  limits for the given odds range, protecting against
#  illiquid markets where displayed odds are meaningless.
#
#  Based on Mark Insley's Spread Control Logic specification
#  (18 February 2026).

# Odds-based maximum spread thresholds.
# Key = (min_odds, max_odds), Value = max acceptable spread.
# None value = REJECT (too volatile / illiquid).
SPREAD_THRESHOLDS = [
    (1.0,  2.0,  0.05),   # Tight market — max 0.05 spread
    (2.0,  3.0,  0.15),   # Core zone — max 0.15
    (3.0,  5.0,  0.30),   # Standard — max 0.30
    (5.0,  8.0,  0.50),   # Wide — max 0.50
    (8.0,  15.0, None),   # REJECT — too volatile
    (15.0, 1000, None),   # REJECT — extreme odds
]


@dataclass
class SpreadCheckResult:
    """Result of a spread validation check."""
    passed: bool
    lay_price: float
    back_price: Optional[float]
    spread: Optional[float]       # lay - back
    max_spread: Optional[float]   # threshold for this odds range
    reason: str = ""


def check_spread(runner: Runner) -> SpreadCheckResult:
    """Validate the back-lay spread for a runner.

    Returns SpreadCheckResult indicating whether the spread
    is within acceptable limits for the given odds range.
    """
    lay = runner.best_available_to_lay
    back = runner.best_available_to_back

    if lay is None:
        return SpreadCheckResult(
            passed=False, lay_price=0, back_price=None,
            spread=None, max_spread=None,
            reason="No lay price available",
        )

    if back is None:
        # No back price means no market depth — reject
        return SpreadCheckResult(
            passed=False, lay_price=lay, back_price=None,
            spread=None, max_spread=None,
            reason=f"No back price available (lay={lay:.2f}) — insufficient market depth",
        )

    spread = round(lay - back, 4)

    # Find the threshold for this odds range
    max_spread = None
    matched_band = False
    for min_odds, max_odds, threshold in SPREAD_THRESHOLDS:
        if min_odds <= lay < max_odds:
            max_spread = threshold
            matched_band = True
            break

    if not matched_band:
        # Odds outside all defined bands
        return SpreadCheckResult(
            passed=False, lay_price=lay, back_price=back,
            spread=spread, max_spread=None,
            reason=f"Lay odds {lay:.2f} outside all defined bands",
        )

    if max_spread is None:
        # Band exists but threshold is None = REJECT
        return SpreadCheckResult(
            passed=False, lay_price=lay, back_price=back,
            spread=spread, max_spread=None,
            reason=f"Lay odds {lay:.2f} in REJECT band (8.0+) — too volatile/illiquid",
        )

    if spread > max_spread:
        inefficiency = (spread / lay) * 100
        return SpreadCheckResult(
            passed=False, lay_price=lay, back_price=back,
            spread=spread, max_spread=max_spread,
            reason=f"Spread {spread:.2f} exceeds max {max_spread:.2f} for odds {lay:.2f} ({inefficiency:.1f}% inefficiency)",
        )

    return SpreadCheckResult(
        passed=True, lay_price=lay, back_price=back,
        spread=spread, max_spread=max_spread,
    )
