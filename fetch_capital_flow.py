"""
板块资金流向分析 v6
- 复用 kline_cache 的 K 线缓存
- 日常更新只补缺失日期，避免重复重算 20 天
"""
import argparse
import json
import os
import re
import time
import warnings

import pandas as pd

from kline_cache import (
    KlineCache,
    format_date_for_query,
    format_date_short,
    get_active_codes,
    get_trade_dates,
    load_industry_map,
)

warnings.filterwarnings("ignore")

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_DATES = 20


def parse_args():
    parser = argparse.ArgumentParser(description="板块资金流向分析")
    parser.add_argument("--dates", help="指定交易日，逗号分隔，格式 YYYYMMDD")
    parser.add_argument("--mode", choices=["incremental", "missing", "rebuild"], default="incremental")
    parser.add_argument("--industry-scheme", choices=["sw", "ths"], default="sw", help="行业分类: sw=申万, ths=同花顺")
    parser.add_argument("--force-refresh", action="store_true", help="强制刷新 K 线缓存并重算指定日期")
    parser.add_argument("--output-dir", default=STATIC, help="输出目录，默认 static")
    return parser.parse_args()


def parse_date_info(info):
    full = (info or {}).get("full_label", "")
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", full)
    if not m:
        return None
    return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"


def date_info(date_str):
    return {
        "label": format_date_short(date_str),
        "full_label": format_date_for_query(date_str),
    }


def load_existing(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("dates"), list) or not isinstance(data.get("industries"), list):
            return None
        return data
    except Exception:
        return None


def existing_date_keys(data):
    if not data:
        return []
    keys = []
    for info in data.get("dates", []):
        k = parse_date_info(info)
        if k:
            keys.append(k)
    return keys


def calc_trend(vals):
    vals = vals[-5:]
    if len(vals) < 3:
        return "—"
    diffs = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    up = sum(1 for d in diffs if d > 0)
    dn = len(diffs) - up
    return "↗" * up + "↘" * dn


def calc_signal(share, chg):
    # 加速流入（高阈值优先）
    if share > 1.5 and chg is not None and chg > 10:
        return "📈 加速流入"
    # 持续流入
    if share > 3 and chg is not None and chg > 5:
        return "🔥 持续流入"
    # 资金撤离
    if chg is not None and chg < -20:
        return "❄️ 资金撤离"
    # 高位缩量
    if share > 2 and chg is not None and chg < -10:
        return "⚠️ 高位缩量"
    return ""


def load_ind_map(scheme="sw"):
    """加载行业映射，支持 sw(申万) 和 ths(同花顺)"""
    active_codes = get_active_codes()
    if scheme == "ths":
        ths_path = os.path.join(STATIC, "industry_map_ths.json")
        if os.path.exists(ths_path):
            ths_map = json.load(open(ths_path, "r", encoding="utf-8"))
            # Merge with SW fallback
            sw_map = load_industry_map(active_codes)
            merged = {}
            for c in active_codes:
                if c in ths_map: merged[c] = ths_map[c]
                elif c in sw_map: merged[c] = sw_map[c]
                else: merged[c] = "其他"
            return merged
    return load_industry_map(active_codes)

