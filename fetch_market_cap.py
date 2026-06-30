"""
行业市值变化分析
- 从 Sina 实时行情获取总股本（总市值/收盘价反推）
- 结合 K 线缓存 close 价格计算每日个股市值
- 按行业聚合，输出 market_cap.json
"""
import argparse
import json
import os
import re
import time
import warnings

import akshare as ak
import pandas as pd
import requests

from kline_cache import (
    KlineCache,
    format_date_for_query,
    format_date_short,
    get_active_codes,
    get_trade_dates,
    load_industry_map,
    _market_prefix,
)

warnings.filterwarnings("ignore")
STATIC = os.path.join(os.path.dirname(__file__), "static")
SHARES_FILE = os.path.join(STATIC, "stock_shares.json")
MAX_DATES = 20


def parse_args():
    p = argparse.ArgumentParser(description="行业市值变化分析")
    p.add_argument("--dates", help="YYYYMMDD 逗号分隔")
    p.add_argument("--mode", choices=["incremental", "missing", "rebuild"], default="incremental")
    p.add_argument("--industry-scheme", choices=["sw", "ths"], default="sw", help="行业分类: sw=申万, ths=同花顺")
    p.add_argument("--force-refresh", action="store_true")
    return p.parse_args()


def fetch_shares(codes):
    """从腾讯行情批量获取总股本（field[72]）"""
    shares = {}
    batch_size = 200  # 腾讯API限制
    total = len(codes)
    for start in range(0, total, batch_size):
        batch = codes[start:start + batch_size]
        tencodes = [f"{'sh' if c.startswith('6') else 'sz'}{c}" for c in batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(tencodes)
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.encoding = "gbk" if resp.apparent_encoding else "utf-8"
            for line in resp.text.strip().split("\n"):
                if "~" not in line:
                    continue
                try:
                    fields = line.split('"')[1].split("~") if '"' in line else []
                    if len(fields) < 73:
                        continue
                    tc = line.split("=")[0].split("_")[-1] if "_" in line else ""
                    code = tc[2:] if tc.startswith(("sh","sz")) else tc
                    total_shares = float(fields[72]) if len(fields) > 72 and fields[72] else 0
                    if code and total_shares > 0:
                        shares[code] = int(total_shares)
                except (ValueError, IndexError):
                    continue
            if (start + len(batch)) % 1000 < len(batch):
                print(f"  股本获取: {min(start+len(batch), total)}/{total}")
        except Exception as e:
            print(f"  batch err: {e}")
    return shares


def load_shares(codes):
    if os.path.exists(SHARES_FILE):
        try:
            existing = json.load(open(SHARES_FILE, "r", encoding="utf-8"))
            missing = [c for c in codes if c not in existing]
            if missing:
                print(f"[shares] 补齐 {len(missing)} 只股本...")
                new_shares = fetch_shares(missing)
                existing.update(new_shares)
                json.dump(existing, open(SHARES_FILE, "w", encoding="utf-8"))
            return existing
        except Exception:
            pass
    print(f"[shares] 首次获取 {len(codes)} 只股本...")
    shares = fetch_shares(codes)
    json.dump(shares, open(SHARES_FILE, "w", encoding="utf-8"))
    return shares


def date_info(date_str):
    return {"label": format_date_short(date_str), "full_label": format_date_for_query(date_str)}


def existing_date_keys(data):
    if not data:
        return set()
    keys = set()
    for info in data.get("dates", []):
        m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", info.get("full_label", ""))
        if m:
            keys.add(f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}")
    return keys


def load_existing(path):
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, "r", encoding="utf-8"))
    except Exception:
        return None


def load_ind_map(scheme="sw"):
    """加载行业映射，支持 sw(申万) 和 ths(同花顺)"""
    active_codes = get_active_codes()
    if scheme == "ths":
        ths_path = os.path.join(STATIC, "industry_map_ths.json")
        if os.path.exists(ths_path):
            ths_map = json.load(open(ths_path, "r", encoding="utf-8"))
            # 同花顺覆盖不到的股票，回退到申万分类
            sw_map = load_industry_map(active_codes)
            merged = {}
            for c in active_codes:
                if c in ths_map:
                    merged[c] = ths_map[c]
                elif c in sw_map:
                    merged[c] = sw_map[c]
                else:
                    merged[c] = "其他"
            return merged
    return load_industry_map(active_codes)


