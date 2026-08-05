#!/usr/bin/env python3
"""成交动能 v2：用量、价、方向、广度和持续性解释行业交易状态。

日线 OHLCV 无法辨认真实主动买卖方。本模块把 ``direction_score`` 明确定义为
“涨跌成交差率”代理，并且将危险与涨跌方向分开表达。所有历史分位和滚动基线
都只使用当前日期之前的数据；当前日只作为待比较的观测值。

计算入口复用 ``crowding`` 已经生成的逐股、市场和行业 DataFrame，不再次扫描
K 线缓存。逐股相对成交量只需在三个行业分类方案之前准备一次。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from datetime import datetime
import math
from typing import Any, Iterable

import pandas as pd


MODEL_VERSION = "turnover-momentum-v2"
PERCENTILE_WINDOW = 250
MIN_PERCENTILE_HISTORY = 20
PARTICIPATION_WINDOW = 60
PRICE_VOL_WINDOW = 20
STOCK_ACTIVITY_WINDOW = 20
OUTPUT_DATES = 80  # 校准概率需要 >=60 个样本交易日(heatmap_opportunity.minimum_days),留未来收益窗口余量
RETURN_NOISE_PCT = 0.05
STRENGTH_NOISE = 0.15


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
    return float(numerator) / denominator if denominator > 0 else None


def effective_participant_count(weights: Iterable[Any]) -> float | None:
    """Concentration-adjusted participant count, ``(Σw)² / Σw²``."""
    usable = [
        float(value) for value in weights
        if _finite(value) and float(value) > 0
    ]
    if not usable:
        return None
    total = sum(usable)
    squared = sum(value * value for value in usable)
    return total * total / squared if squared > 0 else None


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


def causal_percentile(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
    min_periods: int = MIN_PERCENTILE_HISTORY,
) -> pd.Series:
    """ECDF percentile against prior observations only.

    This local implementation keeps the momentum model independent from the
    crowding module while preserving its causal percentile convention.
    """
    if window < 1:
        raise ValueError("window must be positive")
    if min_periods < 1 or min_periods > window:
        raise ValueError("min_periods must be between 1 and window")
    ordered: list[float] = []
    history: deque[float | None] = deque()
    output: list[float | None] = []
    for raw in pd.to_numeric(series, errors="coerce").tolist():
        value = float(raw) if _finite(raw) else None
        if value is not None and len(ordered) >= min_periods:
            output.append(round(
                bisect_right(ordered, value) / len(ordered) * 100, 1))
        else:
            output.append(None)
        history.append(value)
        if value is not None:
            insort(ordered, value)
        if len(history) > window:
            expired = history.popleft()
            if expired is not None:
                ordered.pop(bisect_left(ordered, expired))
    return pd.Series(output, index=series.index, dtype="float64")


def causal_median_ratio(
    series: pd.Series,
    window: int,
    min_periods: int,
) -> pd.Series:
    """Current value / prior rolling median; the current value is excluded."""
    numeric = pd.to_numeric(series, errors="coerce")
    baseline = numeric.shift(1).rolling(
        window, min_periods=min_periods).median()
    return numeric / baseline.where(baseline > 0)


def prepare_stock_momentum_daily(
    stock_daily: pd.DataFrame,
    *,
    window: int = STOCK_ACTIVITY_WINDOW,
    min_history: int = 5,
    max_dates: int = PERCENTILE_WINDOW + PARTICIPATION_WINDOW + 50,
) -> pd.DataFrame:
    """Add causal per-stock relative turnover once for all classifications.

    Only the recent model horizon is retained. It includes enough warm-up data
    for the 250-day industry percentiles and avoids three full-cache scans.
    """
    if stock_daily.empty:
        return stock_daily.copy()
    out = stock_daily.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["code"] = out["code"].astype(str).str.zfill(6)
    out["amount"] = pd.to_numeric(out["amount"], errors="coerce")
    out["return"] = pd.to_numeric(out.get("return"), errors="coerce")
    out = out.dropna(subset=["date", "code", "amount"])
    all_dates = sorted(out["date"].drop_duplicates())
    if max_dates and len(all_dates) > max_dates:
        out = out[out["date"].isin(all_dates[-max_dates:])].copy()
    out = out.sort_values(["code", "date"]).reset_index(drop=True)
    prior_median = out.groupby("code", sort=False)["amount"].transform(
        lambda values: values.shift(1).rolling(
            window, min_periods=min_history).median()
    )
    out["stock_rvol_20"] = out["amount"] / prior_median.where(
        prior_median > 0)
    return out


def _rolling_weighted_direction(group: pd.DataFrame, window: int = 5) -> pd.Series:
    numerator = (
        pd.to_numeric(group["direction_score"], errors="coerce")
        * pd.to_numeric(group["amount"], errors="coerce")
    )
    denominator = pd.to_numeric(group["amount"], errors="coerce")
    return (
        numerator.rolling(window, min_periods=2).sum()
        / denominator.rolling(window, min_periods=2).sum().where(
            denominator.rolling(window, min_periods=2).sum() > 0
        )
    )


def _momentum_state(row: pd.Series) -> tuple[str, str]:
    price = (
        float(row.get("price_change_pct"))
        if _finite(row.get("price_change_pct")) else 0.0
    )
    strength = row.get("price_strength")
    ratio = row.get("share_ratio_60")
    quiet_price = abs(price) < RETURN_NOISE_PCT
    if _finite(strength):
        quiet_price = quiet_price and abs(float(strength)) < STRENGTH_NOISE
    if quiet_price:
        return "balanced", "量价争夺"
    expanding = _finite(ratio) and float(ratio) >= 1.0
    if price > 0:
        return (
            ("expanding_up", "放量上涨")
            if expanding else ("contracting_up", "缩量上涨")
        )
    return (
        ("expanding_down", "放量下跌")
        if expanding else ("contracting_down", "缩量下跌")
    )


def _streak(values: Iterable[str]) -> list[int]:
    output: list[int] = []
    previous = None
    count = 0
    for value in values:
        count = count + 1 if value == previous else 1
        previous = value
        output.append(count)
    return output


def classify_momentum_risk(
    row: pd.Series | dict[str, Any],
    *,
    previous_pattern: str | None = None,
    previous_level: str | None = None,
) -> dict[str, Any]:
    """Classify danger using at least two independent evidence domains.

    A single extreme metric never produces a warning. Three domains produce a
    warning, while four domains or a repeated three-domain pattern produce
    danger. This intentionally separates risk severity from up/down color.
    """
    get = row.get
    price = (
        float(get("price_change_pct"))
        if _finite(get("price_change_pct")) else 0.0
    )
    activity = get("activity_pctile")
    response = get("price_response_pctile")
    result_pctile = get("price_result_pctile")
    active_pctile = get("active_breadth_pctile")
    active_change = get("active_breadth_change_3d")
    direction = get("direction_score")
    direction_change = get("direction_change_3d")
    breadth = get("breadth")
    acceleration = get("acceleration")
    acceleration_pctile = get("acceleration_pctile")
    concentration = get("internal_top5_pctile")
    efficiency = get("efficiency_gap")

    candidates: list[tuple[str, str, dict[str, str]]] = []

    selloff: dict[str, str] = {}
    if price < 0 and _finite(result_pctile) and float(result_pctile) <= 20:
        selloff["price"] = "价格结果处于自身历史后20%"
    if _finite(activity) and float(activity) >= 80:
        selloff["effort"] = f"相对成交参与处于 {float(activity):.0f} 分位"
    if (_finite(breadth) and float(breadth) <= -0.20) or (
        _finite(active_pctile) and float(active_pctile) <= 20 and price < 0
    ):
        selloff["participation"] = "下跌广度较大或同向活跃参与明显转弱"
    if (
        _finite(acceleration)
        and float(acceleration) < 0
        and _finite(acceleration_pctile)
        and float(acceleration_pctile) <= 20
    ):
        selloff["acceleration"] = "负向动能加速度处于历史低位"
    if price < 0:
        candidates.append(("selloff", "放量杀跌", selloff))

    exhaustion: dict[str, str] = {}
    if (
        price > 0
        and _finite(activity)
        and float(activity) >= 80
        and _finite(efficiency)
        and float(efficiency) <= -30
    ):
        exhaustion["effort_result"] = "高成交努力未换来相称价格响应"
    if _finite(active_change) and float(active_change) <= -0.08:
        exhaustion["participation"] = (
            f"同向活跃参与3日下降 {abs(float(active_change)) * 100:.1f} 个百分点"
        )
    if (
        (_finite(direction_change) and float(direction_change) <= -0.12)
        or (_finite(direction) and float(direction) < 0)
    ):
        exhaustion["direction"] = "方向成交差率明显衰减"
    if (
        _finite(concentration)
        and float(concentration) >= 80
        and _finite(breadth)
        and float(breadth) < 0.15
    ):
        exhaustion["concentration"] = "少数股票成交贡献偏高且等权广度不足"
    if price > 0:
        candidates.append(
            ("upside_exhaustion", "上涨衰竭", exhaustion))

    narrow: dict[str, str] = {}
    if price > 0 and _finite(activity) and float(activity) <= 20:
        narrow["effort"] = "上涨但相对成交参与处于历史后20%"
    if (
        _finite(active_pctile)
        and float(active_pctile) <= 30
    ):
        narrow["participation"] = "同向活跃参与偏低"
    if _finite(concentration) and float(concentration) >= 80:
        narrow["concentration"] = "成交贡献集中于少数股票"
    if price > 0:
        candidates.append(("narrow_advance", "窄幅缩量上涨", narrow))

    vacuum: dict[str, str] = {}
    if price < 0 and _finite(activity) and float(activity) <= 20:
        vacuum["effort"] = "下跌发生在低成交参与环境"
    if _finite(response) and float(response) >= 80:
        vacuum["impact"] = "少量成交造成的价格响应处于历史高位"
    if _finite(breadth) and float(breadth) <= -0.20:
        vacuum["participation"] = "下跌覆盖面较广"
    if price < 0:
        candidates.append(
            ("liquidity_vacuum", "下跌流动性真空", vacuum))

    concentrated: dict[str, str] = {}
    if (
        price > 0
        and _finite(direction)
        and float(direction) >= 0.15
    ):
        concentrated["direction"] = "成交额加权方向明显向上"
    if _finite(breadth) and float(breadth) < 0.10:
        concentrated["participation"] = "等权涨跌广度未同步确认"
    if _finite(concentration) and float(concentration) >= 80:
        concentrated["concentration"] = "Top5成交贡献处于历史高位"
    if price > 0:
        candidates.append(
            ("concentrated_drive", "少数权重驱动", concentrated))

    pattern, pattern_label, domains = max(
        candidates,
        key=lambda candidate: len(candidate[2]),
        default=("normal", "风险正常", {}),
    )
    evidence_count = len(domains)
    if evidence_count < 2:
        level = "normal"
        label = "正常"
        pattern = "normal"
        pattern_label = "风险正常"
        reasons = ["未出现至少两个相互独立的危险证据"]
    elif evidence_count == 2:
        level, label = "watch", "观察"
        reasons = list(domains.values()) + ["已有两个独立风险域，等待持续或价格确认"]
    elif evidence_count == 3:
        if previous_pattern == pattern and previous_level in {
            "watch", "warning", "danger"
        }:
            level, label = "danger", "危险"
            reasons = list(domains.values()) + ["相同风险形态连续出现，危险得到确认"]
        else:
            level, label = "warning", "警告"
            reasons = list(domains.values()) + ["三个独立风险域同时确认"]
    else:
        level, label = "danger", "危险"
        reasons = list(domains.values()) + ["四个独立风险域同时确认"]
    return {
        "risk_level": level,
        "risk_label": label,
        "risk_pattern": pattern,
        "risk_pattern_label": pattern_label,
        "risk_reasons": reasons,
        "risk_domains": domains,
        "risk_evidence_count": evidence_count,
    }


def _aggregate_active_participation(
    stock_daily: pd.DataFrame,
    industry_map: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate relative-volume participants for industries and market."""
    if stock_daily.empty:
        empty = pd.DataFrame(columns=[
            "date", "industry", "active_up", "active_down", "active_stocks",
        ])
        return empty, empty.drop(columns="industry")
    stocks = stock_daily.copy()
    stocks["date"] = pd.to_datetime(
        stocks["date"], errors="coerce").dt.strftime("%Y%m%d")
    stocks["industry"] = stocks["code"].map(
        lambda code: industry_map.get(str(code).zfill(6), "其他"))
    rvol = pd.to_numeric(stocks.get("stock_rvol_20"), errors="coerce")
    returns = pd.to_numeric(stocks.get("return"), errors="coerce")
    active = rvol.ge(1.0)
    stocks["active_up"] = (active & returns.gt(0.0001)).astype(int)
    stocks["active_down"] = (active & returns.lt(-0.0001)).astype(int)
    stocks["active_stocks"] = active.astype(int)
    stocks["valid_rvol_stocks"] = rvol.notna().astype(int)
    # N_eff uses abnormal turnover contributions, so one dominant leader is
    # not misrepresented as equally broad participation. Exact-RVOL=1 rows
    # have zero abnormal weight and fall back to raw amount within that side.
    excess_weight = (
        pd.to_numeric(stocks["amount"], errors="coerce").fillna(0)
        * (rvol - 1).clip(lower=0).fillna(0)
    )
    amount = pd.to_numeric(stocks["amount"], errors="coerce").fillna(0)
    for side, mask in (
        ("up", active & returns.gt(0.0001)),
        ("down", active & returns.lt(-0.0001)),
    ):
        side_weight = excess_weight.where(mask, 0.0)
        side_amount = amount.where(mask, 0.0)
        stocks[f"active_{side}_weight"] = side_weight
        stocks[f"active_{side}_weight_sq"] = side_weight.pow(2)
        stocks[f"active_{side}_amount"] = side_amount
        stocks[f"active_{side}_amount_sq"] = side_amount.pow(2)
    columns = [
        "active_up", "active_down", "active_stocks", "valid_rvol_stocks",
        "active_up_weight", "active_up_weight_sq",
        "active_up_amount", "active_up_amount_sq",
        "active_down_weight", "active_down_weight_sq",
        "active_down_amount", "active_down_amount_sq",
    ]
    industry = stocks.groupby(
        ["date", "industry"], as_index=False)[columns].sum()
    market = stocks.groupby("date", as_index=False)[columns].sum()
    return industry, market


