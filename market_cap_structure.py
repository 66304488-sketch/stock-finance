#!/usr/bin/env python3
"""行业市值结构 v2。

市值是边际成交价格对全部股份的重新标价，不是进入市场的现金。本模块复用
``daily_market_cap`` 和 ``stock_details``，把市场变化拆成规模、贡献、广度、
大小盘风格和权重迁移。上游市值目前使用一份股本快照回算历史，因此默认口径
明确标记为 ``current_share_price_proxy``。

如果用户另外提供 ``market_cap_point_in_time_shares.json``，本模块会在覆盖充分
的日期上额外计算点时总股本口径的价格/供给/样本效应；没有可靠历史股本时
``supply_effect_bp`` 和自由流通市值保持 ``None``，绝不以当前股本伪造。

可选文件格式::

    {
      "schema_version": 1,
      "source": "authoritative source description",
      "updated_at": "2026-07-23T18:00:00",
      "total_shares": {"000001": {"20260722": 19405918198}},
      "circulating_a_shares": {"000001": {"20260722": 19405752198}}
    }

所有历史分位仅使用当前日期之前的数据。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
import os
from statistics import fmean
from typing import Any, Iterable

from runtime_paths import data_path, resource_path


MODEL_VERSION = "market-cap-structure-v2"
OUTPUT_DATES = 60
PERCENTILE_WINDOW = 60
MIN_PERCENTILE_HISTORY = 10
POINT_IN_TIME_MIN_COVERAGE = 0.95
RETURN_NOISE_PCT = 0.05

NEUTRAL_COMPANY_ACTION_KEYWORDS = (
    "送股", "送转", "转增", "拆股", "缩股", "股份拆细", "股份合并",
)

# These actions add economically real shares.  Even when their share/price
# ratios happen to resemble a split, they must remain in Supply rather than be
# neutralized into Price.
REAL_SUPPLY_COMPANY_ACTION_KEYWORDS = (
    "增发", "非公开发行", "向特定对象发行", "配股", "行权", "转股",
    "H股上市", "H股发行", "发行H股", "H股首发",
)

SCHEME_LABELS = {
    "sw": "申万一级",
    "ths": "同花顺",
    "sw3": "申万三级",
}


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _number(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if _finite(value) else None


def _safe_div(numerator: Any, denominator: Any) -> float | None:
    if not _finite(numerator) or not _finite(denominator):
        return None
    denominator = float(denominator)
    return float(numerator) / denominator if denominator != 0 else None


def _mean(values: Iterable[Any]) -> float | None:
    usable = [float(value) for value in values if _finite(value)]
    return fmean(usable) if usable else None


def _date_label(date: str) -> str:
    return f"{int(date[4:6])}月{int(date[6:8])}日"


def _full_date_label(date: str) -> str:
    return f"{date[:4]}年{int(date[4:6])}月{int(date[6:8])}日"


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def causal_percentiles(
    values: Iterable[Any],
    *,
    window: int = PERCENTILE_WINDOW,
    min_history: int = MIN_PERCENTILE_HISTORY,
) -> list[float | None]:
    """Return an ECDF percentile against prior observations only."""
    if window < 1:
        raise ValueError("window must be positive")
    if min_history < 1 or min_history > window:
        raise ValueError("min_history must be between 1 and window")
    history: list[float] = []
    result: list[float | None] = []
    for raw in values:
        value = float(raw) if _finite(raw) else None
        prior = sorted(history[-window:])
        if value is None or len(prior) < min_history:
            result.append(None)
        else:
            result.append(round(
                bisect_right(prior, value) / len(prior) * 100, 1))
        if value is not None:
            history.append(value)
    return result


def effective_count(weights: Iterable[Any]) -> float | None:
    """Concentration-adjusted count: ``(sum w)^2 / sum(w^2)``."""
    usable = [
        float(value) for value in weights
        if _finite(value) and float(value) > 0
    ]
    if not usable:
        return None
    total = sum(usable)
    squared = sum(value * value for value in usable)
    return total * total / squared if squared > 0 else None


def load_point_in_time_shares(path: str | None = None) -> dict[str, Any]:
    """Load an optional, explicitly sourced point-in-time share history."""
    path = path or data_path("market_cap_point_in_time_shares.json")
    if not os.path.isfile(path):
        return {
            "available": False,
            "path": path,
            "source": None,
            "updated_at": None,
            "total_shares": {},
            "circulating_a_shares": {},
            "events": {},
            "warning": "未提供可靠点时股本，供给效应与自由流通市值不可计算",
        }
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "available": False,
            "path": path,
            "source": None,
            "updated_at": None,
            "total_shares": {},
            "circulating_a_shares": {},
            "events": {},
            "warning": f"点时股本文件不可用: {exc}",
        }
    total = payload.get("total_shares")
    circulating = payload.get("circulating_a_shares")
    source = payload.get("source")
    if not isinstance(total, dict) or not source:
        return {
            "available": False,
            "path": path,
            "source": source,
            "updated_at": payload.get("updated_at"),
            "total_shares": {},
            "circulating_a_shares": {},
            "events": {},
            "warning": "点时股本缺少 source 或 total_shares，未作为实际口径使用",
        }
    return {
        "available": True,
        "path": path,
        "source": str(source),
        "updated_at": payload.get("updated_at"),
        "total_shares": total,
        "circulating_a_shares": (
            circulating if isinstance(circulating, dict) else {}
        ),
        "events": (
            payload.get("events")
            if isinstance(payload.get("events"), dict) else {}
        ),
        "query_start": payload.get("query_start"),
        "query_end": payload.get("query_end"),
        "lookback_start": payload.get("lookback_start"),
        "lookback_end": payload.get("lookback_end"),
        "calibrated_unit": payload.get("calibrated_unit"),
        "calibrated_unit_counts": payload.get(
            "calibrated_unit_counts") or {},
        "status_counts": payload.get("status_counts") or {},
        "circulating_disclaimer": payload.get(
            "circulating_disclaimer")
        or "circulating_share_proxy_not_csi_free_float",
        "warning": None,
    }


def _share_at(mapping: dict[str, Any], code: str, date: str) -> float | None:
    history = mapping.get(str(code).zfill(6))
    if not isinstance(history, dict):
        return None
    value = history.get(date)
    return float(value) if _finite(value) and float(value) > 0 else None


def _company_action_reason(
    events: dict[str, Any],
    code: str,
    previous_date: str,
    current_date: str,
) -> str:
    """Join all disclosed action reasons inside a return window."""
    rows = events.get(str(code).zfill(6), [])
    if not isinstance(rows, list):
        return ""
    reasons = [
        str(item.get("reason") or "").strip()
        for item in rows
        if (
            isinstance(item, dict)
            and previous_date < str(item.get("date") or "") <= current_date
            and str(item.get("reason") or "").strip()
        )
    ]
    return "；".join(dict.fromkeys(reasons))


def _is_neutral_company_action(
    share_ratio: float,
    price_ratio: float,
    market_cap_ratio: float,
    event_reason: str,
) -> bool:
    """Classify only unit-changing actions as economically neutral.

    A disclosed real-supply reason is a hard veto.  This prevents rights
    issues, placements, option exercise, conversions and H-share listings from
    being moved into Price merely because the endpoint ratios look split-like.
    """
    reason = str(event_reason or "").upper()
    real_supply_action = (
        any(
            keyword.upper() in reason
            for keyword in REAL_SUPPLY_COMPANY_ACTION_KEYWORDS
        )
        or (
            "H股" in reason
            and ("上市" in reason or "发行" in reason)
        )
    )
    if real_supply_action:
        return False
    explicitly_neutral_action = any(
        keyword.upper() in reason
        for keyword in NEUTRAL_COMPANY_ACTION_KEYWORDS
    )
    return (
        abs(share_ratio - 1) >= 0.05
        and (share_ratio - 1) * (price_ratio - 1) < 0
        and abs(price_ratio - 1) >= 0.15
        and (
            explicitly_neutral_action
            or abs(market_cap_ratio - 1) <= 0.15
        )
    )


def _stock_price_return_pct(
    current: dict[str, Any],
    previous: dict[str, Any],
    share_history: dict[str, Any] | None,
) -> float:
    """Price return adjusted by the same neutral-action rule as attribution."""
    current_price = float(current["price"])
    previous_price = float(previous["price"])
    raw_return = (current_price / previous_price - 1) * 100
    if not share_history or not share_history.get("available"):
        return raw_return
    current_date = str(current.get("date") or "")
    previous_date = str(previous.get("date") or "")
    if not previous_date or not current_date or previous_date >= current_date:
        return raw_return
    shares = share_history.get("total_shares") or {}
    previous_shares = _share_at(
        shares, previous["code"], previous_date)
    current_shares = _share_at(shares, current["code"], current_date)
    if not previous_shares or not current_shares:
        return raw_return
    share_ratio = current_shares / previous_shares
    price_ratio = current_price / previous_price
    market_cap_ratio = share_ratio * price_ratio
    reason = _company_action_reason(
        share_history.get("events") or {},
        current["code"],
        previous_date,
        current_date,
    )
    if not _is_neutral_company_action(
        share_ratio, price_ratio, market_cap_ratio, reason
    ):
        return raw_return
    return (market_cap_ratio - 1) * 100


def _recent_dates(db: Any, scheme: str, n_dates: int) -> list[str]:
    rows = db.conn.execute(
        "SELECT DISTINCT date FROM daily_market_cap "
        "WHERE scheme=? ORDER BY date DESC LIMIT ?",
        [scheme, n_dates],
    ).fetchall()
    return [row[0] for row in reversed(rows)]


def _load_aggregate_rows(
    db: Any,
    scheme: str,
    dates: list[str],
) -> tuple[
    dict[str, float],
    dict[str, int],
    dict[str, dict[str, float]],
    list[str],
]:
    if not dates:
        return {}, {}, {}, []
    placeholders = ",".join("?" for _ in dates)
    rows = db.conn.execute(
        "SELECT industry,date,mcap,stock_count,is_total "
        "FROM daily_market_cap "
        f"WHERE scheme=? AND date IN ({placeholders})",
        [scheme] + dates,
    ).fetchall()
    totals: dict[str, float] = {}
    expected: dict[str, int] = {}
    industry_values: dict[str, dict[str, float]] = defaultdict(dict)
    names: set[str] = set()
    for industry, date, mcap, stock_count, is_total in rows:
        value = float(mcap or 0)
        if is_total:
            totals[date] = value
            expected[date] = int(stock_count or 0)
        else:
            industry_values[industry][date] = value
            names.add(industry)
    for date in dates:
        if date not in totals:
            totals[date] = sum(
                values.get(date, 0.0) for values in industry_values.values())
    return totals, expected, dict(industry_values), sorted(names)


def _load_stock_rows(
    db: Any,
    scheme: str,
    dates: list[str],
) -> tuple[
    dict[str, dict[str, dict[str, Any]]],
    dict[str, dict[str, list[dict[str, Any]]]],
]:
    by_date: dict[str, dict[str, dict[str, Any]]] = {
        date: {} for date in dates
    }
    by_industry: dict[str, dict[str, list[dict[str, Any]]]] = {
        date: defaultdict(list) for date in dates
    }
    if not dates:
        return by_date, by_industry
    placeholders = ",".join("?" for _ in dates)
    rows = db.conn.execute(
        "SELECT date,industry,code,name,price,change_pct,mcap "
        "FROM stock_details WHERE direction='market_cap' AND period='daily' "
        f"AND scheme=? AND date IN ({placeholders})",
        [scheme] + dates,
    ).fetchall()
    for date, industry, code, name, price, change_pct, mcap in rows:
        code = str(code).zfill(6)
        record = {
            "date": date,
            "industry": industry,
            "code": code,
            "name": name or code,
            "price": float(price or 0),
            "change_pct": (
                float(change_pct) if _finite(change_pct) else None
            ),
            "mcap": float(mcap or 0),
        }
        by_date.setdefault(date, {})[code] = record
        by_industry.setdefault(date, defaultdict(list))[industry].append(
            record)
    return by_date, by_industry


def _rebuild_point_in_time_market_caps(
    dates: list[str],
    totals: dict[str, float],
    industry_values: dict[str, dict[str, float]],
    stocks_by_date: dict[str, dict[str, dict[str, Any]]],
    stocks_by_industry: dict[
        str, dict[str, list[dict[str, Any]]]
    ],
    share_history: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Rebuild total/industry values with dated shares and explicit fallback.

    Missing point-in-time shares retain the upstream snapshot-share value. The
    coverage and fallback share are recorded per date so the hybrid is never
    represented as a fully actual market cap.
    """
    history = share_history.get("total_shares") or {}
    circulating = share_history.get("circulating_a_shares") or {}
    metadata: dict[str, dict[str, Any]] = {}
    for date in dates:
        rows = stocks_by_date.get(date, {})
        original_total = float(totals.get(date, 0.0))
        ordered_codes = sorted(rows)
        detailed_proxy_total = math.fsum(
            float(rows[code].get("mcap") or 0) for code in ordered_codes)
        covered_proxy = 0.0
        circulating_covered_proxy = 0.0
        circulating_total = 0.0
        for code, row in rows.items():
            proxy_mcap = float(row.get("mcap") or 0)
            row["proxy_mcap"] = proxy_mcap
            shares = _share_at(history, code, date)
            price = float(row.get("price") or 0)
            if shares and price > 0:
                row["mcap"] = price * shares
                row["mcap_basis"] = "point_in_time_total_shares"
                row["point_in_time_total_shares"] = shares
                covered_proxy += proxy_mcap
            else:
                row["mcap_basis"] = "current_share_price_proxy"
                row["point_in_time_total_shares"] = None
            circulating_shares = _share_at(circulating, code, date)
            if circulating_shares and price > 0:
                row["circulating_mcap_proxy"] = (
                    price * circulating_shares)
                circulating_total += row["circulating_mcap_proxy"]
                circulating_covered_proxy += proxy_mcap
            else:
                row["circulating_mcap_proxy"] = None

        detail_coverage = (
            detailed_proxy_total / original_total
            if original_total > 0 else 0.0
        )
        point_coverage = (
            covered_proxy / original_total if original_total > 0 else 0.0
        )
        circulating_coverage = (
            circulating_covered_proxy / original_total
            if original_total > 0 else 0.0
        )
        # Only replace aggregate values when individual rows explain at least
        # 95% of the upstream total. Any unrepresented tail remains an explicit
        # current-share residual instead of being silently discarded.
        if detail_coverage >= POINT_IN_TIME_MIN_COVERAGE and rows:
            for industry, values in industry_values.items():
                industry_rows = stocks_by_industry.get(
                    date, {}).get(industry, [])
                proxy_detail = math.fsum(
                    float(row.get("proxy_mcap") or 0)
                    for row in industry_rows
                )
                rebuilt_detail = math.fsum(
                    float(row.get("mcap") or 0)
                    for row in industry_rows
                )
                upstream = float(values.get(date, 0.0))
                unrepresented_proxy = upstream - proxy_detail
                rebuilt = rebuilt_detail + unrepresented_proxy
                values[date] = rebuilt
            # Sum in a deterministic stock-code order so classification alone
            # cannot create a few-yuan floating-point difference in the
            # all-market total. Preserve any aggregate tail not represented by
            # stock detail rows as an explicit proxy residual.
            rebuilt_stock_total = math.fsum(
                float(rows[code].get("mcap") or 0)
                for code in ordered_codes
            )
            totals[date] = (
                rebuilt_stock_total
                + (original_total - detailed_proxy_total)
            )
        if point_coverage >= 0.999:
            measure_kind = "point_in_time_total_shares"
        elif point_coverage > 0:
            measure_kind = "hybrid_point_in_time_and_current_share_proxy"
        else:
            measure_kind = "current_share_price_proxy"
        metadata[date] = {
            "measure_kind": measure_kind,
            "point_in_time_coverage_pct": _number(
                point_coverage * 100, 2),
            "fallback_ratio": _number(
                max(0.0, 1 - point_coverage), 4),
            "stock_detail_coverage_pct": _number(
                detail_coverage * 100, 2),
            "circulating_share_coverage_pct": _number(
                circulating_coverage * 100, 2),
            "circulating_mcap_proxy": (
                _number(circulating_total, 0)
                if circulating_coverage >= POINT_IN_TIME_MIN_COVERAGE
                else None
            ),
        }
    return metadata


