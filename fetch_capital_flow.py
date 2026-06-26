"""
板块资金流向分析 v5
- 复用 kline_cache 的 K 线缓存
- 只计算行业成交额，不再独立下载全量 K 线
"""
import json
import os
import time
import warnings

import pandas as pd

from kline_cache import (
    KlineCache,
    get_active_codes,
    get_trade_dates,
    load_industry_map,
)

warnings.filterwarnings("ignore")

STATIC = os.path.join(os.path.dirname(__file__), "static")


def main():
    print("[1/4] 加载行业分类...")
    ind_map = load_industry_map()
    codes = get_active_codes()
    print(f"  {len(codes)} 只")

    target_dates = get_trade_dates(20)
    target_set = set(target_dates)
    print(f"  目标日期: {len(target_dates)}天 ({target_dates[0]} ~ {target_dates[-1]})")

    print("[2/4] 从共享缓存读取 K 线...")
    t0 = time.time()
    cache = KlineCache()
    all_data = cache.ensure(codes, target_dates[-1], need_ohlcv=True)
    elapsed = time.time() - t0
    print(f"  完成: {len(all_data)} 只 ({elapsed:.1f}s)")

    print("[3/4] 聚合行业成交额...")
    ind_by_date = {}

    for code, df in all_data.items():
        ind = ind_map.get(code)
        if not ind or df is None or df.empty:
            continue
        for _, row in df.iterrows():
            date_str = row["date"].strftime("%Y%m%d")
            if date_str not in target_set:
                continue
            turnover = row["close"] * row["volume"]
            ind_by_date.setdefault(ind, {}).setdefault(date_str, {"turnover": 0, "stocks": 0})
            ind_by_date[ind][date_str]["turnover"] += turnover
            ind_by_date[ind][date_str]["stocks"] += 1

    # 构建输出
    result_rows = []
    for ind in sorted(ind_by_date.keys()):
        td = ind_by_date[ind]
        latest_date = target_dates[-1]
        t_now = td.get(latest_date, {}).get("turnover", 0)
        total_now = sum(v.get(latest_date, {}).get("turnover", 0) for v in ind_by_date.values())
        share = round(t_now / max(total_now, 1) * 100, 1) if total_now > 0 else 0

        prev_date = target_dates[-2] if len(target_dates) >= 2 else None
        t_prev = td.get(prev_date, {}).get("turnover", 0) if prev_date else 0
        chg = round((t_now - t_prev) / max(t_prev, 1) * 100, 1) if t_prev > 0 else None

        # 5日成交额趋势
        trend_vals = [td.get(d, {}).get("turnover", 0) for d in target_dates[-5:]]
        arrows = "—"
        if len(trend_vals) >= 3:
            diffs = [trend_vals[i] - trend_vals[i - 1] for i in range(1, len(trend_vals))]
            up = sum(1 for d in diffs if d > 0)
            dn = len(diffs) - up
            arrows = "↗" * up + "↘" * dn

        sig = ""
        if share > 3 and (chg is not None and chg > 5):
            sig = "🔥 持续流入"
        elif share > 1.5 and (chg is not None and chg > 10):
            sig = "📈 加速流入"
        elif chg is not None and chg < -20:
            sig = "❄️ 资金撤离"
        elif share > 2 and (chg is not None and chg < -10):
            sig = "⚠️ 高位缩量"

        daily = [round(td.get(d, {}).get("turnover", 0)) for d in target_dates]

        result_rows.append({
            "industry": ind, "turnover": round(t_now), "share": share,
            "change_pct": chg, "trend_5d": arrows, "signal": sig,
            "stock_count": td.get(latest_date, {}).get("stocks", 0),
            "daily_turnover": daily,
        })

    result_rows.sort(key=lambda x: -x["turnover"])
    total_daily = [0] * len(target_dates)
    for r in result_rows:
        for i, v in enumerate(r.get("daily_turnover", [])):
            total_daily[i] += v
    total_t = total_daily[-1] if total_daily else 0
    result_rows.append({
        "industry": "全市场合计", "turnover": round(total_t), "share": 100.0,
        "change_pct": None, "trend_5d": "—", "signal": "",
        "stock_count": len(codes), "is_total": True,
        "daily_turnover": total_daily,
    })

    dates_info = []
    for d in target_dates:
        y, m, d_ = d[:4], str(int(d[4:6])), str(int(d[6:8]))
        dates_info.append({"label": f"{m}月{d_}日", "full_label": f"{y}年{m}月{d_}日"})

    output = {
        "dates": dates_info,
        "industries": result_rows,
        "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_turnover": round(total_t),
    }

    out_path = os.path.join(STATIC, "capital_flow.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n[4/4] 已保存: {out_path}")
    print(f"\n全市场成交: {total_t/1e8:.0f}亿")
    for r in result_rows[:10]:
        if r.get("is_total"):
            continue
        chg = f"{r['change_pct']:+.1f}%" if r["change_pct"] is not None else "—"
        print(f"  {r['industry']:6s} {r['turnover']/1e8:5.0f}亿 ({r['share']:4.1f}%) {r['trend_5d']} {r['signal']}")


if __name__ == "__main__":
    main()
