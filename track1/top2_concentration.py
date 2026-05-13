"""
CHIMERA Lay Engine — TOP2_CONCENTRATION Rule Family
====================================================
Identifies races where the market is heavily concentrated in the top two
runners, indicating a potential two-horse race where standard lay logic
should be reduced or blocked.

Rule family: TOP2_CONCENTRATION
Priority: 3 in spec (after SHORT_PRICE_CONTROL, before core lay engine)
Implemented here as a post-engine backtest control — applied after the core
engine produces instructions, before the Market Overlay Modifier (MOM/RPR).

Sub-rules (Day-One launch set per spec section 14):
  TOP2_01_SCOPE                        — require >= 3 valid runners
  TOP2_02_TOP2_COMBINED_CONCENTRATION  — classify top2 concentration
  TOP2_03_THIRD_GAP_CONFIRMATION       — classify third-runner gap
  TOP2_04_TOP2_CLOSE_TOGETHER          — classify top-two closeness
  TOP2_06_MEDIUM_SUPPRESSOR            — SUPPRESS_MEDIUM (×0.60)
  TOP2_07_STRONG_SUPPRESSOR            — SUPPRESS_STRONG (×0.25)
  TOP2_08_TWO_HORSE_RACE_BLOCK         — BLOCK (×0.00)

Resolution states (spec section 10):
  NONE            — no action
  WATCH           — log only, no stake change  (TOP2_05)
  SUPPRESS_MEDIUM — lay_multiplier = 0.60      (TOP2_06)
  SUPPRESS_STRONG — lay_multiplier = 0.25      (TOP2_07)
  BLOCK           — lay_multiplier = 0.00      (TOP2_08)

Threshold logic (spec section 15):
  BLOCK:           top2_combined >= 0.80 AND third_vs_second_ratio <= 0.30
                   AND second_vs_first_ratio >= 0.85
  SUPPRESS_STRONG: top2_combined >= 0.70 AND third_vs_second_ratio <= 0.40
  SUPPRESS_MEDIUM: top2_combined >= 0.65 AND third_vs_second_ratio <= 0.50
  WATCH:           top2_combined >= 0.60 AND third_vs_second_ratio <= 0.60
  NONE:            none of the above

Derived metrics (spec section 4):
  p1 = 1 / odds_1  (implied prob of 1st favourite)
  p2 = 1 / odds_2  (implied prob of 2nd favourite)
  p3 = 1 / odds_3  (implied prob of 3rd favourite)
  top2_combined        = p1 + p2
  third_vs_second_ratio = p3 / p2
  second_vs_first_ratio = p2 / p1

Works in BACKTEST context only (not wired into live engine).
Uses best_available_to_back from ADVANCED GCS data (batb field).
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class Top2ConcentrationResult:
    """Output from apply_top2_concentration()."""
    state: str                  # NONE | WATCH | SUPPRESS_MEDIUM | SUPPRESS_STRONG | BLOCK
    lay_multiplier: float       # 1.00, 0.60, 0.25, or 0.00
    is_active: bool             # True when any suppression or block is active
    top2_combined: float        # p1 + p2
    third_vs_second_ratio: float  # p3 / p2
    second_vs_first_ratio: float  # p2 / p1
    reason_codes: List[str]     # classification tags e.g. ["TOP2_COMBINED_EXTREME", "THIRD_GAP_EXTREME"]
    reason: str                 # human-readable summary for logging
    skipped: bool = False       # True when TOP2_01_SCOPE not met (< 3 valid runners)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "lay_multiplier": self.lay_multiplier,
            "is_active": self.is_active,
            "top2_combined": round(self.top2_combined, 4),
            "third_vs_second_ratio": round(self.third_vs_second_ratio, 4),
            "second_vs_first_ratio": round(self.second_vs_first_ratio, 4),
            "reason_codes": self.reason_codes,
            "reason": self.reason,
            "skipped": self.skipped,
        }


def apply_top2_concentration(runners, enabled: bool = True) -> Top2ConcentrationResult:
    """
    Apply the TOP2_CONCENTRATION rule family to a set of runners.

    Ranks runners by exchange back odds (ascending — shortest price = favourite),
    takes top 3, converts to implied probabilities, then applies the threshold
    logic from spec section 15.

    Returns NONE (multiplier=1.00) when:
      - enabled=False
      - fewer than 3 valid runners (TOP2_01_SCOPE not met)
      - metrics do not breach any threshold band

    Returns SUPPRESS_MEDIUM (0.60), SUPPRESS_STRONG (0.25), or BLOCK (0.00)
    when concentration thresholds are breached.
    WATCH (1.00) is returned when mild concentration detected — logged only.
    """
    if not enabled:
        return Top2ConcentrationResult(
            state="NONE",
            lay_multiplier=1.0,
            is_active=False,
            top2_combined=0.0,
            third_vs_second_ratio=0.0,
            second_vs_first_ratio=0.0,
            reason_codes=[],
            reason="TOP2_CONCENTRATION disabled",
        )

    # ── TOP2_01_SCOPE: require >= 3 runners with valid exchange back odds ──
    active = sorted(
        [
            r for r in runners
            if getattr(r, "status", "ACTIVE") == "ACTIVE"
            and getattr(r, "best_available_to_back", None)
            and r.best_available_to_back > 1.0
        ],
        key=lambda r: r.best_available_to_back,  # ascending: favourite first
    )

    if len(active) < 3:
        return Top2ConcentrationResult(
            state="NONE",
            lay_multiplier=1.0,
            is_active=False,
            top2_combined=0.0,
            third_vs_second_ratio=0.0,
            second_vs_first_ratio=0.0,
            reason_codes=[],
            reason="TOP2_01_SCOPE: fewer than 3 valid runners — family skipped",
            skipped=True,
        )

    odds_1 = active[0].best_available_to_back
    odds_2 = active[1].best_available_to_back
    odds_3 = active[2].best_available_to_back

    # ── Derived probabilities ──────────────────────────────────────────────
    p1 = 1.0 / odds_1
    p2 = 1.0 / odds_2
    p3 = 1.0 / odds_3

    # ── Derived concentration metrics ──────────────────────────────────────
    top2_combined = p1 + p2
    third_vs_second_ratio = p3 / p2
    second_vs_first_ratio = p2 / p1

    # ── TOP2_02: classify top-two combined concentration ───────────────────
    reason_codes: List[str] = []
    if top2_combined >= 0.80:
        reason_codes.append("TOP2_COMBINED_EXTREME")
    elif top2_combined >= 0.70:
        reason_codes.append("TOP2_COMBINED_STRONG")
    elif top2_combined >= 0.65:
        reason_codes.append("TOP2_COMBINED_MEDIUM")
    elif top2_combined >= 0.60:
        reason_codes.append("TOP2_COMBINED_MILD")

    # ── TOP2_03: classify third-vs-second gap ──────────────────────────────
    if third_vs_second_ratio <= 0.30:
        reason_codes.append("THIRD_GAP_EXTREME")
    elif third_vs_second_ratio <= 0.40:
        reason_codes.append("THIRD_GAP_STRONG")
    elif third_vs_second_ratio <= 0.50:
        reason_codes.append("THIRD_GAP_MEDIUM")
    elif third_vs_second_ratio <= 0.60:
        reason_codes.append("THIRD_GAP_MILD")

    # ── TOP2_04: classify top-two closeness ────────────────────────────────
    if second_vs_first_ratio >= 0.92:
        reason_codes.append("TOP2_VERY_CLOSE")
    elif second_vs_first_ratio >= 0.85:
        reason_codes.append("TOP2_CLOSE")

    # ── Resolution logic — spec section 15 ────────────────────────────────

    # TOP2_08_TWO_HORSE_RACE_BLOCK — extreme two-horse race: BLOCK
    if (top2_combined >= 0.80
            and third_vs_second_ratio <= 0.30
            and second_vs_first_ratio >= 0.85):
        return Top2ConcentrationResult(
            state="BLOCK",
            lay_multiplier=0.00,
            is_active=True,
            top2_combined=top2_combined,
            third_vs_second_ratio=third_vs_second_ratio,
            second_vs_first_ratio=second_vs_first_ratio,
            reason_codes=reason_codes,
            reason=(
                f"TOP2_08_TWO_HORSE_RACE_BLOCK: extreme two-horse race "
                f"(top2={top2_combined:.4f}, 3v2={third_vs_second_ratio:.4f}, "
                f"2v1={second_vs_first_ratio:.4f}) — no lay"
            ),
        )

    # TOP2_07_STRONG_SUPPRESSOR — SUPPRESS_STRONG ×0.25
    if top2_combined >= 0.70 and third_vs_second_ratio <= 0.40:
        return Top2ConcentrationResult(
            state="SUPPRESS_STRONG",
            lay_multiplier=0.25,
            is_active=True,
            top2_combined=top2_combined,
            third_vs_second_ratio=third_vs_second_ratio,
            second_vs_first_ratio=second_vs_first_ratio,
            reason_codes=reason_codes,
            reason=(
                f"TOP2_07_STRONG_SUPPRESSOR: strong top-two market "
                f"(top2={top2_combined:.4f}, 3v2={third_vs_second_ratio:.4f}) — ×0.25"
            ),
        )

    # TOP2_06_MEDIUM_SUPPRESSOR — SUPPRESS_MEDIUM ×0.60
    if top2_combined >= 0.65 and third_vs_second_ratio <= 0.50:
        return Top2ConcentrationResult(
            state="SUPPRESS_MEDIUM",
            lay_multiplier=0.60,
            is_active=True,
            top2_combined=top2_combined,
            third_vs_second_ratio=third_vs_second_ratio,
            second_vs_first_ratio=second_vs_first_ratio,
            reason_codes=reason_codes,
            reason=(
                f"TOP2_06_MEDIUM_SUPPRESSOR: moderate top-two concentration "
                f"(top2={top2_combined:.4f}, 3v2={third_vs_second_ratio:.4f}) — ×0.60"
            ),
        )

    # TOP2_05_MILD_WARNING_STATE — WATCH, log only, no stake change
    if top2_combined >= 0.60 and third_vs_second_ratio <= 0.60:
        return Top2ConcentrationResult(
            state="WATCH",
            lay_multiplier=1.00,
            is_active=False,
            top2_combined=top2_combined,
            third_vs_second_ratio=third_vs_second_ratio,
            second_vs_first_ratio=second_vs_first_ratio,
            reason_codes=reason_codes,
            reason=(
                f"TOP2_05_MILD_WARNING_STATE: mild top-two concentration "
                f"(top2={top2_combined:.4f}, 3v2={third_vs_second_ratio:.4f}) — WATCH only"
            ),
        )

    # NONE — no concentration detected
    return Top2ConcentrationResult(
        state="NONE",
        lay_multiplier=1.00,
        is_active=False,
        top2_combined=top2_combined,
        third_vs_second_ratio=third_vs_second_ratio,
        second_vs_first_ratio=second_vs_first_ratio,
        reason_codes=reason_codes,
        reason=(
            f"TOP2_CONCENTRATION: no concentration "
            f"(top2={top2_combined:.4f}, 3v2={third_vs_second_ratio:.4f})"
        ),
    )