def _stock_window_metrics(
    current: list[dict[str, Any]],
    base: list[dict[str, Any]] | None,
    *,
    fallback_daily: bool = False,
    share_history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute neutral-action-adjusted common-sample return and breadth."""
    if not current:
        return {
            "cap_weighted_return_pct": None,
            "equal_weight_return_pct": None,
            "breadth_pct": None,
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "common_count": 0,
            "current_count": 0,
            "base_count": len(base or []),
            "coverage_pct": 0.0,
        }
    base_by_code = {
        item["code"]: item for item in (base or [])
        if _finite(item.get("price")) and float(item["price"]) > 0
    }
    observations: list[tuple[dict[str, Any], float, float]] = []
    for row in current:
        prior = base_by_code.get(row["code"])
        if (
            prior
            and _finite(row.get("price"))
            and float(row["price"]) > 0
        ):
            price_return = _stock_price_return_pct(
                row, prior, share_history)
            observations.append((row, price_return, float(prior["mcap"])))
        elif fallback_daily and _finite(row.get("change_pct")):
            price_return = float(row["change_pct"])
            denominator = 1 + price_return / 100
            prior_mcap = (
                float(row["mcap"]) / denominator
                if denominator > 0 else float(row["mcap"])
            )
            observations.append((row, price_return, prior_mcap))
    if not observations:
        return {
            "cap_weighted_return_pct": None,
            "equal_weight_return_pct": None,
            "breadth_pct": None,
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "common_count": 0,
            "current_count": len(current),
            "base_count": len(base or []),
            "coverage_pct": 0.0,
        }
    returns = [item[1] for item in observations]
    weights = [max(item[2], 0.0) for item in observations]
    total_weight = sum(weights)
    weighted = (
        sum(value * weight for value, weight in zip(returns, weights))
        / total_weight
        if total_weight > 0 else None
    )
    up = sum(value > RETURN_NOISE_PCT for value in returns)
    down = sum(value < -RETURN_NOISE_PCT for value in returns)
    flat = len(returns) - up - down
    return {
        "cap_weighted_return_pct": _number(weighted, 4),
        "equal_weight_return_pct": _number(_mean(returns), 4),
        "breadth_pct": _number(up / len(returns) * 100, 2),
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "common_count": len(observations),
        "current_count": len(current),
        "base_count": len(base or []),
        "coverage_pct": _number(
            len(observations) / max(len(current), len(base or []), 1) * 100,
            2,
        ),
    }


def _style_metrics(
    current_by_code: dict[str, dict[str, Any]],
    base_by_code: dict[str, dict[str, Any]],
    total_mcap: float,
    share_history: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    ranked = sorted(
        base_by_code.values(),
        key=lambda row: float(row.get("mcap") or 0),
        reverse=True,
    )
    buckets = {
        "top100": ranked[:100],
        "next400": ranked[100:500],
        "rest": ranked[500:],
    }
    output: dict[str, dict[str, Any]] = {}
    for key, base_rows in buckets.items():
        codes = {row["code"] for row in base_rows}
        current_rows = [
            current_by_code[code] for code in codes
            if code in current_by_code
        ]
        metrics = _stock_window_metrics(
            current_rows,
            base_rows,
            share_history=share_history,
        )
        current_value = sum(
            float(row.get("mcap") or 0) for row in current_rows)
        output[key] = {
            "return_pct": metrics["equal_weight_return_pct"],
            "equal_weight_return_pct": metrics[
                "equal_weight_return_pct"],
            "cap_weighted_return_pct": metrics[
                "cap_weighted_return_pct"],
            "breadth_pct": metrics["breadth_pct"],
            "mcap_share_pct": _number(
                current_value / total_mcap * 100, 2)
            if total_mcap > 0 else None,
            "count": len(current_rows),
            "base_count": len(base_rows),
            "coverage_pct": metrics["coverage_pct"],
        }
    return output


def _proxy_effects(
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    previous_total: float,
    market_return_pct: float | None,
) -> dict[str, Any]:
    """Decompose the existing snapshot-share proxy without calling it supply."""
    if previous_total <= 0 or not current or not previous:
        return {
            "effect_measure_kind": "unavailable",
            "price_effect_bp": None,
            "supply_effect_bp": None,
            "share_snapshot_effect_bp": None,
            "company_action_effect_bp": None,
            "company_action_effect_included_in_price": False,
            "company_action_count": 0,
            "company_actions": [],
            "universe_effect_bp": None,
            "residual_effect_bp": None,
            "effect_coverage_pct": 0.0,
        }
    common = set(current) & set(previous)
    entered = set(current) - set(previous)
    exited = set(previous) - set(current)
    price_value = 0.0
    snapshot_value = 0.0
    common_base = 0.0
    for code in common:
        before = previous[code]
        after = current[code]
        prior_price = float(before.get("price") or 0)
        current_price = float(after.get("price") or 0)
        prior_mcap = float(before.get("mcap") or 0)
        if prior_price <= 0 or current_price <= 0:
            continue
        inferred_prior_shares = prior_mcap / prior_price
        price_component = (
            current_price - prior_price) * inferred_prior_shares
        observed_change = float(after.get("mcap") or 0) - prior_mcap
        price_value += price_component
        snapshot_value += observed_change - price_component
        common_base += prior_mcap
    universe_value = (
        sum(float(current[code].get("mcap") or 0) for code in entered)
        - sum(float(previous[code].get("mcap") or 0) for code in exited)
    )
    price_bp = price_value / previous_total * 10000
    snapshot_bp = snapshot_value / previous_total * 10000
    universe_bp = universe_value / previous_total * 10000
    market_bp = (
        float(market_return_pct) * 100
        if _finite(market_return_pct) else None
    )
    explained = price_bp + snapshot_bp + universe_bp
    return {
        "effect_measure_kind": "current_share_price_proxy",
        "price_effect_bp": _number(price_bp, 2),
        "supply_effect_bp": None,
        "share_snapshot_effect_bp": _number(snapshot_bp, 2),
        "company_action_effect_bp": None,
        "company_action_effect_included_in_price": False,
        "company_action_count": 0,
        "company_actions": [],
        "universe_effect_bp": _number(universe_bp, 2),
        "residual_effect_bp": _number(
            market_bp - explained, 2) if market_bp is not None else None,
        "effect_coverage_pct": _number(
            common_base / previous_total * 100, 2),
    }


def _point_in_time_effects(
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
    current_date: str,
    previous_date: str,
    share_history: dict[str, Any],
    current_total: float,
    previous_total: float,
) -> dict[str, Any] | None:
    """Return a dated-share decomposition with corporate-action adjustment."""
    if not share_history.get("available") or not current or not previous:
        return None
    shares = share_history.get("total_shares") or {}
    events = share_history.get("events") or {}
    covered_proxy = 0.0
    common = set(current) & set(previous)
    entered = set(current) - set(previous)
    exited = set(previous) - set(current)
    price_value = 0.0
    raw_price_value = 0.0
    supply_value = 0.0
    snapshot_value = 0.0
    company_action_adjustment = 0.0
    company_actions: list[dict[str, Any]] = []
    for code in common:
        before = previous[code]
        after = current[code]
        prior_shares = _share_at(shares, code, previous_date)
        current_shares = _share_at(shares, code, current_date)
        prior_price = float(before.get("price") or 0)
        current_price = float(after.get("price") or 0)
        if (
            prior_shares and current_shares
            and prior_price > 0 and current_price > 0
        ):
            covered_proxy += float(before.get("mcap") or 0)
            raw_price = prior_shares * (current_price - prior_price)
            raw_supply = current_price * (
                current_shares - prior_shares)
            raw_price_value += raw_price
            share_ratio = current_shares / prior_shares
            price_ratio = current_price / prior_price
            mcap_ratio = (
                current_price * current_shares
                / (prior_price * prior_shares)
            )
            event_reason = _company_action_reason(
                events, code, previous_date, current_date)
            neutral_action = _is_neutral_company_action(
                share_ratio,
                price_ratio,
                mcap_ratio,
                event_reason,
            )
            if neutral_action:
                # A split/bonus issue changes units, not economic supply. Move
                # the mechanical offset into adjusted price so a 3-for-1 event
                # cannot appear as a -67% collapse.
                price_value += raw_price + raw_supply
                company_action_adjustment += raw_supply
                company_actions.append({
                    "code": code,
                    "name": after.get("name") or before.get("name") or code,
                    "date": current_date,
                    "share_ratio": _number(share_ratio, 4),
                    "price_ratio": _number(price_ratio, 4),
                    "market_cap_ratio": _number(mcap_ratio, 4),
                    "reason": event_reason or None,
                    "method": "neutral_divisor_adjustment",
                })
            else:
                price_value += raw_price
                supply_value += raw_supply
        elif prior_price > 0 and current_price > 0:
            inferred_prior_shares = (
                float(before.get("mcap") or 0) / prior_price)
            proxy_price = (
                current_price - prior_price) * inferred_prior_shares
            observed_change = (
                float(after.get("mcap") or 0)
                - float(before.get("mcap") or 0)
            )
            price_value += proxy_price
            raw_price_value += proxy_price
            snapshot_value += observed_change - proxy_price
    coverage = (
        covered_proxy / previous_total
        if previous_total > 0 else 0.0
    )
    if coverage < POINT_IN_TIME_MIN_COVERAGE or previous_total <= 0:
        return {
            "available": False,
            "coverage_pct": _number(coverage * 100, 2),
            "reason": "点时总股本市值覆盖不足95%",
        }
    universe_value = (
        sum(float(current[code].get("mcap") or 0) for code in entered)
        - sum(float(previous[code].get("mcap") or 0) for code in exited)
    )
    actual_change = current_total - previous_total
    residual_value = (
        actual_change - price_value - supply_value
        - snapshot_value - universe_value
    )
    measure_kind = (
        "point_in_time_total_shares"
        if coverage >= 0.999
        else "hybrid_point_in_time_and_current_share_proxy"
    )
    return {
        "available": True,
        "effect_measure_kind": measure_kind,
        "point_in_time_total_mcap": _number(current_total, 0),
        "point_in_time_market_return_pct": _number(
            actual_change / previous_total * 100, 4),
        "price_effect_bp": _number(
            price_value / previous_total * 10000, 2),
        "raw_price_effect_bp": _number(
            raw_price_value / previous_total * 10000, 2),
        "supply_effect_bp": _number(
            supply_value / previous_total * 10000, 2),
        "share_snapshot_effect_bp": _number(
            snapshot_value / previous_total * 10000, 2),
        "company_action_effect_bp": _number(
            company_action_adjustment / previous_total * 10000, 2),
        "company_action_effect_included_in_price": True,
        "company_action_count": len(company_actions),
        "company_actions": company_actions[:20],
        "universe_effect_bp": _number(
            universe_value / previous_total * 10000, 2),
        "residual_effect_bp": _number(
            residual_value / previous_total * 10000, 2),
        "effect_coverage_pct": _number(coverage * 100, 2),
        "coverage_pct": _number(coverage * 100, 2),
        "reason": None,
    }


def _circulating_snapshot(
    rows: dict[str, dict[str, Any]],
    date: str,
    share_history: dict[str, Any],
) -> dict[str, Any]:
    """F022 A-share circulation proxy; explicitly not CSI free float."""
    mapping = share_history.get("circulating_a_shares") or {}
    proxy_total = sum(float(row.get("mcap") or 0) for row in rows.values())
    covered_proxy = 0.0
    circulating_mcap = 0.0
    for code, row in rows.items():
        shares = _share_at(mapping, code, date)
        if not shares or float(row.get("price") or 0) <= 0:
            continue
        covered_proxy += float(row.get("mcap") or 0)
        circulating_mcap += float(row["price"]) * shares
    coverage = (
        covered_proxy / proxy_total if proxy_total > 0 else 0.0)
    return {
        "available": coverage >= POINT_IN_TIME_MIN_COVERAGE,
        "coverage_pct": _number(coverage * 100, 2),
        "circulating_mcap_proxy": (
            _number(circulating_mcap, 0)
            if coverage >= POINT_IN_TIME_MIN_COVERAGE else None
        ),
        "disclaimer": "circulating_share_proxy_not_csi_free_float",
    }


def _concentration(
    values: list[float],
    total: float,
) -> dict[str, Any]:
    positive = sorted(
        [float(value) for value in values if _finite(value) and value > 0],
        reverse=True,
    )
    weights = sorted(
        [float(value) / total * 100 for value in values if value > 0],
        reverse=True,
    ) if total > 0 else []
    hhi = sum(weight * weight for weight in weights)
    return {
        "top3_share_pct": _number(sum(weights[:3]), 2),
        "top5_share_pct": _number(sum(weights[:5]), 2),
        "hhi": _number(hhi, 2),
        "hhi_10000": _number(hhi, 2),
        "effective_industries": _number(
            10000 / hhi, 2) if hhi > 0 else None,
        "positive_contribution_top3_share_pct": _number(
            sum(positive[:3]) / sum(positive) * 100, 2
        ) if positive else None,
        "positive_contribution_top5_share_pct": _number(
            sum(positive[:5]) / sum(positive) * 100, 2
        ) if positive else None,
    }


def _market_state(row: dict[str, Any]) -> tuple[str, str]:
    market_return = float(row.get("market_return_pct") or 0)
    equal_return = row.get("equal_weight_return_pct")
    breadth = row.get("stock_breadth_pct")
    style = row.get("style") or {}
    top = (style.get("top100") or {}).get("equal_weight_return_pct")
    rest = (style.get("rest") or {}).get("equal_weight_return_pct")
    contribution_concentration = row.get(
        "positive_contribution_top3_share_pct")
    if abs(market_return) < RETURN_NOISE_PCT:
        if (
            _finite(equal_return)
            and float(equal_return) > market_return + 0.50
            and _finite(breadth)
            and float(breadth) >= 60
        ):
            return "diffusion_below_index", "指数横盘·个股扩散"
        return "balanced", "结构平衡"
    if market_return > 0:
        if (
            _finite(rest) and _finite(top)
            and float(rest) > float(top) + 0.50
            and _finite(breadth) and float(breadth) >= 60
        ):
            return "small_mid_diffusion", "中小盘扩散"
        if (
            (_finite(breadth) and float(breadth) < 45)
            or (
                _finite(contribution_concentration)
                and float(contribution_concentration) >= 70
            )
        ):
            return "narrow_large_cap_advance", "少数权重拉升"
        if _finite(breadth) and float(breadth) >= 60:
            return "broad_expansion", "全面扩张"
        return "selective_advance", "结构性上涨"
    if (
        _finite(equal_return)
        and float(equal_return) > market_return + 0.50
        and _finite(breadth)
        and float(breadth) >= 45
    ):
        return "large_cap_drag", "权重拖累·个股较强"
    if _finite(breadth) and float(breadth) <= 40:
        return "broad_contraction", "全面收缩"
    return "selective_decline", "结构性下跌"


def _industry_state(row: dict[str, Any]) -> tuple[str, str]:
    relative = row.get("relative_1d_pct")
    relative_5d = row.get("relative_5d_pct")
    breadth = row.get("stock_breadth_pct")
    top5 = row.get("top5_stock_share_pct")
    if not _finite(relative):
        return "insufficient", "样本积累"
    relative = float(relative)
    if relative > RETURN_NOISE_PCT:
        if _finite(relative_5d) and float(relative_5d) < 0:
            return "recovering", "相对修复"
        if (
            (_finite(breadth) and float(breadth) < 45)
            or (_finite(top5) and float(top5) >= 60)
        ):
            return "leading_narrow", "少数权重领涨"
        if _finite(breadth) and float(breadth) >= 60:
            return "leading_broad", "广泛领涨"
        return "leading_selective", "结构性领涨"
    if relative < -RETURN_NOISE_PCT:
        if _finite(relative_5d) and float(relative_5d) > 0:
            return "fading", "领先衰减"
        if _finite(breadth) and float(breadth) <= 40:
            return "lagging_broad", "广泛落后"
        return "lagging_selective", "结构性落后"
    return "market_like", "跟随市场"


def _classification_quality(
    scheme: str,
    latest_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    codes = set(latest_rows)
    direct = len(codes)
    fallback = 0
    source = "industry_stock_map.xlsx"
    if scheme == "sw":
        try:
            from kline_cache import load_industry_map
            mapping = load_industry_map(list(codes))
        except (OSError, ValueError, KeyError):
            mapping = {}
        direct = sum(code in mapping for code in codes)
        fallback = len(codes) - direct
    elif scheme == "ths":
        source = "industry_map_ths.json + 申万一级回退"
        try:
            with open(
                resource_path("industry_map_ths.json"), encoding="utf-8"
            ) as handle:
                mapping = json.load(handle)
        except (OSError, json.JSONDecodeError):
            mapping = {}
        direct = sum(code in mapping for code in codes)
        fallback = len(codes) - direct
    elif scheme == "sw3":
        source = "industry_taxonomy.json + 申万一级回退"
        try:
            with open(
                resource_path("industry_taxonomy.json"), encoding="utf-8"
            ) as handle:
                taxonomy = json.load(handle).get("stocks", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            taxonomy = {}
        direct = sum(
            bool((taxonomy.get(code) or {}).get("sw_level3"))
            for code in codes
        )
        fallback = len(codes) - direct
    total = len(codes)
    return {
        "total": total,
        "direct": direct,
        "fallback": fallback,
        "direct_ratio": _number(direct / total, 4) if total else None,
        "fallback_ratio": _number(fallback / total, 4) if total else None,
        "source": source,
    }


def _current_share_snapshot_quality(trade_date: str) -> dict[str, Any]:
    try:
        with open(data_path("stock_shares.json"), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {
            "available": False,
            "version": None,
            "snapshot_asof": None,
            "stale": True,
            "legacy_invalid": False,
        }
    version = payload.get("version") if isinstance(payload, dict) else None
    updated_at = (
        payload.get("updated_at")
        if isinstance(payload, dict) else None
    )
    try:
        snapshot_asof = datetime.fromisoformat(updated_at).strftime(
            "%Y%m%d")
    except (TypeError, ValueError):
        snapshot_asof = None
    return {
        "available": (
            version == 3
            and isinstance(payload.get("total_shares"), dict)
        ),
        "version": version,
        "snapshot_asof": snapshot_asof,
        "updated_at": updated_at,
        "stale": (
            snapshot_asof is None or snapshot_asof < trade_date),
        "legacy_invalid": version != 3,
        "total_shares_field": 73 if version == 3 else None,
        "circulating_shares_field": 72 if version == 3 else None,
    }


def _add_causal_percentiles(
    rows: list[dict[str, Any]],
    mapping: dict[str, str],
    *,
    min_history: int,
    window: int,
) -> None:
    for output_field, source_field in mapping.items():
        percentiles = causal_percentiles(
            [row.get(source_field) for row in rows],
            window=window,
            min_history=min_history,
        )
        for row, percentile in zip(rows, percentiles):
            row[output_field] = percentile


def _window_return(
    current: float,
    base: float | None,
) -> float | None:
    return (
        (current / float(base) - 1) * 100
        if _finite(base) and float(base) > 0 else None
    )


def build_market_cap_structure_payload(
    db: Any,
    scheme: str = "sw",
    *,
    n_dates: int = OUTPUT_DATES,
    percentile_window: int = PERCENTILE_WINDOW,
    min_history: int = MIN_PERCENTILE_HISTORY,
    share_history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a causal market-cap structure payload from the existing DB."""
    if scheme not in SCHEME_LABELS:
        raise ValueError("scheme must be sw, ths or sw3")
    dates = _recent_dates(db, scheme, n_dates)
    if not dates:
        return {
            "schema_version": 2,
            "model_version": MODEL_VERSION,
            "scheme": scheme,
            "scheme_label": SCHEME_LABELS[scheme],
            "trade_date": None,
            "dates": [],
            "market": {"latest": {}, "series": []},
            "industries": [],
            "data_quality": {
                "measure_kind": "current_share_price_proxy",
                "history_days": 0,
                "warnings": ["市值数据库暂无可用日期"],
            },
        }
    (
        totals,
        expected_counts,
        aggregate_industries,
        industry_names,
    ) = _load_aggregate_rows(db, scheme, dates)
    stocks_by_date, stocks_by_industry = _load_stock_rows(
        db, scheme, dates)
    share_history = (
        share_history
        if share_history is not None
        else load_point_in_time_shares()
    )
    basis_by_date = _rebuild_point_in_time_market_caps(
        dates,
        totals,
        aggregate_industries,
        stocks_by_date,
        stocks_by_industry,
        share_history,
    )

    market_series: list[dict[str, Any]] = []
    industry_daily: dict[str, list[dict[str, Any]]] = {
        industry: [] for industry in industry_names
    }
    date_index = {date: index for index, date in enumerate(dates)}

    # Industry rows first; market contribution concentration depends on them.
    for industry in industry_names:
        values = aggregate_industries[industry]
        series: list[dict[str, Any]] = []
        for index, date in enumerate(dates):
            mcap = float(values.get(date, 0.0))
            total = float(totals.get(date, 0.0))
            row: dict[str, Any] = {
                "date": date,
                "mcap": _number(mcap, 0),
                "weight_pct": _number(
                    mcap / total * 100, 4) if total > 0 else None,
            }
            for offset in (1, 5, 20):
                base_index = index - offset
                base_date = (
                    dates[base_index] if base_index >= 0 else None)
                base_mcap = (
                    values.get(base_date) if base_date else None)
                market_base = (
                    totals.get(base_date) if base_date else None)
                aggregate_return = _window_return(mcap, base_mcap)
                market_return = _window_return(total, market_base)
                row[f"return_{offset}d_pct"] = _number(
                    aggregate_return, 4)
                row[f"relative_{offset}d_pct"] = _number(
                    aggregate_return - market_return, 4
                ) if (
                    _finite(aggregate_return)
                    and _finite(market_return)
                ) else None
                row[f"contribution_{offset}d_bp"] = _number(
                    (mcap - float(base_mcap)) / float(market_base) * 10000,
                    2,
                ) if (
                    _finite(base_mcap)
                    and _finite(market_base)
                    and float(market_base) > 0
                ) else None
                base_weight = (
                    float(base_mcap) / float(market_base) * 100
                    if (
                        _finite(base_mcap)
                        and _finite(market_base)
                        and float(market_base) > 0
                    ) else None
                )
                row[f"weight_change_{offset}d_bp"] = _number(
                    (float(row["weight_pct"]) - base_weight) * 100, 2
                ) if (
                    _finite(row.get("weight_pct"))
                    and _finite(base_weight)
                ) else None

                current_stock_rows = list(
                    stocks_by_industry.get(date, {}).get(industry, []))
                base_stock_rows = (
                    list(stocks_by_industry.get(base_date, {}).get(
                        industry, []))
                    if base_date else []
                )
                stock_metrics = _stock_window_metrics(
                    current_stock_rows,
                    base_stock_rows,
                    fallback_daily=(offset == 1),
                    share_history=share_history,
                )
                row[
                    f"cap_weighted_return_{offset}d_pct"
                ] = stock_metrics["cap_weighted_return_pct"]
                row[
                    f"equal_weight_return_{offset}d_pct"
                ] = stock_metrics["equal_weight_return_pct"]
                row[
                    f"stock_breadth_{offset}d_pct"
                ] = stock_metrics["breadth_pct"]
                row[
                    f"stock_return_coverage_{offset}d_pct"
                ] = stock_metrics["coverage_pct"]
                if offset == 1:
                    row["stock_breadth_pct"] = stock_metrics[
                        "breadth_pct"]
                    row["up_count"] = stock_metrics["up_count"]
                    row["down_count"] = stock_metrics["down_count"]
                    row["flat_count"] = stock_metrics["flat_count"]
                    row["stock_count"] = stock_metrics["current_count"]
            row["contribution_bp"] = row["contribution_1d_bp"]
            row["weight_change_1d_bp"] = row[
                "weight_change_1d_bp"]
            current_stocks = list(
                stocks_by_industry.get(date, {}).get(industry, []))
            stock_values = sorted(
                [float(item.get("mcap") or 0) for item in current_stocks],
                reverse=True,
            )
            detailed_total = sum(stock_values)
            row["top1_stock_share_pct"] = _number(
                stock_values[0] / detailed_total * 100, 2
            ) if stock_values and detailed_total > 0 else None
            row["top5_stock_share_pct"] = _number(
                sum(stock_values[:5]) / detailed_total * 100, 2
            ) if detailed_total > 0 else None
            row["effective_stocks"] = _number(
                effective_count(stock_values), 2)
            state, label = _industry_state(row)
            row["state"] = state
            row["state_label"] = label
            series.append(row)
        _add_causal_percentiles(
            series,
            {
                "return_percentile": "relative_1d_pct",
                "contribution_percentile": "contribution_bp",
                "breadth_percentile": "stock_breadth_pct",
                "weight_migration_percentile": "weight_change_5d_bp",
            },
            min_history=min_history,
            window=percentile_window,
        )
        industry_daily[industry] = series

    for index, date in enumerate(dates):
        total = float(totals.get(date, 0.0))
        previous_date = dates[index - 1] if index > 0 else None
        previous_total = (
            float(totals.get(previous_date, 0.0))
            if previous_date else 0.0
        )
        market_return = _window_return(total, previous_total)
        current_stocks = stocks_by_date.get(date, {})
        previous_stocks = (
            stocks_by_date.get(previous_date, {})
            if previous_date else {}
        )
        stock_metrics = _stock_window_metrics(
            list(current_stocks.values()),
            list(previous_stocks.values()),
            fallback_daily=True,
            share_history=share_history,
        )
        style = (
            _style_metrics(
                current_stocks,
                previous_stocks,
                total,
                share_history,
            )
            if previous_stocks else {
                key: {
                    "return_pct": None,
                    "equal_weight_return_pct": None,
                    "cap_weighted_return_pct": None,
                    "breadth_pct": None,
                    "mcap_share_pct": None,
                    "count": 0,
                    "base_count": 0,
                    "coverage_pct": 0.0,
                }
                for key in ("top100", "next400", "rest")
            }
        )
        industry_rows = [
            industry_daily[industry][index]
            for industry in industry_names
        ]
        industry_returns = [
            row.get("return_1d_pct") for row in industry_rows
            if _finite(row.get("return_1d_pct"))
        ]
        up_industries = sum(
            float(value) > RETURN_NOISE_PCT
            for value in industry_returns)
        down_industries = sum(
            float(value) < -RETURN_NOISE_PCT
            for value in industry_returns)
        flat_industries = (
            len(industry_returns) - up_industries - down_industries)
        contributions = [
            float(row["contribution_bp"])
            for row in industry_rows
            if _finite(row.get("contribution_bp"))
        ]
        concentration = _concentration(
            [
                float(row.get("mcap") or 0)
                for row in industry_rows
            ],
            total,
        )
        positive_contributions = sorted(
            [value for value in contributions if value > 0],
            reverse=True,
        )
        concentration[
            "positive_contribution_top3_share_pct"
        ] = _number(
            sum(positive_contributions[:3])
            / sum(positive_contributions) * 100,
            2,
        ) if positive_contributions else None
        concentration[
            "positive_contribution_top5_share_pct"
        ] = _number(
            sum(positive_contributions[:5])
            / sum(positive_contributions) * 100,
            2,
        ) if positive_contributions else None

        effects = _proxy_effects(
            current_stocks,
            previous_stocks,
            previous_total,
            market_return,
        )
        point_effects = (
            _point_in_time_effects(
                current_stocks,
                previous_stocks,
                date,
                previous_date,
                share_history,
                total,
                previous_total,
            )
            if previous_date else None
        )
        if point_effects and point_effects.get("available"):
            effects = {
                key: value for key, value in point_effects.items()
                if key not in {"available", "coverage_pct", "reason"}
            }
        circulating_snapshot = _circulating_snapshot(
            current_stocks, date, share_history)
        detailed_total = sum(
            float(row.get("mcap") or 0)
            for row in current_stocks.values()
        )
        expected = int(expected_counts.get(date) or 0)
        row = {
            "date": date,
            "total_mcap": _number(total, 0),
            "market_return_pct": _number(market_return, 4),
            "cap_weighted_return_pct": stock_metrics[
                "cap_weighted_return_pct"],
            "equal_weight_return_pct": stock_metrics[
                "equal_weight_return_pct"],
            "stock_breadth_pct": stock_metrics["breadth_pct"],
            "industry_breadth_pct": _number(
                up_industries / len(industry_returns) * 100, 2
            ) if industry_returns else None,
            "up_stock_count": stock_metrics["up_count"],
            "down_stock_count": stock_metrics["down_count"],
            "flat_stock_count": stock_metrics["flat_count"],
            "up_industry_count": up_industries,
            "down_industry_count": down_industries,
            "flat_industry_count": flat_industries,
            "covered_stock_count": len(current_stocks),
            "expected_stock_count": expected or None,
            "stock_detail_coverage_pct": _number(
                detailed_total / total * 100, 2
            ) if total > 0 else None,
            "style": style,
            "measure_kind": basis_by_date.get(date, {}).get(
                "measure_kind", "current_share_price_proxy"),
            "point_in_time_coverage_pct": basis_by_date.get(
                date, {}).get("point_in_time_coverage_pct"),
            "point_in_time_fallback_ratio": basis_by_date.get(
                date, {}).get("fallback_ratio"),
            "circulating_mcap_proxy": circulating_snapshot[
                "circulating_mcap_proxy"],
            "circulating_share_coverage_pct": circulating_snapshot[
                "coverage_pct"],
            **concentration,
            **effects,
        }
        state, label = _market_state(row)
        row["state"] = state
        row["state_label"] = label
        market_series.append(row)

    _add_causal_percentiles(
        market_series,
        {
            "return_percentile": "market_return_pct",
            "breadth_percentile": "stock_breadth_pct",
            "concentration_percentile": "hhi",
            "contribution_concentration_percentile":
                "positive_contribution_top3_share_pct",
        },
        min_history=min_history,
        window=percentile_window,
    )

    latest_date = dates[-1]
    latest_market = dict(market_series[-1])
    industry_output: list[dict[str, Any]] = []
    previous_date = dates[-2] if len(dates) > 1 else None
    previous_market_total = (
        float(totals.get(previous_date, 0.0))
        if previous_date else 0.0
    )
    for industry in industry_names:
        series = industry_daily[industry]
        latest = dict(series[-1])
        current_stocks = sorted(
            stocks_by_industry.get(latest_date, {}).get(industry, []),
            key=lambda row: float(row.get("mcap") or 0),
            reverse=True,
        )
        previous_lookup = {
            row["code"]: row
            for row in (
                stocks_by_industry.get(previous_date, {}).get(industry, [])
                if previous_date else []
            )
        }
        detailed_total = sum(
            float(row.get("mcap") or 0) for row in current_stocks)
        top_stocks = []
        for stock in current_stocks[:5]:
            before = previous_lookup.get(stock["code"])
            contribution = None
            raw_change = stock.get("change_pct")
            adjusted_change = raw_change
            if (
                before
                and _finite(before.get("price"))
                and float(before["price"]) > 0
                and _finite(stock.get("price"))
                and float(stock["price"]) > 0
            ):
                adjusted_change = _stock_price_return_pct(
                    stock, before, share_history)
            if previous_market_total > 0:
                previous_value = (
                    float(before.get("mcap") or 0) if before else 0.0)
                contribution = (
                    float(stock.get("mcap") or 0) - previous_value
                ) / previous_market_total * 10000
            top_stocks.append({
                "code": stock["code"],
                "name": stock["name"],
                "mcap": _number(stock["mcap"], 0),
                "weight_pct": _number(
                    float(stock["mcap"]) / detailed_total * 100, 2
                ) if detailed_total > 0 else None,
                "change_pct": _number(adjusted_change, 2),
                "raw_change_pct": _number(raw_change, 2),
                "contribution_bp": _number(contribution, 2),
            })
        industry_output.append({
            "industry": industry,
            **latest,
            "latest": latest,
            "series": series,
            "top_stocks": top_stocks,
        })
    industry_output.sort(
        key=lambda row: float(row.get("mcap") or 0), reverse=True)

    latest_rows = stocks_by_date.get(latest_date, {})
    classification = _classification_quality(scheme, latest_rows)
    current_share_quality = _current_share_snapshot_quality(latest_date)
    detail_coverage = latest_market.get("stock_detail_coverage_pct")
    pit_rows = [
        row for row in market_series
        if row.get("effect_measure_kind") in {
            "point_in_time_total_shares",
            "hybrid_point_in_time_and_current_share_proxy",
        }
    ]
    pit_coverage = (
        _mean(row.get("effect_coverage_pct") for row in pit_rows)
        if pit_rows else 0.0
    )
    circulating_latest = latest_market.get("circulating_mcap_proxy")
    warnings = [
        "市值变化是边际价格对全部股份的重估，不是资金净流入或净流出",
        (
            "历史个股明细由当前股票宇宙回溯，无法完整纳入此前退市或已退出"
            "样本；Universe 效应存在幸存者偏差，置信度为低"
        ),
    ]
    latest_measure = latest_market.get("measure_kind")
    if latest_measure == "current_share_price_proxy":
        warnings.append(
            "历史市值使用当前/缓存股本快照，是价格重估代理而非严格点时总市值"
        )
    elif latest_measure == "hybrid_point_in_time_and_current_share_proxy":
        point_coverage = float(
            latest_market.get("point_in_time_coverage_pct") or 0)
        warnings.append(
            f"点时总股本市值覆盖 {point_coverage:.2f}%，"
            "未覆盖部分使用当前股本价格代理"
        )
    if share_history.get("warning"):
        warnings.append(str(share_history["warning"]))
    if classification.get("fallback"):
        warnings.append(
            f"{classification['fallback']} 只股票使用了分类回退，"
            "细分行业为混合粒度"
        )
    if not _finite(detail_coverage) or float(detail_coverage) < 95:
        warnings.append("个股明细市值覆盖不足95%，广度和大小盘结构置信度下降")
    if current_share_quality.get("legacy_invalid"):
        warnings.append(
            "旧版股本缓存把腾讯字段72误作总股本，已隔离且不可用于总市值")
    elif current_share_quality.get("stale"):
        warnings.append("腾讯当前股本快照早于估值日，降级口径置信度下降")

    state_distribution = dict(Counter(
        row.get("state") or "unknown" for row in industry_output))
    summary = (
        f"{latest_market.get('state_label', '结构待确认')} · "
        f"市值变化 {latest_market.get('market_return_pct'):+.2f}% · "
        f"股票上涨广度 "
        f"{latest_market.get('stock_breadth_pct'):.1f}%"
        if (
            _finite(latest_market.get("market_return_pct"))
            and _finite(latest_market.get("stock_breadth_pct"))
        )
        else latest_market.get("state_label", "结构待确认")
    )
    market = {
        **latest_market,
        "latest": latest_market,
        "series": market_series,
        "summary": summary,
        "state_distribution": state_distribution,
    }
    quality = {
        "measure_kind": latest_market.get(
            "measure_kind", "current_share_price_proxy"),
        "measure_label": (
            "点时总股本市值"
            if latest_market.get("measure_kind")
            == "point_in_time_total_shares"
            else "点时总股本与当前股本代理混合"
            if latest_market.get("measure_kind")
            == "hybrid_point_in_time_and_current_share_proxy"
            else "当前/缓存总股本价格重估代理"
        ),
        "is_actual_point_in_time_market_cap": (
            latest_market.get("measure_kind")
            == "point_in_time_total_shares"
        ),
        "history_days": len(dates),
        "first_date": dates[0],
        "last_date": latest_date,
        "covered_stocks": len(latest_rows),
        "expected_stocks": expected_counts.get(latest_date) or None,
        "stock_detail_coverage_ratio": _number(
            float(detail_coverage) / 100, 4
        ) if _finite(detail_coverage) else None,
        "classification": classification,
        "current_share_snapshot": current_share_quality,
        "point_in_time_shares": {
            "available": bool(pit_rows),
            "file_available": bool(share_history.get("available")),
            "coverage": _number(float(pit_coverage) / 100, 4)
            if _finite(pit_coverage) else 0.0,
            "source": share_history.get("source"),
            "updated_at": share_history.get("updated_at"),
            "used_for_effect_decomposition": bool(pit_rows),
            "query_start": share_history.get("query_start"),
            "query_end": share_history.get("query_end"),
            "lookback_start": share_history.get("lookback_start"),
            "lookback_end": share_history.get("lookback_end"),
            "fallback_ratio": latest_market.get(
                "point_in_time_fallback_ratio"),
            "calibrated_unit": share_history.get("calibrated_unit"),
            "calibrated_unit_counts": share_history.get(
                "calibrated_unit_counts") or {},
            "status_counts": share_history.get("status_counts") or {},
        },
        "circulating_share_proxy": {
            "available": circulating_latest is not None,
            "coverage": _number(
                float(
                    latest_market.get(
                        "circulating_share_coverage_pct") or 0
                )
                / 100,
                4,
            ),
            "source": (
                share_history.get("source")
                if circulating_latest is not None else None
            ),
            "field": "CNINFO F022N 人民币普通股",
            "disclaimer": (
                "circulating_share_proxy_not_csi_free_float"
            ),
        },
        "free_float": {
            "available": False,
            "coverage": 0.0,
            "source": None,
            "disclaimer": "未提供中证自由流通股本，不以F022N伪造",
        },
        "effects": {
            "price": {
                "method": (
                    latest_market.get("effect_measure_kind")
                    or "unavailable"
                ),
                "confidence": (
                    "high"
                    if latest_market.get("effect_measure_kind")
                    in {
                        "point_in_time_total_shares",
                        "hybrid_point_in_time_and_current_share_proxy",
                    }
                    else "medium"
                    if _finite(latest_market.get("effect_coverage_pct"))
                    and float(latest_market["effect_coverage_pct"]) >= 95
                    else "low"
                ),
            },
            "supply": {
                "method": (
                    (
                        latest_market.get("effect_measure_kind")
                        if latest_market.get("supply_effect_bp") is not None
                        else "unavailable"
                    )
                ),
                "confidence": (
                    "high"
                    if latest_market.get("supply_effect_bp") is not None
                    else "none"
                ),
            },
            "universe": {
                "method": "entered_minus_exited_stock_mcap",
                "confidence": "low",
                "bias": "current_universe_backfill_survivorship_bias",
                "disclosure": (
                    "历史个股明细按当前股票宇宙回溯；此前退市或已退出样本"
                    "可能缺失，因此进入/退出市值不代表完整历史 Universe 变化"
                ),
            },
            "residual": {
                "includes": [
                    "个股明细缺口", "股本快照修订", "分类变化", "舍入误差"
                ],
            },
            "company_action": {
                "method": "neutral_divisor_adjustment",
                "effect_is_reclassification_not_additive": True,
                "company_action_effect_included_in_price": True,
                "latest_count": latest_market.get(
                    "company_action_count") or 0,
                "notice": (
                    "送转、拆并股等股本与价格反向跳变从供给贡献中重分类，"
                    "避免机械价格跳空被识别为崩跌；H股上市、增发、配股、"
                    "行权、转股等真实新增股本始终保留在供给效应"
                ),
            },
        },
        "proxy_notice": (
            "市值变化是点时总股本或明确降级股本下的价格重估；"
            "F022仅为人民币普通股流通市值代理，不是中证自由流通市值，"
            "所有市值变化均不是资金流向或实际投入现金。"
        ),
        "warnings": warnings,
    }
    payload = {
        "schema_version": 2,
        "model_version": MODEL_VERSION,
        "scheme": scheme,
        "scheme_label": SCHEME_LABELS[scheme],
        "trade_date": latest_date,
        "updated_at": (
            db._get_meta("market_cap_updated")
            if hasattr(db, "_get_meta") else None
        ),
        "dates": [
            {
                "date": date,
                "label": _date_label(date),
                "full_label": _full_date_label(date),
            }
            for date in dates
        ],
        "market": market,
        "industries": industry_output,
        "data_quality": quality,
    }
    return _json_safe(payload)


__all__ = [
    "MODEL_VERSION",
    "build_market_cap_structure_payload",
    "causal_percentiles",
    "effective_count",
    "load_point_in_time_shares",
]