def aggregate_dates(target_dates, scheme="sw", force_refresh=False):
    print("[1/4] 加载行业分类...")
    ind_map = load_ind_map(scheme)
    codes = [c for c in get_active_codes() if c in ind_map]
    print(f"  {len(codes)} 只")
    print(f"  目标日期: {len(target_dates)}天 ({target_dates[0]} ~ {target_dates[-1]})")

    print("[2/4] 从共享缓存读取 K 线...")
    t0 = time.time()
    cache = KlineCache(force_refresh=force_refresh)
    all_data = cache.ensure(codes, target_dates[-1], need_ohlcv=True)
    elapsed = time.time() - t0
    print(f"  完成: {len(all_data)} 只 ({elapsed:.1f}s)")

    print("[3/4] 聚合行业成交额...")
    target_set = set(target_dates)
    ind_by_date = {}

    for code, df in all_data.items():
        ind = ind_map.get(code)
        if not ind or df is None or df.empty:
            continue
        date_strs = df["date"].dt.strftime("%Y%m%d")
        subset = df[date_strs.isin(target_set)]
        if subset.empty:
            continue
        # Build per-stock prev_close for direction detection
        closes = {}
        for _, row in df.iterrows():
            closes[row["date"].strftime("%Y%m%d")] = float(row["close"])
        for _, row in subset.iterrows():
            date_str = row["date"].strftime("%Y%m%d")
            turnover = float(row["close"]) * float(row["volume"])
            # Direction: positive if close > prev close (inflow), negative if close < prev close (outflow)
            cur_close = float(row["close"])
            date_idx = target_dates.index(date_str) if date_str in target_dates else -1
            if date_idx > 0:
                prev_close = closes.get(target_dates[date_idx - 1], 0)
            else:
                # 首个日期：从 closets 字典中找前一日的收盘价
                prev_dates = sorted([d for d in closes.keys() if d < date_str])
                prev_close = closes.get(prev_dates[-1], 0) if prev_dates else 0
            net = turnover if (prev_close and prev_close > 0 and cur_close >= prev_close) else -turnover
            ind_by_date.setdefault(ind, {}).setdefault(date_str, {"turnover": 0, "net": 0, "stocks": 0})
            ind_by_date[ind][date_str]["turnover"] += turnover
            ind_by_date[ind][date_str]["net"] += net
            ind_by_date[ind][date_str]["stocks"] += 1

    result_rows = []
    for ind in sorted(ind_by_date.keys()):
        td = ind_by_date[ind]
        daily = [round(td.get(d, {}).get("turnover", 0)) for d in target_dates]
        daily_net = [round(td.get(d, {}).get("net", 0)) for d in target_dates]
        stock_counts = [int(td.get(d, {}).get("stocks", 0)) for d in target_dates]
        # Cumulative net flow
        cum = 0
        cumulative = []
        for n in daily_net:
            cum += n
            cumulative.append(round(cum))
        result_rows.append({
            "industry": ind,
            "daily_turnover": daily,
            "daily_net_flow": daily_net,
            "cumulative_flow": cumulative,
            "daily_stock_counts": stock_counts,
        })

    total_daily = [0] * len(target_dates)
    for r in result_rows:
        for i, v in enumerate(r.get("daily_turnover", [])):
            total_daily[i] += v
    total_t = total_daily[-1] if total_daily else 0
    if total_t <= 0:
        raise RuntimeError("资金流向聚合结果为空，未写入文件")

    output = {
        "dates": [date_info(d) for d in target_dates],
        "industries": result_rows,
        "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_turnover": round(total_t),
    }
    return recompute_fields(output)


def build_maps(data):
    keys = existing_date_keys(data)
    turnover = {}
    stock_counts = {}
    net_flows = {}
    rows_by_ind = {}
    for row in data.get("industries", []):
        if row.get("is_total"):
            continue
        ind = row.get("industry")
        if not ind:
            continue
        rows_by_ind[ind] = row
        daily = row.get("daily_turnover") or []
        counts = row.get("daily_stock_counts") or []
        nf = row.get("daily_net_flow") or []
        turnover[ind] = {}
        stock_counts[ind] = {}
        net_flows[ind] = {}
        for i, k in enumerate(keys):
            turnover[ind][k] = round(daily[i]) if i < len(daily) else 0
            if i < len(counts):
                stock_counts[ind][k] = int(counts[i] or 0)
            if i < len(nf):
                net_flows[ind][k] = round(nf[i])
        if keys and row.get("stock_count") is not None:
            latest_key = keys[-1]
            stock_counts[ind].setdefault(latest_key, int(row.get("stock_count") or 0))
    return keys, turnover, stock_counts, net_flows, rows_by_ind


def recompute_fields(data, old_rows=None):
    dates = data.get("dates", [])
    date_keys = existing_date_keys(data)
    rows = [r for r in data.get("industries", []) if not r.get("is_total")]
    if not dates:
        return data

    total_daily = [0] * len(dates)
    for row in rows:
        daily = row.get("daily_turnover") or []
        for i, v in enumerate(daily[:len(dates)]):
            total_daily[i] += round(v)

    latest_idx = len(dates) - 1
    prev_idx = latest_idx - 1
    total_latest = total_daily[latest_idx] if total_daily else 0
    latest_key = date_keys[-1] if date_keys else None

    for row in rows:
        daily = row.get("daily_turnover") or []
        latest = round(daily[latest_idx]) if latest_idx < len(daily) else 0
        prev = round(daily[prev_idx]) if prev_idx >= 0 and prev_idx < len(daily) else 0
        share = round(latest / max(total_latest, 1) * 100, 1) if total_latest > 0 else 0
        chg = round((latest - prev) / max(prev, 1) * 100, 1) if prev > 0 else None
        row["turnover"] = latest
        row["share"] = share
        row["change_pct"] = chg
        row["trend_5d"] = calc_trend(daily[:latest_idx + 1])
        row["signal"] = calc_signal(share, chg)
        counts = row.get("daily_stock_counts") or []
        if latest_idx < len(counts) and counts[latest_idx] > 0:
            row["stock_count"] = int(counts[latest_idx])
        elif old_rows and row.get("industry") in old_rows:
            row["stock_count"] = int(old_rows[row["industry"]].get("stock_count") or 0)
        else:
            row["stock_count"] = int(row.get("stock_count") or 0)

    rows.sort(key=lambda x: -x.get("turnover", 0))
    total_counts = [0] * len(dates)
    for row in rows:
        counts = row.get("daily_stock_counts") or []
        for i, v in enumerate(counts[:len(dates)]):
            total_counts[i] += int(v or 0)

    total_net = [0] * len(dates)
    for row in rows:
        nf = row.get("daily_net_flow") or []
        for i, v in enumerate(nf[:len(dates)]):
            total_net[i] += v
    total_cum = 0
    total_cumulative = []
    for n in total_net:
        total_cum += n
        total_cumulative.append(round(total_cum))

    rows.append({
        "industry": "全市场合计",
        "turnover": round(total_latest),
        "share": 100.0,
        "change_pct": None,
        "trend_5d": "—",
        "signal": "",
        "stock_count": total_counts[latest_idx] if total_counts else 0,
        "is_total": True,
        "daily_turnover": [round(v) for v in total_daily],
        "daily_net_flow": [round(v) for v in total_net],
        "cumulative_flow": total_cumulative,
        "daily_stock_counts": total_counts,
    })
    data["industries"] = rows
    data["total_turnover"] = round(total_latest)
    return data


