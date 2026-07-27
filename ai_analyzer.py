"""
AI 市场分析脚本
- 复用 analyze_market.py 的指标计算
- 将结构化数据发给 Claude 生成自然语言分析报告
- 报告存入 static/ai_report_latest.json 和 SQLite 历史

使用: python ai_analyzer.py
环境变量: ANTHROPIC_API_KEY (可选，若不设则用规则引擎 fallback)
"""

import json
import os
import sys
import sqlite3
import copy
from datetime import datetime

# 复用 analyze_market 的数据加载和指标计算
from analyze_market import (
    load_all, STATIC, TYPES,
    get_industry_data, get_total_row,
)
from mcp_config import build_anthropic_mcp_parts
from runtime_paths import user_data_path

DB_PATH = user_data_path("history.db")


def _align_to_common_latest(highs, lows):
    datasets = [data for group in (highs, lows) for data in group.values() if data and data.get("dates")]
    if not datasets:
        return highs, lows, None
    common = None
    for data in datasets:
        keys = {item.get("full_label") or item.get("label") for item in data["dates"]}
        common = keys if common is None else common & keys
    if not common:
        return highs, lows, None
    ordered = [item.get("full_label") or item.get("label") for item in datasets[0]["dates"]]
    selected = next((key for key in ordered if key in common), None)
    aligned_groups = []
    for group in (highs, lows):
        aligned = {}
        for period, original in group.items():
            if not original or not original.get("dates"):
                aligned[period] = original
                continue
            data = copy.deepcopy(original)
            keys = [item.get("full_label") or item.get("label") for item in data["dates"]]
            index = keys.index(selected)
            data["dates"] = data["dates"][index:]
            for row in data.get("industries", []):
                row["daily_counts"] = (row.get("daily_counts") or [])[index:]
            aligned[period] = data
        aligned_groups.append(aligned)
    return aligned_groups[0], aligned_groups[1], selected


def ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            date TEXT PRIMARY KEY,
            market_tone TEXT,
            summary TEXT,
            full_report TEXT,
            metrics_json TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def compute_metrics(highs, lows):
    """计算所有分析指标，返回结构化字典"""
    metrics = {"timestamp": datetime.now().isoformat()}
    highs, lows, common_date = _align_to_common_latest(highs, lows)
    if not common_date:
        metrics["data_quality_error"] = "各周期没有共同交易日"
        return metrics

    # 获取最新日期
    l_month = lows["month"]
    if not l_month or not l_month.get("dates"):
        return metrics
    date_label = l_month["dates"][0]["label"]
    metrics["date"] = date_label

    # ── 全局总数 ──
    for t in TYPES:
        h_total = get_total_row(highs[t])
        l_total = get_total_row(lows[t])
        metrics[f"highs_{t}_total"] = h_total["daily_counts"][0] if h_total else 0
        metrics[f"lows_{t}_total"] = l_total["daily_counts"][0] if l_total else 0

        if h_total and len(h_total["daily_counts"]) >= 2:
            prev = h_total["daily_counts"][1]
            curr = h_total["daily_counts"][0]
            metrics[f"highs_{t}_change_pct"] = round((curr - prev) / max(prev, 1) * 100, 1)

        if l_total and len(l_total["daily_counts"]) >= 2:
            prev = l_total["daily_counts"][1]
            curr = l_total["daily_counts"][0]
            metrics[f"lows_{t}_change_pct"] = round((curr - prev) / max(prev, 1) * 100, 1)

    # ── 行业级数据（最高/最低的行业） ──
    l_inds = {t: get_industry_data(lows[t]) for t in TYPES}
    h_inds = {t: get_industry_data(highs[t]) for t in TYPES}

    # 每个行业的新高/新低占比
    industry_stats = {}
    for r in l_inds["month"]:
        ind = r["industry"]
        total = r["total"]
        l_count = r["daily_counts"][0]
        l_ratio = l_count / max(total, 1) * 100
        industry_stats[ind] = {
            "total_stocks": total,
            "lows_20d_count": l_count,
            "lows_20d_ratio": round(l_ratio, 1),
        }

    for r in h_inds["month"]:
        ind = r["industry"]
        if ind not in industry_stats:
            industry_stats[ind] = {"total_stocks": r["total"]}
        h_count = r["daily_counts"][0]
        h_ratio = h_count / max(r["total"], 1) * 100
        industry_stats[ind]["highs_20d_count"] = h_count
        industry_stats[ind]["highs_20d_ratio"] = round(h_ratio, 1)

    # 多周期共振行业
    for t in TYPES:
        for r in h_inds[t]:
            ind = r["industry"]
            ratio = r["daily_counts"][0] / max(r["total"], 1) * 100
            if ind not in industry_stats:
                industry_stats[ind] = {}
            industry_stats[ind][f"highs_{t}_ratio"] = round(ratio, 1)
        for r in l_inds[t]:
            ind = r["industry"]
            ratio = r["daily_counts"][0] / max(r["total"], 1) * 100
            industry_stats[ind][f"lows_{t}_ratio"] = round(ratio, 1)

    metrics["industries"] = industry_stats

    # ── Top/Bottom 排行 ──
    top_highs = sorted(
        [(ind, s.get("highs_20d_ratio", 0)) for ind, s in industry_stats.items() if s.get("highs_20d_ratio", 0) > 0],
        key=lambda x: -x[1],
    )
    top_lows = sorted(
        [(ind, s.get("lows_20d_ratio", 0)) for ind, s in industry_stats.items() if s.get("lows_20d_ratio", 0) > 0],
        key=lambda x: -x[1],
    )
    metrics["top_highs_industries"] = top_highs[:6]
    metrics["top_lows_industries"] = top_lows[:6]

    # ── 趋势扩散共识（基于新高/新低宽度，不代表真实资金流） ──
    inflow = []
    outflow = []
    for ind, s in industry_stats.items():
        h = s.get("highs_20d_ratio", 0)
        l = s.get("lows_20d_ratio", 0)
        if h > 5 and l < 5:
            inflow.append((ind, h, l))
        if l > 20 and h < 2:
            outflow.append((ind, h, l))
    metrics["capital_inflow"] = sorted(inflow, key=lambda x: -x[1])
    metrics["capital_outflow"] = sorted(outflow, key=lambda x: -x[2])

    # ── 日环比变化 ──
    if len(l_month["dates"]) >= 2:
        improving = []
        worsening = []
        for r in l_inds["month"]:
            ind = r["industry"]
            h_row = next((x for x in h_inds["month"] if x["industry"] == ind), None)
            h_curr = h_row["daily_counts"][0] if h_row else 0
            h_prev = h_row["daily_counts"][1] if h_row and len(h_row["daily_counts"]) > 1 else 0
            l_curr = r["daily_counts"][0]
            l_prev = r["daily_counts"][1] if len(r["daily_counts"]) > 1 else 0
            total = r["total"]
            score = (h_curr - h_prev - (l_curr - l_prev)) / max(total, 1) * 100
            if score > 10:
                improving.append((ind, round(score, 1), h_curr - h_prev, l_curr - l_prev))
            elif score < -15:
                worsening.append((ind, round(score, 1), h_curr - h_prev, l_curr - l_prev))
        metrics["day_over_day_improving"] = sorted(improving, key=lambda x: -x[1])[:5]
        metrics["day_over_day_worsening"] = sorted(worsening, key=lambda x: x[1])[:5]

    # ── 新高/新低比 ──
    ath = metrics.get("highs_alltime_total", 0)
    l_7y = metrics.get("lows_alltime_total", 0)
    metrics["high_low_ratio"] = round(ath / max(l_7y, 1), 2)

    # ── 市场基调 ──
    if metrics["high_low_ratio"] >= 2:
        metrics["market_tone"] = "强势"
    elif metrics["high_low_ratio"] >= 1:
        metrics["market_tone"] = "偏多"
    elif metrics["high_low_ratio"] >= 0.5:
        metrics["market_tone"] = "震荡"
    elif metrics["high_low_ratio"] >= 0.2:
        metrics["market_tone"] = "偏空"
    else:
        metrics["market_tone"] = "弱势"

    return metrics


