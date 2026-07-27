"""ETF 热点候选的严格样本外回测。

信号只能使用推荐日 D 收盘时已知的数据，统一在下一交易日（T+1）开盘
成交。ETF 收益扣除默认 20bps 往返成本，并以沪深 300 ETF（510300）
同期收益为基准。热点标签同时要求净收益为正、净超额为正且处于当日
横截面前 20%。

14:50 策略需要分钟级行情才能无穿越复现。日线回测明确禁用该模块，但
保留 ``intraday`` JSON 键，兼容既有 server/UI。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import requests

import etf_recommend
import momentum_etf
from db import get_db
from etf_recommend import SCHEME, _etf_stats, _pick_top, _score_rows, ensure_etf_map
from momentum_etf import WEAK_INDEXES, _etf_symbol, load_pool_config
from runtime_paths import data_path

OUTPUT_FILE = "etf_backtest.json"
BACKTEST_DATES = 120         # 足够覆盖滚动验证；数据不足时自然缩短
FORWARD_DAYS = 10            # 后续观察窗
KLINE_DATALEN = 320          # 覆盖评分、回测日、观察窗和非同步交易余量
BENCHMARK_CODE = "510300"
ROUND_TRIP_COST_BPS = 20.0
EVALUATION_K = 3
CALIBRATION_MIN_DATES = 60
CALIBRATION_MIN_SAMPLES = 500
INTRADAY_DISABLED_REASON = "日线数据无法无穿越复现 14:50 成交价与盘中触发顺序"
SINA_KLINE_URL = momentum_etf.SINA_KLINE_URL


# ------------------------------------------------------------------
# K线（含OHLC，用于入场价和最大涨/回撤）
# ------------------------------------------------------------------

def _adjust_ohlc_corporate_actions(rows: list[dict]) -> list[dict]:
    """把 ETF 份额折算前后的 OHLC/成交量统一到最新份额口径。

    份额拆分会让价格按比例跳低、持有份额同比例增加；直接使用原始 OHLC
    会制造跨折算日 -60%~-80% 的虚假亏损。这里只修正明显超出 ETF 正常
    单日波动范围的非交易性跳点。
    """
    adjusted = [dict(row) for row in rows]
    price_fields = ("open", "high", "low", "close")
    for index in range(1, len(adjusted)):
        previous_close = float(adjusted[index - 1].get("close") or 0)
        current_close = float(adjusted[index].get("close") or 0)
        if previous_close <= 0 or current_close <= 0:
            continue
        ratio = current_close / previous_close
        if 0.70 <= ratio <= 1.43:
            continue
        for prior in adjusted[:index]:
            for field in price_fields:
                if prior.get(field) is not None:
                    prior[field] = float(prior[field]) * ratio
            if prior.get("volume") is not None:
                prior["volume"] = float(prior["volume"]) / ratio
    return adjusted


def _fetch_kline_ohlc(code: str, datalen: int = KLINE_DATALEN):
    """(code, rows|None)，rows 按时间升序，含 open/high/low/close/volume（前复权）"""
    symbol = _etf_symbol(code)
    rows = momentum_etf._tencent_kline(symbol, datalen)
    if rows and len(rows) >= 30:
        return code, _adjust_ohlc_corporate_actions([
            {"date": r["date"].replace("-", ""), "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"], "volume": r["volume"]}
            for r in rows
        ])
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
                return code, None
            rows = [{"date": d.get("day", "").replace("-", ""),
                     "open": float(d["open"]), "high": float(d["high"]),
                     "low": float(d["low"]), "close": float(d["close"]),
                     "volume": float(d["volume"])} for d in data]
            return code, _adjust_ohlc_corporate_actions(rows)
        except Exception:
            if attempt:
                return code, None
            time.sleep(0.5)
    return code, None


def _etf_stats_at(rows: list[dict]) -> dict | None:
    """由 ≤D 的K线切片构造当日 ETF 统计（对齐 etf_recommend 快照字段）"""
    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]
    return _etf_stats(closes, volumes, rows[-1]["date"] if rows else "")


def _forward_perf(
    rows: list[dict],
    d_date: str,
    benchmark_rows: list[dict] | None = None,
    round_trip_cost_bps: float = ROUND_TRIP_COST_BPS,
) -> dict:
    """严格用市场日历的 T+1 开盘成交，计算净收益、基准和净超额。

    ``benchmark_rows`` 同时充当交易日历。ETF 在 T+1 无行情（例如停牌）
    时不把之后的首次成交错当成 T+1，直接视为不可评估。
    """
    horizon_map = {1: "t1", 3: "t3", 5: "t5", 10: "t10"}

    def empty_result(entry_date=None):
        result = {"entry": None, "entry_date": entry_date, "fwd_days": 0}
        for suffix in horizon_map.values():
            result.update({
                f"gross_ret_{suffix}": None,
                f"net_ret_{suffix}": None,
                f"benchmark_ret_{suffix}": None,
                f"excess_ret_{suffix}": None,
                f"ret_{suffix}": None,
                f"exit_date_{suffix}": None,
            })
        return result

    calendar_rows = benchmark_rows or rows
    cal_idx = next((i for i, r in enumerate(calendar_rows) if r["date"] == d_date), None)
    if cal_idx is None or cal_idx + 1 >= len(calendar_rows):
        return empty_result()
    entry_date = calendar_rows[cal_idx + 1]["date"]
    row_by_date = {r["date"]: r for r in rows}
    entry_row = row_by_date.get(entry_date)
    entry = float((entry_row or {}).get("open") or 0)
    if entry <= 0:
        return empty_result(entry_date)

    benchmark_entry = float(calendar_rows[cal_idx + 1].get("open") or 0)
    cost_pct = float(round_trip_cost_bps) / 100.0
    result = {
        "entry": round(entry, 4),
        "entry_date": entry_date,
        "round_trip_cost_bps": float(round_trip_cost_bps),
        "fwd_days": 0,
    }

    for suffix in horizon_map.values():
        result.update({
            f"gross_ret_{suffix}": None,
            f"net_ret_{suffix}": None,
            f"benchmark_ret_{suffix}": None,
            f"excess_ret_{suffix}": None,
            f"ret_{suffix}": None,
            f"exit_date_{suffix}": None,
        })
    for day, suffix in horizon_map.items():
        exit_i = cal_idx + day
        if exit_i >= len(calendar_rows):
            continue
        exit_date = calendar_rows[exit_i]["date"]
        exit_row = row_by_date.get(exit_date)
        if not exit_row or float(exit_row.get("close") or 0) <= 0:
            continue
        gross = (float(exit_row["close"]) / entry - 1) * 100
        net = gross - cost_pct
        benchmark_ret = None
        if benchmark_rows is not None and benchmark_entry > 0:
            benchmark_ret = (
                float(calendar_rows[exit_i]["close"]) / benchmark_entry - 1
            ) * 100
        excess = net - benchmark_ret if benchmark_ret is not None else None
        result.update({
            f"gross_ret_{suffix}": round(gross, 4),
            f"net_ret_{suffix}": round(net, 4),
            f"benchmark_ret_{suffix}": round(benchmark_ret, 4)
            if benchmark_ret is not None else None,
            f"excess_ret_{suffix}": round(excess, 4) if excess is not None else None,
            # 旧页面沿用 ret_t*；v3 明确其含义为扣成本后的净收益。
            f"ret_{suffix}": round(net, 4),
            f"exit_date_{suffix}": exit_date,
        })
    fwd_calendar = calendar_rows[cal_idx + 1:cal_idx + 1 + FORWARD_DAYS]
    path_rows = [row_by_date[r["date"]] for r in fwd_calendar if r["date"] in row_by_date]
    result["fwd_days"] = len(path_rows)
    if path_rows:
        last = path_rows[-1]
        result["ret_now"] = round(
            (float(last["close"]) / entry - 1) * 100 - cost_pct, 4
        )
        result["max_up"] = round(
            (max(float(r["high"]) for r in path_rows) / entry - 1) * 100, 4
        )
        result["max_dd"] = round(
            (min(float(r["low"]) for r in path_rows) / entry - 1) * 100, 4
        )
    return result


def _intraday_proxy_trade(rows: list[dict], d_date: str) -> dict:
    """兼容旧调用；日线无法复现 14:50，永远不生成伪交易收益。"""
    return {
        "status": "disabled",
        "enabled": False,
        "return_pct": None,
        "reason": INTRADAY_DISABLED_REASON,
    }


def _intraday_summary(trades: list[dict]) -> dict:
    return {
        "status": "disabled",
        "reason": INTRADAY_DISABLED_REASON,
        "sessions": len(trades),
        "valid_sessions": 0,
        "win_rate": None,
        "avg_return": None,
        "total_return": None,
        "max_drawdown": None,
        "take_profit_legs": 0,
        "stop_loss_legs": 0,
    }


# ------------------------------------------------------------------
# 走弱期状态 walk-forward 重建（与 momentum_etf.check_weak_period 同规则）
# ------------------------------------------------------------------

def _weak_series(params: dict) -> dict[str, bool]:
    lookback = params.get("weak_ma_lookback", 10)
    max_days = params.get("max_weak_days", 20)
    idx_data = {}
    for ix in WEAK_INDEXES:
        rows = momentum_etf._sina_kline(ix["code"], KLINE_DATALEN)
        if rows:
            idx_data[ix["code"]] = {r["date"].replace("-", ""): r["close"] for r in rows}
    if not idx_data:
        return {}
    calendar = sorted(idx_data[WEAK_INDEXES[0]["code"]])

    series, is_weak, weak_start_i, weak_days = {}, False, None, 0
    for i, d in enumerate(calendar):
        above = below = 0
        for ix in WEAK_INDEXES:
            closes_map = idx_data.get(ix["code"])
            if not closes_map:
                continue
            hist = [closes_map[x] for x in calendar[:i + 1] if x in closes_map]
            if len(hist) < lookback:
                continue
            if hist[-1] > float(np.mean(hist[-lookback:])):
                above += 1
            else:
                below += 1
        if is_weak:
            weak_days = i - weak_start_i
            if weak_days >= max_days or above >= 3:
                is_weak, weak_start_i, weak_days = False, None, 0
        else:
            if below >= 3:
                is_weak, weak_start_i, weak_days = True, i, 0
        series[d] = is_weak
    return series


# ------------------------------------------------------------------
# 横截面标签、按预测日聚类评估与时间外概率校准
# ------------------------------------------------------------------

def _average_ranks(values: list[float]) -> list[float]:
    """返回从小到大的平均秩（1..n），并正确处理并列值。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        for pos in range(start, end):
            ranks[order[pos]] = average_rank
        start = end
    return ranks


