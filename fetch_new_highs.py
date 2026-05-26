"""
行业创新高数据获取脚本
- 从申万官网下载行业分类 (SW2021)
- 通过 pywencai 获取每日创一年新高个股
- 关联行业、聚合统计
- 输出 JSON 供前端使用

使用: python fetch_new_highs.py [--dates 20250508,20250511,...]
"""

import json
import os
import sys
import time
import requests
import pandas as pd
import io
import warnings
import akshare as ak
import baostock as bs

warnings.filterwarnings("ignore")

# =========================================================================
# 1. 申万 2021 行业分类
# =========================================================================

SW2021_INDUSTRY_MAP = {
    "11": "农林牧渔",
    "22": "基础化工",
    "23": "钢铁",
    "24": "有色金属",
    "27": "电子",
    "28": "汽车",
    "33": "家用电器",
    "34": "食品饮料",
    "35": "纺织服饰",
    "36": "轻工制造",
    "37": "医药生物",
    "41": "公用事业",
    "42": "交通运输",
    "43": "房地产",
    "45": "商贸零售",
    "46": "社会服务",
    "48": "银行",
    "49": "非银金融",
    "51": "综合",
    "61": "建筑材料",
    "62": "建筑装饰",
    "63": "电力设备",
    "64": "机械设备",
    "65": "国防军工",
    "71": "计算机",
    "72": "传媒",
    "73": "通信",
    "74": "煤炭",
    "75": "石油石化",
    "76": "环保",
    "77": "美容护理",
}


def get_active_stock_codes():
    """通过 akshare 获取当前上市A股代码列表（排除北交所 9 开头）"""
    print(f"[0/4] 获取当前上市A股列表...")
    df = ak.stock_info_a_code_name()
    codes = set(df["code"].astype(str).str.zfill(6))
    # 排除北交所 (9开头)
    codes = {c for c in codes if not c.startswith("9")}
    print(f"  当前上市A股: {len(codes)} 只 (不含北交所)")
    return codes