def call_llm(metrics):
    """调用 LLM API 生成分析报告 (支持 Anthropic / DeepSeek)"""
    config_file = user_data_path("config.json")
    cfg = {}
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            cfg = json.load(f)

    api_key = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("api_key", "")
    if not api_key:
        return None

    provider = cfg.get("ai_provider", "anthropic")

    # 构建简洁的数据上下文
    ctx_lines = [f"日期: {metrics.get('date', '未知')}"]
    ctx_lines.append(f"市场基调: {metrics.get('market_tone', '未知')}")
    ctx_lines.append("\n全市场总量:")
    ctx_lines.append(f"  20日新低: {metrics.get('lows_month_total', '?')}只 (日环比 {metrics.get('lows_month_change_pct', '?')}%)")
    ctx_lines.append(f"  一年新低: {metrics.get('lows_1year_total', '?')}只")
    ctx_lines.append(f"  近7年新低: {metrics.get('lows_alltime_total', '?')}只")
    ctx_lines.append(f"  历史新高: {metrics.get('highs_alltime_total', '?')}只")
    ctx_lines.append(f"  新高/新低比: {metrics.get('high_low_ratio', '?')}")

    ctx_lines.append(f"\n趋势扩散偏强: {', '.join(f'{ind}({h:.0f}%/{l:.0f}%)' for ind, h, l in metrics.get('capital_inflow', [])) or '无'}")
    ctx_lines.append(f"趋势扩散偏弱: {', '.join(f'{ind}({l:.0f}%/{h:.0f}%)' for ind, h, l in metrics.get('capital_outflow', [])) or '无'}")

    ctx_lines.append(f"\n新高最强行业: {', '.join(f'{ind}({r:.1f}%)' for ind, r in metrics.get('top_highs_industries', [])[:5])}")
    ctx_lines.append(f"新低最严重行业: {', '.join(f'{ind}({r:.1f}%)' for ind, r in metrics.get('top_lows_industries', [])[:5])}")

    if metrics.get("day_over_day_improving"):
        ctx_lines.append(f"\n日环比改善: {', '.join(f'{ind}({s:+.1f})' for ind, s, h, l in metrics['day_over_day_improving'])}")
    if metrics.get("day_over_day_worsening"):
        ctx_lines.append(f"日环比恶化: {', '.join(f'{ind}({s:+.1f})' for ind, s, h, l in metrics['day_over_day_worsening'])}")

    # 行业详细数据（精简）
    ctx_lines.append("\n行业详细 (20日新高%/新低%):")
    for ind, s in sorted(metrics.get("industries", {}).items(),
                         key=lambda x: -(x[1].get("highs_20d_ratio", 0) + x[1].get("lows_20d_ratio", 0)))[:15]:
        h = s.get("highs_20d_ratio", 0)
        l = s.get("lows_20d_ratio", 0)
        ctx_lines.append(f"  {ind}: 新高{h:.0f}% / 新低{l:.0f}% (共{s.get('total_stocks', '?')}只)")

    data_context = "\n".join(ctx_lines)

    prompt = f"""你是专业的A股市场分析师。根据以下今日市场数据，生成一份简洁的复盘报告。

{data_context}

请生成一份200-400字的报告，包含:
1. **市场总览**: 一句话概括今日基调
2. **行业扩散**: 哪些行业新高扩散或新低扩散（不要表述成真实资金流）
3. **关键信号**: 2-3个值得关注的信号
4. **风险提示**: 明日需要关注什么

要求: 具体（列数字不泛泛而谈）、诚实（信号不明确时直说）、简洁"""

    system_msg = "你是一位专业的A股市场分析师。你的分析必须基于给定的数据，使用具体数字，不编造。语言简洁有力。"

    try:
        if provider == "deepseek":
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=1000,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
            )
            return resp.choices[0].message.content
        else:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key)
            mcp_parts = build_anthropic_mcp_parts(cfg)
            if mcp_parts["mcp_servers"]:
                resp = client.beta.messages.create(
                    model="claude-opus-4-8",
                    max_tokens=1000,
                    system=system_msg,
                    messages=[{"role": "user", "content": prompt}],
                    betas=mcp_parts["betas"],
                    mcp_servers=mcp_parts["mcp_servers"],
                    tools=mcp_parts["tools"],
                )
            else:
                resp = client.messages.create(
                    model="claude-opus-4-8",
                    max_tokens=1000,
                    system=system_msg,
                    messages=[{"role": "user", "content": prompt}],
                )
            return resp.content[0].text if resp.content else ""
    except Exception as e:
        print(f"  LLM API 调用失败: {e}")
        return None


