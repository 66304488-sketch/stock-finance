#!/usr/bin/env python3
"""External evidence for crowding and exit fragility.

The OHLCV model can only infer trading-attention concentration.  This module
adds slower, more direct evidence where stable public bulk sources are
available:

* CNI A Share aggregate free-float market value and implied turnover;
* SSE/SZSE margin-financing detail with exchange-specific publication dates;
* exchange-published ETF shares and share changes;
* quarterly CNINFO fund holdings;
* optional Sina level-1 order-book depth for a small Top-N watchlist.

Northbound daily holdings stopped being a daily public disclosure after
2024-08-19, so the source is explicitly marked unsupported and never enters a
score.  Every source is optional.  Missing or stale data stays ``None`` and is
exposed to the frontend; it is never converted to a reassuring zero.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Any, Iterable

from runtime_paths import DATA_DIR, RESOURCE_STATIC_DIR, data_path

SCHEMA_VERSION = 2
EXTERNAL_FILE = data_path("crowding_external.json")
CNI_A_SHARE_INDEX = "399317"
MAX_ORDER_BOOK_CODES = 100
ORDER_BOOK_BATCH_SIZE = 50


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
        if not value or value in {"-", "--", "None", "nan"}:
            return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _code(value: Any) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    return text[-6:].zfill(6) if text else ""


def _atomic_dump(payload: dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".crowding-external-", suffix=".tmp",
                                     dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def load_external_snapshot(path: str = EXTERNAL_FILE) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _source(status: str, *, as_of: str | None = None, rows: int = 0,
            coverage: float | None = None, note: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "rows": int(rows)}
    if as_of:
        result["as_of"] = as_of
    if coverage is not None:
        result["coverage"] = round(float(coverage), 4)
    if note:
        result["note"] = str(note)[:240]
    return result


def _compact_date(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))[:8]


def _date_value(value: Any) -> datetime:
    compact = _compact_date(value)
    if len(compact) != 8:
        raise ValueError(f"invalid date: {value}")
    return datetime.strptime(compact, "%Y%m%d")


def _request_fingerprint(values: Iterable[str]) -> dict[str, Any]:
    normalized = sorted({_code(value) for value in values if _code(value)})
    digest = hashlib.sha256(",".join(normalized).encode("ascii")).hexdigest()[:16]
    return {"count": len(normalized), "hash": digest}


def _fetch_market_free_float(trade_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch and date-validate the aggregate CNI A Share free-float snapshot."""
    import akshare as ak

    all_indexes = ak.index_all_cni()
    matches = all_indexes[
        all_indexes["指数代码"].map(_code).eq(CNI_A_SHARE_INDEX)
    ]
    if matches.empty:
        raise RuntimeError("国证指数列表未返回国证A指")
    row = matches.iloc[0]
    close = _number(row.get("收盘点位"))
    amount_100m = _number(row.get("成交额"))
    total_mcap_100m = _number(row.get("总市值"))
    free_mcap_100m = _number(row.get("自由流通市值"))
    if (
        close is None
        or amount_100m is None
        or total_mcap_100m is None
        or free_mcap_100m is None
        or free_mcap_100m <= 0
    ):
        raise RuntimeError("国证A指聚合值不完整")

    end = _date_value(trade_date)
    start_date = (end - timedelta(days=14)).strftime("%Y%m%d")
    end_date = end.strftime("%Y%m%d")
    history = ak.index_hist_cni(
        symbol=CNI_A_SHARE_INDEX,
        start_date=start_date,
        end_date=end_date,
    )
    matched_date = None
    if history is not None and not history.empty:
        for history_row in reversed(history.to_dict("records")):
            history_close = _number(history_row.get("收盘价"))
            history_amount = _number(history_row.get("成交额"))
            candidate_date = _compact_date(history_row.get("日期"))
            if (
                candidate_date
                and history_close is not None
                and history_amount is not None
                and math.isclose(history_close, close, rel_tol=1e-9, abs_tol=1e-4)
                and math.isclose(
                    history_amount, amount_100m, rel_tol=1e-6, abs_tol=0.02
                )
            ):
                matched_date = candidate_date
                break
    if matched_date is None:
        raise RuntimeError("国证A指聚合快照未通过历史行情日期校验")

    unit = 100_000_000
    market = {
        "index_code": CNI_A_SHARE_INDEX,
        "index_name": str(row.get("指数简称") or "国证A指"),
        "source_date": matched_date,
        "close": close,
        "official_amount": amount_100m * unit,
        "total_mcap": total_mcap_100m * unit,
        "free_float_mcap": free_mcap_100m * unit,
        "free_float_turnover_rate": amount_100m / free_mcap_100m * 100,
    }
    return market, _source(
        "ok",
        as_of=matched_date,
        rows=1,
        note="国证A指市场聚合；金额已由亿元转为元，换手率=成交额/自由流通市值",
    )


