#!/usr/bin/env python3
"""Build intraday high/low snapshots from one real-time market fetch."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd

from kline_cache import KlineCache, fetch_spot, get_active_codes, load_industry_map
from index_constituents import _citic_industry, _load_sw_detail_map
from market_temperature import compute_intraday_temperature, limit_threshold, load_temperature_history
from runtime_paths import DATA_DIR, data_path, resource_path

STATIC = DATA_DIR
MIN_WINDOW_DAYS = 5
MAX_WINDOW_DAYS = 250
PRESET_WINDOWS = {"20d": 20, "60d": 60, "120d": 120, "1year": 250}


def window_key(window_days: int) -> str:
    for key, value in PRESET_WINDOWS.items():
        if value == window_days:
            return key
    return f"{window_days}d"


def scheme_suffix(scheme: str) -> str:
    return {"sw": "", "ths": "_ths", "sw3": "_sw3", "citic": "_citic"}[scheme]


def parse_window(value: str | int) -> int:
    if isinstance(value, int):
        days = value
    else:
        raw = str(value).strip().lower()
        if raw in PRESET_WINDOWS:
            days = PRESET_WINDOWS[raw]
        else:
            days = int(raw[:-1] if raw.endswith("d") else raw)
    if not MIN_WINDOW_DAYS <= days <= MAX_WINDOW_DAYS:
        raise ValueError(f"window must be between {MIN_WINDOW_DAYS} and {MAX_WINDOW_DAYS}")
    return days


def _atomic_json_dump(data: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def load_industry_maps(active_codes: list[str], schemes: list[str]) -> dict[str, dict[str, str]]:
    sw_map = load_industry_map(active_codes)
    result = {"sw": sw_map}
    if "ths" in schemes:
        ths_raw = _load_json(resource_path("industry_map_ths.json"), {})
        result["ths"] = {
            code: ths_raw.get(code) or sw_map.get(code) or "其他"
            for code in active_codes
        }
    if "citic" in schemes:
        result["citic"] = {
            code: _citic_industry(None, sw_map.get(code)) or "其他"
            for code in active_codes
        }
    if "sw3" in schemes:
        result["sw3"] = _load_sw_detail_map(active_codes)
    return {scheme: result[scheme] for scheme in schemes}


def _load_shares() -> dict[str, int]:
    raw = _load_json(data_path("stock_shares.json"), {})
    if not isinstance(raw, dict):
        return {}
    # v3 stores separate total/circulating dictionaries. The heatmap's
    # "industry market cap" is total market cap, so prefer total shares.
    if isinstance(raw.get("total_shares"), dict):
        raw = raw["total_shares"]
    elif isinstance(raw.get("circulating_shares"), dict):
        raw = raw["circulating_shares"]
    shares = {}
    for code, value in raw.items():
        if not re.fullmatch(r"\d{6}", str(code)):
            continue
        try:
            shares[code] = int(value)
        except (TypeError, ValueError):
            continue
    return shares


def _load_first_seen(output_dir: str, scheme: str, days: int, direction: str) -> dict[str, str]:
    suffix = scheme_suffix(scheme)
    path = os.path.join(output_dir, f"intraday_{direction}_{window_key(days)}{suffix}.json")
    previous = _load_json(path, {})
    result = {}
    for row in previous.get("industries", []):
        for stocks in (row.get("daily_details") or {}).values():
            for stock in stocks:
                if stock.get("code") and stock.get("first_seen_at"):
                    result[stock["code"]] = stock["first_seen_at"]
    return result


def _signal_entry(
    code: str,
    spot: dict[str, Any],
    shares: int | None,
    direction: str,
    price_threshold: float,
    close_threshold: float,
    touched: bool,
    standing: bool,
    scan_time: str,
) -> dict[str, Any]:
    price = float(spot["close"])
    day_extreme = float(spot["high"] if direction == "highs" else spot["low"])
    prev_close = float(spot.get("prev_close") or 0)
    if direction == "highs":
        break_pct = (day_extreme / price_threshold - 1) * 100 if price_threshold else 0
        standing_pct = (price / close_threshold - 1) * 100 if close_threshold else 0
        pullback_pct = (price / day_extreme - 1) * 100 if day_extreme else 0
        retained = price > price_threshold
    else:
        break_pct = (day_extreme / price_threshold - 1) * 100 if price_threshold else 0
        standing_pct = (price / close_threshold - 1) * 100 if close_threshold else 0
        pullback_pct = (price / day_extreme - 1) * 100 if day_extreme else 0
        retained = price < price_threshold

    if touched and standing:
        status = "touched_and_standing"
    elif touched:
        status = "touched_pulled_back"
    else:
        status = "close_candidate"

    mcap = round(price * shares) if shares else None
    mcap_change = round((price - prev_close) * shares) if shares and prev_close else None
    return {
        "code": code,
        "name": spot.get("name", ""),
        "price": round(price, 2),
        "day_high": round(float(spot.get("high") or price), 2),
        "day_low": round(float(spot.get("low") or price), 2),
        "change_pct": round(float(spot.get("change_pct") or 0), 2),
        "prev_close": round(prev_close, 2),
        "price_threshold": round(price_threshold, 2),
        "close_threshold": round(close_threshold, 2),
        "break_pct": round(break_pct, 2),
        "standing_break_pct": round(standing_pct, 2),
        "pullback_pct": round(pullback_pct, 2),
        "touched": touched,
        "standing": standing,
        "retained": retained,
        "status": status,
        "first_seen_at": scan_time,
        "last_seen_at": scan_time,
        "mcap": mcap,
        "mcap_change": mcap_change,
    }


def _collect_signals(
    active_codes: list[str],
    spot_data: dict[str, dict[str, Any]],
    ohlcv: dict[str, Any],
    windows: list[int],
    shares: dict[str, int],
    scan_time: str,
) -> tuple[dict[int, dict[str, dict[str, dict]]], dict[str, float]]:
    today = pd.Timestamp(datetime.now().date())
    signals = {
        days: {"highs": {}, "lows": {}}
        for days in windows
    }
    market = {"mcap": 0.0, "mcap_change": 0.0, "spot_count": 0, "mcap_count": 0,
              "up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0,
              "big_up": 0, "big_down": 0, "amount": 0.0}

    for code in active_codes:
        spot = spot_data.get(code)
        df = ohlcv.get(code)
        if not spot or not spot.get("close") or df is None or df.empty:
            continue
        history = df[df["date"] < today]
        if history.empty:
            continue
        market["spot_count"] += 1
        pct = float(spot.get("change_pct") or 0)
        market["amount"] += float(spot["close"]) * float(spot.get("volume") or 0)
        if pct > 0.01:
            market["up"] += 1
        elif pct < -0.01:
            market["down"] += 1
        else:
            market["flat"] += 1
        thr = limit_threshold(code, spot.get("name"))
        if pct >= thr:
            market["limit_up"] += 1
        elif pct <= -thr:
            market["limit_down"] += 1
        if pct >= 5.0:
            market["big_up"] += 1
        elif pct <= -5.0:
            market["big_down"] += 1
        share_count = shares.get(code)
        if share_count:
            market["mcap_count"] += 1
            market["mcap"] += float(spot["close"]) * share_count
            market["mcap_change"] += (float(spot["close"]) - float(spot.get("prev_close") or 0)) * share_count

        for days in windows:
            if len(history) < days:
                continue
            recent = history.iloc[-days:]
            high_threshold = float(recent["high"].max())
            low_threshold = float(recent["low"].min())
            close_high = float(recent["close"].max())
            close_low = float(recent["close"].min())
            current = float(spot["close"])
            day_high = float(spot.get("high") or current)
            day_low = float(spot.get("low") or current)

            high_touched = day_high > high_threshold
            high_standing = current > close_high
            if high_touched or high_standing:
                signals[days]["highs"][code] = _signal_entry(
                    code, spot, share_count, "highs", high_threshold, close_high,
                    high_touched, high_standing, scan_time,
                )

            low_touched = day_low < low_threshold
            low_standing = current < close_low
            if low_touched or low_standing:
                signals[days]["lows"][code] = _signal_entry(
                    code, spot, share_count, "lows", low_threshold, close_low,
                    low_touched, low_standing, scan_time,
                )
    return signals, market


def _build_output(
    direction: str,
    days: int,
    scheme: str,
    industry_map: dict[str, str],
    signal_map: dict[str, dict],
    spot_data: dict[str, dict[str, Any]],
    shares: dict[str, int],
    scan_time: str,
    previous_first_seen: dict[str, str],
) -> dict[str, Any]:
    date_label = f"{datetime.now().month}月{datetime.now().day}日"
    industry_codes: dict[str, list[str]] = defaultdict(list)
    for code, industry in industry_map.items():
        industry_codes[industry or "其他"].append(code)

    industry_signals: dict[str, list[dict]] = defaultdict(list)
    for code, entry in signal_map.items():
        industry = industry_map.get(code, "其他")
        item = dict(entry)
        item["industry"] = industry
        item["first_seen_at"] = previous_first_seen.get(code, item["first_seen_at"])
        industry_signals[industry].append(item)

    rows = []
    total_mcap = 0.0
    total_mcap_change = 0.0
    for industry in sorted(industry_codes):
        codes = industry_codes[industry]
        stocks = industry_signals.get(industry, [])
        mcap = 0.0
        mcap_change = 0.0
        for code in codes:
            spot = spot_data.get(code)
            share_count = shares.get(code)
            if not spot or not share_count:
                continue
            mcap += float(spot["close"]) * share_count
            mcap_change += (float(spot["close"]) - float(spot.get("prev_close") or 0)) * share_count
        total_mcap += mcap
        total_mcap_change += mcap_change
        if direction == "highs":
            stocks.sort(key=lambda item: (-int(item["standing"]), -float(item["break_pct"])))
        else:
            stocks.sort(key=lambda item: (-int(item["standing"]), float(item["break_pct"])))
        for item in stocks:
            item["industry_mcap_contribution_pct"] = round(
                (item.get("mcap_change") or 0) / abs(mcap_change) * 100, 2
            ) if mcap_change else 0.0
        count = len(stocks)
        touched_count = sum(1 for stock in stocks if stock["touched"])
        standing_count = sum(1 for stock in stocks if stock["standing"])
        retained_count = sum(1 for stock in stocks if stock["retained"])
        rows.append({
            "industry": industry,
            "total": len(codes),
            "ratio": round(count / max(len(codes), 1) * 100, 1),
            "daily_counts": [count],
            "touched_count": touched_count,
            "standing_count": standing_count,
            "retained_count": retained_count,
            "market_cap": round(mcap),
            "market_cap_change": round(mcap_change),
            "daily_details": {date_label: stocks},
        })

    rows.sort(key=lambda row: (-row["daily_counts"][0], row["industry"]))
    total_signals = sum(row["daily_counts"][0] for row in rows)
    total_touched = sum(row["touched_count"] for row in rows)
    total_standing = sum(row["standing_count"] for row in rows)
    total_retained = sum(row["retained_count"] for row in rows)
    rows.append({
        "industry": "全市场合计",
        "total": sum(row["total"] for row in rows),
        "daily_counts": [total_signals],
        "touched_count": total_touched,
        "standing_count": total_standing,
        "retained_count": total_retained,
        "market_cap": round(total_mcap),
        "market_cap_change": round(total_mcap_change),
        "daily_details": {},
        "is_total": True,
    })
    word = "新高" if direction == "highs" else "新低"
    return {
        "dates": [{"label": date_label, "full_label": f"{datetime.now().year}年{date_label}"}],
        "updated_at": scan_time,
        "trade_date": datetime.now().strftime("%Y%m%d"),
        "session": "intraday",
        "scheme": scheme,
        "window_days": days,
        "type": f"intraday_{window_key(days)}",
        "type_label": f"盘中{days}日{word}信号",
        "industries": rows,
    }


def _update_history(output_dir: str, scheme: str, outputs: dict[int, dict[str, dict]], scan_time: str) -> None:
    suffix = scheme_suffix(scheme)
    path = os.path.join(output_dir, f"intraday_history{suffix}.json")
    trade_date = datetime.now().strftime("%Y%m%d")
    history = _load_json(path, {})
    if history.get("trade_date") != trade_date:
        history = {"trade_date": trade_date, "scheme": scheme, "snapshots": []}
    by_window = {}
    for days, pair in outputs.items():
        by_window[str(days)] = {}
        for direction in ("highs", "lows"):
            total = next((row for row in pair[direction]["industries"] if row.get("is_total")), {})
            by_window[str(days)][direction] = {
                "signals": (total.get("daily_counts") or [0])[0],
                "touched": total.get("touched_count", 0),
                "standing": total.get("standing_count", 0),
                "retained": total.get("retained_count", 0),
            }
    snapshot = {"time": scan_time, "by_window": by_window}
    snapshots = history.setdefault("snapshots", [])
    if snapshots and snapshots[-1].get("time") == scan_time:
        snapshots[-1] = snapshot
    else:
        snapshots.append(snapshot)
    history["snapshots"] = snapshots[-300:]
    history["updated_at"] = scan_time
    _atomic_json_dump(history, path)


def _update_temperature_history(output_dir: str, temperature: dict[str, Any], scan_time: str) -> None:
    """把每次扫描的盘中温度追加到当日历史,供温度页画分时波动图。"""
    path = os.path.join(output_dir, "intraday_temperature_history.json")
    trade_date = datetime.now().strftime("%Y%m%d")
    history = _load_json(path, {})
    if history.get("trade_date") != trade_date:
        history = {"trade_date": trade_date, "snapshots": []}
    breadth = temperature.get("breadth") or {}
    snapshot = {
        "time": scan_time,
        "temperature": temperature.get("temperature"),
        "up": breadth.get("up", 0),
        "down": breadth.get("down", 0),
        "limit_up": breadth.get("limit_up", 0),
        "limit_down": breadth.get("limit_down", 0),
    }
    snapshots = history.setdefault("snapshots", [])
    if snapshots and snapshots[-1].get("time") == scan_time:
        snapshots[-1] = snapshot
    else:
        snapshots.append(snapshot)
    history["snapshots"] = snapshots[-300:]
    history["updated_at"] = scan_time
    _atomic_json_dump(history, path)


def run_scan(
    windows: list[int] | None = None,
    schemes: list[str] | None = None,
    output_dir: str = STATIC,
    write_files: bool = True,
) -> dict[str, Any]:
    windows = sorted(set(parse_window(value) for value in (windows or PRESET_WINDOWS.values())))
    schemes = list(dict.fromkeys(schemes or ["sw", "ths", "sw3"]))
    invalid = set(schemes) - {"sw", "ths", "sw3", "citic"}
    if invalid:
        raise ValueError(f"unsupported schemes: {sorted(invalid)}")

    active_codes = get_active_codes()
    industry_maps = load_industry_maps(active_codes, schemes)
    shares = _load_shares()
    today_str = datetime.now().strftime("%Y%m%d")
    scan_time = datetime.now().isoformat(timespec="seconds")

    t0 = time.time()
    spot_data = fetch_spot(active_codes, today_str)
    if not spot_data:
        raise RuntimeError("未获取到实时行情（非交易时间或数据源不可用）")
    cache = KlineCache()
    ohlcv = cache.ensure(
        active_codes, today_str, need_ohlcv=True, persist=False, update_live=False
    )
    signals, market = _collect_signals(active_codes, spot_data, ohlcv, windows, shares, scan_time)

    temperature = compute_intraday_temperature(market, signals, load_temperature_history(output_dir))
    temperature["updated_at"] = scan_time
    temperature["trade_date"] = today_str
    if write_files:
        _atomic_json_dump(temperature, os.path.join(output_dir, "intraday_temperature.json"))
        _update_temperature_history(output_dir, temperature, scan_time)

    result: dict[str, Any] = {
        "scan_time": scan_time,
        "elapsed_seconds": round(time.time() - t0, 2),
        "coverage": {
            "active": len(active_codes),
            "spot": int(market["spot_count"]),
            "market_cap": int(market["mcap_count"]),
        },
        "schemes": {},
    }
    for scheme in schemes:
        scheme_outputs = {}
        for days in windows:
            pair = {}
            for direction in ("highs", "lows"):
                previous = _load_first_seen(output_dir, scheme, days, direction)
                pair[direction] = _build_output(
                    direction, days, scheme, industry_maps[scheme], signals[days][direction],
                    spot_data, shares, scan_time, previous,
                )
                pair[direction]["coverage"] = result["coverage"]
                if write_files:
                    suffix = scheme_suffix(scheme)
                    path = os.path.join(
                        output_dir, f"intraday_{direction}_{window_key(days)}{suffix}.json"
                    )
                    _atomic_json_dump(pair[direction], path)
            scheme_outputs[days] = pair
        result["schemes"][scheme] = scheme_outputs
        if write_files:
            _update_history(output_dir, scheme, scheme_outputs, scan_time)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描盘中新高/新低")
    parser.add_argument("--window", default="all", help="all, 20d, 60d, 120d, 1year or custom N")
    parser.add_argument("--scheme", choices=["sw", "ths", "sw3", "all"], default="all")
    parser.add_argument("--output", default=STATIC)
    args = parser.parse_args()
    windows = list(PRESET_WINDOWS.values()) if args.window == "all" else [parse_window(args.window)]
    schemes = ["sw", "ths", "sw3"] if args.scheme == "all" else [args.scheme]
    try:
        result = run_scan(windows, schemes, args.output)
    except Exception as exc:
        print(f"盘中扫描失败: {exc}", file=sys.stderr)
        return 1
    coverage = result["coverage"]
    print(
        f"盘中扫描完成 {result['scan_time']}，行情覆盖 "
        f"{coverage['spot']}/{coverage['active']}，耗时 {result['elapsed_seconds']} 秒"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
