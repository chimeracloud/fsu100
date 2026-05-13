"""The single evaluator function — same code path for live and backtest.

This module implements the 13-step pipeline described in
``docs/STRATEGY_SPECIFICATION.md``. The function :func:`evaluate` takes
a :class:`models.MarketSnapshot` and a :class:`settings.Settings`, runs
every step in order, and returns one :class:`models.EvaluationResult`.

The live engine calls this function for each market that enters the
process window. The backtest tool calls this same function for each
historic snapshot. They cannot diverge — there is only one function.

The pipeline order matches the strategy specification verbatim:

  0  Fetch market               (caller's job — engine or backtest)
  1  Spread Control             rules.check_spread
  2  Core Rules                 rules.apply_rules (with controls inline)
  2.1 MAX_LAY_ODDS guard         inside apply_rules
  2.2 Mark Ceiling               inside apply_rules
  2.3 Mark Floor                 inside apply_rules
  2.4 JOFS split                 inside apply_rules
  2.5 Mark Uplift                inside apply_rules
  3  Point Value multiplier
  4  Signal: Overround           signal_filters.apply_signal_filters
  5  Signal: Field Size          signal_filters.apply_signal_filters
  6  Signal: Steam Gate          signal_filters.apply_signal_filters
  7  Signal: Band Performance    signal_filters.apply_signal_filters
  8  TOP2 Concentration          top2_concentration.apply_top2_concentration
  9  Market Overlay Modifier     market_overlay.apply_market_overlay
  10 Settlement & P&L            engine / backtest — not part of evaluate()

Every step appends to ``result.pipeline`` so the portal can render the
trace. Short-circuiting (e.g. spread control rejects, all rules skip)
still produces a populated trace explaining what happened and why.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rules import (  # type: ignore[import-not-found]
    apply_rules,
    check_spread,
)
from signal_filters import (  # type: ignore[import-not-found]
    SignalConfig,
    apply_signal_filters,
)
from top2_concentration import (  # type: ignore[import-not-found]
    apply_top2_concentration,
)
from market_overlay import apply_market_overlay  # type: ignore[import-not-found]

from models import EvaluationResult, MarketSnapshot
from settings import Settings


# Betfair minimum stake — clamp applied at signal/TOP2/MOM steps when the
# pre-modifier stake was already >= £2.00.
BETFAIR_MIN_STAKE = 2.0


def evaluate(snapshot: MarketSnapshot, settings: Settings) -> EvaluationResult:
    """Run the full 13-step pipeline against one market snapshot.

    Returns an :class:`EvaluationResult`. The result may be:

    * ``skipped=True`` — the market produced no bets (with ``skip_reason``)
    * ``skipped=False`` with non-empty ``instructions`` — bets to place
    * ``skipped=False`` with empty ``instructions`` — every bet got
      filtered out (rare; the skip_reason explains).

    The ``pipeline`` field is always populated so callers can render
    the trace whether a bet was produced or not.
    """

    result = EvaluationResult(
        market_id=snapshot.market_id,
        market_name=snapshot.market_name,
        venue=snapshot.venue,
        race_time=snapshot.race_time,
        evaluated_at=_now_iso(),
    )

    # ── Step 1: Spread Control (favourite only) ─────────────────────────
    if settings.controls.spread_control_enabled:
        fav_for_spread = _pick_favourite(snapshot)
        if fav_for_spread is not None:
            spread_check = check_spread(fav_for_spread)
            if not spread_check.passed:
                result.skipped = True
                result.skip_reason = f"Spread rejected: {spread_check.reason}"
                _trace(result, "spread_control", spread_check.reason)
                return result
            _trace(
                result,
                "spread_control",
                f"spread {spread_check.spread} ≤ max {spread_check.max_spread}",
            )
        else:
            _trace(result, "spread_control", "no favourite — skip spread check")
    else:
        _trace(result, "spread_control", "disabled")

    # ── Step 2: Core Rules (with embedded MAX_LAY_ODDS, Mark Ceiling,
    #            Mark Floor, JOFS, Mark Uplift) ──────────────────────────
    rule_result = apply_rules(
        market_id=snapshot.market_id,
        market_name=snapshot.market_name,
        venue=snapshot.venue,
        race_time=snapshot.race_time,
        runners=snapshot.runners,
        jofs_enabled=settings.controls.jofs_enabled,
        mark_ceiling_enabled=settings.controls.mark_ceiling_enabled,
        mark_floor_enabled=settings.controls.mark_floor_enabled,
        mark_uplift_enabled=settings.controls.mark_uplift_enabled,
        mark_uplift_stake=settings.controls.mark_uplift_stake,
        rule1_enabled=settings.rules.rule1_enabled,
        rule3a_enabled=settings.rules.rule3a_enabled,
        rule3b_enabled=settings.rules.rule3b_enabled,
        rule1_stake=settings.rules.rule1_stake,
        rule3a_stake=settings.rules.rule3a_stake,
        rule3b_stake=settings.rules.rule3b_stake,
        rule2a_enabled=settings.rules.rule2a_enabled,
        rule2b_enabled=settings.rules.rule2b_enabled,
        rule2c_enabled=settings.rules.rule2c_enabled,
        rule2a_stake=settings.rules.rule2a_stake,
        rule2b_stake=settings.rules.rule2b_stake,
        rule2c_stake=settings.rules.rule2c_stake,
        rule2_split1=settings.rules.rule2_split1,
        rule2_split2=settings.rules.rule2_split2,
        rule3_gap_threshold=settings.rules.rule3_gap_threshold,
    )

    # Copy favourite/2nd-fav info from rule result.
    if rule_result.favourite:
        result.favourite = {
            "name": rule_result.favourite.runner_name,
            "odds": rule_result.favourite.best_available_to_lay,
            "selection_id": rule_result.favourite.selection_id,
        }
    if rule_result.second_favourite:
        result.second_favourite = {
            "name": rule_result.second_favourite.runner_name,
            "odds": rule_result.second_favourite.best_available_to_lay,
            "selection_id": rule_result.second_favourite.selection_id,
        }

    if rule_result.skipped:
        result.skipped = True
        result.skip_reason = rule_result.skip_reason
        result.rule_applied = rule_result.rule_applied
        _trace(result, "core_rules", rule_result.skip_reason)
        return result

    result.instructions = list(rule_result.instructions)
    result.rule_applied = rule_result.rule_applied
    _trace(
        result,
        "core_rules",
        rule_result.rule_applied
        or f"{len(result.instructions)} instruction(s) produced",
    )

    # ── Step 3: Point Value multiplier (pts → £) ────────────────────────
    pv = float(settings.general.point_value)
    if pv != 1.0:
        for instr in result.instructions:
            instr.size = round(instr.size * pv, 2)
        _trace(result, "point_value", f"stake × {pv}/pt applied")
    else:
        _trace(result, "point_value", "£1/pt — no scaling")

    # ── Steps 4–7: Signal Filters (per-instruction) ─────────────────────
    signal_cfg = _signal_config(settings)
    surviving: list = []
    fav_odds = (
        rule_result.favourite.best_available_to_lay
        if rule_result.favourite
        else None
    )

    for instr in result.instructions:
        filter_result = apply_signal_filters(
            selection_id=instr.selection_id,
            current_price=fav_odds or instr.price,
            original_stake=instr.size,
            all_runners=snapshot.runners,
            previous_prices=snapshot.previous_prices,
            band_stats=snapshot.band_stats,
            config=signal_cfg,
        )
        for v in filter_result.verdicts:
            if v.fired:
                _trace(
                    result,
                    f"signal_{v.signal.lower()}",
                    f"{v.action}: {v.reason}",
                )
        if not filter_result.allowed:
            _trace(
                result,
                "signal_filters",
                f"bet blocked: {filter_result.skip_reason}",
            )
            continue
        instr.size = filter_result.final_stake
        surviving.append(instr)
    result.instructions = surviving

    if not result.instructions:
        result.skipped = True
        result.skip_reason = "All instructions blocked by signal filters"
        return result

    # ── Step 8: TOP2 Concentration ──────────────────────────────────────
    top2 = apply_top2_concentration(
        snapshot.runners, enabled=settings.risk.top2_concentration_enabled
    )
    if top2.is_active and top2.lay_multiplier == 0.0:
        # BLOCK — clear all instructions.
        result.skipped = True
        result.skip_reason = top2.reason
        _trace(result, "top2", top2.reason)
        return result
    if top2.is_active and top2.lay_multiplier < 1.0:
        for instr in result.instructions:
            original = instr.size
            scaled = round(original * top2.lay_multiplier, 2)
            if original >= BETFAIR_MIN_STAKE:
                scaled = max(scaled, BETFAIR_MIN_STAKE)
            instr.size = scaled
        _trace(result, "top2", top2.reason)
    else:
        _trace(result, "top2", top2.reason)

    # ── Step 9: Market Overlay Modifier (MOM) ───────────────────────────
    mom = apply_market_overlay(
        snapshot.runners, enabled=settings.risk.market_overlay_enabled
    )
    if mom.overlay_multiplier != 1.0:
        for instr in result.instructions:
            original = instr.size
            scaled = round(original * mom.overlay_multiplier, 2)
            if original >= BETFAIR_MIN_STAKE:
                scaled = max(scaled, BETFAIR_MIN_STAKE)
            instr.size = scaled
    _trace(result, "market_overlay", mom.reason)

    return result


# ──────────────────────────────────────────────────────────────────────────────


def _signal_config(settings: Settings) -> SignalConfig:
    """Map flat :class:`Settings` onto :class:`SignalConfig`."""

    s = settings.signals
    return SignalConfig(
        overround_enabled=s.signal_overround_enabled,
        overround_soft_threshold=s.overround_soft_threshold,
        overround_hard_threshold=s.overround_hard_threshold,
        field_size_enabled=s.signal_field_size_enabled,
        field_size_max_runners=s.field_size_max_runners,
        field_size_odds_min=s.field_size_odds_min,
        field_size_stake_cap=s.field_size_stake_cap,
        steam_gate_enabled=s.signal_steam_gate_enabled,
        steam_gate_odds_min=s.steam_gate_odds_min,
        steam_shortening_pct=s.steam_shortening_pct,
        band_perf_enabled=s.signal_band_perf_enabled,
        band_perf_lookback_days=s.band_perf_lookback_days,
        band_perf_min_win_rate=s.band_perf_min_win_rate,
        band_perf_min_sample=s.band_perf_min_sample,
        band_perf_reduced_stake=s.band_perf_reduced_stake,
    )


def _pick_favourite(snapshot: MarketSnapshot):
    """Find the favourite the same way ``rules.identify_favourites`` does
    — for the spread-control pre-check."""

    active = [
        r
        for r in snapshot.runners
        if r.status == "ACTIVE" and r.best_available_to_lay is not None
    ]
    if not active:
        return None
    active.sort(key=lambda r: r.best_available_to_lay)
    return active[0]


def _trace(result: EvaluationResult, step: str, detail: str) -> None:
    """Append one trace event to the result."""

    result.pipeline.append({"step": step, "detail": detail})


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
