"""动量ETF 评分引擎 —— 聚宽「五福5.2」策略信号版（支持盘中刷新）。

复刻策略核心（不含盘中交易执行）:
  动量评分 = 年化收益(exp(加权回归斜率×250)-1) × R²，25日对数价格、权重 linspace(1,2)²
  过滤链   = 得分∈[0,5] → R²>0.4(正常期)/收盘>MA10(走弱期) → 量比<1.8 → 近3日单日跌幅≥-3%
  市场状态 = 沪深300/中小综指/创业板指/中证A500 对 MA10：≥3/4破位进走弱期，≥3/4站上退出，
             最长20交易日强制退出；走弱期仅用全球/海外池
  目标     = 过滤后候选 → 得分≥第1名×0.9(走弱期×1.0) → 取第1名；无目标→防御(银华日利)

盘中模式: 刷新时取新浪实时价拼接到日K末端（与策略13:10运行方式一致），
          量比 = 今日已成交量×(240/已交易分钟) / 前5日均量；盘前刷新则按昨收。
流动性阈值: 简化为 20日日均成交额≥5000万（策略为全市场总额/20000）。

输出: momentum_etf.json(页面数据) + momentum_state.json(走弱期状态持久化)
池子: 全球池/中国池在 momentum_etf_pool.json 中维护；动态池自动读取 ETF 热点候选。

用法: python momentum_etf.py
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time
from bisect import bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dt_time

import numpy as np
import requests

from runtime_paths import data_path, resource_path

POOL_FILE = "momentum_etf_pool.json"
OUTPUT_FILE = "momentum_etf.json"
STATE_FILE = "momentum_state.json"

SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_HQ_URL = "https://hq.sinajs.cn/list="
KLINE_DATALEN = 50  # 26(评分) + 10(MA) + 6(量比/风控) + 余量

WEAK_INDEXES = [
    {"code": "sh000300", "name": "沪深300"},
    {"code": "sz399101", "name": "中小综指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000510", "name": "中证A500"},
]

DYNAMIC_POOL_CACHE = "momentum_dynamic_pool.json"
ETF_RECOMMEND_FILE = "etf_recommend_sw3.json"
DYNAMIC_TOP_N = 10
RANK_HISTORY_LIMIT = 20
RANK_HISTORY_METHOD_REPLAY = "replayed_current_universe"


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------

def _atomic_json_dump(data, path):
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _tencent_kline(symbol: str, datalen: int):
    """腾讯前复权日K → [{'date','open','high','low','close','volume'(股)}] 升序。
    前复权锚定最新价=真实价，盘中最后一根为当日实时bar。"""
    try:
        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,,,{datalen},qfq"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        data = r.json().get("data", {}).get(symbol, {})
        rows = data.get("qfqday") or data.get("day") or []
        out = []
        for d in rows:
            try:
                out.append({
                    "date": d[0],
                    "open": float(d[1]),
                    "close": float(d[2]),
                    "high": float(d[3]),
                    "low": float(d[4]),
                    "volume": float(d[5]) * 100,  # 手→股
                })
            except (ValueError, IndexError, TypeError):
                continue
        return out or None
    except Exception:
        return None


def _sina_kline(symbol: str, datalen: int):
    """前复权日K（优先腾讯qfq，新浪兜底）→ [{'date','close','volume'}...] 按时间升序，失败返回 None"""
    rows = _tencent_kline(symbol, datalen)
    if rows:
        return rows
    for attempt in range(2):
        try:
            r = requests.get(
                SINA_KLINE_URL,
                params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": datalen},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
                timeout=15,
            )
            data = r.json()
            if not isinstance(data, list) or not data:
                return None
            return [{"date": d.get("day", ""),
                     "open": float(d["open"]),
                     "high": float(d["high"]),
                     "low": float(d["low"]),
                     "close": float(d["close"]),
                     "volume": float(d["volume"])} for d in data]
        except Exception:
            if attempt:
                return None
            time.sleep(0.5)
    return None


def _etf_symbol(code: str) -> str:
    return ("sh" if code.startswith("5") else "sz") + code


def _fetch_spot_batch(codes: list[str]) -> dict:
    """批量实时行情(新浪hq) → {code: {price, prev_close, volume, amount, date, time}}"""
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        symbols = ",".join(_etf_symbol(c) for c in batch)
        try:
            r = requests.get(SINA_HQ_URL + symbols,
                             headers={"Referer": "https://finance.sina.com.cn",
                                      "User-Agent": "Mozilla/5.0"}, timeout=10)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                m = re.match(r'var hq_str_(?:sh|sz)(\d{6})="(.*)";', line.strip())
                if not m:
                    continue
                code, f = m.group(1), m.group(2).split(",")
                if len(f) < 32:
                    continue
                try:
                    out[code] = {
                        "price": float(f[3]),
                        "prev_close": float(f[2]),
                        "volume": float(f[8]),   # 股
                        "amount": float(f[9]),   # 元
                        "date": f[30].replace("-", ""),
                        "time": f[31],
                    }
                except (ValueError, IndexError):
                    continue
        except Exception:
            continue
    return out


def _elapsed_trade_minutes(now: datetime | None = None) -> int | None:
    """盘中已经过的交易分钟数；盘前返回 None（按昨收），盘后返回 240"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return 240
    t = now.time()
    if t < dt_time(9, 30):
        return None
    if t >= dt_time(15, 0):
        return 240
    if t >= dt_time(13, 0):
        return min(240, 120 + (now.hour - 13) * 60 + now.minute)
    if t >= dt_time(11, 30):
        return 120
    return max(1, (now.hour - 9) * 60 + now.minute - 30)


