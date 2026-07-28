"""
统一更新引擎 —— 替代 subprocess，直接调用函数写入 SQLite。
解决：管道 Broken pipe、超时、沙箱限制
"""

import json, os, re, sys, time, traceback
from datetime import datetime, time as dt_time

# 确保可以 import 同目录的模块
sys.path.insert(0, os.path.dirname(__file__))

from db import get_db
from runtime_paths import data_path, resource_path
from kline_cache import (
    KlineCache, get_active_codes, load_industry_map, get_trade_dates,
    SW2021_INDUSTRY_MAP, format_date_short, format_date_for_query,
)
from index_constituents import _citic_industry, _load_sw_detail_map
import akshare as ak
import pandas as pd
import numpy as np


_ind_map_cache = {}


def _load_ind_map(scheme="sw"):
    """加载行业映射（进程内缓存，避免每次重复请求股票列表/读文件）"""
    if scheme in _ind_map_cache:
        return _ind_map_cache[scheme]
    active_codes = get_active_codes()
    if scheme == "ths":
        ths_path = resource_path("industry_map_ths.json")
        ths_map = json.load(open(ths_path, encoding="utf-8"))
        sw_map = load_industry_map(active_codes)
        result = {}
        for c in active_codes:
            if c in ths_map: result[c] = ths_map[c]
            elif c in sw_map: result[c] = sw_map[c]
            else: result[c] = "其他"
    elif scheme == "citic":
        sw_map = load_industry_map(active_codes)
        result = {c: _citic_industry(None, sw_map.get(c)) or "其他" for c in active_codes}
    elif scheme == "sw3":
        result = _load_sw_detail_map(active_codes)
    else:
        result = load_industry_map(active_codes)
    _ind_map_cache[scheme] = result
    return result


def _close_data_ready(now=None):
    now = now or datetime.now()
    return now.time() >= dt_time(15, 10)


def _get_trade_dates(n=20, now=None):
    """获取最近 N 个已完成交易日，盘中不把今天当作收盘数据。"""
    df = ak.tool_trade_date_hist_sina()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    now = now or datetime.now()
    now_ts = pd.Timestamp(now)
    today = now_ts.normalize()
    completed = df[df["trade_date"] <= today]
    if not _close_data_ready(now) and not completed.empty and completed.iloc[-1]["trade_date"] == today:
        completed = completed[completed["trade_date"] < today]
    return completed.tail(n)["trade_date"].dt.strftime("%Y%m%d").tolist()


def _validate_date_coverage(all_data, codes, target_dates, min_coverage=0.9):
    date_sets = {
        code: set(df["date"].dt.strftime("%Y%m%d"))
        for code, df in all_data.items() if df is not None and not df.empty
    }
    weak = []
    for date_str in target_dates:
        covered = sum(1 for code in codes if date_str in date_sets.get(code, set()))
        ratio = covered / max(len(codes), 1)
        if ratio < min_coverage:
            weak.append((date_str, covered, ratio))
    if weak:
        summary = ", ".join(f"{d}:{n}/{len(codes)} ({r:.1%})" for d, n, r in weak[:5])
        raise RuntimeError(f"K-line coverage too low: {summary}")
    return min(
        (sum(1 for code in codes if date_str in date_sets.get(code, set())) / max(len(codes), 1)
         for date_str in target_dates),
        default=0,
    )


# ==================== 新高/新低计算 ====================

def _find_events(all_data, target_dates, window_days, direction="highs", alltime_boundary=None):
    """从 K 线数据中找出创 N 日新高/新低的股票"""
    target_set = set(target_dates)
    results = {ds: [] for ds in target_dates}

    for code, df in all_data.items():
        if df is None or df.empty or len(df) < 2:
            continue
        if code.startswith("9"):
            continue

        closes = df["close"].values
        date_strs = df["date"].dt.strftime("%Y%m%d").values
        ab = alltime_boundary.get(code) if alltime_boundary else None

        target_indices = np.where(np.isin(date_strs, list(target_set)))[0]

        for idx in target_indices:
            if idx == 0 and window_days is None:
                continue
            if idx < (window_days or 1):
                continue

            if window_days is None:
                if direction == "highs":
                    prev_extreme = np.max(closes[:idx])
                    if ab is not None: prev_extreme = max(ab, prev_extreme)
                    is_event = closes[idx] > prev_extreme
                else:
                    running_min = np.minimum.accumulate(closes)
                    prev_extreme = running_min[idx - 1]
                    if ab is not None: prev_extreme = min(ab, prev_extreme)
                    is_event = closes[idx] < prev_extreme
            else:
                if direction == "highs":
                    prev_extreme = np.max(closes[idx - window_days:idx])
                    is_event = closes[idx] > prev_extreme
                else:
                    prev_extreme = np.min(closes[idx - window_days:idx])
                    is_event = closes[idx] < prev_extreme

            if is_event:
                price = float(closes[idx])
                prev_close = float(closes[idx - 1]) if idx > 0 else price
                chg = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0
                results[date_strs[idx]].append({
                    "code": code, "price": price, "change_pct": chg,
                })

    return results