def _annotate_forward_outcomes(picks: list[dict]) -> list[dict]:
    """按预测日计算未来收益横截面分位数和热点事件标签。"""
    by_date: dict[str, list[dict]] = {}
    for pick in picks:
        by_date.setdefault(pick["date"], []).append(pick)
    for rows in by_date.values():
        for day in (5, 10):
            suffix = f"t{day}"
            eligible = [
                row for row in rows
                if row.get(f"net_ret_{suffix}") is not None
                and row.get(f"excess_ret_{suffix}") is not None
            ]
            if not eligible:
                continue
            ranks = _average_ranks([float(row[f"net_ret_{suffix}"]) for row in eligible])
            for row, rank in zip(eligible, ranks):
                percentile = (
                    100.0 if len(eligible) == 1
                    else round((rank - 1) / (len(eligible) - 1) * 100, 2)
                )
                net = float(row[f"net_ret_{suffix}"])
                excess = float(row[f"excess_ret_{suffix}"])
                hit = net > 0 and excess > 0 and percentile >= 80.0
                row[f"forward_percentile_{suffix}"] = percentile
                row[f"hotspot_hit_{suffix}"] = hit
                row.setdefault("outcomes", {})[suffix] = {
                    "net_return_pct": net,
                    "benchmark_return_pct": row.get(f"benchmark_ret_{suffix}"),
                    "excess_return_pct": excess,
                    "forward_percentile": percentile,
                    "hotspot_hit": hit,
                }
    return picks