def aggregate_dates(target_dates, shares, force_refresh=False, scheme="sw"):
    print("[1/4] 加载行业分类和股票名称...")
    active_codes = get_active_codes()
    ind_map = load_ind_map(scheme)
    try:
        name_df = ak.stock_info_a_code_name()
        name_map = dict(zip(name_df['code'].astype(str).str.zfill(6), name_df['name']))
    except Exception:
        name_map = {}
    codes = [c for c in active_codes if c in ind_map and c in shares]
    print(f"  {len(codes)} 只（有股本数据，{len(name_map)} 只有名称）")

    print("[2/4] 读取 K 线缓存...")
    t0 = time.time()
    cache = KlineCache(force_refresh=force_refresh)
    all_data = cache.ensure(codes, target_dates[-1], need_ohlcv=True)
    print(f"  完成: {len(all_data)} 只 ({time.time()-t0:.1f}s)")

    print("[3/4] 计算个股市值并聚合行业...")
    target_set = set(target_dates)
    # 行业汇总 + 个股明细
    ind_by_date = {}        # {industry: {date: total_mcap}}
    ind_stocks = {}         # {industry_date: [{code,name,mcap,chg}]}

    for code, df in all_data.items():
        ind = ind_map.get(code)
        sh = shares.get(code)
        if not ind or df is None or df.empty or not sh:
            continue
        date_strs = df["date"].dt.strftime("%Y%m%d")
        subset = df[date_strs.isin(target_set)]
        if subset.empty:
            continue
        # 构建 close 映射用于计算 change_pct
        close_map = {}
        for _, r2 in df.iterrows():
            close_map[r2["date"].strftime("%Y%m%d")] = float(r2["close"])
        for _, row in subset.iterrows():
            date_str = row["date"].strftime("%Y%m%d")
            mcap = row["close"] * sh
            close_val = float(row["close"])
            # 计算当日涨跌幅：从 close_map 中找前一交易日收盘价
            prev_dates = sorted([d for d in close_map.keys() if d < date_str])
            prev_close = close_map.get(prev_dates[-1]) if prev_dates else 0
            chg = round((close_val - prev_close) / prev_close * 100, 2) if prev_close and prev_close > 0 else 0
            ind_by_date.setdefault(ind, {}).setdefault(date_str, 0)
            ind_by_date[ind][date_str] += mcap
            key = f"{ind}|{date_str}"
            ind_stocks.setdefault(key, []).append({
                "code": code, "name": name_map.get(code, ""),
                "close": round(close_val, 2), "mcap": round(mcap),
                "change_pct": chg
            })

    # 构建输出 rows，含 stocks_by_date
    rows = []
    date_labels = [format_date_short(d) for d in target_dates]
    for ind in sorted(ind_by_date.keys()):
        td = ind_by_date[ind]
        daily = [round(td.get(d, 0)) for d in target_dates]
        sbd = {}
        for d, dl in zip(target_dates, date_labels):
            key = f"{ind}|{d}"
            stocks = sorted(ind_stocks.get(key, []), key=lambda x: -x["mcap"])
            if stocks:
                sbd[dl] = stocks
        rows.append({"industry": ind, "daily_mcap": daily, "stocks_by_date": sbd})

    total_daily = [0] * len(target_dates)
    for r in rows:
        for i, v in enumerate(r.get("daily_mcap", [])):
            total_daily[i] += v
    total_t = total_daily[-1] if total_daily else 0
    if total_t <= 0:
        raise RuntimeError("市值聚合结果为空")

    output = {
        "dates": [date_info(d) for d in target_dates],
        "industries": rows,
        "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_mcap": round(total_t),
    }
    return recompute_fields(output), ind_stocks


def recompute_fields(data):
    dates = data.get("dates", [])
    rows = [r for r in data.get("industries", [])]
    if not dates or not rows:
        return data
    latest_idx = len(dates) - 1
    prev_idx = latest_idx - 1

    total_daily = [0] * len(dates)
    for r in rows:
        daily = r.get("daily_mcap", [])
        for i, v in enumerate(daily[:len(dates)]):
            total_daily[i] += round(v)
    total_latest = total_daily[latest_idx] if total_daily else 0

    for r in rows:
        daily = r.get("daily_mcap", [])
        latest = round(daily[latest_idx]) if latest_idx < len(daily) else 0
        prev = round(daily[prev_idx]) if prev_idx >= 0 and prev_idx < len(daily) else 0
        share = round(latest / max(total_latest, 1) * 100, 1)
        chg_val = round((latest - prev) / max(prev, 1) * 100, 1) if prev > 0 else None
        r["mcap"] = latest
        r["share"] = share
        r["change_pct"] = chg_val

        last5 = daily[:latest_idx + 1][-5:]
        if len(last5) >= 3:
            diffs = [last5[i] - last5[i - 1] for i in range(1, len(last5))]
            up = sum(1 for d in diffs if d > 0)
            dn = len(diffs) - up
            r["trend_5d"] = "↗" * up + "↘" * dn
        else:
            r["trend_5d"] = "—"

    rows.sort(key=lambda x: -x.get("mcap", 0))
    rows.append({
        "industry": "全市场合计", "mcap": round(total_latest), "share": 100.0,
        "change_pct": None, "trend_5d": "—", "is_total": True,
        "daily_mcap": [round(v) for v in total_daily],
    })
    data["industries"] = rows
    data["total_mcap"] = round(total_latest)
    return data