def build_rule_report(metrics):
    strong = metrics.get("top_highs_industries", [])[:3]
    weak = metrics.get("top_lows_industries", [])[:3]
    strong_text = "、".join(f"{name}({ratio:.1f}%)" for name, ratio in strong) or "无明显行业"
    weak_text = "、".join(f"{name}({ratio:.1f}%)" for name, ratio in weak) or "无明显行业"
    return (
        f"**市场总览**\n{metrics.get('date', '最新交易日')}市场基调为{metrics.get('market_tone', '未知')}。"
        f"20日新高{metrics.get('highs_month_total', 0)}只，20日新低{metrics.get('lows_month_total', 0)}只。\n\n"
        f"**行业扩散**\n新高扩散靠前：{strong_text}；新低扩散靠前：{weak_text}。"
        "这些是价格宽度信号，不等同于真实资金净流入。\n\n"
        f"**风险提示**\n历史新高{metrics.get('highs_alltime_total', 0)}只，近7年新低"
        f"{metrics.get('lows_alltime_total', 0)}只；关注强弱行业扩散是否在下一交易日延续。"
    )


def main(use_llm=True):
    print("AI 市场分析...")

    # 加载数据
    highs, lows = load_all()
    if not highs.get("month") or not lows.get("month"):
        print("  错误: 数据不完整，请先运行 fetch_new_highs.py 和 fetch_new_lows.py")
        sys.exit(1)

    # 计算指标
    metrics = compute_metrics(highs, lows)
    date_label = metrics.get("date", "unknown")

    # 尝试 AI 分析；失败时明确记录规则引擎回退，避免界面误称为 AI 结果。
    llm_report = call_llm(metrics) if use_llm else None
    analysis_source = "llm" if llm_report else "rules"
    ai_report = llm_report or build_rule_report(metrics)

    # 保存报告
    report_data = {
        "date": date_label,
        "market_tone": metrics.get("market_tone", "未知"),
        "metrics": metrics,
        "ai_report": ai_report,
        "analysis_source": analysis_source,
        "generated_at": datetime.now().isoformat(),
    }

    report_path = os.path.join(STATIC, "ai_report_latest.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)
    print(f"  报告已保存: {report_path}")

    # 存入 SQLite
    conn = None
    try:
        conn = ensure_db()
        conn.execute(
            "INSERT OR REPLACE INTO daily_reports (date, market_tone, summary, full_report, metrics_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date_label, metrics.get("market_tone"), "",
             ai_report or "AI 分析未生成（请设置 ANTHROPIC_API_KEY 环境变量）",
             json.dumps(metrics, ensure_ascii=False),
             datetime.now().isoformat()),
        )
        conn.commit()
    except Exception as e:
        print(f"  警告: DB 写入失败: {e}")
    finally:
        if conn:
            conn.close()

    # 输出摘要
    print(f"\n  {date_label} | 基调: {metrics.get('market_tone', '?')}")
    if ai_report:
        print(f"\n{ai_report}")
    else:
        print("  (未设置 ANTHROPIC_API_KEY，跳过了 AI 分析。仅保存了指标数据。)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics-only", action="store_true")
    args = parser.parse_args()
    main(use_llm=not args.metrics_only)
