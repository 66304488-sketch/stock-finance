"""
市场分析复盘脚本
- 行业轮动：新高/新低集中行业对比，找资金流向
- 趋势变化：新低家数日环比，判断杀跌加速/放缓
- 多周期共振：同一行业在多个窗口同时出现新高/新低

使用: python analyze_market.py
"""

import json
import os
import sys

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
    """加载所有新高/新低数据"""
    highs = {}
    lows = {}
    for t in TYPES:
        highs[t] = load_json(f"new_highs_data_{t}.json")
        lows[t] = load_json(f"new_lows_data_{t}.json")
    return highs, lows


# =========================================================================
# 3. 行业轮动分析
# =========================================================================

def analyze_sector_rotation(highs, lows):
    """对比最新一日新高/新低集中行业，识别资金流向"""
    print("=" * 60)
    print("【行业轮动】最新交易日 新高 vs 新低 行业分布")
    print("=" * 60)

    for t in TYPES:
        h_data = highs[t]
        l_data = lows[t]
        if not h_data or not l_data:
            continue

        h_industries = [r for r in h_data["industries"] if not r.get("is_total")]
        l_industries = [r for r in l_data["industries"] if not r.get("is_total")]

        latest_date = h_data["dates"][0]["label"]

        # 最新一日各行业新高数 / 新低数
        h_map = {r["industry"]: r["daily_counts"][0] for r in h_industries}
        l_map = {r["industry"]: r["daily_counts"][0] for r in l_industries}

        # 偏多行业：新高多、新低少
        # 偏空行业：新低多、新高少
        all_inds = set(list(h_map.keys()) + list(l_map.keys()))
        scores = {}
        for ind in all_inds:
            hc = h_map.get(ind, 0)
            lc = l_map.get(ind, 0)
            h_total = next((r["total"] for r in h_industries if r["industry"] == ind), 1)
            # 占比差：新高占比 - 新低占比，正值偏多
            scores[ind] = round((hc - lc) / h_total * 100, 1)

        # 偏多 top5
        bullish = sorted(scores.items(), key=lambda x: -x[1])[:5]
        bearish = sorted(scores.items(), key=lambda x: x[1])[:5]

        h_total_count = sum(h_map.values())
        l_total_count = sum(l_map.values())

        print(f"\n  {TYPE_CN[t]} ({latest_date})")
        print(f"    全市场: 新高 {h_total_count} 只, 新低 {l_total_count} 只, "
              f"比值 {h_total_count/max(l_total_count,1):.2f}")
        print(f"    偏多行业: {', '.join(f'{ind}({s:+.1f}%)' for ind, s in bullish if s > -999)}")
        print(f"    偏空行业: {', '.join(f'{ind}({s:+.1f}%)' for ind, s in bearish if s < 999)}")


# =========================================================================
# 4. 趋势变化分析
# =========================================================================

def analyze_trend_change(highs, lows):
    """新低家数日环比变化，判断杀跌加速/放缓"""
    print("\n" + "=" * 60)
    print("【趋势变化】新低家数日环比")
    print("=" * 60)

    for t in TYPES:
        l_data = lows[t]
        if not l_data:
            continue

        dates = l_data["dates"]
        total_row = next((r for r in l_data["industries"] if r.get("is_total")), None)
        if not total_row:
            continue

        counts = total_row["daily_counts"]

        print(f"\n  {TYPE_CN[t]}新低:")
        changes = []
        for i in range(len(counts) - 1):
            curr, prev = counts[i], counts[i + 1]
            if prev == 0:
                continue
            pct = round((curr - prev) / prev * 100, 1)
            arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
            changes.append((dates[i]["label"], dates[i + 1]["label"], curr, prev, pct, arrow))
            print(f"    {dates[i]['label']} vs {dates[i+1]['label']}: "
                  f"{prev} → {curr} ({arrow}{abs(pct):.1f}%)")

        # 判断趋势信号：以最新一日变化为主，3日均值为辅
        if changes:
            latest_pct = changes[0][4]  # 最新一日环比
            avg_pct = sum(c[4] for c in changes[:3]) / min(len(changes), 3)
            if latest_pct < -15:
                signal = "杀跌明显放缓，关注反弹"
            elif latest_pct > 15:
                signal = "杀跌加速，注意风险"
            elif avg_pct < -10:
                signal = "近期杀跌持续放缓"
            elif avg_pct > 10:
                signal = "近期杀跌持续加速"
            else:
                signal = "杀跌力度平稳"
            print(f"    最新日环比 {latest_pct:+.1f}%，近3日均值 {avg_pct:+.1f}% → {signal}")


# =========================================================================
# 5. 多周期共振分析
# =========================================================================

