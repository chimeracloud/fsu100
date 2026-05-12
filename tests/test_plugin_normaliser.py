"""Tests for :mod:`core.plugin_normaliser` — the plugin Acceptor.

Each test exercises one buffer rule from the v2 template's
``_acceptor_buffer_rules``. Tests pair a "broken" payload with the
expected repair message and verify the result also passes Pydantic
validation against :class:`PluginConfig`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.plugin_normaliser import normalise_plugin_payload
from models.schemas import PluginConfig


def _minimal_rule_plugin() -> dict:
    return {
        "name": "test_plugin_v1",
        "version": "1.0.0",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 1}
            ]
        },
    }


def test_minimal_plugin_validates_after_defaults_filled():
    payload = _minimal_rule_plugin()
    repaired, _ = normalise_plugin_payload(payload)
    # Strict required fields preserved, optionals filled with defaults.
    assert repaired["name"] == "test_plugin_v1"
    assert repaired["version"] == "1.0.0"
    PluginConfig.model_validate(repaired)


def test_fully_populated_plugin_no_repairs():
    payload = {
        "name": "complete_plugin_v1",
        "version": "1.0.0",
        "description": "Fully spec'd plugin — no repairs expected.",
        "author": "qa",
        "sport": "horse_racing",
        "plugin_role": "rule",
        "compatible_tools": ["backtest-tool", "fsu100-betting-engine"],
        "parser": {
            "format": "betfair_mcm",
            "time_before_off_seconds": 300,
            "price_field": "ltp",
            "extract_bsp": True,
        },
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 1, "enabled": True}
            ],
            "controls": {},
        },
        "staking": {"point_value": 1.0},
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repairs == []
    PluginConfig.model_validate(repaired)


def test_alias_plugin_name_rewritten_to_name():
    payload = {
        "plugin_name": "marks_strategy_v1",
        "plugin_version": "1.2.3",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 1}
            ]
        },
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["name"] == "marks_strategy_v1"
    assert repaired["version"] == "1.2.3"
    assert "renamed 'plugin_name' → 'name'" in repairs
    assert "renamed 'plugin_version' → 'version'" in repairs
    PluginConfig.model_validate(repaired)


def test_name_missing_uses_url_hint():
    payload = {
        "version": "1.0.0",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 1}
            ]
        },
    }
    repaired, repairs = normalise_plugin_payload(payload, name_hint="from_url")
    assert repaired["name"] == "from_url"
    assert any("URL hint" in r for r in repairs)


def test_name_missing_synthesised_from_author_and_description():
    payload = {
        "author": "Mark Insley",
        "description": "Lay favourites",
        "version": "1.0.0",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 1}
            ]
        },
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["name"].startswith("mark_insley_lay_favourites_v")
    assert any("synthesised" in r for r in repairs)


def test_version_missing_defaults_to_1_0_0():
    payload = _minimal_rule_plugin()
    del payload["version"]
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["version"] == "1.0.0"
    assert any("version missing" in r for r in repairs)


def test_version_non_semver_coerced():
    payload = _minimal_rule_plugin()
    payload["version"] = "v2"
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["version"] == "1.0.0"
    assert any("not semver" in r for r in repairs)


def test_top_level_rules_hoisted_into_strategy():
    payload = {
        "name": "marks_top_level_rules_v1",
        "version": "1.0.0",
        "rules": [{"name": "rule_1", "odds_band": [1.5, 2.0], "base_stake": 1}],
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert "rules" not in repaired
    assert repaired["strategy"]["rules"][0]["name"] == "rule_1"
    assert any("top-level 'rules'" in r for r in repairs)
    PluginConfig.model_validate(repaired)


def test_control_plugin_no_rules_gets_noop_injected():
    payload = {
        "name": "three_horse_cluster_guardrail_v1",
        "version": "1.0.0",
        "plugin_role": "control",
        "strategy": {"controls": {}},
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["name"] == "noop_passthrough"
    assert repaired["strategy"]["rules"][0]["base_stake"] == 0
    assert repaired["strategy"]["rules"][0]["enabled"] is False
    assert any("noop_passthrough" in r for r in repairs)
    PluginConfig.model_validate(repaired)


def test_modifier_plugin_no_rules_also_gets_noop_injected():
    payload = {
        "name": "mod_plugin_v1",
        "version": "1.0.0",
        "plugin_role": "modifier",
        "strategy": {},
    }
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["name"] == "noop_passthrough"
    PluginConfig.model_validate(repaired)


def test_rule_plugin_without_rules_leaves_pydantic_to_422():
    payload = {
        "name": "broken_rule_plugin_v1",
        "version": "1.0.0",
        "plugin_role": "rule",
        "strategy": {},
    }
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired["strategy"].get("rules") in (None, [])
    with pytest.raises(ValidationError):
        PluginConfig.model_validate(repaired)


def test_odds_band_as_object_coerced_to_list():
    payload = {
        "name": "broken_band_v1",
        "version": "1.0.0",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": {"min": 1.5, "max": 2.0}, "base_stake": 1}
            ]
        },
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["odds_band"] == [1.5, 2.0]
    assert any("odds_band" in r for r in repairs)
    PluginConfig.model_validate(repaired)


def test_odds_band_as_strings_coerced_to_floats():
    payload = {
        "name": "string_band_v1",
        "version": "1.0.0",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": ["1.5", "2.0"], "base_stake": 1}
            ]
        },
    }
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["odds_band"] == [1.5, 2.0]
    PluginConfig.model_validate(repaired)


def test_odds_band_inverted_swapped():
    payload = {
        "name": "inverted_v1",
        "version": "1.0.0",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [3.0, 1.5], "base_stake": 1}
            ]
        },
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["odds_band"] == [1.5, 3.0]
    assert any("odds_band" in r for r in repairs)
    PluginConfig.model_validate(repaired)


def test_odds_band_below_betfair_min_clamped():
    payload = {
        "name": "low_band_v1",
        "version": "1.0.0",
        "strategy": {
            "rules": [
                {"name": "rule_1", "odds_band": [0.5, 2.0], "base_stake": 1}
            ]
        },
    }
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["odds_band"][0] == 1.01
    PluginConfig.model_validate(repaired)


def test_rule_with_no_stake_parked():
    payload = {
        "name": "no_stake_v1",
        "version": "1.0.0",
        "strategy": {
            "rules": [{"name": "rule_1", "odds_band": [1.5, 2.0]}]
        },
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["base_stake"] == 0
    assert repaired["strategy"]["rules"][0]["enabled"] is False
    assert any("parked" in r for r in repairs)
    PluginConfig.model_validate(repaired)


def test_rule_name_synthesised_when_missing():
    payload = {
        "name": "anon_rule_v1",
        "version": "1.0.0",
        "strategy": {
            "rules": [{"odds_band": [1.5, 2.0], "base_stake": 1}]
        },
    }
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["strategy"]["rules"][0]["name"] == "rule_1"
    assert any("synthesised 'rule_1'" in r for r in repairs)
    PluginConfig.model_validate(repaired)


def test_parser_missing_defaults_to_canonical_block():
    payload = _minimal_rule_plugin()
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired["parser"] == {
        "format": "betfair_mcm",
        "time_before_off_seconds": 300,
        "price_field": "ltp",
        "extract_bsp": True,
    }


def test_parser_partial_merged_over_defaults():
    payload = _minimal_rule_plugin()
    payload["parser"] = {"time_before_off_seconds": 600}
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired["parser"]["time_before_off_seconds"] == 600
    assert repaired["parser"]["format"] == "betfair_mcm"


def test_unknown_parser_format_coerced():
    payload = _minimal_rule_plugin()
    payload["parser"] = {"format": "racing_api_json"}
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["parser"]["format"] == "betfair_mcm"
    assert any("unsupported" in r for r in repairs)


def test_plugin_role_default_is_rule():
    payload = _minimal_rule_plugin()
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired["plugin_role"] == "rule"


def test_plugin_role_unknown_coerced_to_rule():
    payload = _minimal_rule_plugin()
    payload["plugin_role"] = "weird_role"
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["plugin_role"] == "rule"
    assert any("not recognised" in r for r in repairs)


def test_marks_three_horse_cluster_chimera_plugin_loads():
    """End-to-end: Mark's actual ChatGPT-authored plugin should now load."""

    payload = {
        "plugin_name": "three_horse_cluster_guardrail",
        "plugin_version": "1.0.0",
        "plugin_type": "control",
        "enabled": True,
        "purpose": "Detect pre-off markets where the top three runners are tightly clustered.",
        "rules": [
            {
                "rule_id": "THC_SCOPE_001",
                "priority": 1,
                "conditions": [],
                "effects": {"market_condition": "WATCH"},
            }
        ],
    }
    repaired, repairs = normalise_plugin_payload(payload, name_hint="three_horse_cluster_guardrail")

    assert repaired["name"] == "three_horse_cluster_guardrail"
    assert repaired["version"] == "1.0.0"
    assert repaired["plugin_role"] == "control"
    assert "rules" not in repaired
    assert "strategy" in repaired
    PluginConfig.model_validate(repaired)


def test_staking_missing_defaults_to_point_value_1():
    payload = _minimal_rule_plugin()
    repaired, repairs = normalise_plugin_payload(payload)
    assert repaired["staking"] == {"point_value": 1.0}
    assert any("staking missing" in r for r in repairs)


def test_compatible_tools_default():
    payload = _minimal_rule_plugin()
    repaired, _ = normalise_plugin_payload(payload)
    assert "backtest-tool" in repaired["compatible_tools"]
    assert "fsu100-betting-engine" in repaired["compatible_tools"]


def test_description_auto_generated_when_missing():
    payload = _minimal_rule_plugin()
    repaired, repairs = normalise_plugin_payload(payload)
    assert "auto-generated" in repaired["description"]
    assert any("description missing" in r for r in repairs)


def test_in_place_mutation_only():
    payload = _minimal_rule_plugin()
    repaired, _ = normalise_plugin_payload(payload)
    assert repaired is payload, "normaliser should mutate in place"


def test_non_dict_payload_raises_type_error():
    with pytest.raises(TypeError):
        normalise_plugin_payload([1, 2, 3])  # type: ignore[arg-type]
