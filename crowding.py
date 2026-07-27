#!/usr/bin/env python3
"""交易拥挤度核心计算。

现有 OHLCV 只能观察“成交注意力是否集中”，不能直接观察共同持仓、杠杆或
订单簿退出容量。本模块因此同时保留原有 CR/HHI 字段，并补充可解释的三类证据：

1. 数据质量：市场/行业覆盖率；
2. 相对异常：只使用当时以前数据的滚动分位、成交异常和价格延伸；
3. 风险形态：方向一致度、涨跌宽度、内部集中、领涨身份持续性和价格冲击。

``build_crowding_frames`` 仍返回 ``(market_df, industry_df)``，旧字段保持不变。
增强字段直接附加在两个 DataFrame 上，列表/字典列可由新版 DB 层 JSON 序列化；
旧 DB 层会忽略未绑定的额外字段。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from datetime import datetime
import json
import math
import os
import sys
import tempfile
from typing import Any, Callable, Iterable

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd

from db import get_db
from kline_cache import KlineCache, load_industry_map
from runtime_paths import DATA_DIR, resource_path


MIN_STOCKS_PER_DATE = 500
MIN_COVERAGE_RATIO = 0.90
PERCENTILE_WINDOW = 250
MIN_PERCENTILE_HISTORY = 20
ACTIVITY_WINDOW = 20
BASELINE_WINDOW = 60
PRICE_EXTENSION_WINDOW = 20
MARKET_TOP_N = 10
INDUSTRY_TOP_N = 5
AMIHUD_SCALE = 10_000_000_000
RETURN_FLAT_BAND = 0.0001
SCHEME_CONFIG = {
    "sw": {"label": "申万一级", "suffix": ""},
    "ths": {"label": "同花顺", "suffix": "_ths"},
    "sw3": {"label": "申万三级", "suffix": "_sw3"},
}


def load_crowding_industry_schemes(
    codes: Iterable[str],
    *,
    classification_codes: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load the same three classification rules used by the heatmap."""
    report_codes = {
        str(code).zfill(6)
        for code in (
            classification_codes if classification_codes is not None else codes
        )
    }
    normalized = sorted(
        {str(code).zfill(6) for code in codes} | report_codes)
    sw_map = load_industry_map(normalized)

    try:
        with open(resource_path("industry_map_ths.json"), encoding="utf-8") as handle:
            ths_raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        ths_raw = {}
    if not isinstance(ths_raw, dict):
        ths_raw = {}

    try:
        with open(resource_path("industry_taxonomy.json"), encoding="utf-8") as handle:
            taxonomy_payload = json.load(handle)
        taxonomy = (
            taxonomy_payload.get("stocks", {})
            if isinstance(taxonomy_payload, dict) else {}
        )
    except (OSError, json.JSONDecodeError):
        taxonomy = {}

    sw = {}
    ths = {}
    sw3 = {}
    ths_direct = 0
    sw3_levels = {"sw3": 0, "sw2": 0, "sw1": 0, "other": 0}
    for code in normalized:
        sw_label = sw_map.get(code) or "其他"
        sw[code] = sw_label

        ths_label = ths_raw.get(code)
        if ths_label:
            if code in report_codes:
                ths_direct += 1
            ths[code] = ths_label
        else:
            ths[code] = sw_label

        details = taxonomy.get(code, {})
        if not isinstance(details, dict):
            details = {}
        if details.get("sw_level3"):
            sw3[code] = details["sw_level3"]
            if code in report_codes:
                sw3_levels["sw3"] += 1
        elif details.get("sw_level2"):
            sw3[code] = details["sw_level2"]
            if code in report_codes:
                sw3_levels["sw2"] += 1
        elif details.get("sw_level1") or sw_label != "其他":
            sw3[code] = details.get("sw_level1") or sw_label
            if code in report_codes:
                sw3_levels["sw1"] += 1
        else:
            sw3[code] = "其他"
            if code in report_codes:
                sw3_levels["other"] += 1

    total = len(report_codes)
    sw_mapped = sum(
        sw.get(code, "其他") != "其他" for code in report_codes)
    return {
        "sw": {
            **SCHEME_CONFIG["sw"],
            "map": sw,
            "classification": {
                "total": total,
                "direct": sw_mapped,
                "fallback": 0,
                "other": total - sw_mapped,
                "note": "申万2021一级行业",
            },
        },
        "ths": {
            **SCHEME_CONFIG["ths"],
            "map": ths,
            "classification": {
                "total": total,
                "direct": ths_direct,
                "fallback": total - ths_direct,
                "other": sum(
                    ths.get(code, "其他") == "其他" for code in report_codes),
                "note": "缺少同花顺分类时回退申万一级",
            },
        },
        "sw3": {
            **SCHEME_CONFIG["sw3"],
            "map": sw3,
            "classification": {
                "total": total,
                "direct": sw3_levels["sw3"],
                "fallback": sw3_levels["sw2"] + sw3_levels["sw1"],
                "other": sw3_levels["other"],
                "levels": sw3_levels,
                "note": "缺少申万三级时依次回退申万二级、一级",
            },
        },
    }


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _optional_float(value: Any, digits: int = 6) -> float | None:
    return round(float(value), digits) if _finite(value) else None


def causal_rolling_percentile(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
    min_periods: int = MIN_PERCENTILE_HISTORY,
) -> pd.Series:
    """当前值相对“此前 window 个值”的 ECDF，严格不使用当前及未来样本。

    并列值采用 ``<=`` 的右侧分位。样本少于 ``min_periods`` 时返回缺失值。
    该实现用有序窗口维护，避免 ``rolling.apply`` 在长序列上的二次复杂度。
    """
    if window < 1:
        raise ValueError("window must be positive")
    if min_periods < 1 or min_periods > window:
        raise ValueError("min_periods must be between 1 and window")

    values = pd.to_numeric(series, errors="coerce").tolist()
    result: list[float | None] = []
    ordered: list[float] = []
    history: deque[float | None] = deque()

    for raw in values:
        value = float(raw) if _finite(raw) else None
        if value is not None and len(ordered) >= min_periods:
            result.append(round(bisect_right(ordered, value) / len(ordered) * 100, 1))
        else:
            result.append(None)

        history.append(value)
        if value is not None:
            insort(ordered, value)
        if len(history) > window:
            expired = history.popleft()
            if expired is not None:
                ordered.pop(bisect_left(ordered, expired))

    return pd.Series(result, index=series.index, dtype="float64")


def causal_rolling_ratio(
    series: pd.Series,
    window: int = ACTIVITY_WINDOW,
    min_periods: int = 5,
) -> pd.Series:
    """当前值/此前滚动均值；基线显式 shift(1)，避免当前值泄漏到自身基线。"""
    numeric = pd.to_numeric(series, errors="coerce")
    baseline = numeric.shift(1).rolling(window, min_periods=min_periods).mean()
    return numeric / baseline.where(baseline > 0)


def causal_rolling_zscore(
    series: pd.Series,
    window: int = BASELINE_WINDOW,
    min_periods: int = 10,
) -> pd.Series:
    """当前值相对此前滚动窗口的 z-score。"""
    numeric = pd.to_numeric(series, errors="coerce")
    prior = numeric.shift(1).rolling(window, min_periods=min_periods)
    mean = prior.mean()
    std = prior.std(ddof=0)
    return (numeric - mean) / std.where(std > 0)


