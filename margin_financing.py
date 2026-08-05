"""行业融资融券数据采集与三分类聚合。

数据源为上海、深圳证券交易所逐证券融资融券明细（通过 AKShare 读取）。
页面只展示两所均已发布的共同完整交易日，避免把单一交易所的提前发布
误当作全市场数据。申万一级、同花顺和申万三级共用同一份交易所原始数据。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import akshare as ak
import pandas as pd

from kline_cache import KlineCache, format_date_for_query, format_date_short, get_active_codes
from runtime_paths import data_path
from update_engine import _get_trade_dates, _load_ind_map


OUTPUT_BASENAME = "margin_financing"
MAX_DATES = 20
FETCH_PADDING = 5
SCHEMES = {
    "sw": ("申万一级", ""),
    "ths": ("同花顺", "_ths"),
    "sw3": ("申万三级", "_sw3"),
}


def _atomic_json_dump(payload: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _number(value, default=0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _normalize_code(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _normalize_sse(frame: pd.DataFrame, date: str, active_codes: set[str]) -> list[dict]:
    rows = []
    if frame is None or frame.empty:
        return rows
    for item in frame.to_dict("records"):
        code = _normalize_code(item.get("标的证券代码"))
        if code not in active_codes:
            continue
        rows.append({
            "date": date,
            "market": "SSE",
            "code": code,
            "name": str(item.get("标的证券简称") or code).strip(),
            "financing_balance": round(_number(item.get("融资余额"))),
            "financing_buy": round(_number(item.get("融资买入额"))),
            "financing_repay": round(_number(item.get("融资偿还额"))),
            "short_balance_qty": round(_number(item.get("融券余量"))),
            "short_sell_qty": round(_number(item.get("融券卖出量"))),
            "short_repay_qty": round(_number(item.get("融券偿还量"))),
            "short_balance_exchange": None,
        })
    return rows


def _normalize_szse(frame: pd.DataFrame, date: str, active_codes: set[str]) -> list[dict]:
    rows = []
    if frame is None or frame.empty:
        return rows
    for item in frame.to_dict("records"):
        code = _normalize_code(item.get("证券代码"))
        if code not in active_codes:
            continue
        rows.append({
            "date": date,
            "market": "SZSE",
            "code": code,
            "name": str(item.get("证券简称") or code).replace("&nbsp;", "").strip(),
            "financing_balance": round(_number(item.get("融资余额"))),
            "financing_buy": round(_number(item.get("融资买入额"))),
            "financing_repay": None,
            "short_balance_qty": round(_number(item.get("融券余量"))),
            "short_sell_qty": round(_number(item.get("融券卖出量"))),
            "short_repay_qty": None,
            "short_balance_exchange": round(_number(item.get("融券余额"))),
        })
    return rows


def _fetch_frame(function, date: str) -> pd.DataFrame:
    last_error = None
    for attempt in range(2):
        try:
            return function(date=date)
        except Exception as exc:  # transient exchange/network errors
            last_error = exc
            if attempt == 0:
                time.sleep(0.6)
    raise RuntimeError(f"{function.__name__}({date}) failed: {last_error}")


def _fetch_exchange_history(dates: list[str], active_codes: set[str]) -> tuple[dict, list[str]]:
    bundles = {date: {"sse": [], "szse": [], "errors": {}} for date in dates}
    tasks = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for date in dates:
            tasks[pool.submit(_fetch_frame, ak.stock_margin_detail_sse, date)] = (date, "sse")
            tasks[pool.submit(_fetch_frame, ak.stock_margin_detail_szse, date)] = (date, "szse")
        for future in as_completed(tasks):
            date, exchange = tasks[future]
            try:
                frame = future.result()
                bundles[date][exchange] = (
                    _normalize_sse(frame, date, active_codes)
                    if exchange == "sse"
                    else _normalize_szse(frame, date, active_codes)
                )
            except Exception as exc:
                bundles[date]["errors"][exchange] = str(exc)
    complete = [date for date in dates if bundles[date]["sse"] and bundles[date]["szse"]]
    return bundles, complete[-MAX_DATES:]


def _load_market_data(codes: list[str], dates: list[str]) -> dict[tuple[str, str], dict]:
    if not codes or not dates:
        return {}
    try:
        frames = KlineCache(force_refresh=False).ensure_dates(codes, dates, min_coverage=0.70)
    except Exception as exc:
        print(f"[margin] K线读取失败，成交强度与沪市融券金额将降级: {exc}")
        return {}
    lookup = {}
    wanted = set(dates)
    for code, frame in frames.items():
        if frame is None or frame.empty:
            continue
        for _, row in frame.iterrows():
            date = row["date"].strftime("%Y%m%d")
            if date not in wanted:
                continue
            close = _number(row.get("close"))
            volume = _number(row.get("volume"))
            lookup[(code, date)] = {
                "close": close,
                "turnover": close * volume if close > 0 and volume > 0 else 0,
            }
    return lookup


def _enrich_rows(bundles: dict, dates: list[str], market_data: dict) -> dict[str, list[dict]]:
    by_date = {}
    for date in dates:
        rows = []
        for exchange in ("sse", "szse"):
            for original in bundles[date][exchange]:
                row = dict(original)
                quote = market_data.get((row["code"], date), {})
                close = _number(quote.get("close"))
                row["close"] = round(close, 3) if close else None
                row["turnover"] = round(_number(quote.get("turnover")))
                exchange_value = row.get("short_balance_exchange")
                if exchange_value is not None:
                    row["short_balance"] = round(_number(exchange_value))
                    row["short_value_method"] = "exchange"
                elif close > 0:
                    row["short_balance"] = round(row["short_balance_qty"] * close)
                    row["short_value_method"] = "close_x_quantity"
                else:
                    row["short_balance"] = 0
                    row["short_value_method"] = "unavailable"
                row["total_balance"] = row["financing_balance"] + row["short_balance"]
                rows.append(row)
        by_date[date] = rows
    return by_date


def _pct_change(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round((current / previous - 1) * 100, 2)


def _state_for(changes: list[float], five_day_pct: float | None) -> str:
    recent = [value for value in changes[-3:] if value is not None]
    if len(recent) >= 3 and all(value > 0 for value in recent):
        return "连续增加"
    if len(recent) >= 3 and all(value < 0 for value in recent):
        return "连续回落"
    latest = recent[-1] if recent else 0
    if latest > 0 and (five_day_pct or 0) > 0:
        return "余额增加"
    if latest < 0 and (five_day_pct or 0) < 0:
        return "余额回落"
    return "震荡"


def _aggregate_scheme(by_date: dict[str, list[dict]], dates: list[str], scheme: str,
                      industry_map: dict[str, str]) -> dict:
    daily_by_industry: dict[str, dict[str, dict]] = {}
    stock_rows_by_date: dict[str, dict[str, dict]] = {}
    for date in dates:
        stock_rows_by_date[date] = {row["code"]: row for row in by_date.get(date, [])}
        for row in by_date.get(date, []):
            industry = industry_map.get(row["code"]) or "其他"
            metrics = daily_by_industry.setdefault(industry, {}).setdefault(date, {
                "financing_balance": 0,
                "financing_buy": 0,
                "short_balance": 0,
                "total_balance": 0,
                "turnover": 0,
                "stock_count": 0,
                "short_value_covered": 0,
            })
            for key in ("financing_balance", "financing_buy", "short_balance", "total_balance", "turnover"):
                metrics[key] += round(_number(row.get(key)))
            metrics["stock_count"] += 1
            if row.get("short_value_method") != "unavailable":
                metrics["short_value_covered"] += 1

    industries = []
    total_financing_latest = sum(
        daily_by_industry[industry].get(dates[-1], {}).get("financing_balance", 0)
        for industry in daily_by_industry
    ) if dates else 0
    for industry in sorted(daily_by_industry):
        series = daily_by_industry[industry]
        financing = [series.get(date, {}).get("financing_balance", 0) for date in dates]
        buys = [series.get(date, {}).get("financing_buy", 0) for date in dates]
        shorts = [series.get(date, {}).get("short_balance", 0) for date in dates]
        totals = [series.get(date, {}).get("total_balance", 0) for date in dates]
        turnovers = [series.get(date, {}).get("turnover", 0) for date in dates]
        counts = [series.get(date, {}).get("stock_count", 0) for date in dates]
        changes = [None] + [financing[i] - financing[i - 1] for i in range(1, len(financing))]
        change_pcts = [None] + [_pct_change(financing[i], financing[i - 1]) for i in range(1, len(financing))]
        five_idx = max(0, len(financing) - 6)
        five_day_pct = _pct_change(financing[-1], financing[five_idx]) if financing else None
        latest_turnover = turnovers[-1] if turnovers else 0
        latest_buy = buys[-1] if buys else 0
        buy_intensity = round(latest_buy / latest_turnover * 100, 2) if latest_turnover else None
        industries.append({
            "industry": industry,
            "stock_count": counts[-1] if counts else 0,
            "financing_balance": financing[-1] if financing else 0,
            "financing_buy": latest_buy,
            "financing_change": changes[-1] if changes else None,
            "financing_change_pct": change_pcts[-1] if change_pcts else None,
            "financing_change_5d_pct": five_day_pct,
            "buy_intensity": buy_intensity,
            "short_balance": shorts[-1] if shorts else 0,
            "total_balance": totals[-1] if totals else 0,
            "balance_share": round(financing[-1] / total_financing_latest * 100, 2)
                if financing and total_financing_latest else 0,
            "state": _state_for(changes, five_day_pct),
            "daily": {
                "financing_balance": financing,
                "financing_buy": buys,
                "financing_change": changes,
                "financing_change_pct": change_pcts,
                "short_balance": shorts,
                "total_balance": totals,
                "turnover": turnovers,
                "stock_count": counts,
            },
        })
    industries.sort(key=lambda row: (-row["financing_balance"], row["industry"]))

    details = {}
    if dates:
        latest, previous = dates[-1], dates[-2] if len(dates) > 1 else None
        previous_rows = stock_rows_by_date.get(previous, {}) if previous else {}
        for row in by_date.get(latest, []):
            industry = industry_map.get(row["code"]) or "其他"
            old = previous_rows.get(row["code"])
            change = row["financing_balance"] - old["financing_balance"] if old else None
            intensity = (
                round(row["financing_buy"] / row["turnover"] * 100, 2)
                if row.get("turnover") else None
            )
            details.setdefault(industry, []).append({
                "code": row["code"],
                "name": row["name"],
                "market": row["market"],
                "close": row.get("close"),
                "financing_balance": row["financing_balance"],
                "financing_buy": row["financing_buy"],
                "financing_change": change,
                "buy_intensity": intensity,
                "short_balance": row["short_balance"],
                "short_balance_qty": row["short_balance_qty"],
                "total_balance": row["total_balance"],
            })
        for rows in details.values():
            rows.sort(key=lambda row: (-row["financing_balance"], row["code"]))

    market = {
        "financing_balance": sum(row["financing_balance"] for row in industries),
        "financing_buy": sum(row["financing_buy"] for row in industries),
        "financing_change": sum((row["financing_change"] or 0) for row in industries),
        "short_balance": sum(row["short_balance"] for row in industries),
        "total_balance": sum(row["total_balance"] for row in industries),
        "turnover": sum((row["daily"]["turnover"][-1] if row["daily"]["turnover"] else 0) for row in industries),
        "stock_count": sum(row["stock_count"] for row in industries),
    }
    market["buy_intensity"] = (
        round(market["financing_buy"] / market["turnover"] * 100, 2)
        if market["turnover"] else None
    )
    return {"market": market, "industries": industries, "details": details}


def update_margin_financing(target_dates=None, schemes=None) -> dict:
    requested_schemes = list(schemes or SCHEMES)
    invalid = set(requested_schemes) - set(SCHEMES)
    if invalid:
        raise ValueError(f"unsupported schemes: {sorted(invalid)}")

    recent = _get_trade_dates(MAX_DATES + FETCH_PADDING)
    candidates = sorted(set(recent) | set(target_dates or []))[-(MAX_DATES + FETCH_PADDING):]
    if not candidates:
        raise RuntimeError("没有可用交易日")
    active_codes = set(get_active_codes())
    print(f"[margin] 拉取沪深交易所明细: {len(candidates)} 个候选交易日")
    bundles, complete_dates = _fetch_exchange_history(candidates, active_codes)
    if not complete_dates:
        raise RuntimeError("沪深交易所没有共同完整的融资融券交易日，保留旧数据")

    codes = sorted({
        row["code"] for date in complete_dates
        for exchange in ("sse", "szse") for row in bundles[date][exchange]
    })
    market_data = _load_market_data(codes, complete_dates)
    by_date = _enrich_rows(bundles, complete_dates, market_data)
    exchange_latest = {
        "sse": max((date for date in candidates if bundles[date]["sse"]), default=None),
        "szse": max((date for date in candidates if bundles[date]["szse"]), default=None),
        "complete": complete_dates[-1],
    }
    latest_bundle = bundles[complete_dates[-1]]
    price_covered = sum(
        1 for row in by_date[complete_dates[-1]] if row.get("close") is not None
    )
    outputs = {}
    for scheme in requested_schemes:
        aggregated = _aggregate_scheme(by_date, complete_dates, scheme, _load_ind_map(scheme))
        label, suffix = SCHEMES[scheme]
        payload = {
            "schema_version": 1,
            "scheme": scheme,
            "scheme_label": label,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "latest_date": complete_dates[-1],
            "dates": [{
                "date": date,
                "label": format_date_short(date),
                "full_label": format_date_for_query(date),
            } for date in complete_dates],
            "source": {
                "provider": "上海证券交易所、深圳证券交易所",
                "adapter": "AKShare",
                "exchange_latest": exchange_latest,
                "complete_date_rule": "仅展示沪深两所均已发布的共同完整交易日",
                "short_balance_method": "深市采用交易所披露金额；沪市采用融券余量×当日收盘价",
            },
            "coverage": {
                "active_stocks": len(active_codes),
                "margin_stocks": len(by_date[complete_dates[-1]]),
                "sse_rows": len(latest_bundle["sse"]),
                "szse_rows": len(latest_bundle["szse"]),
                "price_covered": price_covered,
                "price_coverage_ratio": round(price_covered / max(len(by_date[complete_dates[-1]]), 1), 4),
            },
            **aggregated,
        }
        output_path = data_path(f"{OUTPUT_BASENAME}{suffix}.json")
        _atomic_json_dump(payload, output_path)
        outputs[scheme] = payload
        print(f"[margin] {label}: {len(payload['industries'])} 个行业 -> {output_path}")
    return outputs


if __name__ == "__main__":
    update_margin_financing()
