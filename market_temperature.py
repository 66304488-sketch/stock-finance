#!/usr/bin/env python3
"""大盘冷热（市场温度）:历史日度宽度统计 + 综合温度分。

数据源全部复用现有设施:
- kline_cache.pkl:全市场 OHLCV,自行计算涨/跌/涨跌停/大涨大跌/成交额
- SQLite:每日新高/新低合计、资金流合计、市值合计
温度分 = 各因子在当日可见的滚动历史中的分位数加权合成 (0-100)。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd

from db import get_db
from kline_cache import KlineCache

# 涨跌停近似阈值(无逐股涨停价数据,按板块规则取 98% 处)
LIMIT_MAIN = 9.8       # 主板
LIMIT_ST = 4.8         # 主板 ST
LIMIT_GROWTH = 19.8    # 创业板/科创板
LIMIT_BJ = 29.8        # 北交所

# 综合温度权重
DAILY_WEIGHTS = {
    "breadth": 0.30,     # 涨跌家数差占比
    "limit": 0.20,       # 涨停-跌停差
    "highs_lows": 0.25,  # 60日新高-新低差
    "activity": 0.10,    # 成交额/20日均量
    "flow": 0.075,       # 资金净流入
    "mcap": 0.075,       # 总市值涨跌幅
}
INTRADAY_WEIGHTS = {"breadth": 0.35, "limit": 0.30, "highs_lows": 0.35}
PERCENTILE_WINDOW = 250
MIN_PERCENTILE_SAMPLES = 20

# 温度曲线对比用指数(sina 代码)
INDEX_SYMBOLS = {
    "sh000001": "上证指数",
    "sz399006": "创业板指",
    "sz399005": "中小板指",
    "sh000688": "科创50",
}
INDEX_KEEP_DAYS = 400

FLAT_BAND = 0.01       # 平盘带宽 (%)
BIG_MOVE = 5.0         # 大涨/大跌阈值 (%)


def limit_threshold(code: str, name: str | None = None) -> float:
    """按板块规则返回涨跌停近似阈值 (%)。"""
    code = str(code)
    if code.startswith(("300", "301", "688", "689")):
        return LIMIT_GROWTH
    if code.startswith(("43", "83", "87", "88")):
        return LIMIT_BJ
    if "ST" in (name or "").upper():
        return LIMIT_ST
    return LIMIT_MAIN


def compute_daily_breadth(cache: KlineCache | None = None) -> pd.DataFrame:
    """从 kline cache 计算全历史每日宽度指标,返回以 YYYYMMDD 为索引的 DataFrame。"""
    cache = cache or KlineCache()
    cache._load()
    frames = []
    for code, df in (cache._cache.get("data") or {}).items():
        if df is None or len(df) < 2 or "close" not in df.columns:
            continue
        names = df["name"].dropna() if "name" in df.columns else pd.Series(dtype=object)
        name = str(names.iloc[-1]) if len(names) else ""
        close = df["close"].astype(float)
        prev = close.shift(1)
        pct = (close / prev - 1) * 100
        # 新浪 spot 行自带真实单日涨跌幅(跨数据空洞时 shift 会累计多日,优先用现货值)
        if "change_pct" in df.columns:
            spot_pct = df["change_pct"].astype(float)
            pct = pct.where(spot_pct.isna(), spot_pct)
        volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(0.0, index=df.index)
        frames.append(pd.DataFrame({
            "date": df["date"].values,
            "pct": pct.values,
            "amount": (close * volume).values,
            "thr": limit_threshold(code, name),
        }))
    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True).dropna(subset=["pct"])
    pct = all_df["pct"]
    all_df["up"] = pct > FLAT_BAND
    all_df["down"] = pct < -FLAT_BAND
    all_df["flat"] = ~all_df["up"] & ~all_df["down"]
    all_df["limit_up"] = pct >= all_df["thr"]
    all_df["limit_down"] = pct <= -all_df["thr"]
    all_df["big_up"] = pct >= BIG_MOVE
    all_df["big_down"] = pct <= -BIG_MOVE
    agg = all_df.groupby("date").agg(
        stocks=("pct", "count"),
        up=("up", "sum"), down=("down", "sum"), flat=("flat", "sum"),
        limit_up=("limit_up", "sum"), limit_down=("limit_down", "sum"),
        big_up=("big_up", "sum"), big_down=("big_down", "sum"),
        amount=("amount", "sum"),
    ).sort_index()
    agg.index = agg.index.strftime("%Y%m%d")
    # 剔除缓存空洞日期(覆盖不足,统计无意义)
    return agg[agg["stocks"] >= 500]


def _db_series(db, table: str, column: str) -> dict[str, float]:
    """取某表 SW 口径全市场合计的日期序列。"""
    rows = db.conn.execute(
        f"SELECT date, {column} FROM {table} WHERE scheme='sw' AND is_total=1"
        + (" AND period='60d'" if table in ("daily_new_highs", "daily_new_lows") else "")
    ).fetchall()
    return {date: (value or 0) for date, value in rows}


def _rolling_pct_rank(
    series: pd.Series,
    window: int = PERCENTILE_WINDOW,
    min_samples: int = MIN_PERCENTILE_SAMPLES,
) -> pd.Series:
    """只用当日及此前窗口计算分位数，避免历史值引用未来数据。

    并列值采用平均秩，与 pandas ``rank(pct=True)`` 的语义一致。
    """
    result = pd.Series(float("nan"), index=series.index, dtype=float)
    for pos, value in enumerate(series):
        if pd.isna(value):
            continue
        history = series.iloc[max(0, pos - window + 1):pos + 1].dropna()
        if len(history) < min_samples:
            continue
        below = int((history < value).sum())
        equal = int((history == value).sum())
        average_rank = below + (equal + 1) / 2
        result.iloc[pos] = average_rank / len(history) * 100
    return result


def build_daily_frame(db=None) -> pd.DataFrame:
    """合并宽度与 DB 辅助因子,计算每日温度分。返回完整历史 DataFrame。"""
    db = db or get_db()
    agg = compute_daily_breadth()
    if agg.empty:
        return agg

    highs = _db_series(db, "daily_new_highs", "count")
    lows = _db_series(db, "daily_new_lows", "count")
    flow = _db_series(db, "daily_capital_flow", "net_flow")
    mcap = _db_series(db, "daily_market_cap", "mcap")

    agg["highs_total"] = [highs.get(d) for d in agg.index]
    agg["lows_total"] = [lows.get(d) for d in agg.index]
    agg["net_flow"] = [flow.get(d) for d in agg.index]
    mcap_s = pd.Series([mcap.get(d) for d in agg.index], index=agg.index, dtype=float)
    # 缺失市值必须保持缺失；不能把停更数据前向填充成“0% 变化”。
    agg["mcap_change_pct"] = mcap_s.pct_change(fill_method=None) * 100

    factors = pd.DataFrame(index=agg.index)
    factors["breadth"] = (agg["up"] - agg["down"]) / agg["stocks"]
    factors["limit"] = (agg["limit_up"] - agg["limit_down"]).astype(float)
    factors["highs_lows"] = (agg["highs_total"].fillna(0) - agg["lows_total"].fillna(0)).astype(float)
    factors.loc[agg["highs_total"].isna() & agg["lows_total"].isna(), "highs_lows"] = None
    factors["activity"] = agg["amount"] / agg["amount"].rolling(20, min_periods=5).mean()
    factors["flow"] = agg["net_flow"].astype(float)
    factors["mcap"] = agg["mcap_change_pct"].astype(float)

    ranks = factors.apply(_rolling_pct_rank)
    temperature = pd.Series(0.0, index=agg.index)
    for date in agg.index:
        total_w = 0.0
        score = 0.0
        for factor, weight in DAILY_WEIGHTS.items():
            value = ranks.loc[date, factor]
            if pd.notna(value):
                score += weight * value
                total_w += weight
        temperature[date] = round(score / total_w, 1) if total_w else None
    agg["temperature"] = temperature
    return agg


def fetch_index_quotes() -> list[dict]:
    """拉取对比指数日线(新浪),返回 [{date, symbol, close}],失败指数跳过。"""
    import akshare as ak
    records = []
    for symbol in INDEX_SYMBOLS:
        try:
            df = ak.stock_zh_index_daily(symbol=symbol)
            for _, row in df.tail(INDEX_KEEP_DAYS).iterrows():
                records.append({
                    "date": pd.Timestamp(row["date"]).strftime("%Y%m%d"),
                    "symbol": symbol,
                    "close": round(float(row["close"]), 3),
                })
        except Exception as exc:
            print(f"指数 {symbol} 拉取失败(跳过): {exc}")
    return records


def update_market_temperature() -> int:
    """全量重算历史温度并写入 SQLite。返回写入行数。"""
    db = get_db()
    frame = build_daily_frame(db)
    if frame.empty:
        return 0
    try:
        db.replace_index_quotes(fetch_index_quotes())
    except Exception as exc:
        print(f"指数数据更新失败(不影响温度): {exc}")
    records = []
    for date, row in frame.iterrows():
        records.append({
            "date": date,
            "stocks": int(row["stocks"]),
            "up": int(row["up"]), "down": int(row["down"]), "flat": int(row["flat"]),
            "limit_up": int(row["limit_up"]), "limit_down": int(row["limit_down"]),
            "big_up": int(row["big_up"]), "big_down": int(row["big_down"]),
            "amount": round(float(row["amount"])),
            "highs_total": _opt_int(row["highs_total"]),
            "lows_total": _opt_int(row["lows_total"]),
            "net_flow": _opt_float(row["net_flow"]),
            "mcap_change_pct": _opt_float(row["mcap_change_pct"], ndigits=2),
            "temperature": _opt_float(row["temperature"], ndigits=1),
        })
    db.replace_market_temperature(records)
    return len(records)


def _opt_int(value) -> int | None:
    return None if pd.isna(value) else int(value)


def _opt_float(value, ndigits: int = 2) -> float | None:
    return None if pd.isna(value) else round(float(value), ndigits)


def _percentile_of(history: list[float], value: float) -> float | None:
    """value 在 history 中的分位数 (0-100);样本不足返回 None。"""
    clean = [v for v in history if v is not None]
    if len(clean) < MIN_PERCENTILE_SAMPLES:
        return None
    clean = clean[-PERCENTILE_WINDOW:]
    below = sum(1 for v in clean if v < value)
    equal = sum(1 for v in clean if v == value)
    average_rank = below + (equal + 1) / 2
    return round(average_rank / len(clean) * 100, 1)


def compute_intraday_temperature(
    market: dict[str, Any],
    signals: dict[int, dict[str, dict[str, dict]]],
    history_rows: list[dict] | None,
) -> dict[str, Any]:
    """盘中温度:原始宽度 + 相对历史日线序列的分位数合成。

    market: scan_intraday._collect_signals 的全市场累加统计
    signals: 各窗口新高/新低信号(取 60 日窗口的 standing 数)
    history_rows: market_temperature.json 的 rows(可为空)
    """
    stocks = market.get("spot_count") or 0
    up, down = market.get("up", 0), market.get("down", 0)
    limit_diff = market.get("limit_up", 0) - market.get("limit_down", 0)

    window = 60 if 60 in signals else (max(signals) if signals else None)
    hl_diff = None
    highs_n = lows_n = 0
    if window:
        for direction in ("highs", "lows"):
            standing = sum(1 for s in signals[window][direction].values() if s.get("standing"))
            if direction == "highs":
                highs_n = standing
            else:
                lows_n = standing
        hl_diff = highs_n - lows_n

    raw = {
        "breadth": (up - down) / stocks if stocks else None,
        "limit": float(limit_diff),
        "highs_lows": float(hl_diff) if hl_diff is not None else None,
    }

    percentiles: dict[str, float | None] = {k: None for k in INTRADAY_WEIGHTS}
    if history_rows:
        hist = {
            "breadth": [(r["up"] - r["down"]) / r["stocks"] for r in history_rows if r.get("stocks")],
            "limit": [r["limit_up"] - r["limit_down"] for r in history_rows],
            "highs_lows": [
                (r["highs_total"] or 0) - (r["lows_total"] or 0)
                for r in history_rows
                if r.get("highs_total") is not None or r.get("lows_total") is not None
            ],
        }
        for factor in INTRADAY_WEIGHTS:
            if raw[factor] is not None and hist.get(factor):
                percentiles[factor] = _percentile_of(hist[factor], raw[factor])

    total_w = score = 0.0
    for factor, weight in INTRADAY_WEIGHTS.items():
        if percentiles[factor] is not None:
            score += weight * percentiles[factor]
            total_w += weight
    temperature = round(score / total_w, 1) if total_w else None

    return {
        "temperature": temperature,
        "factors": percentiles,
        "breadth": {
            "stocks": stocks,
            "up": up, "down": down, "flat": market.get("flat", 0),
            "limit_up": market.get("limit_up", 0), "limit_down": market.get("limit_down", 0),
            "big_up": market.get("big_up", 0), "big_down": market.get("big_down", 0),
            "amount": round(market.get("amount", 0.0)),
        },
        "highs_total": highs_n,
        "lows_total": lows_n,
        "signal_window": window,
        "mcap": round(market.get("mcap", 0.0)),
        "mcap_change": round(market.get("mcap_change", 0.0)),
    }


def load_temperature_history(output_dir: str) -> list[dict]:
    """读取已导出的 market_temperature.json 的 rows(供盘中分位数基线)。"""
    path = os.path.join(output_dir, "market_temperature.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("rows") or []
    except (OSError, json.JSONDecodeError):
        return []
