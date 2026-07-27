"""Intraday monitor primitives for China's four equity-index futures.

The module deliberately keeps raw basis separate from directional evidence.
Basis contains carry, expected dividends, hedging pressure, liquidity, and
arbitrage constraints; it is not a forecast of the underlying index.

Public entry points:

``generate_contracts``
    Generate the currently listed nearby/quarterly contract candidates.
``fetch_futures_quotes``
    Fetch and parse a batch of Sina ``nf_`` quotes with last-good fallback.
``enrich_futures_quote``
    Add spot basis, optional fair value, and in-memory horizon changes.
``get_product_overview``
    Build one IF/IH/IC/IM product overview.
``get_index_futures_overview``
    Fetch once and build the four-product overview.

Rates configured through ``INDEX_FUTURES_FUNDING_RATE`` and
``INDEX_FUTURES_DIVIDEND_YIELD`` are annual decimal rates (``0.02`` means 2%).
Product-specific variables such as ``INDEX_FUTURES_IF_FUNDING_RATE`` override
the global values. A trailing percent sign (``2%``) is also accepted.
"""

from __future__ import annotations

import copy
import math
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SINA_FUTURES_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}

INDEX_FUTURES: dict[str, dict[str, Any]] = {
    "IF": {
        "index_code": "000300",
        "index_name": "沪深300",
        "multiplier": 300,
    },
    "IH": {
        "index_code": "000016",
        "index_name": "上证50",
        "multiplier": 300,
    },
    "IC": {
        "index_code": "000905",
        "index_name": "中证500",
        "multiplier": 200,
    },
    "IM": {
        "index_code": "000852",
        "index_name": "中证1000",
        "multiplier": 200,
    },
}
INDEX_TO_PRODUCT = {
    config["index_code"]: product for product, config in INDEX_FUTURES.items()
}

BASIS_INTERPRETATION = (
    "原始基差仅表示期货与现货指数的价差，受资金成本、预期分红、"
    "套保/套利需求和流动性影响，不可直接解释为未来涨跌方向。"
)
FAIR_VALUE_MISSING_REASON = (
    "未同时配置年化资金利率和预期股息率，无法可靠估算公平价值；"
    "公平价值及其残差保持 null，原始基差不可解释为方向。"
)

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_FRESH_SECONDS = 60
DEFAULT_CLOSED_MAX_AGE_DAYS = 10
HISTORY_WINDOWS_MINUTES = (1, 5, 15)
HISTORY_LIMIT = 4096

_SINA_LINE_RE = re.compile(
    r'var\s+hq_str_nf_([A-Za-z]{2}\d{4})="(.*?)";'
)
_CONTRACT_RE = re.compile(r"^([A-Z]{2})(\d{2})(\d{2})$")

