"""Plugin Acceptor — normalise raw plugin payloads before validation.

Operators (and the AIs they use) author plugins that don't always match
the strict :class:`models.schemas.PluginConfig` shape. Rather than
returning a wall of Pydantic errors, the Acceptor applies a fixed set of
repair rules to the raw JSON first, records each repair, and only then
hands the payload to :class:`PluginConfig` for validation.

The repair rules mirror ``_acceptor_buffer_rules`` in
``Chimera_Plugin_Template.json`` v2.0. If you change them here, update
the template too — the template is what AI authors read to understand
what they can omit.

Repairs are deliberately conservative:

* Missing optional fields → filled with documented defaults.
* Known alias keys (``plugin_name``, ``plugin_version``, ``plugin_type``)
  → rewritten to the canonical schema keys.
* Structurally salvageable malformations (``odds_band`` as ``{min,max}``,
  string numbers, inverted tuples) → coerced.
* Anything ambiguous or risky (``hard_ceiling <= hard_floor``,
  non-positive ``point_value``, missing rules on a rule plugin) →
  *not* repaired. The caller surfaces the ValidationError.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_NAME_ALIASES = ("plugin_name", "strategy_name", "id")
_VERSION_ALIASES = ("plugin_version", "ver", "semver")
_DESCRIPTION_ALIASES = ("purpose", "summary")
_ROLE_ALIASES = ("plugin_type", "role")

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SAFE_NAME_RE = re.compile(r"[^a-z0-9_]+")

_DEFAULT_COMPATIBLE_TOOLS = ["backtest-tool", "fsu100-betting-engine"]
_DEFAULT_PARSER = {
    "format": "betfair_mcm",
    "time_before_off_seconds": 300,
    "price_field": "ltp",
    "extract_bsp": True,
}
_NOOP_RULE = {
    "name": "noop_passthrough",
    "odds_band": [1.01, 1000.0],
    "base_stake": 0,
    "enabled": False,
}


def normalise_plugin_payload(
    payload: dict[str, Any],
    *,
    name_hint: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Repair a raw plugin JSON dict in place and return repair log.

    Args:
        payload: Raw plugin JSON as parsed by :func:`json.loads`. Mutated
            in place — pass a copy if you need to keep the original.
        name_hint: Optional hint for the canonical name (e.g. the URL
            path segment in ``PUT /api/strategies/{name}``). If
            ``payload["name"]`` is missing, the hint wins.

    Returns:
        Tuple of (repaired_payload, list_of_repair_messages). The
        messages are stable strings suitable for inclusion in a
        ``StrategyInfo`` warning panel or audit log.
    """

    if not isinstance(payload, dict):
        raise TypeError("plugin payload must be a JSON object")

    repairs: list[str] = []

    _rewrite_aliases(payload, repairs)
    _ensure_name(payload, name_hint, repairs)
    _ensure_version(payload, repairs)
    _ensure_description(payload, repairs)
    _ensure_string_default(payload, "author", "unknown", repairs)
    _ensure_string_default(payload, "sport", "horse_racing", repairs)
    _ensure_plugin_role(payload, repairs)
    _ensure_list_default(
        payload, "compatible_tools", _DEFAULT_COMPATIBLE_TOOLS, repairs
    )
    _ensure_parser(payload, repairs)
    _ensure_strategy(payload, repairs)
    _ensure_rules(payload, repairs)
    _ensure_controls(payload, repairs)
    _ensure_staking(payload, repairs)

    return payload, repairs


# ---------------------------------------------------------------------------
# Field-level repairs
# ---------------------------------------------------------------------------


def _rewrite_aliases(payload: dict[str, Any], repairs: list[str]) -> None:
    """Rewrite known wrong key names to the canonical ones."""

    for alias in _NAME_ALIASES:
        if alias in payload and "name" not in payload:
            payload["name"] = payload.pop(alias)
            repairs.append(f"renamed '{alias}' → 'name'")
        elif alias in payload:
            payload.pop(alias)
            repairs.append(f"dropped duplicate alias '{alias}' (canonical 'name' already present)")

    for alias in _VERSION_ALIASES:
        if alias in payload and "version" not in payload:
            payload["version"] = payload.pop(alias)
            repairs.append(f"renamed '{alias}' → 'version'")
        elif alias in payload:
            payload.pop(alias)
            repairs.append(f"dropped duplicate alias '{alias}'")

    for alias in _DESCRIPTION_ALIASES:
        if alias in payload and "description" not in payload:
            payload["description"] = payload.pop(alias)
            repairs.append(f"renamed '{alias}' → 'description'")

    for alias in _ROLE_ALIASES:
        if alias in payload and "plugin_role" not in payload:
            payload["plugin_role"] = payload.pop(alias)
            repairs.append(f"renamed '{alias}' → 'plugin_role'")