def _join_universe_outcomes(
    picks: list[dict], universe_rows: list[dict]
) -> list[dict]:
    """把统一 ETF/date 横截面标签连接回各模型，确保模型与基线可比。"""
    lookup = {(row["date"], row["code"]): row for row in universe_rows}
    identity_keys = {"date", "code", "name"}
    for pick in picks:
        outcome = lookup.get((pick["date"], pick["code"]))
        if not outcome:
            continue
        for key, value in outcome.items():
            if key not in identity_keys:
                pick[key] = value
    return picks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    xr = np.asarray(_average_ranks(xs), dtype=float)
    yr = np.asarray(_average_ranks(ys), dtype=float)
    if float(np.std(xr)) == 0 or float(np.std(yr)) == 0:
        return None
    return float(np.corrcoef(xr, yr)[0, 1])


def _clustered_evaluation(
    picks: list[dict],
    day: int,
    k: int = EVALUATION_K,
    expected_dates: list[str] | None = None,
) -> dict:
    """每个预测日先等权聚合，再跨日期统计，避免把重叠行当独立命中。"""
    suffix = f"t{day}"
    all_dates = sorted(set(expected_dates or [p["date"] for p in picks]))
    grouped: dict[str, list[dict]] = {}
    for pick in picks:
        if (
            pick.get(f"net_ret_{suffix}") is not None
            and pick.get(f"excess_ret_{suffix}") is not None
            and pick.get(f"hotspot_hit_{suffix}") is not None
        ):
            grouped.setdefault(pick["date"], []).append(pick)

    daily_precision, daily_net, daily_excess, daily_ic = [], [], [], []
    eligible_samples = 0
    for rows in grouped.values():
        eligible_samples += len(rows)
        ordered = sorted(
            rows,
            key=lambda row: (
                float(row.get("score") or float("-inf")),
                -int(row.get("rank") or 9999),
            ),
            reverse=True,
        )
        selected = ordered[:k]
        if not selected:
            continue
        daily_precision.append(
            sum(bool(row[f"hotspot_hit_{suffix}"]) for row in selected) / len(selected)
        )
        daily_net.append(float(np.mean([row[f"net_ret_{suffix}"] for row in selected])))
        daily_excess.append(
            float(np.mean([row[f"excess_ret_{suffix}"] for row in selected]))
        )
        ic_rows = [
            row for row in rows
            if row.get("score") is not None and row.get(f"excess_ret_{suffix}") is not None
        ]
        ic = _spearman(
            [float(row["score"]) for row in ic_rows],
            [float(row[f"excess_ret_{suffix}"]) for row in ic_rows],
        )
        if ic is not None:
            daily_ic.append(ic)

    eligible_dates = len(grouped)
    total_dates = len(all_dates)
    return {
        "horizon": suffix,
        "k": k,
        "prediction_dates": total_dates,
        "eligible_dates": eligible_dates,
        "eligible_samples": eligible_samples,
        "precision_at_k": round(float(np.mean(daily_precision)) * 100, 2)
        if daily_precision else None,
        "avg_net_return": round(float(np.mean(daily_net)), 4) if daily_net else None,
        "avg_excess_return": round(float(np.mean(daily_excess)), 4)
        if daily_excess else None,
        "rank_ic": round(float(np.mean(daily_ic)), 4) if daily_ic else None,
        "rank_ic_dates": len(daily_ic),
        "coverage": round(eligible_dates / total_dates * 100, 2) if total_dates else 0.0,
        "aggregation": "先按预测日等权TopK，再跨日期平均",
    }