def build_custom_heatmap_snapshot(window_days, scheme="sw", target_dates=None, n_dates=20):
    """Build a read-only custom-window close snapshot without creating period files."""
    try:
        window_days = int(window_days)
    except (TypeError, ValueError):
        raise ValueError("window_days must be an integer")
    if not 5 <= window_days <= 250:
        raise ValueError("window_days must be between 5 and 250")
    if scheme not in ("sw", "ths", "sw3"):
        raise ValueError("scheme must be sw, ths or sw3")

    target_dates = sorted(set(target_dates or _get_trade_dates(n_dates)))
    if not target_dates:
        raise RuntimeError("no trading dates available")
    active_codes = get_active_codes()
    industry_map = _load_ind_map(scheme)
    cache = KlineCache(force_refresh=False)
    all_data = cache.ensure_dates(active_codes, target_dates)
    latest_dt = pd.Timestamp(
        f"{target_dates[-1][:4]}-{target_dates[-1][4:6]}-{target_dates[-1][6:8]}"
    )
    covered = sum(
        1 for df in all_data.values()
        if df is not None and not df.empty and df["date"].max() >= latest_dt
    )

    try:
        name_df = ak.stock_info_a_code_name()
        name_map = dict(zip(name_df["code"].astype(str).str.zfill(6), name_df["name"]))
    except Exception:
        name_map = {}
    try:
        with open(data_path("stock_shares.json"), encoding="utf-8") as handle:
            share_payload = json.load(handle)
            shares = (
                share_payload.get("total_shares") or {}
                if isinstance(share_payload, dict)
                and share_payload.get("version") == 3
                else {}
            )
    except (OSError, json.JSONDecodeError):
        shares = {}

    ind_totals = {}
    for code, industry in industry_map.items():
        ind_totals[industry] = ind_totals.get(industry, 0) + 1
    if scheme == "sw":
        industries = list(dict.fromkeys(
            industry for industry in SW2021_INDUSTRY_MAP.values() if industry != "综合"
        ))
    else:
        industries = sorted(ind_totals, key=lambda industry: (-ind_totals[industry], industry))
    if "其他" not in industries:
        industries.append("其他")

    date_list = sorted(target_dates, reverse=True)
    result = {
        "window_days": window_days,
        "scheme": scheme,
        "session": "daily",
        "coverage": {
            "active": len(active_codes),
            "covered": covered,
            "ratio": covered / max(len(active_codes), 1),
        },
    }
    for direction in ("highs", "lows"):
        daily_stocks = _find_events(all_data, target_dates, window_days, direction)
        by_industry = {industry: {date: [] for date in date_list} for industry in industries}
        for date, stocks in daily_stocks.items():
            for stock in stocks:
                code = stock["code"]
                industry = industry_map.get(code, "其他")
                if industry not in by_industry:
                    by_industry[industry] = {item: [] for item in date_list}
                    industries.append(industry)
                share_count = shares.get(code)
                try:
                    mcap = round(float(stock["price"]) * int(share_count)) if share_count else None
                except (TypeError, ValueError):
                    mcap = None
                by_industry[industry][date].append({
                    "code": code,
                    "name": name_map.get(code, code),
                    "price": round(float(stock["price"]), 2),
                    "change_pct": round(float(stock["change_pct"]), 2),
                    "mcap": mcap,
                })

        rows = []
        details = {}
        for industry in industries:
            counts = [len(by_industry.get(industry, {}).get(date, [])) for date in date_list]
            total = ind_totals.get(industry, 0)
            rows.append({
                "industry": industry,
                "total": total,
                "daily_counts": counts,
                "ratio": round(counts[0] / max(total, 1) * 100, 1),
            })
            details[industry] = {
                format_date_short(date): by_industry.get(industry, {}).get(date, [])
                for date in date_list
            }
        total_counts = [sum(row["daily_counts"][i] for row in rows) for i in range(len(date_list))]
        rows.sort(key=lambda row: (-row["daily_counts"][0], row["industry"]))
        rows.append({
            "industry": "全市场合计",
            "total": sum(row["total"] for row in rows),
            "daily_counts": total_counts,
            "is_total": True,
        })
        word = "新高" if direction == "highs" else "新低"
        result[direction] = {
            "dates": [
                {"label": format_date_short(date), "full_label": format_date_for_query(date)}
                for date in date_list
            ],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "type": f"custom_{window_days}d",
            "type_label": f"创{window_days}日{word}",
            "window_days": window_days,
            "scheme": scheme,
            "session": "daily",
            "industries": rows,
        }
        result[f"{direction}_details"] = details
    return result