def identity_overlap(current: Iterable[str], previous: Iterable[str]) -> float | None:
    """两个身份集合的 Jaccard 重合率；双方都为空时返回缺失。"""
    left, right = set(current or ()), set(previous or ())
    if not left and not right:
        return None
    union = left | right
    return len(left & right) / len(union) if union else None


def add_identity_overlap(
    frame: pd.DataFrame,
    identity_col: str = "leader_codes",
    group_col: str | None = None,
    lookback: int = 5,
) -> pd.DataFrame:
    """为有序日序列增加领涨身份 1 日重合和近 N 日平均重合率。"""
    out = frame.copy()
    overlap_1d = pd.Series(index=out.index, dtype="float64")
    overlap_nd = pd.Series(index=out.index, dtype="float64")
    groups = out.groupby(group_col, sort=False) if group_col else [(None, out)]

    for _, raw_group in groups:
        group = raw_group.sort_values("date")
        prior_sets: deque[tuple[str, ...]] = deque(maxlen=lookback)
        for idx, raw_codes in zip(group.index, group[identity_col]):
            codes = tuple(raw_codes or ())
            overlap_1d.loc[idx] = identity_overlap(codes, prior_sets[-1]) if prior_sets else None
            historical = [
                score
                for score in (identity_overlap(codes, old) for old in prior_sets)
                if score is not None
            ]
            overlap_nd.loc[idx] = (
                sum(historical) / len(historical) if historical else None
            )
            prior_sets.append(codes)

    out["leader_overlap_1d"] = overlap_1d.round(4)
    out[f"leader_overlap_{lookback}d"] = overlap_nd.round(4)
    return out


