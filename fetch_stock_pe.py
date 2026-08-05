#!/usr/bin/env python3
"""刷新全市场 A 股动态市盈率,输出 stock_pe.json 供热力图个股明细展示。"""

import json
import os

import akshare as ak

from runtime_paths import data_path


def update_stock_pe() -> int:
    df = ak.stock_zh_a_spot_em()
    out = {}
    for code, pe in zip(df["代码"], df["市盈率-动态"]):
        try:
            value = float(pe)
        except (TypeError, ValueError):
            continue
        if value > 0:  # 亏损/停牌为负或空,不展示
            out[str(code).zfill(6)] = round(value, 2)
    path = data_path("stock_pe.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    print(f"[pe] {len(out)} 只市盈率已刷新")
    return len(out)


if __name__ == "__main__":
    update_stock_pe()