def merge_industry(existing, partial):
    if not existing:
        return recompute_fields(partial)
    old = existing
    new = partial
    old_keys = [d for d in [re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", x.get("full_label","")) for x in old.get("dates",[])] if d]
    old_keys = [f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" for m in old_keys]
    new_keys = [d for d in [re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", x.get("full_label","")) for x in new.get("dates",[])] if d]
    new_keys = [f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" for m in new_keys]
    merged_keys = sorted(set(old_keys) | set(new_keys))[-MAX_DATES:]
    new_set = set(new_keys)

    date_labels = [format_date_short(k) for k in merged_keys]

    old_map = {}
    old_sbd = {}
    for r in old.get("industries", []):
        if r.get("is_total"):
            continue
        old_map[r["industry"]] = dict(zip(old_keys, r.get("daily_mcap", [])))
        old_sbd[r["industry"]] = r.get("stocks_by_date", {})
    new_map = {}
    new_sbd = {}
    for r in new.get("industries", []):
        if r.get("is_total"):
            continue
        new_map[r["industry"]] = dict(zip(new_keys, r.get("daily_mcap", [])))
        new_sbd[r["industry"]] = r.get("stocks_by_date", {})

    industries = sorted(set(list(old_map.keys()) + list(new_map.keys())))
    rows = []
    for ind in industries:
        daily = []
        for k in merged_keys:
            if k in new_set:
                daily.append(round(new_map.get(ind, {}).get(k, 0)))
            else:
                daily.append(round(old_map.get(ind, {}).get(k, 0)))
        if any(daily):
            # 合并 stocks_by_date：新数据覆盖旧数据
            sbd = {}
            sbd.update(old_sbd.get(ind, {}))
            sbd.update(new_sbd.get(ind, {}))
            # 只保留合并后日期范围内的 key
            sbd = {dl: sbd[dl] for dl in date_labels if dl in sbd}
            rows.append({"industry": ind, "daily_mcap": daily, "stocks_by_date": sbd})

    merged = {"dates": [date_info(k) for k in merged_keys], "industries": rows}
    return recompute_fields(merged)


def main():
    args = parse_args()
    scheme = args.industry_scheme
    suffix = f"_{scheme}" if scheme != "sw" else ""
    out_path = os.path.join(STATIC, f"market_cap{suffix}.json")
    requested = [d.strip() for d in args.dates.split(",") if d.strip()] if args.dates else get_trade_dates(MAX_DATES)
    requested = sorted(dict.fromkeys(requested))
    if not requested:
        raise RuntimeError("无交易日")

    codes = get_active_codes()
    ind_map = load_ind_map(scheme)
    mapped_codes = [c for c in codes if c in ind_map]
    shares = load_shares(mapped_codes)

    existing = load_existing(out_path)
    existing_keys = existing_date_keys(existing)
    should_rebuild = args.mode == "rebuild" or args.force_refresh
    compute = requested if should_rebuild else [d for d in requested if d not in existing_keys]

    if not compute and existing:
        print(f"[skip] 市值已包含请求日期: {','.join(requested)}")
        return

    partial, ind_stocks = aggregate_dates(compute, shares, force_refresh=args.force_refresh, scheme=scheme)
    output = partial if should_rebuild else merge_industry(existing, partial)

    # Attach latest date stock details
    latest_key = compute[-1]
    for row in output.get("industries", []):
        if row.get("is_total"):
            continue
        key = f"{row['industry']}|{latest_key}"
        stocks = ind_stocks.get(key, [])
        stocks.sort(key=lambda x: -x["mcap"])
        # Add name from spot cache if available
        # For now keep code+mcap
        row["stocks"] = stocks[:50]  # top 50 per industry

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n[4/4] 已保存: {out_path}")
    print(f"\n全市场总市值: {output.get('total_mcap', 0)/1e8:.0f} 亿")
    for r in output.get("industries", [])[:10]:
        if r.get("is_total"):
            continue
        chg = f"{r['change_pct']:+.1f}%" if r.get("change_pct") is not None else "—"
        print(f"  {r['industry']:6s} {r['mcap']/1e8:6.0f} 亿 ({r['share']:4.1f}%) {r['trend_5d']} {chg}")


if __name__ == "__main__":
    main()
