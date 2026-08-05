#!/usr/bin/env python3
"""从明细 JSON 预计算每只股票在各周期的创新高/新低天数，输出轻量计数文件。

输出:
  static/highs_period_counts.json  → {"000001": [3,5,8,12], ...}
  static/lows_period_counts.json   → {"000001": [1,2,4,6], ...}
  数组顺序: [20d, 60d, 120d, 1year]
"""

import json, os, sys
from collections import defaultdict

from runtime_paths import data_path

PERIODS = [
    ("20d", "month"),
    ("60d", "60d"),
    ("120d", "120d"),
    ("1year", "1year"),
]


def count_period(detail_file: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    with open(detail_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    for ind_name, by_date in data.items():
        if ind_name == "全市场合计":
            continue  # 避免重复计数
        for date_label, stocks in by_date.items():
            for s in stocks:
                counts[s["code"]] += 1
    return dict(counts)


def main():
    for direction, prefix in [("highs", "new_highs"), ("lows", "new_lows")]:
        all_codes: set[str] = set()
        period_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        for i, (key, suffix) in enumerate(PERIODS):
            path = data_path(f"{prefix}_details_{suffix}.json")
            if not os.path.exists(path):
                print(f"  skip {path} (not found)")
                continue
            print(f"  {direction}/{key}: {path} ...")
            counts = count_period(path)
            all_codes.update(counts.keys())
            for code, cnt in counts.items():
                period_counts[code][i] = cnt
        out_path = data_path(f"{direction}_period_counts.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(period_counts, f, ensure_ascii=False, separators=(",", ":"))
        print(f"  → {out_path} ({len(period_counts)} codes, {os.path.getsize(out_path)} bytes)")


if __name__ == "__main__":
    main()