# ------------------------------------------------------------------
# 配置与状态
# ------------------------------------------------------------------

def load_pool_config() -> dict:
    path = data_path(POOL_FILE)
    if not os.path.exists(path):
        # 老数据目录没有池文件（升级场景）→ 从打包资源复制策略预设
        bundled = resource_path(POOL_FILE)
        if os.path.exists(bundled):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(bundled, path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_state() -> dict:
    path = data_path(STATE_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"is_weak": False, "weak_start_date": None, "weak_days_count": 0}


def _trade_days_between(start_yyyymmdd: str, end_yyyymmdd: str) -> int:
    """两个日期之间的交易日数（含两端），失败时按自然日估算"""
    try:
        import akshare as ak
        import pandas as pd
        df = ak.tool_trade_date_hist_sina()
        dates = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
        return int(((dates >= start_yyyymmdd) & (dates <= end_yyyymmdd)).sum())
    except Exception:
        fmt = "%Y%m%d"
        delta = datetime.strptime(end_yyyymmdd, fmt) - datetime.strptime(start_yyyymmdd, fmt)
        return max(1, round(delta.days * 5 / 7))


# ------------------------------------------------------------------
# 动态池（ETF 热点候选）
# ------------------------------------------------------------------

def _extract_dynamic_pool(recommendation: dict, limit: int = DYNAMIC_TOP_N) -> list[dict]:
    """从 ETF 热点候选提取去重后的前 N 只 ETF，并保留预测来源。"""
    entries, seen = [], set()
    for row in recommendation.get("top") or []:
        etf = row.get("etf") or {}
        code = str(etf.get("code") or "").strip()
        if not re.fullmatch(r"\d{6}", code) or code in seen:
            continue
        seen.add(code)
        entries.append({
            "rank": len(entries) + 1,
            "code": code,
            "name": str(etf.get("name") or code),
            "industry": str(row.get("industry") or ""),
            "industry_score": row.get("opportunity_score", row.get("score")),
            "industry_heat": row.get("heat_score"),
            "industry_stage": row.get("stage"),
            "industry_confidence": row.get("data_quality", row.get("confidence")),
            "prediction_probability": row.get("prediction_probability"),
            "prediction_status": row.get("prediction_status"),
            "risk_level": ((row.get("risk") or {}).get("state")
                           or (row.get("risk") or {}).get("level")
                           if isinstance(row.get("risk"), dict)
                           else row.get("risk_level")),
            "match_level": (etf.get("match_level")
                            or (row.get("carrier_match") or {}).get("primary_level")
                            or (row.get("carrier_match") or {}).get("level")),
            "match_label": (etf.get("match_label")
                            or (row.get("carrier_match") or {}).get("label")),
        })
        if len(entries) >= limit:
            break
    return entries


def _load_dynamic_pool() -> tuple[dict[str, str], dict[str, dict], dict]:
    """读取 ETF 热点候选；源文件不可用时才回退上次有效快照。

    V3 明确允许“今日无信号”。这是一条有效决策，必须覆盖旧缓存，不能把
    昨日候选悄悄补回并伪装成今日信号。
    """
    recommendation = None
    source_path = data_path(ETF_RECOMMEND_FILE)
    if not os.path.exists(source_path):
        source_path = resource_path(ETF_RECOMMEND_FILE)
    if os.path.exists(source_path):
        try:
            with open(source_path, encoding="utf-8") as f:
                recommendation = json.load(f)
        except (OSError, json.JSONDecodeError):
            recommendation = None

    entries = _extract_dynamic_pool(recommendation or {})
    cache_path = data_path(DYNAMIC_POOL_CACHE)
    is_v3_payload = str((recommendation or {}).get("model_version") or "").startswith(
        "etf-hotspot-v3"
    )
    if entries or is_v3_payload:
        stats = {
            "source": "etf_recommend_sw3",
            "source_date": recommendation.get("date"),
            "source_updated_at": recommendation.get("updated_at"),
            "model_version": recommendation.get("model_version"),
            "decision_status": recommendation.get("decision_status"),
            "actionable_message": recommendation.get("actionable_message"),
            "calibration": recommendation.get("calibration"),
            "dynamic_count": len(entries),
            "entries": entries,
            "intentional_empty": bool(is_v3_payload and not entries),
        }
        _atomic_json_dump(stats, cache_path)
    else:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("source") == "etf_recommend_sw3":
                entries = cached.get("entries") or []
                stats = {**cached, "stale": True}
            else:
                stats = {"source": "etf_recommend_sw3", "dynamic_count": 0, "entries": []}
        except (OSError, json.JSONDecodeError):
            stats = {"source": "etf_recommend_sw3", "dynamic_count": 0, "entries": []}

    names = {entry["code"]: entry["name"] for entry in entries}
    metadata = {entry["code"]: entry for entry in entries}
    print(f"[momentum] 动态池: ETF热点候选（上限 {DYNAMIC_TOP_N}）→ {len(entries)} 只"
          + ("（今日无信号）" if stats.get("intentional_empty") else "")
          + ("（使用上次快照）" if stats.get("stale") else ""))
    return names, metadata, stats


# ------------------------------------------------------------------
# 走弱期判断（复刻 check_a_share_weak_period）
# ------------------------------------------------------------------

def check_weak_period(params: dict) -> dict:
    lookback = params.get("weak_ma_lookback", 10)
    max_weak_days = params.get("max_weak_days", 20)
    state = _load_state()

    indexes, above_count, below_count = [], 0, 0
    today = None
    for idx in WEAK_INDEXES:
        rows = _sina_kline(idx["code"], lookback + 5)
        if not rows or len(rows) < lookback:
            indexes.append({"name": idx["name"], "close": None, "ma": None, "above": None})
            continue
        closes = [r["close"] for r in rows]
        cur = closes[-1]
        if today is None:
            today = rows[-1]["date"].replace("-", "")
        ma = float(np.mean(closes[-lookback:]))
        above = cur > ma
        above_count += above
        below_count += (not above)
        indexes.append({"name": idx["name"], "close": round(cur, 2),
                        "ma": round(ma, 2), "above": above})
    today = today or datetime.now().strftime("%Y%m%d")

    is_weak = bool(state.get("is_weak"))
    weak_start = state.get("weak_start_date")

    weak_days = 0
    if is_weak and weak_start:
        weak_days = _trade_days_between(weak_start, today)

    if is_weak:
        if weak_days >= max_weak_days or above_count >= 3:
            is_weak, weak_start, weak_days = False, None, 0
    else:
        if below_count >= 3:
            is_weak, weak_start, weak_days = True, today, 0

    new_state = {"is_weak": is_weak, "weak_start_date": weak_start,
                 "weak_days_count": weak_days}
    _atomic_json_dump(new_state, data_path(STATE_FILE))
    return {**new_state, "indexes": indexes,
            "above_count": above_count, "below_count": below_count,
            "max_weak_days": max_weak_days, "date": today}


# ------------------------------------------------------------------
# 动量评分（复刻 calculate_momentum_score / calculate_all_metrics_for_etf）
# ------------------------------------------------------------------

def _momentum_score(closes: list[float], lookback: int):
    """返回 (score, annualized, r_squared)，数据不足返回 None"""
    if len(closes) < lookback + 1:
        return None
    y = np.log(np.array(closes[-(lookback + 1):]))
    x = np.arange(len(y))
    weights = np.linspace(1, 2, len(y))
    # 与 np.polyfit(..., w=weights) 的目标函数保持一致：回归和
    # 拟合优度都使用 weights²，避免把不同权重口径混在同一个 R² 中。
    W = weights ** 2
    W_sum = W.sum()
    x_bar = (W * x).sum() / W_sum
    y_bar = (W * y).sum() / W_sum
    dx, dy = x - x_bar, y - y_bar
    var_x = (W * dx ** 2).sum()
    if var_x == 0:
        return 0.0, 0.0, 0.0
    slope = (W * dx * dy).sum() / var_x
    intercept = y_bar - slope * x_bar
    annualized = math.exp(slope * 250) - 1
    y_pred = slope * x + intercept
    ss_res = (W * (y - y_pred) ** 2).sum()
    ss_tot = (W * (y - y_bar) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return annualized * r2, annualized, r2


def _etf_metrics(code: str, name: str, rows: list[dict], params: dict, is_weak: bool,
                 spot: dict | None = None, elapsed: int | None = None):
    lookback = params["lookback_days"]
    min_score, max_score = params["score_range"]
    vol_lookback = params["volume_lookback"]
    ma_lookback = params["ma_lookback"]

    # 剔除无成交日（复刻策略 valid_mask）
    valid = [r for r in rows if r["volume"] > 0]
    closes = [r["close"] for r in valid]
    volumes = [r["volume"] for r in valid]
    if len(closes) < lookback + 1:
        return None

    # 盘中：把实时价拼到日K末端（策略 current_price 的用法）；量比用投影
    intraday = False
    last_label = valid[-1]["date"]
    if spot and spot.get("price") and spot.get("volume"):
        kline_last = valid[-1]["date"].replace("-", "")
        spot_is_today = spot.get("date") == datetime.now().strftime("%Y%m%d")
        if spot["date"] > kline_last:
            closes.append(spot["price"])
            volumes.append(spot["volume"])
            last_label = f"{spot['date']} {spot.get('time', '')}".strip()
            intraday = spot_is_today and elapsed is not None and elapsed < 240
        elif spot["date"] == kline_last:
            # K线已含当日bar（腾讯盘中会带）→ 用实时价量替换
            closes[-1] = spot["price"]
            volumes[-1] = spot["volume"]
            last_label = f"{spot['date']} {spot.get('time', '')}".strip()
            intraday = spot_is_today and elapsed is not None and elapsed < 240

    scored = _momentum_score(closes, lookback)
    if scored is None:
        return None
    score, annualized, r2 = scored

    # 量比：盘中=今日已成交×(240/已交易分钟)/前5日均量；收盘=今日量/前5日均量
    volume_ratio = None
    if len(volumes) >= vol_lookback + 1:
        avg_vol = float(np.mean(volumes[-(vol_lookback + 1):-1]))
        if avg_vol > 0:
            if intraday and elapsed:
                volume_ratio = volumes[-1] * (240.0 / elapsed) / avg_vol
            else:
                volume_ratio = volumes[-1] / avg_vol

    # 近3日单日涨跌比（风控：任一天 <-3% 不通过）
    day_ratios = []
    passed_loss = True
    if len(closes) >= 4:
        day_ratios = [closes[-1] / closes[-2], closes[-2] / closes[-3], closes[-3] / closes[-4]]
        passed_loss = min(day_ratios) >= params["loss"]

    # MA 过滤（仅走弱期启用）
    ma_value = float(np.mean(closes[-ma_lookback:])) if len(closes) >= ma_lookback else None
    passed_ma = ma_value is not None and closes[-1] > ma_value * params["ma_threshold"]

    # 流动性：20日日均成交额（盘中剔除今日未完成的 bar）
    amounts = [c * v for c, v in zip(closes, volumes)]
    amount_window = amounts[-21:-1] if intraday and len(amounts) > 20 else amounts[-20:]
    avg_amount = float(np.mean(amount_window))

    def period_return(days: int):
        if len(closes) <= days or not closes[-days - 1]:
            return None
        return round(float(closes[-1] / closes[-days - 1] - 1), 4)

    m = {
        "code": code,
        "name": name,
        "score": round(float(score), 4),
        "annualized": round(float(annualized), 4),
        "r_squared": round(float(r2), 3),
        "price": round(float(closes[-1]), 3),
        "change_pct": round(float(closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) >= 2 else 0.0,
        "return_5d": period_return(5),
        "return_10d": period_return(10),
        "return_20d": period_return(20),
        "volume_ratio": round(float(volume_ratio), 2) if volume_ratio is not None else None,
        "min_day_ratio": round(float(min(day_ratios)), 4) if day_ratios else None,
        "ma_value": round(float(ma_value), 3) if ma_value else None,
        "avg_amount_20d": round(float(avg_amount)),
        "passed_score": bool(min_score <= score <= max_score),
        "passed_r2": bool(r2 > params["r2_threshold"]),
        "passed_ma": bool(passed_ma),
        "passed_volume": bool(volume_ratio is not None and volume_ratio < params["volume_threshold"]),
        "passed_loss": bool(passed_loss),
        "last_date": last_label,
        "intraday": bool(intraday),
    }
    # 复刻 apply_filters：R²仅正常期、MA仅走弱期，其余始终启用
    m["passed_all"] = (
        m["passed_score"]
        and (m["passed_r2"] if not is_weak else True)
        and (m["passed_ma"] if is_weak else True)
        and m["passed_volume"]
        and m["passed_loss"]
    )
    filter_reasons = []
    if not m["passed_score"]:
        filter_reasons.append("得分阈值外")
    if not is_weak and not m["passed_r2"]:
        filter_reasons.append("R²不足")
    if is_weak and not m["passed_ma"]:
        filter_reasons.append("走弱期未站上MA")
    if not m["passed_volume"]:
        filter_reasons.append("量比超限或缺失")
    if not m["passed_loss"]:
        filter_reasons.append("近3日单日跌幅超限")
    m["filter_reasons"] = filter_reasons
    return m


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def _variant_result(metrics_by_code: dict, pool_names: dict, params: dict, is_weak: bool,
                    metadata: dict[str, dict] | None = None) -> dict:
    """从已算好的指标中为一个池产出排名、目标及动态推荐来源。"""
    metadata = metadata or {}
    pool_metrics = []
    for code in pool_names:
        if code not in metrics_by_code:
            continue
        metric = dict(metrics_by_code[code])
        if code in metadata:
            metric["recommendation"] = metadata[code]
        pool_metrics.append(metric)
    pool_metrics.sort(key=lambda x: -x["score"])
    filtered = [m for m in pool_metrics if m["passed_all"]]
    top10 = filtered[:10]
    target, candidate_threshold = None, None
    if top10:
        ratio = params["score_threshold_ratio"] if not is_weak else 1.0
        candidate_threshold = round(top10[0]["score"] * ratio, 4)
        candidates = [m for m in top10 if m["score"] >= candidate_threshold]
        target = candidates[0]
    return {
        "pool_size": len(pool_names),
        "pool_qualified": len(pool_metrics),
        "candidate_threshold": candidate_threshold,
        "target": target,
        "top10": top10,
        "all": pool_metrics,
    }


def _rank_history_snapshot(payload: dict) -> dict | None:
    """提取一次真实输出的过滤后排名快照，供页面展示名次变化。"""
    date = str(payload.get("date") or "").strip()
    variants = payload.get("variants") or {}
    if not date or not isinstance(variants, dict):
        return None
    snapshot_variants = {}
    for key in ("china", "dynamic", "combined", "global", "strategy"):
        rows = (variants.get(key) or {}).get("top10") or []
        snapshot_variants[key] = [
            {
                "rank": index + 1,
                "code": row.get("code"),
                "name": row.get("name"),
                "score": row.get("score"),
                "annualized": row.get("annualized"),
                "r_squared": row.get("r_squared"),
            }
            for index, row in enumerate(rows[:10])
            if re.fullmatch(r"\d{6}", str(row.get("code") or ""))
        ]
    return {
        "date": date,
        "updated_at": payload.get("updated_at"),
        "mode": payload.get("mode"),
        "method": payload.get("history_method") or "observed",
        "universe_note": payload.get("history_universe_note"),
        "market_regime": payload.get("history_market_regime"),
        "variants": snapshot_variants,
    }


def _merge_rank_history(previous: dict | None, current: dict,
                        limit: int = RANK_HISTORY_LIMIT,
                        replayed: list[dict] | None = None) -> list[dict]:
    """合并历史回算与真实快照；同日真实生成结果永远优先。"""
    snapshots = list(replayed or [])
    if isinstance(previous, dict):
        snapshots.extend(
            item for item in (previous.get("rank_history") or [])
            if (isinstance(item, dict) and item.get("date")
                and item.get("method") != RANK_HISTORY_METHOD_REPLAY)
        )
        previous_snapshot = _rank_history_snapshot(previous)
        if previous_snapshot:
            snapshots.append(previous_snapshot)
    current_snapshot = _rank_history_snapshot(current)
    if current_snapshot:
        snapshots.append(current_snapshot)
    by_date = {str(item["date"]): item for item in snapshots}
    return [by_date[key] for key in sorted(by_date)[-max(1, int(limit)):]]


def _bar_date_key(row: dict) -> str:
    digits = re.sub(r"\D", "", str(row.get("date") or ""))
    return digits[:8] if len(digits) >= 8 else ""


def _historical_weak_flags(index_klines: dict[str, list[dict]], params: dict) -> dict[str, bool]:
    """只用每个历史日及此前指数收盘，重放正常/走弱状态机。"""
    lookback = int(params.get("weak_ma_lookback", 10))
    max_weak_days = int(params.get("max_weak_days", 20))
    prepared = {}
    all_dates = set()
    for code, rows in index_klines.items():
        by_date = {
            _bar_date_key(row): float(row["close"])
            for row in rows
            if _bar_date_key(row) and row.get("close") is not None
        }
        prepared[code] = by_date
        all_dates.update(by_date)

    closes = {code: [] for code in prepared}
    is_weak, weak_age = False, 0
    flags = {}
    for date in sorted(all_dates):
        above = below = covered = 0
        for code, by_date in prepared.items():
            if date not in by_date:
                continue
            closes[code].append(by_date[date])
            if len(closes[code]) < lookback:
                continue
            covered += 1
            ma_value = float(np.mean(closes[code][-lookback:]))
            if closes[code][-1] > ma_value:
                above += 1
            else:
                below += 1
        if covered >= 3:
            if is_weak:
                weak_age += 1
                if weak_age >= max_weak_days or above >= 3:
                    is_weak, weak_age = False, 0
            elif below >= 3:
                is_weak, weak_age = True, 0
        flags[date] = is_weak
    return flags


def _historical_rank_dates(klines: dict[str, list[dict]], limit: int,
                           exclude_dates: set[str] | None = None) -> list[str]:
    exclude_dates = exclude_dates or set()
    counts = Counter()
    covered_series = 0
    for rows in klines.values():
        dates = {
            _bar_date_key(row) for row in rows
            if _bar_date_key(row) and float(row.get("volume") or 0) > 0
        }
        if dates:
            covered_series += 1
            counts.update(dates)
    threshold = 1 if covered_series <= 4 else max(2, math.ceil(covered_series * 0.5))
    dates = [
        date for date, count in counts.items()
        if count >= threshold and date not in exclude_dates
    ]
    return sorted(dates)[-max(1, int(limit)):]


def _replay_rank_history(klines: dict[str, list[dict]], all_names: dict[str, str],
                         variant_pools: dict[str, dict[str, str]], params: dict,
                         min_amount: float, weak_flags: dict[str, bool],
                         exclude_dates: set[str] | None = None,
                         limit: int = RANK_HISTORY_LIMIT) -> list[dict]:
    """按当前池成员和参数逐日回算，不把当前动态池冒充历史候选池。"""
    dates = _historical_rank_dates(klines, limit, exclude_dates)
    prepared = {}
    for code, rows in klines.items():
        ordered = sorted(
            ((_bar_date_key(row), row) for row in rows if _bar_date_key(row)),
            key=lambda item: item[0],
        )
        prepared[code] = ([item[0] for item in ordered], [item[1] for item in ordered])

    normal_strategy_names = {**variant_pools.get("global", {}),
                             **variant_pools.get("combined", {})}
    snapshots = []
    for date in dates:
        is_weak = bool(weak_flags.get(date, False))
        metrics_by_code = {}
        for code, (keys, rows) in prepared.items():
            end = bisect_right(keys, date)
            if not end or keys[end - 1] != date:
                continue
            metric = _etf_metrics(code, all_names.get(code, code), rows[:end], params, is_weak)
            if metric is None or metric["avg_amount_20d"] < min_amount:
                continue
            metrics_by_code[code] = metric
        day_pools = dict(variant_pools)
        day_pools["strategy"] = (variant_pools.get("global", {})
                                 if is_weak else normal_strategy_names)
        variants = {
            key: _variant_result(metrics_by_code, pool, params, is_weak)
            for key, pool in day_pools.items()
        }
        snapshot = _rank_history_snapshot({
            "date": date,
            "mode": "close",
            "history_method": RANK_HISTORY_METHOD_REPLAY,
            "history_universe_note": "按当前池成员与当前参数逐日历史回算",
            "history_market_regime": "weak" if is_weak else "normal",
            "variants": variants,
        })
        if snapshot:
            snapshots.append(snapshot)
    return snapshots


def update_momentum_etf() -> dict:
    cfg = load_pool_config()
    params = cfg["params"]
    min_amount = params.get("min_avg_amount", 5e7)

    weak = check_weak_period(params)
    is_weak = weak["is_weak"]
    print(f"[momentum] 市场状态: {'🔴 走弱期' if is_weak else '🟢 正常期'}"
          f" (破位 {weak['below_count']}/4, 站上 {weak['above_count']}/4)")

    # 中国池与推荐动态池始终分别计算；策略池在正常期合并三池，走弱期仅使用全球池。
    global_names = {e["code"]: e.get("name", "") for e in cfg["global_pool"]}
    china_names = {e["code"]: e.get("name", "") for e in cfg["china_pool"]}
    dynamic_names, dynamic_meta, dyn_stats = _load_dynamic_pool()
    combined_names = {**china_names, **dynamic_names}
    normal_strategy_names = {**global_names, **combined_names}
    strategy_names = global_names if is_weak else normal_strategy_names
    variant_pools = {
        "china": china_names,
        "dynamic": dynamic_names,
        "combined": combined_names,
        "global": global_names,
        "strategy": strategy_names,
    }

    # 拉全部所需代码的 K线 + 盘中实时价（所有池共享）。额外长度用于历史逐日回算。
    all_names = {}
    for p in variant_pools.values():
        all_names.update(p)
    kline_datalen = max(
        KLINE_DATALEN,
        int(params.get("lookback_days", 25)) + RANK_HISTORY_LIMIT
        + max(int(params.get("ma_lookback", 10)),
              int(params.get("volume_lookback", 5)), 20) + 8,
    )
    klines, fails = {}, 0
    with ThreadPoolExecutor(max_workers=10) as pool_exec:
        futures = {
            pool_exec.submit(_sina_kline, _etf_symbol(c), kline_datalen): c
            for c in all_names
        }
        for f in as_completed(futures):
            code = futures[f]
            rows = f.result()
            if rows:
                klines[code] = rows
            else:
                fails += 1
    spots = _fetch_spot_batch(list(all_names))
    elapsed = _elapsed_trade_minutes()
    print(f"[momentum] K线: 成功 {len(klines)}/{len(all_names)} 只"
          + (f", 失败 {fails}" if fails else "")
          + f" | 实时价 {len(spots)} 只, 已交易分钟: {elapsed if elapsed is not None else '盘前'}")

    # 计算指标 + 流动性过滤（所有池共享同一份指标）
    metrics_by_code, intraday_count, illiquid_codes = {}, 0, set()
    for code, rows in klines.items():
        m = _etf_metrics(code, all_names[code], rows, params, is_weak,
                         spot=spots.get(code), elapsed=elapsed)
        if m is None:
            continue
        if m.get("intraday"):
            intraday_count += 1
        if m["avg_amount_20d"] < min_amount:
            illiquid_codes.add(code)
            continue
        metrics_by_code[code] = m

    variants = {
        key: _variant_result(
            metrics_by_code,
            pool,
            params,
            is_weak,
            dynamic_meta if key in ("dynamic", "combined", "strategy") else None,
        )
        for key, pool in variant_pools.items()
    }

    defensive = cfg.get("defensive_etf", {"code": "511880", "name": "银华日利"})
    result = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "date": weak["date"],
        "asof": datetime.now().strftime("%H:%M"),
        "mode": "intraday" if intraday_count > 0 else "close",
        "is_weak": is_weak,
        "weak_days_count": weak["weak_days_count"],
        "max_weak_days": weak["max_weak_days"],
        "indexes": weak["indexes"],
        "dynamic_pool": dyn_stats,
        "defensive_etf": defensive,
        "variants": variants,
        "strategy_variant": "global" if is_weak else "global+china+dynamic",
        "target": variants["strategy"]["target"],
        "params": params,
    }

    # 历史轨迹：回算最近20个交易日；盘中当日由真实实时快照负责，不用未完成日K冒充收盘。
    index_history_length = max(
        90,
        RANK_HISTORY_LIMIT + int(params.get("weak_ma_lookback", 10))
        + int(params.get("max_weak_days", 20)) + 30,
    )
    index_klines = {}
    with ThreadPoolExecutor(max_workers=4) as pool_exec:
        index_futures = {
            pool_exec.submit(_sina_kline, item["code"], index_history_length): item["code"]
            for item in WEAK_INDEXES
        }
        for future in as_completed(index_futures):
            rows = future.result()
            if rows:
                index_klines[index_futures[future]] = rows
    weak_flags = _historical_weak_flags(index_klines, params)
    replayed_history = _replay_rank_history(
        klines,
        all_names,
        variant_pools,
        params,
        min_amount,
        weak_flags,
        {str(result["date"])} if result["mode"] == "intraday" else set(),
    )
    previous = None
    output_path = data_path(OUTPUT_FILE)
    if os.path.exists(output_path):
        try:
            with open(output_path, encoding="utf-8") as f:
                previous = json.load(f)
        except (OSError, json.JSONDecodeError):
            previous = None
    result["rank_history"] = _merge_rank_history(
        previous, result, replayed=replayed_history
    )
    _atomic_json_dump(result, output_path)
    for key, label in (("china", "中国池"), ("dynamic", "动态池"),
                       ("combined", "中动态合并池"), ("global", "全球池"),
                       ("strategy", "策略池")):
        v = variants[key]
        t = v["target"]
        print(f"[momentum] {label}: 池 {v['pool_size']} 只 → 合格 {v['pool_qualified']} 只 → "
              f"过滤通过 {len(v['top10'])} 只 | 目标: "
              + (f"{t['name']}({t['code']}) 得分 {t['score']}" if t
                 else f"无 → 防御({defensive.get('name', '')})"))
    return result


if __name__ == "__main__":
    update_momentum_etf()
    print(f"\n✅ 已生成 {OUTPUT_FILE}")
