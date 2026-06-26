"""
行业创新高数据获取脚本 (v3 - 共享 K 线缓存版)
- 使用 kline_cache 复用 K 线数据，避免每次全量下载
- 支持真正的一年 / alltime 新高计算

使用: python fetch_new_highs.py [--dates 20250508,20250511,...] [--type all]
"""

import json
import os
import sys
import argparse
import warnings

import akshare as ak
import numpy as np
import pandas as pd

from kline_cache import (
    KlineCache,
    SW2021_INDUSTRY_MAP,
    format_date_for_query,
    format_date_short,
    get_active_codes,
    get_trade_dates,
    load_industry_map,
)

warnings.filterwarnings("ignore")


# =========================================================================
# 1. 从 K 线缓存中计算每日创新高个股
# =========================================================================


def find_new_highs(all_data, target_dates, window_days, alltime_high_before=None):
    """从 K 线缓存中找出每个目标日期创 N 日新高的股票
    window_days: None 表示 alltime 新高，否则为窗口天数 (20/60/120/250)
    """
    target_set = set(target_dates)
    results = {ds: [] for ds in target_dates}

    for code, df in all_data.items():
        if df is None or df.empty or len(df) < 2:
            continue
        if code.startswith("9"):
            continue

        closes = df["close"].values
        date_strs = df["date"].dt.strftime("%Y%m%d").values
        ab = alltime_high_before.get(code) if alltime_high_before else None

        target_indices = np.where(np.isin(date_strs, list(target_set)))[0]
        for idx in target_indices:
            if window_days is None:
                if idx == 0:
                    continue
                prev_max_in_window = np.max(closes[:idx])
                if ab is not None:
                    prev_max = max(ab, prev_max_in_window)
                else:
                    prev_max = prev_max_in_window
            else:
                if idx < window_days:
                    continue
                prev_max = np.max(closes[idx - window_days : idx])

            if closes[idx] > prev_max:
                price = float(closes[idx])
                prev_close = float(closes[idx - 1]) if idx > 0 else price
                change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0
                results[date_strs[idx]].append({
                    "code": code,
                    "price": price,
                    "change_pct": change_pct,
                })

    return results


def enrich_names(daily_stocks):
    df = ak.stock_info_a_code_name()
    name_map = dict(zip(df["code"].astype(str).str.zfill(6), df["name"]))
    for ds, stocks in daily_stocks.items():
        for s in stocks:
            s["name"] = name_map.get(s["code"], s["code"])
    return daily_stocks


# =========================================================================
# 2. 关联行业、聚合统计
# =========================================================================