def merge_capital_flow(existing, partial):
    if not existing:
        return recompute_fields(partial)

    old_keys, old_turnover, old_counts, old_net_flows, old_rows = build_maps(existing)
    new_keys, new_turnover, new_counts, new_net_flows, _ = build_maps(partial)
    merged_keys = sorted(set(old_keys) | set(new_keys))[-MAX_DATES:]
    new_key_set = set(new_keys)

    industries = sorted(set(old_turnover.keys()) | set(new_turnover.keys()))
    rows = []
    for ind in industries:
        daily = []
        counts = []
        net_flow = []
        for k in merged_keys:
            if k in new_key_set:
                daily.append(round(new_turnover.get(ind, {}).get(k, 0)))
                counts.append(int(new_counts.get(ind, {}).get(k, 0)))
                net_flow.append(round(new_net_flows.get(ind, {}).get(k, 0)))
            else:
                daily.append(round(old_turnover.get(ind, {}).get(k, 0)))
                counts.append(int(old_counts.get(ind, {}).get(k, 0)))
                net_flow.append(round(old_net_flows.get(ind, {}).get(k, 0)))
        if any(daily):
            # 计算 cumulative_flow
            cum = 0
            cumulative = []
            for n in net_flow:
                cum += n
                cumulative.append(round(cum))
            rows.append({
                "industry": ind,
                "daily_turnover": daily,
                "daily_net_flow": net_flow,
                "cumulative_flow": cumulative,
                "daily_stock_counts": counts,
            })

    merged = {
        "dates": [date_info(k) for k in merged_keys],
        "industries": rows,
        "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return recompute_fields(merged, old_rows=old_rows)


def main():
    args = parse_args()
    out_dir = args.output_dir
    os.makedirs(out_dir, exist_ok=True)
    scheme = args.industry_scheme
    suffix = f"_{scheme}" if scheme != "sw" else ""
    out_path = os.path.join(out_dir, f"capital_flow{suffix}.json")

    requested_dates = [d.strip() for d in args.dates.split(",") if d.strip()] if args.dates else get_trade_dates(MAX_DATES)
    requested_dates = sorted(dict.fromkeys(requested_dates))
    if not requested_dates:
        raise RuntimeError("没有可更新的交易日")

    existing = load_existing(out_path)
    existing_keys = set(existing_date_keys(existing))
    should_rebuild = args.mode == "rebuild" or args.force_refresh
    compute_dates = requested_dates if should_rebuild else [d for d in requested_dates if d not in existing_keys]

    if not compute_dates and existing:
        print(f"[skip] 资金流向已包含请求日期: {','.join(requested_dates)}")
        return

    partial = aggregate_dates(compute_dates, scheme=scheme, force_refresh=args.force_refresh)
    output = partial if should_rebuild else merge_capital_flow(existing, partial)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n[4/4] 已保存: {out_path}")
    print(f"\n全市场成交: {output.get('total_turnover', 0)/1e8:.0f}亿")
    for r in output.get("industries", [])[:10]:
        if r.get("is_total"):
            continue
        chg = f"{r['change_pct']:+.1f}%" if r.get("change_pct") is not None else "—"
        print(f"  {r['industry']:6s} {r['turnover']/1e8:5.0f}亿 ({r['share']:4.1f}%) {r['trend_5d']} {r['signal']}")


if __name__ == "__main__":
    main()
