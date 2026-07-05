"""
统一更新引擎 —— 替代 subprocess，直接调用函数写入 SQLite。
解决：管道 Broken pipe、超时、沙箱限制
"""

import json, os, sys, time, traceback

# 确保可以 import 同目录的模块
sys.path.insert(0, os.path.dirname(__file__))

from db import get_db
from kline_cache import (
    KlineCache, get_active_codes, load_industry_map, get_trade_dates,
    SW2021_INDUSTRY_MAP, format_date_short, format_date_for_query,
)
import akshare as ak
import pandas as pd
import numpy as np


def _load_ind_map(scheme="sw"):
    """加载行业映射"""
    active_codes = get_active_codes()
    STATIC = os.path.join(os.path.dirname(__file__), "static")
    if scheme == "ths":
        ths_path = os.path.join(STATIC, "industry_map_ths.json")
        ths_map = json.load(open(ths_path, encoding="utf-8"))
        sw_map = load_industry_map(active_codes)
        result = {}
        for c in active_codes:
            if c in ths_map: result[c] = ths_map[c]
            elif c in sw_map: result[c] = sw_map[c]
            else: result[c] = "其他"
        return result
    return load_industry_map(active_codes)


def _get_trade_dates(n=20):
    """获取最近 N 个交易日"""
    df = ak.tool_trade_date_hist_sina()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    end = pd.Timestamp.now()
    return df[df["trade_date"] <= end].tail(n)["trade_date"].dt.strftime("%Y%m%d").tolist()


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


def update_highs_lows(target_dates=None, schemes=None, periods=None, force_refresh=False):
    """计算新高/新低，直接写入 SQLite。同时输出 SW + THS。"""
    target_dates = target_dates or _get_trade_dates(20)
    schemes = schemes or ["sw", "ths"]
    periods = periods or ["month", "60d", "120d", "1year", "alltime"]
    window_map = {"month": 20, "60d": 60, "120d": 120, "1year": 250, "alltime": None}

    db = get_db()
    active_codes = get_active_codes()
    name_df = ak.stock_info_a_code_name()
    name_map = dict(zip(name_df["code"].astype(str).str.zfill(6), name_df["name"]))

    results = {"highs": {}, "lows": {}}

    for scheme in schemes:
        industry_map = _load_ind_map(scheme)
        codes_with_ind = [c for c in active_codes if c in industry_map]

        print(f"[{scheme}] 加载 K 线缓存 ({len(codes_with_ind)} 只)...")
        cache = KlineCache(force_refresh=force_refresh)
        all_data = cache.ensure(codes_with_ind, target_dates[-1])
        # 检查中间日期是否有数据，没有则重建缓存
        sample_stocks = list(all_data.keys())[:100]
        missing_any = False
        for code in sample_stocks:
            df = all_data.get(code)
            if df is not None and not df.empty:
                date_strs = set(df["date"].dt.strftime("%Y%m%d"))
                if not all(d in date_strs for d in target_dates):
                    missing_any = True
                    break
        if missing_any:
            print(f"[{scheme}] 缓存缺失中间日期，强制重建...")
            cache = KlineCache(force_refresh=True)
            all_data = cache.ensure(codes_with_ind, target_dates[-1])
        hb = cache.alltime_high_before
        lb = cache.alltime_low_before

        for direction, boundary in [("highs", hb), ("lows", lb)]:
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

                # 确定主行业列表
                if scheme == "sw":
                    main = [i for i in SW2021_INDUSTRY_MAP.values() if i != "综合"]
                else:
                    main = sorted(set(industry_map.values()), key=lambda x: -ind_totals.get(x, 0))
                if "其他" not in main:
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

                db.insert_highs_lows(records, direction)
                results[direction][(scheme, period)] = len(records)
                print(f"    {len(records)} 条记录")

    return results


# ==================== 资金流向计算 ====================

def update_capital_flow(target_dates=None, schemes=None):
    """计算资金流向，直接写入 SQLite"""
    target_dates = target_dates or _get_trade_dates(20)
    schemes = schemes or ["sw", "ths"]

    db = get_db()

    for scheme in schemes:
        print(f"[{scheme}] 资金流向...")
        ind_map = _load_ind_map(scheme)
        codes = [c for c in ind_map if c in ind_map]  # all mapped codes

        cache = KlineCache(force_refresh=False)
        all_data = cache.ensure(codes, target_dates[-1])
        # 检查中间日期是否有数据
        sample_codes = [c for c in list(all_data.keys())[:100]]
        if sample_codes:
            df = all_data.get(sample_codes[0])
            if df is not None and not df.empty:
                date_strs = set(df["date"].dt.strftime("%Y%m%d"))
                if not all(d in date_strs for d in target_dates):
                    print(f"[{scheme}] 缓存缺失中间日期，强制重建...")
                    cache = KlineCache(force_refresh=True)
                    all_data = cache.ensure(codes, target_dates[-1])

        target_set = set(target_dates)
        ind_turnover = {}
        ind_net = {}
        ind_stocks = {}

        for code, df in all_data.items():
            ind = ind_map.get(code)
            if not ind or df is None or df.empty:
                continue
            date_strs = df["date"].dt.strftime("%Y%m%d")
            subset = df[date_strs.isin(target_set)]
            if subset.empty:
                continue

            closes = {}
            for _, row in df.iterrows():
                closes[row["date"].strftime("%Y%m%d")] = float(row["close"])

            for _, row in subset.iterrows():
                ds = row["date"].strftime("%Y%m%d")
                turnover = float(row["close"]) * float(row["volume"])
                cur_close = float(row["close"])
                prev_dates = sorted([d for d in closes if d < ds])
                prev_close = closes.get(prev_dates[-1], 0) if prev_dates else 0
                net = turnover if (prev_close and prev_close > 0 and cur_close >= prev_close) else -turnover

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

        db.insert_capital_flow(records)
        print(f"  {len(records)} 条记录")

    return True


