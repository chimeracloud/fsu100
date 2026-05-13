"""Shared models used across the engine, evaluator, and HTTP layer.

The ``Runner`` dataclass lives in :mod:`rules` (copied verbatim from
charles-ascot/lay-engine). This module adds:

* :class:`MarketSnapshot` — the input to :func:`evaluator.evaluate`.
* :class:`EvaluationResult` — the output of :func:`evaluator.evaluate`.
* :class:`SettledBet` — one row of the daily settled-results file.

By design these are all plain dataclasses (no Pydantic). The portal
serialises them via ``to_dict()``; the evaluator is a pure function.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from rules import LayInstruction, Runner  # type: ignore[import-not-found]


@dataclass
class MarketSnapshot:
    """One point-in-time view of a Betfair WIN market.

    Identical shape whether sourced from the live stream or a historic
    file. The evaluator does not care which — it sees the same fields.
    """

    market_id: str
    market_name: str
    venue: str
    country: str
    race_time: str  # ISO 8601
    snapshot_at: str  # ISO 8601 — when the snapshot was taken
    runners: list[Runner]
    # ``previous_prices`` is used by the Steam Gate signal — keyed by
    # selection_id, value is the best_available_to_lay at T-15min.
    # Optional: when not provided, Steam Gate skips with reason "no
    # previous price snapshot".
    previous_prices: dict[int, float] = field(default_factory=dict)
    # Per-band rolling stats used by the Band Performance signal.
    # Optional: when not provided, the signal skips with reason "below
    # min sample".
    band_stats: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class EvaluationResult:
    """The output of one pass through the evaluator pipeline.

    This is what the live engine reads to decide whether to place
    bets, and what the backtest writes to its daily results file.
    """

    market_id: str
    market_name: str
    venue: str
    race_time: str
    evaluated_at: str

    favourite: Optional[dict] = None  # {name, odds, selection_id}
    second_favourite: Optional[dict] = None

    instructions: list[LayInstruction] = field(default_factory=list)

    # Pipeline outcomes — each step that ran logs a record so the
    # portal can show the trace. Each entry is {"step", "detail"}.
    pipeline: list[dict[str, Any]] = field(default_factory=list)

    skipped: bool = False
    skip_reason: str = ""
    rule_applied: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "market_name": self.market_name,
            "venue": self.venue,
            "race_time": self.race_time,
            "evaluated_at": self.evaluated_at,
            "favourite": self.favourite,
            "second_favourite": self.second_favourite,
            "instructions": [i.to_dict() for i in self.instructions],
            "pipeline": list(self.pipeline),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "rule_applied": self.rule_applied,
            "total_stake": sum(i.size for i in self.instructions),
            "total_liability": sum(i.liability for i in self.instructions),
        }


@dataclass
class PlacedBet:
    """A bet sent to (or simulated against) Betfair.

    Includes the full Betfair ``bet_id`` so the settlement step can
    poll ``list_cleared_orders`` and match results back to placements.
    """

    market_id: str
    selection_id: int
    runner_name: str
    side: str  # always "LAY" in this build
    price: float
    stake: float
    liability: float
    rule_applied: str
    bet_id: Optional[str] = None
    placed_at: str = field(default_factory=lambda: _now())
    simulated: bool = False  # True in DRY_RUN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SettledBet:
    """One row of the daily settled-results file."""

    market_id: str
    market_name: str
    venue: str
    race_time: str
    selection_id: int
    runner_name: str
    rule_applied: str
    side: str
    price: float
    stake: float
    liability: float
    outcome: str  # WON | LOST | VOID
    pnl: float
    settled_at: str = field(default_factory=lambda: _now())
    bet_id: Optional[str] = None
    simulated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
