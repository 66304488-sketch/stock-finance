"""
行业创新低数据获取脚本 (v2 - 缓存版)
- 从 baostock 下载全量日K线数据（首次慢，后续缓存复用）
- 自行计算各阶段新低
- pywencai 对"新低"查询的日期过滤有 bug，改用此方案

使用:
  python fetch_new_lows.py --type month          # 单类型
  python fetch_new_lows.py --type all            # 全部5个类型（推荐首次运行）
"""

import json
import os
import pickle
import sys
import time
import requests
import pandas as pd
import numpy as np
import io
import warnings
import akshare as ak
import baostock as bs

warnings.filterwarnings("ignore")

SW2021_INDUSTRY_MAP = {
    "11": "农林牧渔", "22": "基础化工", "23": "钢铁", "24": "有色金属",
    "27": "电子", "28": "汽车", "33": "家用电器", "34": "食品饮料",
    "35": "纺织服饰", "36": "轻工制造", "37": "医药生物", "41": "公用事业",
    "42": "交通运输", "43": "房地产", "45": "商贸零售", "46": "社会服务",
    "48": "银行", "49": "非银金融", "51": "综合", "61": "建筑材料",
    "62": "建筑装饰", "63": "电力设备", "64": "机械设备", "65": "国防军工",
    "71": "计算机", "72": "传媒", "73": "通信", "74": "煤炭",
    "75": "石油石化", "76": "环保", "77": "美容护理",
}

LOOKBACK_DAYS = 2500
CACHE_FILE = os.path.join(os.path.dirname(__file__), "static", "all_klines.pkl")


def get_active_stock_codes():
    print(f"[0/5] 获取当前上市A股列表...")
    df = ak.stock_info_a_code_name()
    codes = [c for c in df["code"].astype(str).str.zfill(6) if not c.startswith("9")]
    print(f"  当前上市A股: {len(codes)} 只 (不含北交所)")
    return codes


def load_sw_classification(active_codes=None):
    """从本地 Excel 文件加载行业分类（避免依赖外部网站）"""
    print(f"[1/5] 加载行业分类...")
    xlsx_path = os.path.join(os.path.dirname(__file__), "static", "industry_stock_map.xlsx")
    if os.path.exists(xlsx_path):
        df = pd.read_excel(xlsx_path, sheet_name="个股行业映射")
        df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
        if active_codes:
            before = len(df)
            df = df[df["股票代码"].isin(active_codes)]
            print(f"  过滤退市股: {before} → {len(df)}")
        mapping = dict(zip(df["股票代码"], df["行业名称"]))
        mapping = {k: (v if pd.notna(v) else "其他") for k, v in mapping.items()}
        print(f"  已加载 {len(mapping)} 只个股的行业分类 (本地缓存)")
        return mapping

    # fallback: 在线下载
    url = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
    print(f"  本地缓存不存在，从申万官网下载...")
    r = requests.get(url, verify=False, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    df = pd.read_excel(io.BytesIO(r.content))
    df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
    df["一级代码"] = df["行业代码"].astype(str).str[:2]
    df_sorted = df.sort_values("更新日期", ascending=False)
    df_latest = df_sorted.groupby("股票代码").first().reset_index()
    if active_codes:
        before = len(df_latest)
        df_latest = df_latest[df_latest["股票代码"].isin(active_codes)]
        print(f"  过滤退市股: {before} → {len(df_latest)}")
    df_latest["行业名称"] = df_latest["一级代码"].map(SW2021_INDUSTRY_MAP)
    mapping = dict(zip(df_latest["股票代码"], df_latest["行业名称"]))
    mapping = {k: (v if pd.notna(v) else "其他") for k, v in mapping.items()}
    print(f"  已加载 {len(mapping)} 只个股的行业分类")
    return mapping


def format_date_for_query(date_str):
    y, m, d = date_str[:4], str(int(date_str[4:6])), str(int(date_str[6:8]))
    return f"{y}年{m}月{d}日"


def format_date_short(date_str):
    return f"{str(int(date_str[4:6]))}月{str(int(date_str[6:8]))}日"


def download_klines_cached(codes, target_dates, force_refresh=False):
    """下载K线数据（带缓存）"""
    if not force_refresh and os.path.exists(CACHE_FILE):
        print(f"[2/5] 从缓存加载K线数据: {CACHE_FILE}")
        with open(CACHE_FILE, "rb") as f:
            cache = pickle.load(f)
        cached_codes = set(cache["codes"])
        new_codes = [c for c in codes if c not in cached_codes]
        if new_codes:
            print(f"  新增 {len(new_codes)} 只股票，补充下载...")
            new_data = download_klines_raw(new_codes, target_dates)
            cache["data"].update(new_data)
            cache["codes"] = codes
            with open(CACHE_FILE, "wb") as f:
                pickle.dump(cache, f)
        return cache["data"]

    print(f"[2/5] 下载日K线数据 (共 {len(codes)} 只股票, 首次较慢, 后续缓存)...")
    data = download_klines_raw(codes, target_dates)
    cache = {"codes": codes, "data": data}
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(cache, f)
    print(f"  已缓存到: {CACHE_FILE}")
    return data


def download_klines_raw(codes, target_dates):
    """实际下载K线数据"""
    all_dates = sorted(target_dates)
    earliest = all_dates[0]
    latest = all_dates[-1]
    start_date = pd.Timestamp(earliest) - pd.Timedelta(days=LOOKBACK_DAYS)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"

    bs.login()
    all_data = {}
    failed = 0
    total = len(codes)
    t0 = time.time()

    for i, code in enumerate(codes):
        if (i + 1) % 200 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"  {i+1}/{total} ({rate:.0f} stk/s, ETA {eta:.0f}s, 失败: {failed})")
        market = "sz" if code.startswith(("0", "3")) else "sh"
        symbol = f"{market}.{code}"
        try:
            rs = bs.query_history_k_data_plus(
                symbol, "date,close",
                start_date=start_str, end_date=end_str,
                frequency="d", adjustflag="2"
            )
            if rs.error_code != "0":
                failed += 1
                continue
            rows = []
            while rs.next():
                row_data = rs.get_row_data()
                if row_data and len(row_data) >= 2 and row_data[1]:
                    try:
                        rows.append({"date": row_data[0], "close": float(row_data[1])})
                    except (ValueError, TypeError):
                        pass
            if rows:
                df = pd.DataFrame(rows)
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date")
                all_data[code] = df
        except Exception:
            failed += 1
    bs.logout()
    elapsed = time.time() - t0
    print(f"  完成: {len(all_data)} 只 ({elapsed:.0f}s, 失败: {failed})")
    return all_data