# ==================== 市值计算 ====================

def _load_shares(codes):
    """加载股本数据"""
    import requests
    STATIC = os.path.join(os.path.dirname(__file__), "static")
    shares_file = os.path.join(STATIC, "stock_shares.json")
    if os.path.exists(shares_file):
        return json.load(open(shares_file, encoding="utf-8"))
    # 如果缓存不存在，从腾讯 API 获取
    shares = {}
    for i in range(0, len(codes), 100):
        batch = codes[i:i+100]
        codes_str = ",".join(f"{'sh' if c.startswith(('6','9')) else 'sz'}{c}" for c in batch)
        try:
            r = requests.get(f"https://qt.gtimg.cn/q={codes_str}", timeout=10)
            r.encoding = "gbk"
            for line in r.text.strip().split("\n"):
                if "~" not in line: continue
                parts = line.split("~")
                if len(parts) > 72:
                    tc = parts[0].split("_")[-1] if "_" in parts[0] else ""
                    code = tc[2:] if tc.startswith(("sh", "sz")) else tc
                    sh = parts[72].strip()
                    if code and sh:
                        try: shares[code] = float(sh)
                        except: pass
        except Exception: pass
    json.dump(shares, open(shares_file, "w", encoding="utf-8"))
    return shares


def update_market_cap(target_dates=None, schemes=None):
    """计算行业市值，直接写入 SQLite"""
    target_dates = target_dates or _get_trade_dates(20)
    schemes = schemes or ["sw", "ths"]

    db = get_db()
    active_codes = get_active_codes()

    for scheme in schemes:
        print(f"[{scheme}] 市值...")
        ind_map = _load_ind_map(scheme)
        codes = [c for c in active_codes if c in ind_map]
        shares = _load_shares(codes)

        cache = KlineCache(force_refresh=False)
        all_data = cache.ensure(codes, target_dates[-1])
        # 检查中间日期是否有数据
        sample_codes = [c for c in list(all_data.keys())[:100]]
        if sample_codes:
            df = all_data.get(sample_codes[0])
            if df is not None and not df.empty:
                date_strs = set(df["date"].dt.strftime("%Y%m%d"))
                if not all(d in date_strs for d in target_dates):
                    print(f"[{scheme}] 缓存缺失中间日期，强制重建...")
                    cache = KlineCache(force_refresh=True)
                    all_data = cache.ensure(codes, target_dates[-1])

        target_set = set(target_dates)
        ind_mcap = {}
        ind_stocks = {}  # {ind: {date: [{code, name, close, mcap, change_pct}]}}
        name_map = {}
        try:
            df_names = ak.stock_info_a_code_name()
            name_map = dict(zip(df_names["code"].astype(str).str.zfill(6), df_names["name"]))
        except: pass

        for code, df in all_data.items():
            ind = ind_map.get(code)
            sh = shares.get(code)
            if not ind or df is None or df.empty or not sh:
                continue
            date_strs = df["date"].dt.strftime("%Y%m%d")
            subset = df[date_strs.isin(target_set)]
            if subset.empty:
                continue
            # Build close map for change_pct
            close_map = {}
            for _, r2 in df.iterrows():
                close_map[r2["date"].strftime("%Y%m%d")] = float(r2["close"])
            for _, row in subset.iterrows():
                ds = row["date"].strftime("%Y%m%d")
                mcap = float(row["close"]) * sh
                close_val = float(row["close"])
                # Compute change_pct
                prev_dates = sorted([d for d in close_map if d < ds])
                prev_close = close_map.get(prev_dates[-1], 0) if prev_dates else 0
                chg = round((close_val - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

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
        if detail_records:
            db.insert_stock_details(detail_records)

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
            records.append({"date": ds, "scheme": scheme, "industry": "全市场合计",
                           "mcap": round(t), "stock_count": 0, "is_total": 1})

        db.insert_market_cap(records)
        print(f"  {len(records)} 条记录")

    return True


# ==================== 一键更新 ====================

def run_all(datasets=None, days=20, force_refresh=False):
    """运行全部更新并导出 JSON"""
    datasets = datasets or ["highs", "lows", "capital_flow", "market_cap"]
    target_dates = _get_trade_dates(max(days, 20))
    print(f"目标日期: {len(target_dates)}天 ({target_dates[0]} ~ {target_dates[-1]})")

    t0 = time.time()

    if "highs" in datasets or "lows" in datasets:
        result = update_highs_lows(target_dates, force_refresh=force_refresh)
        print(f"新高/新低完成: {sum(len(v) for v in result.values())} 条")

    if "capital_flow" in datasets:
        update_capital_flow(target_dates)
        print("资金流完成")

    if "market_cap" in datasets:
        update_market_cap(target_dates)
        print("市值完成")

    # 导出 JSON
    get_db().export_to_json()
    print(f"\n✅ 全部完成 ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=20)
    p.add_argument("--force-refresh", action="store_true")
    args = p.parse_args()
    run_all(days=args.days, force_refresh=args.force_refresh)
