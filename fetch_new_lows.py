"""
行业创新低数据获取脚本 (v3 - 共享 K 线缓存版)
- 使用 kline_cache 复用 K 线数据，避免每次全量下载
- 支持真正的一年 / alltime 新低计算

使用:
  python fetch_new_lows.py --type month
  python fetch_new_lows.py --type all
"""

import argparse
import json
import os
import sys
import tempfile
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


def _atomic_json_dump(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, "r", encoding="utf-8") as f:
            json.load(f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# =========================================================================
# 1. 从 K 线缓存计算每日创新低个股
# =========================================================================


def find_new_lows(all_data, target_dates, window_days, alltime_low_before=None):
    """找出每个目标日期创N日新低的股票"""
    results = {ds: [] for ds in target_dates}

    for code, df in all_data.items():
        if df is None or df.empty or len(df) < 2:
            continue
        closes = df["close"].values
        dates = df["date"]
        ab = alltime_low_before.get(code) if alltime_low_before else None

        if window_days is None:
            running_min = np.minimum.accumulate(closes)
            is_new_low = np.zeros(len(closes), dtype=bool)
            for j in range(1, len(closes)):
                # 融入 alltime_low_before 边界
                if ab is not None:
                    prev_min = min(ab, running_min[j - 1])
                else:
                    prev_min = running_min[j - 1]
                if ab is not None:
                    prev_min = min(ab, prev_min)
                if closes[j] < prev_min:
                    is_new_low[j] = True
        else:
            if len(closes) < window_days + 1:
                continue
            is_new_low = np.zeros(len(closes), dtype=bool)
            for j in range(window_days, len(closes)):
                prev_min = np.min(closes[j - window_days : j])
                if closes[j] < prev_min:
                    is_new_low[j] = True

        for j in range(len(is_new_low)):
            if not is_new_low[j]:
                continue
            ds = pd.Timestamp(dates.iloc[j]).strftime("%Y%m%d")
            if ds in target_dates:
                price = float(closes[j])
                if j > 0 and closes[j - 1]:
                    change_pct = round((price - float(closes[j - 1])) / float(closes[j - 1]) * 100, 2)
                else:
                    change_pct = 0
                results[ds].append({"code": code, "price": price, "change_pct": change_pct})

    return results


def enrich_with_names(stocks_by_date):
    all_codes = set()
    for stocks in stocks_by_date.values():
        for s in stocks:
            all_codes.add(s["code"])
    print("[3/5] 获取股票名称...")
    df = ak.stock_info_a_code_name()
    name_map = dict(zip(df["code"].astype(str).str.zfill(6), df["name"]))
    for ds, stocks in stocks_by_date.items():
        for s in stocks:
            s["name"] = name_map.get(s["code"], s["code"])
    return stocks_by_date


# =========================================================================
# 2. 关联行业、聚合统计
# =========================================================================


def aggregate(daily_stocks, industry_map, industry_scheme="sw"):
    print("[4/5] 关联行业并聚合...")
    date_list = sorted(daily_stocks.keys(), reverse=True)

    for date_str, stocks in daily_stocks.items():
        for s in stocks:
            s["industry"] = industry_map.get(s["code"], "其他")

    industry_total_map = {}
    for code, ind in industry_map.items():
        industry_total_map[ind] = industry_total_map.get(ind, 0) + 1

    main_industries = [ind for ind in SW2021_INDUSTRY_MAP.values() if ind != "综合"]
    if industry_scheme == "ths":
        ths_industries = sorted(set(industry_map.values()), key=lambda x: -sum(1 for v in industry_map.values() if v == x))
        main_industries = [ind for ind in ths_industries if ind != "综合"]
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

    dates_info = [{"label": format_date_short(ds), "full_label": format_date_for_query(ds)}
                  for ds in date_list]

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


def run_type(args, dates, active_codes, industry_map, all_data, alltime_low_before, industry_scheme="sw"):
    """运行单个类型的数据处理"""
    window_map = {
        "month": 20, "60d": 60, "120d": 120, "1year": 250, "alltime": None,
    }
    type_labels = {
        "month": "创20日新低", "60d": "创60日新低", "120d": "创120日新低",
        "1year": "创一年新低", "alltime": "收盘创近7年新低",
    }
    window = window_map[args.type]
    target_dates = set(dates)
    scheme_suffix = "_ths" if industry_scheme == "ths" else ""

    print(f"\n{'='*60}")
    print(f"[3/5] 计算{type_labels[args.type]}... (窗口: {window or 'alltime'})")
    daily_stocks = find_new_lows(all_data, target_dates, window, alltime_low_before)
    daily_stocks = enrich_with_names(daily_stocks)

    for ds in sorted(daily_stocks.keys(), reverse=True):
        print(f"  {ds}: {len(daily_stocks[ds])} 只")

    output = aggregate(daily_stocks, industry_map, industry_scheme)

    data_file = os.path.join(args.output_dir, f"new_lows_data_{args.type}{scheme_suffix}.json")
    details_file = os.path.join(args.output_dir, f"new_lows_details_{args.type}{scheme_suffix}.json")

    counts_output = {
        "dates": output["dates"],
        "updated_at": output["updated_at"],
        "type": args.type,
        "type_label": type_labels.get(args.type, args.type),
        "industries": [],
    }
    for row in output["industries"]:
        item = {"industry": row["industry"], "total": row["total"], "daily_counts": row["daily_counts"]}
        if row.get("is_total"):
            item["is_total"] = True
        else:
            item["ratio"] = row["ratio"]
        counts_output["industries"].append(item)

    _atomic_json_dump(counts_output, data_file)
    print(f"[5/5] counts 已保存: {data_file}")

    details_output = {}
    for row in output["industries"]:
        details_output[row["industry"]] = row["daily_details"]
    _atomic_json_dump(details_output, details_file)
    print(f"[5/5] details 已保存: {details_file}")

    print("\n=== 数据摘要 ===")
    for row in output["industries"][:5]:
        counts_str = " ".join(f"{c:4d}" for c in row["daily_counts"])
        ratio_str = f"{row.get('ratio', 0):5.1f}%" if not row.get("is_total") else "  —"
        print(f"  {row['industry']:6s} | {counts_str} | {row['total']:4d} | {ratio_str}")
    print(f"  ... ({len(output['industries'])} 行)")


def main():
    parser = argparse.ArgumentParser(description="获取行业创新低数据 (v3: 共享 K 线缓存)")
    parser.add_argument("--dates", type=str, default=None,
                        help="逗号分隔的日期 YYYYMMDD；默认取最近20个交易日")
    parser.add_argument("--type", type=str, default="month",
                        choices=["month", "60d", "120d", "1year", "alltime", "all"],
                        help="创新低类型")
    parser.add_argument("--output-dir", type=str,
                        default=os.path.join(os.path.dirname(__file__), "static"),
                        help="输出目录")
    parser.add_argument("--force-refresh", action="store_true",
                        help="强制重建 K 线缓存")
    parser.add_argument("--industry-scheme", type=str, default="sw", choices=["sw", "ths"],
                        help="行业分类: sw=申万2021, ths=同花顺")
    args = parser.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()] if args.dates else get_trade_dates(n=20)
    industry_scheme = args.industry_scheme

    active_codes = get_active_codes()
    if industry_scheme == "ths":
        ths_path = os.path.join(args.output_dir, "industry_map_ths.json")
        if not os.path.exists(ths_path):
            raise FileNotFoundError(f"缺少同花顺行业映射文件: {ths_path}")
        with open(ths_path, "r", encoding="utf-8") as f:
            ths_map = json.load(f)
        sw_map = load_industry_map(set(active_codes))
        active_set = set(active_codes)
        industry_map = {}
        for c in active_codes:
            if c in ths_map: industry_map[c] = ths_map[c]
            elif c in sw_map: industry_map[c] = sw_map[c]
            else: industry_map[c] = "其他"
    else:
        industry_map = load_industry_map(set(active_codes))
    codes_with_industry = [c for c in active_codes if c in industry_map]

    target_date = max(dates)
    print(f"[2/5] 加载/更新 K 线缓存 ({len(codes_with_industry)} 只)...")
    cache = KlineCache(force_refresh=args.force_refresh)
    all_data = cache.ensure(codes_with_industry, target_date)
    alltime_low_before = cache.alltime_low_before

    if args.type == "all":
        for t in ["month", "60d", "120d", "1year", "alltime"]:
            args.type = t
            run_type(args, dates, active_codes, industry_map, all_data, alltime_low_before, industry_scheme)
    else:
        run_type(args, dates, active_codes, industry_map, all_data, alltime_low_before, industry_scheme)


if __name__ == "__main__":
    main()