def find_new_lows(all_data, target_dates, window_days):
    """找出每个目标日期创N日新低的股票"""
    results = {ds: [] for ds in target_dates}

    for code, df in all_data.items():
        if df.empty or len(df) < 2:
            continue
        closes = df["close"].values
        dates = df["date"]

        if window_days is None:
            hist_min = np.minimum.accumulate(closes)
            is_new_low = np.zeros(len(closes), dtype=bool)
            for j in range(1, len(closes)):
                if closes[j] < hist_min[j-1]:
                    is_new_low[j] = True
        else:
            if len(closes) < window_days + 1:
                continue
            is_new_low = np.zeros(len(closes), dtype=bool)
            for j in range(window_days, len(closes)):
                prev_min = np.min(closes[j-window_days:j])
                if closes[j] < prev_min:
                    is_new_low[j] = True

        for j in range(len(is_new_low)):
            if not is_new_low[j]:
                continue
            ds = pd.Timestamp(dates.iloc[j]).strftime("%Y%m%d")
            if ds in target_dates:
                price = float(closes[j])
                if j > 0 and closes[j-1]:
                    change_pct = round((price - float(closes[j-1])) / float(closes[j-1]) * 100, 2)
                else:
                    change_pct = 0
                results[ds].append({"code": code, "price": price, "change_pct": change_pct})

    return results


def enrich_with_names(stocks_by_date):
    all_codes = set()
    for stocks in stocks_by_date.values():
        for s in stocks:
            all_codes.add(s["code"])
    print(f"[3/5] 获取股票名称...")
    df = ak.stock_info_a_code_name()
    name_map = dict(zip(df["code"].astype(str).str.zfill(6), df["name"]))
    for ds, stocks in stocks_by_date.items():
        for s in stocks:
            s["name"] = name_map.get(s["code"], s["code"])
    return stocks_by_date


