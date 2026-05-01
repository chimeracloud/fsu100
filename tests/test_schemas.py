"""Validation tests for the FSU100 request/response schemas.

These check that the contract documented in the README is enforced by
Pydantic — a typo in a strategy field returns 422 long before the engine
attempts to act on it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.schemas import (
    AdminConfig,
    BetDecisionView,
    CancelRequest,
    EngineMode,
    EvaluateRequest,
    GcsSourceConfig,
    PlaceRequest,
    PluginConfig,
    SourceType,
    StrategyConfig,
    StrategyControls,
    StrategyRule,
)


def _valid_plugin_payload() -> dict:
    return {
        "name": "mark_4rule_lay_v1",
        "version": "1.0.0",
        "source": {
            "type": "gcs",
            "bucket": "gs://betfair-basic-historic/ADVANCED/",
            "date_range": {"start": "2025-01-01", "end": "2025-01-31"},
            "filters": {
                "countries": ["GB", "IE"],
                "market_types": ["WIN"],
            },
        },
        "parser": {
            "format": "betfair_mcm",
            "time_before_off_seconds": 300,
            "price_field": "ltp",
            "extract_bsp": True,
        },
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 3}
            ],
            "controls": {"hard_floor": 1.5, "hard_ceiling": 8.0},
        },
        "staking": {"point_value": 7.5},
    }


def _valid_evaluate_payload() -> dict:
    return {
        "market_snapshot": {
            "market_id": "1.234567890",
            "publish_time": "2026-06-01T13:55:00+00:00",
            "inplay": False,
            "market_definition": {
                "market_time": "2026-06-01T14:00:00+00:00",
                "venue": "Kempton",
                "country_code": "GB",
                "market_type": "WIN",
                "in_play": False,
                "runners": [
                    {"selection_id": 101, "name": "Alpha", "status": "ACTIVE"},
                    {"selection_id": 102, "name": "Bravo", "status": "ACTIVE"},
                ],
            },
            "runners": [
                {
                    "selection_id": 101,
                    "name": "Alpha",
                    "status": "ACTIVE",
                    "last_price_traded": 1.80,
                },
                {
                    "selection_id": 102,
                    "name": "Bravo",
                    "status": "ACTIVE",
                    "last_price_traded": 4.00,
                },
            ],
        },
        "plugin": _valid_plugin_payload(),
    }


def test_full_plugin_validates() -> None:
    plugin = PluginConfig.model_validate(_valid_plugin_payload())
    assert isinstance(plugin.source, GcsSourceConfig)
    assert plugin.source.type is SourceType.GCS
    assert len(plugin.strategy.rules) == 1


def test_evaluate_request_validates() -> None:
    request = EvaluateRequest.model_validate(_valid_evaluate_payload())
    assert request.market_snapshot.market_id == "1.234567890"
    assert request.plugin.staking.point_value == 7.5


def test_unknown_top_level_field_is_rejected() -> None:
    payload = _valid_evaluate_payload()
    payload["unexpected"] = "value"
    with pytest.raises(ValidationError):
        EvaluateRequest.model_validate(payload)


def test_rule_without_stake_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyRule.model_validate({"name": "x", "odds_band": [1.5, 2.0]})


def test_floor_must_be_below_ceiling() -> None:
    with pytest.raises(ValidationError):
        StrategyControls.model_validate(
            {"hard_floor": 5.0, "hard_ceiling": 4.0}
        )


def test_date_range_must_be_ordered() -> None:
    payload = _valid_plugin_payload()
    payload["source"]["date_range"] = {
        "start": "2025-02-01",
        "end": "2025-01-01",
    }
    with pytest.raises(ValidationError):
        PluginConfig.model_validate(payload)


def test_strategy_requires_at_least_one_rule() -> None:
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate({"rules": []})


def test_plugin_round_trips_to_json_and_back() -> None:
    plugin = PluginConfig.model_validate(_valid_plugin_payload())
    serialised = plugin.model_dump_json()
    rehydrated = PluginConfig.model_validate_json(serialised)
    assert rehydrated == plugin


def test_strategy_controls_accept_unknown_fields() -> None:
    """Plugins can add new controls without breaking schema validation."""

    controls = StrategyControls.model_validate(
        {
            "hard_floor": 1.5,
            "hard_ceiling": 10.0,
            "magic_factor": 1.5,
            "vendor_specific_flag": True,
        }
    )
    assert controls.hard_floor == 1.5
    assert getattr(controls, "magic_factor", None) == 1.5


def test_admin_config_validates() -> None:
    payload = {
        "log_level": "INFO",
        "activity_log_size": 200,
        "results_bucket": "chiops-fsu100-results",
        "active_plugin": "mark_4rule_lay_v1",
        "countries": ["GB", "IE"],
        "market_types": ["WIN"],
        "point_value": 7.5,
        "customer_strategy_ref": "fsu100",
    }
    config = AdminConfig.model_validate(payload)
    assert config.active_plugin == "mark_4rule_lay_v1"
    assert config.countries == ["GB", "IE"]


def test_admin_config_rejects_invalid_log_level() -> None:
    payload = {
        "log_level": "VERBOSE",
        "activity_log_size": 200,
        "results_bucket": "chiops-fsu100-results",
        "active_plugin": "mark_4rule_lay_v1",
        "countries": ["GB"],
        "market_types": ["WIN"],
        "point_value": 7.5,
    }
    with pytest.raises(ValidationError):
        AdminConfig.model_validate(payload)


def test_place_request_validates() -> None:
    payload = {
        "market_id": "1.234567890",
        "decision": {
            "selection_id": 101,
            "runner_name": "Alpha",
            "side": "LAY",
            "price": 1.80,
            "stake": 22.50,
            "liability": 18.00,
            "rule_applied": "rule_1",
            "notes": "",
        },
        "persistence_type": "LAPSE",
    }
    request = PlaceRequest.model_validate(payload)
    assert request.market_id == "1.234567890"
    assert request.decision.side == "LAY"


def test_place_request_rejects_unknown_persistence_type() -> None:
    payload = {
        "market_id": "1.234567890",
        "decision": {
            "selection_id": 101,
            "runner_name": "Alpha",
            "side": "LAY",
            "price": 1.80,
            "stake": 22.50,
            "liability": 18.00,
            "rule_applied": "rule_1",
        },
        "persistence_type": "FOREVER",
    }
    with pytest.raises(ValidationError):
        PlaceRequest.model_validate(payload)


def test_cancel_request_requires_positive_size_reduction() -> None:
    with pytest.raises(ValidationError):
        CancelRequest.model_validate(
            {
                "market_id": "1.234567890",
                "bet_id": "abc",
                "size_reduction": -1.0,
            }
        )


def test_engine_mode_enum_values() -> None:
    """The mode enum is contract — drift here breaks the portal."""

    assert EngineMode.LIVE.value == "LIVE"
    assert EngineMode.DRY_RUN.value == "DRY_RUN"
    assert EngineMode.STOPPED.value == "STOPPED"


def test_bet_decision_view_round_trips() -> None:
    payload = {
        "selection_id": 101,
        "runner_name": "Alpha",
        "side": "LAY",
        "price": 1.80,
        "stake": 22.50,
        "liability": 18.00,
        "rule_applied": "rule_1",
        "notes": "",
    }
    view = BetDecisionView.model_validate(payload)
    serialised = view.model_dump()
    assert BetDecisionView.model_validate(serialised) == view