def _ensure_name(
    payload: dict[str, Any],
    name_hint: str | None,
    repairs: list[str],
) -> None:
    """Pick the canonical name. Hint > existing name > synthesised."""

    existing = payload.get("name")
    if existing and isinstance(existing, str) and existing.strip():
        payload["name"] = _safe_name(existing)
        return

    if name_hint:
        payload["name"] = _safe_name(name_hint)
        repairs.append(f"name missing → used URL hint '{payload['name']}'")
        return

    author = payload.get("author") or "anon"
    purpose = payload.get("description") or "plugin"
    stamp = datetime.now(tz=timezone.utc).strftime("%y%m%d%H%M")
    derived = f"{_safe_name(str(author))}_{_safe_name(str(purpose))[:24]}_v{stamp}"
    payload["name"] = derived
    repairs.append(f"name missing → synthesised '{derived}'")


def _ensure_version(payload: dict[str, Any], repairs: list[str]) -> None:
    raw = payload.get("version")
    if isinstance(raw, str) and _SEMVER_RE.match(raw):
        return
    if raw in (None, ""):
        payload["version"] = "1.0.0"
        repairs.append("version missing → defaulted '1.0.0'")
    else:
        repairs.append(f"version {raw!r} not semver — coerced to '1.0.0'")
        payload["version"] = "1.0.0"


def _ensure_description(payload: dict[str, Any], repairs: list[str]) -> None:
    raw = payload.get("description")
    if isinstance(raw, str) and raw.strip():
        return
    payload["description"] = f"{payload['name']} — auto-generated description, edit me"
    repairs.append("description missing → auto-generated placeholder")


def _ensure_string_default(
    payload: dict[str, Any],
    key: str,
    default: str,
    repairs: list[str],
) -> None:
    raw = payload.get(key)
    if isinstance(raw, str) and raw.strip():
        return
    payload[key] = default
    if raw is None:
        repairs.append(f"{key} missing → defaulted '{default}'")
    else:
        repairs.append(f"{key} {raw!r} not a string → defaulted '{default}'")


def _ensure_plugin_role(payload: dict[str, Any], repairs: list[str]) -> None:
    raw = payload.get("plugin_role")
    allowed = {"rule", "control", "modifier"}
    if isinstance(raw, str) and raw.lower() in allowed:
        payload["plugin_role"] = raw.lower()
        return
    if raw is None:
        payload["plugin_role"] = "rule"
        repairs.append("plugin_role missing → defaulted 'rule'")
    else:
        payload["plugin_role"] = "rule"
        repairs.append(f"plugin_role {raw!r} not recognised → coerced to 'rule'")


def _ensure_list_default(
    payload: dict[str, Any],
    key: str,
    default: list[Any],
    repairs: list[str],
) -> None:
    raw = payload.get(key)
    if isinstance(raw, list):
        return
    payload[key] = list(default)
    if raw is None:
        repairs.append(f"{key} missing → defaulted to {default}")
    else:
        repairs.append(f"{key} {raw!r} not a list → defaulted to {default}")


def _ensure_parser(payload: dict[str, Any], repairs: list[str]) -> None:
    raw = payload.get("parser")
    if raw is None:
        payload["parser"] = dict(_DEFAULT_PARSER)
        repairs.append("parser missing → defaulted")
        return
    if not isinstance(raw, dict):
        payload["parser"] = dict(_DEFAULT_PARSER)
        repairs.append(f"parser {type(raw).__name__} not a dict → defaulted")
        return

    merged = dict(_DEFAULT_PARSER)
    merged.update(raw)
    if merged.get("format") != "betfair_mcm":
        repairs.append(
            f"parser.format {merged.get('format')!r} unsupported → coerced 'betfair_mcm'"
        )
        merged["format"] = "betfair_mcm"
    payload["parser"] = merged


def _ensure_strategy(payload: dict[str, Any], repairs: list[str]) -> None:
    """Hoist a top-level rules array into strategy.rules if needed.

    On a control / modifier plugin, a top-level ``rules`` array typically
    holds guardrail logic (Mark's three_horse_cluster pattern) and lacks
    the ``odds_band`` field a betting rule requires. We move those into
    ``strategy.guardrail_rules`` instead and leave ``strategy.rules`` to
    be filled by the noop_passthrough injection step.
    """

    top_rules = payload.pop("rules", None)
    strategy = payload.get("strategy")

    if not isinstance(strategy, dict):
        if strategy is not None:
            repairs.append(f"strategy {type(strategy).__name__} not a dict → reset to {{}}")
        else:
            repairs.append("strategy block missing → created empty (rules will be filled in next step)")
        payload["strategy"] = {}
        strategy = payload["strategy"]

    if top_rules is None:
        return

    role = payload.get("plugin_role", "rule")
    looks_like_betting_rules = isinstance(top_rules, list) and all(
        isinstance(r, dict) and ("odds_band" in r or "stake" in r or "base_stake" in r)
        for r in top_rules
    )

    if role in ("control", "modifier") and not looks_like_betting_rules:
        existing = strategy.get("guardrail_rules")
        if existing is None:
            strategy["guardrail_rules"] = top_rules
            repairs.append(
                f"top-level 'rules' on {role} plugin moved to 'strategy.guardrail_rules' "
                "(no odds_band → not betting rules)"
            )
        else:
            repairs.append(
                "top-level 'rules' dropped — 'strategy.guardrail_rules' already present"
            )
        return

    if "rules" not in strategy:
        strategy["rules"] = top_rules
        repairs.append("top-level 'rules' moved under 'strategy.rules'")
    else:
        repairs.append(
            "both top-level 'rules' and 'strategy.rules' present — kept 'strategy.rules', dropped top-level"
        )