def aggregate(daily_stocks, industry_map):
    print("[4/5] 关联行业并聚合...")
    date_list = sorted(daily_stocks.keys(), reverse=True)

    for date_str, stocks in daily_stocks.items():
        for s in stocks:
            s["industry"] = industry_map.get(s["code"], "其他")

    industry_total_map = {}
    for code, ind in industry_map.items():
        industry_total_map[ind] = industry_total_map.get(ind, 0) + 1

    main_industries = [ind for ind in SW2021_INDUSTRY_MAP.values() if ind != "综合"]

    result = []
    for ind in main_industries:
        total_stocks = industry_total_map.get(ind, 0)
        row = {"industry": ind, "total": total_stocks, "ratio": 0.0,
               "daily_counts": [], "daily_details": {}}
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
        "industry": "全市场合计", "total": all_total, "ratio": 0,
        "daily_counts": totals, "daily_details": total_details, "is_total": True,
    })

    dates_info = [{"label": format_date_short(ds), "full_label": format_date_for_query(ds)}
                  for ds in date_list]

    output = {"dates": dates_info, "industries": result,
              "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}
    print(f"  共 {len(main_industries)} 个行业, {len(date_list)} 个交易日")
    return output


def run_type(args, dates, active_codes, industry_map, all_data):
    """运行单个类型的数据处理"""
    window_map = {
        "month": 20, "60d": 60, "120d": 120, "1year": 250, "alltime": None,
    }
    type_labels = {
        "month": "创20日新低", "60d": "创60日新低", "120d": "创120日新低",
        "1year": "创一年新低", "alltime": "收盘创历史新低",
    }
    window = window_map[args.type]
    target_dates = set(dates)

    print(f"\n{'='*60}")
    print(f"[3/5] 计算{type_labels[args.type]}... (窗口: {window or 'alltime'})")
    daily_stocks = find_new_lows(all_data, target_dates, window)
    daily_stocks = enrich_with_names(daily_stocks)

    for ds in sorted(daily_stocks.keys(), reverse=True):
        print(f"  {ds}: {len(daily_stocks[ds])} 只")

    output = aggregate(daily_stocks, industry_map)

    data_file = os.path.join(args.output_dir, f"new_lows_data_{args.type}.json")
    details_file = os.path.join(args.output_dir, f"new_lows_details_{args.type}.json")

    counts_output = {
        "dates": output["dates"], "updated_at": output["updated_at"],
        "type": args.type, "type_label": type_labels.get(args.type, args.type),
        "industries": [],
    }
    for row in output["industries"]:
        item = {"industry": row["industry"], "total": row["total"], "daily_counts": row["daily_counts"]}
        if row.get("is_total"):
            item["is_total"] = True
        else:
            item["ratio"] = row["ratio"]
        counts_output["industries"].append(item)

    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(counts_output, f, ensure_ascii=False, indent=2)
    print(f"[5/5] counts 已保存: {data_file}")

    details_output = {}
    for row in output["industries"]:
        details_output[row["industry"]] = row["daily_details"]
    with open(details_file, "w", encoding="utf-8") as f:
        json.dump(details_output, f, ensure_ascii=False, indent=2)
    print(f"[5/5] details 已保存: {details_file}")

    print("\n=== 数据摘要 ===")
    for row in output["industries"][:5]:
        counts_str = " ".join(f"{c:4d}" for c in row["daily_counts"])
        ratio_str = f"{row.get('ratio', 0):5.1f}%" if not row.get("is_total") else "  —"
        print(f"  {row['industry']:6s} | {counts_str} | {row['total']:4d} | {ratio_str}")
    print(f"  ... ({len(output['industries'])} 行)")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="获取行业创新低数据 (v2: baostock K线)")
    parser.add_argument("--dates", type=str,
                        default="20260526,20260525,20260522,20260521,20260520,20260519,20260515,20260514,20260513,20260512",
                        help="逗号分隔的日期 YYYYMMDD")
    parser.add_argument("--type", type=str, default="month",
                        choices=["month", "60d", "120d", "1year", "alltime", "all"],
                        help="创新低类型")
    parser.add_argument("--output-dir", type=str,
                        default=os.path.join(os.path.dirname(__file__), "static"),
                        help="输出目录")
    parser.add_argument("--force-refresh", action="store_true",
                        help="强制重新下载K线数据（忽略缓存）")
    args = parser.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]

    # 获取股票列表 & 行业分类
    active_codes = get_active_stock_codes()
    industry_map = load_sw_classification(set(active_codes))
    codes_with_industry = [c for c in active_codes if c in industry_map]

    # 下载/加载K线缓存
    all_data = download_klines_cached(codes_with_industry, dates, force_refresh=args.force_refresh)

    if args.type == "all":
        for t in ["month", "60d", "120d", "1year", "alltime"]:
            args.type = t
            run_type(args, dates, active_codes, industry_map, all_data)
    else:
        run_type(args, dates, active_codes, industry_map, all_data)


if __name__ == "__main__":
    main()