_state_lock = threading.RLock()
_last_good_quotes: dict[str, dict[str, Any]] = {}
_quote_history: dict[str, list[dict[str, Any]]] = {}
_settlement_spot_history: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _shanghai_datetime(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _iso_time(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _positive_price(value: Any) -> float | None:
    number = _to_float(value)
    return number if number is not None and number > 0 else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    serial = year * 12 + month - 1 + offset
    return serial // 12, serial % 12 + 1


def third_friday(year: int, month: int) -> date:
    """Return the calendar third Friday for a contract month."""

    fifteenth = date(year, month, 15)
    return fifteenth + timedelta(days=(4 - fifteenth.weekday()) % 7)


def contract_expiry_date(
    contract: str,
    expiry_overrides: Mapping[str, date] | None = None,
) -> date:
    """Return the standard expiry date, with an optional holiday override."""

    normalized = str(contract).upper()
    if expiry_overrides and normalized in expiry_overrides:
        return expiry_overrides[normalized]
    match = _CONTRACT_RE.match(normalized)
    if not match:
        raise ValueError(f"invalid CFFEX equity-index contract: {contract!r}")
    year = 2000 + int(match.group(2))
    month = int(match.group(3))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid contract month: {contract!r}")
    return third_friday(year, month)


def generate_contracts(
    product: str,
    as_of: datetime | None = None,
    *,
    expiry_overrides: Mapping[str, date] | None = None,
) -> list[str]:
    """Generate nearby contracts: front, next, and the next two quarter months.

    After 15:00 on the standard expiry day, the front month rolls forward. The
    optional ``expiry_overrides`` lets a caller supply exchange-calendar shifts
    when the third Friday is not a trading day.
    """

    normalized = str(product).upper()
    if normalized not in INDEX_FUTURES:
        raise ValueError(f"unsupported index-futures product: {product!r}")
    current = _shanghai_datetime(as_of)
    front_year, front_month = current.year, current.month
    current_contract = f"{normalized}{front_year % 100:02d}{front_month:02d}"
    expiry = contract_expiry_date(current_contract, expiry_overrides)
    after_expiry = current.date() > expiry or (
        current.date() == expiry and current.time() > dt_time(15, 0)
    )
    if after_expiry:
        front_year, front_month = _add_months(front_year, front_month, 1)

    months: list[tuple[int, int]] = [
        (front_year, front_month),
        _add_months(front_year, front_month, 1),
    ]
    cursor_year, cursor_month = months[-1]
    while len(months) < 4:
        cursor_year, cursor_month = _add_months(cursor_year, cursor_month, 1)
        if cursor_month in (3, 6, 9, 12):
            months.append((cursor_year, cursor_month))
    return [
        f"{normalized}{year % 100:02d}{month:02d}" for year, month in months
    ]


def _parse_quote_datetime(date_text: str, time_text: str) -> datetime | None:
    clean_date = str(date_text or "").strip().replace("/", "-")
    clean_time = str(time_text or "").strip()
    if not clean_date or not clean_time:
        return None
    for date_format in ("%Y-%m-%d", "%Y%m%d"):
        try:
            parsed_date = datetime.strptime(clean_date, date_format).date()
            parsed_time = datetime.strptime(clean_time[:8], "%H:%M:%S").time()
            return datetime.combine(parsed_date, parsed_time, SHANGHAI_TZ)
        except ValueError:
            continue
    return None


def _market_phase(now: datetime) -> str:
    if now.weekday() >= 5:
        return "closed"
    current_time = now.time()
    if dt_time(9, 30) <= current_time <= dt_time(11, 30):
        return "trading"
    if dt_time(13, 0) <= current_time <= dt_time(15, 0):
        return "trading"
    if dt_time(11, 30) < current_time < dt_time(13, 0):
        return "paused"
    return "closed"


def _freshness(
    quote_time: datetime | None,
    now: datetime,
    *,
    source: str = "sina_nf",
    forced_stale_reason: str | None = None,
) -> dict[str, Any]:
    if quote_time is None:
        return {
            "status": "stale" if forced_stale_reason else "unknown",
            "stale": True,
            "age_seconds": None,
            "source": source,
            "reason": forced_stale_reason or "行情未提供可解析的时间戳",
        }

    age_seconds = max(0.0, (now - quote_time).total_seconds())
    if forced_stale_reason:
        return {
            "status": "stale",
            "stale": True,
            "age_seconds": round(age_seconds, 1),
            "source": source,
            "reason": forced_stale_reason,
        }

    phase = _market_phase(now)
    fresh_seconds = int(
        os.getenv("INDEX_FUTURES_FRESH_SECONDS", str(DEFAULT_FRESH_SECONDS))
    )
    if phase == "trading":
        is_fresh = quote_time.date() == now.date() and age_seconds <= fresh_seconds
        return {
            "status": "fresh" if is_fresh else "stale",
            "stale": not is_fresh,
            "age_seconds": round(age_seconds, 1),
            "source": source,
            "reason": None if is_fresh else "交易时段行情时间戳超出新鲜度阈值",
        }

    closed_age_days = int(
        os.getenv(
            "INDEX_FUTURES_CLOSED_MAX_AGE_DAYS",
            str(DEFAULT_CLOSED_MAX_AGE_DAYS),
        )
    )
    recent_closed_quote = (
        quote_time <= now and (now.date() - quote_time.date()).days <= closed_age_days
    )
    status = phase if recent_closed_quote else "stale"
    return {
        "status": status,
        "stale": not recent_closed_quote,
        "age_seconds": round(age_seconds, 1),
        "source": source,
        "reason": (
            None
            if recent_closed_quote
            else "休市期最近行情已超出允许的闭市年龄"
        ),
    }


def parse_sina_futures_payload(
    payload: str | bytes,
    *,
    requested_contracts: Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Parse actual CFFEX contracts from a Sina ``nf_`` batch response.

    Relevant Sina positions (zero-based) are: last=3, volume=4, OI=6,
    previous close=13, previous settlement=14, previous OI=15, bid=16,
    ask=26, trade date=36, time=37.
    """

    if isinstance(payload, bytes):
        payload = payload.decode("gb18030", errors="replace")
    current = _shanghai_datetime(now)
    requested = (
        {str(contract).upper() for contract in requested_contracts}
        if requested_contracts is not None
        else None
    )
    parsed: dict[str, dict[str, Any]] = {}
    for match in _SINA_LINE_RE.finditer(str(payload)):
        contract = match.group(1).upper()
        if requested is not None and contract not in requested:
            continue
        fields = match.group(2).split(",")
        if len(fields) < 38:
            continue
        last = _positive_price(fields[3])
        if last is None:
            continue
        product = contract[:2]
        if product not in INDEX_FUTURES:
            continue
        bid = _positive_price(fields[16]) if len(fields) > 16 else None
        ask = _positive_price(fields[26]) if len(fields) > 26 else None
        mark = (
            (bid + ask) / 2
            if bid is not None and ask is not None and ask >= bid
            else last
        )
        previous_settlement = (
            _positive_price(fields[14]) if len(fields) > 14 else None
        )
        if previous_settlement is None and len(fields) > 13:
            previous_settlement = _positive_price(fields[13])
        current_oi = _to_int(fields[6]) if len(fields) > 6 else None
        previous_oi = _to_int(fields[15]) if len(fields) > 15 else None
        quote_dt = _parse_quote_datetime(fields[36], fields[37])
        change_pct = (
            (last / previous_settlement - 1) * 100
            if previous_settlement
            else None
        )
        freshness = _freshness(quote_dt, current)
        try:
            expiry = contract_expiry_date(contract)
        except ValueError:
            expiry = None
        parsed[contract] = {
            "contract": contract,
            "product": product,
            "index_code": INDEX_FUTURES[product]["index_code"],
            "name": fields[-1].strip() if fields else contract,
            "last": _round(last, 4),
            "bid": _round(bid, 4),
            "ask": _round(ask, 4),
            "mark": _round(mark, 4),
            "prev_settle": _round(previous_settlement, 4),
            "change_pct": _round(change_pct, 4),
            "volume": _to_int(fields[4]) if len(fields) > 4 else None,
            "OI": current_oi,
            "OI_change": (
                current_oi - previous_oi
                if current_oi is not None and previous_oi is not None
                else None
            ),
            "quote_time": _iso_time(quote_dt),
            "freshness": freshness,
            "stale": freshness["stale"],
            "source": "sina_nf",
            "expiry_date": expiry.isoformat() if expiry else None,
        }
    return parsed


def _fallback_quote(
    cached: Mapping[str, Any],
    *,
    now: datetime,
    reason: str,
) -> dict[str, Any]:
    quote = copy.deepcopy(dict(cached))
    quote_dt = _parse_iso_datetime(quote.get("quote_time"))
    quote["freshness"] = _freshness(
        quote_dt,
        now,
        source="last_good",
        forced_stale_reason=reason,
    )
    quote["source"] = "last_good"
    quote["stale"] = True
    return quote


def fetch_futures_quotes(
    contracts: Sequence[str],
    *,
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    requester: Any = None,
) -> dict[str, Any]:
    """Fetch contracts in one Sina request, preserving last-good on failure.

    Returns batch metadata and a ``quotes`` mapping. A cached fallback is always
    marked stale, even if its original timestamp is recent.
    """

    current = _shanghai_datetime(now)
    requested = list(dict.fromkeys(str(code).upper() for code in contracts))
    invalid = [code for code in requested if not _CONTRACT_RE.match(code)]
    if invalid:
        raise ValueError(f"invalid futures contracts: {', '.join(invalid)}")
    if not requested:
        return {
            "quotes": {},
            "requested": [],
            "received": [],
            "missing": [],
            "source": "sina_nf",
            "stale": False,
            "error": None,
        }

    get = requester.get if requester is not None else requests.get
    error: str | None = None
    live_quotes: dict[str, dict[str, Any]] = {}
    try:
        symbols = ",".join(f"nf_{contract}" for contract in requested)
        response = get(
            SINA_FUTURES_URL + symbols,
            headers=SINA_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        if hasattr(response, "encoding"):
            response.encoding = "gb18030"
        live_quotes = parse_sina_futures_payload(
            response.text,
            requested_contracts=requested,
            now=current,
        )
        if not live_quotes:
            raise RuntimeError("新浪期指批量行情未返回任何有效实际合约")
    except Exception as exc:  # network and upstream format errors share fallback
        error = f"{type(exc).__name__}: {exc}"
        live_quotes = {}

    with _state_lock:
        for contract, quote in live_quotes.items():
            _last_good_quotes[contract] = copy.deepcopy(quote)
        result_quotes = copy.deepcopy(live_quotes)
        missing = [contract for contract in requested if contract not in result_quotes]
        for contract in missing:
            cached = _last_good_quotes.get(contract)
            if cached is not None:
                reason = (
                    f"新浪期指行情请求失败，使用 last-good：{error}"
                    if error
                    else "新浪批量响应缺少该合约，使用 last-good"
                )
                result_quotes[contract] = _fallback_quote(
                    cached,
                    now=current,
                    reason=reason,
                )

    fallback_count = sum(
        quote.get("source") == "last_good" for quote in result_quotes.values()
    )
    missing_after_fallback = [
        contract for contract in requested if contract not in result_quotes
    ]
    quote_stale_count = sum(
        bool(quote.get("stale")) for quote in result_quotes.values()
    )
    if not result_quotes and error:
        source = "unavailable"
    elif result_quotes and fallback_count == len(result_quotes):
        source = "last_good"
    elif fallback_count:
        source = "mixed"
    elif missing_after_fallback:
        source = "partial"
    else:
        source = "sina_nf"
    return {
        "quotes": result_quotes,
        "requested": requested,
        "received": sorted(live_quotes),
        "missing": missing_after_fallback,
        "source": source,
        "stale": bool(
            fallback_count or error or missing_after_fallback or quote_stale_count
        ),
        "error": error,
    }


def select_main_contract(
    quotes: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Select the actual contract with the highest current-session volume."""

    values = list(quotes.values()) if isinstance(quotes, Mapping) else list(quotes)
    valid = [
        quote
        for quote in values
        if quote
        and _CONTRACT_RE.match(str(quote.get("contract", "")).upper())
    ]
    if not valid:
        return None
    selected = max(
        valid,
        key=lambda quote: (
            _to_float(quote.get("volume")) or -1,
            _to_float(quote.get("OI")) or -1,
            str(quote.get("contract")),
        ),
    )
    return copy.deepcopy(dict(selected))


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _shanghai_datetime(value)
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return _shanghai_datetime(parsed)


def _history_session(value: datetime) -> str | None:
    current_time = value.time()
    if dt_time(9, 30) <= current_time <= dt_time(11, 30):
        label = "am"
    elif dt_time(13, 0) <= current_time <= dt_time(15, 0):
        label = "pm"
    else:
        return None
    return f"{value.date().isoformat()}-{label}"


def record_futures_sample(
    contract: str,
    *,
    mark: float,
    basis: float | None = None,
    at: datetime | None = None,
) -> None:
    """Record one in-memory sample, de-duplicated by event timestamp."""

    normalized = str(contract).upper()
    if not _CONTRACT_RE.match(normalized):
        raise ValueError(f"invalid futures contract: {contract!r}")
    mark_value = _positive_price(mark)
    if mark_value is None:
        raise ValueError("mark must be a positive finite number")
    current = _shanghai_datetime(at)
    session = _history_session(current)
    if session is None:
        return
    basis_value = _to_float(basis)
    item = {
        "at": current,
        "session": session,
        "mark": mark_value,
        "basis": basis_value,
    }
    with _state_lock:
        points = _quote_history.setdefault(normalized, [])
        replaced = False
        for index, existing in enumerate(points):
            if existing["at"] == current:
                points[index] = item
                replaced = True
                break
        if not replaced:
            points.append(item)
            points.sort(key=lambda point: point["at"])
        if len(points) > HISTORY_LIMIT:
            del points[:-HISTORY_LIMIT]


def calculate_intraday_changes(
    contract: str,
    *,
    at: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Calculate 1m/5m/15m futures return and point basis change."""

    normalized = str(contract).upper()
    current = _shanghai_datetime(at)
    session = _history_session(current)
    with _state_lock:
        points = [
            copy.deepcopy(point)
            for point in _quote_history.get(normalized, [])
            if point["session"] == session and point["at"] <= current
        ]
    current_point = points[-1] if points else None
    output: dict[str, dict[str, Any]] = {}
    for minutes in HISTORY_WINDOWS_MINUTES:
        label = f"{minutes}m"
        target = current - timedelta(minutes=minutes)
        references = [point for point in points if point["at"] <= target]
        reference = references[-1] if references else None
        tolerance_seconds = max(90, minutes * 60 * 0.25)
        reference_is_close = (
            reference is not None
            and (target - reference["at"]).total_seconds() <= tolerance_seconds
        )
        if current_point is None or not reference_is_close:
            output[label] = {
                "futures_return_pct": None,
                "basis_change": None,
                "reference_time": None,
                "status": "insufficient_history",
            }
            continue
        future_return = (
            (current_point["mark"] / reference["mark"] - 1) * 100
            if reference["mark"]
            else None
        )
        basis_change = (
            current_point["basis"] - reference["basis"]
            if current_point["basis"] is not None
            and reference["basis"] is not None
            else None
        )
        output[label] = {
            "futures_return_pct": _round(future_return, 4),
            "basis_change": _round(basis_change, 4),
            "reference_time": _iso_time(reference["at"]),
            "status": "available",
        }
    return output


def _spot_price(spot_quote: Mapping[str, Any] | float | int | None) -> float | None:
    if isinstance(spot_quote, (float, int)):
        return _positive_price(spot_quote)
    if not isinstance(spot_quote, Mapping):
        return None
    for key in ("last", "price", "close", "mark", "index_price"):
        price = _positive_price(spot_quote.get(key))
        if price is not None:
            return price
    return None


def _spot_time(
    spot_quote: Mapping[str, Any] | float | int | None,
    fallback: datetime,
) -> datetime:
    if isinstance(spot_quote, Mapping):
        for key in ("quote_time", "datetime", "timestamp"):
            parsed = _parse_iso_datetime(spot_quote.get(key))
            if parsed is not None:
                return parsed
        date_text = spot_quote.get("date")
        time_text = spot_quote.get("time")
        parsed = _parse_quote_datetime(
            str(date_text or ""), str(time_text or "")
        )
        if parsed is not None:
            return parsed
    return fallback


def _parse_rate(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    number = _to_float(text)
    if number is None:
        return None
    return number / 100 if is_percent else number


def _configured_rate(product: str, rate_name: str) -> float | None:
    product_key = f"INDEX_FUTURES_{product}_{rate_name}"
    global_key = f"INDEX_FUTURES_{rate_name}"
    return _parse_rate(os.getenv(product_key, os.getenv(global_key)))


def _expiry_datetime(
    contract: str,
    expiry_overrides: Mapping[str, date] | None = None,
) -> datetime:
    expiry = contract_expiry_date(contract, expiry_overrides)
    return datetime.combine(expiry, dt_time(15, 0), SHANGHAI_TZ)


def _basis_fields(
    quote: Mapping[str, Any],
    spot_quote: Mapping[str, Any] | float | int | None,
    *,
    now: datetime,
    expiry_overrides: Mapping[str, date] | None = None,
) -> dict[str, Any]:
    mark = _positive_price(quote.get("mark"))
    spot = _spot_price(spot_quote)
    contract = str(quote.get("contract", "")).upper()
    product = str(quote.get("product") or contract[:2]).upper()
    expiry_at = (
        _expiry_datetime(contract, expiry_overrides)
        if _CONTRACT_RE.match(contract)
        else None
    )
    seconds_to_expiry = (
        max(0.0, (expiry_at - now).total_seconds()) if expiry_at else 0.0
    )
    day_count = _to_float(os.getenv("INDEX_FUTURES_DAY_COUNT", "365")) or 365.0
    years_to_expiry = seconds_to_expiry / (day_count * 24 * 60 * 60)
    days_to_expiry = seconds_to_expiry / (24 * 60 * 60)
    basis = mark - spot if mark is not None and spot is not None else None
    basis_pct = basis / spot * 100 if basis is not None and spot else None
    annualized_basis = (
        basis_pct / years_to_expiry
        if basis_pct is not None and years_to_expiry > 0
        else None
    )

    funding_rate = _configured_rate(product, "FUNDING_RATE")
    dividend_yield = _configured_rate(product, "DIVIDEND_YIELD")
    fair_available = (
        spot is not None
        and mark is not None
        and years_to_expiry > 0
        and funding_rate is not None
        and dividend_yield is not None
    )
    if fair_available:
        fair_value = spot * math.exp(
            (funding_rate - dividend_yield) * years_to_expiry
        )
        fair_basis = fair_value - spot
        fair_residual = mark - fair_value
        fair_reason = None
        fair_status = "available"
    else:
        fair_value = None
        fair_basis = None
        fair_residual = None
        fair_status = "unavailable"
        if spot is None:
            fair_reason = "缺少有效现货指数点位，无法计算公平价值。"
        elif years_to_expiry <= 0:
            fair_reason = "合约已到期，持有成本公平价值不再适用。"
        else:
            fair_reason = FAIR_VALUE_MISSING_REASON
    return {
        "spot_last": _round(spot, 4),
        "basis": _round(basis, 4),
        "basis_pct": _round(basis_pct, 6),
        "annualized_basis": _round(annualized_basis, 6),
        "days_to_expiry": _round(days_to_expiry, 6),
        "annualization_day_count": day_count,
        "annualization_warning": (
            "距到期不足1日，年化基差会被机械放大。"
            if 0 < days_to_expiry < 1
            else None
        ),
        "basis_direction_signal": None,
        "basis_interpretation": BASIS_INTERPRETATION,
        "fair_value": _round(fair_value, 4),
        "fair_basis": _round(fair_basis, 4),
        "fair_basis_residual": _round(fair_residual, 4),
        "fair_value_status": fair_status,
        "fair_value_reason": fair_reason,
        "funding_rate": funding_rate,
        "dividend_yield": dividend_yield,
        "fair_value_model": (
            "spot * exp((funding_rate - dividend_yield) * years_to_expiry)"
            if fair_available
            else None
        ),
    }


def enrich_futures_quote(
    quote: Mapping[str, Any],
    spot_quote: Mapping[str, Any] | float | int | None,
    *,
    now: datetime | None = None,
    record_history: bool = True,
    expiry_overrides: Mapping[str, date] | None = None,
) -> dict[str, Any]:
    """Attach basis, optional fair value, and in-memory horizon metrics."""

    current = _shanghai_datetime(now)
    enriched = copy.deepcopy(dict(quote))
    basis_fields = _basis_fields(
        enriched,
        spot_quote,
        now=current,
        expiry_overrides=expiry_overrides,
    )
    enriched.update(basis_fields)
    quote_dt = _parse_iso_datetime(enriched.get("quote_time")) or current
    spot_dt = _spot_time(spot_quote, current)
    enriched["spot_quote_time"] = _iso_time(spot_dt)
    enriched["quote_skew_seconds"] = _round(
        abs((quote_dt - spot_dt).total_seconds()), 1
    )
    enriched["basis_quality"] = (
        "aligned"
        if enriched["quote_skew_seconds"] is not None
        and enriched["quote_skew_seconds"] <= 10
        else "time_misaligned"
    )
    if (
        record_history
        and not bool(enriched.get("stale"))
        and _positive_price(enriched.get("mark")) is not None
    ):
        record_futures_sample(
            str(enriched["contract"]),
            mark=float(enriched["mark"]),
            basis=_to_float(enriched.get("basis")),
            at=quote_dt,
        )
    enriched["history"] = calculate_intraday_changes(
        str(enriched["contract"]),
        at=quote_dt,
    )
    return enriched


def _settlement_key(product: str, expiry: date) -> tuple[str, str]:
    return product, expiry.isoformat()


def _record_settlement_spot(
    product: str,
    expiry: date,
    spot: float,
    at: datetime,
) -> None:
    key = _settlement_key(product, expiry)
    item = {"at": at, "spot": spot}
    with _state_lock:
        points = _settlement_spot_history.setdefault(key, [])
        for index, existing in enumerate(points):
            if existing["at"] == at:
                points[index] = item
                break
        else:
            points.append(item)
            points.sort(key=lambda point: point["at"])
        # Keep only the official afternoon observation window.
        points[:] = [
            point
            for point in points
            if point["at"].date() == expiry
            and dt_time(13, 0) <= point["at"].time() <= dt_time(15, 0)
        ]


def build_settlement_monitor(
    product: str,
    contracts: Sequence[Mapping[str, Any]],
    spot_quote: Mapping[str, Any] | float | int | None,
    *,
    now: datetime | None = None,
    expiry_overrides: Mapping[str, date] | None = None,
) -> dict[str, Any]:
    """Build the expiry-afternoon running settlement-price approximation."""

    normalized = str(product).upper()
    current = _shanghai_datetime(now)
    expiring: Mapping[str, Any] | None = None
    expiry: date | None = None
    for quote in contracts:
        contract = str(quote.get("contract", "")).upper()
        if not _CONTRACT_RE.match(contract):
            continue
        candidate_expiry = contract_expiry_date(contract, expiry_overrides)
        if candidate_expiry == current.date():
            expiring = quote
            expiry = candidate_expiry
            break
    if expiring is None or expiry is None:
        return {
            "active": False,
            "phase": "inactive",
            "contract": None,
            "window": "13:00:00-15:00:00 Asia/Shanghai",
            "samples": 0,
            "simulated_settlement": None,
            "futures_deviation": None,
            "reason": "今天不是返回合约的到期日。",
        }

    if current.time() < dt_time(13, 0):
        return {
            "active": False,
            "phase": "not_started",
            "contract": expiring["contract"],
            "window": "13:00:00-15:00:00 Asia/Shanghai",
            "samples": 0,
            "simulated_settlement": None,
            "futures_deviation": None,
            "reason": "到期日模拟交割均价从13:00开始记录。",
        }

    spot = _spot_price(spot_quote)
    spot_at = _spot_time(spot_quote, current)
    if (
        spot is not None
        and spot_at.date() == expiry
        and dt_time(13, 0) <= spot_at.time() <= dt_time(15, 0)
    ):
        _record_settlement_spot(normalized, expiry, spot, spot_at)
    key = _settlement_key(normalized, expiry)
    with _state_lock:
        points = copy.deepcopy(_settlement_spot_history.get(key, []))
    simulated = (
        sum(point["spot"] for point in points) / len(points) if points else None
    )
    futures_mark = _positive_price(expiring.get("mark"))
    deviation = (
        futures_mark - simulated
        if futures_mark is not None and simulated is not None
        else None
    )
    phase = "collecting" if current.time() <= dt_time(15, 0) else "complete"
    return {
        "active": phase == "collecting",
        "phase": phase,
        "contract": expiring["contract"],
        "window": "13:00:00-15:00:00 Asia/Shanghai",
        "samples": len(points),
        "first_sample_time": _iso_time(points[0]["at"]) if points else None,
        "last_sample_time": _iso_time(points[-1]["at"]) if points else None,
        "simulated_settlement": _round(simulated, 4),
        "futures_deviation": _round(deviation, 4),
        "method": "本进程收到的spot样本算术平均",
        "reason": (
            None
            if points
            else "尚无有效spot样本，不能模拟交割结算价。"
        ),
        "warning": (
            "这是按调用时收到的spot记录生成的监控近似值；"
            "正式交割结算价以中金所发布结果为准。"
        ),
    }


def _resolve_spot_quote(
    spot_quotes: Mapping[str, Any] | None,
    product: str,
) -> Mapping[str, Any] | float | int | None:
    if not isinstance(spot_quotes, Mapping):
        return None
    index_code = INDEX_FUTURES[product]["index_code"]
    if index_code in spot_quotes:
        return spot_quotes[index_code]
    if product in spot_quotes:
        return spot_quotes[product]
    return None


def get_product_overview(
    product: str,
    spot_quote: Mapping[str, Any] | float | int | None,
    *,
    now: datetime | None = None,
    quotes: Mapping[str, Mapping[str, Any]] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    requester: Any = None,
    expiry_overrides: Mapping[str, date] | None = None,
) -> dict[str, Any]:
    """Build one IF/IH/IC/IM product overview."""

    normalized = str(product).upper()
    if normalized not in INDEX_FUTURES:
        raise ValueError(f"unsupported index-futures product: {product!r}")
    current = _shanghai_datetime(now)
    candidates = generate_contracts(
        normalized,
        current,
        expiry_overrides=expiry_overrides,
    )
    batch: dict[str, Any] | None = None
    if quotes is None:
        batch = fetch_futures_quotes(
            candidates,
            now=current,
            timeout=timeout,
            requester=requester,
        )
        raw_quotes = batch["quotes"]
    else:
        raw_quotes = quotes
    product_quotes = [
        quote
        for contract in candidates
        if (quote := raw_quotes.get(contract)) is not None
    ]
    enriched_contracts = [
        enrich_futures_quote(
            quote,
            spot_quote,
            now=current,
            expiry_overrides=expiry_overrides,
        )
        for quote in product_quotes
    ]
    main = select_main_contract(enriched_contracts)
    settlement = build_settlement_monitor(
        normalized,
        enriched_contracts,
        spot_quote,
        now=current,
        expiry_overrides=expiry_overrides,
    )
    config = INDEX_FUTURES[normalized]
    warnings: list[str] = []
    if not enriched_contracts:
        warnings.append("没有获得任何有效实际合约行情。")
    if _spot_price(spot_quote) is None:
        warnings.append("缺少现货指数点位，基差与公平价值不可用。")
    if batch and batch.get("error"):
        warnings.append(f"新浪行情异常：{batch['error']}")
    quote_sources = {
        str(quote.get("source", "unknown")) for quote in enriched_contracts
    }
    if quote_sources == {"last_good"}:
        product_source = "last_good"
    elif "last_good" in quote_sources:
        product_source = "mixed"
    elif quote_sources == {"sina_nf"}:
        product_source = "sina_nf"
    else:
        product_source = "provided_quotes" if quotes is not None else "unavailable"
    return {
        "product": normalized,
        "index_code": config["index_code"],
        "index_name": config["index_name"],
        "multiplier": config["multiplier"],
        "candidate_contracts": candidates,
        "main_contract": main["contract"] if main else None,
        "quote": main,
        "contracts": enriched_contracts,
        "spot": {
            "last": _round(_spot_price(spot_quote), 4),
            "quote_time": _iso_time(_spot_time(spot_quote, current)),
        },
        "settlement_monitor": settlement,
        "basis_direction_signal": None,
        "basis_interpretation": BASIS_INTERPRETATION,
        "warnings": warnings,
        "source": batch["source"] if batch else product_source,
        "stale": (
            bool(batch.get("stale"))
            if batch
            else bool(not main or any(quote.get("stale") for quote in enriched_contracts))
        ),
    }


def get_index_futures_overview(
    spot_quotes: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    requester: Any = None,
    expiry_overrides: Mapping[str, date] | None = None,
) -> dict[str, Any]:
    """Fetch once and return IF/IH/IC/IM product overviews."""

    current = _shanghai_datetime(now)
    candidate_map = {
        product: generate_contracts(
            product,
            current,
            expiry_overrides=expiry_overrides,
        )
        for product in INDEX_FUTURES
    }
    all_candidates = [
        contract
        for product in INDEX_FUTURES
        for contract in candidate_map[product]
    ]
    batch = fetch_futures_quotes(
        all_candidates,
        now=current,
        timeout=timeout,
        requester=requester,
    )
    products = {
        product: get_product_overview(
            product,
            _resolve_spot_quote(spot_quotes, product),
            now=current,
            quotes=batch["quotes"],
            expiry_overrides=expiry_overrides,
        )
        for product in INDEX_FUTURES
    }
    warnings: list[str] = []
    if batch.get("error"):
        warnings.append(f"新浪期指行情异常，已尽量使用last-good：{batch['error']}")
    if batch.get("missing"):
        warnings.append("缺少合约：" + "、".join(batch["missing"]))
    return {
        "generated_at": _iso_time(current),
        "timezone": "Asia/Shanghai",
        "source": batch["source"],
        "stale": batch["stale"],
        "products": products,
        "warnings": warnings,
        "methodology": {
            "main_contract": "在本次返回的实际合约中按成交量最大选择。",
            "mark": "买一卖一有效且未倒挂时取中价，否则使用最新价。",
            "change_pct": "相对上一交易日结算价计算。",
            "basis": "mark - spot；原始基差不产生方向信号。",
            "annualized_basis": (
                "basis_pct / years_to_expiry，默认365日制；"
                "可由INDEX_FUTURES_DAY_COUNT配置。"
            ),
            "fair_value": (
                "仅在资金利率与预期股息率均配置时使用持有成本模型；"
                "缺任一参数即返回null。"
            ),
            "settlement": (
                "到期日13:00起按本进程收到的spot样本模拟算术平均；"
                "正式结果以中金所为准。"
            ),
            "basis_interpretation": BASIS_INTERPRETATION,
        },
    }


# Integration-friendly aliases.
build_index_futures_overview = get_index_futures_overview
get_four_contract_overview = get_index_futures_overview


def reset_in_memory_state() -> None:
    """Clear last-good and intraday histories (primarily for tests)."""

    with _state_lock:
        _last_good_quotes.clear()
        _quote_history.clear()
        _settlement_spot_history.clear()


__all__ = [
    "BASIS_INTERPRETATION",
    "FAIR_VALUE_MISSING_REASON",
    "INDEX_FUTURES",
    "INDEX_TO_PRODUCT",
    "build_index_futures_overview",
    "build_settlement_monitor",
    "calculate_intraday_changes",
    "contract_expiry_date",
    "enrich_futures_quote",
    "fetch_futures_quotes",
    "generate_contracts",
    "get_four_contract_overview",
    "get_index_futures_overview",
    "get_product_overview",
    "parse_sina_futures_payload",
    "record_futures_sample",
    "reset_in_memory_state",
    "select_main_contract",
    "third_friday",
]