def _ensure_rules(payload: dict[str, Any], repairs: list[str]) -> None:
    strategy = payload["strategy"]
    rules = strategy.get("rules")
    role = payload.get("plugin_role", "rule")

    if not isinstance(rules, list) or not rules:
        if role in ("control", "modifier"):
            strategy["rules"] = [dict(_NOOP_RULE)]
            repairs.append(
                f"strategy.rules missing on {role} plugin → injected noop_passthrough"
            )
            return
        # Rule plugin without rules — leave as-is and let Pydantic 422.
        return

    repaired_rules: list[dict[str, Any]] = []
    for idx, raw in enumerate(rules, start=1):
        if not isinstance(raw, dict):
            repairs.append(f"rules[{idx-1}] not a dict → dropped")
            continue
        rule = dict(raw)
        _repair_rule(rule, idx, repairs)
        repaired_rules.append(rule)
    strategy["rules"] = repaired_rules


def _repair_rule(rule: dict[str, Any], idx: int, repairs: list[str]) -> None:
    if not rule.get("name"):
        synthetic = f"rule_{idx}"
        rule["name"] = synthetic
        repairs.append(f"rule[{idx-1}].name missing → synthesised '{synthetic}'")
    else:
        rule["name"] = _safe_name(str(rule["name"]))

    band = rule.get("odds_band")
    repaired_band = _coerce_odds_band(band)
    if repaired_band is None:
        # Let Pydantic 422 — we refuse to invent odds bounds.
        return
    if repaired_band != band:
        rule["odds_band"] = repaired_band
        repairs.append(
            f"rule[{idx-1}].odds_band {band!r} coerced to {repaired_band}"
        )

    has_base = "base_stake" in rule and rule["base_stake"] is not None
    has_stake = "stake" in rule and rule["stake"] is not None
    if not has_base and not has_stake:
        rule["base_stake"] = 0
        rule["enabled"] = False
        repairs.append(
            f"rule[{idx-1}] had no base_stake or stake → parked (base_stake=0, enabled=False)"
        )
    if "enabled" not in rule:
        rule["enabled"] = True


def _coerce_odds_band(band: Any) -> list[float] | None:
    """Coerce a band to ``[lo, hi]`` floats, or return None if hopeless."""

    if isinstance(band, dict):
        lo = band.get("min") or band.get("lower") or band.get("from")
        hi = band.get("max") or band.get("upper") or band.get("to")
        if lo is None or hi is None:
            return None
        candidate = [lo, hi]
    elif isinstance(band, (list, tuple)) and len(band) == 2:
        candidate = list(band)
    else:
        return None

    try:
        lo_f = float(candidate[0])
        hi_f = float(candidate[1])
    except (TypeError, ValueError):
        return None

    if lo_f > hi_f:
        lo_f, hi_f = hi_f, lo_f
    if lo_f < 1.01:
        lo_f = 1.01
    return [lo_f, hi_f]


def _ensure_controls(payload: dict[str, Any], repairs: list[str]) -> None:
    strategy = payload["strategy"]
    controls = strategy.get("controls")
    if controls is None:
        strategy["controls"] = {}
        repairs.append("strategy.controls missing → defaulted to empty (Pydantic fills defaults)")
    elif not isinstance(controls, dict):
        strategy["controls"] = {}
        repairs.append(f"strategy.controls {type(controls).__name__} not a dict → reset")


def _ensure_staking(payload: dict[str, Any], repairs: list[str]) -> None:
    staking = payload.get("staking")
    if staking is None:
        payload["staking"] = {"point_value": 1.0}
        repairs.append("staking missing → defaulted {'point_value': 1.0}")
        return
    if not isinstance(staking, dict):
        payload["staking"] = {"point_value": 1.0}
        repairs.append(f"staking {type(staking).__name__} not a dict → defaulted")
        return
    if "point_value" not in staking:
        staking["point_value"] = 1.0
        repairs.append("staking.point_value missing → defaulted 1.0")


def _safe_name(raw: str) -> str:
    """Coerce a string to snake_case-safe form."""

    lowered = raw.strip().lower()
    lowered = lowered.replace(" ", "_").replace("-", "_")
    cleaned = _SAFE_NAME_RE.sub("", lowered)
    cleaned = cleaned.strip("_")
    return cleaned[:64] or "plugin"