def _pav_isotonic(scores: list[float], labels: list[int]) -> tuple[list[float], list[float]]:
    """对训练窗分箱后执行单调递增 PAV；不依赖 sklearn。"""
    pairs = sorted(zip(scores, labels), key=lambda pair: pair[0])
    if not pairs:
        return [], []
    bin_count = min(10, max(3, len(pairs) // 50))
    bins = []
    for bin_i in range(bin_count):
        lo = bin_i * len(pairs) // bin_count
        hi = (bin_i + 1) * len(pairs) // bin_count
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        item = {
            "max_score": float(chunk[-1][0]),
            "weight": len(chunk),
            "hits": sum(int(label) for _, label in chunk),
        }
        # 不在同一个分数值中间人为切断概率层级。
        if bins and bins[-1]["max_score"] == item["max_score"]:
            bins[-1]["weight"] += item["weight"]
            bins[-1]["hits"] += item["hits"]
        else:
            bins.append(item)

    blocks = []
    for item in bins:
        blocks.append(dict(item))
        while (
            len(blocks) >= 2
            and blocks[-2]["hits"] / blocks[-2]["weight"]
            > blocks[-1]["hits"] / blocks[-1]["weight"]
        ):
            right = blocks.pop()
            left = blocks.pop()
            blocks.append({
                "max_score": right["max_score"],
                "weight": left["weight"] + right["weight"],
                "hits": left["hits"] + right["hits"],
            })
    knots = [round(float(block["max_score"]), 6) for block in blocks]
    probabilities = [
        round(float(block["hits"] / block["weight"]), 6) for block in blocks
    ]
    return knots, probabilities


def _isotonic_predict(score: float, knots: list[float], probabilities: list[float]) -> float:
    for knot, probability in zip(knots, probabilities):
        if score <= knot:
            return probability
    return probabilities[-1]


def _calibrate_scores(
    picks: list[dict],
    day: int = 5,
    min_dates: int = CALIBRATION_MIN_DATES,
    min_samples: int = CALIBRATION_MIN_SAMPLES,
) -> dict:
    """前段日期训练、后段日期验证；验证期绝不参与分箱或 PAV 拟合。"""
    suffix = f"t{day}"
    rows = [
        pick for pick in picks
        if pick.get("score") is not None and pick.get(f"hotspot_hit_{suffix}") is not None
    ]
    dates = sorted({row["date"] for row in rows})
    base = {
        "status": "insufficient",
        "method": "temporal_train_bins+pav_isotonic",
        "horizon": suffix,
        "min_dates": min_dates,
        "min_samples": min_samples,
        "independent_dates": len(dates),
        "sample_size": len(rows),
        "base_rate": round(
            sum(bool(row[f"hotspot_hit_{suffix}"]) for row in rows) / len(rows), 6
        ) if rows else None,
        "score_knots": [],
        "probabilities": [],
        "model": None,
        "validation": {"brier": None, "reliability": []},
    }
    if len(dates) < min_dates or len(rows) < min_samples:
        return base

    split_i = max(1, min(len(dates) - 1, int(len(dates) * 0.70)))
    validation_dates = set(dates[split_i:])
    # 避免训练期 T+10 标签与验证起点重叠。
    embargo = max(day, 10)
    train_dates = set(dates[:max(0, split_i - embargo)])
    train = [row for row in rows if row["date"] in train_dates]
    validation = [row for row in rows if row["date"] in validation_dates]
    if not train or not validation:
        return base

    knots, probabilities = _pav_isotonic(
        [float(row["score"]) for row in train],
        [int(bool(row[f"hotspot_hit_{suffix}"])) for row in train],
    )
    if not knots:
        return base
    predictions = [
        _isotonic_predict(float(row["score"]), knots, probabilities)
        for row in validation
    ]
    labels = [int(bool(row[f"hotspot_hit_{suffix}"])) for row in validation]
    brier = float(np.mean([(p - y) ** 2 for p, y in zip(predictions, labels)]))
    reliability = []
    for probability in sorted(set(predictions)):
        idxs = [i for i, value in enumerate(predictions) if value == probability]
        reliability.append({
            "predicted": round(probability, 6),
            "actual": round(float(np.mean([labels[i] for i in idxs])), 6),
            "n": len(idxs),
        })
    model = {"score_knots": knots, "probabilities": probabilities}
    return {
        **base,
        "status": "ready",
        "base_rate": round(float(np.mean(
            [int(bool(row[f"hotspot_hit_{suffix}"])) for row in train]
        )), 6),
        "score_knots": knots,
        "probabilities": probabilities,
        "model": model,
        "training": {
            "first_date": min(train_dates),
            "last_date": max(train_dates),
            "dates": len(train_dates),
            "sample_size": len(train),
            "embargo_dates": embargo,
        },
        "validation": {
            "first_date": min(validation_dates),
            "last_date": max(validation_dates),
            "dates": len(validation_dates),
            "sample_size": len(validation),
            "brier": round(brier, 6),
            "reliability": reliability,
        },
    }


def _industry_candidates(
    db, window_dates: list[str], mapping: dict, snap_at_d: dict,
    benchmark: dict | None = None,
    regime: dict | None = None,
) -> list[dict]:
    """优先走 v3 ETF 聚合接口；后端尚未升级时兼容旧行业选取。"""
    industry_rows = _score_rows(db, window_dates, mapping, snap_at_d)
    aggregate = getattr(etf_recommend, "_aggregate_etf_candidates", None)
    if callable(aggregate):
        try:
            candidate_rows = aggregate(
                industry_rows,
                crowding=None,
                external=None,
                regime=regime,
                benchmark=benchmark,
            )
        except TypeError:
            candidate_rows = aggregate(industry_rows)
        selector = getattr(etf_recommend, "_select_etf_candidates", None)
        if callable(selector):
            try:
                return list(selector(candidate_rows))
            except TypeError:
                return list(selector(candidate_rows, limit=10))
        return list(candidate_rows)[:10]
    return _pick_top(industry_rows)


def _normalise_candidate(row: dict, fallback_rank: int) -> dict | None:
    etf = row.get("etf") or {}
    code = str(etf.get("code") or row.get("code") or "")
    if not code:
        return None
    industries = row.get("related_industries") or row.get("industries") or []
    industry_labels = [
        str(item.get("name") or item.get("industry") or item)
        if isinstance(item, dict) else str(item)
        for item in industries[:3]
    ]
    label = (
        row.get("label") or row.get("industry") or row.get("name")
        or ("、".join(industry_labels) if industry_labels else code)
    )
    score = row.get("opportunity_score")
    if score is None:
        score = row.get("score")
    return {
        "rank": int(row.get("rank") or fallback_rank),
        "label": label,
        "code": code,
        "name": etf.get("name") or row.get("name") or code,
        "score": score,
        "opportunity_score": row.get("opportunity_score"),
        "heat_score": row.get("heat_score"),
        "stage": row.get("stage"),
        "penalty": row.get("penalty"),
        "tags": row.get("tags", []),
        "industries": industries,
        "aggregation": row.get("aggregation"),
        "signals": row.get("signals"),
        "risk": row.get("risk"),
        "data_quality": row.get("data_quality"),
        "carrier_match": row.get("carrier_match"),
        "confidence": row.get("confidence"),
        "confidence_label": row.get("confidence_label"),
        "evidence": row.get("evidence", []),
        "invalidation": row.get("invalidation", []),
    }


def _relative_strength_5d(
    code: str, rows: list[dict], benchmark_rows: list[dict], d_date: str
) -> float | None:
    idx = next((i for i, row in enumerate(benchmark_rows) if row["date"] == d_date), None)
    if idx is None or idx < 5:
        return None
    start_date = benchmark_rows[idx - 5]["date"]
    by_date = {row["date"]: row for row in rows}
    if start_date not in by_date or d_date not in by_date:
        return None
    start = float(by_date[start_date].get("close") or 0)
    end = float(by_date[d_date].get("close") or 0)
    benchmark_start = float(benchmark_rows[idx - 5].get("close") or 0)
    benchmark_end = float(benchmark_rows[idx].get("close") or 0)
    if min(start, end, benchmark_start, benchmark_end) <= 0:
        return None
    return ((end / start - 1) - (benchmark_end / benchmark_start - 1)) * 100


# ------------------------------------------------------------------
# 汇总统计
# ------------------------------------------------------------------

def _summary(picks: list[dict]) -> dict:
    def horizon(key):
        grouped: dict[str, list[float]] = {}
        for pick in picks:
            value = pick.get(key)
            if value is None and key.startswith("ret_t"):
                value = pick.get(f"net_{key}")
            if value is not None:
                grouped.setdefault(pick["date"], []).append(float(value))
        daily = [float(np.mean(values)) for values in grouped.values()]
        if not daily:
            return {"n": 0, "samples": 0, "mean": None, "win": None}
        return {
            "n": len(daily),
            "samples": sum(len(values) for values in grouped.values()),
            "mean": round(float(np.mean(daily)), 2),
            "win": round(sum(1 for value in daily if value > 0) / len(daily) * 100, 1),
            "aggregation": "prediction_date",
        }

    def avg(key):
        vals = [p[key] for p in picks if p.get(key) is not None]
        return round(float(np.mean(vals)), 2) if vals else None

    with_ret = [p for p in picks if p.get("ret_t5") is not None]
    best = max(with_ret, key=lambda p: p["ret_t5"], default=None)
    worst = min(with_ret, key=lambda p: p["ret_t5"], default=None)
    shrink = lambda p: p and {"label": p["label"], "code": p["code"],
                              "date": p["date"], "ret_t5": p["ret_t5"]}
    return {
        "total": len(picks),
        "t1": horizon("ret_t1"), "t3": horizon("ret_t3"),
        "t5": horizon("ret_t5"), "t10": horizon("ret_t10"),
        "now": horizon("ret_now"),
        "avg_max_up": avg("max_up"), "avg_max_dd": avg("max_dd"),
        "best": shrink(best), "worst": shrink(worst),
    }


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def run_backtest() -> dict:
    db = get_db()
    rec_dates = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT date FROM daily_new_highs WHERE scheme=? AND period='month' "
        "ORDER BY date DESC LIMIT ?", [SCHEME, BACKTEST_DATES]).fetchall()]
    if len(rec_dates) < 3:
        raise RuntimeError(f"可回测日期不足 ({len(rec_dates)} 天)")
    earliest = rec_dates[-1]

    map_obj = ensure_etf_map()
    mapping = map_obj.get("mapping", {})
    pool_cfg = load_pool_config()
    params = pool_cfg["params"]
    min_amount = params.get("min_avg_amount", 5e7)

    # 全部需要的 ETF 代码（行业候选 ∪ 动量池 ∪ 基准）
    industry_codes = {c["code"] for cands in mapping.values() for c in cands}
    momentum_names = {e["code"]: e.get("name", "")
                      for e in list(pool_cfg["global_pool"]) + list(pool_cfg["china_pool"])}
    code_names = dict(momentum_names)
    for candidates in mapping.values():
        for candidate in candidates:
            code_names.setdefault(candidate["code"], candidate.get("name", candidate["code"]))
    code_names[BENCHMARK_CODE] = code_names.get(BENCHMARK_CODE) or "沪深300ETF"
    all_codes = sorted(industry_codes | set(momentum_names) | {BENCHMARK_CODE})

    # 一次性拉全窗口K线（各回测日共用），另拉指数K线重建走弱期
    print(f"[backtest] 回测 {len(rec_dates)} 个推荐日 ({earliest} ~ {rec_dates[0]})，"
          f"{len(all_codes)} 只ETF K线...")
    klines, fails = {}, 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_kline_ohlc, c): c for c in all_codes}
        for f in as_completed(futures):
            code, rows = f.result()
            if rows:
                klines[code] = rows
            else:
                fails += 1
    print(f"[backtest] K线: 成功 {len(klines)}/{len(all_codes)}" + (f", 失败 {fails}" if fails else ""))
    benchmark_rows = klines.get(BENCHMARK_CODE)
    if not benchmark_rows:
        raise RuntimeError(f"基准 {BENCHMARK_CODE} K线获取失败，不能计算严格超额收益")
    weak_map = _weak_series(params)
    try:
        with open(data_path("market_temperature.json"), encoding="utf-8") as handle:
            temperature_history = json.load(handle)
    except (OSError, ValueError, TypeError):
        temperature_history = {}
    regime_builder = getattr(etf_recommend, "_build_market_regime", None)

    industry_picks, momentum_picks = [], []
    baseline_picks, baseline_scored, outcome_universe = [], [], []
    for D in rec_dates:
        # ---- 统一结果宇宙：所有已配置且 T+1 可交易的 ETF ----
        daily_outcomes = {}
        for code in all_codes:
            if code == BENCHMARK_CODE or code not in klines:
                continue
            perf = _forward_perf(klines[code], D, benchmark_rows=benchmark_rows)
            if perf.get("entry") is None:
                continue
            outcome = {
                "date": D,
                "code": code,
                "name": code_names.get(code, code),
                **perf,
            }
            daily_outcomes[code] = outcome
            outcome_universe.append(outcome)

        # ---- 行业推荐当日重算 ----
        window_dates = [r[0] for r in db.conn.execute(
            "SELECT DISTINCT date FROM daily_new_highs WHERE scheme=? AND period='month' "
            "AND date<=? ORDER BY date DESC LIMIT 20", [SCHEME, D]).fetchall()]
        snap_at_d = {}
        for code in industry_codes | {BENCHMARK_CODE}:
            rows = klines.get(code)
            if not rows:
                continue
            sliced = [r for r in rows if r["date"] <= D]
            stats = _etf_stats_at(sliced) if sliced else None
            if stats:
                snap_at_d[code] = stats
        top = _industry_candidates(
            db,
            window_dates,
            mapping,
            snap_at_d,
            benchmark=snap_at_d.get(BENCHMARK_CODE),
            regime=(regime_builder(temperature_history, D)
                    if callable(regime_builder) else None),
        )
        for fallback_rank, row in enumerate(top, 1):
            candidate = _normalise_candidate(row, fallback_rank)
            if not candidate:
                continue
            perf = {
                key: value
                for key, value in daily_outcomes.get(candidate["code"], {}).items()
                if key not in {"date", "code", "name"}
            }
            if not perf:
                perf = _forward_perf(
                    klines.get(candidate["code"], []), D, benchmark_rows=benchmark_rows
                )
            industry_picks.append({"date": D, **candidate, **perf})

        # ---- 动量当日重算 ----
        is_weak = weak_map.get(D, False)
        pool_list = list(pool_cfg["global_pool"]) if is_weak else (
            list(pool_cfg["global_pool"]) + list(pool_cfg["china_pool"]))
        metrics = []
        for e in pool_list:
            rows = klines.get(e["code"])
            if not rows:
                continue
            sliced = [dict(r, date=r["date"][:4] + "-" + r["date"][4:6] + "-" + r["date"][6:])
                      for r in rows if r["date"] <= D]
            if len(sliced) < params["lookback_days"] + 1:
                continue
            m = momentum_etf._etf_metrics(e["code"], e.get("name", ""), sliced,
                                          params, is_weak, spot=None, elapsed=None)
            if m and m["avg_amount_20d"] >= min_amount:
                metrics.append(m)
        metrics.sort(key=lambda x: -x["score"])
        for i, m in enumerate([x for x in metrics if x["passed_all"]][:5], 1):
            perf = {
                key: value
                for key, value in daily_outcomes.get(m["code"], {}).items()
                if key not in {"date", "code", "name"}
            }
            if not perf:
                perf = _forward_perf(
                    klines.get(m["code"], []), D, benchmark_rows=benchmark_rows
                )
            momentum_picks.append({
                "date": D, "rank": i, "label": m["name"], "code": m["code"],
                "name": m["name"], "score": m["score"],
                "tags": (["走弱期"] if is_weak else []) + [
                    f"R²{m['r_squared']}", f"年化{round(m['annualized'] * 100)}%"],
                **perf,
            })

        # ---- 可解释的简单基线：截至 D 收盘的 5 日相对强弱 TopK ----
        rs_candidates = []
        for code in all_codes:
            if code == BENCHMARK_CODE or code not in klines:
                continue
            score = _relative_strength_5d(code, klines[code], benchmark_rows, D)
            if score is not None:
                rs_candidates.append((score, code))
        rs_candidates.sort(reverse=True)
        for rank, (score, code) in enumerate(rs_candidates, 1):
            perf = {
                key: value
                for key, value in daily_outcomes.get(code, {}).items()
                if key not in {"date", "code", "name"}
            }
            if not perf:
                perf = _forward_perf(klines[code], D, benchmark_rows=benchmark_rows)
            row = {
                "date": D,
                "rank": rank,
                "label": code_names.get(code, code),
                "code": code,
                "name": code_names.get(code, code),
                "score": round(score, 4),
                "signal": "5d_relative_strength",
                **perf,
            }
            baseline_scored.append(row)
            if rank <= EVALUATION_K:
                baseline_picks.append(dict(row))

    _annotate_forward_outcomes(outcome_universe)
    for picks in (industry_picks, momentum_picks, baseline_picks, baseline_scored):
        _join_universe_outcomes(picks, outcome_universe)

    industry_eval = {
        "t5": _clustered_evaluation(industry_picks, 5, expected_dates=rec_dates),
        "t10": _clustered_evaluation(industry_picks, 10, expected_dates=rec_dates),
    }
    momentum_eval = {
        "t5": _clustered_evaluation(momentum_picks, 5, expected_dates=rec_dates),
        "t10": _clustered_evaluation(momentum_picks, 10, expected_dates=rec_dates),
    }
    baseline_eval = {
        "t5": _clustered_evaluation(baseline_scored, 5, expected_dates=rec_dates),
        "t10": _clustered_evaluation(baseline_scored, 10, expected_dates=rec_dates),
    }
    calibration_t5 = _calibrate_scores(industry_picks, 5)
    calibration_t10 = _calibrate_scores(industry_picks, 10)

    result = {
        "model_version": "etf-hotspot-v3",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "signal_rule": "推荐日收盘后生成，仅使用截至当日收盘的数据",
        "entry_rule": "下一市场交易日（T+1）开盘价；停牌/无当日行情则不评估",
        "forward_days": FORWARD_DAYS,
        "benchmark": {
            "code": BENCHMARK_CODE,
            "name": code_names[BENCHMARK_CODE],
            "return_definition": "同期T+1开盘至目标交易日收盘，不扣交易成本",
        },
        "costs": {
            "round_trip_bps": ROUND_TRIP_COST_BPS,
            "application": "ETF毛收益减去20bps；净超额=ETF净收益-基准收益",
        },
        "hotspot_definition": "净绝对收益>0、净超额>0、当日横截面forward_percentile>=80",
        "dates": rec_dates,
        "sample_note": "统计先按预测日聚类；相邻日期的远期窗口仍会重叠，不应把明细行视作独立样本。",
        "feature_availability": {
            "point_in_time": ["行业宽度与方向参与代理", "ETF复权OHLCV", "市场温度", "510300基准"],
            "unavailable_historically": ["行业拥挤外部证据", "交易所ETF历史份额变化"],
            "note": (
                "历史回测仅启用能按当日还原的特征；缺少逐日归档的拥挤外部证据和ETF份额变化，"
                "按缺失处理而非用今天的数据回填。"
            ),
        },
        "outcome_universe": {
            "definition": "所有已配置且在严格T+1有开盘行情的ETF；模型与基线共用该横截面标签",
            "code_count": len({row["code"] for row in outcome_universe}),
            "rows": outcome_universe,
        },
        # 推荐模块直接读取根 calibration，将分数映射为 T+5 热点概率。
        "calibration": calibration_t5,
        "calibration_by_horizon": {"t5": calibration_t5, "t10": calibration_t10},
        "industry": {
            "picks": industry_picks,
            "summary": _summary(industry_picks),
            "evaluation": industry_eval,
            "calibration": {"t5": calibration_t5, "t10": calibration_t10},
        },
        "momentum": {
            "picks": momentum_picks,
            "summary": _summary(momentum_picks),
            "evaluation": momentum_eval,
        },
        "baselines": {
            "relative_strength_5d": {
                "description": "D日收盘可知的5日ETF相对510300强弱，Top3",
                "picks": baseline_picks,
                "summary": _summary(baseline_picks),
                "evaluation": baseline_eval,
            },
        },
        "intraday": {
            "name": "14:50 Top2 单日轮换",
            "enabled": False,
            "status": "disabled",
            "method": "disabled_no_intraday_data",
            "reason": INTRADAY_DISABLED_REASON,
            "rules": None,
            "trades": [],
            "curve": [],
            "summary": _intraday_summary([]),
            "limitations": INTRADAY_DISABLED_REASON,
        },
    }
    momentum_etf._atomic_json_dump(result, data_path(OUTPUT_FILE))

    for mod in ("industry", "momentum"):
        s = result[mod]["summary"]
        print(f"[backtest] {mod}: {s['total']} 条推荐, "
              f"T+5胜率 {s['t5']['win']}% (均 {s['t5']['mean']}%), "
              f"均最大涨幅 {s['avg_max_up']}% / 均最大回撤 {s['avg_max_dd']}%")
    print(f"[backtest] 14:50 Top2: 已禁用（{INTRADAY_DISABLED_REASON}）")
    return result


if __name__ == "__main__":
    run_backtest()
    print(f"\n✅ 已生成 {OUTPUT_FILE}")