def _stock_identity_rows(
    group: pd.DataFrame,
    n: int,
    *,
    leading: bool,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """返回可序列化 Top 身份；leading=True 按上涨成交贡献排序。"""
    ranked = group.copy()
    if leading:
        ranked = ranked[ranked["return"] > RETURN_FLAT_BAND].copy()
        ranked["rank_value"] = ranked["amount"] * ranked["return"]
    else:
        ranked["rank_value"] = ranked["amount"]
    ranked = ranked.sort_values(["rank_value", "amount", "code"], ascending=[False, False, True]).head(n)

    rows = []
    for row in ranked.to_dict("records"):
        stock_return = row.get("return")
        rows.append({
            "code": str(row["code"]),
            "name": str(row.get("name") or ""),
            "amount": round(float(row["amount"])),
            "return_pct": (
                round(float(stock_return) * 100, 2)
                if _finite(stock_return) else None
            ),
            "price_extension_pct": (
                round(float(row.get("price_extension")) * 100, 2)
                if _finite(row.get("price_extension")) else None
            ),
        })
    return rows, tuple(item["code"] for item in rows)


def _weighted_mean(group: pd.DataFrame, value_col: str, weight_col: str = "amount") -> float | None:
    valid = group[
        pd.to_numeric(group[value_col], errors="coerce").notna()
        & pd.to_numeric(group[weight_col], errors="coerce").gt(0)
    ]
    if valid.empty:
        return None
    weights = valid[weight_col].astype(float)
    return float((valid[value_col].astype(float) * weights).sum() / weights.sum())


def compute_scope_metrics(group: pd.DataFrame, top_n: int) -> dict[str, Any]:
    """从某日市场或行业的逐股数据提取方向、流动性、延伸和身份指标。"""
    traded = group[group["amount"] > 0].copy()
    total = float(traded["amount"].sum())
    if traded.empty or total <= 0:
        return {
            "traded_stocks": 0,
            "up": 0,
            "down": 0,
            "flat": 0,
            "breadth": None,
            "direction_score": None,
            "direction_consistency": None,
            "up_amount_share": None,
            "down_amount_share": None,
            "amihud_1e10": None,
            "downside_impact": None,
            "price_extension": None,
            "extension_breadth": None,
            "price_change_pct": None,
            "internal_top5": None,
            "top_stocks": [],
            "top_stock_codes": (),
            "leaders": [],
            "leader_codes": (),
        }

    returns = pd.to_numeric(traded["return"], errors="coerce")
    valid_return = returns.notna()
    up_mask = valid_return & returns.gt(RETURN_FLAT_BAND)
    down_mask = valid_return & returns.lt(-RETURN_FLAT_BAND)
    flat_mask = valid_return & ~up_mask & ~down_mask
    valid_n = int(valid_return.sum())
    up, down, flat = int(up_mask.sum()), int(down_mask.sum()), int(flat_mask.sum())

    up_amount = float(traded.loc[up_mask, "amount"].sum())
    down_amount = float(traded.loc[down_mask, "amount"].sum())
    direction_score = (up_amount - down_amount) / total

    liquid = traded[valid_return & traded["amount"].gt(0)]
    amihud = (
        (liquid["return"].abs() / liquid["amount"] * AMIHUD_SCALE).median()
        if not liquid.empty else None
    )
    downside_impact = float(
        (returns.clip(upper=0).abs().fillna(0) * traded["amount"]).sum() / total
    )

    extension = _weighted_mean(traded, "price_extension")
    weighted_return = _weighted_mean(traded, "return")
    valid_extension = traded["price_extension"].notna()
    extension_breadth = (
        float(traded.loc[valid_extension, "price_extension"].gt(0).mean())
        if valid_extension.any() else None
    )

    top_stocks, top_stock_codes = _stock_identity_rows(traded, top_n, leading=False)
    leaders, leader_codes = _stock_identity_rows(traded, top_n, leading=True)
    internal_top5 = float(traded.nlargest(5, "amount")["amount"].sum() / total)

    return {
        "traded_stocks": int(traded["code"].nunique()),
        "up": up,
        "down": down,
        "flat": flat,
        "breadth": (up - down) / valid_n if valid_n else None,
        "direction_score": direction_score,
        "direction_consistency": abs(direction_score),
        "up_amount_share": up_amount / total,
        "down_amount_share": down_amount / total,
        "amihud_1e10": _optional_float(amihud),
        "downside_impact": _optional_float(downside_impact),
        "price_extension": _optional_float(extension),
        "extension_breadth": _optional_float(extension_breadth),
        "price_change_pct": (
            _optional_float(weighted_return * 100, 3)
            if weighted_return is not None else None
        ),
        "internal_top5": _optional_float(internal_top5),
        "top_stocks": top_stocks,
        "top_stock_codes": top_stock_codes,
        "leaders": leaders,
        "leader_codes": leader_codes,
    }


def compute_daily_amounts(cache: KlineCache | None = None) -> pd.DataFrame:
    """逐股日度特征。

    兼容旧调用所需的 ``date/code/amount``，并增加 return、价格延伸等后续纯
    聚合所需字段。所有滚动价格基线均只使用当前日期之前的数据。
    """
    cache = cache or KlineCache()
    cache._load()
    frames = []
    for code, raw in (cache._cache.get("data") or {}).items():
        if raw is None or len(raw) < 2 or "close" not in raw.columns:
            continue
        df = raw.sort_values("date").drop_duplicates("date", keep="last").copy()
        close = pd.to_numeric(df["close"], errors="coerce")
        volume = (
            pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            if "volume" in df.columns else pd.Series(0.0, index=df.index)
        )
        returns = close.pct_change(fill_method=None)
        if "change_pct" in df.columns:
            spot_return = pd.to_numeric(df["change_pct"], errors="coerce") / 100
            returns = returns.where(spot_return.isna(), spot_return)
        prior_ma = close.shift(1).rolling(
            PRICE_EXTENSION_WINDOW, min_periods=5
        ).mean()
        names = df["name"].dropna() if "name" in df.columns else pd.Series(dtype=object)
        name = str(names.iloc[-1]) if len(names) else ""
        frames.append(pd.DataFrame({
            "date": pd.to_datetime(df["date"], errors="coerce").values,
            "code": str(code),
            "name": name,
            "close": close.values,
            "volume": volume.values,
            "amount": (close * volume).values,
            "return": returns.values,
            "price_extension": (close / prior_ma - 1).values,
        }))
    if not frames:
        return pd.DataFrame(columns=[
            "date", "code", "name", "close", "volume", "amount", "return",
            "price_extension",
        ])
    result = pd.concat(frames, ignore_index=True)
    result = result.dropna(subset=["date", "code", "amount"])
    result = result[result["amount"] >= 0]
    return result


def compute_coverage(
    stock_daily: pd.DataFrame,
    expected_stocks: int | None = None,
) -> pd.DataFrame:
    """按日覆盖率；分母取缓存声明股票数与观测最大值的较大者。"""
    counts = (
        stock_daily[stock_daily["amount"] > 0]
        .groupby("date")["code"].nunique()
        .sort_index()
    )
    observed_max = int(counts.max()) if not counts.empty else 0
    expected = max(int(expected_stocks or 0), observed_max, 1)
    return pd.DataFrame({
        "date": counts.index,
        "stocks": counts.astype(int).values,
        "expected_stocks": expected,
        "coverage": (counts / expected).clip(upper=1).values,
    })


def _assign_group_feature(
    frame: pd.DataFrame,
    source: str,
    target: str,
    function: Callable[[pd.Series], pd.Series],
    group_col: str | None = None,
) -> None:
    result = pd.Series(index=frame.index, dtype="float64")
    groups = frame.groupby(group_col, sort=False) if group_col else [(None, frame)]
    for _, raw_group in groups:
        group = raw_group.sort_values("date")
        result.loc[group.index] = function(group[source]).values
    frame[target] = result


def add_causal_market_features(
    market: pd.DataFrame,
    window: int = PERCENTILE_WINDOW,
    min_history: int = MIN_PERCENTILE_HISTORY,
) -> pd.DataFrame:
    """增加市场域因果分位、综合证据和状态机字段。"""
    out = market.sort_values("date").reset_index(drop=True).copy()
    percentile_fields = (
        "total_amount", "cr5", "cr10", "hhi", "top10_stock_share",
        "top50_stock_share", "internal_top5", "amihud_1e10", "downside_impact",
        "price_extension",
    )
    for source in percentile_fields:
        if source in out:
            out[f"{source}_pctile"] = causal_rolling_percentile(
                out[source], window=window, min_periods=min_history
            )
    out["amount_ratio_20"] = causal_rolling_ratio(out["total_amount"])
    out["amount_zscore_60"] = causal_rolling_zscore(out["total_amount"])
    industry_domain = [
        col for col in ("cr5_pctile", "cr10_pctile", "hhi_pctile") if col in out
    ]
    stock_domain = [
        col for col in ("top10_stock_share_pctile", "top50_stock_share_pctile")
        if col in out
    ]
    out["industry_concentration_pctile"] = out[industry_domain].mean(axis=1)
    out["stock_concentration_pctile"] = out[stock_domain].mean(axis=1)
    # 两个统计域等权，避免 CR5/CR10/HHI 三个嵌套指标重复抬高行业域权重。
    out["concentration_pctile"] = out[
        ["industry_concentration_pctile", "stock_concentration_pctile"]
    ].mean(axis=1)
    out["crowding_score"] = out.apply(_market_crowding_score, axis=1)
    states = out.apply(lambda row: classify_crowding_state(row, "market"), axis=1)
    out["state"] = states.map(lambda value: value["state"])
    out["state_label"] = states.map(lambda value: value["label"])
    out["reasons"] = states.map(lambda value: value["reasons"])
    out["state_evidence"] = states.map(lambda value: value["evidence"])
    return out


def add_causal_industry_features(
    industry: pd.DataFrame,
    window: int = PERCENTILE_WINDOW,
    min_history: int = MIN_PERCENTILE_HISTORY,
) -> pd.DataFrame:
    """增加各行业自身历史域的因果分位、相对成交异常与状态。"""
    out = industry.sort_values(["industry", "date"]).reset_index(drop=True).copy()
    percentile_fields = (
        "amount", "share", "internal_top5", "amihud_1e10",
        "downside_impact", "price_extension",
    )
    for source in percentile_fields:
        if source in out:
            _assign_group_feature(
                out,
                source,
                f"{source}_pctile",
                lambda series, _w=window, _m=min_history: causal_rolling_percentile(
                    series, window=_w, min_periods=_m
                ),
                group_col="industry",
            )
    _assign_group_feature(
        out, "amount", "amount_ratio_20",
        lambda series: causal_rolling_ratio(series, ACTIVITY_WINDOW, 5),
        group_col="industry",
    )
    _assign_group_feature(
        out, "share", "share_ratio_60",
        lambda series: causal_rolling_ratio(series, BASELINE_WINDOW, 10),
        group_col="industry",
    )
    _assign_group_feature(
        out, "amount", "amount_zscore_60",
        lambda series: causal_rolling_zscore(series, BASELINE_WINDOW, 10),
        group_col="industry",
    )
    _assign_group_feature(
        out, "share", "share_zscore_60",
        lambda series: causal_rolling_zscore(series, BASELINE_WINDOW, 10),
        group_col="industry",
    )
    out["relative_amount_anomaly"] = out["share_ratio_60"] - 1
    out["crowding_score"] = out.apply(_industry_crowding_score, axis=1)
    states = out.apply(lambda row: classify_crowding_state(row, "industry"), axis=1)
    out["state"] = states.map(lambda value: value["state"])
    out["state_label"] = states.map(lambda value: value["label"])
    out["reasons"] = states.map(lambda value: value["reasons"])
    out["state_evidence"] = states.map(lambda value: value["evidence"])
    return out.sort_values(["date", "industry"]).reset_index(drop=True)


def _weighted_score(parts: Iterable[tuple[Any, float]]) -> float | None:
    usable = [(float(value), weight) for value, weight in parts if _finite(value)]
    if not usable:
        return None
    total_weight = sum(weight for _, weight in usable)
    return round(sum(value * weight for value, weight in usable) / total_weight, 1)


def _market_crowding_score(row: pd.Series) -> float | None:
    if not _finite(row.get("concentration_pctile")) or not _finite(
        row.get("total_amount_pctile")
    ):
        return None
    return _weighted_score((
        (row.get("concentration_pctile"), 0.45),
        (row.get("total_amount_pctile"), 0.15),
        ((row.get("leader_overlap_5d") or 0) * 100, 0.15),
        ((row.get("direction_consistency") or 0) * 100, 0.10),
        (row.get("price_extension_pctile"), 0.10),
        (row.get("amihud_1e10_pctile"), 0.05),
    ))


def _industry_crowding_score(row: pd.Series) -> float | None:
    if not _finite(row.get("share_pctile")) or not _finite(
        row.get("amount_pctile")
    ):
        return None
    return _weighted_score((
        (row.get("share_pctile"), 0.30),
        (row.get("amount_pctile"), 0.20),
        (row.get("internal_top5_pctile"), 0.15),
        ((row.get("leader_overlap_5d") or 0) * 100, 0.10),
        (row.get("price_extension_pctile"), 0.10),
        ((row.get("direction_consistency") or 0) * 100, 0.10),
        (row.get("amihud_1e10_pctile"), 0.05),
    ))


def classify_crowding_state(row: pd.Series, scope: str) -> dict[str, Any]:
    """透明状态机；返回状态、中文标签、触发理由和原始证据。"""
    if scope not in {"market", "industry"}:
        raise ValueError("scope must be market or industry")

    concentration = row.get("concentration_pctile") if scope == "market" else row.get("share_pctile")
    activity = row.get("total_amount_pctile") if scope == "market" else row.get("amount_pctile")
    direction = row.get("direction_score")
    breadth = row.get("breadth")
    extension = row.get("price_extension")
    downside = row.get("downside_impact_pctile")
    overlap = row.get("leader_overlap_5d")
    evidence = {
        "concentration_pctile": _optional_float(concentration, 1),
        "industry_concentration_pctile": _optional_float(
            row.get("industry_concentration_pctile"), 1
        ),
        "stock_concentration_pctile": _optional_float(
            row.get("stock_concentration_pctile"), 1
        ),
        "activity_pctile": _optional_float(activity, 1),
        "internal_top5_pctile": _optional_float(row.get("internal_top5_pctile"), 1),
        "amihud_pctile": _optional_float(row.get("amihud_1e10_pctile"), 1),
        "direction_score": _optional_float(direction, 3),
        "breadth": _optional_float(breadth, 3),
        "price_extension": _optional_float(extension, 4),
        "downside_impact_pctile": _optional_float(downside, 1),
        "leader_overlap_5d": _optional_float(overlap, 3),
        "coverage": _optional_float(row.get("coverage"), 3),
        "crowding_score": _optional_float(row.get("crowding_score"), 1),
    }
    reasons = [
        f"集中分位 {float(concentration):.0f}" if _finite(concentration) else "集中分位样本不足",
        f"活跃分位 {float(activity):.0f}" if _finite(activity) else "活跃分位样本不足",
    ]

    if not _finite(concentration) or not _finite(activity):
        return {
            "state": "insufficient_history",
            "label": "数据积累",
            "reasons": reasons,
            "evidence": evidence,
        }

    concentration, activity = float(concentration), float(activity)
    direction = float(direction) if _finite(direction) else 0.0
    breadth = float(breadth) if _finite(breadth) else 0.0
    extension = float(extension) if _finite(extension) else 0.0
    downside = float(downside) if _finite(downside) else 50.0

    if concentration >= 80:
        if direction >= 0.10 and breadth >= 0.10 and extension > 0:
            state, label = "crowded_advance", "拥挤上行"
            reasons += ["方向成交与涨跌宽度同向为正", "价格位于前期均线上方"]
        elif direction <= -0.10 and breadth <= -0.10:
            state, label = "crowded_decline", "拥挤下行"
            reasons += ["方向成交与涨跌宽度同向为负"]
        elif downside >= 80 or direction * breadth < 0:
            state, label = "crowded_divergence", "拥挤分歧"
            reasons += ["方向证据分歧或下跌冲击偏高"]
        else:
            state, label = "crowded", "高位集中"
            reasons += ["集中度高但方向尚未确认"]
    elif activity >= 80 and direction >= 0.10 and breadth >= 0.10:
        state, label = "emerging", "放量扩散"
        reasons += ["成交活跃且上涨覆盖较广"]
    elif activity >= 80 and direction <= -0.10:
        state, label = "distribution", "放量退潮"
        reasons += ["成交活跃但方向成交为负"]
    elif concentration <= 20 and activity <= 30:
        state, label = "quiet", "低关注"
        reasons += ["集中与活跃均处低位"]
    elif concentration <= 30:
        state, label = "diffuse", "交易分散"
        reasons += ["成交未集中在少数身份"]
    else:
        state, label = "neutral", "常态"
        reasons += ["未触发极端状态"]

    return {"state": state, "label": label, "reasons": reasons, "evidence": evidence}


def _streak(values: Iterable[bool]) -> list[int]:
    result: list[int] = []
    current = 0
    for value in values:
        current = current + 1 if bool(value) else 0
        result.append(current)
    return result


def _breadth_label(value: Any) -> str:
    if not _finite(value):
        return "宽度未知"
    value = float(value)
    if value >= 0.25:
        return "广泛上涨"
    if value >= 0.08:
        return "上涨占优"
    if value <= -0.25:
        return "广泛下跌"
    if value <= -0.08:
        return "下跌占优"
    return "涨跌均衡"


def _direction_label(value: Any) -> str:
    if not _finite(value):
        return "方向未知"
    value = float(value)
    if value >= 0.15:
        return "上涨成交占优"
    if value <= -0.15:
        return "下跌成交占优"
    return "方向成交均衡"


def assess_exit_risk(row: pd.Series, scope: str) -> dict[str, Any]:
    """Keep attention concentration separate from fragility and active unwind.

    ``watch`` needs concentration only.  ``fragile`` needs at least two
    independent confirmation domains.  ``unwind`` additionally needs a prior
    crowded state and current downside damage.
    """
    concentration = (
        row.get("concentration_pctile")
        if scope == "market" else row.get("share_pctile")
    )
    if not _finite(concentration):
        return {
            "state": "unknown",
            "label": "证据不足",
            "reasons": ["因果历史样本不足，尚不能评估退出危险"],
            "domains": {},
        }

    concentration = float(concentration)
    domains: dict[str, str] = {}
    if _finite(row.get("leader_overlap_5d")) and float(row.get("leader_overlap_5d")) >= 0.50:
        domains["synchrony"] = "领涨身份近5日高度重合"
    if (scope != "market"
            and _finite(row.get("internal_top5_pctile"))
            and float(row.get("internal_top5_pctile")) >= 80):
        domains["internal"] = "行业内部成交进一步集中"
    if _finite(row.get("price_extension_pctile")) and float(row.get("price_extension_pctile")) >= 80:
        domains["extension"] = "价格延伸处于历史高位"
    if _finite(row.get("amihud_1e10_pctile")) and float(row.get("amihud_1e10_pctile")) >= 80:
        domains["liquidity"] = "单位成交额价格冲击偏高"
    if (_finite(row.get("direction_score")) and _finite(row.get("breadth"))
            and float(row.get("direction_score")) <= -0.10
            and float(row.get("breadth")) <= -0.10):
        domains["breadth"] = "方向成交与涨跌宽度同步转弱"
    if _finite(row.get("downside_impact_pctile")) and float(row.get("downside_impact_pctile")) >= 80:
        domains["downside"] = "下跌冲击处于历史高位"
    if (_finite(row.get("direct_position_score"))
            and float(row.get("direct_position_score")) >= 80):
        position_domains = row.get("direct_position_domains")
        if isinstance(position_domains, (list, tuple)) and position_domains:
            domains["position"] = (
                "、".join(str(value) for value in position_domains)
                + "相对日成交承载处于高位"
            )
        else:
            domains["position"] = "融资、ETF或披露持仓显示直接拥挤证据"
    if (_finite(row.get("etf_exit_pctile"))
            and float(row.get("etf_exit_pctile")) >= 80
            and _finite(row.get("price_change_pct"))
            and float(row.get("price_change_pct")) < 0):
        domains["redemption"] = "ETF份额下降与价格转弱同时出现"
    if (_finite(row.get("external_fragility_score"))
            and float(row.get("external_fragility_score")) >= 80):
        domains["external_liquidity"] = "外部流动性证据显示退出承载偏弱"

    reasons = [f"成交集中处于 {concentration:.0f} 分位"]
    reasons.extend(domains.values())
    persistence = int(row.get("persistence_days") or 0)
    active_damage = (
        "breadth" in domains
        and "downside" in domains
        and (_finite(row.get("price_change_pct"))
             and float(row.get("price_change_pct")) < 0)
    )
    if concentration >= 80 and persistence >= 3 and active_damage:
        return {
            "state": "unwind",
            "label": "去拥挤中",
            "reasons": reasons + [f"此前已连续集中 {persistence} 日，当前破坏正在发生"],
            "domains": domains,
        }
    if concentration >= 80 and len(domains) >= 2:
        return {
            "state": "fragile",
            "label": "脆弱拥挤",
            "reasons": reasons + ["至少两个独立风险域得到确认"],
            "domains": domains,
        }
    if concentration >= 80:
        return {
            "state": "watch",
            "label": "集中观察",
            "reasons": reasons + ["尚未获得足够独立退出危险证据"],
            "domains": domains,
        }
    return {
        "state": "normal",
        "label": "普通",
        "reasons": reasons + ["成交集中尚未进入极端区间"],
        "domains": domains,
    }


def add_presentation_features(
    market: pd.DataFrame,
    industry: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Add frontend aliases, causal persistence and explicit risk domains."""
    market = market.sort_values("date").reset_index(drop=True).copy()
    industry = industry.sort_values(["industry", "date"]).reset_index(drop=True).copy()

    market["persistence_days"] = _streak(
        pd.to_numeric(market["concentration_pctile"], errors="coerce").ge(80)
    )
    market["advance_ratio"] = market.apply(
        lambda row: (
            row["up"] / (row["up"] + row["down"])
            if (row.get("up") or 0) + (row.get("down") or 0) > 0 else None
        ),
        axis=1,
    )
    market["breadth_label"] = market["breadth"].map(_breadth_label)
    market["direction_label"] = market["direction_score"].map(_direction_label)
    market["internal_top5_share"] = market["internal_top5"]

    persistence = pd.Series(index=industry.index, dtype="int64")
    for _, group in industry.groupby("industry", sort=False):
        ordered = group.sort_values("date")
        persistence.loc[ordered.index] = _streak(
            pd.to_numeric(ordered["share_pctile"], errors="coerce").ge(80)
        )
    industry["persistence_days"] = persistence.astype(int)
    industry["share_change_1d"] = (
        industry.groupby("industry", sort=False)["share"].diff()
    )
    industry["advance_ratio"] = industry.apply(
        lambda row: (
            row["up"] / (row["up"] + row["down"])
            if (row.get("up") or 0) + (row.get("down") or 0) > 0 else None
        ),
        axis=1,
    )
    industry["breadth_label"] = industry["breadth"].map(_breadth_label)
    industry["direction_label"] = industry["direction_score"].map(_direction_label)
    industry["internal_top5_share"] = industry["internal_top5"]

    hhi_by_date = dict(zip(market["date"], market["hhi"]))
    industry["hhi_contribution_pct"] = industry.apply(
        lambda row: (
            float(row["share"]) ** 2 / float(hhi_by_date.get(row["date"])) * 100
            if _finite(row.get("share")) and _finite(hhi_by_date.get(row["date"]))
            and float(hhi_by_date.get(row["date"])) > 0 else None
        ),
        axis=1,
    )
    industry["market_impact_score"] = (
        industry.groupby("date", sort=False)["share"]
        .rank(method="average", pct=True)
        .mul(100)
    )

    market_risk = market.apply(
        lambda row: assess_exit_risk(row, "market"), axis=1)
    market["risk_state"] = market_risk.map(lambda value: value["state"])
    market["risk_label"] = market_risk.map(lambda value: value["label"])
    market["risk_reasons"] = market_risk.map(lambda value: value["reasons"])
    market["risk_domains"] = market_risk.map(lambda value: value["domains"])

    industry_risk = industry.apply(
        lambda row: assess_exit_risk(row, "industry"), axis=1)
    industry["risk_state"] = industry_risk.map(lambda value: value["state"])
    industry["risk_label"] = industry_risk.map(lambda value: value["label"])
    industry["risk_reasons"] = industry_risk.map(lambda value: value["reasons"])
    industry["risk_domains"] = industry_risk.map(lambda value: value["domains"])
    return market, industry


def build_crowding_frames(
    db=None,
    cache: KlineCache | None = None,
    *,
    industry_map: dict[str, str] | None = None,
    stock_daily: pd.DataFrame | None = None,
    expected_stocks: int | None = None,
    min_coverage_ratio: float = MIN_COVERAGE_RATIO,
    min_stocks_per_date: int = MIN_STOCKS_PER_DATE,
    percentile_window: int = PERCENTILE_WINDOW,
    min_percentile_history: int = MIN_PERCENTILE_HISTORY,
):
    """计算增强市场/行业拥挤序列，兼容旧的二元 DataFrame 返回契约。"""
    # 保留 db 参数以兼容已有调用；核心计算本身不读取数据库。
    _ = db
    cache = cache or KlineCache()
    stock_daily = (
        compute_daily_amounts(cache)
        if stock_daily is None else stock_daily.copy()
    )
    if stock_daily.empty:
        return pd.DataFrame(), pd.DataFrame()

    codes = stock_daily["code"].unique().tolist()
    if industry_map is None:
        industry_map = load_industry_map(codes)
    stock_daily["industry"] = stock_daily["code"].map(
        lambda code: industry_map.get(code, "其他")
    )

    declared_codes = (
        int(expected_stocks)
        if expected_stocks is not None
        else len((cache._cache or {}).get("codes") or ())
    )
    coverage = compute_coverage(stock_daily, expected_stocks=declared_codes)
    valid_coverage = coverage[
        coverage["stocks"].ge(min_stocks_per_date)
        & coverage["coverage"].ge(min_coverage_ratio)
    ]
    valid_dates = set(valid_coverage["date"])
    stock_daily = stock_daily[
        stock_daily["date"].isin(valid_dates) & stock_daily["amount"].gt(0)
    ].copy()
    if stock_daily.empty:
        return pd.DataFrame(), pd.DataFrame()
    stock_daily["date"] = stock_daily["date"].dt.strftime("%Y%m%d")
    valid_coverage = valid_coverage.copy()
    valid_coverage["date"] = valid_coverage["date"].dt.strftime("%Y%m%d")

    ind_amounts = (
        stock_daily.groupby(["date", "industry"], as_index=False)["amount"].sum()
    )
    totals = stock_daily.groupby("date")["amount"].sum().rename("total")
    ind_amounts = ind_amounts.merge(totals, on="date")
    ind_amounts["share"] = ind_amounts["amount"] / ind_amounts["total"]

    expected_by_industry = (
        stock_daily[["code", "industry"]].drop_duplicates()
        .groupby("industry")["code"].nunique().to_dict()
    )
    industry_rows: list[dict[str, Any]] = []
    for (date, industry_name), group in stock_daily.groupby(["date", "industry"], sort=True):
        metrics = compute_scope_metrics(group, INDUSTRY_TOP_N)
        industry_rows.append({
            "date": date,
            "industry": industry_name,
            **metrics,
            "eligible_stocks": int(expected_by_industry.get(industry_name, 0)),
        })
    industry = pd.DataFrame(industry_rows)
    industry = industry.merge(
        ind_amounts[["date", "industry", "amount", "share"]],
        on=["date", "industry"],
        how="left",
    )
    industry["coverage"] = (
        industry["traded_stocks"] / industry["eligible_stocks"].where(
            industry["eligible_stocks"] > 0
        )
    ).clip(upper=1)
    industry = add_identity_overlap(
        industry, identity_col="leader_codes", group_col="industry", lookback=5
    )
    industry = add_causal_industry_features(
        industry, window=percentile_window, min_history=min_percentile_history
    )

    market_rows: list[dict[str, Any]] = []
    for date, group in stock_daily.groupby("date", sort=True):
        metrics = compute_scope_metrics(group, MARKET_TOP_N)
        shares = (
            ind_amounts[ind_amounts["date"] == date]["share"]
            .sort_values(ascending=False)
        )
        amounts = group.groupby("code")["amount"].sum().sort_values(ascending=False)
        total = float(amounts.sum()) or 1.0
        market_rows.append({
            "date": date,
            "total_amount": round(total),
            "cr5": round(float(shares.head(5).sum()), 4),
            "cr10": round(float(shares.head(10).sum()), 4),
            "hhi": round(float((shares ** 2).sum()), 6),
            "top10_stock_share": round(float(amounts.head(10).sum() / total), 4),
            "top50_stock_share": round(float(amounts.head(50).sum() / total), 4),
            **metrics,
        })
    market = pd.DataFrame(market_rows)
    market = market.merge(
        valid_coverage[["date", "stocks", "expected_stocks", "coverage"]],
        on="date",
        how="left",
    )
    market = add_identity_overlap(
        market, identity_col="leader_codes", lookback=5
    )
    market = add_causal_market_features(
        market, window=percentile_window, min_history=min_percentile_history
    )
    return market, industry


def apply_external_evidence(
    market: pd.DataFrame,
    industry: pd.DataFrame,
    snapshot: dict[str, Any],
    industry_map: dict[str, str],
    *,
    scheme: str = "sw",
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Attach latest direct evidence without rewriting historical observations."""
    from crowding_external import aggregate_external_by_industry

    market = market.copy()
    industry = industry.copy()
    industry["external_evidence"] = pd.Series(
        [None] * len(industry), index=industry.index, dtype="object")
    numeric_keys = (
        "direct_position_score", "external_fragility_score",
        "margin_float_pct", "margin_change_pct", "fund_float_pct",
        "etf_share_change", "spread_bps", "bid_depth_to_amount_bps",
        "margin_coverage", "fund_coverage", "margin_turnover_days",
        "fund_turnover_days", "etf_share_change_pct",
        "margin_turnover_pctile", "fund_turnover_pctile",
        "etf_inflow_pctile", "etf_exit_pctile",
    )
    for key in numeric_keys:
        if key not in industry.columns:
            industry[key] = float("nan")
    industry["direct_position_domains"] = pd.Series(
        [None] * len(industry), index=industry.index, dtype="object")
    market["external_evidence"] = pd.Series(
        [None] * len(market), index=market.index, dtype="object")
    for key in ("direct_position_score", "external_fragility_score"):
        if key not in market.columns:
            market[key] = float("nan")

    names = industry["industry"].dropna().unique().tolist()
    aggregated, summary = aggregate_external_by_industry(
        snapshot, industry_map, names, scheme=scheme)
    if market.empty or industry.empty:
        return market, industry, summary

    latest_date = str(market.iloc[-1]["date"])
    latest_indexes = industry.index[industry["date"].eq(latest_date)]
    evidence_by_index: dict[Any, dict[str, Any]] = {}
    for idx in latest_indexes:
        name = str(industry.at[idx, "industry"])
        evidence = dict(aggregated.get(name) or {})
        eligible_value = (
            industry.at[idx, "eligible_stocks"]
            if "eligible_stocks" in industry.columns else None
        )
        eligible = int(eligible_value) if _finite(eligible_value) else 0
        amount = _optional_float(industry.at[idx, "amount"])
        margin_count = int(evidence.get("margin_count") or 0)
        fund_count = int(evidence.get("fund_count") or 0)
        margin_coverage = margin_count / eligible if eligible > 0 else None
        fund_coverage = fund_count / eligible if eligible > 0 else None
        evidence["margin_coverage"] = margin_coverage
        evidence["fund_coverage"] = fund_coverage
        evidence["margin_turnover_days"] = (
            float(evidence["margin_balance"]) / amount
            if amount and margin_coverage is not None
            and margin_coverage >= 0.25
            and _finite(evidence.get("margin_balance")) else None
        )
        evidence["fund_turnover_days"] = (
            float(evidence["fund_hold_mcap"]) / amount
            if amount and fund_coverage is not None
            and fund_coverage >= 0.20
            and _finite(evidence.get("fund_hold_mcap")) else None
        )
        etf_shares = _optional_float(evidence.get("etf_shares"))
        etf_change = _optional_float(evidence.get("etf_share_change"))
        previous_etf_shares = (
            etf_shares - etf_change
            if etf_shares is not None and etf_change is not None else None
        )
        evidence["etf_share_change_pct"] = (
            etf_change / previous_etf_shares * 100
            if previous_etf_shares is not None and previous_etf_shares > 0
            else None
        )
        evidence_by_index[idx] = evidence

    def add_cross_sectional_percentile(
        source: str,
        target: str,
        *,
        predicate: Callable[[float], bool] | None = None,
        reverse: bool = False,
    ) -> None:
        values = {
            idx: float(evidence[source])
            for idx, evidence in evidence_by_index.items()
            if _finite(evidence.get(source))
            and (predicate is None or predicate(float(evidence[source])))
        }
        if len(values) < 5:
            for evidence in evidence_by_index.values():
                evidence[target] = None
            return
        ranked = pd.Series(values, dtype="float64").rank(
            method="average", pct=True)
        if reverse:
            ranked = 1.0 - ranked + 1.0 / len(ranked)
        for idx, evidence in evidence_by_index.items():
            evidence[target] = (
                round(float(ranked.loc[idx]) * 100, 1)
                if idx in ranked.index else None
            )

    add_cross_sectional_percentile(
        "margin_turnover_days", "margin_turnover_pctile")
    add_cross_sectional_percentile(
        "fund_turnover_days", "fund_turnover_pctile")
    add_cross_sectional_percentile(
        "etf_share_change_pct", "etf_inflow_pctile",
        predicate=lambda value: value > 0)
    add_cross_sectional_percentile(
        "etf_share_change_pct", "etf_exit_pctile",
        predicate=lambda value: value < 0, reverse=True)

    for idx, evidence in evidence_by_index.items():
        direct_domains: list[str] = []
        direct_components: list[float] = []
        margin_components = [
            value for value in (
                evidence.get("margin_turnover_pctile"),
                evidence.get("margin_change_pctile")
                if _finite(evidence.get("margin_change_pct"))
                and float(evidence["margin_change_pct"]) > 0 else None,
            )
            if _finite(value)
        ]
        if margin_components:
            direct_domains.append("融资余额")
            direct_components.append(
                sum(float(value) for value in margin_components)
                / len(margin_components)
            )
        if _finite(evidence.get("fund_turnover_pctile")):
            direct_domains.append("基金披露持仓")
            direct_components.append(float(evidence["fund_turnover_pctile"]))
        if _finite(evidence.get("etf_inflow_pctile")):
            direct_domains.append("ETF份额净增")
            direct_components.append(float(evidence["etf_inflow_pctile"]))
        evidence["direct_position_domains"] = direct_domains
        evidence["direct_position_score"] = (
            round(sum(direct_components) / len(direct_components), 1)
            if direct_components else None
        )

        industry.at[idx, "external_evidence"] = evidence
        for key in numeric_keys:
            industry.at[idx, key] = evidence.get(key)
        industry.at[idx, "direct_position_domains"] = direct_domains
        assessment = assess_exit_risk(industry.loc[idx], "industry")
        industry.at[idx, "risk_state"] = assessment["state"]
        industry.at[idx, "risk_label"] = assessment["label"]
        industry.at[idx, "risk_reasons"] = assessment["reasons"]
        industry.at[idx, "risk_domains"] = assessment["domains"]

    latest = industry.loc[latest_indexes]
    weights = pd.to_numeric(latest["share"], errors="coerce").fillna(0)
    for key in ("direct_position_score", "external_fragility_score"):
        values = pd.to_numeric(latest.get(key), errors="coerce")
        valid = values.notna() & weights.gt(0)
        market.at[market.index[-1], key] = (
            float((values[valid] * weights[valid]).sum() / weights[valid].sum())
            if valid.any() else None
        )
    market.at[market.index[-1], "external_evidence"] = summary
    summary = dict(summary)
    summary["industry_direct_evidence_count"] = int(
        pd.to_numeric(
            latest["direct_position_score"], errors="coerce").notna().sum())
    summary["industry_fragility_evidence_count"] = int(
        pd.to_numeric(
            latest["external_fragility_score"], errors="coerce").notna().sum())
    summary["industry_count"] = int(len(latest))
    market.at[market.index[-1], "external_evidence"] = summary
    assessment = assess_exit_risk(market.iloc[-1], "market")
    for key, output in (
        ("risk_state", "state"),
        ("risk_label", "label"),
        ("risk_reasons", "reasons"),
        ("risk_domains", "domains"),
    ):
        market.at[market.index[-1], key] = assessment[output]
    return market, industry, summary


def _label(date: str) -> str:
    return f"{int(date[4:6])}月{int(date[6:8])}日"


def _full_label(date: str) -> str:
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
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _atomic_json_dump(payload: dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".crowding-detail-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                _json_safe(payload), handle, ensure_ascii=False, indent=2,
                allow_nan=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _frontend_top_stocks(row: dict[str, Any]) -> list[dict[str, Any]]:
    total = float(row.get("amount") or 0)
    result = []
    for stock in row.get("top_stocks") or []:
        stock = dict(stock)
        amount = _optional_float(stock.get("amount"), 0)
        stock["share"] = (
            float(amount) / total if amount is not None and total > 0 else None)
        stock["change_pct"] = stock.get("return_pct")
        result.append(stock)
    return result


def _top_level_exit_state(
    market_row: dict[str, Any],
    industries: list[dict[str, Any]],
) -> tuple[str, str, list[str]]:
    state = str(market_row.get("risk_state") or "unknown")
    label = str(market_row.get("risk_label") or "证据不足")
    reasons = list(market_row.get("risk_reasons") or [])
    rank = {"unknown": 0, "normal": 1, "watch": 2, "fragile": 3, "unwind": 4}
    # A tiny industry's local stress must not turn the whole-market badge red.
    material = [
        row for row in industries
        if float(row.get("hhi_contribution_pct") or 0) >= 3
        or float(row.get("share") or 0) >= 0.03
    ]
    for row in material:
        candidate = str(row.get("risk_state") or "unknown")
        if rank.get(candidate, 0) > rank.get(state, 0):
            state = candidate
            label = str(row.get("risk_label") or candidate)
            reasons = [
                f"{row.get('industry')}：{reason}"
                for reason in (row.get("risk_reasons") or [])
            ]
    return state, label, reasons


def build_crowding_payload(
    market: pd.DataFrame,
    industry: pd.DataFrame,
    *,
    external_summary: dict[str, Any] | None = None,
    scheme: str = "sw",
    scheme_label: str = "申万一级",
    classification: dict[str, Any] | None = None,
    n_dates: int = 250,
) -> dict[str, Any]:
    """Create the frontend snapshot while retaining causal daily market history."""
    if market.empty:
        return {
            "schema_version": 2,
            "scheme": scheme,
            "scheme_label": scheme_label,
            "dates": [],
            "market": [],
            "industries": [],
        }
    dates = market["date"].astype(str).tail(n_dates).tolist()
    market_tail = market[market["date"].isin(dates)].sort_values("date")
    industry_tail = industry[industry["date"].isin(dates)].copy()
    latest_date = dates[-1]

    industries: list[dict[str, Any]] = []
    for name, group in industry_tail.groupby("industry", sort=True):
        ordered = group.sort_values("date")
        latest_candidates = ordered[ordered["date"].eq(latest_date)]
        if latest_candidates.empty:
            continue
        row = latest_candidates.iloc[-1].to_dict()
        by_date = {str(item["date"]): item for item in ordered.to_dict("records")}
        row["daily_shares"] = [
            by_date.get(date, {}).get("share") for date in dates]
        row["daily_amounts"] = [
            by_date.get(date, {}).get("amount", 0) for date in dates]
        recent_shares = [
            value for value in row["daily_shares"][-5:] if _finite(value)]
        row["avg5"] = (
            sum(float(value) for value in recent_shares) / len(recent_shares)
            if recent_shares else row.get("share")
        )
        row["pctile"] = row.get("share_pctile")
        row["top_stocks"] = _frontend_top_stocks(row)
        industries.append(row)
    industries.sort(
        key=lambda row: (
            {"unwind": 4, "fragile": 3, "watch": 2, "normal": 1}.get(
                str(row.get("risk_state")), 0),
            float(row.get("market_impact_score") or 0),
        ),
        reverse=True,
    )

    latest_market = market_tail.iloc[-1].to_dict()
    concentration = _optional_float(
        latest_market.get("concentration_pctile"), 1)
    if concentration is None:
        concentration_state = {
            "state": "unknown", "label": "数据积累"}
    elif concentration >= 95:
        concentration_state = {
            "state": "watch", "label": "高度集中"}
    elif concentration >= 80:
        concentration_state = {
            "state": "watch", "label": "集中偏高"}
    else:
        concentration_state = {
            "state": "normal", "label": "分布常态"}

    exit_state, exit_label, exit_reasons = _top_level_exit_state(
        latest_market, industries)
    source = max(
        industries,
        key=lambda row: float(row.get("hhi_contribution_pct") or 0),
        default=None,
    )
    conclusion = (
        f"成交集中处于“{concentration_state['label']}”"
        + (
            f"，主要来源为{source.get('industry')}（成交占比"
            f"{float(source.get('share') or 0) * 100:.1f}%）"
            if source else ""
        )
        + f"；退出危险状态为“{exit_label}”。"
    )
    external_summary = external_summary or {}
    available = int(external_summary.get("available_count") or 0)
    requested = int(external_summary.get("requested_count") or 0)
    available_core = int(
        external_summary.get("available_core_count") or 0)
    requested_core = int(
        external_summary.get("requested_core_count") or 0)
    direct_count = int(
        external_summary.get("industry_direct_evidence_count") or 0)
    industry_count = int(external_summary.get("industry_count") or 0)
    confidence_prefix = {
        "high": "高", "medium": "中", "low": "低",
    }.get(str(external_summary.get("confidence")), "基础代理可用")
    if requested_core:
        confidence_label = (
            f"{confidence_prefix} · 核心源 {available_core}/{requested_core}"
            + (
                f" · 行业直接证据 {direct_count}/{industry_count}"
                if industry_count else ""
            )
        )
    elif requested:
        confidence_label = f"{confidence_prefix} · 外部源 {available}/{requested}"
    else:
        confidence_label = "基础代理可用 · 外部证据不足"
    coverage = {
        "covered": int(latest_market.get("stocks") or 0),
        "total": int(latest_market.get("expected_stocks") or 0),
        "ratio": _optional_float(latest_market.get("coverage"), 4),
    }
    return _json_safe({
        "schema_version": 2,
        "model_version": "crowding-risk-v2",
        "scheme": scheme,
        "scheme_label": scheme_label,
        "trade_date": latest_date,
        "as_of": latest_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dates": [
            {"label": _label(date), "full_label": _full_label(date)}
            for date in dates
        ],
        "market": market_tail.to_dict("records"),
        "industries": industries,
        "concentration_state": concentration_state,
        "exit_risk_state": {"state": exit_state, "label": exit_label},
        "exit_risk_reason": "；".join(exit_reasons[:4]) if exit_reasons else "证据不足",
        "conclusion": conclusion,
        "coverage": coverage,
        "confidence": {
            "level": external_summary.get("confidence") or "low",
            "label": confidence_label,
        },
        "external": external_summary,
        "data_quality": {
            "coverage": coverage,
            "classification": classification or {},
            "causal_percentiles": True,
            "missing_external_is_neutral": False,
            "proxy_notice": (
                "成交集中来自OHLCV代理；融资、ETF和披露持仓按来源日期单独展示，"
                "缺失不会按零值处理。"
            ),
        },
    })


def update_crowding() -> int:
    """Rebuild all heatmap classification schemes from one OHLCV snapshot."""
    db = get_db()
    cache = KlineCache()
    cache._load()
    stock_daily = compute_daily_amounts(cache)
    if stock_daily.empty:
        return 0
    # 成交动能的逐股相对量能与行业分类无关，只准备一次，避免三个方案重复扫描 K 线。
    from turnover_momentum import (
        build_turnover_momentum_payload,
        prepare_stock_momentum_daily,
    )
    momentum_stock_daily = prepare_stock_momentum_daily(stock_daily)
    codes = stock_daily["code"].astype(str).unique().tolist()
    active_codes = list(cache._cache.get("codes") or ()) or codes
    schemes = load_crowding_industry_schemes(
        codes, classification_codes=active_codes)
    expected_stocks = len(active_codes)

    frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for scheme, config in schemes.items():
        market, industry = build_crowding_frames(
            db,
            cache=cache,
            industry_map=config["map"],
            stock_daily=stock_daily,
            expected_stocks=expected_stocks,
        )
        if market.empty:
            continue
        frames[scheme] = add_presentation_features(market, industry)
    if "sw" not in frames:
        return 0

    market_dates = frames["sw"][0]["date"].astype(str).tolist()
    latest_stock_daily = stock_daily[
        stock_daily["date"].dt.strftime("%Y%m%d").eq(market_dates[-1])
    ]
    top_codes = (
        latest_stock_daily.nlargest(100, "amount")["code"]
        .astype(str).drop_duplicates().tolist()
    )
    snapshot: dict[str, Any] | None = None
    refresh_error: str | None = None
    try:
        from crowding_external import refresh_external_snapshot
        snapshot = refresh_external_snapshot(
            codes,
            market_dates[-1],
            previous_trade_date=(
                market_dates[-2] if len(market_dates) > 1 else None
            ),
            top_codes=top_codes,
        )
    except Exception as exc:
        refresh_error = str(exc)[:240]

    final_frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for scheme, (market, industry) in frames.items():
        config = schemes[scheme]
        external_summary: dict[str, Any]
        if snapshot is not None:
            try:
                market, industry, external_summary = apply_external_evidence(
                    market,
                    industry,
                    snapshot,
                    config["map"],
                    scheme=scheme,
                )
            except Exception as exc:
                external_summary = {
                    "scheme": scheme,
                    "confidence": "low",
                    "available_count": 0,
                    "requested_count": 0,
                    "error": str(exc)[:240],
                    "limitations": ["外部证据聚合失败；核心成交代理仍可用"],
                }
        else:
            external_summary = {
                "scheme": scheme,
                "confidence": "low",
                "available_count": 0,
                "requested_count": 0,
                "error": refresh_error,
                "limitations": ["外部证据更新失败；核心成交代理仍可用"],
            }

        payload = build_crowding_payload(
            market,
            industry,
            external_summary=external_summary,
            scheme=scheme,
            scheme_label=config["label"],
            classification=config["classification"],
        )
        suffix = config["suffix"]
        _atomic_json_dump(
            payload,
            os.path.join(DATA_DIR, f"crowding_detail{suffix}.json"),
        )
        momentum_payload = build_turnover_momentum_payload(
            momentum_stock_daily,
            market,
            industry,
            industry_map=config["map"],
            scheme=scheme,
            scheme_label=config["label"],
            classification=config["classification"],
        )
        _atomic_json_dump(
            momentum_payload,
            os.path.join(DATA_DIR, f"capital_flow_v2{suffix}.json"),
        )
        final_frames[scheme] = (market, industry)

    market, industry = final_frames["sw"]
    db.replace_crowding(
        market.to_dict("records"),
        industry.to_dict("records"),
    )
    return len(market)