def _enrich_scope(
    raw: pd.DataFrame,
    market_returns: dict[str, float],
    *,
    percentile_window: int,
    min_history: int,
    is_market: bool = False,
) -> pd.DataFrame:
    """Add causal momentum features to one market or industry history."""
    if raw.empty:
        return raw.copy()
    out = raw.sort_values("date").reset_index(drop=True).copy()
    amount_col = "total_amount" if is_market else "amount"
    ratio_window = PARTICIPATION_WINDOW
    out["share_ratio_60"] = causal_median_ratio(
        out[amount_col] if is_market else out["share"],
        ratio_window,
        min(10, min_history),
    )
    out["activity_pctile"] = causal_percentile(
        out["share_ratio_60"],
        percentile_window,
        min_history,
    )

    out["direction_score"] = pd.to_numeric(
        out.get("direction_score"), errors="coerce")
    out["direction_5d"] = _rolling_weighted_direction(
        out.rename(columns={amount_col: "amount"})
        if amount_col != "amount" else out
    )
    out["price_change_pct"] = pd.to_numeric(
        out.get("price_change_pct"), errors="coerce")
    market_series = out["date"].map(market_returns).fillna(0.0)
    out["excess_return_pct"] = (
        out["price_change_pct"] if is_market
        else out["price_change_pct"] - market_series
    )
    prior_vol = out["excess_return_pct"].shift(1).rolling(
        PRICE_VOL_WINDOW,
        min_periods=min(10, min_history),
    ).std(ddof=0)
    out["price_strength"] = (
        out["excess_return_pct"] / prior_vol.where(prior_vol > 0)
    )
    out["price_response_pctile"] = causal_percentile(
        out["price_strength"].abs(), percentile_window, min_history)
    out["price_result_pctile"] = causal_percentile(
        out["price_strength"], percentile_window, min_history)

    aligned = (
        out["direction_score"].mul(out["price_change_pct"]).gt(0)
        & out["direction_score"].abs().ge(0.02)
        & out["price_change_pct"].abs().ge(RETURN_NOISE_PCT)
    )
    out["coherence"] = aligned.astype(float).rolling(
        5, min_periods=3).mean()
    ratio_for_score = pd.to_numeric(
        out["share_ratio_60"], errors="coerce").clip(0.25, 4)
    out["momentum_score"] = (
        pd.to_numeric(out["price_strength"], errors="coerce").clip(-3, 3)
        * ratio_for_score.pow(0.5)
    )
    prior_impulse = out["momentum_score"].shift(1).ewm(
        span=3, adjust=False, min_periods=min(3, min_history)).mean()
    out["acceleration"] = out["momentum_score"] - prior_impulse
    out["acceleration_pctile"] = causal_percentile(
        out["acceleration"], percentile_window, min_history)

    eligible = pd.to_numeric(
        out.get("eligible_stocks"), errors="coerce")
    active_up = pd.to_numeric(out.get("active_up"), errors="coerce").fillna(0)
    active_down = pd.to_numeric(
        out.get("active_down"), errors="coerce").fillna(0)
    valid_rvol = pd.to_numeric(
        out.get("valid_rvol_stocks"), errors="coerce").fillna(0)
    out["active_coverage"] = valid_rvol / eligible.where(eligible > 0)
    positive = out["price_change_pct"].ge(0)
    out["active_participants"] = active_up.where(
        positive, active_down).astype(int)
    weight = pd.to_numeric(
        out.get("active_up_weight"), errors="coerce").where(
            positive,
            pd.to_numeric(out.get("active_down_weight"), errors="coerce"),
        )
    weight_sq = pd.to_numeric(
        out.get("active_up_weight_sq"), errors="coerce").where(
            positive,
            pd.to_numeric(out.get("active_down_weight_sq"), errors="coerce"),
        )
    fallback_weight = pd.to_numeric(
        out.get("active_up_amount"), errors="coerce").where(
            positive,
            pd.to_numeric(out.get("active_down_amount"), errors="coerce"),
        )
    fallback_weight_sq = pd.to_numeric(
        out.get("active_up_amount_sq"), errors="coerce").where(
            positive,
            pd.to_numeric(out.get("active_down_amount_sq"), errors="coerce"),
        )
    usable_weight = weight.where(weight.gt(0), fallback_weight)
    usable_weight_sq = weight_sq.where(weight.gt(0), fallback_weight_sq)
    out["effective_participants"] = (
        usable_weight.pow(2) / usable_weight_sq.where(usable_weight_sq > 0)
    )
    out["effective_participation_ratio"] = (
        out["effective_participants"] / eligible.where(eligible > 0)
    ).where(out["active_coverage"].ge(0.50))
    out["active_breadth"] = (
        out["active_participants"] / eligible.where(eligible > 0)
    ).where(out["active_coverage"].ge(0.50))
    out["active_direction_breadth"] = (
        (active_up - active_down) / eligible.where(eligible > 0)
    ).where(out["active_coverage"].ge(0.50))
    out["active_breadth_pctile"] = causal_percentile(
        out["active_breadth"], percentile_window, min_history)
    out["active_breadth_change_3d"] = (
        out["active_breadth"] - out["active_breadth"].shift(3))
    out["direction_change_3d"] = (
        out["direction_score"] - out["direction_score"].shift(3))
    out["efficiency_gap"] = (
        out["price_response_pctile"] - out["activity_pctile"])
    out["efficiency_label"] = out["efficiency_gap"].map(
        lambda value: (
            "高努力低响应" if _finite(value) and float(value) <= -30
            else "低努力大位移" if _finite(value) and float(value) >= 30
            else "量价匹配" if _finite(value) else "样本积累"
        )
    )

    states = out.apply(_momentum_state, axis=1)
    out["state"] = states.map(lambda value: value[0])
    out["state_label"] = states.map(lambda value: value[1])
    out["persistence"] = _streak(out["state"].tolist())

    risks: list[dict[str, Any]] = []
    previous_pattern = None
    previous_level = None
    for _, row in out.iterrows():
        risk = classify_momentum_risk(
            row,
            previous_pattern=previous_pattern,
            previous_level=previous_level,
        )
        risks.append(risk)
        previous_pattern = risk["risk_pattern"]
        previous_level = risk["risk_level"]
    for key in (
        "risk_level", "risk_label", "risk_pattern", "risk_pattern_label",
        "risk_reasons", "risk_domains", "risk_evidence_count",
    ):
        out[key] = [risk[key] for risk in risks]
    return out