def aggregate(daily_stocks, industry_map):
    """关联行业分类，按 (行业, 日期) 聚合"""
    print("[3/4] 关联行业并聚合...")

    all_industries = set()
    date_list = sorted(daily_stocks.keys(), reverse=True)
    date_labels = [format_date_short(d) for d in date_list]

    for date_str, stocks in daily_stocks.items():
        for s in stocks:
            industry = industry_map.get(s["code"], "其他")
            s["industry"] = industry
            all_industries.add(industry)

    industry_total_map = {}
    for code, ind in industry_map.items():
        industry_total_map[ind] = industry_total_map.get(ind, 0) + 1

    main_industries = [ind for ind in SW2021_INDUSTRY_MAP.values() if ind != "综合"]
    main_industries.append("其他")

    result = []
    for ind in main_industries:
        total_stocks = industry_total_map.get(ind, 0)
        row = {
            "industry": ind,
            "total": total_stocks,
            "ratio": 0.0,
            "daily_counts": [],
            "daily_details": {},
        }
        for date_str in date_list:
            date_label = format_date_short(date_str)
            stocks_in_ind = [s for s in daily_stocks.get(date_str, []) if s["industry"] == ind]
            count = len(stocks_in_ind)
            row["daily_counts"].append(count)
            row["daily_details"][date_label] = [
                {"code": s["code"], "name": s["name"],
                 "price": s["price"], "change_pct": s["change_pct"]}
                for s in stocks_in_ind
            ]
        if total_stocks > 0 and row["daily_counts"]:
            row["ratio"] = round(row["daily_counts"][0] / total_stocks * 100, 1)
        result.append(row)

    result.sort(key=lambda r: r["daily_counts"][0] if r["daily_counts"] else 0, reverse=True)

    totals = []
    total_details = {}
    for i, date_str in enumerate(date_list):
        date_label = format_date_short(date_str)
        day_total = sum(row["daily_counts"][i] for row in result)
        totals.append(day_total)
        total_details[date_label] = []
        for s in daily_stocks.get(date_str, []):
            total_details[date_label].append({
                "code": s["code"], "name": s["name"],
                "price": s["price"], "change_pct": s["change_pct"],
                "industry": s["industry"],
            })

    all_total = sum(ind_total for ind, ind_total in industry_total_map.items()
                    if ind in main_industries)

    result.append({
        "industry": "全市场合计",
        "total": all_total,
        "ratio": 0,
        "daily_counts": totals,
        "daily_details": total_details,
        "is_total": True,
    })

    dates_info = []
    for date_str in date_list:
        dates_info.append({
            "label": format_date_short(date_str),
            "full_label": format_date_for_query(date_str),
        })

    output = {
        "dates": dates_info,
        "industries": result,
        "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"  共 {len(main_industries)} 个行业, {len(date_list)} 个交易日")
    return output


# =========================================================================
# 3. 主流程
# =========================================================================


def _run_single(args, dates, active_codes, industry_map, type_labels,
                all_data=None, name_map=None, alltime_high_before=None):
    """运行单个类型（被 main 和 --type all 复用）"""
    type_suffix = args.type
    data_file = os.path.join(args.output_dir, f"new_highs_data_{type_suffix}.json")
    details_file = os.path.join(args.output_dir, f"new_highs_details_{type_suffix}.json")

    window_map = {
        "month": 20, "60d": 60, "120d": 120, "1year": 250, "alltime": None,
    }
    window = window_map[args.type]

    # K 线数据（如果调用方已加载则复用）
    if all_data is None:
        codes_with_industry = [c for c in active_codes if c in industry_map]
        target_date = max(dates)
        print(f"[2/4] 加载/更新 K 线缓存 ({len(codes_with_industry)} 只)...")
        cache = KlineCache(force_refresh=getattr(args, "force_refresh", False))
        all_data = cache.ensure(codes_with_industry, target_date)
        alltime_high_before = cache.alltime_high_before

    daily_stocks = find_new_highs(all_data, set(dates), window, alltime_high_before)

    # 股票名称
    if name_map:
        for ds, stocks in daily_stocks.items():
            for s in stocks:
                s["name"] = name_map.get(s["code"], s["code"])
    else:
        daily_stocks = enrich_names(daily_stocks)

    # 聚合
    output = aggregate(daily_stocks, industry_map)

    os.makedirs(args.output_dir, exist_ok=True)

    # 轻量 counts 文件
    counts_output = {
        "dates": output["dates"],
        "updated_at": output["updated_at"],
        "type": args.type,
        "type_label": type_labels.get(args.type, args.type),
        "industries": [],
    }
    for row in output["industries"]:
        item = {
            "industry": row["industry"],
            "total": row["total"],
            "daily_counts": row["daily_counts"],
        }
        if row.get("is_total"):
            item["is_total"] = True
        else:
            item["ratio"] = row["ratio"]
        counts_output["industries"].append(item)

    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(counts_output, f, ensure_ascii=False, indent=2)
    print(f"[4/4] counts 已保存: {data_file}")

    # 明细文件
    details_output = {}
    for row in output["industries"]:
        details_output[row["industry"]] = row["daily_details"]

    with open(details_file, "w", encoding="utf-8") as f:
        json.dump(details_output, f, ensure_ascii=False, indent=2)
    print(f"[4/4] details 已保存: {details_file}")

    # 打印摘要
    print("\n=== 数据摘要 ===")
    for row in output["industries"]:
        counts_str = " ".join(f"{c:4d}" for c in row["daily_counts"])
        ratio_str = f"{row.get('ratio', 0):5.1f}%" if not row.get("is_total") else "  —"
        print(f"  {row['industry']:6s} | {counts_str} | {row['total']:4d} | {ratio_str}")


def main():
    parser = argparse.ArgumentParser(description="获取行业创新高数据")
    parser.add_argument(
        "--dates",
        type=str,
        default=None,
        help="逗号分隔的日期 YYYYMMDD；默认取最近20个交易日",
    )
    parser.add_argument(
        "--type",
        type=str,
        default="month",
        choices=["month", "60d", "120d", "1year", "alltime", "all"],
        help="创新高类型",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=os.path.join(os.path.dirname(__file__), "static"),
        help="输出目录",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="强制重建 K 线缓存",
    )
    args = parser.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()] if args.dates else get_trade_dates(n=20)

    type_labels = {
        "month": "创20日新高", "60d": "创60日新高", "120d": "创120日新高",
        "1year": "创一年新高", "alltime": "收盘创历史新高",
    }

    active_codes = get_active_codes()
    industry_map = load_industry_map(active_codes)

    if args.type == "all":
        codes_with_industry = [c for c in active_codes if c in industry_map]
        target_date = max(dates)
        print(f"加载 K 线缓存 ({len(codes_with_industry)} 只)...")
        cache = KlineCache(force_refresh=args.force_refresh)
        all_data = cache.ensure(codes_with_industry, target_date)
        df_names = ak.stock_info_a_code_name()
        name_map = dict(zip(df_names["code"].astype(str).str.zfill(6), df_names["name"]))
        alltime_high_before = cache.alltime_high_before
        for t in ["month", "60d", "120d", "1year", "alltime"]:
            print(f"\n{'='*60}\n处理类型: {type_labels.get(t, t)}\n{'='*60}")
            args.type = t
            _run_single(args, dates, active_codes, industry_map, type_labels,
                        all_data, name_map, alltime_high_before)
    else:
        _run_single(args, dates, active_codes, industry_map, type_labels)


if __name__ == "__main__":
    main()
