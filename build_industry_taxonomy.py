"""Build a local SW level-2/3 classification cache for index constituents."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from index_constituents import INDEXES, _fetch_constituents
from kline_cache import get_active_codes


ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(ROOT, "static", "industry_taxonomy.json")
F10_URL = "https://basic.10jqka.com.cn/{code}/field.html"


def fetch_sw_levels(code: str) -> dict[str, str] | None:
    response = requests.get(
        F10_URL.format(code=code),
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=(4, 12),
    )
    response.raise_for_status()
    response.encoding = "gbk"
    node = BeautifulSoup(response.text, "lxml").select_one("p.threecate span.tip")
    if not node:
        return None
    label = re.split(r"\s*（共", node.get_text(" ", strip=True), maxsplit=1)[0]
    levels = [part.strip() for part in label.split("--") if part.strip()]
    if not levels:
        return None
    return {
        "sw_level1": levels[0],
        "sw_level2": levels[1] if len(levels) > 1 else "",
        "sw_level3": levels[2] if len(levels) > 2 else "",
    }


def _load_existing(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_dump(data: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="industry_taxonomy.", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def build(output: str = DEFAULT_OUTPUT, workers: int = 16) -> dict:
    index_codes = sorted({
        stock["code"]
        for index_code in INDEXES
        for stock in _fetch_constituents(index_code)
    })
    # The three index lists seed the cache, while the detailed pages need a
    # complete market mapping so heatmap/flow/market-cap rows do not fall back
    # to a mixed or incomplete classification.
    codes = sorted(set(index_codes) | set(get_active_codes()))
    existing = _load_existing(output)
    stocks = existing.get("stocks") if isinstance(existing.get("stocks"), dict) else {}
    pending = [code for code in codes if not stocks.get(code)]
    failures = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_sw_levels, code): code for code in pending}
        for completed, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                details = future.result()
                if details:
                    stocks[code] = details
                else:
                    failures.append(code)
            except Exception:
                failures.append(code)
            if completed % 100 == 0 or completed == len(pending):
                print(f"  已处理 {completed}/{len(pending)}")

    data = {
        "version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "同花顺 F10 三级行业分类（全市场）",
        "index_codes": list(INDEXES),
        "requested": len(codes),
        "covered": sum(1 for code in codes if stocks.get(code)),
        "failed_codes": failures,
        "stocks": stocks,
    }
    _atomic_dump(data, output)
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    result = build(args.output, max(1, args.workers))
    print(f"分类缓存完成: {result['covered']}/{result['requested']} -> {args.output}")


if __name__ == "__main__":
    main()
