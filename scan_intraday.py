#!/usr/bin/env python3
"""盘中实时扫描：用 Sina 实时行情对比历史 K 线，检测盘中创新高/新低个股。

使用:
  python scan_intraday.py                     # 扫描所有窗口
  python scan_intraday.py --window 20d        # 只扫 20 日
  python scan_intraday.py --output /tmp/out   # 自定义输出目录

输出:
  static/intraday_highs_20d.json / intraday_lows_20d.json 等
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 确保可以导入同级模块
sys.path.insert(0, os.path.dirname(__file__))

from kline_cache import (
    KlineCache,
    fetch_spot,
    get_active_codes,
    load_industry_map,
)

STATIC = os.path.join(os.path.dirname(__file__), "static")

WINDOWS = {
    "20d": 20,
    "60d": 60,
    "120d": 120,
    "1year": 250,
}

TYPE_LABELS = {
    "20d": ("创20日新高", "创20日新低"),
    "60d": ("创60日新高", "创60日新低"),
    "120d": ("创120日新高", "创120日新低"),
    "1year": ("创一年新高", "创一年新低"),
}


def scan(active_codes: list[str], industry_map: dict[str, str], cache: KlineCache, window_days: int) -> dict[str, Any]:
    """扫描盘中新高/新低。"""
    today_str = datetime.now().strftime("%Y%m%d")
    date_label = datetime.now().strftime("%m月%d日")

    print(f"  拉取 {len(active_codes)} 只个股实时行情...")
    t0 = time.time()
    spot_data = fetch_spot(active_codes, today_str)
    print(f"  获取到 {len(spot_data)} 只 (耗时 {time.time()-t0:.1f}s)")

    if not spot_data:
        print("  ⚠️ 未获取到实时行情（非交易时间或网络问题）")
        return {"date_label": date_label, "industries": [], "total": 0, "error": "no_spot_data"}

    print(f"  加载 {window_days} 日历史高/低点...")
    t0 = time.time()
    ohlcv = cache.ensure(active_codes, today_str, need_ohlcv=True)
    print(f"  K线数据 {len(ohlcv)} 只 (耗时 {time.time()-t0:.1f}s)")

    # 按行业聚合
    industry_highs: dict[str, list[dict]] = defaultdict(list)
    industry_lows: dict[str, list[dict]] = defaultdict(list)

    for code in active_codes:
        df = ohlcv.get(code)
        if df is None or len(df) < max(window_days + 1, 21):
            continue
        spot = spot_data.get(code)
        if not spot or not spot.get("close"):
            continue

        current = spot["close"]  # intraday current/latest price
        industry = industry_map.get(code, "其他")

        # 前 window_days 天（不含今天）的最高价和最低价
        if len(df) > window_days:
            hist_high = float(df["high"].iloc[-(window_days + 1):-1].max())
            hist_low = float(df["low"].iloc[-(window_days + 1):-1].min())
        else:
            hist_high = float(df["high"].iloc[:-1].max())
            hist_low = float(df["low"].iloc[:-1].min())

        entry = {
            "code": code,
            "name": spot.get("name", ""),
            "price": round(float(current), 2),
            "change_pct": round(float(spot.get("change_pct", 0)), 2),
            "prev_close": round(float(spot.get("prev_close", 0)), 2),
            "hist_high": round(hist_high, 2),
            "hist_low": round(hist_low, 2),
        }

        if current > hist_high:
            entry["break_pct"] = round((current / hist_high - 1) * 100, 2)
            industry_highs[industry].append(entry)
        elif current < hist_low:
            entry["break_pct"] = round((current / hist_low - 1) * 100, 2)
            industry_lows[industry].append(entry)

    return {
        "date_label": date_label,
        "scan_time": datetime.now().isoformat(),
        "industry_highs": dict(industry_highs),
        "industry_lows": dict(industry_lows),
    }


def build_output(scanned: dict[str, Any], window_key: str, window_days: int) -> tuple[dict, dict]:
    """生成与现有 heatmap JSON 兼容格式的输出。"""
    high_label, low_label = TYPE_LABELS.get(window_key, TYPE_LABELS["20d"])

    date_label = scanned["date_label"]
    scan_time = scanned["scan_time"]

    ind_highs = scanned.get("industry_highs", {})
    ind_lows = scanned.get("industry_lows", {})

    all_industries = sorted(set(list(ind_highs.keys()) + list(ind_lows.keys())))

    # 统计行业成分股数量（直接从 industry_map 算）
    ind_total_map: dict[str, int] = {}
    # 从现有的 industry_map 反推成分股数
    for code in get_active_codes():
        ind = industry_map.get(code, "其他")
        ind_total_map[ind] = ind_total_map.get(ind, 0) + 1

    # 新高输出
    highs_rows = []
    total_highs = 0
    for ind in sorted(all_industries):
        stocks = ind_highs.get(ind, [])
        total_highs += len(stocks)
        count = len(stocks)
        ind_total = ind_total_map.get(ind, 1)
        highs_rows.append({
            "industry": ind,
            "total": ind_total,
            "ratio": round(count / max(ind_total, 1) * 100, 1),
            "daily_counts": [count],
            "daily_details": {date_label: sorted(stocks, key=lambda s: -(s.get("break_pct") or 0))},
        })
    highs_rows.sort(key=lambda r: -r["daily_counts"][0])
    # 全市场合计
    all_total = sum(r["total"] for r in highs_rows)
    highs_rows.append({
        "industry": "全市场合计",
        "total": all_total,
        "daily_counts": [total_highs],
        "daily_details": {},
        "is_total": True,
    })

    highs_output = {
        "dates": [{"label": date_label, "full_label": f"{datetime.now().year}年{date_label}"}],
        "updated_at": scan_time,
        "type": f"intraday_{window_key}",
        "type_label": f"盘中{high_label}",
        "industries": highs_rows,
    }

    # 新低输出
    lows_rows = []
    total_lows = 0
    for ind in sorted(all_industries):
        stocks = ind_lows.get(ind, [])
        total_lows += len(stocks)
        count = len(stocks)
        ind_total = ind_total_map.get(ind, 1)
        lows_rows.append({
            "industry": ind,
            "total": ind_total,
            "ratio": round(count / max(ind_total, 1) * 100, 1),
            "daily_counts": [count],
            "daily_details": {date_label: sorted(stocks, key=lambda s: (s.get("break_pct") or 0))},
        })
    lows_rows.sort(key=lambda r: -r["daily_counts"][0])
    lows_rows.append({
        "industry": "全市场合计",
        "total": all_total,
        "daily_counts": [total_lows],
        "daily_details": {},
        "is_total": True,
    })

    lows_output = {
        "dates": [{"label": date_label, "full_label": f"{datetime.now().year}年{date_label}"}],
        "updated_at": scan_time,
        "type": f"intraday_{window_key}",
        "type_label": f"盘中{low_label}",
        "industries": lows_rows,
    }

    return highs_output, lows_output


def main():
    parser = argparse.ArgumentParser(description="盘中新高/新低实时扫描")
    parser.add_argument("--window", default="all", help="扫描窗口: 20d, 60d, 120d, 1year, all")
    parser.add_argument("--output", default=STATIC, help="输出目录")
    args = parser.parse_args()

    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)

    windows_to_scan = list(WINDOWS.items()) if args.window == "all" else [
        (args.window, WINDOWS.get(args.window, 20))
    ]

    print(f"盘中扫描开始 {datetime.now().strftime('%H:%M:%S')}")
    print(f"窗口: {[w[0] for w in windows_to_scan]}")

    # 获取股票列表和行业映射
    print("获取A股列表...")
    active_codes = get_active_codes()
    print(f"共 {len(active_codes)} 只")

    global industry_map
    industry_map = load_industry_map(active_codes)

    cache = KlineCache()

    for window_key, window_days in windows_to_scan:
        print(f"\n--- 扫描 {window_key} ({window_days}日) ---")
        try:
            scanned = scan(active_codes, industry_map, cache, window_days)
            highs, lows = build_output(scanned, window_key, window_days)
        except Exception as e:
            print(f"  ✗ 扫描失败: {e}")
            continue

        high_path = os.path.join(output_dir, f"intraday_highs_{window_key}.json")
        low_path = os.path.join(output_dir, f"intraday_lows_{window_key}.json")

        with open(high_path, "w", encoding="utf-8") as f:
            json.dump(highs, f, ensure_ascii=False, indent=2)
        with open(low_path, "w", encoding="utf-8") as f:
            json.dump(lows, f, ensure_ascii=False, indent=2)

        total_h = sum(r["daily_counts"][0] for r in highs["industries"] if not r.get("is_total"))
        total_l = sum(r["daily_counts"][0] for r in lows["industries"] if not r.get("is_total"))
        print(f"  盘中新高: {total_h} 只 → {high_path}")
        print(f"  盘中新低: {total_l} 只 → {low_path}")

    print(f"\n盘中扫描完成 {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()
