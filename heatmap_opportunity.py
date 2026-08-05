"""Evidence-gated opportunity states for the industry extreme-breadth heatmap.

The module deliberately keeps prediction and description separate:

* new-high/new-low breadth describes tail participation;
* turnover momentum confirms participation and relative price response;
* risk remains a separate output instead of being hidden in one score;
* invalid or incomparable data disables the opportunity conclusion.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Iterable


SCHEME_SUFFIX = {"sw": "", "ths": "_ths", "sw3": "_sw3"}
PERIODS = {"month", "60d", "120d", "1year", "alltime"}
STAGE_LABELS = {
    "insufficient": "数据不足",
    "dormant": "沉寂",
    "emerging": "萌芽",
    "confirmed": "确认",
    "extending": "延续",
    "relative": "相对强势",
    "crowded": "拥挤/衰竭",
    "declining": "衰退",
    "failed": "失效",
    "panic": "恐慌观察",
    "intraday_watch": "盘中观察",
}
STAGE_ORDER = {
    "confirmed": 0,
    "emerging": 1,
    "extending": 2,
    "relative": 3,
    "intraday_watch": 4,
    "crowded": 5,
    "declining": 6,
    "failed": 7,
    "dormant": 8,
    "insufficient": 9,
}


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _mean(values: Iterable[float]) -> float:
    valid = [float(value) for value in values if _number(value) is not None]
    return statistics.fmean(valid) if valid else 0.0


def _percentile_rank(value: float, history: Iterable[float]) -> float:
    values = sorted(float(item) for item in history if _number(item) is not None)
    if not values:
        return 50.0
    below = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    return round((below + 0.5 * equal) / len(values) * 100, 1)


def _quantile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(item) for item in values if _number(item) is not None)
    if not ordered:
        return None
    position = (len(ordered) - 1) * _clamp(fraction, 0.0, 1.0)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


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
        return digits[:8]
    return None


def _date_keys(payload: dict) -> list[str]:
    return [
        key
        for key in (_date_key(item.get("full_label") or item.get("date")) for item in payload.get("dates") or [])
        if key
    ]


def _rows(payload: dict) -> list[dict]:
    return [row for row in payload.get("industries") or [] if not row.get("is_total")]


def _total(payload: dict) -> dict:
    return next((row for row in payload.get("industries") or [] if row.get("is_total")), {})


def _row_map(payload: dict) -> dict[str, dict]:
    return {str(row.get("industry")): row for row in _rows(payload) if row.get("industry")}


def _count(row: dict | None, index: int = 0) -> int:
    counts = (row or {}).get("daily_counts") or []
    value = counts[index] if index < len(counts) else 0
    return int(_number(value, 0) or 0)


def _flow_map(flow: dict | None) -> dict[str, dict]:
    return {
        str(row.get("industry")): row
        for row in (flow or {}).get("industries") or []
        if row.get("industry") and not row.get("is_total")
    }


def _total_count(payload: dict, index: int = 0) -> int:
    return _count(_total(payload), index)


def _strict_retained(row: dict, direction: str) -> int:
    explicit = _number(row.get("retained_count"))
    if explicit is not None:
        return int(explicit)
    details = row.get("daily_details") or {}
    stocks = next(iter(details.values()), []) if details else []
    retained = 0
    for stock in stocks:
        if "retained" in stock:
            retained += int(bool(stock.get("retained")))
            continue
        price = _number(stock.get("price"))
        threshold = _number(stock.get("price_threshold"))
        if price is None or threshold is None:
            continue
        retained += int(price > threshold) if direction == "highs" else int(price < threshold)
    return retained


def _has_strict_retained(row: dict) -> bool:
    if _number(row.get("retained_count")) is not None:
        return True
    details = row.get("daily_details") or {}
    return any(
        "retained" in stock
        for stocks in details.values()
        for stock in (stocks or [])
    )


def _history_gap_dates(highs: dict, lows: dict) -> list[str]:
    high_dates = _date_keys(highs)
    low_dates = _date_keys(lows)
    common = min(len(high_dates), len(low_dates))
    return [
        high_dates[index]
        for index in range(common)
        if _total_count(highs, index) == 0 and _total_count(lows, index) == 0
    ]


def validate_inputs(
    highs: dict,
    lows: dict,
    flow: dict | None,
    *,
    mode: str,
    stale: bool = False,
    peer_totals: list[dict] | None = None,
) -> dict:
    """Return a hard quality gate and machine-readable evidence."""
    reasons: list[str] = []
    warnings: list[str] = []
    high_dates = _date_keys(highs)
    low_dates = _date_keys(lows)
    latest = high_dates[0] if high_dates else None
    dates_aligned = bool(latest and low_dates and latest == low_dates[0])
    if not dates_aligned:
        reasons.append("创新高与创新低日期不一致")

    high_rows = _rows(highs)
    low_rows = _rows(lows)
    high_total = _total_count(highs)
    low_total = _total_count(lows)
    high_sum = sum(_count(row) for row in high_rows)
    low_sum = sum(_count(row) for row in low_rows)
    totals_consistent = high_total == high_sum and low_total == low_sum
    if not totals_consistent:
        reasons.append("行业合计与全市场总数不一致")

    high_names = {row.get("industry") for row in high_rows}
    low_names = {row.get("industry") for row in low_rows}
    classifications_aligned = high_names == low_names and bool(high_names)
    if not classifications_aligned:
        reasons.append("创新高与创新低行业集合不一致")

    invalid_denominators = sorted(
        str(row.get("industry"))
        for row in high_rows
        if (_number(row.get("total"), 0) or 0) <= 0 and row.get("industry") not in {"其他"}
    )
    if invalid_denominators:
        reasons.append("存在无有效成分股分母的行业")

    gap_dates = [] if mode == "intraday" else _history_gap_dates(highs, lows)
    if gap_dates:
        # 空白日期会从分位数历史(valid_indexes)中剔除，仅降级提示而非全板停用
        warnings.append(f"历史序列存在{len(gap_dates)}个整市场空白日期，已从分位历史剔除")

    coverage = highs.get("coverage") or lows.get("coverage") or {}
    active = _number(coverage.get("active"), 0) or 0
    spot = _number(coverage.get("spot") or coverage.get("covered"), 0) or 0
    coverage_ratio = spot / active if active > 0 else None
    if mode == "intraday" and (coverage_ratio is None or coverage_ratio < 0.9):
        reasons.append("盘中行情覆盖不足90%")

    market_cap_covered = _number(coverage.get("market_cap"), 0) or 0
    market_cap_ratio = market_cap_covered / active if active > 0 else None
    if mode == "intraday" and market_cap_ratio is not None and market_cap_ratio < 0.9:
        warnings.append("盘中市值覆盖不足90%，市值字段已降级")

    flow_date = _date_key((flow or {}).get("as_of") or (flow or {}).get("trade_date"))
    flow_aligned = bool(latest and flow_date and latest == flow_date)
    if mode == "daily" and not flow_aligned:
        # 动能分位相对其自身历史计算，日期错位时仍可作参考背景，降级而非停用
        warnings.append("成交动能与热力图日期不一致，动能仅作参考背景")
    elif mode == "intraday":
        warnings.append("成交动能仅作为上一收盘日风险背景，不参与盘中确认")

    flow_coverage = _number((((flow or {}).get("data_quality") or {}).get("coverage") or {}).get("ratio"))
    if mode == "daily" and flow is not None and flow_coverage is not None and flow_coverage < 0.9:
        warnings.append("成交动能覆盖不足90%")

    peer_mismatch = False
    if peer_totals:
        comparable = [
            item for item in peer_totals
            if item.get("date") == latest and item.get("highs") is not None and item.get("lows") is not None
        ]
        if len(comparable) >= 2:
            # 不同分类的覆盖率天然不同(如三级分类存在未归类股票),只在差异显著时降级提示
            for key in ("highs", "lows"):
                values = [item[key] for item in comparable]
                if max(values) - min(values) > max(10, 0.05 * max(values)):
                    peer_mismatch = True
            if peer_mismatch:
                warnings.append("部分行业分类的全市场事件总数差异超过5%，跨分类比较需谨慎")

    if stale:
        warnings.append("盘中快照已超过实时新鲜度阈值")

    score = 100
    score -= 24 * len(reasons)
    score -= 8 * len(warnings)
    score = int(_clamp(score))
    status = "invalid" if reasons else ("degraded" if warnings else "valid")
    return {
        "status": status,
        "label": {"valid": "可信", "degraded": "降级", "invalid": "暂停评分"}[status],
        "score": score,
        "can_score": status != "invalid",
        "latest_date": latest,
        "flow_date": flow_date,
        "dates_aligned": dates_aligned,
        "flow_aligned": flow_aligned,
        "totals_consistent": totals_consistent,
        "classifications_aligned": classifications_aligned,
        "peer_totals_consistent": not peer_mismatch,
        "coverage_ratio": round(coverage_ratio, 4) if coverage_ratio is not None else None,
        "market_cap_coverage_ratio": round(market_cap_ratio, 4) if market_cap_ratio is not None else None,
        "invalid_dates": gap_dates,
        "invalid_denominators": invalid_denominators,
        "reasons": reasons,
        "warnings": warnings,
        "classification": ((flow or {}).get("data_quality") or {}).get("classification"),
    }


def market_permission(highs: dict, lows: dict, flow: dict | None, quality: dict, mode: str) -> dict:
    high_total_row = _total(highs)
    low_total_row = _total(lows)
    total = max(int(_number(high_total_row.get("total"), 0) or 0), 1)
    if mode == "intraday":
        high_count = (
            _strict_retained(high_total_row, "highs")
            if _has_strict_retained(high_total_row)
            else int(_number(high_total_row.get("standing_count"), 0) or 0)
        )
        low_count = (
            _strict_retained(low_total_row, "lows")
            if _has_strict_retained(low_total_row)
            else int(_number(low_total_row.get("standing_count"), 0) or 0)
        )
    else:
        high_count = _count(high_total_row)
        low_count = _count(low_total_row)
    net_rate = (high_count - low_count) / total * 100
    ratio = high_count / low_count if low_count > 0 else (math.inf if high_count else 1.0)
    flow_market = (flow or {}).get("market") or {}
    market_breadth = _number(flow_market.get("breadth"))
    price_change = _number(flow_market.get("price_change_pct"))

    if not quality.get("can_score"):
        state, label = "paused", "暂停"
        message = "数据闸门未通过，停止机会排序"
    elif (
        net_rate <= -3
        or ratio < 0.6
        or (mode == "daily" and market_breadth is not None and market_breadth < -0.25)
    ):
        state, label = "defense", "防守"
        message = "新低与下跌广度占优，只保留相对强势观察"
    elif (
        net_rate >= 3
        and ratio > 1.5
        and (mode != "daily" or market_breadth is None or market_breadth > -0.1)
    ):
        state, label = "attack", "进攻"
        message = "新高扩散与市场广度允许寻找确认机会"
    else:
        state, label = "watch", "观察"
        message = "方向证据尚未形成一致优势"

    return {
        "state": state,
        "label": label,
        "message": message,
        "highs": high_count,
        "lows": low_count,
        "total": total,
        "net_breadth_pct": round(net_rate, 2),
        "high_low_ratio": None if math.isinf(ratio) else round(ratio, 2),
        "market_breadth": round(market_breadth, 4) if market_breadth is not None else None,
        "market_price_change_pct": round(price_change, 3) if price_change is not None else None,
    }


def _series_metrics(
    high_row: dict,
    low_row: dict,
    total: int,
    market_high_rates: list[float],
    market_low_rates: list[float],
    valid_indexes: list[int],
    kappa: float = 20.0,
) -> dict:
    high_counts = high_row.get("daily_counts") or []
    low_counts = low_row.get("daily_counts") or []
    length = min(len(high_counts), len(low_counts))
    raw_series: list[float] = []
    adjusted_series: list[float] = []
    for index in range(length):
        high = float(_number(high_counts[index], 0) or 0)
        low = float(_number(low_counts[index], 0) or 0)
        raw_series.append((high - low) / max(total, 1) * 100)
        market_high = market_high_rates[index] if index < len(market_high_rates) else 0
        market_low = market_low_rates[index] if index < len(market_low_rates) else 0
        adjusted_high = (high + kappa * market_high) / (total + kappa)
        adjusted_low = (low + kappa * market_low) / (total + kappa)
        adjusted_series.append((adjusted_high - adjusted_low) * 100)

    prior_indexes = [index for index in valid_indexes if 0 < index < length]
    current = adjusted_series[0] if adjusted_series else 0.0
    prior_values = [adjusted_series[index] for index in prior_indexes]
    breadth_percentile = _percentile_rank(current, prior_values)
    previous_three = [adjusted_series[index] for index in prior_indexes[:3]]
    acceleration = current - _mean(previous_three)
    historical_acceleration = []
    for index in prior_indexes:
        older = [adjusted_series[j] for j in prior_indexes if j > index][:3]
        if older:
            historical_acceleration.append(adjusted_series[index] - _mean(older))
    acceleration_percentile = _percentile_rank(acceleration, historical_acceleration)
    return {
        "raw": raw_series,
        "adjusted": adjusted_series,
        "net_breadth_pct": round(raw_series[0], 2) if raw_series else 0.0,
        "adjusted_net_breadth_pct": round(current, 2),
        "breadth_percentile": breadth_percentile,
        "acceleration_pp": round(acceleration, 2),
        "acceleration_percentile": acceleration_percentile,
    }


def _current_adjusted_net(
    high_row: dict,
    low_row: dict,
    total: int,
    market_high_rate: float,
    market_low_rate: float,
    kappa: float = 20.0,
) -> float:
    """当日行业规模收缩后的净扩散,与 _series_metrics 同口径。"""
    high = float(_count(high_row))
    low = float(_count(low_row))
    adjusted_high = (high + kappa * market_high_rate) / (total + kappa)
    adjusted_low = (low + kappa * market_low_rate) / (total + kappa)
    return (adjusted_high - adjusted_low) * 100


def _risk_evidence(flow_row: dict | None) -> tuple[list[str], list[str]]:
    if not flow_row:
        return [], ["缺少成交动能风险背景"]
    domains: list[str] = []
    reasons: list[str] = []
    level = str(flow_row.get("risk_level") or "")
    pattern = str(flow_row.get("risk_pattern") or "")
    if level in {"warning", "danger"}:
        domains.append("模型风险")
    if (_number(flow_row.get("price_extension_pctile"), 0) or 0) >= 90:
        domains.append("价格延伸")
    if (_number(flow_row.get("internal_top5_pctile"), 0) or 0) >= 85:
        domains.append("成交集中")
    if (_number(flow_row.get("crowding_score"), 0) or 0) >= 80:
        domains.append("拥挤")
    if (_number(flow_row.get("direct_position_score"), 0) or 0) >= 80:
        domains.append("持仓集中")
    if (_number(flow_row.get("external_fragility_score"), 0) or 0) >= 80:
        domains.append("外部脆弱")
    for item in flow_row.get("risk_reasons") or []:
        if item and item not in reasons:
            reasons.append(str(item))
    if pattern and pattern != "normal":
        label = flow_row.get("risk_pattern_label") or pattern
        reasons.insert(0, str(label))
    return list(dict.fromkeys(domains)), reasons[:4]


def _daily_stage(
    *,
    quality: dict,
    permission: dict,
    breadth_percentile: float,
    acceleration_percentile: float,
    net_breadth: float,
    strength_percentile: float,
    trend_percentile: float,
    activity_percentile: float,
    participation_percentile: float,
    persistence: int,
    risk_domains: list[str],
    risk_level: str,
    risk_pattern: str,
) -> str:
    if not quality.get("can_score"):
        return "insufficient"
    positives = sum(
        metric >= 60
        for metric in (breadth_percentile, trend_percentile, activity_percentile)
    )
    weak = sum(
        metric < 45
        for metric in (breadth_percentile, trend_percentile, activity_percentile)
    )
    crowded = (
        risk_level == "danger"
        or risk_pattern == "upside_exhaustion"
        or (len(risk_domains) >= 2 and acceleration_percentile < 50)
    )
    if crowded and (trend_percentile >= 55 or breadth_percentile >= 55):
        return "crowded"
    if net_breadth < 0 and acceleration_percentile < 35 and weak >= 2:
        return "declining"
    if positives == 3 and acceleration_percentile >= 45 and persistence >= 2:
        return "relative" if permission.get("state") == "defense" else "confirmed"
    if acceleration_percentile >= 70 and positives >= 2 and len(risk_domains) <= 1:
        return "relative" if permission.get("state") == "defense" else "emerging"
    if positives >= 2 and net_breadth > 0 and persistence >= 2:
        return "relative" if permission.get("state") == "defense" else "extending"
    # 强度通道:扩散相对自身历史不新鲜(持续强势会被自身分位压回50),
    # 但在当日全市场横截面中仍居前,且趋势与成交参与配合,视为延续
    if (
        strength_percentile >= 80
        and net_breadth > 0
        and trend_percentile >= 55
        and activity_percentile >= 55
        and len(risk_domains) <= 1
    ):
        return "relative" if permission.get("state") == "defense" else "extending"
    if weak >= 2:
        return "declining"
    return "dormant"


def _daily_industry(
    industry: str,
    high_row: dict,
    low_row: dict,
    flow_row: dict | None,
    *,
    market_high_rates: list[float],
    market_low_rates: list[float],
    valid_indexes: list[int],
    strength_percentile: float,
    quality: dict,
    permission: dict,
) -> dict:
    total = max(int(_number(high_row.get("total"), 0) or 0), 1)
    high_count = _count(high_row)
    low_count = _count(low_row)
    series = _series_metrics(
        high_row, low_row, total, market_high_rates, market_low_rates, valid_indexes
    )
    trend_percentile = float(_number((flow_row or {}).get("price_result_pctile"), 50) or 50)
    activity_percentile = float(_number((flow_row or {}).get("activity_pctile"), 50) or 50)
    participation_percentile = float(
        _number((flow_row or {}).get("active_breadth_pctile"), 50) or 50
    )
    excess_return = float(_number((flow_row or {}).get("excess_return_pct"), 0) or 0)
    effective_participants = float(
        _number((flow_row or {}).get("effective_participants"), 0) or 0
    )
    persistence = int(_number((flow_row or {}).get("persistence"), 0) or 0)
    risk_domains, risk_reasons = _risk_evidence(flow_row)
    risk_level = str((flow_row or {}).get("risk_level") or "unknown")
    risk_pattern = str((flow_row or {}).get("risk_pattern") or "unknown")
    stage = _daily_stage(
        quality=quality,
        permission=permission,
        breadth_percentile=series["breadth_percentile"],
        acceleration_percentile=series["acceleration_percentile"],
        net_breadth=series["adjusted_net_breadth_pct"],
        strength_percentile=strength_percentile,
        trend_percentile=trend_percentile,
        activity_percentile=activity_percentile,
        participation_percentile=participation_percentile,
        persistence=persistence,
        risk_domains=risk_domains,
        risk_level=risk_level,
        risk_pattern=risk_pattern,
    )

    breadth_component = max(series["breadth_percentile"], strength_percentile)
    confirmations = {
        "trend": trend_percentile >= 60 and excess_return > 0,
        "breadth": breadth_component >= 60 and series["adjusted_net_breadth_pct"] > 0,
        "activity": activity_percentile >= 60,
        "participation": participation_percentile >= 60,
    }
    why: list[str] = []
    if confirmations["breadth"]:
        if series["breadth_percentile"] >= 60:
            why.append(
                f"净扩散 {series['adjusted_net_breadth_pct']:+.1f}% · 自身历史"
                f"{series['breadth_percentile']:.0f}分位"
            )
        else:
            why.append(
                f"净扩散 {series['adjusted_net_breadth_pct']:+.1f}% · 横截面强度"
                f"{strength_percentile:.0f}分位"
            )
    if series["acceleration_percentile"] >= 65:
        why.append(
            f"扩散加速度 {series['acceleration_pp']:+.1f}pct · "
            f"{series['acceleration_percentile']:.0f}分位"
        )
    if confirmations["trend"]:
        why.append(f"行业超额收益 {excess_return:+.2f}%")
    if confirmations["activity"]:
        why.append(f"成交参与 {activity_percentile:.0f}分位")
    if confirmations["participation"]:
        why.append(f"活跃广度 {participation_percentile:.0f}分位")
    missing = [
        label for key, label in (
            ("trend", "相对收益确认"),
            ("breadth", "扩散确认"),
            ("activity", "成交参与确认"),
            ("participation", "有效参与者确认"),
        )
        if not confirmations[key]
    ]
    if not why:
        why.append("尚未形成趋势、扩散和成交的共振")

    base_score = (
        0.25 * trend_percentile
        + 0.25 * breadth_component
        + 0.20 * activity_percentile
        + 0.15 * series["acceleration_percentile"]
        + 0.15 * participation_percentile
    )
    score = base_score - len(risk_domains) * 7
    if permission.get("state") == "defense":
        score -= 12
    if stage in {"crowded", "declining", "insufficient"}:
        score -= 12

    if stage == "crowded":
        invalidation = "禁止追涨；扩散继续减速或跌破触发日相对低点则退出"
    elif stage == "declining":
        invalidation = "仅观察；需净扩散转正且相对收益重新确认"
    else:
        invalidation = "相对价格跌破触发位且净扩散转弱，或5日内没有跟随"

    return {
        "industry": industry,
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "actionable": stage in {"confirmed", "extending"} and permission.get("state") != "defense",
        "score": round(_clamp(score), 1),
        "total": total,
        "high_count": high_count,
        "low_count": low_count,
        "high_rate_pct": round(high_count / total * 100, 2),
        "low_rate_pct": round(low_count / total * 100, 2),
        **{key: value for key, value in series.items() if key not in {"raw", "adjusted"}},
        "strength_percentile": round(strength_percentile, 1),
        "trend_percentile": round(trend_percentile, 1),
        "activity_percentile": round(activity_percentile, 1),
        "participation_percentile": round(participation_percentile, 1),
        "turnover_amount": float(_number((flow_row or {}).get("amount"), 0) or 0),
        "excess_return_pct": round(excess_return, 3),
        "effective_participants": round(effective_participants, 1),
        "persistence": persistence,
        "confirmations": confirmations,
        "confirmation_count": sum(confirmations.values()),
        "why_now": why[:3],
        "missing_confirmations": missing,
        "risk_level": risk_level,
        "risk_pattern": risk_pattern,
        "risk_pattern_label": (flow_row or {}).get("risk_pattern_label") or risk_pattern,
        "risk_domains": risk_domains,
        "risk_reasons": risk_reasons,
        "invalidation": invalidation,
        "leaders": (flow_row or {}).get("leaders") or [],
        "classification_level": (flow_row or {}).get("classification_level"),
    }


def _intraday_industry(
    industry: str,
    high_row: dict,
    low_row: dict,
    flow_row: dict | None,
    *,
    quality: dict,
    permission: dict,
) -> dict:
    total = max(int(_number(high_row.get("total"), 0) or 0), 1)
    high_signal = _count(high_row)
    low_signal = _count(low_row)
    high_touched = int(_number(high_row.get("touched_count"), 0) or 0)
    low_touched = int(_number(low_row.get("touched_count"), 0) or 0)
    high_retained = _strict_retained(high_row, "highs")
    low_retained = _strict_retained(low_row, "lows")
    high_retention = high_retained / high_touched if high_touched > 0 else None
    low_retention = low_retained / low_touched if low_touched > 0 else None
    net_breadth = (high_retained - low_retained) / total * 100
    evidence = []
    if high_touched:
        evidence.append(
            f"触及历史最高价 {high_touched}只，严格保持 {high_retained}只"
        )
    if high_retention is not None:
        evidence.append(f"同阈值保持率 {high_retention * 100:.0f}%")
    if net_breadth > 0:
        evidence.append(f"盘中严格净扩散 {net_breadth:+.1f}%")

    if not quality.get("can_score"):
        stage = "insufficient"
    elif low_retained > high_retained and (low_retention or 0) >= 0.45:
        stage = "declining"
    elif high_retained >= 3 and net_breadth > 0 and (high_retention or 0) >= 0.65:
        stage = "relative" if permission.get("state") == "defense" else "intraday_watch"
    else:
        stage = "dormant"

    prior_risk_domains, prior_risk_reasons = _risk_evidence(flow_row)
    score = _clamp(
        45
        + net_breadth * 3
        + ((high_retention or 0.0) - 0.5) * 30
        - len(prior_risk_domains) * 5
        - (10 if permission.get("state") == "defense" else 0)
    )
    missing = ["连续两个5分钟窗口", "同时段成交活跃确认", "行业相对VWAP确认"]
    if not evidence:
        evidence.append("尚未形成可比较的盘中扩散")
    return {
        "industry": industry,
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "actionable": False,
        "score": round(score, 1),
        "total": total,
        "high_count": high_retained,
        "low_count": low_retained,
        "high_rate_pct": round(high_retained / total * 100, 2),
        "low_rate_pct": round(low_retained / total * 100, 2),
        "signal_high_count": high_signal,
        "signal_low_count": low_signal,
        "high_touched": high_touched,
        "low_touched": low_touched,
        "high_retained": high_retained,
        "low_retained": low_retained,
        "high_retention_pct": round(high_retention * 100, 1) if high_retention is not None else None,
        "low_retention_pct": round(low_retention * 100, 1) if low_retention is not None else None,
        "net_breadth_pct": round(net_breadth, 2),
        "adjusted_net_breadth_pct": round(net_breadth, 2),
        "breadth_percentile": None,
        "acceleration_pp": None,
        "acceleration_percentile": None,
        "trend_percentile": None,
        "activity_percentile": None,
        "participation_percentile": None,
        "turnover_amount": float(_number((flow_row or {}).get("amount"), 0) or 0),
        "effective_participants": None,
        "confirmations": {
            "breadth": stage in {"intraday_watch", "relative"},
            "retention": (high_retention or 0) >= 0.65,
            "activity": False,
            "trend": False,
        },
        "confirmation_count": int(stage in {"intraday_watch", "relative"}) + int(
            (high_retention or 0) >= 0.65
        ),
        "why_now": evidence[:3],
        "missing_confirmations": missing,
        "risk_level": (flow_row or {}).get("risk_level") or "prior_close",
        "risk_pattern": (flow_row or {}).get("risk_pattern") or "prior_close",
        "risk_pattern_label": "上一收盘日风险背景",
        "risk_domains": prior_risk_domains,
        "risk_reasons": prior_risk_reasons,
        "invalidation": "严格保持率跌破45%，或连续两个5分钟窗口净扩散转负",
        "leaders": [],
    }


def _historical_calibration(highs: dict, lows: dict, flow: dict | None) -> dict:
    """Small, causal diagnostic sample; never presents an uncalibrated probability.

    阶段判定与实盘共用 _daily_stage(同口径 kappa 收缩、横截面强度、
    市场闸门降级),保证校准概率对应实盘信号。
    """
    flow_rows = _flow_map(flow)
    high_rows = _row_map(highs)
    low_rows = _row_map(lows)
    date_keys = _date_keys(highs)
    chronological = [date for date in reversed(date_keys) if date]
    gap_dates = set(_history_gap_dates(highs, lows))
    valid_dates = [date for date in chronological if date not in gap_dates]
    date_index = {date: index for index, date in enumerate(date_keys)}

    high_total = _total(highs)
    low_total = _total(lows)
    total_market = max(int(_number(high_total.get("total"), 0) or 0), 1)
    # 与实盘 market_permission 同口径:动能市场广度序列(若有)参与 defense 判定
    market_breadth_by_date = {}
    for item in ((flow or {}).get("market") or {}).get("series") or []:
        key = _date_key(item.get("date"))
        if key:
            market_breadth_by_date[key] = _number(item.get("breadth"))
    market_rates: dict[str, tuple[float, float]] = {}
    permissions: dict[str, dict] = {}
    for date in valid_dates:
        index = date_index[date]
        market_high = _count(high_total, index) / total_market
        market_low = _count(low_total, index) / total_market
        market_rates[date] = (market_high, market_low)
        high_count = _count(high_total, index)
        low_count = _count(low_total, index)
        net_rate = (high_count - low_count) / total_market * 100
        ratio = high_count / low_count if low_count > 0 else (
            math.inf if high_count else 1.0
        )
        breadth = market_breadth_by_date.get(date)
        state = "defense" if (
            net_rate <= -3
            or ratio < 0.6
            or (breadth is not None and breadth < -0.25)
        ) else "watch"
        permissions[date] = {"state": state}

    totals = {
        industry: max(int(_number(row.get("total"), 0) or 0), 1)
        for industry, row in high_rows.items()
    }
    net_series: dict[str, list[float | None]] = {}
    for industry, high_row in high_rows.items():
        low_row = low_rows.get(industry)
        if not low_row:
            continue
        high_counts = high_row.get("daily_counts") or []
        low_counts = low_row.get("daily_counts") or []
        total = totals[industry]
        series: list[float | None] = []
        for date in valid_dates:
            index = date_index[date]
            if index >= len(high_counts) or index >= len(low_counts):
                series.append(None)
                continue
            market_high, market_low = market_rates[date]
            adjusted_high = (float(_number(high_counts[index], 0) or 0) + 20.0 * market_high) / (total + 20.0)
            adjusted_low = (float(_number(low_counts[index], 0) or 0) + 20.0 * market_low) / (total + 20.0)
            series.append((adjusted_high - adjusted_low) * 100)
        net_series[industry] = series

    returns_by_stage: dict[str, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    event_dates: dict[str, set[str]] = defaultdict(set)
    quality_ok = {"can_score": True}

    for industry, series in net_series.items():
        flow_row = flow_rows.get(industry)
        if not flow_row:
            continue
        flow_series = {
            _date_key(item.get("date")): item
            for item in flow_row.get("series") or []
            if _date_key(item.get("date"))
        }
        history: list[float] = []
        for position, date in enumerate(valid_dates):
            net = series[position]
            if net is None:
                continue
            item = flow_series.get(date)
            if not item:
                history.append(net)
                continue
            priors = history
            breadth_p = _percentile_rank(net, priors)
            acceleration = net - _mean(priors[-3:])
            acceleration_history = [
                priors[index] - _mean(priors[max(0, index - 3):index])
                for index in range(1, len(priors))
            ]
            acceleration_p = _percentile_rank(acceleration, acceleration_history)
            cross_section = [
                other[position]
                for other in net_series.values()
                if other[position] is not None
            ]
            strength_p = _percentile_rank(net, cross_section)
            risk_domains, _ = _risk_evidence(item)
            stage = _daily_stage(
                quality=quality_ok,
                permission=permissions[date],
                breadth_percentile=breadth_p,
                acceleration_percentile=acceleration_p,
                net_breadth=net,
                strength_percentile=strength_p,
                trend_percentile=float(_number(item.get("price_result_pctile"), 50) or 50),
                activity_percentile=float(_number(item.get("activity_pctile"), 50) or 50),
                participation_percentile=float(_number(item.get("active_breadth_pctile"), 50) or 50),
                persistence=int(_number(item.get("persistence"), 0) or 0),
                risk_domains=risk_domains,
                risk_level=str(item.get("risk_level") or "normal"),
                risk_pattern=str(item.get("risk_pattern") or "normal"),
            )
            for horizon in (1, 3, 5):
                future_dates = valid_dates[position + 1:position + 1 + horizon]
                if len(future_dates) < horizon:
                    continue
                returns = [
                    _number((flow_series.get(future_date) or {}).get("excess_return_pct"))
                    for future_date in future_dates
                ]
                if any(value is None for value in returns):
                    continue
                returns_by_stage[stage][horizon].append(sum(float(value) for value in returns))
                event_dates[stage].add(date)
            history.append(net)

    stages = {}
    for stage, horizons in returns_by_stage.items():
        stage_result = {"label": STAGE_LABELS.get(stage, stage), "horizons": {}}
        for horizon, values in horizons.items():
            stage_result["horizons"][str(horizon)] = {
                "samples": len(values),
                "mean_excess_pct": round(_mean(values), 3),
                "median_excess_pct": round(statistics.median(values), 3),
                "hit_rate": round(sum(value > 0 for value in values) / len(values), 4),
                "q20_excess_pct": round(_quantile(values, 0.2) or 0, 3),
            }
        stage_result["independent_dates"] = len(event_dates.get(stage, set()))
        stages[stage] = stage_result

    sample_days = len({date for dates in event_dates.values() for date in dates})
    confirmed_samples = (
        ((stages.get("confirmed") or {}).get("horizons") or {}).get("5") or {}
    ).get("samples", 0)
    calibrated = sample_days >= 60 and confirmed_samples >= 100
    return {
        "status": "ready" if calibrated else "calibrating",
        "label": "已校准" if calibrated else "校准中",
        "sample_days": sample_days,
        "minimum_days": 60,
        "probability_available": calibrated,
        "message": (
            "历史样本达到展示概率门槛"
            if calibrated
            else "样本不足，不显示成功概率；当前统计只作透明诊断"
        ),
        "entry_rule": "信号后下一交易日开盘或VWAP",
        "stages": stages,
    }


def build_opportunity_snapshot(
    highs: dict,
    lows: dict,
    flow: dict | None,
    *,
    scheme: str,
    period: str,
    mode: str,
    stale: bool = False,
    peer_totals: list[dict] | None = None,
) -> dict:
    if scheme not in SCHEME_SUFFIX:
        raise ValueError("scheme must be sw, ths or sw3")
    if mode not in {"daily", "intraday"}:
        raise ValueError("mode must be daily or intraday")
    quality = validate_inputs(
        highs, lows, flow, mode=mode, stale=stale, peer_totals=peer_totals
    )
    permission = market_permission(highs, lows, flow, quality, mode)
    high_map = _row_map(highs)
    low_map = _row_map(lows)
    flow_rows = _flow_map(flow)
    # 动能日期错位时只作背景:阶段判定与评分不使用错位分位数(降级已在 quality 体现)
    flow_usable = mode != "daily" or quality.get("flow_aligned", True)
    names = sorted(high_map.keys() & low_map.keys())
    industries: list[dict] = []

    if mode == "daily":
        high_total = _total(highs)
        low_total = _total(lows)
        total_market = max(int(_number(high_total.get("total"), 0) or 0), 1)
        length = min(
            len(high_total.get("daily_counts") or []),
            len(low_total.get("daily_counts") or []),
        )
        invalid_dates = set(quality.get("invalid_dates") or [])
        date_keys = _date_keys(highs)
        valid_indexes = [
            index for index in range(length)
            if index < len(date_keys) and date_keys[index] not in invalid_dates
        ]
        market_high_rates = [
            _count(high_total, index) / total_market for index in range(length)
        ]
        market_low_rates = [
            _count(low_total, index) / total_market for index in range(length)
        ]
        # 横截面强度:当日调整后净扩散在全部行业中的分位,弥补自身历史分位
        # 对持续强势行业的系统性低估
        current_net = {}
        for name in names:
            total = max(int(_number(high_map[name].get("total"), 0) or 0), 1)
            current_net[name] = _current_adjusted_net(
                high_map[name],
                low_map[name],
                total,
                market_high_rates[0] if market_high_rates else 0,
                market_low_rates[0] if market_low_rates else 0,
            )
        strength_percentiles = {
            name: _percentile_rank(value, current_net.values())
            for name, value in current_net.items()
        }
        industries = [
            _daily_industry(
                name,
                high_map[name],
                low_map[name],
                flow_rows.get(name) if flow_usable else None,
                market_high_rates=market_high_rates,
                market_low_rates=market_low_rates,
                valid_indexes=valid_indexes,
                strength_percentile=strength_percentiles.get(name, 50.0),
                quality=quality,
                permission=permission,
            )
            for name in names
        ]
        calibration = _historical_calibration(highs, lows, flow)
    else:
        industries = [
            _intraday_industry(
                name,
                high_map[name],
                low_map[name],
                flow_rows.get(name),
                quality=quality,
                permission=permission,
            )
            for name in names
        ]
        calibration = {
            "status": "collecting",
            "label": "积累中",
            "sample_days": 0,
            "minimum_days": 60,
            "probability_available": False,
            "message": "盘中同分钟历史尚不足，不显示成功概率",
            "entry_rule": "连续两个5分钟窗口确认后，下一根5分钟K线",
            "stages": {},
        }

    industries.sort(
        key=lambda row: (
            STAGE_ORDER.get(row["stage"], 99),
            -row["score"],
            row["industry"],
        )
    )
    counts = defaultdict(int)
    for row in industries:
        counts[row["stage"]] += 1
    return {
        "schema_version": 1,
        "model_version": "heatmap-opportunity-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scheme": scheme,
        "period": period,
        "mode": mode,
        "quality": quality,
        "market_permission": permission,
        "stage_counts": dict(counts),
        "industries": industries,
        "calibration": calibration,
        "methodology": {
            "opportunity_definition": "状态迁移×扩散×相对趋势×成交参与－独立风险",
            "breadth": "行业规模收缩后的(创新高－创新低)/有效成分数",
            "strength": "当日调整后净扩散的全行业横截面分位,弥补自身历史分位对持续强势的低估",
            "acceleration": "相对最近3个有效交易日的净扩散变化及自身历史分位",
            "confirmation": "相对收益、成交参与、活跃广度与有效参与者",
            "risk": "价格延伸、成交集中、拥挤、持仓和流动性脆弱性独立展示",
            "intraday_limit": "盘中不使用上一收盘日成交动能作为实时确认",
            "probability_rule": "样本不足时只展示证据状态，不展示成功概率",
        },
    }


def load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def source_filenames(period: str, scheme: str, mode: str, window: int = 20) -> tuple[str, str, str]:
    if scheme not in SCHEME_SUFFIX:
        raise ValueError("scheme must be sw, ths or sw3")
    suffix = SCHEME_SUFFIX[scheme]
    if mode == "intraday":
        key = "1year" if window == 250 else f"{window}d"
        if window == 20:
            key = "20d"
        return (
            f"intraday_highs_{key}{suffix}.json",
            f"intraday_lows_{key}{suffix}.json",
            f"capital_flow_v2{suffix}.json",
        )
    if period not in PERIODS:
        raise ValueError("period must be month, 60d, 120d, 1year or alltime")
    return (
        f"new_highs_data_{period}{suffix}.json",
        f"new_lows_data_{period}{suffix}.json",
        f"capital_flow_v2{suffix}.json",
    )


def load_peer_totals(data_dir: str, period: str, mode: str, window: int = 20) -> list[dict]:
    result = []
    for scheme in SCHEME_SUFFIX:
        try:
            high_name, low_name, _ = source_filenames(period, scheme, mode, window)
        except ValueError:
            continue
        highs = load_json(os.path.join(data_dir, high_name), {}) or {}
        lows = load_json(os.path.join(data_dir, low_name), {}) or {}
        result.append({
            "scheme": scheme,
            "date": (_date_keys(highs) or [None])[0],
            "highs": _total_count(highs) if highs else None,
            "lows": _total_count(lows) if lows else None,
        })
    return result


def load_opportunity_snapshot(
    data_dir: str,
    *,
    scheme: str,
    period: str,
    mode: str,
    window: int = 20,
    stale: bool = False,
) -> dict:
    high_name, low_name, flow_name = source_filenames(period, scheme, mode, window)
    highs = load_json(os.path.join(data_dir, high_name), {}) or {}
    lows = load_json(os.path.join(data_dir, low_name), {}) or {}
    flow = load_json(os.path.join(data_dir, flow_name), None)
    if not highs or not lows:
        raise FileNotFoundError("热力图数据尚未生成")
    return build_opportunity_snapshot(
        highs,
        lows,
        flow,
        scheme=scheme,
        period=period,
        mode=mode,
        stale=stale,
        peer_totals=load_peer_totals(data_dir, period, mode, window),
    )