def analyze_multi_tf_resonance(highs, lows):
    """同一行业在多个窗口同时出现高占比新高/新低"""
    print("\n" + "=" * 60)
    print("【多周期共振】行业跨窗口新高/新低占比")
    print("=" * 60)

    # 新低共振：同一行业在20/60/120/年窗口都有高新低占比
    print("\n  — 新低多周期共振 (持续弱势) —")
    resonance_lows = compute_resonance(lows, "lows")
    if resonance_lows:
        for ind, ratios in resonance_lows[:8]:
            ratio_str = " | ".join(f"{TYPE_CN[t]}: {r:.1f}%" for t, r in ratios.items())
            print(f"    {ind}: {ratio_str}")
    else:
        print("    无明显多周期共振行业")

    # 新高共振
    print("\n  — 新高多周期共振 (持续强势) —")
    resonance_highs = compute_resonance(highs, "highs")
    if resonance_highs:
        for ind, ratios in resonance_highs[:8]:
            ratio_str = " | ".join(f"{TYPE_CN[t]}: {r:.1f}%" for t, r in ratios.items())
            print(f"    {ind}: {ratio_str}")
    else:
        print("    无明显多周期共振行业")


def compute_resonance(data_dict, direction):
    """
    计算多周期共振：同一行业在不同窗口的占比
    返回 [(行业名, {type: ratio}), ...]，按平均占比排序
    """
    # 收集所有行业 x 类型的最新一日占比
    ind_ratios = {}

    for t in TYPES:
        data = data_dict[t]
        if not data:
            continue
        industries = [r for r in data["industries"] if not r.get("is_total")]
        for r in industries:
            ind = r["industry"]
            total = r["total"]
            count = r["daily_counts"][0] if r["daily_counts"] else 0
            ratio = count / total * 100 if total > 0 else 0
            if ind not in ind_ratios:
                ind_ratios[ind] = {}
            ind_ratios[ind][t] = ratio

    # 筛选：在至少3个窗口占比 > 阈值
    threshold = 3 if direction == "highs" else 5  # 新低阈值稍高
    result = []
    for ind, ratios in ind_ratios.items():
        if len(ratios) >= 3:
            avg = sum(ratios.values()) / len(ratios)
            over_threshold = sum(1 for r in ratios.values() if r > threshold)
            if over_threshold >= 3:
                result.append((ind, ratios, avg))

    result.sort(key=lambda x: -x[2])
    return [(ind, ratios) for ind, ratios, _ in result]


# =========================================================================
# 综合复盘摘要
# =========================================================================

def summary(highs, lows):
    """输出一段简短的每日复盘"""
    print("\n" + "=" * 60)
    print("【每日复盘摘要】")
    print("=" * 60)

    l_month = lows["month"]
    l_alltime = lows["alltime"]
    h_alltime = highs["alltime"]

    if not l_month or not l_alltime or not h_alltime:
        print("  数据不完整")
        return

    date_label = l_month["dates"][0]["label"]
    l_month_total = next((r for r in l_month["industries"] if r.get("is_total")), None)
    l_alltime_total = next((r for r in l_alltime["industries"] if r.get("is_total")), None)
    h_alltime_total = next((r for r in h_alltime["industries"] if r.get("is_total")), None)

    cnt_20d = l_month_total["daily_counts"][0] if l_month_total else 0
    cnt_prev = l_month_total["daily_counts"][1] if l_month_total and len(l_month_total["daily_counts"]) > 1 else 0
    cnt_7y = l_alltime_total["daily_counts"][0] if l_alltime_total else 0
    cnt_ath = h_alltime_total["daily_counts"][0] if h_alltime_total else 0

    change_pct = round((cnt_20d - cnt_prev) / max(cnt_prev, 1) * 100, 1)
    change_word = "减少" if change_pct < 0 else "增加"

    # 判断整体基调
    if cnt_ath > cnt_7y:
       基调 = "偏多"
    elif cnt_7y > cnt_ath * 2:
       基调 = "偏空"
    else:
       基调 = "震荡"

    print(f"\n  {date_label} 复盘")
    print(f"  20日新低: {cnt_20d} 只 (较前日{change_word} {abs(change_pct):.1f}%)")
    print(f"  近7年新低: {cnt_7y} 只")
    print(f"  历史新高: {cnt_ath} 只")
    print(f"  市场基调: {基调}")
    print(f"  新高/近7年新低比值: {cnt_ath}/{cnt_7y} = {cnt_ath/max(cnt_7y,1):.2f}")

    # 找出同时出现在新高top和新低top的行业（分化）
    l_ind = [r for r in l_month["industries"] if not r.get("is_total")]
    h_ind = [r for r in h_alltime["industries"] if not r.get("is_total")]
    l_top5 = set(r["industry"] for r in sorted(l_ind, key=lambda x: -x["daily_counts"][0])[:5])
    h_top5 = set(r["industry"] for r in sorted(h_ind, key=lambda x: -x["daily_counts"][0])[:5] if r["daily_counts"][0] > 0)

    if h_top5:
        print(f"  历史新高集中: {', '.join(h_top5)}")
    if l_top5:
        print(f"  20日新低集中: {', '.join(l_top5)}")
    if h_top5 & l_top5:
        print(f"  ⚠ 行业分化: {', '.join(h_top5 & l_top5)} 同时出现在新高和新低前列")


def main():
    highs, lows = load_all()
    analyze_sector_rotation(highs, lows)
    analyze_trend_change(highs, lows)
    analyze_multi_tf_resonance(highs, lows)
    summary(highs, lows)


if __name__ == "__main__":
    main()