def update_highs_lows(target_dates=None, schemes=None, periods=None, directions=None,
                      force_refresh=False, min_coverage=0.9, cache=None):
    """计算新高/新低，直接写入 SQLite。同时输出 SW + THS。"""
    target_dates = target_dates or _get_trade_dates(20)
    schemes = schemes or ["sw", "ths", "sw3"]
    periods = periods or ["month", "60d", "120d", "1year", "alltime"]
    directions = directions or ["highs", "lows"]
    invalid_directions = set(directions) - {"highs", "lows"}
    if invalid_directions:
        raise ValueError(f"unsupported directions: {sorted(invalid_directions)}")
    window_map = {"month": 20, "60d": 60, "120d": 120, "1year": 250, "alltime": None}

    db = get_db()
    active_codes = get_active_codes()
    name_df = ak.stock_info_a_code_name()
    name_map = dict(zip(name_df["code"].astype(str).str.zfill(6), name_df["name"]))
    try:
        with open(data_path("stock_shares.json"), encoding="utf-8") as handle:
            share_payload = json.load(handle)
            shares = (
                share_payload.get("total_shares") or {}
                if isinstance(share_payload, dict)
                and share_payload.get("version") == 3
                else {}
            )
    except (OSError, json.JSONDecodeError):
        shares = {}

    results = {"highs": {}, "lows": {}}

    # K-line data does not depend on the industry scheme. Load and validate it once.
    cache = cache or KlineCache(force_refresh=force_refresh)
    all_data = cache.ensure_dates(active_codes, target_dates)
    coverage = _validate_date_coverage(all_data, active_codes, target_dates, min_coverage)
    covered = round(coverage * len(active_codes))
    hb = cache.alltime_high_before
    lb = cache.alltime_low_before

    slices = []
    for scheme in schemes:
        industry_map = _load_ind_map(scheme)

        for direction, boundary in [("highs", hb), ("lows", lb)]:
            if direction not in directions:
                continue
            label = "新高" if direction == "highs" else "新低"
            for period in periods:
                w = window_map[period]
                print(f"  [{scheme}] {label}/{period} (窗口={w or 'alltime'})...")
                daily_stocks = _find_events(all_data, target_dates, w, direction, boundary)

                # 添加名称和行业
                for ds, stocks in daily_stocks.items():
                    for s in stocks:
                        s["name"] = name_map.get(s["code"], s["code"])
                        s["industry"] = industry_map.get(s["code"], "其他")

                # 聚合
                date_list = sorted(daily_stocks.keys(), reverse=True)
                ind_counts = {}
                for ds in date_list:
                    for s in daily_stocks.get(ds, []):
                        ind = s["industry"]
                        ind_counts.setdefault(ind, {}).setdefault(ds, 0)
                        ind_counts[ind][ds] += 1

                # 行业总数
                ind_totals = {}
                for code, ind in industry_map.items():
                    ind_totals[ind] = ind_totals.get(ind, 0) + 1

                # 确定主行业列表（与 legacy fetch_new_highs 一致，排除“综合”）
                if scheme == "sw":
                    main = list(dict.fromkeys(
                        ind for ind in SW2021_INDUSTRY_MAP.values() if ind != "综合"
                    ))
                else:
                    main = sorted(
                        set(industry_map.values()) - {"综合"},
                        key=lambda x: -ind_totals.get(x, 0),
                    )
                if ind_totals.get("其他", 0) and "其他" not in main:
                    main.append("其他")

                # 构建记录
                records = []
                for ind in main:
                    for i, ds in enumerate(date_list):
                        count = ind_counts.get(ind, {}).get(ds, 0)
                        records.append({
                            "date": ds, "period": period, "scheme": scheme,
                            "industry": ind, "count": count,
                            "total_stocks": ind_totals.get(ind, 0),
                            "is_total": 0,
                        })
                    # 全市场合计
                totals = [0] * len(date_list)
                for ind in main:
                    for i, ds in enumerate(date_list):
                        totals[i] += ind_counts.get(ind, {}).get(ds, 0)
                for i, ds in enumerate(date_list):
                    records.append({
                        "date": ds, "period": period, "scheme": scheme,
                        "industry": "全市场合计", "count": totals[i],
                        "total_stocks": sum(ind_totals.get(i, 0) for i in main),
                        "is_total": 1,
                    })

                detail_records = []
                for ds, stocks in daily_stocks.items():
                    for stock in stocks:
                        share_count = shares.get(stock["code"])
                        try:
                            mcap = (
                                round(float(stock["price"]) * int(share_count))
                                if share_count else None
                            )
                        except (TypeError, ValueError):
                            mcap = None
                        detail_records.append({
                            "date": ds, "direction": direction, "period": period,
                            "scheme": scheme, "industry": stock["industry"],
                            "code": stock["code"], "name": stock["name"],
                            "price": stock["price"], "change_pct": stock["change_pct"],
                            "mcap": mcap,
                        })
                slices.append({
                    "records": records, "detail_records": detail_records,
                    "direction": direction, "period": period,
                    "scheme": scheme, "dates": date_list,
                })
                results[direction][(scheme, period)] = len(records)
                print(f"    {len(records)} 条记录")

    db.replace_heatmap_batch(slices)
    results["coverage"] = {"covered": covered, "total": len(active_codes), "ratio": coverage}
    return results


