"""Decision intelligence layer for the stock-finance desktop application.

The existing pages are deliberately kept as the evidence layer.  This module
does not add their scores together and call the result a probability.  It
turns the available point-in-time observations into a decision loop:

market regime -> change detection -> driver/structure -> tradability ->
trade plan -> portfolio risk -> alerts -> probability trust.

Every derived conclusion carries evidence and source dates.  Missing event or
execution data is reported as unavailable instead of being imputed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from datetime import date, datetime
from typing import Any, Iterable


SCHEMA_VERSION = 1
MODEL_VERSION = "decision-intelligence-v1"
SCHEME_LABELS = {"sw": "申万一级", "ths": "同花顺", "sw3": "申万三级"}
SCHEME_SUFFIX = {"sw": "", "ths": "_ths", "sw3": "_sw3"}
CORE_SOURCES = ("capital_flow", "market_cap", "crowding")


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _round(value: Any, digits: int = 2) -> float | None:
    number = _num(value)
    return round(number, digits) if number is not None else None


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
    if len(digits) < 8:
        return None
    candidate = digits[:8]
    try:
        datetime.strptime(candidate, "%Y%m%d")
    except ValueError:
        return None
    return candidate


def _payload_date(payload: dict, *keys: str) -> str | None:
    for key in keys:
        candidate = _date_key(payload.get(key))
        if candidate:
            return candidate
    market = payload.get("market")
    if isinstance(market, dict):
        return _date_key(market.get("date"))
    return None


def _industry_map(payload: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in payload.get("industries") or []:
        if not isinstance(row, dict):
            continue
        industry = str(row.get("industry") or "").strip()
        if industry and not row.get("is_total"):
            result[industry] = row
    return result


def _unique(values: Iterable[Any], limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def _series(row: dict) -> list[dict]:
    values = [item for item in row.get("series") or [] if isinstance(item, dict)]
    values.sort(key=lambda item: str(item.get("date") or ""))
    return values


def _series_value(row: dict, key: str, offset: int = 0) -> float | None:
    values = _series(row)
    index = len(values) - 1 - offset
    if index >= 0:
        value = _num(values[index].get(key))
        if value is not None:
            return value
    if offset == 0:
        return _num(row.get(key))
    return None


def _delta(row: dict, key: str) -> tuple[float | None, float | None, float | None]:
    current = _series_value(row, key, 0)
    previous = _series_value(row, key, 1)
    older = _series_value(row, key, 2)
    change = current - previous if current is not None and previous is not None else None
    previous_change = (
        previous - older if previous is not None and older is not None else None
    )
    acceleration = (
        change - previous_change
        if change is not None and previous_change is not None
        else None
    )
    return current, change, acceleration


def _business_day_distance(start: str | None, end: str | None) -> int | None:
    if not start or not end or start >= end:
        return 0 if start and end else None
    try:
        cursor = datetime.strptime(start, "%Y%m%d").date()
        target = datetime.strptime(end, "%Y%m%d").date()
    except ValueError:
        return None
    count = 0
    while cursor < target:
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            count += 1
    return count


def _source_quality(
    payloads: dict[str, dict],
    expected_date: str | None = None,
) -> dict:
    sources: dict[str, dict] = {}
    core_dates = []
    for name, payload in payloads.items():
        source_date = _payload_date(
            payload,
            "trade_date",
            "as_of",
            "latest_date",
            "date",
            "updated_at",
        )
        available = bool(payload)
        sources[name] = {
            "date": source_date,
            "available": available,
            "status": "available" if available else "missing",
        }
        if name in CORE_SOURCES and source_date:
            core_dates.append(source_date)

    as_of = min(core_dates) if len(core_dates) == len(CORE_SOURCES) else (
        max(core_dates) if core_dates else None
    )
    for name, source in sources.items():
        source_date = source["date"]
        if not source["available"]:
            continue
        if not source_date:
            source["status"] = "undated"
        elif as_of and source_date == as_of:
            source["status"] = "aligned"
        elif as_of and source_date < as_of:
            source["status"] = "prior"
        elif as_of and source_date > as_of:
            source["status"] = "future_relative_to_common_date"

    missing_core = [name for name in CORE_SOURCES if not sources[name]["available"]]
    dated_core = [sources[name]["date"] for name in CORE_SOURCES if sources[name]["date"]]
    aligned_core = len(set(dated_core)) <= 1 and len(dated_core) == len(CORE_SOURCES)
    status = "valid" if not missing_core and aligned_core else (
        "degraded" if not missing_core else "invalid"
    )
    warnings = []
    if missing_core:
        warnings.append("核心数据缺失：" + "、".join(missing_core))
    if dated_core and not aligned_core:
        warnings.append("核心数据日期不一致，跨模块结论仅作观察")
    for name, source in sources.items():
        if source["status"] in {"prior", "future_relative_to_common_date", "undated"}:
            warnings.append(f"{name} 日期为 {source['date'] or '未知'}，未与共同决策日对齐")
    expected_date = _date_key(expected_date)
    stale_business_days = _business_day_distance(as_of, expected_date)
    if stale_business_days:
        status = "degraded" if status != "invalid" else status
        warnings.insert(
            0,
            f"共同决策日落后最新应完成交易日 {stale_business_days} 个工作日",
        )
    return {
        "status": status,
        "label": {"valid": "可决策", "degraded": "仅观察", "invalid": "不可用"}[status],
        "as_of": as_of,
        "expected_date": expected_date,
        "stale_business_days": stale_business_days,
        "core_aligned": aligned_core,
        "sources": sources,
        "warnings": _unique(warnings),
    }


def _latest_temperature(temperature: dict, as_of: str | None) -> dict:
    eligible = []
    for row in temperature.get("rows") or []:
        if not isinstance(row, dict):
            continue
        row_date = _date_key(row.get("date"))
        if row_date and (not as_of or row_date <= as_of):
            eligible.append((row_date, row))
    return max(eligible, key=lambda item: item[0])[1] if eligible else {}


def _index_metrics(temperature: dict, as_of: str | None) -> list[dict]:
    result = []
    for code, index in (temperature.get("indices") or {}).items():
        points = []
        for point in index.get("points") or []:
            point_date = _date_key(point.get("date"))
            close = _num(point.get("close"))
            if point_date and close is not None and (not as_of or point_date <= as_of):
                points.append((point_date, close))
        points.sort()
        if not points:
            continue
        closes = [value for _, value in points]
        ret5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else None
        ret20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None
        daily = [
            (closes[i] / closes[i - 1] - 1) * 100
            for i in range(max(1, len(closes) - 20), len(closes))
            if closes[i - 1]
        ]
        volatility = statistics.pstdev(daily) if len(daily) >= 2 else None
        result.append(
            {
                "code": code,
                "name": index.get("name") or code,
                "date": points[-1][0],
                "return_5d_pct": _round(ret5),
                "return_20d_pct": _round(ret20),
                "volatility_20d_pct": _round(volatility),
            }
        )
    return result


def build_market_regime(
    flow: dict,
    market_cap: dict,
    crowding: dict,
    temperature: dict,
    quality: dict,
) -> dict:
    """Classify the market before any industry signal is interpreted."""
    flow_market = flow.get("market") or {}
    cap_market = market_cap.get("market") or {}
    crowd_market = crowding.get("market") or {}
    if isinstance(flow_market, list):
        flow_market = flow_market[-1] if flow_market else {}
    if isinstance(cap_market, list):
        cap_market = cap_market[-1] if cap_market else {}
    if isinstance(crowd_market, list):
        crowd_market = crowd_market[-1] if crowd_market else {}
    as_of = quality.get("as_of")
    temp = _latest_temperature(temperature, as_of)

    breadth = _num(flow_market.get("breadth"))
    direction = _num(flow_market.get("direction_score"))
    activity = _num(flow_market.get("activity_pctile", flow_market.get("total_amount_pctile")))
    amount_ratio = _num(flow_market.get("amount_ratio_20"))
    market_return = _num(cap_market.get("market_return_pct", flow_market.get("price_change_pct")))
    stock_breadth = _num(cap_market.get("stock_breadth_pct"))
    temperature_value = _num(temp.get("temperature"))

    series = _series(flow_market)
    prev = series[-2] if len(series) >= 2 else {}
    breadth_delta = breadth - _num(prev.get("breadth"), breadth or 0) if breadth is not None else None
    direction_delta = direction - _num(prev.get("direction_score"), direction or 0) if direction is not None else None
    activity_delta = activity - _num(prev.get("activity_pctile"), activity or 0) if activity is not None else None

    style = cap_market.get("style") or {}
    style_rows = []
    for key, label in (("top100", "大盘权重"), ("next400", "中盘"), ("rest", "小盘")):
        item = style.get(key) or {}
        style_rows.append(
            {
                "key": key,
                "label": label,
                "return_pct": _round(item.get("return_pct")),
                "breadth_pct": _round(item.get("breadth_pct")),
                "mcap_share_pct": _round(item.get("mcap_share_pct")),
            }
        )
    valid_styles = [item for item in style_rows if item["return_pct"] is not None]
    style_leader = max(valid_styles, key=lambda item: item["return_pct"], default=None)
    style_laggard = min(valid_styles, key=lambda item: item["return_pct"], default=None)
    style_spread = (
        style_leader["return_pct"] - style_laggard["return_pct"]
        if style_leader and style_laggard
        else None
    )

    b = breadth or 0.0
    r = market_return or 0.0
    a = activity if activity is not None else 50.0
    if quality.get("status") == "invalid":
        state, label, permission = "unknown", "数据不足", "paused"
        reason = "核心数据缺失，暂停解释行业信号"
    elif b <= -0.55 and r <= -1.0:
        if a <= 35:
            state, label, permission = "liquidity_defense", "缩量防守", "paused"
            reason = "下跌宽度占优且成交活跃度偏低，承接不足"
        else:
            state, label, permission = "stress_release", "压力释放", "limited"
            reason = "市场广泛下跌，优先等待卖压衰竭与反向确认"
    elif r > 0.8 and b < 0.15:
        state, label, permission = "high_divergence", "指数强、结构弱", "limited"
        reason = "指数上涨但个股扩散不足，注意权重拉动"
    elif b >= 0.45 and r >= 0.8 and a >= 55:
        state, label, permission = "trend_expansion", "趋势扩张", "allowed"
        reason = "价格、上涨宽度和成交参与共同扩张"
    elif b >= 0.15 and r >= 0:
        state = "repair" if (breadth_delta or 0) > 0.15 else "healthy_rotation"
        label = "底部修复" if state == "repair" else "健康轮动"
        permission = "allowed"
        reason = "市场宽度改善，允许寻找有独立确认的方向"
    elif b <= -0.15 or r < -0.5:
        state, label, permission = "defense", "防守退潮", "limited"
        reason = "下跌宽度或价格结果偏弱，只保留相对强势观察"
    else:
        state, label, permission = "balanced", "均衡震荡", "limited"
        reason = "市场缺少一致方向，适合等待行业级别确认"

    if quality.get("status") == "degraded" and permission == "allowed":
        permission = "limited"
        reason += "；但核心日期未完全对齐，执行权限已降级"

    strategy_fit = {
        "breakout": 80 if state == "trend_expansion" else 35 if permission == "limited" else 10,
        "reversal": 75 if state in {"stress_release", "repair"} else 45,
        "rotation": 80 if state in {"healthy_rotation", "balanced"} else 35,
        "defense": 85 if permission == "paused" else 65 if permission == "limited" else 25,
    }
    risks = []
    if crowd_market.get("risk_state") in {"danger", "fragile", "unwind"}:
        risks.append(str(crowd_market.get("risk_label") or "全市场拥挤风险升高"))
    if style_spread is not None and style_spread >= 1.5:
        risks.append(f"风格分化 {style_spread:.2f} 个百分点")
    if quality.get("status") != "valid":
        risks.extend(quality.get("warnings") or [])

    return {
        "state": state,
        "label": label,
        "permission": permission,
        "reason": reason,
        "as_of": as_of,
        "metrics": {
            "market_return_pct": _round(market_return),
            "breadth_pct": _round(breadth * 100 if breadth is not None and abs(breadth) <= 1.5 else breadth),
            "stock_breadth_pct": _round(stock_breadth),
            "direction_score_pct": _round(direction * 100 if direction is not None and abs(direction) <= 1.5 else direction),
            "activity_percentile": _round(activity, 1),
            "amount_ratio_20": _round(amount_ratio),
            "temperature": _round(temperature_value, 1),
            "temperature_date": _date_key(temp.get("date")),
            "breadth_change_pp": _round((breadth_delta or 0) * 100),
            "direction_change_pp": _round((direction_delta or 0) * 100),
            "activity_change": _round(activity_delta),
            "style_spread_pct": _round(style_spread),
        },
        "style": {
            "groups": style_rows,
            "leader": style_leader,
            "laggard": style_laggard,
            "spread_pct": _round(style_spread),
        },
        "indices": _index_metrics(temperature, as_of),
        "strategy_fit": strategy_fit,
        "risks": _unique(risks),
        "evidence": _unique(
            [
                f"全市场涨跌宽度 {breadth * 100:+.1f}%" if breadth is not None else None,
                f"市场收益 {market_return:+.2f}%" if market_return is not None else None,
                f"成交活跃度历史 {activity:.0f} 分位" if activity is not None else None,
                f"{style_leader['label']}相对占优" if style_leader else None,
            ]
        ),
    }


CHANGE_LABELS = {
    "first_strength": "首次转强",
    "accelerating": "加速确认",
    "high_stall": "高位钝化",
    "momentum_divergence": "动能背离",
    "structure_break": "结构破坏",
    "risk_release": "风险解除",
    "stable": "延续观察",
}


def build_change_signal(flow_row: dict, crowd_row: dict | None = None) -> dict:
    """Return level, first derivative and second derivative for one industry."""
    price, price_delta, price_accel = _delta(flow_row, "price_change_pct")
    breadth, breadth_delta, breadth_accel = _delta(flow_row, "breadth")
    direction, direction_delta, direction_accel = _delta(flow_row, "direction_score")
    activity, activity_delta, activity_accel = _delta(flow_row, "activity_pctile")
    if activity is None:
        activity, activity_delta, activity_accel = _delta(flow_row, "amount_pctile")

    values = _series(flow_row)
    previous = values[-2] if len(values) >= 2 else {}
    previous_price = _num(previous.get("price_change_pct"))
    previous_breadth = _num(previous.get("breadth"))
    previous_direction = _num(previous.get("direction_score"))
    current_risk = str((crowd_row or flow_row).get("risk_state") or "normal")
    previous_risk = str(previous.get("risk_level") or previous.get("risk_state") or "normal")

    flags: list[str] = []
    if (
        (price or 0) > 0
        and (breadth or 0) >= 0.15
        and (direction or 0) > 0
        and (
            (previous_price is not None and previous_price <= 0)
            or (previous_breadth is not None and previous_breadth <= 0)
            or (previous_direction is not None and previous_direction <= 0)
        )
    ):
        flags.append("first_strength")
    positive_levels = sum(
        value is not None and value > 0
        for value in (price, breadth, direction)
    )
    if (
        positive_levels >= 2
        and sum(value is not None and value > 0 for value in (price_delta, breadth_delta, direction_delta)) >= 2
        and (price_accel or 0) >= 0
    ):
        flags.append("accelerating")
    if (activity or 0) >= 75 and (price or 0) <= 0.3 and (breadth_delta or 0) < 0:
        flags.append("high_stall")
    if (price or 0) > 0 and ((breadth_delta or 0) <= -0.15 or (direction_delta or 0) <= -0.2):
        flags.append("momentum_divergence")
    structure_crossed = (
        (previous_price is not None and previous_price >= 0)
        or (previous_breadth is not None and previous_breadth > -0.25)
        or (previous_direction is not None and previous_direction >= 0)
    )
    structure_accelerated = (
        (price_delta or 0) <= -0.8
        and (breadth_delta or 0) <= -0.12
        and (direction_delta or 0) <= -0.12
    )
    if (
        (price or 0) < 0
        and (breadth or 0) <= -0.25
        and (direction or 0) < 0
        and (structure_crossed or structure_accelerated)
    ):
        flags.append("structure_break")
    if previous_risk not in {"", "normal", "none"} and current_risk in {"", "normal", "none"}:
        flags.append("risk_release")
    if not flags:
        flags.append("stable")

    priority = {
        "structure_break": 6,
        "high_stall": 5,
        "momentum_divergence": 4,
        "first_strength": 3,
        "accelerating": 2,
        "risk_release": 1,
        "stable": 0,
    }
    primary = max(flags, key=lambda value: priority[value])
    positive = sum(key in flags for key in ("first_strength", "accelerating", "risk_release"))
    negative = sum(key in flags for key in ("high_stall", "momentum_divergence", "structure_break"))
    score = _clamp(50 + positive * 15 - negative * 18 + (price_delta or 0) * 2 + (breadth_delta or 0) * 20)
    evidence = []
    if price is not None:
        evidence.append(f"价格结果 {price:+.2f}% · 日变 {price_delta or 0:+.2f}")
    if breadth is not None:
        evidence.append(f"涨跌宽度 {breadth * 100:+.1f}% · 日变 {(breadth_delta or 0) * 100:+.1f}pp")
    if direction is not None:
        evidence.append(f"方向成交 {direction * 100:+.1f}% · 日变 {(direction_delta or 0) * 100:+.1f}pp")
    if activity is not None:
        evidence.append(f"成交活跃度 {activity:.0f} 分位 · 日变 {activity_delta or 0:+.1f}")
    return {
        "state": primary,
        "label": CHANGE_LABELS[primary],
        "flags": flags,
        "score": round(score, 1),
        "direction": "positive" if positive > negative else "negative" if negative > positive else "neutral",
        "metrics": {
            "price": {"value": _round(price), "delta": _round(price_delta), "acceleration": _round(price_accel)},
            "breadth": {"value": _round((breadth or 0) * 100), "delta": _round((breadth_delta or 0) * 100), "acceleration": _round((breadth_accel or 0) * 100)},
            "direction": {"value": _round((direction or 0) * 100), "delta": _round((direction_delta or 0) * 100), "acceleration": _round((direction_accel or 0) * 100)},
            "activity": {"value": _round(activity, 1), "delta": _round(activity_delta), "acceleration": _round(activity_accel)},
        },
        "evidence": evidence,
    }


def _load_context(
    data_dir: str,
    scheme: str,
    expected_date: str | None = None,
) -> dict:
    if scheme not in SCHEME_SUFFIX:
        raise ValueError("scheme must be sw, ths or sw3")
    suffix = SCHEME_SUFFIX[scheme]
    payloads = {
        "capital_flow": _read_json(os.path.join(data_dir, f"capital_flow_v2{suffix}.json")),
        "market_cap": _read_json(os.path.join(data_dir, f"market_cap_v2{suffix}.json")),
        "crowding": _read_json(os.path.join(data_dir, f"crowding{suffix}.json")),
        "margin": _read_json(os.path.join(data_dir, f"margin_financing{suffix}.json")),
        "etf": _read_json(os.path.join(data_dir, f"etf_recommend_{scheme}.json")) if scheme in {"ths", "sw3"} else {},
        "temperature": _read_json(os.path.join(data_dir, "market_temperature.json")),
    }
    quality = _source_quality(payloads, expected_date)
    maps = {
        name: _industry_map(payload)
        for name, payload in payloads.items()
        if name in {"capital_flow", "market_cap", "crowding", "margin"}
    }
    # Never let a source newer than the common core date leak into an older
    # decision snapshot.  Prior-close low-frequency evidence remains usable
    # and is still disclosed by source status.
    if quality["sources"].get("margin", {}).get("status") == "future_relative_to_common_date":
        maps["margin"] = {}
    return {"data_dir": data_dir, "scheme": scheme, "payloads": payloads, "quality": quality, "maps": maps}


def _etf_map(payload: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in list(payload.get("top") or []) + list(payload.get("industries") or []):
        if not isinstance(row, dict):
            continue
        industry = str(row.get("industry") or "").strip()
        if not industry:
            continue
        current = result.get(industry)
        score = _num(row.get("opportunity_score", row.get("score")), 0) or 0
        current_score = _num((current or {}).get("opportunity_score", (current or {}).get("score")), -1) or -1
        if current is None or score > current_score:
            result[industry] = row
    return result


def _radar_map(data_dir: str, scheme: str) -> tuple[dict[str, dict], dict]:
    try:
        from sentiment_radar import build_sentiment_radar

        payload = build_sentiment_radar(data_dir, scheme)
    except Exception:
        return {}, {}
    return _industry_map(payload), payload


def _opportunity_map(data_dir: str, scheme: str, period: str, mode: str) -> tuple[dict[str, dict], dict]:
    try:
        from opportunity_summary import build_opportunity_summary

        payload = build_opportunity_summary(data_dir, scheme=scheme, period=period, mode=mode)
    except Exception:
        return {}, {}
    rows = {
        str(row.get("industry")): row
        for row in payload.get("candidates") or []
        if isinstance(row, dict) and row.get("industry")
    }
    return rows, payload


def _structure(flow_row: dict, cap_row: dict, crowd_row: dict) -> dict:
    top_stocks = [item for item in flow_row.get("top_stocks") or [] if isinstance(item, dict)]
    top1 = _num((top_stocks[0] if top_stocks else {}).get("share"))
    top5 = _num(flow_row.get("internal_top5", flow_row.get("internal_top5_share")))
    advance = _num(flow_row.get("advance_ratio"))
    if advance is None:
        breadth = _num(flow_row.get("breadth"))
        advance = (breadth + 1) / 2 if breadth is not None else None
    active_breadth = _num(flow_row.get("active_breadth"))
    effective = _num(flow_row.get("effective_participation_ratio"))
    price = _num(flow_row.get("price_change_pct"), 0) or 0

    if advance is not None and advance >= 0.65 and (top5 is None or top5 <= 0.60):
        state, label = "broad_resonance", "全面共振"
    elif advance is not None and advance >= 0.50 and price > 0:
        state, label = "leader_diffusion", "龙头扩散"
    elif (top5 or 0) >= 0.75 and price > 0:
        state, label = "leader_solo", "龙头独舞"
    elif advance is not None and advance <= 0.25:
        state, label = "broad_retreat", "广泛退潮"
    elif price > 0 and (top5 or 0) >= 0.65:
        state, label = "narrow_follow", "少数补涨"
    else:
        state, label = "mixed", "结构混合"

    leader_names = _unique(
        f"{item.get('name') or item.get('code')} {(_num(item.get('return_pct')) or 0):+.2f}%"
        for item in top_stocks[:5]
    )
    cap_top1 = _num(cap_row.get("top1_stock_share_pct"))
    evidence = _unique(
        [
            f"上涨家数占比 {(advance or 0) * 100:.1f}%" if advance is not None else None,
            f"成交前五集中度 {(top5 or 0) * 100:.1f}%" if top5 is not None else None,
            f"市值第一大权重占比 {cap_top1:.1f}%" if cap_top1 is not None else None,
            f"有效活跃参与比例 {(effective or 0) * 100:.1f}%" if effective is not None else None,
        ]
    )
    return {
        "state": state,
        "label": label,
        "advance_ratio_pct": _round((advance or 0) * 100) if advance is not None else None,
        "active_breadth_pct": _round((active_breadth or 0) * 100) if active_breadth is not None else None,
        "top1_amount_share_pct": _round((top1 or 0) * 100) if top1 is not None else None,
        "top5_amount_share_pct": _round((top5 or 0) * 100) if top5 is not None else None,
        "cap_top1_share_pct": _round(cap_top1),
        "effective_participation_pct": _round((effective or 0) * 100) if effective is not None else None,
        "leaders": top_stocks[:5],
        "leader_labels": leader_names,
        "leader_persistence_5d": _round(flow_row.get("leader_overlap_5d")),
        "evidence": evidence,
    }


def _driver_domains(flow_row: dict, cap_row: dict, crowd_row: dict, margin_row: dict, etf_row: dict) -> dict:
    domains: list[dict] = []

    def add(key: str, label: str, direction: str, strength: float, evidence: str, available: bool = True):
        domains.append({
            "key": key,
            "label": label,
            "direction": direction if available else "unavailable",
            "strength": round(_clamp(strength), 1) if available else None,
            "evidence": evidence,
            "available": available,
        })

    direction = _num(flow_row.get("direction_score"))
    activity = _num(flow_row.get("activity_pctile", flow_row.get("amount_pctile")))
    add(
        "participation", "方向成交",
        "positive" if (direction or 0) > 0.12 else "negative" if (direction or 0) < -0.12 else "neutral",
        abs(direction or 0) * 100,
        f"方向成交 {(direction or 0) * 100:+.1f}%，活跃度 {activity:.0f} 分位" if activity is not None else f"方向成交 {(direction or 0) * 100:+.1f}%",
        direction is not None,
    )
    cap_return = _num(cap_row.get("relative_1d_pct", cap_row.get("return_1d_pct")))
    cap_breadth = _num(cap_row.get("stock_breadth_1d_pct", cap_row.get("stock_breadth_pct")))
    add(
        "price_structure", "价格与市值结构",
        "positive" if (cap_return or 0) > 0.2 else "negative" if (cap_return or 0) < -0.2 else "neutral",
        min(100, abs(cap_return or 0) * 20 + abs((cap_breadth or 50) - 50)),
        f"相对收益 {cap_return:+.2f}%，上涨宽度 {cap_breadth:.1f}%" if cap_return is not None and cap_breadth is not None else "市值结构数据不可用",
        cap_return is not None,
    )
    margin_change = _num(margin_row.get("financing_change_pct"))
    add(
        "leverage", "融资需求",
        "positive" if (margin_change or 0) > 0.3 else "negative" if (margin_change or 0) < -0.3 else "neutral",
        min(100, abs(margin_change or 0) * 15),
        f"融资余额日变 {margin_change:+.2f}%" if margin_change is not None else "融资数据不可用",
        margin_change is not None,
    )
    etf = etf_row.get("etf") if isinstance(etf_row.get("etf"), dict) else {}
    share_change = _num(etf_row.get("share_change_pct", etf.get("share_change_pct")))
    etf_amount = _num(etf.get("amount_today"))
    add(
        "etf_demand", "ETF申赎与载体",
        "positive" if (share_change or 0) > 0.2 else "negative" if (share_change or 0) < -0.2 else "neutral",
        min(100, abs(share_change or 0) * 12 + (20 if etf_amount and etf_amount >= 1e8 else 0)),
        f"ETF份额变化 {share_change:+.2f}%" if share_change is not None else "ETF份额变化不可用",
        bool(etf_row),
    )
    direct = _num(crowd_row.get("direct_position_score"))
    risk = _num(crowd_row.get("external_fragility_score"))
    add(
        "positioning", "直接持仓与脆弱性",
        "negative" if (risk or 0) >= 65 else "positive" if (direct or 0) >= 60 else "neutral",
        max(direct or 0, risk or 0),
        f"直接需求 {direct:.0f} · 外部脆弱性 {risk:.0f}" if direct is not None and risk is not None else "外部持仓证据不完整",
        direct is not None or risk is not None,
    )
    ranked = sorted(
        [item for item in domains if item["available"] and item["direction"] != "neutral"],
        key=lambda item: item["strength"] or 0,
        reverse=True,
    )
    primary = ranked[0] if ranked else {
        "key": "unconfirmed", "label": "驱动未确认", "direction": "neutral", "strength": 0,
        "evidence": "现有量价、结构与直接需求尚未形成一致归因", "available": True,
    }
    return {
        "primary": primary,
        "domains": domains,
        "agreement_count": sum(item["direction"] == primary["direction"] for item in domains if item["available"]),
        "event_source": {
            "status": "manual_journal",
            "label": "事件源未自动接入",
            "note": "自动结论仅使用可审计的量价、申赎、两融与结构数据；政策、公告和产业催化可在页面事件日志中人工记录。",
        },
    }


def _tradability(flow_row: dict, crowd_row: dict, etf_row: dict, quality: dict) -> dict:
    etf = etf_row.get("etf") if isinstance(etf_row.get("etf"), dict) else {}
    amount = _num(etf.get("amount_today"))
    avg_amount = _num(etf.get("avg_amount_20d"))
    volatility = _num(etf.get("volatility_20d"))
    spread = _num(crowd_row.get("spread_bps"))
    amihud = _num(flow_row.get("amihud_1e10_pctile", crowd_row.get("amihud_1e10_pctile")))
    impact = _num(flow_row.get("market_impact_score", crowd_row.get("market_impact_score")))
    liquid_flag = bool(etf_row.get("liquid", etf.get("liquid", False)))
    carrier_available = bool(etf.get("code"))
    amount_score = 0
    reference_amount = avg_amount or amount
    if reference_amount:
        amount_score = _clamp(20 + math.log10(max(reference_amount, 1e6) / 1e6) * 20)
    liquidity_score = _clamp(
        amount_score * 0.55
        + (100 - (amihud if amihud is not None else 60)) * 0.25
        + (80 if liquid_flag else 20) * 0.20
    ) if carrier_available else _clamp(100 - (amihud or 75))
    estimated_cost = (spread / 2 if spread is not None else 3.0) + (amihud or 50) / 12 + (impact or 30) / 20
    risk_state = str(crowd_row.get("risk_state") or "normal")
    if quality.get("status") != "valid":
        mode, mode_label = "observation_only", "数据未对齐，仅观察"
    elif not carrier_available:
        mode, mode_label = "carrier_missing", "缺少同口径ETF载体"
    elif risk_state in {"danger", "fragile", "unwind"} or liquidity_score < 45:
        mode, mode_label = "avoid_chase", "避免追价，等待承接"
    elif volatility is not None and volatility >= 3.5:
        mode, mode_label = "watch_vwap", "高波动，观察VWAP承接"
    else:
        mode, mode_label = "next_open_confirm", "T+1开盘后确认执行"
    return {
        "can_trade": mode in {"next_open_confirm", "watch_vwap"},
        "mode": mode,
        "mode_label": mode_label,
        "liquidity_score": round(liquidity_score, 1),
        "estimated_cost_bps": round(estimated_cost, 1),
        "cost_kind": "基于价差与冲击分位的研究估算，非真实成交成本",
        "carrier": {
            "code": etf.get("code"),
            "name": etf.get("name"),
            "amount_today": amount,
            "avg_amount_20d": avg_amount,
            "volatility_20d_pct": volatility,
            "liquid": liquid_flag,
        },
        "microstructure": {
            "spread_bps": _round(spread),
            "amihud_percentile": _round(amihud, 1),
            "market_impact_score": _round(impact, 1),
            "opening_gap": None,
            "vwap": None,
            "note": "开盘跳空与实时VWAP需在T+1盘中确认，日线数据不做伪造。",
        },
    }


def _probability_trust(
    radar_row: dict,
    lane: str,
    regime: dict | None = None,
) -> dict:
    block_name = "top" if lane == "risk" else "bottom"
    block = radar_row.get(block_name) or {}
    available = bool(block.get("probability_available"))
    samples = int(_num(block.get("samples"), 0) or 0)
    dates = int(_num(block.get("independent_dates"), 0) or 0)
    ci = block.get("ci_t5") if isinstance(block.get("ci_t5"), list) else []
    ci_width = (
        _num(ci[1]) - _num(ci[0])
        if len(ci) >= 2 and _num(ci[0]) is not None and _num(ci[1]) is not None
        else None
    )
    horizons = []
    for item in block.get("horizons") or []:
        if not isinstance(item, dict):
            continue
        horizons.append({
            "horizon": item.get("horizon"),
            "probability": _round(item.get("probability"), 1),
            "base_probability": _round(item.get("base_probability"), 1),
            "lift": _round(item.get("lift"), 2),
        })
    if not available:
        level, label, score = "insufficient", "概率不可用", 0
        reason = block.get("unavailable_reason") or "当前条件未触发或样本不足"
    else:
        score = _clamp(min(45, samples / 10) + min(30, dates / 4) + (25 if ci_width is not None and ci_width <= 15 else 10))
        if samples >= 500 and dates >= 120 and ci_width is not None and ci_width <= 15:
            level, label = "stable", "样本较充分"
        elif samples >= 100 and dates >= 50:
            level, label = "research", "研究样本"
        else:
            level, label = "low", "小样本"
        reason = "历史条件频率，需结合当前环境与执行成本，不代表确定性"
    return {
        "available": available,
        "kind": block.get("probability_kind") or "historical_condition_frequency",
        "level": level,
        "label": label,
        "trust_score": round(score, 1),
        "samples": samples,
        "independent_dates": dates,
        "ci_t5": ci or None,
        "ci_width": _round(ci_width),
        "horizons": horizons,
        "avg_excess_t5": _round(block.get("avg_excess_t5"), 3),
        "condition": block.get("condition_label"),
        "reason": reason,
        "precision_note": "页面最多显示一位小数；样本不足时不输出伪精确概率。",
        "regime_conditioning": {
            "current_regime": (regime or {}).get("label"),
            "supported": False,
            "note": "当前历史条件频率尚未按市场环境完成walk-forward分层校准，因此不对原频率做主观加减。",
        },
    }


def _trade_plan(
    lane: str,
    change: dict,
    structure: dict,
    tradability: dict,
    probability: dict,
    opportunity: dict,
    radar_row: dict,
    regime: dict,
    quality: dict,
) -> dict:
    opportunity_direction = lane != "risk"
    if lane == "risk":
        trigger = "已有持仓先检查龙头、宽度和方向成交是否同步破坏"
        confirmation = "价格转负、上涨宽度低于35%、方向成交转负三项中至少两项"
        action = "风险确认后降低暴露；未确认前不把高位预警当作必然下跌"
    else:
        trigger = "T+1开盘后不追高，等待价格维持正向且方向成交继续改善"
        confirmation = "价格、上涨宽度、方向成交三项至少两项为正，且结构不是龙头独舞"
        action = "满足触发与确认后才进入试仓观察，T1至T5逐日复核"
    invalidations = []
    invalidations.extend(opportunity.get("invalidation") or [])
    if opportunity_direction:
        invalidations.extend([
            "行业价格结果转负且上涨宽度跌破35%",
            "龙头跌破当日VWAP并带动方向成交转负",
            "成交快速集中到少数个股且跟随股停止扩散",
        ])
    else:
        invalidations.extend([
            "上涨宽度重新超过60%且方向成交转正",
            "龙头创新高并出现ETF份额持续净增",
        ])
    if tradability["mode"] == "observation_only":
        action = "数据日期未对齐，只记录观察，不生成执行建议"
    if regime.get("permission") == "paused" and lane != "risk":
        action = "市场总开关暂停，只观察反转证据，不主动追涨"
    risk_level = str((radar_row.get("danger") or {}).get("level") or "normal")
    if tradability["can_trade"] and quality.get("status") == "valid" and regime.get("permission") == "allowed" and probability.get("level") in {"stable", "research"}:
        position = "验证仓"
    elif tradability["can_trade"] and regime.get("permission") != "paused":
        position = "观察仓"
    else:
        position = "不建仓"
    return {
        "signal_time": "信号日收盘",
        "entry_window": "T+1开盘后至前30分钟VWAP确认",
        "horizon": "T1–T5短线观察",
        "trigger": trigger,
        "confirmation": confirmation,
        "action": action,
        "invalidation": _unique(invalidations, 5),
        "risk_upgrade": "若拥挤风险升级、流动性下降或结构破坏，立即降级为仅观察",
        "position_band": position,
        "max_risk": "单笔风险预算以1R为上限；页面不替代个人仓位约束",
        "danger_level": risk_level,
        "expected_value": {
            "status": "research_only" if probability.get("available") else "unavailable",
            "avg_excess_t5": probability.get("avg_excess_t5"),
            "cost_bps": tradability.get("estimated_cost_bps"),
            "note": "仅在概率和成本均可用时作研究比较，不输出确定收益。",
        },
    }


def _transmission(industry: str, drivers: dict, structure: dict, tradability: dict, regime: dict) -> dict:
    primary = drivers.get("primary") or {}
    nodes = [
        {"id": "driver", "type": "driver", "label": primary.get("label") or "驱动未确认", "status": primary.get("direction") or "neutral"},
        {"id": "industry", "type": "industry", "label": industry, "status": structure.get("state")},
    ]
    edges = [{"from": "driver", "to": "industry", "label": "数据归因"}]
    for index, leader in enumerate(structure.get("leaders") or []):
        node_id = f"leader-{index}"
        nodes.append({"id": node_id, "type": "stock", "label": leader.get("name") or leader.get("code"), "status": "leader"})
        edges.append({"from": "industry", "to": node_id, "label": "内部扩散" if index else "核心带动"})
    carrier = tradability.get("carrier") or {}
    if carrier.get("code"):
        nodes.append({"id": "etf", "type": "etf", "label": carrier.get("name") or carrier.get("code"), "status": tradability.get("mode")})
        edges.append({"from": "industry", "to": "etf", "label": "可交易载体"})
    nodes.append({"id": "market", "type": "market", "label": regime.get("label"), "status": regime.get("permission")})
    edges.append({"from": "market", "to": "industry", "label": "环境许可"})
    return {
        "stage": structure.get("state"),
        "stage_label": structure.get("label"),
        "nodes": nodes,
        "edges": edges,
        "lead_lag": {
            "status": "proxy",
            "leader_overlap_5d": structure.get("leader_persistence_5d"),
            "note": "当前以龙头持续度和扩散宽度作为领先关系代理；没有分钟级快照时不宣称精确领先分钟数。",
        },
    }


def _alert_id(as_of: str | None, alert_type: str, industry: str | None) -> str:
    raw = f"{as_of or 'undated'}|{alert_type}|{industry or 'market'}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:14]


def _card_alerts(card: dict, as_of: str | None) -> list[dict]:
    industry = card["industry"]
    alerts = []
    change = card["change"]
    mapping = {
        "first_strength": ("high", "首次转强", "价格、宽度与方向成交由弱转强"),
        "accelerating": ("medium", "加速确认", "至少两个变化域同步改善"),
        "high_stall": ("high", "高位钝化", "成交仍热但价格响应和宽度下降"),
        "momentum_divergence": ("high", "动能背离", "价格与参与宽度出现背离"),
        "structure_break": ("critical", "结构破坏", "价格、宽度与方向成交共同转弱"),
        "risk_release": ("medium", "风险解除", "此前风险状态已回到正常"),
    }
    for flag in change.get("flags") or []:
        if flag not in mapping:
            continue
        priority, title, message = mapping[flag]
        alerts.append({
            "id": _alert_id(as_of, flag, industry),
            "type": flag,
            "priority": priority,
            "industry": industry,
            "title": title,
            "message": message,
            "evidence": change.get("evidence") or [],
            "action": card["trade_plan"]["action"],
            "cooldown_hours": 6,
        })
    danger = (card.get("radar") or {}).get("danger") or {}
    if danger.get("level") in {"danger", "warning", "opportunity"}:
        alert_type = f"radar_{danger.get('level')}"
        priority = "critical" if danger.get("level") == "danger" else "high"
        alerts.append({
            "id": _alert_id(as_of, alert_type, industry),
            "type": alert_type,
            "priority": priority,
            "industry": industry,
            "title": danger.get("label"),
            "message": danger.get("message"),
            "evidence": card["probability"].get("horizons") or [],
            "action": card["trade_plan"]["action"],
            "cooldown_hours": 6,
        })
    return alerts


def _build_card(
    industry: str,
    context: dict,
    radar_row: dict,
    opportunity: dict,
    etf_row: dict,
    regime: dict,
) -> dict:
    maps = context["maps"]
    flow_row = maps.get("capital_flow", {}).get(industry, {})
    cap_row = maps.get("market_cap", {}).get(industry, {})
    crowd_row = maps.get("crowding", {}).get(industry, {})
    margin_row = maps.get("margin", {}).get(industry, {})
    change = build_change_signal(flow_row, crowd_row)
    structure = _structure(flow_row, cap_row, crowd_row)
    drivers = _driver_domains(flow_row, cap_row, crowd_row, margin_row, etf_row)

    danger = (radar_row.get("danger") or {}).get("level")
    if danger in {"danger", "warning"} or change["state"] in {"structure_break", "high_stall", "momentum_divergence"}:
        lane = "risk"
    elif (
        structure.get("state") != "broad_retreat"
        and not opportunity.get("vetoes")
        and (
            danger == "opportunity"
            or change["state"] in {"first_strength", "accelerating", "risk_release"}
            or opportunity.get("lane") == "confirmed"
        )
    ):
        lane = "opportunity"
    else:
        lane = "watch"
    probability = _probability_trust(radar_row, lane, regime)
    tradability = _tradability(flow_row, crowd_row, etf_row, context["quality"])
    plan = _trade_plan(lane, change, structure, tradability, probability, opportunity, radar_row, regime, context["quality"])
    risk_score = _num(crowd_row.get("crowding_score"), 50) or 50
    radar_rank = _num(radar_row.get("radar_rank"), 0) or 0
    opportunity_score = _num(opportunity.get("score"), 0) or 0
    etf_score = _num(etf_row.get("opportunity_score", etf_row.get("score")), 0) or 0
    lane_bonus = 22 if lane in {"risk", "opportunity"} else 0
    attention = _clamp(
        lane_bonus + change["score"] * 0.35 + min(25, radar_rank / 20) + opportunity_score * 0.12 + etf_score * 0.10 + abs(risk_score - 50) * 0.12
    )
    summary = {
        "risk": f"{change['label']}，{structure['label']}；先处理风险再寻找机会",
        "opportunity": f"{change['label']}，{structure['label']}；等待T+1确认",
        "watch": f"{change['label']}，{structure['label']}；证据尚未闭环",
    }[lane]
    card = {
        "industry": industry,
        "lane": lane,
        "attention_score": round(attention, 1),
        "summary": summary,
        "change": change,
        "drivers": drivers,
        "structure": structure,
        "tradability": tradability,
        "probability": probability,
        "trade_plan": plan,
        "transmission": _transmission(industry, drivers, structure, tradability, regime),
        "radar": {
            "sentiment_position": radar_row.get("sentiment_position"),
            "sentiment_label": radar_row.get("sentiment_label"),
            "danger": radar_row.get("danger") or {},
            "bottom_stage": (radar_row.get("bottom") or {}).get("stage"),
            "top_stage": (radar_row.get("top") or {}).get("stage"),
        },
        "opportunity": {
            "lane": opportunity.get("lane"),
            "confirmation_count": opportunity.get("confirmation_count"),
            "conflicts": opportunity.get("conflicts") or [],
            "risks": opportunity.get("risks") or [],
            "vetoes": opportunity.get("vetoes") or [],
        },
        "source_date": _date_key(flow_row.get("date")) or context["quality"].get("as_of"),
    }
    card["alerts"] = _card_alerts(card, context["quality"].get("as_of"))
    return card


def build_decision_center(
    data_dir: str,
    scheme: str = "sw3",
    period: str = "month",
    mode: str = "daily",
    expected_date: str | None = None,
) -> dict:
    """Build the unified market/industry decision centre payload."""
    context = _load_context(os.fspath(data_dir), scheme, expected_date)
    payloads = context["payloads"]
    regime = build_market_regime(
        payloads["capital_flow"], payloads["market_cap"], payloads["crowding"], payloads["temperature"], context["quality"]
    )
    radar_rows, radar_payload = _radar_map(data_dir, scheme)
    opportunities, opportunity_payload = _opportunity_map(data_dir, scheme, period, mode)
    etfs = (
        {}
        if context["quality"]["sources"].get("etf", {}).get("status")
        == "future_relative_to_common_date"
        else _etf_map(payloads["etf"])
    )
    industries = sorted(
        set(context["maps"].get("capital_flow", {}))
        | set(radar_rows)
        | set(opportunities)
        | set(etfs)
    )
    all_cards = [
        _build_card(
            industry,
            context,
            radar_rows.get(industry, {}),
            opportunities.get(industry, {}),
            etfs.get(industry, {}),
            regime,
        )
        for industry in industries
    ]
    lane_order = {"risk": 0, "opportunity": 1, "watch": 2}
    all_cards.sort(key=lambda item: (lane_order[item["lane"]], -item["attention_score"], item["industry"]))
    risk_cards = [card for card in all_cards if card["lane"] == "risk"]
    opportunity_cards = [card for card in all_cards if card["lane"] == "opportunity"]
    watch_cards = [card for card in all_cards if card["lane"] == "watch"]
    # The workbench must show both sides of the decision.  A broad sell-off
    # can otherwise fill every slot with risks and hide the few repair setups.
    battle_cards = risk_cards[:4] + opportunity_cards[:4]
    if len(battle_cards) < 8:
        battle_cards.extend(watch_cards[: 8 - len(battle_cards)])
    if not battle_cards:
        battle_cards = all_cards[:8]
    alerts = []
    # Alerts are an attention queue, not a second copy of the entire industry
    # table.  Rank first and retain the most consequential state changes.
    alert_candidates = sorted(
        all_cards,
        key=lambda card: (-card["attention_score"], card["industry"]),
    )[:24]
    for card in alert_candidates:
        alerts.extend(card.get("alerts") or [])
    priority_order = {"critical": 0, "high": 1, "medium": 2, "info": 3}
    alerts.sort(key=lambda item: (priority_order.get(item["priority"], 9), -next((card["attention_score"] for card in all_cards if card["industry"] == item["industry"]), 0)))
    if context["quality"]["status"] != "valid":
        alerts.insert(0, {
            "id": _alert_id(context["quality"].get("as_of"), "data_quality", None),
            "type": "data_quality",
            "priority": "critical" if context["quality"]["status"] == "invalid" else "high",
            "industry": None,
            "title": "数据日期未完全对齐",
            "message": "跨模块结论降级为观察，先更新数据再执行",
            "evidence": context["quality"].get("warnings") or [],
            "action": "打开设置更新数据，直到核心来源处于同一交易日",
            "cooldown_hours": 6,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scheme": scheme,
        "scheme_label": SCHEME_LABELS[scheme],
        "period": period,
        "mode": mode,
        "as_of": context["quality"].get("as_of"),
        "quality": context["quality"],
        "regime": regime,
        "battle_cards": battle_cards,
        "alerts": alerts[:30],
        "catalog": [
            {
                "industry": card["industry"],
                "lane": card["lane"],
                "attention_score": card["attention_score"],
                "change_state": card["change"]["state"],
                "change_label": card["change"]["label"],
                "structure_label": card["structure"]["label"],
                "crowding_risk": card["trade_plan"]["danger_level"],
            }
            for card in all_cards
        ],
        "counts": {
            "industries": len(all_cards),
            "risk": sum(card["lane"] == "risk" for card in all_cards),
            "opportunity": sum(card["lane"] == "opportunity" for card in all_cards),
            "watch": sum(card["lane"] == "watch" for card in all_cards),
            "alerts": min(len(alerts), 30),
            "alerts_before_attention_cap": sum(
                len(card.get("alerts") or []) for card in all_cards
            ),
        },
        "research": {
            "radar_quality": radar_payload.get("quality") or {},
            "opportunity_quality": opportunity_payload.get("quality") or {},
            "probability_rule": "概率只来自雷达历史条件频率；注意力分仅用于排序，绝不解释为上涨概率。",
            "event_rule": "未接入可审计事件源时不自动编造政策、公告或产业催化。",
        },
        "methodology": {
            "items": [
                "市场环境总开关",
                "统一变化引擎",
                "可审计驱动归因",
                "板块内部结构",
                "可交易性与成本",
                "触发确认失效",
                "组合相关与重叠",
                "去重提醒",
                "概率可信度",
            ],
            "signal_time": "信号日收盘",
            "execution_time": "T+1开盘后确认",
            "horizon": "T1–T5",
        },
    }


def build_industry_decision(
    data_dir: str,
    industry: str,
    scheme: str = "sw3",
    period: str = "month",
    mode: str = "daily",
    expected_date: str | None = None,
) -> dict:
    industry = str(industry or "").strip()
    if not industry:
        raise ValueError("industry is required")
    context = _load_context(os.fspath(data_dir), scheme, expected_date)
    payloads = context["payloads"]
    regime = build_market_regime(payloads["capital_flow"], payloads["market_cap"], payloads["crowding"], payloads["temperature"], context["quality"])
    radar_rows, _ = _radar_map(data_dir, scheme)
    opportunities, _ = _opportunity_map(data_dir, scheme, period, mode)
    etfs = (
        {}
        if context["quality"]["sources"].get("etf", {}).get("status")
        == "future_relative_to_common_date"
        else _etf_map(payloads["etf"])
    )
    known = set(context["maps"].get("capital_flow", {})) | set(radar_rows) | set(opportunities) | set(etfs)
    if industry not in known:
        raise LookupError(f"未找到行业：{industry}")
    card = _build_card(industry, context, radar_rows.get(industry, {}), opportunities.get(industry, {}), etfs.get(industry, {}), regime)
    return {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "scheme": scheme,
        "as_of": context["quality"].get("as_of"),
        "quality": context["quality"],
        "regime": regime,
        "card": card,
    }


def _return_series(row: dict) -> dict[str, float]:
    result = {}
    for item in _series(row):
        day = _date_key(item.get("date"))
        value = _num(item.get("price_change_pct"))
        if day and value is not None:
            result[day] = value
    return result


def _correlation(left: dict[str, float], right: dict[str, float]) -> tuple[float | None, int]:
    dates = sorted(set(left) & set(right))
    if len(dates) < 5:
        return None, len(dates)
    x = [left[day] for day in dates]
    y = [right[day] for day in dates]
    x_mean, y_mean = statistics.mean(x), statistics.mean(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - x_mean) ** 2 for a in x) * sum((b - y_mean) ** 2 for b in y))
    return (numerator / denominator if denominator else None), len(dates)


def build_portfolio_risk(
    data_dir: str,
    industries: Iterable[str],
    scheme: str = "sw3",
) -> dict:
    """Evaluate hidden overlap for an equal-weight research watchlist."""
    context = _load_context(os.fspath(data_dir), scheme)
    flow_map = context["maps"].get("capital_flow", {})
    crowd_map = context["maps"].get("crowding", {})
    cap_map = context["maps"].get("market_cap", {})
    selected = _unique(industries, 20)
    selected = [industry for industry in selected if industry in flow_map]
    if not selected:
        return {
            "scheme": scheme,
            "as_of": context["quality"].get("as_of"),
            "industries": [],
            "status": "empty",
            "label": "请选择行业",
            "warnings": ["至少选择两个行业后才能评估相关性与隐性重叠"],
        }
    pairs = []
    correlations = []
    overlaps = []
    for i, left_name in enumerate(selected):
        left = flow_map[left_name]
        left_codes = set(left.get("top_stock_codes") or [])
        for right_name in selected[i + 1:]:
            right = flow_map[right_name]
            corr, samples = _correlation(_return_series(left), _return_series(right))
            shared = sorted(left_codes & set(right.get("top_stock_codes") or []))
            pair = {
                "left": left_name,
                "right": right_name,
                "correlation": _round(corr, 3),
                "samples": samples,
                "shared_leaders": shared,
            }
            pairs.append(pair)
            if corr is not None:
                correlations.append(corr)
            if shared:
                overlaps.append(pair)
    average_corr = statistics.mean(correlations) if correlations else None
    max_pair = max((pair for pair in pairs if pair["correlation"] is not None), key=lambda item: item["correlation"], default=None)
    high_risk = [
        industry for industry in selected
        if str(crowd_map.get(industry, {}).get("risk_state") or "normal") in {"danger", "fragile", "unwind"}
    ]
    style_states = {}
    for industry in selected:
        label = str(cap_map.get(industry, {}).get("state_label") or "结构未知")
        style_states[label] = style_states.get(label, 0) + 1
    hhi = 10000 / len(selected)
    warnings = []
    if average_corr is not None and average_corr >= 0.65:
        warnings.append(f"平均相关性 {average_corr:.2f}，名义分散但可能属于同一笔风格交易")
    if max_pair and (max_pair["correlation"] or 0) >= 0.80:
        warnings.append(f"{max_pair['left']} 与 {max_pair['right']} 高度同步")
    if overlaps:
        warnings.append("部分行业共享成交龙头，存在隐性个股集中")
    if len(high_risk) >= max(1, len(selected) // 2):
        warnings.append("一半以上观察行业处于去拥挤或脆弱状态")
    if len(selected) == 1:
        warnings.append("单一行业无法形成分散，组合风险等同该行业风险")
    risk_score = _clamp(
        25
        + (max(0, average_corr or 0) * 35)
        + min(20, len(overlaps) * 5)
        + len(high_risk) / len(selected) * 25
        + (15 if len(selected) == 1 else 0)
    )
    level = "high" if risk_score >= 70 else "medium" if risk_score >= 45 else "low"
    return {
        "scheme": scheme,
        "as_of": context["quality"].get("as_of"),
        "status": "ok",
        "level": level,
        "label": {"high": "组合暴露偏高", "medium": "组合风险中等", "low": "组合相对分散"}[level],
        "risk_score": round(risk_score, 1),
        "industries": selected,
        "weights": {industry: round(100 / len(selected), 2) for industry in selected},
        "concentration_hhi": round(hhi, 1),
        "effective_positions": len(selected),
        "average_correlation": _round(average_corr, 3),
        "max_correlation_pair": max_pair,
        "pairs": sorted(pairs, key=lambda item: item["correlation"] if item["correlation"] is not None else -2, reverse=True),
        "leader_overlaps": overlaps,
        "crowding_risk_industries": high_risk,
        "structure_exposure": style_states,
        "warnings": warnings or ["暂未发现显著的相关性、共享龙头或拥挤重叠"],
        "methodology": {
            "weights": "用户未输入权重时按等权研究观察篮子计算",
            "correlation": "使用各行业已有日度价格结果序列，至少5个共同样本",
            "overlap": "比较各行业成交额领先个股代码",
            "limitation": "不是账户级持仓或实时风险系统，不替代券商持仓数据。",
        },
    }
