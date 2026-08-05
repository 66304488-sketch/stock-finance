"""ETF 热点候选引擎 —— 申万三级行业证据 → 唯一 ETF 候选。

第一性原理：热点延续需要更多成分股参与、边际需求持续改善、ETF 价格确认，
同时不能已经过热。因此输出两个互不混淆的量：
  - opportunity_score / score：ETF 级证据排序，最多输出 5 只且允许为空
  - prediction.probability：仅在样本外校准充分后显示，否则为 null

V3 不把上涨成交额减下跌成交额称为“资金流入”，它只是
directional_participation_proxy（方向成交参与代理）。真实 ETF 需求只使用
交易所公布的份额变化。最终预测单位是 ETF；同一 ETF 只做一次价格确认。

旧的行业评分辅助函数继续保留，供历史回测和兼容测试使用。
每项以绝对门槛为主、行业横截面分位为辅，避免弱市中也被迫产生虚假高分。

流程:
  1. ensure_etf_map()     映射表缺失时自动生成（build_etf_map）
  2. fetch_etf_snapshot() 拉候选 ETF 60日K，计算趋势、均线距离、量能和波动 → etf_snapshot.json
  3. build_recommendations() 读 SQLite + 快照 → etf_recommend_sw3.json

用法: python etf_recommend.py
"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from db import get_db
from runtime_paths import data_path, resource_path

MAP_FILE = "industry_etf_map_sw3.json"
SNAPSHOT_FILE = "etf_snapshot.json"
OUTPUT_FILE = "etf_recommend_sw3.json"
PREDICTION_LOG_FILE = "etf_prediction_log.jsonl"

SCHEME = "sw3"
RECENT_DAYS = 5          # 近期窗口
BASE_DAYS = 20           # 基准窗口
TOP_N = 10               # 旧行业回测兼容；V3 页面候选使用 V3_TOP_N
V3_TOP_N = 5
V3_MODEL_VERSION = "etf-hotspot-v3"
V3_MIN_SCORE = 58.0
V3_MIN_DATA_QUALITY = 60.0
BENCHMARK_CODE = "510300"
MATCH_WEIGHTS = {"override": 1.0, "sw3": 1.0, "sw2": 0.65, "sw1": 0.35}
MIN_AVG_AMOUNT = 5e7     # ETF 20日日均成交额下限（元）
CHASE_RISK_RET5 = 10.0   # 5日涨幅进入过热观察的阈值(%)

WEIGHTS = {"breadth": 0.35, "capital": 0.30, "confirmation": 0.20, "quality": 0.15}

SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------

def _atomic_json_dump(data, path):
    import tempfile
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


def _rank_map(values: dict[str, float]) -> dict[str, float]:
    """{key: 原始值} → {key: 0-100 分位排名}，相同值取平均名次。"""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][1]: 50.0}
    result = {}
    start = 0
    while start < n:
        end = start + 1
        while end < n and items[end][0] == items[start][0]:
            end += 1
        percentile = ((start + end - 1) / 2) / (n - 1) * 100
        for _, key in items[start:end]:
            result[key] = percentile
        start = end
    return result


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _absolute_score(value: float | None, weak: float, strong: float) -> float:
    """把可解释的绝对阈值映射到 0-100；不依赖当日市场强弱。"""
    if value is None or strong == weak:
        return 0.0
    return _clamp((value - weak) / (strong - weak) * 100)


def _blended_score(value: float | None, percentile: float | None,
                   weak: float, strong: float, rank_weight: float = 0.25) -> float:
    absolute = _absolute_score(value, weak, strong)
    relative = 50.0 if percentile is None else percentile
    return (1 - rank_weight) * absolute + rank_weight * relative


def _ratio(numer: float, denom: float) -> float:
    return numer / denom if denom > 0 else 0.0


def _metric(metrics: dict, preferred: str, legacy: str, default=0.0):
    """读取 V3 语义字段，并兼容旧回测构造的 flow_* 字段。"""
    value = metrics.get(preferred)
    return metrics.get(legacy, default) if value is None else value


def _shrink_small_sample(value: float | None, sample_size: int | float | None,
                         prior: float = 50.0) -> float:
    """按 n/(n+10) 向中性先验收缩，防止极小行业制造虚假强信号。"""
    if value is None:
        return prior
    n = max(float(sample_size or 0), 0.0)
    weight = n / (n + 10.0)
    return prior + weight * (float(value) - prior)


def _load_optional_json(filename: str) -> dict:
    try:
        with open(data_path(filename), encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def _normalise_date(value) -> str:
    return str(value or "").replace("-", "")[:8]


def _adjust_etf_corporate_actions(closes: list[float], volumes: list[float]) -> tuple[list[float], list[float], int]:
    """ETF 份额折算会产生非交易性跳点；把历史价格/份额换算到最新口径。"""
    adjusted_closes = list(closes)
    adjusted_volumes = list(volumes)
    adjustments = 0
    for i in range(1, len(adjusted_closes)):
        previous = adjusted_closes[i - 1]
        if previous <= 0:
            continue
        ratio = adjusted_closes[i] / previous
        if 0 < ratio < 0.70 or ratio > 1.43:
            for j in range(i):
                adjusted_closes[j] *= ratio
                adjusted_volumes[j] /= ratio
            adjustments += 1
    return adjusted_closes, adjusted_volumes, adjustments


def _etf_stats(closes: list[float], volumes: list[float], last_date: str = "") -> dict | None:
    """由时间升序 K 线构造推荐和回测共用的 ETF 特征。"""
    if len(closes) < 6 or len(closes) != len(volumes):
        return None
    closes, volumes, adjustment_count = _adjust_etf_corporate_actions(closes, volumes)
    amounts = [c * v for c, v in zip(closes, volumes)]
    returns = [(closes[i] / closes[i - 1] - 1) * 100
               for i in range(max(1, len(closes) - 20), len(closes))
               if closes[i - 1] > 0]
    last20_closes = closes[-20:]
    avg_amount_20d = _mean(amounts[-20:])
    base_amounts = amounts[-20:-5]
    return {
        "price": round(closes[-1], 3),
        "change_pct": round((closes[-1] / closes[-2] - 1) * 100, 2),
        "ret_2d": round((closes[-1] / closes[-3] - 1) * 100, 2) if len(closes) >= 3 else None,
        "ret_5d": round((closes[-1] / closes[-6] - 1) * 100, 2),
        "ret_10d": round((closes[-1] / closes[-11] - 1) * 100, 2) if len(closes) >= 11 else None,
        "ret_20d": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None,
        "ma20_distance": round((closes[-1] / _mean(last20_closes) - 1) * 100, 2),
        "amount_today": round(amounts[-1]),
        "avg_amount_20d": round(avg_amount_20d),
        "amount_ratio_5_20": round(_ratio(_mean(amounts[-5:]), _mean(base_amounts)), 2)
        if base_amounts else None,
        "volatility_20d": round(statistics.pstdev(returns), 2) if len(returns) >= 2 else None,
        "positive_days_5d": sum(1 for i in range(len(closes) - 5, len(closes))
                                  if i > 0 and closes[i] > closes[i - 1]),
        "corporate_action_adjusted": adjustment_count > 0,
        "adjustment_count": adjustment_count,
        "last_date": last_date,
    }


# ------------------------------------------------------------------
# 1. 映射表
# ------------------------------------------------------------------

def _map_file(scheme: str) -> str:
    return f"industry_etf_map_{scheme}.json"


def _output_file(scheme: str) -> str:
    return f"etf_recommend_{scheme}.json"


def ensure_etf_map(scheme: str = SCHEME) -> dict:
    path = data_path(_map_file(scheme))
    if not os.path.exists(path):
        # 老数据目录没有映射表（升级场景）→ 优先从打包资源复制
        bundled = resource_path(_map_file(scheme))
        if os.path.exists(bundled):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(bundled, path)
        elif scheme == SCHEME:
            print("映射表不存在，自动生成...")
            import build_etf_map
            build_etf_map.main()
        else:
            raise FileNotFoundError(f"缺少 {scheme} 行业 ETF 映射表: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------
# 2. ETF 行情快照
# ------------------------------------------------------------------

def _fetch_one_etf(code: str, datalen: int = 90, ref_date: str | None = None):
    """拉单只 ETF 日K（腾讯前复权，新浪兜底），返回 (code, dict|None)。"""
    from momentum_etf import _tencent_kline
    symbol = ("sh" if code.startswith("5") else "sz") + code
    data = _tencent_kline(symbol, datalen)
    if data and ref_date:
        data = [row for row in data if str(row.get("date", "")).replace("-", "") <= ref_date]
    if data and len(data) >= 6:
        closes = [r["close"] for r in data]
        volumes = [r["volume"] for r in data]
        stats = _etf_stats(closes, volumes, data[-1].get("date", ""))
        return (code, {"code": code, **stats}) if stats else (code, None)
    for attempt in range(2):
        try:
            r = requests.get(
                SINA_KLINE_URL,
                params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": datalen},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
                timeout=15,
            )
            data = r.json()
            if not isinstance(data, list) or len(data) < 6:
                return code, None
            if ref_date:
                data = [row for row in data if str(row.get("day", "")).replace("-", "") <= ref_date]
            if len(data) < 6:
                return code, None
            closes = [float(d["close"]) for d in data]
            volumes = [float(d["volume"]) for d in data]
            stats = _etf_stats(closes, volumes, data[-1].get("day", ""))
            return (code, {"code": code, **stats}) if stats else (code, None)
        except Exception:
            if attempt:
                return code, None
            time.sleep(0.5)
    return code, None


def fetch_etf_snapshot(codes: list[str], ref_date: str | None = None) -> dict:
    """多线程拉取候选 ETF；ref_date 用于与行业信号严格对齐。"""
    etfs, fails = {}, 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one_etf, c, 90, ref_date): c for c in codes}
        for f in as_completed(futures):
            code, info = f.result()
            if info:
                etfs[code] = info
            else:
                fails += 1
    snapshot = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": ref_date,
        "etfs": etfs,
    }
    _atomic_json_dump(snapshot, data_path(SNAPSHOT_FILE))
    print(f"  ETF 行情: 成功 {len(etfs)} 只, 失败 {fails} 只")
    if not etfs:
        raise RuntimeError("ETF 行情全部拉取失败")
    return snapshot


# ------------------------------------------------------------------
# 3. 行业信号 + 综合评分
# ------------------------------------------------------------------

def _pivot_counts(db, table: str, period: str, dates: list[str], scheme: str = SCHEME) -> dict[str, dict]:
    """{industry: {"counts": [最新在前], "total": int}}"""
    rows = db.conn.execute(
        f"SELECT industry, date, count, total_stocks FROM {table} "
        f"WHERE scheme=? AND period=? AND is_total=0 AND date IN ({','.join('?' * len(dates))})",
        [scheme, period] + dates,
    ).fetchall()
    out = {}
    for ind, d, cnt, total in rows:
        e = out.setdefault(ind, {"counts": [0] * len(dates), "total": int(total or 0)})
        idx = dates.index(d)
        e["counts"][idx] = int(cnt or 0)
    return out


def _pivot_flow(db, dates: list[str], scheme: str = SCHEME) -> dict[str, dict]:
    rows = db.conn.execute(
        "SELECT industry, date, turnover, net_flow FROM daily_capital_flow "
        f"WHERE scheme=? AND is_total=0 AND date IN ({','.join('?' * len(dates))})",
        [scheme] + dates,
    ).fetchall()
    out = {}
    for ind, d, to, nf in rows:
        e = out.setdefault(ind, {"turnover": [0.0] * len(dates), "net_flow": [0.0] * len(dates)})
        idx = dates.index(d)
        e["turnover"][idx] = float(to or 0)
        e["net_flow"][idx] = float(nf or 0)
    return out


def _recent_base_split(series: list[float], n_dates: int):
    """series 最新在前 → (近5日均值, 之前窗口均值)"""
    recent = series[:RECENT_DAYS]
    base = series[RECENT_DAYS:n_dates]
    return _mean(recent), _mean(base)


def _liquidity_score(avg_amount: float | None) -> float:
    if not avg_amount or avg_amount <= 0:
        return 0.0
    low = math.log10(MIN_AVG_AMOUNT)
    high = math.log10(1e9)
    return _absolute_score(math.log10(avg_amount), low, high)


def _carrier_score(snapshot: dict) -> float:
    """同类 ETF 载体质量：短趋势确认、流动性和不过热。"""
    ret2 = snapshot.get("ret_2d")
    ret5 = snapshot.get("ret_5d")
    ma20 = snapshot.get("ma20_distance")
    volatility = snapshot.get("volatility_20d")
    amount_ratio = snapshot.get("amount_ratio_5_20")
    trend = (
        0.35 * _absolute_score(ret2, -1.0, 3.0)
        + 0.30 * _absolute_score(ret5, -2.0, 7.0)
        + 0.20 * _absolute_score(ma20, -3.0, 6.0)
        + 0.15 * _absolute_score(amount_ratio, 0.7, 1.6)
    )
    overheat = max(0.0, (ret5 or 0) - 9.0) * 2.0
    overheat += max(0.0, (ma20 or 0) - 9.0) * 1.5
    overheat += max(0.0, (volatility or 0) - 4.5) * 2.0
    return _clamp(trend - overheat)


def _choose_candidate(candidates: list[dict], snap_etfs: dict) -> dict | None:
    """在同类产品中兼顾匹配精度、流动性和载体状态。"""
    match_scores = {"override": 100.0, "sw3": 100.0, "sw2": 76.0, "sw1": 55.0}
    match_priority = {"override": 3, "sw3": 3, "sw2": 2, "sw1": 1}
    available = []
    for candidate in candidates:
        snapshot = snap_etfs.get(candidate.get("code"))
        if not snapshot:
            continue
        liquid = snapshot.get("avg_amount_20d", 0) >= MIN_AVG_AMOUNT
        selection_score = (
            0.50 * match_scores.get(candidate.get("match_level", "sw3"), 55.0)
            + 0.25 * _liquidity_score(snapshot.get("avg_amount_20d"))
            + 0.25 * _carrier_score(snapshot)
        )
        available.append({
            "candidate": candidate,
            "snapshot": snapshot,
            "liquid": liquid,
            "match_priority": match_priority.get(candidate.get("match_level", "sw3"), 1),
            "selection_score": round(selection_score, 1),
        })
    if not available:
        return None
    pool = [item for item in available if item["liquid"]] or available
    best_level = max(item["match_priority"] for item in pool)
    pool = [item for item in pool if item["match_priority"] == best_level]
    return max(pool, key=lambda item: item["selection_score"])


def _score_components(metrics: dict, etf: dict | None, ranks: dict) -> dict:
    """纯函数评分，便于 walk-forward 回测与边界测试共用。"""
    breadth_level = _blended_score(metrics["h5_pct"], ranks.get("h5_pct"), 0.0, 8.0)
    breadth_accel = _blended_score(metrics["breadth_accel_pp"], ranks.get("breadth_accel_pp"), 0.0, 3.0)
    net_breadth = _blended_score(metrics["net_breadth_2d"], ranks.get("net_breadth_2d"), 0.0, 8.0)
    breadth = 0.35 * breadth_level + 0.45 * breadth_accel + 0.20 * net_breadth

    participation_ratio = _metric(
        metrics, "directional_participation_ratio", "flow_ratio")
    participation_persistence = _metric(
        metrics, "directional_participation_persistence", "flow_persistence")
    participation_accel = _metric(
        metrics, "directional_participation_accel", "flow_accel")
    flow_strength = _blended_score(
        participation_ratio,
        ranks.get("directional_participation_ratio", ranks.get("flow_ratio")),
        0.0, 0.20)
    flow_persistence = _blended_score(
        participation_persistence,
        ranks.get("directional_participation_persistence", ranks.get("flow_persistence")),
        0.2, 0.8)
    flow_accel = _blended_score(
        participation_accel,
        ranks.get("directional_participation_accel", ranks.get("flow_accel")),
        0.0, 0.15)
    volume = _blended_score(metrics["vol_ratio"], ranks.get("vol_ratio"), 0.8, 1.5)
    capital = 0.40 * flow_strength + 0.30 * flow_persistence + 0.20 * flow_accel + 0.10 * volume

    if etf:
        ret2 = _blended_score(etf.get("ret_2d"), ranks.get("ret_2d"), 0.0, 3.0)
        ret5 = _blended_score(etf.get("ret_5d"), ranks.get("ret_5d"), 0.0, 8.0)
        ma20 = _blended_score(etf.get("ma20_distance"), ranks.get("ma20_distance"), -1.0, 6.0)
        positive_days = _absolute_score(etf.get("positive_days_5d"), 1.0, 4.0)
        confirmation = 0.35 * ret2 + 0.30 * ret5 + 0.20 * ma20 + 0.15 * positive_days
    else:
        confirmation = 0.0

    low_quality = 100.0 - _absolute_score(metrics["lows_2d_pct"], 0.0, 6.0)
    volatility = etf.get("volatility_20d") if etf else None
    volatility_quality = 100.0 - _absolute_score(volatility, 2.0, 5.0) if volatility is not None else 0.0
    liquidity = _liquidity_score(etf.get("avg_amount_20d")) if etf else 0.0
    price2 = (etf or {}).get("ret_2d")
    positive_evidence = metrics["breadth_accel_pp"] > 0 or participation_accel > 0
    if not positive_evidence:
        agreement = 45.0
    elif price2 is not None and price2 >= 0:
        agreement = 100.0
    elif price2 is not None and price2 >= -1.0:
        agreement = 60.0
    else:
        agreement = 10.0
    quality = 0.35 * low_quality + 0.25 * volatility_quality + 0.25 * liquidity + 0.15 * agreement

    base = sum(WEIGHTS[key] * value for key, value in {
        "breadth": breadth,
        "capital": capital,
        "confirmation": confirmation,
        "quality": quality,
    }.items())

    penalty = 0.0
    reasons = []

    def add_penalty(points: float, label: str):
        nonlocal penalty
        points = round(max(points, 0.0), 1)
        if points > 0:
            penalty += points
            reasons.append(f"{label} -{points:g}")

    if etf:
        add_penalty(min(12.0, max(0.0, (etf.get("ret_2d") or 0) - 6.0) * 1.5), "短时过急")
        add_penalty(min(12.0, max(0.0, (etf.get("ret_5d") or 0) - 8.0) * 1.8), "短线过热")
        add_penalty(min(8.0, max(0.0, (etf.get("ret_20d") or 0) - 15.0) * 0.8), "中期过热")
        add_penalty(min(10.0, max(0.0, (etf.get("ma20_distance") or 0) - 8.0) * 1.2), "偏离均线")
        add_penalty(min(6.0, max(0.0, (etf.get("amount_ratio_5_20") or 0) - 2.2) * 4.0), "量能过激")
        add_penalty(min(6.0, max(0.0, (etf.get("volatility_20d") or 0) - 4.0) * 2.0), "波动过大")
        if metrics["breadth_accel_pp"] >= 0.8 and (etf.get("ret_2d") or 0) < -1.5:
            add_penalty(5.0, "宽价背离")
    if participation_ratio >= 0.08 and participation_persistence < 0.4:
        add_penalty(6.0, "方向参与脉冲")
    if metrics["lows_rising"]:
        add_penalty(4.0, "内部转弱")

    heat_breadth = _blended_score(metrics["h5_pct"], ranks.get("h5_pct"), 0.0, 8.0)
    heat_flow = _blended_score(
        participation_ratio,
        ranks.get("directional_participation_ratio", ranks.get("flow_ratio")),
        0.0, 0.20)
    heat_volume = _blended_score(metrics["vol_ratio"], ranks.get("vol_ratio"), 0.8, 1.6)
    heat_price = _blended_score((etf or {}).get("ret_5d"), ranks.get("ret_5d"), 0.0, 8.0) if etf else 0.0
    heat = 0.35 * heat_breadth + 0.30 * heat_flow + 0.15 * heat_volume + 0.20 * heat_price

    # “下一热点”不是当前最热：信号达到可确认区间后，继续升温意味着空间被消耗。
    add_penalty(min(20.0, max(0.0, heat - 68.0) * 0.65), "热度拥挤")

    score = _clamp(base - min(penalty, 30.0))
    return {
        "score": round(score, 1),
        "heat_score": round(_clamp(heat), 1),
        "penalty": round(min(penalty, 30.0), 1),
        "penalty_reasons": reasons,
        "signals": {
            "breadth": round(breadth, 1),
            "directional_participation_proxy": round(capital, 1),
            "confirmation": round(confirmation, 1),
            "quality": round(quality, 1),
            # 兼容旧页面或外部读取方。
            "capital": round(capital, 1),
            "highs": round(breadth, 1),
            "flow": round(capital, 1),
            "price": round(confirmation, 1),
            "volume": round(volume, 1),
        },
    }


def _stage_for(score: float, penalty: float, metrics: dict, etf: dict | None) -> str:
    ret2 = (etf or {}).get("ret_2d")
    ret5 = (etf or {}).get("ret_5d")
    ma20 = (etf or {}).get("ma20_distance")
    if penalty >= 8 or (ret2 or 0) >= 8 or (ret5 or 0) >= CHASE_RISK_RET5 or (ma20 or 0) >= 10:
        return "过热"
    participation_persistence = _metric(
        metrics, "directional_participation_persistence", "flow_persistence")
    participation_accel = _metric(
        metrics, "directional_participation_accel", "flow_accel")
    if (score >= 62 and metrics["h5_pct"] >= 3 and participation_persistence >= 0.6
            and (ret5 or 0) >= 0):
        return "扩散"
    if score >= 52 and metrics["breadth_accel_pp"] > 0 and (ret2 is None or ret2 >= -0.5):
        return "启动"
    if score >= 38 and (metrics["breadth_accel_pp"] > 0 or participation_accel > 0):
        return "潜伏"
    return "观察"


def _confidence(chosen: dict | None, metrics: dict) -> tuple[float, str]:
    if not chosen:
        return 20.0, "低"
    candidate, etf = chosen["candidate"], chosen["snapshot"]
    match = {"override": 100, "sw3": 100, "sw2": 78, "sw1": 58}.get(
        candidate.get("match_level", "sw3"), 58)
    fields = ("ret_2d", "ret_5d", "ret_20d", "ma20_distance", "amount_ratio_5_20", "volatility_20d")
    completeness = sum(etf.get(key) is not None for key in fields) / len(fields) * 100
    participation_days = _metric(
        metrics, "directional_participation_days", "flow_days")
    flow_coverage = min(participation_days, RECENT_DAYS) / RECENT_DAYS * 100
    value = 0.45 * match + 0.30 * completeness + 0.15 * flow_coverage + 0.10 * _liquidity_score(etf.get("avg_amount_20d"))
    label = "高" if value >= 82 else ("中" if value >= 65 else "低")
    return round(value, 1), label


def _candidate_links(candidates: list[dict], snap_etfs: dict) -> list[dict]:
    """把行业映射展开为 ETF 证据边；同一行业/ETF 只保留最精确匹配。"""
    links: dict[str, dict] = {}
    for candidate in candidates or []:
        code = str(candidate.get("code") or "")
        snapshot = snap_etfs.get(code)
        if not code or not snapshot:
            continue
        level = candidate.get("match_level", "sw3")
        weight = MATCH_WEIGHTS.get(level, MATCH_WEIGHTS["sw1"])
        existing = links.get(code)
        if existing and existing["match_weight"] >= weight:
            continue
        etf = dict(snapshot)
        etf.update({
            "code": code,
            "name": candidate.get("name") or snapshot.get("name") or code,
            "match": candidate.get("match"),
            "match_level": level,
            "matched_industry": candidate.get("matched_industry"),
            "match_label": candidate.get("match_label"),
        })
        links[code] = {
            "etf": etf,
            "match_level": level,
            "match_weight": weight,
            "matched_industry": candidate.get("matched_industry"),
            "match_label": candidate.get("match_label"),
        }
    return sorted(links.values(), key=lambda item: (-item["match_weight"], item["etf"]["code"]))


def _score_rows(db, dates: list[str], mapping: dict, snap_etfs: dict, scheme: str = SCHEME) -> list[dict]:
    """对指定日期窗口（最新在前）计算全部行业评分行。
    snap_etfs: {code: {price, ret_2d, ret_5d, ma20_distance, ...}}
    回测时传入历史切片快照即可复现当日推荐。
    """
    highs = _pivot_counts(db, "daily_new_highs", "month", dates, scheme)
    highs60 = _pivot_counts(db, "daily_new_highs", "60d", dates, scheme)
    lows = _pivot_counts(db, "daily_new_lows", "month", dates, scheme)
    flow = _pivot_flow(db, dates, scheme)

    # ---- 原始指标 ----
    metrics = {}
    industries = sorted(set(highs) | set(flow))
    for ind in industries:
        h = highs.get(ind, {"counts": [0] * len(dates), "total": 0})
        total = max(h["total"], 1)
        h_pct = [c / total * 100 for c in h["counts"]]
        h5, h_prev = _recent_base_split(h_pct, len(dates))
        h2, h_prev3 = _mean(h_pct[:2]), _mean(h_pct[2:5])

        f = flow.get(ind, {"turnover": [0.0] * len(dates), "net_flow": [0.0] * len(dates)})
        t5, t_prev = _recent_base_split(f["turnover"], len(dates))
        vol_ratio = t5 / t_prev if t_prev > 0 else 1.0
        net5 = sum(f["net_flow"][:RECENT_DAYS])
        to5 = sum(f["turnover"][:RECENT_DAYS])
        flow_ratio = _ratio(net5, to5)
        recent_flow = _ratio(sum(f["net_flow"][:2]), sum(f["turnover"][:2]))
        previous_flow = _ratio(sum(f["net_flow"][2:5]), sum(f["turnover"][2:5]))
        valid_flow = [(to, nf) for to, nf in zip(f["turnover"][:RECENT_DAYS], f["net_flow"][:RECENT_DAYS]) if to > 0]

        h60 = highs60.get(ind, {"counts": [0] * len(dates), "total": total})
        t60 = max(h60["total"], 1)
        h60_pct = [c / t60 * 100 for c in h60["counts"]]
        h60_5, h60_prev = _recent_base_split(h60_pct, len(dates))

        lo = lows.get(ind, {"counts": [0] * len(dates), "total": total})
        lo_pct = [c / total * 100 for c in lo["counts"]]
        lo5, lo_prev = _recent_base_split(lo_pct, len(dates))
        lo2 = _mean(lo_pct[:2])

        metrics[ind] = {
            "stock_count": int(h.get("total") or 0),
            "h5_pct": round(h5, 3),
            "growth": round(min(h5 / max(h_prev, 0.5), 20.0), 2),
            "breadth_2d_pct": round(h2, 3),
            "breadth_accel_pp": round(h2 - h_prev3, 3),
            "breadth_vs_base_pp": round(h2 - _mean(h_pct[5:]), 3),
            "net_breadth_2d": round(h2 - lo2, 3),
            "highs_5d_count": sum(h["counts"][:RECENT_DAYS]),
            "vol_ratio": round(vol_ratio, 2),
            "directional_participation_5d": round(net5),
            "directional_participation_ratio": round(flow_ratio, 4),
            "directional_participation_accel": round(recent_flow - previous_flow, 4),
            "directional_participation_persistence": (
                round(sum(1 for _, nf in valid_flow if nf > 0) / len(valid_flow), 2)
                if valid_flow else 0.0
            ),
            "positive_directional_participation_days": sum(
                1 for _, nf in valid_flow if nf > 0),
            "directional_participation_days": len(valid_flow),
            "lows_2d_pct": round(lo2, 3),
            "resonance": h5 > h_prev and h60_5 > h60_prev,
            "lows_rising": lo5 > lo_prev * 1.5 and lo5 >= 1.0,
        }

    # ---- 在每个行业的候选产品中选择更合适的 ETF 载体 ----
    chosen = {ind: _choose_candidate(mapping.get(ind, []), snap_etfs) for ind in industries}
    chosen = {ind: item for ind, item in chosen.items() if item}

    rank_fields = (
        "h5_pct", "breadth_accel_pp", "net_breadth_2d", "vol_ratio",
        "directional_participation_ratio", "directional_participation_accel",
        "directional_participation_persistence",
    )
    rank_maps = {field: _rank_map({i: m[field] for i, m in metrics.items()}) for field in rank_fields}
    for field in ("ret_2d", "ret_5d", "ma20_distance"):
        rank_maps[field] = _rank_map({
            ind: item["snapshot"].get(field) for ind, item in chosen.items()
            if item["liquid"] and item["snapshot"].get(field) is not None
        })

    # ---- 启动分、当前热度、阶段与置信度 ----
    rows = []
    for ind in industries:
        m = metrics[ind]
        picked = chosen.get(ind)
        c = picked["candidate"] if picked else None
        etf = picked["snapshot"] if picked else None
        ranks = {field: values.get(ind) for field, values in rank_maps.items()}
        scored = _score_components(m, etf, ranks)
        score = scored["score"]
        stage = _stage_for(score, scored["penalty"], m, etf)
        confidence, confidence_label = _confidence(picked, m)
        tags = []
        if m["breadth_accel_pp"] >= 0.8:
            tags.append("宽度加速")
        if m["positive_directional_participation_days"] >= 3:
            tags.append("方向参与连续")
        if etf and (etf.get("ret_2d") or 0) > 0 and (etf.get("ma20_distance") or 0) > 0:
            tags.append("价格确认")
        if m["resonance"]:
            tags.append("多周期共振")
        if m["lows_rising"]:
            tags.append("分化")
        if stage == "过热":
            tags.append("追高风险")

        row = {
            "industry": ind,
            "score": score,
            "ignition_score": score,
            "heat_score": scored["heat_score"],
            "signals": scored["signals"],
            "metrics": m,
            "tags": tags,
            "stage": stage,
            "penalty": scored["penalty"],
            "penalty_reasons": scored["penalty_reasons"],
            "confidence": confidence,
            "confidence_label": confidence_label,
            "has_etf": bool(picked),
            "liquid": bool(picked and picked["liquid"]),
            "_etf_candidates": _candidate_links(mapping.get(ind, []), snap_etfs),
        }
        if picked:
            row["etf"] = dict(etf)
            row["etf"].update({
                "code": c["code"],
                "name": c["name"],
                "match": c.get("match"),
                "match_level": c.get("match_level", "sw3"),
                "matched_industry": c.get("matched_industry", ind),
                "match_label": c.get("match_label"),
                "selection_score": picked["selection_score"],
            })
        rows.append(row)
    return rows


def _pick_top(rows: list[dict]) -> list[dict]:
    """每行业 1 只，ETF 代码去重；下一热点优先潜伏/启动阶段。"""
    eligible = [r for r in rows if r.get("liquid") and r.get("stage") != "过热"]
    def stage_tier(row):
        if row.get("stage") in ("潜伏", "启动") and row.get("score", 0) >= 45:
            return 0
        if row.get("stage") == "扩散":
            return 1
        return 2
    eligible.sort(key=lambda r: (stage_tier(r), -r["score"]))
    top, seen_codes = [], set()
    for r in eligible:
        code = r["etf"]["code"]
        if code in seen_codes:
            continue
        seen_codes.add(code)
        top.append(r)
        if len(top) >= TOP_N:
            break
    for i, r in enumerate(top, 1):
        r["rank"] = i
    return top


def _build_market_regime(payload: dict | None, as_of: str | None = None) -> dict:
    """把市场温度转成“是否允许承担热点风险”的门控信号。"""
    payload = payload or {}
    if "score" in payload and "state" in payload and not payload.get("rows"):
        return dict(payload)
    cutoff = _normalise_date(as_of)
    rows = [
        row for row in payload.get("rows", [])
        if not cutoff or _normalise_date(row.get("date")) <= cutoff
    ]
    if not rows:
        return {
            "score": 50.0, "state": "unknown", "permission": "unknown",
            "temperature": None, "date": None, "status": "missing",
            "reason": "缺少可用的市场温度数据",
        }
    row = max(rows, key=lambda item: _normalise_date(item.get("date")))
    temperature = row.get("temperature")
    if temperature is None:
        return {
            "score": 50.0, "state": "unknown", "permission": "unknown",
            "temperature": None, "date": _normalise_date(row.get("date")),
            "status": "missing", "reason": "市场温度记录缺少温度值",
        }
    temperature = float(temperature)
    if temperature < 22:
        score, state, permission = 18.0, "risk_off", "restricted"
    elif temperature < 35:
        score, state, permission = 38.0, "cautious", "selective"
    elif temperature <= 70:
        score, state, permission = 78.0, "supportive", "allowed"
    elif temperature <= 82:
        score, state, permission = 62.0, "heated", "selective"
    else:
        score, state, permission = 32.0, "overheated", "restricted"

    source_date = _normalise_date(row.get("date"))
    stale_days = None
    if cutoff and source_date:
        try:
            stale_days = (datetime.strptime(cutoff, "%Y%m%d")
                          - datetime.strptime(source_date, "%Y%m%d")).days
        except ValueError:
            stale_days = None
    status = "fresh" if stale_days is None or stale_days <= 5 else "stale"
    if status == "stale":
        # 陈旧门控只保留中性影响，不能把两周前的 risk-on 当成今天的许可。
        score = 50.0 + (score - 50.0) * 0.25
    return {
        "score": round(score, 1),
        "state": state,
        "permission": permission if status == "fresh" else "unknown",
        "temperature": round(temperature, 1),
        "date": source_date,
        "status": status,
        "stale_days": stale_days,
        "reason": (
            f"市场温度 {temperature:.1f}"
            if status == "fresh"
            else f"市场温度数据滞后 {stale_days} 天，仅作弱参考"
        ),
    }


def _crowding_index(crowding: dict | None, as_of: str | None = None) -> dict[str, dict]:
    crowding = crowding or {}
    if "industries" not in crowding:
        return {
            str(key): value for key, value in crowding.items()
            if isinstance(value, dict)
        }
    cutoff = _normalise_date(as_of)
    trade_date = _normalise_date(crowding.get("trade_date"))
    if cutoff and trade_date and trade_date > cutoff:
        return {}
    return {
        str(item.get("industry")): item
        for item in crowding.get("industries", [])
        if item.get("industry")
    }


def _external_etf_index(external: dict | None, as_of: str | None = None) -> dict[str, dict]:
    external = external or {}
    cutoff = _normalise_date(as_of)
    trade_date = _normalise_date(external.get("trade_date"))
    if cutoff and trade_date and trade_date > cutoff:
        return {}
    source = external.get("etfs", external)
    return {
        str(code): value for code, value in source.items()
        if isinstance(value, dict)
    }


def _industry_crowding_risk(item: dict | None) -> tuple[float, list[str]]:
    if not item:
        return 35.0, []
    state = item.get("risk_state", "normal")
    state_score = {
        "normal": 20.0, "watch": 48.0, "fragile": 70.0, "unwind": 88.0,
    }.get(state, 45.0)
    crowding_score = float(item.get("crowding_score") or 0)
    fragility = item.get("external_fragility_score")
    values = [state_score]
    if crowding_score:
        values.append(crowding_score)
    if fragility is not None:
        values.append(float(fragility))
    risk = 0.55 * max(values) + 0.45 * _mean(values)
    reasons = list(item.get("risk_reasons") or [])
    if not reasons and item.get("state_label"):
        reasons.append(str(item["state_label"]))
    return _clamp(risk), reasons[:3]


def _relative_strength_signal(etf: dict, benchmark: dict | None) -> tuple[float, dict]:
    benchmark = benchmark or {}
    values = []
    details = {"benchmark": BENCHMARK_CODE, "relative_5d": None, "relative_20d": None}
    for days, weight, scale in ((5, 0.55, 10.0), (20, 0.45, 4.0)):
        own = etf.get(f"ret_{days}d")
        base = benchmark.get(f"ret_{days}d")
        if own is None or base is None:
            continue
        relative = float(own) - float(base)
        details[f"relative_{days}d"] = round(relative, 2)
        values.append((weight, _clamp(50.0 + relative * scale)))
    if not values:
        return 35.0, details
    total_weight = sum(weight for weight, _ in values)
    return (
        round(sum(weight * score for weight, score in values) / total_weight, 1),
        details,
    )


def _prediction_for(score: float, calibration: dict | None) -> dict:
    """只消费显式的样本外校准器；没有映射时绝不生成伪概率。"""
    calibration = calibration or {}
    if calibration.get("status") not in ("calibrated", "ready"):
        return {
            "probability": None,
            "status": "insufficient",
            "reason": calibration.get("reason") or "尚无 V3 样本外概率校准",
        }

    probability = None
    model = calibration.get("model") or {}
    bins = calibration.get("bins") or calibration.get("score_bins") or []
    for item in bins:
        lower = item.get("min_score", item.get("lower", item.get("min", 0)))
        upper = item.get("max_score", item.get("upper", item.get("max", 100)))
        if float(lower) <= score <= float(upper):
            probability = item.get(
                "probability", item.get("hit_rate", item.get("win_rate")))
            break
    if probability is None:
        points = calibration.get("points") or []
        parsed = sorted(
            (float(item["score"]), float(item["probability"]))
            for item in points
            if item.get("score") is not None and item.get("probability") is not None
        )
        if parsed:
            if score <= parsed[0][0]:
                probability = parsed[0][1]
            elif score >= parsed[-1][0]:
                probability = parsed[-1][1]
            else:
                for (x0, y0), (x1, y1) in zip(parsed, parsed[1:]):
                    if x0 <= score <= x1:
                        probability = y0 + (score - x0) / (x1 - x0) * (y1 - y0)
                        break
    if probability is None:
        knots = calibration.get("score_knots") or model.get("score_knots") or []
        probabilities = calibration.get("probabilities") or model.get("probabilities") or []
        if knots and len(knots) == len(probabilities):
            parsed = sorted(
                (float(knot), float(value))
                for knot, value in zip(knots, probabilities)
            )
            probability = parsed[-1][1]
            for knot, value in parsed:
                if score <= knot:
                    probability = value
                    break
    if probability is None and calibration.get("slope") is not None:
        intercept = float(calibration.get("intercept") or 0)
        logit = intercept + float(calibration["slope"]) * score
        probability = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))
    if probability is None:
        return {
            "probability": None, "status": "insufficient",
            "reason": "校准文件没有可用于该分数的概率映射",
        }
    probability = float(probability)
    if probability <= 1.0:
        probability *= 100.0
    return {
        "probability": round(_clamp(probability), 1),
        "status": "calibrated",
        "method": calibration.get("method"),
        "sample_size": calibration.get("sample_size"),
    }


def _load_v3_calibration() -> tuple[dict, dict]:
    payload = _load_optional_json("etf_backtest.json")
    calibration = (
        payload.get("calibration")
        or (payload.get("etf") or {}).get("calibration")
        or (payload.get("industry") or {}).get("calibration")
        or {}
    )
    if payload.get("model_version") != V3_MODEL_VERSION:
        reason = "回测尚未升级为 ETF 热点 V3，不能提供概率"
        return {
            "status": "insufficient", "reason": reason,
            "model_version": payload.get("model_version"),
            "sample_size": calibration.get("sample_size"),
        }, {"status": "insufficient", "reason": reason}
    if calibration.get("status") not in ("calibrated", "ready"):
        reason = calibration.get("reason") or "V3 样本量不足，概率尚未校准"
        return {
            "status": "insufficient", "reason": reason,
            "model_version": V3_MODEL_VERSION,
            "sample_size": calibration.get("sample_size"),
        }, {"status": "insufficient", "reason": reason}
    model = calibration.get("model") or {}
    has_mapping = bool(
        calibration.get("bins")
        or calibration.get("score_bins")
        or calibration.get("points")
        or calibration.get("score_knots")
        or model.get("score_knots")
        or calibration.get("slope") is not None
    )
    if not has_mapping:
        reason = "V3 校准状态可用，但缺少分数到概率的映射"
        return {
            "status": "insufficient", "reason": reason,
            "model_version": V3_MODEL_VERSION,
            "sample_size": calibration.get("sample_size"),
        }, {"status": "insufficient", "reason": reason}
    summary = {
        "status": "calibrated",
        "model_version": V3_MODEL_VERSION,
        "method": calibration.get("method"),
        "sample_size": calibration.get("sample_size"),
        "independent_dates": calibration.get("independent_dates"),
        "base_rate": calibration.get("base_rate"),
        "horizon": calibration.get("horizon", "t5"),
        "validation_brier": (calibration.get("validation") or {}).get("brier"),
        "as_of": calibration.get("as_of"),
    }
    return summary, calibration


def _aggregate_etf_candidates(industry_rows: list[dict], *, crowding=None,
                              external=None, regime=None, benchmark=None) -> list[dict]:
    """把多条 SW3 行业证据聚合成唯一 ETF 行。

    ``industry_rows`` 可直接使用 ``_score_rows`` 的输出；回测旧切片若没有
    ``_etf_candidates``，会回退到旧行内的单只 ``etf``。
    """
    crowd_index = _crowding_index(crowding)
    external_index = _external_etf_index(external)
    regime = _build_market_regime(regime or {})
    groups: dict[str, dict] = {}

    for row in industry_rows:
        metrics = row.get("metrics") or {}
        industry = row.get("industry")
        if not industry:
            continue
        links = list(row.get("_etf_candidates") or [])
        if not links and row.get("etf"):
            etf = dict(row["etf"])
            level = etf.get("match_level", "sw3")
            links = [{
                "etf": etf,
                "match_level": level,
                "match_weight": MATCH_WEIGHTS.get(level, MATCH_WEIGHTS["sw1"]),
                "matched_industry": etf.get("matched_industry"),
                "match_label": etf.get("match_label"),
            }]
        if not links:
            continue

        sample_size = int(metrics.get("stock_count") or metrics.get("total_stocks") or 0)
        shrinkage = sample_size / (sample_size + 10.0) if sample_size >= 0 else 0.0
        signals = row.get("signals") or {}
        raw_diffusion = signals.get("breadth", signals.get("highs", 0.0))
        raw_proxy = signals.get(
            "directional_participation_proxy", signals.get("capital", signals.get("flow", 0.0)))
        diffusion = _shrink_small_sample(raw_diffusion, sample_size)
        directional_proxy = _shrink_small_sample(raw_proxy, sample_size)
        crowd_item = crowd_index.get(str(industry))
        crowd_risk, crowd_reasons = _industry_crowding_risk(crowd_item)

        for link in links:
            etf = dict(link.get("etf") or {})
            code = str(etf.get("code") or "")
            if not code:
                continue
            match_level = link.get("match_level", etf.get("match_level", "sw3"))
            match_weight = float(
                link.get("match_weight", MATCH_WEIGHTS.get(match_level, MATCH_WEIGHTS["sw1"])))
            group = groups.setdefault(code, {
                "etf": etf,
                "related": [],
            })
            existing_level = group["etf"].get("match_level", "sw1")
            if MATCH_WEIGHTS.get(match_level, 0) > MATCH_WEIGHTS.get(existing_level, 0):
                group["etf"] = etf
            group["related"].append({
                "industry": industry,
                "match_level": match_level,
                "match_weight": round(match_weight, 2),
                "sample_size": sample_size,
                "shrinkage": round(shrinkage, 3),
                "diffusion": round(diffusion, 1),
                "directional_participation_proxy": round(directional_proxy, 1),
                "crowding_risk": round(crowd_risk, 1),
                "crowding_state": (crowd_item or {}).get("risk_state"),
                "risk_reasons": crowd_reasons,
            })

    results = []
    for code, group in groups.items():
        etf = group["etf"]
        related = group["related"]
        total_weight = sum(item["match_weight"] for item in related) or 1.0
        weighted = lambda field: sum(
            item["match_weight"] * item[field] for item in related) / total_weight
        diffusion = weighted("diffusion")
        directional_proxy = weighted("directional_participation_proxy")
        crowding_risk = weighted("crowding_risk")

        share_row = external_index.get(code) or {}
        share_change_pct = share_row.get("share_change_pct")
        if share_change_pct is None:
            demand = 0.65 * directional_proxy + 0.35 * 50.0
        else:
            share_score = _clamp(50.0 + float(share_change_pct) * 12.5)
            demand = 0.65 * share_score + 0.35 * directional_proxy

        relative_strength, relative_details = _relative_strength_signal(etf, benchmark)
        liquidity = _liquidity_score(etf.get("avg_amount_20d"))
        volatility = etf.get("volatility_20d")
        volatility_quality = (
            100.0 - _absolute_score(volatility, 2.0, 5.5)
            if volatility is not None else 25.0
        )
        tradability = 0.72 * liquidity + 0.28 * volatility_quality
        regime_score = float(regime.get("score", 50.0))

        raw_score = (
            0.15 * regime_score
            + 0.25 * demand
            + 0.25 * diffusion
            + 0.25 * relative_strength
            + 0.10 * tradability
        )

        price_risks, risk_reasons = [], []
        ret5 = float(etf.get("ret_5d") or 0)
        ret20 = float(etf.get("ret_20d") or 0)
        ma20 = float(etf.get("ma20_distance") or 0)
        if ret5 > 10:
            price_risks.append(_clamp(50 + (ret5 - 10) * 5))
            risk_reasons.append(f"ETF 5日涨幅 {ret5:.1f}% 已明显延伸")
        if ret20 > 18:
            price_risks.append(_clamp(45 + (ret20 - 18) * 2.5))
            risk_reasons.append(f"ETF 20日涨幅 {ret20:.1f}% 偏热")
        if ma20 > 9:
            price_risks.append(_clamp(50 + (ma20 - 9) * 5))
            risk_reasons.append(f"ETF 偏离20日均线 {ma20:.1f}%")
        if volatility is not None and float(volatility) > 4.5:
            price_risks.append(_clamp(45 + (float(volatility) - 4.5) * 8))
            risk_reasons.append("ETF 近期波动偏高")
        price_risk = max(price_risks, default=25.0)
        if share_change_pct is not None and float(share_change_pct) < -3:
            price_risk = max(price_risk, 68.0)
            risk_reasons.append(f"交易所份额减少 {abs(float(share_change_pct)):.1f}%")
        risk_reasons.extend(
            reason for item in related for reason in item.get("risk_reasons", [])
            if reason not in risk_reasons
        )
        regime_risk = 75.0 if regime.get("permission") == "restricted" else (
            52.0 if regime.get("permission") in ("selective", "unknown") else 25.0)
        liquidity_risk = 100.0 - liquidity
        risk_score = _clamp(
            0.45 * crowding_risk + 0.30 * price_risk
            + 0.15 * liquidity_risk + 0.10 * regime_risk
        )
        opportunity_score = _clamp(raw_score - max(0.0, risk_score - 38.0) * 0.42)

        completeness_fields = (
            "ret_5d", "ret_20d", "ma20_distance", "avg_amount_20d",
            "volatility_20d", "amount_ratio_5_20",
        )
        completeness = sum(etf.get(key) is not None for key in completeness_fields) / len(completeness_fields)
        carrier_score = weighted("match_weight") / max(MATCH_WEIGHTS.values()) * 100
        quality_score = (
            35 * completeness
            + 15 * min(len(related) / 2, 1.0)
            + 15 * (max(item["match_weight"] for item in related))
            + 10 * bool(benchmark and benchmark.get("ret_5d") is not None)
            + 10 * (1.0 if regime.get("status") == "fresh" else 0.5 if regime.get("status") == "stale" else 0)
            + 10 * bool(crowd_index)
            + 5 * (share_change_pct is not None)
        )
        quality_score = _clamp(quality_score)
        quality_reasons = []
        if completeness < 1:
            quality_reasons.append("ETF 行情特征不完整")
        if not benchmark or benchmark.get("ret_5d") is None:
            quality_reasons.append("缺少510300基准快照")
        if share_change_pct is None:
            quality_reasons.append("缺少交易所ETF份额变化")
        if regime.get("status") != "fresh":
            quality_reasons.append("市场温度缺失或陈旧")
        if not crowd_index:
            quality_reasons.append("缺少行业拥挤度")

        if quality_score < 45 or not related:
            stage = "insufficient"
        elif risk_score >= 64 or regime.get("permission") == "restricted":
            stage = "avoid"
        elif (opportunity_score >= 66 and relative_strength >= 55
              and demand >= 52 and diffusion >= 50):
            stage = "confirmed"
        elif (opportunity_score >= V3_MIN_SCORE and diffusion >= 48
              and max(demand, relative_strength) >= 50 and risk_score < 58):
            stage = "emerging"
        else:
            stage = "watch"

        primary = max(
            related,
            key=lambda item: (
                item["match_weight"] * max(item["shrinkage"], 0.05),
                item["diffusion"] + item["directional_participation_proxy"],
            ),
        )
        evidence = [
            f"行业扩散 {diffusion:.1f}",
            f"相对510300强度 {relative_strength:.1f}",
        ]
        if share_change_pct is not None:
            evidence.append(f"交易所ETF份额变化 {float(share_change_pct):+.2f}%")
        else:
            evidence.append(f"方向成交参与代理 {directional_proxy:.1f}（非真实资金流）")
        if regime.get("temperature") is not None:
            evidence.append(f"市场温度 {regime['temperature']:.1f}，状态 {regime.get('state')}")
        invalidation = [
            "相对510300的5日强度转负",
            "行业扩散跌破45且继续收缩",
            "交易所ETF份额明显转为净减少",
        ]
        if risk_score >= 50:
            invalidation.insert(0, "拥挤或脆弱度继续上升")

        risk_state = "low" if risk_score < 38 else (
            "watch" if risk_score < 55 else "high" if risk_score < 70 else "danger")
        quality_state = "high" if quality_score >= 82 else (
            "medium" if quality_score >= 65 else "low")
        results.append({
            "industry": primary["industry"],
            "etf": etf,
            "code": code,
            "name": etf.get("name") or share_row.get("name") or code,
            "opportunity_score": round(opportunity_score, 1),
            "score": round(opportunity_score, 1),
            "stage": stage,
            "signals": {
                "regime": round(regime_score, 1),
                "demand": round(demand, 1),
                "diffusion": round(diffusion, 1),
                "relative_strength": round(relative_strength, 1),
                "tradability": round(tradability, 1),
            },
            "risk": {
                "score": round(risk_score, 1),
                "state": risk_state,
                "reasons": risk_reasons[:5],
            },
            "prediction": {
                "probability": None, "status": "insufficient",
                "reason": "等待 V3 样本外概率校准",
            },
            "data_quality": {
                "score": round(quality_score, 1),
                "state": quality_state,
                "reasons": quality_reasons,
            },
            "carrier_match": {
                "score": round(carrier_score, 1),
                "primary_level": primary["match_level"],
                "primary_weight": primary["match_weight"],
                "related_count": len(related),
            },
            "confidence": round(quality_score, 1),
            "confidence_label": {"high": "高", "medium": "中", "low": "低"}[quality_state],
            "directional_participation_proxy": round(directional_proxy, 1),
            "share_change_pct": (
                round(float(share_change_pct), 3) if share_change_pct is not None else None),
            "relative_strength": relative_details,
            "evidence": evidence,
            "invalidation": invalidation,
            "related_industries": sorted(
                related,
                key=lambda item: (
                    -item["match_weight"], -item["diffusion"], item["industry"]),
            ),
            "liquid": bool((etf.get("avg_amount_20d") or 0) >= MIN_AVG_AMOUNT),
        })
    return sorted(results, key=lambda item: (-item["score"], item["code"]))


def _select_etf_candidates(rows: list[dict]) -> list[dict]:
    """选择性预测：宁可空仓，也不为凑榜单输出低质量或高风险标的。"""
    eligible = [
        row for row in rows
        if row.get("stage") in ("emerging", "confirmed")
        and float(row.get("score") or 0) >= V3_MIN_SCORE
        and float((row.get("data_quality") or {}).get("score") or 0) >= V3_MIN_DATA_QUALITY
        and float((row.get("risk") or {}).get("score") or 100) < 58
        and row.get("liquid")
    ]
    eligible.sort(key=lambda row: (
        0 if row.get("stage") == "confirmed" else 1,
        -float(row.get("score") or 0),
        float((row.get("risk") or {}).get("score") or 100),
    ))
    selected, seen = [], set()
    for row in eligible:
        code = str((row.get("etf") or {}).get("code") or row.get("code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        item = dict(row)
        item["rank"] = len(selected) + 1
        selected.append(item)
        if len(selected) >= V3_TOP_N:
            break
    return selected


def _write_prediction_log(result: dict) -> None:
    """JSONL 按 date+model_version 幂等覆盖，避免重复计入后续校准样本。"""
    path = data_path(PREDICTION_LOG_FILE)
    key = (result.get("date"), result.get("model_version"))
    record = {
        "date": result.get("date"),
        "model_version": result.get("model_version"),
        "generated_at": result.get("updated_at"),
        "decision_status": result.get("decision_status"),
        "regime": result.get("regime"),
        "picks": [{
            "rank": row.get("rank"),
            "code": (row.get("etf") or {}).get("code"),
            "name": (row.get("etf") or {}).get("name"),
            "score": row.get("score"),
            "stage": row.get("stage"),
            "probability": (row.get("prediction") or {}).get("probability"),
        } for row in result.get("top", [])],
    }
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if (item.get("date"), item.get("model_version")) != key:
                    records.append(item)
    except FileNotFoundError:
        pass
    records.append(record)
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for item in records:
                f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def build_recommendations(map_obj: dict | None = None, snapshot: dict | None = None,
                          ref_date: str | None = None, scheme: str = SCHEME) -> dict:
    map_obj = map_obj or ensure_etf_map(scheme)
    if snapshot is None:
        with open(data_path(SNAPSHOT_FILE), encoding="utf-8") as f:
            snapshot = json.load(f)
    snap_etfs = snapshot.get("etfs", {})

    db = get_db()
    if ref_date:
        dates = [r[0] for r in db.conn.execute(
            "SELECT DISTINCT date FROM daily_new_highs WHERE scheme=? AND period='month' "
            "AND date<=? ORDER BY date DESC LIMIT ?", [scheme, ref_date, BASE_DAYS]).fetchall()]
    else:
        dates = [r[0] for r in db.conn.execute(
            "SELECT DISTINCT date FROM daily_new_highs WHERE scheme=? AND period='month' "
            "ORDER BY date DESC LIMIT ?", [scheme, BASE_DAYS]).fetchall()]
    if len(dates) < RECENT_DAYS + 5:
        raise RuntimeError(f"行业数据不足 ({len(dates)} 天)")

    latest = dates[0]
    mapping = map_obj.get("mapping", {})
    rows = _score_rows(db, dates, mapping, snap_etfs, scheme)
    regime = _build_market_regime(
        _load_optional_json("market_temperature.json"), latest)
    crowding_payload = _load_optional_json(
        "crowding.json" if scheme == "sw" else f"crowding_{scheme}.json")
    crowding = _crowding_index(crowding_payload, latest)
    external_payload = _load_optional_json("crowding_external.json")
    external = _external_etf_index(external_payload, latest)
    benchmark = snap_etfs.get(BENCHMARK_CODE)
    etf_rows = _aggregate_etf_candidates(
        rows,
        crowding=crowding,
        external=external,
        regime=regime,
        benchmark=benchmark,
    )

    calibration_summary, calibration = _load_v3_calibration()
    for etf_row in etf_rows:
        etf_row["prediction"] = _prediction_for(etf_row["score"], calibration)
    top = _select_etf_candidates(etf_rows)

    rows.sort(key=lambda r: -r["score"])
    match_levels = Counter(
        relation.get("match_level", "unknown")
        for row in etf_rows for relation in row.get("related_industries", [])
    )
    public_industries = []
    for row in rows:
        public_row = {
            key: value for key, value in row.items() if not key.startswith("_")
        }
        public_row["signals"] = {
            key: value for key, value in (row.get("signals") or {}).items()
            if key not in ("capital", "flow")
        }
        public_industries.append(public_row)
    quality_scores = [
        float((row.get("data_quality") or {}).get("score") or 0)
        for row in etf_rows
    ]
    root_quality = round(_mean(quality_scores), 1) if quality_scores else 0.0
    root_quality_state = (
        "high" if root_quality >= 82 else "medium" if root_quality >= 65 else "low"
    )
    if top:
        decision_status = "actionable"
        actionable_message = f"筛出 {len(top)} 只通过信号与风险门槛的 ETF 候选。"
        if calibration_summary.get("status") != "calibrated":
            actionable_message += "概率尚未完成样本外校准，只能作为观察候选，不能解读为胜率。"
    elif not etf_rows or max(quality_scores or [0]) < 45:
        decision_status = "insufficient_data"
        actionable_message = "当前数据不足，系统不输出热点候选。"
    else:
        decision_status = "no_high_quality_signal"
        actionable_message = "今天没有同时通过最低分、数据质量和风险门槛的 ETF，建议等待。"

    result = {
        "model_version": V3_MODEL_VERSION,
        "score_label": "ETF热点机会分",
        "scheme": scheme,
        "date": latest,
        "etf_date": Counter(
            str(item.get("last_date", "")).replace("-", "") for item in snap_etfs.values()
            if item.get("last_date")
        ).most_common(1)[0][0] if snap_etfs else None,
        "date_label": f"{int(latest[4:6])}月{int(latest[6:8])}日",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "decision_status": decision_status,
        "actionable_message": actionable_message,
        "regime": regime,
        "calibration": calibration_summary,
        "data_quality": {
            "score": root_quality,
            "state": root_quality_state,
            "etfs": len(etf_rows),
            "with_exchange_share_change": sum(
                row.get("share_change_pct") is not None for row in etf_rows),
            "with_benchmark": bool(benchmark),
        },
        "weights": {
            "regime": 0.15,
            "demand": 0.25,
            "diffusion": 0.25,
            "relative_strength": 0.25,
            "tradability": 0.10,
        },
        "params": {"recent_days": RECENT_DAYS, "base_days": BASE_DAYS,
                   "min_avg_amount": MIN_AVG_AMOUNT, "chase_risk_ret5": CHASE_RISK_RET5,
                   "benchmark": BENCHMARK_CODE, "top_max": V3_TOP_N,
                   "min_score": V3_MIN_SCORE,
                   "min_data_quality": V3_MIN_DATA_QUALITY,
                   "small_sample_shrinkage": "n/(n+10)",
                   "match_weights": MATCH_WEIGHTS},
        "field_semantics": {
            "directional_participation_proxy": (
                "上涨股票成交额减下跌股票成交额形成的方向参与代理，不代表真实资金流入"
            ),
            "share_change_pct": "交易所公布的ETF份额变化，作为真实申赎需求证据",
            "confidence": "兼容字段，语义为数据质量，不是预测概率",
        },
        "mapping_summary": {
            "matched": sum(1 for row in rows if row.get("has_etf")),
            "unmatched": sum(1 for row in rows if not row.get("has_etf")),
            "levels": dict(match_levels),
        },
        "top": top,
        "etfs": etf_rows,
        "industries": public_industries,
    }
    if ref_date is None:
        _atomic_json_dump(result, data_path(_output_file(scheme)))
        if scheme == SCHEME:
            _write_prediction_log(result)
    return result


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

def update_etf_recommend(schemes: tuple = ("sw3", "ths")) -> dict:
    maps = {scheme: ensure_etf_map(scheme) for scheme in schemes}
    codes = sorted(
        {c["code"] for map_obj in maps.values()
         for cands in map_obj.get("mapping", {}).values() for c in cands}
        | {BENCHMARK_CODE}
    )
    print(f"[etf] {len(codes)} 只候选 ETF ({'/'.join(schemes)})")
    db = get_db()
    latest_row = db.conn.execute(
        "SELECT MAX(date) FROM daily_new_highs WHERE scheme=? AND period='month'", [SCHEME]
    ).fetchone()
    ref_date = latest_row[0] if latest_row else None
    snapshot = fetch_etf_snapshot(codes, ref_date=ref_date)
    result = None
    for scheme in schemes:
        result = build_recommendations(maps[scheme], snapshot, scheme=scheme)
        print(f"[etf:{scheme}] 选择性候选 {len(result['top'])}/{V3_TOP_N}:")
        for r in result["top"]:
            print(f"  {r['rank']}. {r['industry']} → {r['etf']['name']}({r['etf']['code']}) "
                  f"机会分 {r['score']}  阶段 {r['stage']}  风险 {r['risk']['score']}")
    return result


if __name__ == "__main__":
    update_etf_recommend()
    print(f"\n✅ 已生成 {OUTPUT_FILE}")