# ==================== 资金流向计算 ====================

def update_capital_flow(target_dates=None, schemes=None, min_coverage=0.9, cache=None):
    """计算资金流向，直接写入 SQLite"""
    target_dates = target_dates or _get_trade_dates(20)
    schemes = schemes or ["sw", "ths", "sw3"]

    db = get_db()

    all_records = []
    dates_by_scheme = {}
    cache = cache or KlineCache(force_refresh=False)
    for scheme in schemes:
        print(f"[{scheme}] 资金流向...")
        ind_map = _load_ind_map(scheme)
        # 全市场成交额不应随行业分类覆盖变化。未进入行业映射的 active 股票
        # （如新股）仍需计入市场并暂归“其他”，否则全市场口径系统性偏小。
        codes = list(get_active_codes())

        all_data = cache.ensure_dates(codes, target_dates)
        _validate_date_coverage(all_data, codes, target_dates, min_coverage)

        target_set = set(target_dates)
        ind_turnover = {}
        ind_net = {}
        ind_stocks = {}

        for code, df in all_data.items():
            if df is None or df.empty:
                continue
            ind = ind_map.get(code) or "其他"
            frame = df.sort_values("date").reset_index(drop=True)
            date_strs = frame["date"].dt.strftime("%Y%m%d").to_numpy()
            closes = frame["close"].astype(float).to_numpy()
            volumes = frame["volume"].astype(float).to_numpy()
            # 前收 = 同一股票按日期排序后的上一行收盘（平盘时净流为 0，与原逻辑一致）
            for idx in np.nonzero(np.isin(date_strs, list(target_set)))[0]:
                ds = date_strs[idx]
                cur_close = float(closes[idx])
                turnover = cur_close * float(volumes[idx])
                prev_close = float(closes[idx - 1]) if idx > 0 else 0
                net = turnover if (prev_close and prev_close > 0 and cur_close > prev_close) else \
                      (-turnover if prev_close and cur_close < prev_close else 0)

                ind_turnover.setdefault(ind, {}).setdefault(ds, 0)
                ind_net.setdefault(ind, {}).setdefault(ds, 0)
                ind_stocks.setdefault(ind, {}).setdefault(ds, 0)
                ind_turnover[ind][ds] += turnover
                ind_net[ind][ds] += net
                ind_stocks[ind][ds] += 1

        records = []
        for ind in sorted(ind_turnover.keys()):
            for ds in target_dates:
                records.append({
                    "date": ds, "scheme": scheme, "industry": ind,
                    "turnover": round(ind_turnover[ind].get(ds, 0)),
                    "net_flow": round(ind_net[ind].get(ds, 0)),
                    "stock_count": ind_stocks[ind].get(ds, 0),
                    "is_total": 0,
                })

        # 全市场合计
        for ds in target_dates:
            t = sum(ind_turnover.get(i, {}).get(ds, 0) for i in ind_turnover)
            n = sum(ind_net.get(i, {}).get(ds, 0) for i in ind_net)
            s = sum(ind_stocks.get(i, {}).get(ds, 0) for i in ind_stocks)
            records.append({"date": ds, "scheme": scheme, "industry": "全市场合计",
                           "turnover": round(t), "net_flow": round(n),
                           "stock_count": s, "is_total": 1})

        all_records.extend(records)
        dates_by_scheme[scheme] = target_dates
        print(f"  {len(records)} 条记录")

    db.replace_capital_flow_batch(all_records, dates_by_scheme)
    return True


