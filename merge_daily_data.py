"""
增量数据更新脚本：基于共享 K 线缓存，拉取最新交易日数据
- K 线缓存做增量更新（秒级），股票计算在所有日期上重新运行
- 同时生成申万(SW)和同花顺(THS)两套行业分类数据
- 用法: python merge_daily_data.py [--date 20260629]
"""

import json
import os
import sys
import subprocess
from datetime import datetime

STATIC = os.path.join(os.path.dirname(__file__), "static")
TYPES = ["month", "60d", "120d", "1year", "alltime"]
SCHEMES = [
    ("sw", ""),      # 申万2021
    ("ths", "_ths"), # 同花顺
]


def get_all_trade_dates(n=20):
    """获取最近 N 个交易日列表（逗号分隔字符串）。日历获取失败直接退出，
    不能回退成 [today]——周末/节假日会写入非交易日行。"""
    import akshare as ak
    import pandas as pd
    try:
        df = ak.tool_trade_date_hist_sina()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        now = pd.Timestamp.now()
        dates = df[df["trade_date"] <= now].tail(n)["trade_date"].dt.strftime("%Y%m%d").tolist()
    except Exception as exc:
        print(f"获取交易日历失败，拒绝写入可疑数据: {exc}", file=sys.stderr)
        sys.exit(1)
    if not dates:
        print("交易日历为空，拒绝写入可疑数据", file=sys.stderr)
        sys.exit(1)
    return dates


def get_latest_trading_date():
    """获取最新的实际交易日"""
    dates = get_all_trade_dates(1)
    return dates[0] if dates else datetime.now().strftime("%Y%m%d")


def run_script(script, args):
    """运行 Python 脚本并返回是否成功"""
    r = subprocess.run(
        [sys.executable, script] + args,
        cwd=os.path.dirname(__file__),
        capture_output=True, text=True,
        timeout=600,
    )
    ok = r.returncode == 0
    if not ok:
        print(f"  ❌ {script} 失败: {r.stderr[-300:]}")
    return ok, r.stdout, r.stderr


def print_data_summary(label, scheme_suffix):
    """打印某个行业分类的数据摘要"""
    for t in TYPES:
        path = os.path.join(STATIC, f"{label}_data_{t}{scheme_suffix}.json")
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            total = 0
            if data.get("dates"):
                for row in data.get("industries", []):
                    if row.get("is_total") and row.get("daily_counts"):
                        total = row["daily_counts"][0]
                        break
            print(f"  [{t}] ✅ {len(data.get('dates',[]))}天 {total}只")


def main():
    target_date = None
    if len(sys.argv) > 1 and sys.argv[1] == "--date":
        target_date = sys.argv[2]

    if not target_date:
        target_date = get_latest_trading_date()

    all_dates = get_all_trade_dates(20)
    dates_arg = ",".join(all_dates)

    print(f"增量更新 - 最新交易日: {target_date}")
    print(f"计算范围: {len(all_dates)} 个交易日 ({all_dates[0]} ~ {all_dates[-1]})")
    print()

    # 1. 新高数据（SW + THS，共享 K 线缓存）
    for scheme, suffix in SCHEMES:
        label = "申万" if scheme == "sw" else "同花顺"
        print(f"=== 新高数据 ({label}) ===")
        ok, stdout, stderr = run_script("fetch_new_highs.py", [
            "--type", "all",
            "--dates", dates_arg,
            "--industry-scheme", scheme,
        ])
        if ok:
            print_data_summary("new_highs", suffix)
        else:
            print(f"  ❌ {label}新高数据获取失败")

    # 2. 新低数据（SW + THS，复用 K 线缓存）
    for scheme, suffix in SCHEMES:
        label = "申万" if scheme == "sw" else "同花顺"
        print(f"\n=== 新低数据 ({label}) ===")
        ok, stdout, stderr = run_script("fetch_new_lows.py", [
            "--type", "all",
            "--dates", dates_arg,
            "--industry-scheme", scheme,
        ])
        if ok:
            print_data_summary("new_lows", suffix)
        else:
            print(f"  ❌ {label}新低数据获取失败")

    # 3. 资金流向（SW + THS）
    for scheme, suffix in SCHEMES:
        label = "申万" if scheme == "sw" else "同花顺"
        print(f"\n=== 资金流向 ({label}) ===")
        ok, stdout, stderr = run_script("fetch_capital_flow.py", [
            "--dates", dates_arg,
            "--mode", "missing",
            "--industry-scheme", scheme,
        ])
        if ok:
            path = os.path.join(STATIC, f"capital_flow{suffix}.json")
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                print(f"  ✅ {len(data.get('dates',[]))}天 全市场成交: {data.get('total_turnover',0)/1e8:.0f}亿")
        else:
            print(f"  ❌ {label}资金流向获取失败")

    # 3.5 行业市值（SW + THS）
    for scheme, suffix in SCHEMES:
        label = "申万" if scheme == "sw" else "同花顺"
        print(f"\n=== 行业市值 ({label}) ===")
        ok, stdout, stderr = run_script("fetch_market_cap.py", [
            "--dates", dates_arg,
            "--mode", "missing",
            "--industry-scheme", scheme,
        ])
        if ok:
            path = os.path.join(STATIC, f"market_cap{suffix}.json")
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                print(f"  ✅ {len(data.get('dates',[]))}天 全市场市值: {data.get('total_mcap',0)/1e8:.0f}亿")
        else:
            print(f"  ❌ {label}市值获取失败")

    # 4. AI 分析
    print("\n=== AI 分析 ===")
    ok, stdout, stderr = run_script("ai_analyzer.py", [])
    print(f"  {'✅' if ok else '❌'}")

    # 5. 生成独立 HTML
    print("\n=== 独立 HTML ===")
    ok, stdout, stderr = run_script("generate_standalone.py", [])
    print(f"  {'✅' if ok else '❌'}")

    print(f"\n✅ 增量更新完成 ({target_date})")


if __name__ == "__main__":
    main()
