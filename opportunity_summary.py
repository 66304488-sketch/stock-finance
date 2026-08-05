"""Point-in-time opportunity summary built from independent evidence domains.

The summary intentionally does not add page scores together.  Each industry can
receive at most one confirmation from each of five domains:

``trigger``, ``price``, ``participation``, ``structure`` and
``direct_demand``.

Market state is a permission gate, while data quality and risk can only
withhold/reject an opportunity; neither contributes a positive vote.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import date, datetime
from typing import Any, Iterable

from heatmap_opportunity import load_opportunity_snapshot, source_filenames


SCHEMA_VERSION = 1
MODEL_VERSION = "opportunity-summary-v1"
SCHEME_SUFFIX = {"sw": "", "ths": "_ths", "sw3": "_sw3"}
PERIODS = {"month", "60d", "120d", "1year", "alltime"}
INTRADAY_WINDOWS = {"month": 20, "60d": 60, "120d": 120, "1year": 250}
ETF_SOURCE_FILES = {
    "sw3": "etf_recommend_sw3.json",
    "ths": "etf_recommend_ths.json",
    # There is currently no same-classification SW1 recommendation artifact.
    # Do not silently borrow SW3 names because occasional string matches would
    # look like a valid same-scheme carrier mapping.
    "sw": None,
}
DOMAIN_ORDER = (
    "trigger",
    "price",
    "participation",
    "structure",
    "direct_demand",
)
INDEPENDENT_CONFIRMATION_DOMAINS = DOMAIN_ORDER[1:]
MIN_INDEPENDENT_CONFIRMATIONS = 2
DOMAIN_LABELS = {
    "trigger": "行业扩散触发",
    "price": "相对价格确认",
    "participation": "成交参与确认",
    "structure": "市值结构确认",
    "direct_demand": "直接需求确认",
}
HARD_RISK_PATTERNS = {
    "upside_exhaustion": "上涨衰竭",
    "liquidity_vacuum": "流动性真空",
    "selloff": "放量杀跌",
}
HARD_CROWDING_STATES = {
    "crowded": "成交拥挤",
    "crowded_decline": "拥挤下行",
    "crowded_divergence": "拥挤背离",
}
HARD_EXIT_STATES = {
    "danger": "退出危险",
    "fragile": "流动性脆弱",
    "unwind": "去拥挤中",
}
METHODOLOGY = {
    "trigger_domain": "trigger",
    "trigger_required": True,
    "independent_confirmation_domains": list(
        INDEPENDENT_CONFIRMATION_DOMAINS
    ),
    "independent_confirmation_total": len(
        INDEPENDENT_CONFIRMATION_DOMAINS
    ),
    "minimum_independent_confirmations": MIN_INDEPENDENT_CONFIRMATIONS,
    "confirmation_rule": "扩散触发后，四个非触发独立域中至少两个确认",
    "risk_and_quality_are_positive_votes": False,
    "market_is_permission_only": True,
}


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _date_key(value: Any) -> str | None:
    text = str(value or "")
    separated = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if separated:
        year, month, day = (int(part) for part in separated.groups())
        try:
            return date(year, month, day).strftime("%Y%m%d")
        except ValueError:
            return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        candidate = digits[:8]
        try:
            datetime.strptime(candidate, "%Y%m%d")
        except ValueError:
            return None
        return candidate
    return None


def _payload_date(payload: dict | None, *keys: str) -> str | None:
    if not payload:
        return None
    for key in keys:
        candidate = _date_key(payload.get(key))
        if candidate:
            return candidate
    market = payload.get("market")
    if isinstance(market, dict):
        candidate = _date_key(market.get("date"))
        if candidate:
            return candidate
    return None


def _latest_date(*values: Any) -> str | None:
    dates = [candidate for value in values if (candidate := _date_key(value))]
    return max(dates) if dates else None


def _industry_map(payload: dict | None) -> dict[str, dict]:
    return {
        str(row.get("industry")): row
        for row in (payload or {}).get("industries") or []
        if isinstance(row, dict) and row.get("industry") and not row.get("is_total")
    }


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _source_record(
    payload: dict | None,
    source_date: str | None,
    as_of: str | None,
    *,
    required: bool,
    allow_prior: bool = False,
) -> dict:
    if payload is None:
        return {
            "date": None,
            "status": "missing",
            "required": required,
            "used": False,
        }
    if source_date is None:
        return {
            "date": None,
            "status": "undated",
            "required": required,
            "used": False,
        }
    if as_of is None or source_date == as_of:
        return {
            "date": source_date,
            "status": "valid",
            "required": required,
            "used": True,
        }
    if source_date > as_of:
        return {
            "date": source_date,
            "status": "future",
            "required": required,
            "used": False,
        }
    return {
        "date": source_date,
        "status": "prior_close" if allow_prior else "stale",
        "required": required,
        "used": allow_prior,
    }


def _select_temperature(
    payload: dict | None, as_of: str | None
) -> tuple[dict | None, str | None]:
    eligible: list[tuple[str, dict]] = []
    for row in (payload or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        row_date = _date_key(row.get("date"))
        if row_date and (as_of is None or row_date <= as_of):
            eligible.append((row_date, row))
    if not eligible:
        return None, None
    selected_date, selected_row = max(eligible, key=lambda item: item[0])
    return selected_row, selected_date


def _snapshot_as_of(snapshot: dict | None, payloads: Iterable[dict | None]) -> str | None:
    quality = (snapshot or {}).get("quality") or {}
    direct = (
        _date_key(quality.get("latest_date"))
        or _date_key((snapshot or {}).get("as_of"))
        or _date_key((snapshot or {}).get("date"))
    )
    if direct:
        return direct
    candidates = [
        _payload_date(
            payload,
            "as_of",
            "trade_date",
            "date",
            "etf_date",
            "source_date",
        )
        for payload in payloads
    ]
    valid = [candidate for candidate in candidates if candidate]
    return max(valid) if valid else None


def _evidence(
    domain: str,
    status: str,
    details: Iterable[Any],
    sources: Iterable[str],
) -> dict:
    return {
        "status": status,
        "label": DOMAIN_LABELS[domain],
        "details": _unique_strings(details),
        "sources": _unique_strings(sources),
    }


def _trigger_evidence(row: dict, source_status: str) -> dict:
    confirmations = row.get("confirmations") or {}
    breadth = confirmations.get("breadth")
    breadth_pctile = _number(row.get("breadth_percentile"))
    net_breadth = _number(
        row.get("adjusted_net_breadth_pct", row.get("net_breadth_pct"))
    )
    acceleration = _number(row.get("acceleration_percentile"))
    details: list[str] = []
    if net_breadth is not None:
        details.append(f"净扩散 {net_breadth:+.1f}%")
    if breadth_pctile is not None:
        details.append(f"扩散处于自身历史 {breadth_pctile:.0f} 分位")
    if acceleration is not None:
        details.append(f"扩散加速度处于 {acceleration:.0f} 分位")
    if source_status == "invalid" or (
        breadth is None and breadth_pctile is None and net_breadth is None
    ):
        status = "missing"
    elif breadth is True or (
        breadth_pctile is not None
        and breadth_pctile >= 60
        and net_breadth is not None
        and net_breadth > 0
    ):
        status = "confirmed"
    elif net_breadth is not None and net_breadth < 0:
        status = "conflict"
        details.append("净扩散仍为负")
    else:
        status = "not_confirmed"
    return _evidence("trigger", status, details, ["heatmap_opportunity"])


def _price_evidence(
    row: dict, flow_row: dict | None, flow_usable: bool
) -> dict:
    if not flow_usable or not flow_row:
        return _evidence("price", "missing", ["缺少同日成交动能价格字段"], [])
    percentile = _number(flow_row.get("price_result_pctile"))
    excess = _number(flow_row.get("excess_return_pct"))
    details: list[str] = []
    if excess is not None:
        details.append(f"行业超额收益 {excess:+.2f}%")
    if percentile is not None:
        details.append(f"价格结果处于 {percentile:.0f} 分位")
    if percentile is None or excess is None:
        status = "missing"
    elif bool((row.get("confirmations") or {}).get("trend")) or (
        percentile >= 60 and excess > 0
    ):
        status = "confirmed"
    elif excess < 0:
        status = "conflict"
        details.append("相对价格尚未跟随扩散")
    else:
        status = "not_confirmed"
    return _evidence("price", status, details, ["capital_flow_v2"])


def _participation_evidence(
    row: dict, flow_row: dict | None, flow_usable: bool
) -> dict:
    if not flow_usable or not flow_row:
        return _evidence(
            "participation", "missing", ["缺少同日成交参与字段"], []
        )
    activity = _number(flow_row.get("activity_pctile"))
    active_breadth = _number(flow_row.get("active_breadth_pctile"))
    effective = _number(flow_row.get("effective_participants"))
    direction = _number(flow_row.get("active_direction_breadth"))
    details: list[str] = []
    if activity is not None:
        details.append(f"成交活跃 {activity:.0f} 分位")
    if active_breadth is not None:
        details.append(f"活跃广度 {active_breadth:.0f} 分位")
    if effective is not None:
        details.append(f"有效参与者 {effective:.1f}")
    observed = [activity, active_breadth, effective]
    if all(value is None for value in observed):
        status = "missing"
    elif (
        bool((row.get("confirmations") or {}).get("activity"))
        or bool((row.get("confirmations") or {}).get("participation"))
        or (activity is not None and activity >= 60)
        or (active_breadth is not None and active_breadth >= 60)
    ):
        # Activity and active breadth are deliberately one domain and one vote.
        status = "confirmed"
    elif direction is not None and direction < 0:
        status = "conflict"
        details.append("活跃成交方向偏弱")
    else:
        status = "not_confirmed"
    return _evidence("participation", status, details, ["capital_flow_v2"])


def _structure_evidence(
    market_cap_row: dict | None, market_cap_usable: bool
) -> dict:
    if not market_cap_usable or not market_cap_row:
        return _evidence("structure", "missing", ["缺少同日市值结构字段"], [])
    cap_weighted = _number(
        market_cap_row.get(
            "cap_weighted_return_1d_pct", market_cap_row.get("return_1d_pct")
        )
    )
    equal_weighted = _number(market_cap_row.get("equal_weight_return_1d_pct"))
    breadth = _number(
        market_cap_row.get(
            "stock_breadth_1d_pct", market_cap_row.get("stock_breadth_pct")
        )
    )
    relative = _number(market_cap_row.get("relative_1d_pct"))
    details: list[str] = []
    if cap_weighted is not None:
        details.append(f"市值加权收益 {cap_weighted:+.2f}%")
    if equal_weighted is not None:
        details.append(f"等权收益 {equal_weighted:+.2f}%")
    if breadth is not None:
        details.append(f"上涨广度 {breadth:.1f}%")
    if relative is not None:
        details.append(f"相对市场 {relative:+.2f}%")
    if cap_weighted is None or equal_weighted is None or breadth is None:
        status = "missing"
    elif cap_weighted > 0 and equal_weighted > 0 and breadth >= 50:
        status = "confirmed"
    elif cap_weighted > 0 >= equal_weighted:
        status = "conflict"
        details.append("市值加权上涨但等权未跟随，可能由权重股托举")
    elif cap_weighted < 0 < equal_weighted:
        status = "conflict"
        details.append("等权上涨但市值加权未跟随，结构尚未共振")
    else:
        status = "not_confirmed"
    return _evidence("structure", status, details, ["market_cap_v2"])


def _observed_direct_changes(row: dict) -> list[tuple[str, float]]:
    external = row.get("external_evidence")
    external = external if isinstance(external, dict) else {}
    observations: list[tuple[str, float]] = []

    etf_change = _number(
        row.get("etf_share_change_pct", external.get("etf_share_change_pct"))
    )
    etf_count = _number(external.get("etf_change_count"))
    if etf_change is not None and (etf_change != 0 or (etf_count or 0) > 0):
        observations.append(("ETF份额", etf_change))

    margin_change = _number(
        row.get("margin_change_pct", external.get("margin_change_pct"))
    )
    margin_count = _number(external.get("margin_change_count"))
    margin_coverage = _number(
        row.get("margin_coverage", external.get("margin_coverage"))
    )
    if margin_change is not None and (
        margin_change != 0
        or (margin_count or 0) > 0
        or (margin_coverage or 0) > 0
    ):
        observations.append(("融资余额", margin_change))
    return observations


def _direct_demand_evidence(
    crowding_row: dict | None, crowding_usable: bool
) -> dict:
    if not crowding_usable or not crowding_row:
        return _evidence(
            "direct_demand", "missing", ["缺少同日直接需求字段"], []
        )
    observations = _observed_direct_changes(crowding_row)
    if not observations:
        return _evidence(
            "direct_demand",
            "missing",
            ["ETF份额与融资余额变化均无有效覆盖；缺失未按零处理"],
            ["crowding"],
        )
    details = [f"{label}变化 {value:+.2f}%" for label, value in observations]
    positives = [value for _, value in observations if value > 0]
    negatives = [value for _, value in observations if value < 0]
    if positives and not negatives:
        status = "confirmed"
    elif positives and negatives:
        status = "conflict"
        details.append("直接需求来源方向不一致")
    elif negatives:
        status = "conflict"
        details.append("直接需求净流出")
    else:
        status = "not_confirmed"
    return _evidence("direct_demand", status, details, ["crowding"])


def _code(value: Any) -> str | None:
    match = re.search(r"(\d{6})", str(value or ""))
    return match.group(1) if match else None


def _item_not_future(item: dict, as_of: str | None, fallback_date: str | None) -> bool:
    item_date = (
        _date_key(item.get("last_date"))
        or _date_key(item.get("date"))
        or fallback_date
    )
    return not (as_of and item_date and item_date > as_of)


def _momentum_by_code(
    payload: dict | None, as_of: str | None, source_date: str | None
) -> dict[str, dict]:
    if not payload or (as_of and source_date and source_date > as_of):
        return {}
    rows: list[dict] = []
    dynamic_pool = payload.get("dynamic_pool")
    if isinstance(dynamic_pool, dict):
        rows.extend(
            row
            for row in dynamic_pool.get("entries") or []
            if isinstance(row, dict)
        )
    variants = payload.get("variants")
    if isinstance(variants, dict):
        for variant_name in ("dynamic", "combined", "strategy"):
            variant = variants.get(variant_name)
            if not isinstance(variant, dict):
                continue
            target = variant.get("target")
            if isinstance(target, dict):
                rows.append(target)
            for key in ("top10", "all"):
                rows.extend(
                    row for row in variant.get(key) or [] if isinstance(row, dict)
                )
    result: dict[str, dict] = {}
    for row in rows:
        code = _code(row.get("code"))
        if not code or not _item_not_future(row, as_of, source_date):
            continue
        normalized = {
            "date": _date_key(row.get("last_date") or row.get("date"))
            or source_date,
            "passed_all": row.get("passed_all"),
            "score": _number(row.get("score")),
            "r_squared": _number(row.get("r_squared")),
            "above_ma": row.get("passed_ma"),
        }
        current = result.get(code)
        if current is None or (
            normalized.get("passed_all") is True
            and current.get("passed_all") is not True
        ):
            result[code] = normalized
    return result


def _etf_rows(payload: dict | None) -> list[dict]:
    result: list[dict] = []
    for key in ("top", "etfs"):
        for row in (payload or {}).get(key) or []:
            if not isinstance(row, dict):
                continue
            nested = row.get("etf") if isinstance(row.get("etf"), dict) else {}
            result.append(
                {
                    **nested,
                    **row,
                    "_qualification_source": key,
                    "_qualification_rank": 0 if key == "top" else 1,
                }
            )
    for row in (payload or {}).get("industries") or []:
        if not isinstance(row, dict) or not isinstance(row.get("etf"), dict):
            continue
        # The industry table is a mapping/display projection.  Its row omits
        # the ETF-level risk and quality gates, so it must never upgrade a
        # rejected decision record into a qualified carrier.
        result.append(
            {
                **row["etf"],
                **{k: v for k, v in row.items() if k != "etf"},
                "_qualification_source": None,
                "_qualification_rank": 2,
            }
        )
    return result


def _etfs_by_industry(
    payload: dict | None,
    *,
    as_of: str | None,
    source_date: str | None,
    source_usable: bool,
    qualification_usable: bool = True,
    momentum: dict[str, dict],
) -> dict[str, list[dict]]:
    if not source_usable:
        return {}
    grouped: dict[str, dict[str, dict]] = {}
    for row in _etf_rows(payload):
        code = _code(row.get("code"))
        primary_industry = str(row.get("industry") or "").strip()
        if (
            not code
            or not primary_industry
            or not _item_not_future(row, as_of, source_date)
        ):
            continue
        stage = str(row.get("stage") or "")
        liquid = row.get("liquid") is True
        risk = row.get("risk") if isinstance(row.get("risk"), dict) else {}
        quality = (
            row.get("data_quality")
            if isinstance(row.get("data_quality"), dict)
            else {}
        )
        disallowed_stage = stage.lower() in {
            "avoid",
            "rejected",
            "crowded",
        } or stage in {"回避", "拥挤", "过热"}
        is_decision_record = bool(row.get("_qualification_source"))
        qualified = qualification_usable and is_decision_record and (
            liquid
            and not disallowed_stage
            and str(risk.get("state") or "").lower() not in {"danger", "unwind"}
            and str(quality.get("state") or "").lower() not in {"low", "invalid"}
        )
        related = [
            str(item.get("industry") or "").strip()
            for item in row.get("related_industries") or []
            if isinstance(item, dict) and item.get("industry")
        ]
        for industry in _unique_strings([primary_industry, *related]):
            carrier = {
                "code": code,
                "name": row.get("name") or code,
                "industry": industry,
                "primary_industry": primary_industry,
                "score": _number(row.get("opportunity_score", row.get("score"))),
                "stage": stage or None,
                "liquid": liquid,
                "qualified": qualified,
                "last_date": _date_key(row.get("last_date")) or source_date,
                "share_change_pct": _number(row.get("share_change_pct")),
                "momentum": momentum.get(code),
                "_qualification_rank": int(row.get("_qualification_rank", 2)),
            }
            industry_rows = grouped.setdefault(industry, {})
            previous = industry_rows.get(code)
            if previous is None:
                industry_rows[code] = carrier
                continue
            # Qualification follows the most authoritative complete decision
            # record: top, then etfs, then the non-authoritative industry view.
            if carrier["_qualification_rank"] < previous["_qualification_rank"]:
                previous["qualified"] = carrier["qualified"]
                previous["liquid"] = carrier["liquid"]
                previous["stage"] = carrier["stage"]
                previous["_qualification_rank"] = carrier["_qualification_rank"]
            carrier_score = carrier.get("score")
            previous_score = previous.get("score")
            if carrier_score is not None and (
                previous_score is None or carrier_score > previous_score
            ):
                previous["score"] = carrier_score
            if previous.get("momentum") is None and carrier.get("momentum") is not None:
                previous["momentum"] = carrier["momentum"]
    for rows in grouped.values():
        for carrier in rows.values():
            carrier.pop("_qualification_rank", None)
    return {
        industry: sorted(
            rows.values(),
            key=lambda row: (
                not row["qualified"],
                -(row["score"] if row["score"] is not None else -1),
                row["code"],
            ),
        )
        for industry, rows in grouped.items()
    }


def _stock_carriers(
    heatmap_row: dict,
    flow_row: dict | None,
    market_cap_row: dict | None,
    crowding_row: dict | None,
    *,
    include_heatmap_leaders: bool = True,
) -> list[dict]:
    collections = (
        (
            "heatmap_leader",
            (heatmap_row.get("leaders") or [])
            if include_heatmap_leaders
            else [],
        ),
        ("flow_leader", (flow_row or {}).get("leaders") or []),
        ("flow_top", (flow_row or {}).get("top_stocks") or []),
        ("market_cap_top", (market_cap_row or {}).get("top_stocks") or []),
        ("crowding_leader", (crowding_row or {}).get("leaders") or []),
        ("crowding_top", (crowding_row or {}).get("top_stocks") or []),
    )
    result: dict[str, dict] = {}
    for source, rows in collections:
        for rank, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            code = _code(row.get("code"))
            if not code:
                continue
            normalized = {
                "code": code,
                "name": row.get("name") or code,
                "source": source,
                "sources": [source],
                "amount": _number(row.get("amount")),
                "return_pct": _number(
                    row.get("return_pct", row.get("change_pct"))
                ),
                "weight_pct": _number(row.get("weight_pct")),
                "contribution_bp": _number(row.get("contribution_bp")),
                "_rank": rank,
            }
            previous = result.get(code)
            if previous is None:
                result[code] = normalized
                continue
            previous["sources"] = _unique_strings(
                [*previous.get("sources", []), source]
            )
            previous["_rank"] = min(previous["_rank"], rank)
            for key, value in normalized.items():
                if key not in {"source", "sources", "_rank"} and previous.get(key) is None:
                    previous[key] = value
    ordered = sorted(
        result.values(),
        key=lambda row: (
            0 if "heatmap_leader" in row["sources"] else 1,
            row["_rank"],
            -(row["amount"] or 0),
            row["code"],
        ),
    )
    for row in ordered:
        row.pop("_rank", None)
    return ordered


def _risk_and_vetoes(
    heatmap_row: dict, crowding_row: dict | None, crowding_usable: bool
) -> tuple[list[str], list[str]]:
    risks: list[Any] = [
        *(heatmap_row.get("risk_reasons") or []),
        *(heatmap_row.get("risk_domains") or []),
    ]
    vetoes: list[str] = []
    risk_level = str(heatmap_row.get("risk_level") or "").lower()
    risk_pattern = str(heatmap_row.get("risk_pattern") or "").lower()
    stage = str(heatmap_row.get("stage") or "").lower()
    if risk_level == "danger":
        vetoes.append("成交动能风险等级为危险")
    if risk_pattern in HARD_RISK_PATTERNS:
        vetoes.append(HARD_RISK_PATTERNS[risk_pattern])
    if stage in {"crowded", "failed"}:
        vetoes.append("机会状态已拥挤或失效")

    if crowding_usable and crowding_row:
        risks.extend(crowding_row.get("risk_reasons") or [])
        risk_domains = crowding_row.get("risk_domains")
        if isinstance(risk_domains, dict):
            risks.extend(risk_domains.values())
        elif isinstance(risk_domains, list):
            risks.extend(risk_domains)
        crowd_state = str(crowding_row.get("state") or "").lower()
        exit_state = str(crowding_row.get("risk_state") or "").lower()
        if crowd_state in HARD_CROWDING_STATES:
            vetoes.append(HARD_CROWDING_STATES[crowd_state])
        if exit_state in HARD_EXIT_STATES:
            vetoes.append(HARD_EXIT_STATES[exit_state])
    return _unique_strings(risks), _unique_strings(vetoes)


def _market_summary(
    snapshot: dict | None,
    quality_status: str,
    temperature_row: dict | None,
) -> dict:
    source = (snapshot or {}).get("market_permission") or {}
    state = str(source.get("state") or "paused").lower()
    if quality_status == "invalid":
        permission = "paused"
    else:
        permission = {
            "attack": "allowed",
            "allowed": "allowed",
            "watch": "cautious",
            "cautious": "cautious",
            "defense": "restricted",
            "restricted": "restricted",
            "risk_off": "restricted",
            "paused": "paused",
        }.get(state, "cautious")
    return {
        "permission": permission,
        "state": state,
        "label": source.get("label") or permission,
        "reason": source.get("message"),
        "temperature": (
            {
                "date": _date_key(temperature_row.get("date")),
                "value": _number(temperature_row.get("temperature")),
                "up": temperature_row.get("up"),
                "down": temperature_row.get("down"),
            }
            if temperature_row
            else None
        ),
        "breadth": {
            "highs": source.get("highs"),
            "lows": source.get("lows"),
            "net_breadth_pct": source.get("net_breadth_pct"),
            "market_breadth": source.get("market_breadth"),
        },
    }


def _quality(
    snapshot: dict | None,
    snapshot_error: str | None,
    sources: dict[str, dict],
    *,
    mode: str,
    as_of: str | None,
) -> dict:
    reasons: list[str] = []
    warnings: list[str] = []
    snapshot_quality = (snapshot or {}).get("quality") or {}
    if snapshot_error:
        reasons.append(snapshot_error)
    if not as_of:
        reasons.append("无法确定统一决策日期")
    if snapshot_quality.get("status") == "invalid":
        reasons.extend(snapshot_quality.get("reasons") or ["热力图质量闸门未通过"])
    elif snapshot_quality.get("status") == "degraded":
        warnings.extend(snapshot_quality.get("warnings") or ["热力图数据已降级"])

    heatmap_status = sources["heatmap_opportunity"]["status"]
    if heatmap_status in {"missing", "invalid", "undated"}:
        reasons.append("热力图机会快照不可用")

    flow_status = sources["capital_flow_v2"]["status"]
    if mode == "daily" and flow_status != "valid":
        reasons.append("成交动能与决策日期不一致或缺失")
    elif mode == "intraday" and flow_status in {"missing", "future", "undated"}:
        warnings.append("上一收盘日成交动能风险背景不可用")

    for name, label in (
        ("market_cap_v2", "市值结构"),
        ("crowding", "成交拥挤"),
    ):
        status = sources[name]["status"]
        if status != "valid":
            warnings.append(f"{label}与决策日期不一致或缺失")
    temperature_status = sources["market_temperature"]["status"]
    if temperature_status in {"missing", "future", "undated"}:
        warnings.append("市场温度不可用")
    elif temperature_status == "stale":
        warnings.append("市场温度不是决策日快照")

    status = "invalid" if reasons else ("degraded" if warnings else "valid")
    core_exact = all(
        sources[name]["status"] == "valid"
        for name in (
            "heatmap_opportunity",
            "capital_flow_v2",
            "market_cap_v2",
            "crowding",
        )
    )
    return {
        "status": status,
        "label": {"valid": "可信", "degraded": "降级", "invalid": "暂停"}[status],
        "can_rank": status != "invalid",
        "can_act": status != "invalid" and mode == "daily" and core_exact,
        "reasons": _unique_strings(reasons),
        "warnings": _unique_strings(warnings),
        "sources": sources,
    }


def _empty_result(
    *,
    scheme: str,
    period: str,
    mode: str,
    as_of: str | None,
    quality: dict,
    market: dict,
    universe_total: int = 0,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scheme": scheme,
        "period": period,
        "mode": mode,
        "as_of": as_of,
        "quality": quality,
        "market": market,
        "methodology": METHODOLOGY,
        "funnel": {
            "total": universe_total,
            "triggered": 0,
            "confirmed": 0,
            "risk_passed": 0,
            "with_qualified_etf": 0,
            "actionable": 0,
            "lanes": {"confirmed": 0, "watch": 0, "rejected": 0},
        },
        "candidates": [],
    }


def _intraday_snapshot_stale(
    data_dir: str,
    *,
    scheme: str,
    period: str,
    window: int,
    threshold_seconds: float = 120.0,
) -> bool:
    """Return True unless both matching intraday inputs are fresh."""
    high_name, low_name, _ = source_filenames(
        period, scheme, "intraday", window
    )
    paths = (
        os.path.join(data_dir, high_name),
        os.path.join(data_dir, low_name),
    )
    if not all(os.path.isfile(path) for path in paths):
        return True
    try:
        oldest_mtime = min(os.path.getmtime(path) for path in paths)
    except OSError:
        return True
    return time.time() - oldest_mtime > threshold_seconds


def build_opportunity_summary(
    data_dir: str | os.PathLike[str],
    scheme: str = "sw3",
    period: str = "month",
    mode: str = "daily",
) -> dict:
    """Load point-in-time inputs and return a cross-validated opportunity view."""
    if scheme not in SCHEME_SUFFIX:
        raise ValueError("scheme must be sw, ths or sw3")
    if period not in PERIODS:
        raise ValueError("period must be month, 60d, 120d, 1year or alltime")
    if mode not in {"daily", "intraday"}:
        raise ValueError("mode must be daily or intraday")
    if mode == "intraday" and period == "alltime":
        raise ValueError("intraday mode does not support alltime period")

    root = os.fspath(data_dir)
    suffix = SCHEME_SUFFIX[scheme]
    etf_source_file = ETF_SOURCE_FILES[scheme]
    etf_source_key = f"etf_recommend_{scheme}"
    flow = _read_json(os.path.join(root, f"capital_flow_v2{suffix}.json"))
    market_cap = _read_json(os.path.join(root, f"market_cap_v2{suffix}.json"))
    crowding = _read_json(os.path.join(root, f"crowding{suffix}.json"))
    temperature = _read_json(os.path.join(root, "market_temperature.json"))
    etf = (
        _read_json(os.path.join(root, etf_source_file))
        if etf_source_file
        else None
    )
    momentum = _read_json(os.path.join(root, "momentum_etf.json"))

    snapshot: dict | None
    snapshot_error: str | None = None
    window = INTRADAY_WINDOWS.get(period, 20)
    intraday_stale = (
        _intraday_snapshot_stale(
            root,
            scheme=scheme,
            period=period,
            window=window,
        )
        if mode == "intraday"
        else False
    )
    try:
        snapshot = load_opportunity_snapshot(
            root,
            scheme=scheme,
            period=period,
            mode=mode,
            window=window,
            stale=intraday_stale,
        )
    except FileNotFoundError:
        snapshot = None
        snapshot_error = "热力图数据尚未生成"

    as_of = _snapshot_as_of(
        snapshot, (flow, market_cap, crowding, etf, momentum)
    )
    temperature_row, temperature_date = _select_temperature(temperature, as_of)
    etf_source_date = _latest_date(
        (etf or {}).get("date"),
        (etf or {}).get("etf_date"),
        (etf or {}).get("as_of"),
    )
    dynamic_pool = (
        momentum.get("dynamic_pool")
        if isinstance((momentum or {}).get("dynamic_pool"), dict)
        else {}
    )
    momentum_source_date = _latest_date(
        (momentum or {}).get("date"),
        (momentum or {}).get("as_of"),
        dynamic_pool.get("source_date"),
    )
    snapshot_quality = (snapshot or {}).get("quality") or {}
    snapshot_status = str(snapshot_quality.get("status") or "missing")
    snapshot_source = {
        "date": _date_key(snapshot_quality.get("latest_date")) or as_of,
        "status": snapshot_status if snapshot else "missing",
        "required": True,
        "used": bool(snapshot),
        "stale": intraday_stale if mode == "intraday" else False,
    }
    sources = {
        "heatmap_opportunity": snapshot_source,
        "capital_flow_v2": _source_record(
            flow,
            _payload_date(flow, "as_of", "trade_date", "date"),
            as_of,
            required=True,
            allow_prior=mode == "intraday",
        ),
        "market_cap_v2": _source_record(
            market_cap,
            _payload_date(market_cap, "trade_date", "as_of", "date"),
            as_of,
            required=True,
            allow_prior=mode == "intraday",
        ),
        "crowding": _source_record(
            crowding,
            _payload_date(crowding, "as_of", "trade_date", "date"),
            as_of,
            required=True,
            allow_prior=mode == "intraday",
        ),
        "market_temperature": _source_record(
            temperature_row,
            temperature_date,
            as_of,
            required=True,
        ),
        etf_source_key: _source_record(
            etf,
            etf_source_date,
            as_of,
            required=False,
        ),
        "momentum_etf": _source_record(
            momentum,
            momentum_source_date,
            as_of,
            required=False,
        ),
    }
    sources[etf_source_key].update(
        {
            "file": etf_source_file,
            "scheme": scheme,
            "mapping": "same_scheme" if etf_source_file else "unavailable",
        }
    )
    if etf_source_file is None:
        sources[etf_source_key][
            "reason"
        ] = "暂无申万一级同口径ETF推荐文件，未借用其他分类映射"
    quality = _quality(
        snapshot,
        snapshot_error,
        sources,
        mode=mode,
        as_of=as_of,
    )
    market = _market_summary(snapshot, quality["status"], temperature_row)
    if not snapshot:
        return _empty_result(
            scheme=scheme,
            period=period,
            mode=mode,
            as_of=as_of,
            quality=quality,
            market=market,
        )

    universe_total = sum(
        isinstance(row, dict) and bool(row.get("industry"))
        for row in snapshot.get("industries") or []
    )
    if quality["status"] == "invalid":
        return _empty_result(
            scheme=scheme,
            period=period,
            mode=mode,
            as_of=as_of,
            quality=quality,
            market=market,
            universe_total=universe_total,
        )

    flow_map = _industry_map(flow)
    market_cap_map = _industry_map(market_cap)
    crowding_map = _industry_map(crowding)
    flow_usable = sources["capital_flow_v2"]["status"] == "valid" and mode == "daily"
    market_cap_usable = (
        sources["market_cap_v2"]["status"] == "valid" and mode == "daily"
    )
    crowding_confirmation_usable = (
        sources["crowding"]["status"] == "valid" and mode == "daily"
    )
    crowding_risk_usable = sources["crowding"]["status"] in {
        "valid",
        "prior_close",
    }
    etf_usable = sources[etf_source_key]["status"] in {"valid", "stale"}
    etf_qualification_usable = (
        sources[etf_source_key]["status"] == "valid"
    )
    momentum_map = _momentum_by_code(momentum, as_of, momentum_source_date)
    etf_map = _etfs_by_industry(
        etf,
        as_of=as_of,
        source_date=sources[etf_source_key]["date"],
        source_usable=etf_usable,
        qualification_usable=etf_qualification_usable,
        momentum=momentum_map,
    )

    candidates: list[dict] = []
    triggered_total = 0
    for row in (snapshot.get("industries") or []):
        if not isinstance(row, dict) or not row.get("industry"):
            continue
        industry = str(row["industry"])
        flow_row = flow_map.get(industry)
        market_cap_row = market_cap_map.get(industry)
        crowding_row = crowding_map.get(industry)
        evidence = {
            "trigger": _trigger_evidence(row, snapshot_source["status"]),
            "price": _price_evidence(row, flow_row, flow_usable),
            "participation": _participation_evidence(
                row, flow_row, flow_usable
            ),
            "structure": _structure_evidence(
                market_cap_row, market_cap_usable
            ),
            "direct_demand": _direct_demand_evidence(
                crowding_row, crowding_confirmation_usable
            ),
        }
        if evidence["trigger"]["status"] == "confirmed":
            triggered_total += 1
        confirmed_domains = [
            domain
            for domain in DOMAIN_ORDER
            if evidence[domain]["status"] == "confirmed"
        ]
        trigger_confirmed = evidence["trigger"]["status"] == "confirmed"
        independent_confirmations = [
            domain
            for domain in INDEPENDENT_CONFIRMATION_DOMAINS
            if evidence[domain]["status"] == "confirmed"
        ]
        conflicts = _unique_strings(
            detail
            for domain in DOMAIN_ORDER
            if evidence[domain]["status"] == "conflict"
            for detail in evidence[domain]["details"]
        )
        missing = [
            DOMAIN_LABELS[domain]
            for domain in DOMAIN_ORDER
            if evidence[domain]["status"] == "missing"
        ]
        risk_assessed = bool(crowding_risk_usable and crowding_row)
        if not risk_assessed:
            missing.append("成交拥挤风险评估")
        missing = _unique_strings(missing)
        risks, vetoes = _risk_and_vetoes(
            row, crowding_row, crowding_risk_usable
        )
        stage = str(row.get("stage") or "").lower()
        signal_relevant = (
            evidence["trigger"]["status"] == "confirmed"
            or stage not in {"", "dormant", "insufficient"}
        )
        if not signal_relevant:
            continue
        etfs = etf_map.get(industry, [])
        qualified_etfs = [carrier for carrier in etfs if carrier["qualified"]]
        independently_confirmed = (
            trigger_confirmed
            and len(independent_confirmations)
            >= MIN_INDEPENDENT_CONFIRMATIONS
        )
        if vetoes:
            lane = "rejected"
        elif independently_confirmed:
            lane = "confirmed"
        else:
            lane = "watch"
        actionable = bool(
            mode == "daily"
            and market["permission"] == "allowed"
            and quality["can_act"]
            and risk_assessed
            and independently_confirmed
            and not vetoes
            and qualified_etfs
        )
        invalidation = row.get("invalidation")
        invalidations = (
            invalidation if isinstance(invalidation, list) else [invalidation]
        )
        candidates.append(
            {
                "industry": industry,
                "lane": lane,
                "actionable": actionable,
                "stage": row.get("stage"),
                "stage_label": row.get("stage_label"),
                "score": _number(row.get("score")),
                "triggered": trigger_confirmed,
                "confirmation_count": len(independent_confirmations),
                "confirmation_total": len(
                    INDEPENDENT_CONFIRMATION_DOMAINS
                ),
                "independent_confirmation_count": len(
                    independent_confirmations
                ),
                "independent_confirmation_total": len(
                    INDEPENDENT_CONFIRMATION_DOMAINS
                ),
                "evidence": evidence,
                "confirmations": confirmed_domains,
                "independent_confirmations": independent_confirmations,
                "confirmed_domains": confirmed_domains,
                "conflicts": conflicts,
                "missing": missing,
                "risks": risks,
                "vetoes": vetoes,
                "invalidation": _unique_strings(invalidations),
                "carriers": {
                    "etfs": etfs,
                    "stocks": _stock_carriers(
                        row,
                        flow_row if flow_usable else None,
                        market_cap_row if market_cap_usable else None,
                        crowding_row if crowding_confirmation_usable else None,
                        include_heatmap_leaders=flow_usable,
                    ),
                },
            }
        )

    lane_order = {"confirmed": 0, "watch": 1, "rejected": 2}
    candidates.sort(
        key=lambda row: (
            lane_order[row["lane"]],
            not row["actionable"],
            -row["confirmation_count"],
            -(row["score"] if row["score"] is not None else -1),
            row["industry"],
        )
    )
    lanes = {
        lane: sum(candidate["lane"] == lane for candidate in candidates)
        for lane in ("confirmed", "watch", "rejected")
    }
    funnel = {
        "total": universe_total,
        "triggered": triggered_total,
        "confirmed": lanes["confirmed"],
        "risk_passed": sum(
            candidate["lane"] == "confirmed" and not candidate["vetoes"]
            for candidate in candidates
        ),
        "with_qualified_etf": sum(
            candidate["lane"] == "confirmed"
            and any(etf_row["qualified"] for etf_row in candidate["carriers"]["etfs"])
            for candidate in candidates
        ),
        "actionable": sum(candidate["actionable"] for candidate in candidates),
        "lanes": lanes,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scheme": scheme,
        "period": period,
        "mode": mode,
        "as_of": as_of,
        "quality": quality,
        "market": market,
        "methodology": METHODOLOGY,
        "funnel": funnel,
        "candidates": candidates,
    }