def _latest_top_stocks(
    row: dict[str, Any],
    stock_lookup: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach relative activity to the existing amount-ranked stock list."""
    total = float(row.get("amount") or 0)
    output = []
    for raw in row.get("top_stocks") or []:
        stock = dict(raw)
        code = str(stock.get("code") or "").zfill(6)
        source = stock_lookup.get(code, {})
        amount = float(stock.get("amount") or 0)
        stock["code"] = code
        stock["share"] = amount / total if total > 0 else None
        stock["change_pct"] = stock.get("return_pct")
        stock["rvol_20"] = _number(source.get("stock_rvol_20"), 2)
        stock["active"] = bool(
            _finite(source.get("stock_rvol_20"))
            and float(source["stock_rvol_20"]) >= 1
        )
        output.append(stock)
    return output


SERIES_FIELDS = (
    "date", "amount", "share", "share_ratio_60", "activity_pctile",
    "direction_score", "direction_5d", "breadth", "active_breadth",
    "active_direction_breadth", "active_coverage",
    "active_participants", "effective_participants",
    "effective_participation_ratio", "eligible_stocks",
    "internal_top5", "price_change_pct", "excess_return_pct",
    "price_strength", "price_response_pctile", "price_result_pctile",
    "active_breadth_pctile", "persistence", "coherence",
    "momentum_score", "acceleration", "efficiency_gap", "state",
    "state_label", "risk_level", "risk_label", "risk_pattern",
)


def _series_records(frame: pd.DataFrame, n_dates: int) -> list[dict[str, Any]]:
    columns = [column for column in SERIES_FIELDS if column in frame.columns]
    return frame.sort_values("date").tail(n_dates)[columns].to_dict("records")


def _date_label(date: str) -> str:
    return f"{int(date[4:6])}月{int(date[6:8])}日"


def _full_date_label(date: str) -> str:
    return f"{date[:4]}年{int(date[4:6])}月{int(date[6:8])}日"


def build_turnover_momentum_payload(
    stock_daily: pd.DataFrame,
    market: pd.DataFrame,
    industry: pd.DataFrame,
    *,
    industry_map: dict[str, str],
    scheme: str = "sw",
    scheme_label: str = "申万一级",
    classification: dict[str, Any] | None = None,
    n_dates: int = OUTPUT_DATES,
    percentile_window: int = PERCENTILE_WINDOW,
    min_history: int = MIN_PERCENTILE_HISTORY,
) -> dict[str, Any]:
    """Build the v2 frontend payload from already-computed crowding frames."""
    if market.empty or industry.empty:
        return {
            "schema_version": 2,
            "model_version": MODEL_VERSION,
            "scheme": scheme,
            "scheme_label": scheme_label,
            "trade_date": None,
            "dates": [],
            "market": {},
            "industries": [],
            "data_quality": {"classification": classification or {}},
        }
    if "stock_rvol_20" not in stock_daily:
        stock_daily = prepare_stock_momentum_daily(stock_daily)

    market = market.copy()
    industry = industry.copy()
    market["date"] = market["date"].astype(str)
    industry["date"] = industry["date"].astype(str)
    model_horizon = percentile_window + PARTICIPATION_WINDOW + n_dates + 20
    available_dates = sorted(market["date"].unique())
    kept_dates = set(available_dates[-model_horizon:])
    market = market[market["date"].isin(kept_dates)].copy()
    industry = industry[industry["date"].isin(kept_dates)].copy()

    industry_active, market_active = _aggregate_active_participation(
        stock_daily, industry_map)
    industry = industry.merge(
        industry_active, on=["date", "industry"], how="left")
    market = market.merge(market_active, on="date", how="left")
    market["eligible_stocks"] = pd.to_numeric(
        market.get("expected_stocks"), errors="coerce")
    market_returns = {
        str(row["date"]): float(row.get("price_change_pct") or 0)
        for row in market.to_dict("records")
    }

    enriched_groups = []
    for _, raw_group in industry.groupby("industry", sort=True):
        enriched_groups.append(_enrich_scope(
            raw_group,
            market_returns,
            percentile_window=percentile_window,
            min_history=min_history,
        ))
    enriched_industry = pd.concat(
        enriched_groups, ignore_index=True) if enriched_groups else pd.DataFrame()
    enriched_market = _enrich_scope(
        market,
        market_returns,
        percentile_window=percentile_window,
        min_history=min_history,
        is_market=True,
    )
    latest_date = str(enriched_market["date"].max())
    latest_industry = enriched_industry[
        enriched_industry["date"].eq(latest_date)].copy()
    stock_dates = pd.to_datetime(
        stock_daily["date"], errors="coerce").dt.strftime("%Y%m%d")
    latest_stocks = stock_daily[stock_dates.eq(latest_date)]
    latest_stock_lookup = {
        str(item["code"]).zfill(6): item
        for item in latest_stocks.to_dict("records")
    }

    industries: list[dict[str, Any]] = []
    for name, group in enriched_industry.groupby("industry", sort=True):
        latest = group[group["date"].eq(latest_date)]
        if latest.empty:
            continue
        row = latest.iloc[-1].to_dict()
        row["series"] = _series_records(group, n_dates)
        row["top_stocks"] = _latest_top_stocks(
            row, latest_stock_lookup)
        industries.append(row)
    risk_rank = {"danger": 3, "warning": 2, "watch": 1, "normal": 0}
    industries.sort(
        key=lambda row: (
            risk_rank.get(str(row.get("risk_level")), 0),
            abs(float(row.get("momentum_score") or 0)),
            float(row.get("share") or 0),
        ),
        reverse=True,
    )

    latest_market = enriched_market[
        enriched_market["date"].eq(latest_date)].iloc[-1].to_dict()
    latest_market["series"] = _series_records(enriched_market, n_dates)
    state_rows = []
    for state, group in latest_industry.groupby("state", sort=True):
        state_rows.append({
            "state": state,
            "label": str(group.iloc[0]["state_label"]),
            "count": int(len(group)),
            "turnover_share": float(
                pd.to_numeric(group["share"], errors="coerce").fillna(0).sum()
            ),
        })
    risk_rows = []
    for level, group in latest_industry.groupby("risk_level", sort=True):
        risk_rows.append({
            "level": level,
            "label": str(group.iloc[0]["risk_label"]),
            "count": int(len(group)),
            "turnover_share": float(
                pd.to_numeric(group["share"], errors="coerce").fillna(0).sum()
            ),
        })
    latest_market["state_distribution"] = state_rows
    latest_market["risk_distribution"] = risk_rows
    latest_market["risk_industry_count"] = int(
        latest_industry["risk_level"].isin(
            ["watch", "warning", "danger"]).sum())
    latest_market["danger_industry_count"] = int(
        latest_industry["risk_level"].eq("danger").sum())
    latest_market["top_risks"] = [{
        "industry": row.get("industry"),
        "risk_level": row.get("risk_level"),
        "risk_label": row.get("risk_label"),
        "risk_pattern_label": row.get("risk_pattern_label"),
        "reasons": list(row.get("risk_reasons") or [])[:3],
    } for row in industries if row.get("risk_level") != "normal"][:5]
    latest_market["summary"] = (
        f"{latest_market.get('state_label', '量价状态未知')}"
        f" · 同向活跃 {float(latest_market.get('active_breadth') or 0) * 100:.0f}%"
        f" · {latest_market.get('risk_label', '风险未知')}"
    )

    output_dates = sorted(enriched_market["date"].unique())[-n_dates:]
    coverage = {
        "covered": int(latest_market.get("stocks") or 0),
        "total": int(latest_market.get("expected_stocks") or 0),
        "ratio": _number(latest_market.get("coverage"), 4),
    }
    return _json_safe({
        "schema_version": 2,
        "model_version": MODEL_VERSION,
        "scheme": scheme,
        "scheme_label": scheme_label,
        "trade_date": latest_date,
        "as_of": latest_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dates": [{
            "date": date,
            "label": _date_label(date),
            "full_label": _full_date_label(date),
        } for date in output_dates],
        "market": latest_market,
        "industries": industries,
        "data_quality": {
            "coverage": coverage,
            "classification": classification or {},
            "causal_percentiles": True,
            "percentile_window": percentile_window,
            "relative_participation_baseline": (
                "当前行业成交占比 / 此前60日成交占比中位数"
            ),
            "direction_method": (
                "(上涨股票成交额－下跌股票成交额) / 行业成交额"
            ),
            "proxy_notice": (
                "成交量本身没有买卖方向；方向指标为日线价格条件下的涨跌成交差率，"
                "不是资金净流入或逐笔主动买卖量。"
            ),
            "risk_method": (
                "观察至少需要两个独立风险域，警告需要三个；危险需要四个，"
                "或三个风险域连续两日确认。"
            ),
            "history_days": n_dates,
        },
    })


__all__ = [
    "MODEL_VERSION",
    "build_turnover_momentum_payload",
    "causal_median_ratio",
    "causal_percentile",
    "classify_momentum_risk",
    "effective_participant_count",
    "prepare_stock_momentum_daily",
]