def load_sw_classification(active_codes=None):
    """下载申万2021行业分类并构建 stock_code → industry_name 映射
    若提供 active_codes，则只保留当前上市股票"""
    url = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
    print(f"[1/4] 下载申万行业分类...")
    r = requests.get(url, verify=False, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    df = pd.read_excel(io.BytesIO(r.content))

    df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
    df["一级代码"] = df["行业代码"].astype(str).str[:2]

    # 按更新日期取每个股票的最新分类
    df_sorted = df.sort_values("更新日期", ascending=False)
    df_latest = df_sorted.groupby("股票代码").first().reset_index()

    # 过滤：只保留当前上市股票
    if active_codes:
        before = len(df_latest)
        df_latest = df_latest[df_latest["股票代码"].isin(active_codes)]
        print(f"  过滤退市股: {before} → {len(df_latest)}")

    # 映射一级代码 → 行业名称
    df_latest["行业名称"] = df_latest["一级代码"].map(SW2021_INDUSTRY_MAP)
    unmapped = df_latest[df_latest["行业名称"].isna()]["一级代码"].unique()
    if len(unmapped) > 0:
        print(f"  警告: 未映射的行业代码: {unmapped}")

    mapping = dict(zip(df_latest["股票代码"], df_latest["行业名称"]))
    # 清除 NaN 值（未映射行业的股票）
    mapping = {k: (v if pd.notna(v) else "其他") for k, v in mapping.items()}
    print(f"  已加载 {len(mapping)} 只个股的行业分类")
    return mapping


# =========================================================================
# 2. 获取每日创新高个股 (pywencai)
# =========================================================================


def fetch_daily_new_highs(dates, query_type="month"):
    """通过 pywencai 查询每个日期创新高的个股
    query_type: "month" (创月新高/20日), "alltime" (收盘创历史新高，后复权比较)"""
    import pywencai as wc

    query_map = {
        "month": "创月新高",
        "60d": "创60日新高",
        "120d": "创120日新高",
        "1year": "创一年新高",
        "alltime": "创历史新高",
    }
    keyword = query_map.get(query_type, "创月新高")

    daily_stocks = {}
    for i, date_str in enumerate(dates):
        query = f"{format_date_for_query(date_str)} {keyword} 股票"
        print(f"[2/4] 查询 {date_str} ({i+1}/{len(dates)})...", end=" ", flush=True)
        try:
            df = wc.get(query=query, loop=True)
            if isinstance(df, pd.DataFrame) and len(df) > 0:
                stocks = []
                filtered_out = 0

                # 历史新高：需要比较后复权收盘价 vs 后复权历史最高价
                if query_type == "alltime":
                    # 收集需要验证的股票
                    candidates = []
                    for _, row in df.iterrows():
                        code = str(row.get("code", row.get("股票代码", ""))).strip()
                        if len(code) < 6:
                            code = code.zfill(6)
                        hist_high = row.get("历史最高价后复权")
                        if hist_high is not None and pd.notna(hist_high):
                            candidates.append((code, row, float(hist_high)))

                    # 批量获取后复权收盘价
                    hfq_closes = _get_hfq_closes([c[0] for c in candidates], date_str)

                    for code, row, hist_high in candidates:
                        if code.startswith("9"):
                            filtered_out += 1
                            continue
                        price = float(row.get("最新价", 0)) if pd.notna(row.get("最新价", 0)) else 0
                        change_pct = round(float(row.get("最新涨跌幅", 0)), 2) if pd.notna(row.get("最新涨跌幅", 0)) else 0

                        hfq_close = hfq_closes.get(code)
                        if hfq_close is None:
                            filtered_out += 1
                            continue
                        if hfq_close < hist_high:
                            filtered_out += 1
                            continue

                        stocks.append({
                            "code": code,
                            "name": str(row.get("股票简称", "")),
                            "price": price,
                            "change_pct": change_pct,
                        })
                    print(f"{len(stocks)} 只 (过滤 {filtered_out} 只)", end="")
                else:
                    for _, row in df.iterrows():
                        code = str(row.get("code", row.get("股票代码", ""))).strip()
                        if len(code) < 6:
                            code = code.zfill(6)
                        if code.startswith("9"):
                            continue
                        price = float(row.get("最新价", 0)) if pd.notna(row.get("最新价", 0)) else 0
                        change_pct = round(float(row.get("最新涨跌幅", 0)), 2) if pd.notna(row.get("最新涨跌幅", 0)) else 0
                        stocks.append({
                            "code": code,
                            "name": str(row.get("股票简称", "")),
                            "price": price,
                            "change_pct": change_pct,
                        })
                    print(f"{len(stocks)} 只", end="")

                daily_stocks[date_str] = stocks
                print()
            else:
                daily_stocks[date_str] = []
                print("0 只")
        except Exception as e:
            print(f"错误: {e}")
            daily_stocks[date_str] = []
        # 避免请求过快
        if i < len(dates) - 1:
            time.sleep(1)
    return daily_stocks


def _get_hfq_closes(codes, date_str):
    """批量获取后复权收盘价，返回 {code: hfq_close}"""
    if not codes:
        return {}
    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    bs.login()
    result = {}
    try:
        for code in codes:
            market = "sz" if code.startswith(("0", "3")) else "sh"
            symbol = f"{market}.{code}"
            rs = bs.query_history_k_data_plus(
                symbol, "close",
                start_date=date_fmt, end_date=date_fmt,
                frequency="d", adjustflag="1"  # 1=后复权
            )
            if rs.error_code == "0":
                while rs.next():
                    row_data = rs.get_row_data()
                    if row_data and len(row_data) >= 1:
                        try:
                            result[code] = float(row_data[0])
                        except (ValueError, TypeError):
                            pass
    finally:
        bs.logout()
    return result


def format_date_for_query(date_str):
    """将 20250521 → 2025年5月21日"""
    y = date_str[:4]
    m = str(int(date_str[4:6]))
    d = str(int(date_str[6:8]))
    return f"{y}年{m}月{d}日"


# =========================================================================
# 3. 关联行业、聚合统计
# =========================================================================


def aggregate(daily_stocks, industry_map):
    """关联行业分类，按 (行业, 日期) 聚合"""
    print("[3/4] 关联行业并聚合...")

    # 获取所有出现的行业
    all_industries = set()
    date_list = sorted(daily_stocks.keys(), reverse=True)  # 最新在前
    date_labels = [format_date_short(d) for d in date_list]

    # stock → industry
    for date_str, stocks in daily_stocks.items():
        for s in stocks:
            industry = industry_map.get(s["code"], "其他")
            s["industry"] = industry
            all_industries.add(industry)

    # 行业个股总数（从申万分类统计）
    industry_total_map = {}
    for code, ind in industry_map.items():
        industry_total_map[ind] = industry_total_map.get(ind, 0) + 1

    # 排除"综合"和"其他"（非主要行业）
    main_industries = [
        ind for ind in SW2021_INDUSTRY_MAP.values() if ind != "综合"
    ]

    # 按行业聚合: 每日上涨家数 + 个股明细
    result = []
    for ind in main_industries:
        total_stocks = industry_total_map.get(ind, 0)
        row = {
            "industry": ind,
            "total": total_stocks,
            "ratio": 0.0,
            "daily_counts": [],
            "daily_details": {},  # date_label → [stock_codes]
        }
        for date_str in date_list:
            date_label = format_date_short(date_str)
            stocks_in_ind = [
                s for s in daily_stocks.get(date_str, []) if s["industry"] == ind
            ]
            count = len(stocks_in_ind)
            row["daily_counts"].append(count)
            row["daily_details"][date_label] = [
                {"code": s["code"], "name": s["name"],
                 "price": s["price"], "change_pct": s["change_pct"]}
                for s in stocks_in_ind
            ]
        # 比例 = 最新一日 创新高数 / 行业总数 * 100
        if total_stocks > 0 and row["daily_counts"]:
            row["ratio"] = round(row["daily_counts"][0] / total_stocks * 100, 1)
        result.append(row)

    # 按最新一日创新高数降序排列
    result.sort(key=lambda r: r["daily_counts"][0] if r["daily_counts"] else 0, reverse=True)

    # 合计行
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


def format_date_short(date_str):
    """20250521 → 5月21日"""
    m = str(int(date_str[4:6]))
    d = str(int(date_str[6:8]))
    return f"{m}月{d}日"


# =========================================================================
# 4. 主流程
# =========================================================================


def main():
    import argparse

    parser = argparse.ArgumentParser(description="获取行业创新高数据")
    parser.add_argument(
        "--dates",
        type=str,
        default="20260526,20260525,20260522,20260521,20260520,20260519,20260515,20260514,20260513,20260512",
        help="逗号分隔的日期 YYYYMMDD",
    )
    parser.add_argument(
        "--type",
        type=str,
        default="month",
        choices=["month", "60d", "120d", "1year", "alltime"],
        help="创新高类型: month=20日, 60d=60日, 120d=120日, 1year=一年, alltime=历史新高",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=os.path.join(os.path.dirname(__file__), "static"),
        help="输出目录",
    )
    args = parser.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]

    type_suffix = args.type  # month, 60d, 120d
    type_labels = {"month": "创20日新高", "60d": "创60日新高", "120d": "创120日新高", "1year": "创一年新高", "alltime": "收盘创历史新高"}
    data_file = os.path.join(args.output_dir, f"new_highs_data_{type_suffix}.json")
    details_file = os.path.join(args.output_dir, f"new_highs_details_{type_suffix}.json")

    # 0. 获取当前上市股票
    active_codes = get_active_stock_codes()

    # 1. 行业分类（只保留当前上市股票）
    industry_map = load_sw_classification(active_codes)

    # 2. 每日创新高数据
    daily_stocks = fetch_daily_new_highs(dates, query_type=args.type)

    # 3. 聚合
    output = aggregate(daily_stocks, industry_map)

    # 4. 拆分为两个文件
    os.makedirs(args.output_dir, exist_ok=True)

    # 4a. 轻量 counts 文件（不含 daily_details）
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

    # 4b. 明细文件
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


if __name__ == "__main__":
    main()