# ==================== 市值计算 ====================

SHARES_TTL_SECONDS = 24 * 60 * 60
MIN_TOTAL_SHARE_COVERAGE = 0.95


def _snapshot_datetime(value):
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _load_share_snapshot(codes, as_of_date=None):
    """加载腾讯当前股本快照。

    qt 字段 72 是流通股本，字段 73 才是总股本。旧版 v2 缓存把 72
    错当成总股本，不能继续用于总市值；迁移时仅保留为流通股参考并强制
    刷新字段 73。
    """
    import requests
    shares_file = data_path("stock_shares.json")
    total_shares = {}
    circulating_shares = {}
    updated_at = None
    if os.path.exists(shares_file):
        try:
            payload = json.load(open(shares_file, encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and payload.get("version") == 3
                and isinstance(payload.get("total_shares"), dict)
            ):
                total_shares = payload["total_shares"]
                circulating_shares = (
                    payload.get("circulating_shares") or {})
                updated_at = payload.get("updated_at")
                snapshot_time = _snapshot_datetime(updated_at)
                age = (
                    time.time() - snapshot_time.timestamp()
                    if snapshot_time else float("inf")
                )
                coverage = sum(
                    1 for code in codes if code in total_shares
                ) / max(len(codes), 1)
                snapshot_date = (
                    snapshot_time.strftime("%Y%m%d")
                    if snapshot_time else None
                )
                date_is_current = (
                    not as_of_date
                    or (
                        snapshot_date is not None
                        and snapshot_date >= str(as_of_date)
                    )
                )
                if (
                    age < SHARES_TTL_SECONDS
                    and coverage >= 0.95
                    and date_is_current
                ):
                    return {
                        "version": 3,
                        "updated_at": updated_at,
                        "total_shares": total_shares,
                        "circulating_shares": circulating_shares,
                        "legacy_invalid": False,
                        "snapshot_asof": snapshot_date,
                        "stale": False,
                    }
            elif (
                isinstance(payload, dict)
                and isinstance(payload.get("shares"), dict)
            ):
                # v2 shares came from field 72: keep only as circulating proxy.
                circulating_shares = dict(payload["shares"])
            elif isinstance(payload, dict):
                # Legacy plain mapping also came from field 72.
                circulating_shares = {
                    code: value for code, value in payload.items()
                    if len(str(code)) == 6 and _finite_share(value)
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            total_shares = {}
            circulating_shares = {}

    refreshed = 0
    failed_batches = 0
    for i in range(0, len(codes), 100):
        batch = codes[i:i+100]
        codes_str = ",".join(f"{'sh' if c.startswith(('6','9')) else 'sz'}{c}" for c in batch)
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={codes_str}", timeout=10)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if "~" not in line: continue
                parts = line.split("~")
                if len(parts) > 73:
                    match = re.match(r"v_(?:sh|sz)(\d{6})=", parts[0])
                    code = match.group(1) if match else ""
                    circulating = parts[72].strip()
                    total = parts[73].strip()
                    if code:
                        try:
                            if total and float(total) > 0:
                                total_shares[code] = float(total)
                                refreshed += 1
                            if circulating and float(circulating) > 0:
                                circulating_shares[code] = float(
                                    circulating)
                        except (TypeError, ValueError):
                            pass
        except Exception as exc:
            failed_batches += 1
            print(f"[shares] 腾讯股本批次 {i // 100 + 1} 拉取失败: {exc}")
    total_batches = (len(codes) + 99) // 100
    if failed_batches:
        print(
            f"[shares] 警告: {failed_batches}/{total_batches} 个股本批次拉取失败，"
            "股本快照可能不完整"
        )
    if refreshed:
        payload = {
            "version": 3,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "source": {
                "provider": "Tencent qt",
                "total_shares_field": 73,
                "circulating_shares_field": 72,
            },
            "total_shares": total_shares,
            "circulating_shares": circulating_shares,
        }
        tmp = shares_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, shares_file)
        updated_at = payload["updated_at"]
    snapshot_time = _snapshot_datetime(updated_at)
    stale = (
        not updated_at
        or (
            bool(as_of_date)
            and (
                snapshot_time is None
                or snapshot_time.strftime("%Y%m%d")
                < str(as_of_date)
            )
        )
    )
    if stale:
        print(
            f"[shares] 警告: 股本快照过期 "
            f"(updated_at={updated_at or '无'}, as_of={as_of_date or '无'}, "
            f"失败批次={failed_batches})，市值数据可能不准确"
        )
    return {
        "version": 3,
        "updated_at": updated_at,
        "total_shares": total_shares,
        "circulating_shares": circulating_shares,
        "legacy_invalid": not bool(total_shares),
        "failed_batches": failed_batches,
        "snapshot_asof": (
            snapshot_time.strftime("%Y%m%d")
            if snapshot_time else None
        ),
        "stale": stale,
    }


def _finite_share(value):
    try:
        return value is not None and np.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _load_shares(codes, as_of_date=None):
    """Compatibility helper: return current *total* shares (Tencent field 73)."""
    return _load_share_snapshot(
        codes, as_of_date=as_of_date)["total_shares"]


def _validate_total_share_coverage(
    codes,
    total_shares,
    *,
    circulating_shares=None,
    all_data=None,
    target_date=None,
    min_coverage=MIN_TOTAL_SHARE_COVERAGE,
):
    """Fail closed before replacing market-cap data on a weak share snapshot.

    Code coverage catches a legacy/offline cache immediately.  When prices and
    circulating shares are available, a circulating-market-cap weighted proxy
    additionally catches the more dangerous case where a small number of
    missing large companies passes the stock-count threshold.
    """
    normalized_codes = [str(code).zfill(6) for code in codes]
    valid_total = {
        code for code in normalized_codes
        if _finite_share((total_shares or {}).get(code))
    }
    code_ratio = len(valid_total) / max(len(normalized_codes), 1)
    if code_ratio < min_coverage:
        raise RuntimeError(
            "Total-share coverage too low; market-cap data was not replaced: "
            f"{len(valid_total)}/{len(normalized_codes)} ({code_ratio:.1%}), "
            f"required {min_coverage:.0%}"
        )

    value_ratio = None
    if all_data and circulating_shares and target_date:
        proxy_total = 0.0
        proxy_covered = 0.0
        target_timestamp = pd.Timestamp(str(target_date))
        for code in normalized_codes:
            circulating = (circulating_shares or {}).get(code)
            frame = all_data.get(code)
            if (
                not _finite_share(circulating)
                or frame is None
                or frame.empty
            ):
                continue
            ordered = frame.sort_values("date").reset_index(drop=True)
            position = ordered["date"].searchsorted(
                target_timestamp, side="right") - 1
            if position < 0:
                continue
            try:
                close_value = float(ordered.iloc[position]["close"])
            except (TypeError, ValueError):
                continue
            if not np.isfinite(close_value) or close_value <= 0:
                continue
            proxy_value = close_value * float(circulating)
            proxy_total += proxy_value
            if code in valid_total:
                proxy_covered += proxy_value
        if proxy_total > 0:
            value_ratio = proxy_covered / proxy_total
            if value_ratio < min_coverage:
                raise RuntimeError(
                    "Total-share value coverage too low; market-cap data was "
                    "not replaced: circulating-market-cap proxy "
                    f"{value_ratio:.1%}, required {min_coverage:.0%}"
                )
    return {
        "code_ratio": code_ratio,
        "value_ratio": value_ratio,
    }


def update_market_cap(
    target_dates=None,
    schemes=None,
    min_coverage=0.9,
    share_min_coverage=MIN_TOTAL_SHARE_COVERAGE,
    cache=None,
):
    """计算行业市值，直接写入 SQLite。

    市值结构至少重建最近 60 个交易日。停牌股票使用估值日前最后一个可用
    收盘价前向承接；股本优先使用 CNINFO 点时总股本，缺失时才降级到腾讯
    当前股本缓存。单只股票的股本接口失败不会阻断整体更新。
    """
    requested_dates = sorted(set(target_dates or []))
    try:
        history_dates = _get_trade_dates(60)
    except Exception:
        history_dates = []
    target_dates = sorted(set(
        requested_dates + history_dates
        or _get_trade_dates(60)
    ))
    schemes = schemes or ["sw", "ths", "sw3"]

    db = get_db()
    active_codes = get_active_codes()
    current_share_snapshot = _load_share_snapshot(
        active_codes, as_of_date=target_dates[-1])
    if current_share_snapshot.get("stale"):
        print(
            f"[shares] 警告: 股本快照过期 "
            f"(snapshot_asof={current_share_snapshot.get('snapshot_asof')}, "
            f"估值日={target_dates[-1]})，市值数据可能不准确"
        )
    current_shares = current_share_snapshot["total_shares"]
    current_circulating_shares = current_share_snapshot[
        "circulating_shares"]
    # This check deliberately happens before any DB mutation. A legacy v2
    # field-72 cache plus an offline Tencent endpoint otherwise produces
    # near-zero totals and could replace a previously valid market-cap slice.
    _validate_total_share_coverage(
        active_codes,
        current_shares,
        circulating_shares=current_circulating_shares,
        min_coverage=share_min_coverage,
    )
    point_in_time_history = {
        "total_shares": {},
        "circulating_a_shares": {},
        "events": {},
    }
    try:
        from share_history_cninfo import (
            refresh_point_in_time_share_history,
        )
        point_in_time_history = refresh_point_in_time_share_history(
            active_codes,
            target_dates,
            current_shares,
            current_circulating_shares=current_circulating_shares,
            max_workers=8,
        )
        covered = len(point_in_time_history.get("total_shares") or {})
        print(
            f"[shares] CNINFO 点时股本覆盖 {covered}/{len(active_codes)}，"
            "其余使用当前股本代理"
        )
    except Exception as exc:
        # 市值更新必须可以在离线环境继续运行。
        print(f"[shares] 点时股本刷新失败，降级当前股本代理: {exc}")
    dated_shares = point_in_time_history.get("total_shares") or {}

    all_records = []
    all_details = []
    dates_by_scheme = {}
    cache = cache or KlineCache(force_refresh=False)
    for scheme in schemes:
        print(f"[{scheme}] 市值...")
        ind_map = _load_ind_map(scheme)
        # 全市场总额不应随行业分类覆盖变化。新股可能尚未进入申万映射，
        # 仍需计入市场并暂归“其他”，否则三套分类的总市值会不一致。
        codes = list(active_codes)

        all_data = cache.ensure_dates(codes, target_dates)
        _validate_date_coverage(all_data, codes, target_dates, min_coverage)
        _validate_total_share_coverage(
            codes,
            current_shares,
            circulating_shares=current_circulating_shares,
            all_data=all_data,
            target_date=target_dates[-1],
            min_coverage=share_min_coverage,
        )

        ind_mcap = {}
        ind_stocks = {}  # {ind: {date: [{code, name, close, mcap, change_pct}]}}
        name_map = {}
        try:
            df_names = ak.stock_info_a_code_name()
            name_map = dict(zip(df_names["code"].astype(str).str.zfill(6), df_names["name"]))
        except: pass

        for code, df in all_data.items():
            ind = ind_map.get(code) or "其他"
            current_share = current_shares.get(code)
            code_share_history = dated_shares.get(code) or {}
            if (
                not ind or df is None or df.empty
                or (not current_share and not code_share_history)
            ):
                continue
            frame = df.sort_values("date").reset_index(drop=True)
            asof_closes = {}
            for ds in target_dates:
                position = frame["date"].searchsorted(
                    pd.Timestamp(ds), side="right") - 1
                if position < 0:
                    continue  # not yet listed on this valuation date
                asof_closes[ds] = float(frame.iloc[position]["close"])
            if not asof_closes:
                continue
            for date_index, ds in enumerate(target_dates):
                close_val = asof_closes.get(ds)
                if close_val is None:
                    continue
                point_share = code_share_history.get(ds)
                share_value = point_share or current_share
                if (
                    share_value is None
                    or not np.isfinite(float(share_value))
                    or float(share_value) <= 0
                ):
                    continue
                mcap = close_val * float(share_value)
                previous_ds = (
                    target_dates[date_index - 1]
                    if date_index > 0 else None
                )
                prev_close = (
                    asof_closes.get(previous_ds, 0)
                    if previous_ds else 0
                )
                chg = (
                    round(
                        (close_val - prev_close) / prev_close * 100,
                        2,
                    )
                    if prev_close > 0 else 0
                )

                ind_mcap.setdefault(ind, {}).setdefault(ds, 0)
                ind_mcap[ind][ds] += mcap
                ind_stocks.setdefault(ind, {}).setdefault(ds, []).append({
                    "code": code, "name": name_map.get(code, ""),
                    "close": round(close_val, 2), "mcap": round(mcap),
                    "change_pct": chg,
                })

        # 存储个股明细到 SQLite
        detail_records = []
        for ind, by_date in ind_stocks.items():
            for ds, stocks in by_date.items():
                for s in stocks:
                    detail_records.append({
                        "date": ds, "direction": "market_cap", "period": "daily",
                        "scheme": scheme, "industry": ind,
                        "code": s["code"], "name": s["name"],
                        "price": s["close"], "change_pct": s["change_pct"],
                        "mcap": s["mcap"],
                    })
        all_details.extend(detail_records)

        records = []
        for ind in sorted(ind_mcap.keys()):
            for ds in target_dates:
                records.append({
                    "date": ds, "scheme": scheme, "industry": ind,
                    "mcap": round(ind_mcap[ind].get(ds, 0)),
                    "stock_count": len(ind_stocks.get(ind, {}).get(ds, [])),
                    "is_total": 0,
                })

        # 全市场合计
        for ds in target_dates:
            t = sum(ind_mcap.get(i, {}).get(ds, 0) for i in ind_mcap)
            stock_count = sum(len(ind_stocks.get(i, {}).get(ds, [])) for i in ind_stocks)
            records.append({"date": ds, "scheme": scheme, "industry": "全市场合计",
                           "mcap": round(t), "stock_count": stock_count, "is_total": 1})

        all_records.extend(records)
        dates_by_scheme[scheme] = target_dates
        print(f"  {len(records)} 条记录")

    db.replace_market_cap_batch(all_records, all_details, dates_by_scheme)
    return True


# ==================== 一键更新 ====================

def run_all(datasets=None, days=20, force_refresh=False):
    """运行全部更新并导出 JSON。单个数据集失败不阻断其他数据集与导出。"""
    datasets = datasets or ["highs", "lows", "capital_flow", "market_cap", "etf", "temperature"]
    target_dates = _get_trade_dates(max(days, 20))
    print(f"目标日期: {len(target_dates)}天 ({target_dates[0]} ~ {target_dates[-1]})")

    t0 = time.time()
    status = {}
    # 共享一个 KlineCache 实例，避免每个数据集重复 pickle.load 整个缓存
    cache = KlineCache(force_refresh=force_refresh)

    if "highs" in datasets or "lows" in datasets:
        try:
            result = update_highs_lows(target_dates, force_refresh=force_refresh, cache=cache)
            record_count = sum(sum(group.values()) for key, group in result.items() if key in ("highs", "lows"))
            print(f"新高/新低完成: {record_count} 条")
            status["highs_lows"] = "ok"
        except Exception as e:
            status["highs_lows"] = f"failed: {e}"
            print(f"新高/新低失败: {e}")

    if "capital_flow" in datasets:
        try:
            update_capital_flow(target_dates, cache=cache)
            print("资金流完成")
            status["capital_flow"] = "ok"
        except Exception as e:
            status["capital_flow"] = f"failed: {e}"
            print(f"资金流失败: {e}")

    if "market_cap" in datasets:
        try:
            update_market_cap(target_dates, cache=cache)
            print("市值完成")
            status["market_cap"] = "ok"
        except Exception as e:
            status["market_cap"] = f"failed: {e}"
            print(f"市值失败: {e}")

    if "etf" in datasets:
        try:
            from etf_recommend import update_etf_recommend
            update_etf_recommend()
            print("ETF推荐完成")
        except Exception as e:
            print(f"ETF推荐失败(不影响主流程): {e}")
        try:
            from momentum_etf import update_momentum_etf
            update_momentum_etf()
            print("动量ETF完成")
        except Exception as e:
            print(f"动量ETF失败(不影响主流程): {e}")

    if "temperature" in datasets:
        try:
            from market_temperature import update_market_temperature
            n = update_market_temperature()
            print(f"市场温度完成: {n} 天")
        except Exception as e:
            print(f"市场温度失败(不影响主流程): {e}")
        try:
            from crowding import update_crowding
            n = update_crowding()
            print(f"拥挤度完成: {n} 天")
        except Exception as e:
            print(f"拥挤度失败(不影响主流程): {e}")

    # ETF V3 使用市场温度与行业拥挤风险。先完成上述风险数据，再重建候选和
    # 动态池，避免一次完整更新仍引用上一交易日的风险状态。
    if "etf" in datasets and "temperature" in datasets:
        try:
            from etf_recommend import build_recommendations
            build_recommendations()
            print("ETF热点候选已按最新温度/拥挤度重建")
        except Exception as e:
            print(f"ETF热点候选重建失败(不影响主流程): {e}")
        try:
            from momentum_etf import update_momentum_etf
            update_momentum_etf()
            print("动量ETF动态池已同步")
        except Exception as e:
            print(f"动量ETF同步失败(不影响主流程): {e}")

    # 导出 JSON：即使部分数据集失败，也把已成功写入 SQLite 的数据导出，
    # 避免 SQLite 与 JSON 长期脱钩
    try:
        from export_json import export_all
        export_all()
        status["export"] = "ok"
    except Exception as e:
        status["export"] = f"failed: {e}"
        print(f"JSON 导出失败: {e}")
    failed = {k: v for k, v in status.items() if v != "ok"}
    if failed:
        print(f"\n⚠️ 部分步骤失败: {failed}")
    print(f"\n✅ 全部完成 ({time.time()-t0:.1f}s)")
    return status


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=20)
    p.add_argument("--force-refresh", action="store_true")
    args = p.parse_args()
    run_all(days=args.days, force_refresh=args.force_refresh)