def _weekday_candidates(
    end_date: str,
    *,
    preferred: Iterable[str] = (),
    limit: int = 5,
) -> list[str]:
    """Return recent weekday candidates; source responses validate holidays."""
    end = _date_value(end_date)
    candidates: set[str] = set()
    for value in preferred:
        try:
            candidate = _date_value(value)
        except ValueError:
            continue
        if candidate <= end and candidate.weekday() < 5:
            candidates.add(candidate.strftime("%Y%m%d"))

    cursor = end
    while len(candidates) < limit:
        if cursor.weekday() < 5:
            candidates.add(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return sorted(candidates, reverse=True)[:limit]


def _load_first_nonempty(loader, candidates: Iterable[str]):
    errors = []
    for candidate_date in candidates:
        try:
            frame = loader(date=candidate_date)
        except Exception as exc:
            errors.append(f"{candidate_date}:{type(exc).__name__}:{exc}")
            continue
        if frame is None or frame.empty:
            errors.append(f"{candidate_date}:empty")
            continue
        return frame, candidate_date, errors
    return None, None, errors


def _fetch_margin(active: set[str], trade_date: str,
                  previous_trade_date: str | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    import akshare as ak

    current_candidates = _weekday_candidates(
        trade_date,
        preferred=([previous_trade_date] if previous_trade_date else ()),
        limit=5,
    )
    records: dict[str, dict[str, Any]] = {}
    source_dates: dict[str, str] = {}
    previous_source_dates: dict[str, str] = {}
    exchange_rows: dict[str, int] = {}
    errors = []
    for exchange, label, loader in (
        ("SSE", "上交所", ak.stock_margin_detail_sse),
        ("SZSE", "深交所", ak.stock_margin_detail_szse),
    ):
        current, source_date, current_errors = _load_first_nonempty(
            loader, current_candidates
        )
        if current is None or source_date is None:
            errors.append(f"{label}:" + "|".join(current_errors))
            continue

        source_dates[exchange] = source_date
        exchange_rows[exchange] = len(current)
        prior_end = (_date_value(source_date) - timedelta(days=1)).strftime("%Y%m%d")
        preferred_previous = (
            [previous_trade_date]
            if previous_trade_date and _compact_date(previous_trade_date) < source_date
            else ()
        )
        previous, previous_source_date, previous_errors = _load_first_nonempty(
            loader,
            _weekday_candidates(
                prior_end, preferred=preferred_previous, limit=5
            ),
        )
        if previous_source_date:
            previous_source_dates[exchange] = previous_source_date
        elif previous_errors:
            errors.append(f"{label}前值:" + "|".join(previous_errors))

        previous_map: dict[str, float] = {}
        if previous is not None:
            for row in previous.to_dict("records"):
                code = _code(row.get("标的证券代码") or row.get("证券代码"))
                balance = _number(row.get("融资余额"))
                if code and balance is not None:
                    previous_map[code] = balance

        for row in current.to_dict("records"):
            code = _code(row.get("标的证券代码") or row.get("证券代码"))
            if code not in active:
                continue
            balance = _number(row.get("融资余额"))
            short_balance = _number(row.get("融券余额"))
            short_qty = _number(row.get("融券余量"))
            prior = previous_map.get(code)
            records[code] = {
                "margin_balance": balance,
                "margin_change": (
                    balance - prior
                    if balance is not None and prior is not None else None
                ),
                "short_balance": short_balance,
                "short_qty": short_qty,
                "margin_exchange": exchange,
                "margin_source_date": source_date,
                "margin_previous_source_date": previous_source_date,
            }

    if len(source_dates) == 2:
        source_status = "ok"
    elif source_dates:
        source_status = "partial"
    else:
        source_status = "unavailable"
    status = _source(
        source_status,
        as_of=min(source_dates.values()) if source_dates else None,
        rows=len(records),
        coverage=len(records) / max(len(active), 1),
        note=(
            "沪深交易所融资融券明细；各交易所独立回退最近5个工作日"
            + (f"；部分失败:{'; '.join(errors)}" if errors else "")
        ),
    )
    status["source_dates"] = source_dates
    status["previous_source_dates"] = previous_source_dates
    status["exchange_rows"] = exchange_rows
    return records, status


def _quarter_candidates(trade_date: str) -> list[str]:
    year, month, day = int(trade_date[:4]), int(trade_date[4:6]), int(trade_date[6:8])
    current = year * 4 + ((month - 1) // 3)
    result = []
    for offset in range(5):
        quarter_index = current - offset
        q_year, zero_q = divmod(quarter_index, 4)
        q_month = (zero_q + 1) * 3
        q_day = 31 if q_month in (3, 12) else 30
        candidate = f"{q_year:04d}{q_month:02d}{q_day:02d}"
        if candidate <= f"{year:04d}{month:02d}{day:02d}":
            result.append(candidate)
    return result


def _fetch_fund_holdings(active: set[str], trade_date: str) -> tuple[
        dict[str, dict[str, Any]], dict[str, Any]]:
    import akshare as ak

    used_date = None
    frame = None
    errors = []
    for quarter in _quarter_candidates(trade_date):
        try:
            candidate = ak.fund_report_stock_cninfo(date=quarter)
            if candidate is not None and len(candidate) >= 20:
                frame, used_date = candidate, quarter
                break
        except Exception as exc:
            errors.append(f"{quarter}:{exc}")
    if frame is None:
        raise RuntimeError("; ".join(errors) or "未找到已披露基金持仓")

    records: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        code = _code(row.get("股票代码"))
        if code not in active:
            continue
        records[code] = {
            "fund_count": _number(row.get("基金覆盖家数")),
            "fund_hold_shares": _number(row.get("持股总数")),
            "fund_hold_mcap": (
                _number(row.get("持股总市值")) * 10_000
                if _number(row.get("持股总市值")) is not None else None
            ),
        }
    return records, _source(
        "ok" if records else "unavailable",
        as_of=used_date,
        rows=len(records),
        coverage=len(records) / max(len(active), 1),
        note="巨潮基金季报汇总；持股总市值已由万元转为元，低频且存在披露滞后",
    )


def _fetch_northbound(active: set[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    _ = active
    return {}, _source(
        "unsupported",
        rows=0,
        note="北向逐股持仓自2024-08-19起改为季度披露；日频接口停用且不参与评分",
    )


def _fetch_etf_shares(trade_date: str, previous_trade_date: str | None,
                      cached: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    import akshare as ak

    current: dict[str, dict[str, Any]] = {}
    source_dates: dict[str, str] = {}
    previous_source_dates: dict[str, str] = {}
    errors = []

    loaders = (
        ("SSE", lambda value: ak.fund_etf_scale_sse(date=value)),
        (
            "SZSE",
            lambda value: ak.fund_scale_daily_szse(
                start_date=value, end_date=value, symbol="ETF"
            ),
        ),
    )

    for exchange, loader in loaders:
        try:
            frame = loader(trade_date)
        except Exception as exc:
            errors.append(f"{exchange}:{exc}")
            continue
        if frame is None or frame.empty:
            errors.append(f"{exchange}:{trade_date}:empty")
            continue
        dates = set()
        for row in frame.to_dict("records"):
            code = _code(row.get("基金代码"))
            if code:
                source_date = (
                    _compact_date(row.get("统计日期") or row.get("日期"))
                    or trade_date
                )
                dates.add(source_date)
                current[code] = {
                    "name": str(row.get("基金简称") or ""),
                    "shares": _number(row.get("基金份额")),
                    "exchange": exchange,
                    "source_date": source_date,
                }
        source_dates[exchange] = max(dates) if dates else trade_date

    previous_records: dict[str, Any] = {}
    if previous_trade_date:
        for exchange, loader in loaders:
            try:
                frame = loader(previous_trade_date)
            except Exception as exc:
                errors.append(f"{exchange}前值:{exc}")
                continue
            if frame is None or frame.empty:
                errors.append(f"{exchange}前值:{previous_trade_date}:empty")
                continue
            dates = set()
            for row in frame.to_dict("records"):
                code = _code(row.get("基金代码"))
                if code:
                    previous_records[code] = _number(row.get("基金份额"))
                    source_date = (
                        _compact_date(row.get("统计日期") or row.get("日期"))
                        or previous_trade_date
                    )
                    dates.add(source_date)
            previous_source_dates[exchange] = (
                max(dates) if dates else previous_trade_date
            )

    cached_etfs = cached.get("etfs") if isinstance(cached, dict) else {}
    cached_trade_date = _compact_date(
        cached.get("trade_date") if isinstance(cached, dict) else None
    )
    if (
        isinstance(cached_etfs, dict)
        and cached_trade_date
        and cached_trade_date < _compact_date(trade_date)
    ):
        for code, row in cached_etfs.items():
            normalized_code = _code(code)
            if normalized_code not in previous_records and isinstance(row, dict):
                previous_records[normalized_code] = _number(row.get("shares"))

    for code, row in current.items():
        previous_shares = previous_records.get(code)
        shares = row.get("shares")
        row["share_change"] = (
            shares - previous_shares
            if shares is not None and previous_shares is not None else None
        )
        row["share_change_pct"] = (
            (shares / previous_shares - 1) * 100
            if shares is not None and previous_shares and previous_shares > 0 else None
        )

    if len(source_dates) == 2:
        source_status = "ok"
    elif source_dates:
        source_status = "partial"
    else:
        source_status = "unavailable"
    status = _source(
        source_status,
        as_of=min(source_dates.values()) if source_dates else None,
        rows=len(current),
        note=("交易所日度ETF份额；首次运行或前值源失败时变化保持缺失"
              + (f"；部分失败:{'; '.join(errors)}" if errors else "")),
    )
    status["source_dates"] = source_dates
    status["previous_source_dates"] = previous_source_dates
    return current, status


def _fetch_order_book(codes: Iterable[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    import requests

    ordered = [
        code for code in dict.fromkeys(_code(value) for value in codes) if code
    ][:MAX_ORDER_BOOK_CODES]
    if not ordered:
        return {}, _source(
            "skipped",
            rows=0,
            note="未提供成交额Top-N股票，跳过可选新浪五档盘口",
        )

    records: dict[str, dict[str, Any]] = {}
    errors = []
    source_dates = set()
    for start in range(0, len(ordered), ORDER_BOOK_BATCH_SIZE):
        batch = ordered[start:start + ORDER_BOOK_BATCH_SIZE]
        symbols = [("sh" if code.startswith("6") else "sz") + code for code in batch]
        try:
            response = requests.get(
                "https://hq.sinajs.cn/list=" + ",".join(symbols),
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:
            errors.append(f"batch-{start // ORDER_BOOK_BATCH_SIZE + 1}:{exc}")
            continue
        response.encoding = "gbk"
        for line in response.text.splitlines():
            if "hq_str_" not in line or '="' not in line:
                continue
            symbol = line.split("hq_str_", 1)[1].split("=", 1)[0]
            code = _code(symbol)
            fields = line.split('="', 1)[1].rstrip('";').split(",")
            if len(fields) < 30:
                continue
            if len(fields) > 30:
                source_date = _compact_date(fields[30])
                if source_date:
                    source_dates.add(source_date)
            bids = [(_number(fields[i + 1]), _number(fields[i]))
                    for i in range(10, 20, 2)]
            asks = [(_number(fields[i + 1]), _number(fields[i]))
                    for i in range(20, 30, 2)]
            bid1 = bids[0][0] if bids else None
            ask1 = asks[0][0] if asks else None
            mid = (bid1 + ask1) / 2 if bid1 and ask1 else None
            bid_depth = sum((price or 0) * (volume or 0) for price, volume in bids)
            ask_depth = sum((price or 0) * (volume or 0) for price, volume in asks)
            records[code] = {
                "spread_bps": ((ask1 - bid1) / mid * 10000) if mid else None,
                "bid_depth_amount": bid_depth or None,
                "ask_depth_amount": ask_depth or None,
                "official_amount": _number(fields[9]),
                "book_imbalance": (
                    (bid_depth - ask_depth) / (bid_depth + ask_depth)
                    if bid_depth + ask_depth > 0 else None
                ),
            }

    if records and not errors and len(records) == len(ordered):
        source_status = "ok"
    elif records:
        source_status = "partial"
    else:
        source_status = "unavailable"
    return records, _source(
        source_status,
        as_of=max(source_dates) if source_dates else None,
        rows=len(records),
        coverage=len(records) / max(len(ordered), 1),
        note=(
            f"新浪五档盘口，仅覆盖成交额Top-{len(ordered)}；不是完整Level-2深度"
            + (f"；部分失败:{'; '.join(errors)}" if errors else "")
        ),
    )


def _merge_stock_fields(target: dict[str, dict[str, Any]],
                        source: dict[str, dict[str, Any]]) -> None:
    for code, fields in source.items():
        target.setdefault(code, {}).update({
            key: value for key, value in fields.items() if value is not None
        })


def refresh_external_snapshot(
    codes: Iterable[str],
    trade_date: str,
    *,
    previous_trade_date: str | None = None,
    top_codes: Iterable[str] = (),
    force: bool = False,
    path: str = EXTERNAL_FILE,
) -> dict[str, Any]:
    """Refresh all optional evidence sources and return a cached snapshot.

    A source failure is recorded but never aborts the core crowding update.
    """
    active = {_code(code) for code in codes if _code(code)}
    normalized_top_codes = [
        code for code in dict.fromkeys(_code(value) for value in top_codes) if code
    ][:MAX_ORDER_BOOK_CODES]
    request_meta = {
        "stocks": _request_fingerprint(active),
        "order_book": _request_fingerprint(normalized_top_codes),
    }
    cached = load_external_snapshot(path)
    fetched_at = cached.get("fetched_at")
    if (
        not force
        and cached.get("schema_version") == SCHEMA_VERSION
        and cached.get("trade_date") == trade_date
        and cached.get("request") == request_meta
        and fetched_at
    ):
        try:
            age = (datetime.now() - datetime.fromisoformat(fetched_at)).total_seconds()
            ok_sources = sum(
                1 for value in (cached.get("sources") or {}).values()
                if isinstance(value, dict)
                and value.get("status") in {"ok", "partial"}
            )
            if age < 6 * 3600 and ok_sources >= 2:
                return cached
        except (TypeError, ValueError):
            pass

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trade_date": trade_date,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "request": request_meta,
        "sources": {},
        "market": {},
        "stocks": {},
        "etfs": {},
    }
    loaders = (
        ("free_float", lambda: _fetch_market_free_float(trade_date)),
        ("margin", lambda: _fetch_margin(active, trade_date, previous_trade_date)),
        ("fund_holdings", lambda: _fetch_fund_holdings(active, trade_date)),
        ("northbound", lambda: _fetch_northbound(active)),
        ("etf_shares", lambda: _fetch_etf_shares(
            trade_date, previous_trade_date, cached)),
        ("order_book", lambda: _fetch_order_book(normalized_top_codes)),
    )
    for name, loader in loaders:
        try:
            records, status = loader()
            payload["sources"][name] = status
            if name == "free_float":
                payload["market"] = records
            elif name == "etf_shares":
                payload["etfs"] = records
            else:
                _merge_stock_fields(payload["stocks"], records)
        except Exception as exc:
            payload["sources"][name] = _source(
                "unavailable", as_of=trade_date, note=str(exc))

    _atomic_dump(payload, path)
    return payload


def load_etf_industry_map(
    industry_names: Iterable[str],
    *,
    scheme: str = "sw",
) -> dict[str, str]:
    """Map ETF codes to the selected heatmap classification."""
    valid = set(industry_names)
    candidates: dict[str, list[tuple[int, str]]] = {}
    filename = (
        "industry_etf_map_ths.json"
        if scheme == "ths" else "industry_etf_map_sw3.json"
    )
    paths = [
        os.path.join(DATA_DIR, filename),
        os.path.join(RESOURCE_STATIC_DIR, filename),
    ]
    payload = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            break
        except (OSError, json.JSONDecodeError):
            continue
    parent_map = payload.get("parent_map") or {}
    for key, rows in (payload.get("mapping") or {}).items():
        parents = parent_map.get(key) or {}
        if key in valid:
            industry = key
            target_priority = 4
        elif scheme == "sw3" and parents.get("sw2") in valid:
            industry = parents["sw2"]
            target_priority = 3
        elif parents.get("sw1") in valid:
            industry = parents["sw1"]
            target_priority = 2
        else:
            continue
        for row in rows or []:
            code = _code(row.get("code"))
            if not code:
                continue
            level = str(row.get("match_level") or "")
            source_priority = {"sw3": 3, "sw2": 2, "sw1": 1}.get(
                level, 2 if scheme == "ths" else 0)
            priority = target_priority * 10 + source_priority
            candidates.setdefault(code, []).append((priority, industry))

    result = {}
    for code, rows in candidates.items():
        best_priority = max(priority for priority, _ in rows)
        parents = {parent for priority, parent in rows if priority == best_priority}
        if len(parents) == 1:
            result[code] = next(iter(parents))
    return result


def _percentile(values: list[float | None], value: float | None,
                *, reverse: bool = False) -> float | None:
    clean = sorted(number for number in values if number is not None and math.isfinite(number))
    if value is None or not clean:
        return None
    rank = sum(number <= value for number in clean) / len(clean) * 100
    return round(100 - rank if reverse else rank, 1)


def aggregate_external_by_industry(
    snapshot: dict[str, Any],
    industry_map: dict[str, str],
    industry_names: Iterable[str],
    *,
    scheme: str = "sw",
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Aggregate direct evidence and calculate cross-sectional percentiles."""
    valid = set(industry_names)
    groups: dict[str, dict[str, Any]] = {
        industry: {
            "float_mcap": 0.0,
            "margin_balance": 0.0,
            "margin_change": 0.0,
            "fund_hold_mcap": 0.0,
            "north_hold_mcap": 0.0,
            "bid_depth_amount": 0.0,
            "official_amount": 0.0,
            "etf_shares": 0.0,
            "etf_share_change": 0.0,
            "spread_values": [],
            "stock_count": 0,
            "float_count": 0,
            "margin_count": 0,
            "margin_change_count": 0,
            "fund_count": 0,
            "north_count": 0,
            "book_count": 0,
            "etf_count": 0,
            "etf_change_count": 0,
        } for industry in valid
    }
    for code, row in (snapshot.get("stocks") or {}).items():
        industry = industry_map.get(code)
        if industry not in groups or not isinstance(row, dict):
            continue
        group = groups[industry]
        group["stock_count"] += 1
        float_mcap = _number(row.get("float_mcap"))
        if float_mcap:
            group["float_mcap"] += float_mcap
            group["float_count"] += 1
        for source_key, count_key in (
            ("margin_balance", "margin_count"),
            ("fund_hold_mcap", "fund_count"),
            ("north_hold_mcap", "north_count"),
            ("bid_depth_amount", "book_count"),
        ):
            value = _number(row.get(source_key))
            if value is not None:
                group[source_key] += value
                group[count_key] += 1
        margin_change = _number(row.get("margin_change"))
        if margin_change is not None:
            group["margin_change"] += margin_change
            group["margin_change_count"] += 1
        official_amount = _number(row.get("official_amount"))
        if official_amount:
            group["official_amount"] += official_amount
        spread = _number(row.get("spread_bps"))
        if spread is not None:
            group["spread_values"].append(spread)

    etf_map = load_etf_industry_map(valid, scheme=scheme)
    for code, row in (snapshot.get("etfs") or {}).items():
        industry = etf_map.get(code)
        if industry not in groups or not isinstance(row, dict):
            continue
        group = groups[industry]
        shares = _number(row.get("shares"))
        change = _number(row.get("share_change"))
        if shares is not None:
            group["etf_shares"] += shares
            group["etf_count"] += 1
        if change is not None:
            group["etf_share_change"] += change
            group["etf_change_count"] += 1

    result: dict[str, dict[str, Any]] = {}
    for industry, group in groups.items():
        float_mcap = group["float_mcap"]
        margin_balance = group["margin_balance"]
        official_amount = group["official_amount"]
        spreads = group.pop("spread_values")
        result[industry] = {
            **group,
            "etf_share_change": (
                group["etf_share_change"]
                if group["etf_change_count"] > 0 else None
            ),
            "margin_float_pct": (
                margin_balance / float_mcap * 100
                if float_mcap > 0 and group["margin_count"] > 0 else None),
            "margin_change_pct": (
                group["margin_change"] / margin_balance * 100
                if margin_balance > 0 and group["margin_change_count"] > 0 else None),
            "fund_float_pct": (
                group["fund_hold_mcap"] / float_mcap * 100
                if float_mcap > 0 and group["fund_count"] > 0 else None),
            "north_float_pct": (
                group["north_hold_mcap"] / float_mcap * 100
                if float_mcap > 0 and group["north_count"] > 0 else None),
            "spread_bps": (
                sum(spreads) / len(spreads) if spreads else None),
            "bid_depth_to_amount_bps": (
                group["bid_depth_amount"] / official_amount * 10000
                if official_amount > 0 else None),
        }

    metric_specs = (
        ("margin_float_pct", "margin_pctile", False),
        ("margin_change_pct", "margin_change_pctile", False),
        ("fund_float_pct", "fund_pctile", False),
        ("etf_share_change", "etf_flow_pctile", False),
        ("spread_bps", "spread_pctile", False),
        ("bid_depth_to_amount_bps", "thin_book_pctile", True),
    )
    for source_key, output_key, reverse in metric_specs:
        values = [_number(row.get(source_key)) for row in result.values()]
        for row in result.values():
            row[output_key] = _percentile(
                values, _number(row.get(source_key)), reverse=reverse)

    for row in result.values():
        # Kept as an explicit compatibility field, but daily northbound data
        # is structurally unsupported and therefore cannot enter a score.
        row["north_pctile"] = None
        margin_components = [
            row.get("margin_pctile"),
            row.get("margin_change_pctile"),
        ]
        margin_components = [
            value for value in margin_components if value is not None
        ]
        margin_component = (
            sum(margin_components) / len(margin_components)
            if margin_components else None
        )
        position_components = [
            margin_component,
            row.get("fund_pctile"),
            row.get("etf_flow_pctile"),
        ]
        position_components = [value for value in position_components if value is not None]
        liquidity_components = [
            row.get("spread_pctile"),
            row.get("thin_book_pctile"),
        ]
        liquidity_components = [value for value in liquidity_components if value is not None]
        row["direct_position_score"] = (
            round(sum(position_components) / len(position_components), 1)
            if position_components else None)
        row["external_fragility_score"] = (
            round(sum(liquidity_components) / len(liquidity_components), 1)
            if liquidity_components else None)

    sources = snapshot.get("sources") or {}
    available_sources = [
        name for name, value in sources.items()
        if name != "northbound"
        and isinstance(value, dict)
        and value.get("status") in {"ok", "partial"}
    ]
    unsupported_sources = [
        name for name, value in sources.items()
        if name == "northbound"
        or (
            isinstance(value, dict)
            and value.get("status") == "unsupported"
        )
    ]
    requested_sources = [
        name for name, value in sources.items()
        if name not in unsupported_sources
        and not (
            isinstance(value, dict)
            and value.get("status") == "skipped"
        )
    ]
    core_sources = {"free_float", "margin", "fund_holdings", "etf_shares"}
    available_core_sources = sorted(core_sources.intersection(available_sources))
    available_core_count = len(available_core_sources)
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    summary = {
        "scheme": scheme,
        "trade_date": snapshot.get("trade_date"),
        "fetched_at": snapshot.get("fetched_at"),
        "sources": sources,
        "market": market,
        "market_free_float_turnover_rate": _number(
            market.get("free_float_turnover_rate")
        ),
        "available_sources": available_sources,
        "available_count": len(available_sources),
        "requested_count": len(requested_sources),
        "available_core_sources": available_core_sources,
        "available_core_count": available_core_count,
        "requested_core_count": len(core_sources),
        "unsupported_sources": unsupported_sources,
        "confidence": (
            "high" if available_core_count >= 4
            else "medium" if available_core_count >= 2
            else "low"
        ),
        "limitations": [
            "基金持仓为低频披露数据",
            "自由流通市值仅为国证A指市场聚合，不能充当逐股或行业分母",
            "北向逐股日频披露已停止且不参与评分",
            "新浪五档盘口仅为可选Top-N快照，不是完整Level-2深度",
            "缺失来源不参与评分，不按零值处理",
        ],
    }
    return result, summary
