"""
市场分析复盘脚本 v2
- 不是简单的数数和排序，而是挖掘交易信号
- 三个维度：行业轮动（资金流向）、趋势变化（转折点）、多周期共振（持续性 vs 拐点）
使用: python analyze_market.py
"""

import json
import os

STATIC = os.path.join(os.path.dirname(__file__), "static")
TYPES = ["month", "60d", "120d", "1year", "alltime"]
TYPE_CN = {"month": "20日", "60d": "60日", "120d": "120日", "1year": "一年", "alltime": "历史/近7年"}


def load_json(filename):
    path = os.path.join(STATIC, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def load_all():
    highs, lows = {}, {}
    for t in TYPES:
        highs[t] = load_json(f"new_highs_data_{t}.json")
        lows[t] = load_json(f"new_lows_data_{t}.json")
    return highs, lows


def get_industry_data(data, exclude_total=True):
    return [r for r in data["industries"] if not r.get("is_total")] if data else []


def get_total_row(data):
    if not data:
        return None
    return next((r for r in data["industries"] if r.get("is_total")), None)


# =========================================================================
# 1. 行业轮动 — 找资金迁移的方向和节奏
# =========================================================================

def sector_rotation(highs, lows):
    print("=" * 64)
    print("一、行业轮动 — 资金流向与迁移")
    print("=" * 64)

    h_inds = get_industry_data(highs["month"])
    l_inds = get_industry_data(lows["month"])
    date_label = highs["month"]["dates"][0]["label"]

    # 每个行业的新高占比、新低占比
    h_map = {r["industry"]: (r["daily_counts"][0], r["daily_counts"][0] / max(r["total"], 1) * 100)
             for r in h_inds}
    l_map = {r["industry"]: (r["daily_counts"][0], r["daily_counts"][0] / max(r["total"], 1) * 100)
             for r in l_inds}

    # ---- 信号1: 资金明确流入/流出的行业 ----
    # 条件：新高占比 > 5% 且 新低占比 < 5%（资金共识流入）
    #      新低占比 > 20% 且 新高占比 < 2%（资金共识流出）
    inflow = []
    outflow = []
    for r in h_inds:
        ind = r["industry"]
        h_ratio = h_map.get(ind, (0, 0))[1]
        l_ratio = l_map.get(ind, (0, 0))[1]
        if h_ratio > 5 and l_ratio < 5:
            inflow.append((ind, h_ratio, l_ratio))
        if l_ratio > 20 and h_ratio < 2:
            outflow.append((ind, h_ratio, l_ratio))
    inflow.sort(key=lambda x: -x[1])
    outflow.sort(key=lambda x: -x[2])

    print(f"\n  {date_label}")
    if inflow:
        print(f"  ✅ 资金共识流入: {', '.join(f'{ind}(新高{h:.0f}%/新低{l:.0f}%)' for ind, h, l in inflow)}")
    else:
        print(f"  ✅ 资金共识流入: 无（没有行业同时满足新高>5%且新低<5%）")

    if outflow:
        print(f"  ❌ 资金共识流出: {', '.join(f'{ind}(新低{l:.0f}%/新高{h:.0f}%)' for ind, h, l in outflow)}")
    else:
        print(f"  ❌ 资金共识流出: 无")

    # ---- 信号2: 行业分化 — 同时出现在新高和新低前列 ----
    h_top = set(r["industry"] for r in sorted(h_inds, key=lambda x: -x["daily_counts"][0])[:6])
    l_top = set(r["industry"] for r in sorted(l_inds, key=lambda x: -x["daily_counts"][0])[:6])
    divergent = h_top & l_top
    if divergent:
        print(f"  ⚠️  行业内部分化: {', '.join(divergent)}")
        print(f"      这些行业同时有大量新高和新低个股 → 选股比选行业重要，龙头恒强、弱者恒弱")

    # ---- 信号3: 资金迁移方向（环比变化） ----
    # 对比前一天，哪些行业在"变好"（新低减少+新高增加）vs"变差"
    if len(highs["month"]["dates"]) >= 2:
        improving = []
        worsening = []
        for r in h_inds:
            ind = r["industry"]
            h_curr = r["daily_counts"][0]
            h_prev = r["daily_counts"][1] if len(r["daily_counts"]) > 1 else 0
            l_row = next((x for x in l_inds if x["industry"] == ind), None)
            l_curr = l_row["daily_counts"][0] if l_row else 0
            l_prev = l_row["daily_counts"][1] if l_row and len(l_row["daily_counts"]) > 1 else 0
            total = r["total"]

            h_delta = (h_curr - h_prev) / max(total, 1) * 100
            l_delta = (l_curr - l_prev) / max(total, 1) * 100
            score = h_delta - l_delta  # 正=改善

            if score > 10:
                improving.append((ind, score, h_curr - h_prev, l_curr - l_prev))
            elif score < -15:
                worsening.append((ind, score, h_curr - h_prev, l_curr - l_prev))

        improving.sort(key=lambda x: -x[1])
        worsening.sort(key=lambda x: x[1])
        if improving:
            items = [f"{ind}(新高{h:+d}/新低{l:+d})" for ind, s, h, l in improving[:5]]
            print(f"  📈 日环比改善最大: {', '.join(items)}")
        if worsening:
            items = [f"{ind}(新高{h:+d}/新低{l:+d})" for ind, s, h, l in worsening[:5]]
            print(f"  📉 日环比恶化最大: {', '.join(items)}")


# =========================================================================
# 2. 趋势变化 — 找转折点，不是简单计数
# =========================================================================

def trend_analysis(highs, lows):
    print("\n" + "=" * 64)
    print("二、趋势变化 — 转折点检测")
    print("=" * 64)

    for t in TYPES:
        l_data = lows[t]
        if not l_data:
            continue
        total_row = get_total_row(l_data)
        if not total_row:
            continue

        counts = total_row["daily_counts"]
        dates = l_data["dates"]
        if len(counts) < 4:
            continue

        # ---- 信号: 加速度（二阶导数）----
        # 新低数下降 = 好，但速度在加快还是减慢？
        deltas = []
        for i in range(len(counts) - 1):
            if counts[i + 1] > 0:
                deltas.append((counts[i] - counts[i + 1]) / counts[i + 1] * 100)

        if len(deltas) >= 3:
            # 一阶导（速度）：最新变化
            v_now = deltas[0]
            # 二阶导（加速度）：速度的变化
            v_prev = deltas[1]
            accel = v_now - v_prev

            # 区分方向：v_now负=新低减少(好), v_now正=新低增加(坏)
            if v_now < -10 and v_prev > 10:
                insight = "前日新低激增后今日大幅回落 → 恐慌释放后的快速修复，若次日持续减少则短期底部确认"
            elif v_now < -10 and v_prev < -10:
                if accel > 10:
                    insight = "新低持续减少且速度加快 → 市场加速修复，短期偏多"
                else:
                    insight = "新低持续减少但速度放缓 → 修复进入后半段，注意反弹力度衰减"
            elif v_now > 10 and v_prev < -10:
                insight = "新低由降转升 → 短期修复被打断，趋势可能再度转弱"
            elif v_now > 10 and v_prev > 10:
                if accel > 10:
                    insight = "新低加速扩散 → 恐慌蔓延，短期不宜抄底"
                elif accel < -10:
                    insight = "新低仍在扩散但加速度放缓 → 恐慌接近尾声，密切观察"
                else:
                    insight = "新低持续扩散中 → 趋势偏空"
            elif abs(v_now) < 10:
                insight = "新低家数趋于稳定 → 方向不明，等待信号"
            else:
                insight = "趋势延续中"

            print(f"\n  {TYPE_CN[t]}新低:")
            print(f"    最新变化: {v_now:+.1f}% | 前日变化: {v_prev:+.1f}% | 加速度: {accel:+.1f}%")
            print(f"    → {insight}")

            # 额外信号：极端值
            if abs(v_now) > 40:
                direction = "骤降" if v_now < 0 else "骤升"
                print(f"    ⚡ 新低家数{direction}超40%，属于极端波动，需要关注次日是否延续")


# =========================================================================
# 3. 多周期共振 — 三维分析
# =========================================================================

def multi_tf_resonance(highs, lows):
    print("\n" + "=" * 64)
    print("三、多周期共振 — 持续性 × 扩散度 × 极端度")
    print("=" * 64)

    # 每个行业 × 每个窗口的新低/新高占比
    l_ratios = {}  # {industry: {type: ratio}}
    h_ratios = {}
    for t in TYPES:
        for r in get_industry_data(lows[t]):
            ind = r["industry"]
            ratio = r["daily_counts"][0] / max(r["total"], 1) * 100
            l_ratios.setdefault(ind, {})[t] = ratio
        for r in get_industry_data(highs[t]):
            ind = r["industry"]
            ratio = r["daily_counts"][0] / max(r["total"], 1) * 100
            h_ratios.setdefault(ind, {})[t] = ratio

    # ---- 分析1: 新低的结构 — 短期恐慌 vs 长期衰退 ----
    print("\n  — 新低结构分析（短期恐慌 vs 长期衰退） —")
    l_structural = []
    for ind, ratios in l_ratios.items():
        r20 = ratios.get("month", 0)
        r60 = ratios.get("60d", 0)
        r120 = ratios.get("120d", 0)
        r1y = ratios.get("1year", 0)
        r7y = ratios.get("alltime", 0)

        # 短期/长期比值：高比值=短期恐慌为主，低比值=长期衰退
        short_term = r20
        long_term = (r60 + r120 + r1y) / 3 if (r60 + r120 + r1y) > 0 else 1
        ratio_stl = short_term / max(long_term, 1)

        total_windows = sum(1 for r in [r20, r60, r120, r1y, r7y] if r > 5)
        avg_all = (r20 + r60 + r120 + r1y + r7y) / 5

        if avg_all > 5:
            if ratio_stl > 2.0:
                nature = "短期恐慌（20日新低远高于长周期）→ 急跌后可能快速修复"
            elif ratio_stl < 0.8 and r7y > 3:
                nature = "长期衰退（各周期新低都高且均衡）→ 行业基本面有问题，回避"
            elif r20 < 10 and total_windows >= 3:
                nature = "阴跌（占比不高但持续多周期）→ 温水煮青蛙，关注但不恐慌"
            else:
                nature = "普遍弱势"
            l_structural.append((ind, r20, long_term, ratio_stl, total_windows, nature))

    l_structural.sort(key=lambda x: -(x[1] + x[2]))
    for ind, r20, lt, rstl, tw, nature in l_structural[:8]:
        print(f"    {ind}: 20日{r20:.0f}% | 长期均值{lt:.0f}% | 短/长比{rstl:.1f}")
        print(f"      → {nature}")

    # ---- 分析2: 极端度 — 离历史极值的距离 ----
    print("\n  — 极端度信号 —")
    extreme_signals = []
    for ind, ratios in l_ratios.items():
        r20 = ratios.get("month", 0)
        r7y = ratios.get("alltime", 0)

        # 近7年新低占比 > 5%：处于多年极端区域
        if r7y > 5:
            extreme_signals.append((ind, r20, r7y, "多年极端"))
        # 20日新低 > 40% 但近7年新低 = 0：短期急跌但未到历史极端
        elif r20 > 40 and r7y == 0:
            extreme_signals.append((ind, r20, r7y, "短期急跌，距历史底部尚有距离"))

    if extreme_signals:
        for ind, r20, r7y, desc in extreme_signals:
            print(f"    {ind}: 20日{r20:.0f}% | 近7年{r7y:.1f}% → {desc}")
    else:
        print(f"    无极端信号")

    # ---- 分析3: 强势行业的持续性 ----
    print("\n  — 强势行业持续性 —")
    h_strong = []
    for ind, ratios in h_ratios.items():
        avg_h = sum(ratios.values()) / len(ratios) if ratios else 0
        windows_with_highs = sum(1 for r in ratios.values() if r > 3)
        if windows_with_highs >= 3:
            h_strong.append((ind, ratios, avg_h, windows_with_highs))

    h_strong.sort(key=lambda x: -x[2])
    if h_strong:
        for ind, ratios, avg, tw in h_strong[:6]:
            ratio_str = " > ".join(f"{TYPE_CN[t]}{ratios.get(t,0):.1f}%" for t in TYPES if ratios.get(t, 0) > 0)
            print(f"    {ind} ({tw}窗口共振): {ratio_str}")
            if tw >= 4:
                print(f"      → 多周期新高共振，趋势确立，回调可能是买入机会")
    else:
        print(f"    无强势共振行业")

    # ---- 分析4: 扩散度 — 弱势在集中还是扩散 ----
    print("\n  — 弱势扩散度 —")
    # 统计新低占比>20%的行业数量变化
    for t_window, t_cn in [("month", "20日"), ("60d", "60日"), ("1year", "一年")]:
        l_data = lows[t_window]
        if not l_data:
            continue
        inds = get_industry_data(l_data)
        counts_over_time = []
        for i in range(min(5, len(l_data["dates"]))):
            cnt = sum(1 for r in inds if len(r["daily_counts"]) > i and
                      r["daily_counts"][i] / max(r["total"], 1) > 0.2)
            counts_over_time.append((l_data["dates"][i]["label"], cnt))

        if len(counts_over_time) >= 2:
            latest_cnt = counts_over_time[0][1]
            prev_cnt = counts_over_time[1][1]
            recent_dir = "收敛" if latest_cnt < prev_cnt else ("扩散" if latest_cnt > prev_cnt else "持平")
            recent_delta = latest_cnt - prev_cnt

            # 中期趋势：比较最近2天 vs 更早2天
            recent_2 = [c for _, c in counts_over_time[:2]]
            older_2 = [c for _, c in counts_over_time[2:4]] if len(counts_over_time) >= 4 else []
            mid_trend = ""
            if older_2:
                avg_recent = sum(recent_2) / len(recent_2)
                avg_older = sum(older_2) / len(older_2)
                if avg_recent > avg_older + 3:
                    mid_trend = "中期趋势：弱势行业在扩大"
                elif avg_recent < avg_older - 3:
                    mid_trend = "中期趋势：弱势行业在收敛"
                else:
                    mid_trend = "中期趋势：弱势行业数量稳定"

            print(f"    {t_cn}新低占比>20%的行业数: "
                  + " → ".join(f"{lbl}({cnt})" for lbl, cnt in counts_over_time[:5]))
            print(f"      → 日环比{recent_dir}({recent_delta:+d}) | {mid_trend}")


# =========================================================================
# 每日复盘摘要
# =========================================================================

def daily_summary(highs, lows):
    print("\n" + "=" * 64)
    print("四、每日复盘摘要")
    print("=" * 64)

    l_month = lows["month"]
    h_alltime = highs["alltime"]
    l_alltime = lows["alltime"]
    l_1year = lows["1year"]

    if not all([l_month, h_alltime, l_alltime, l_1year]):
        print("  数据不完整")
        return

    dl = l_month["dates"][0]["label"]
    l20_total = get_total_row(l_month)
    l1y_total = get_total_row(l_1year)
    l7y_total = get_total_row(l_alltime)
    h_ath_total = get_total_row(h_alltime)

    cnt_20d = l20_total["daily_counts"][0] if l20_total else 0
    cnt_prev = l20_total["daily_counts"][1] if l20_total and len(l20_total["daily_counts"]) > 1 else 0
    cnt_1y = l1y_total["daily_counts"][0] if l1y_total else 0
    cnt_7y = l7y_total["daily_counts"][0] if l7y_total else 0
    cnt_ath = h_ath_total["daily_counts"][0] if h_ath_total else 0

    chg_20d = round((cnt_20d - cnt_prev) / max(cnt_prev, 1) * 100, 1)

    # 基调判断
    ath_7y_ratio = cnt_ath / max(cnt_7y, 1)
    if ath_7y_ratio >= 2:
        tone = "强势（历史新高远超近7年新低）"
    elif ath_7y_ratio >= 1:
        tone = "偏多"
    elif ath_7y_ratio >= 0.5:
        tone = "震荡"
    elif ath_7y_ratio >= 0.2:
        tone = "偏空"
    else:
        tone = "弱势（历史新高极少，近7年新低压制）"

    print(f"\n  {dl}")
    print(f"  ┌─────────────────────────────────────────┐")
    print(f"  │ 20日新低: {cnt_20d:>5} 只 (日环比 {chg_20d:+.1f}%)"
          f"      一年新低: {cnt_1y:>5} 只       │")
    print(f"  │ 近7年新低: {cnt_7y:>5} 只"
          f"                  历史新高: {cnt_ath:>5} 只        │")
    print(f"  │ 新高/新低比: {ath_7y_ratio:.2f}"
          f"                       基调: {tone}        │")
    print(f"  └─────────────────────────────────────────┘")

    # 找出最有操作价值的信号
    print(f"\n  📌 关键信号:")

    signals = []

    # 信号1: 极端缩量杀跌
    if chg_20d < -30:
        signals.append(f"20日新低单日骤降{abs(chg_20d):.0f}%，可能是恐慌释放后的暂时喘息，需明日确认是否持续")

    # 信号2: 多年极端
    l_inds = get_industry_data(l_alltime)
    extreme_inds = [(r["industry"], r["daily_counts"][0] / max(r["total"], 1) * 100)
                    for r in l_inds if r["daily_counts"][0] / max(r["total"], 1) > 5]
    if extreme_inds:
        names = ", ".join(f"{ind}({r:.1f}%)" for ind, r in extreme_inds[:3])
        signals.append(f"近7年极端弱势行业: {names} — 这些行业处于多年低位，可能超跌但趋势极弱")

    # 信号3: 行业分化
    l_inds_m = get_industry_data(l_month)
    h_inds_a = get_industry_data(h_alltime)
    l_top3 = set(r["industry"] for r in sorted(l_inds_m, key=lambda x: -x["daily_counts"][0])[:3])
    h_top3 = set(r["industry"] for r in sorted(h_inds_a, key=lambda x: -x["daily_counts"][0])[:3]
                 if r["daily_counts"][0] > 0)
    both = l_top3 & h_top3
    if both:
        signals.append(f"行业极度分化: {', '.join(both)} 内部同时存在历史新高和大量新低个股，轻指数重个股")

    # 信号4: 资金共识
    only_sell = l_top3 - h_top3
    if only_sell and cnt_ath < 20:
        signals.append(f"资金共识偏空: 历史新高仅{cnt_ath}只，弱势集中于{', '.join(list(only_sell)[:3])}等，短期内这些行业不宜抄底")

    for i, s in enumerate(signals, 1):
        print(f"  {i}. {s}")

    if not signals:
        print(f"  无明显信号，市场平淡")


def main():
    highs, lows = load_all()
    sector_rotation(highs, lows)
    trend_analysis(highs, lows)
    multi_tf_resonance(highs, lows)
    daily_summary(highs, lows)


if __name__ == "__main__":
    main()
