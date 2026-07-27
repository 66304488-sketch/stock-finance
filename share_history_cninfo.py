#!/usr/bin/env python3
"""CNINFO 点时股本缓存。

AkShare 的 ``stock_share_change_cninfo`` 每次调用都会创建 MiniRacer。
MiniRacer 的全局 address pool 不能安全地在线程中并发初始化，因此这里只在
主线程生成一次 ``Accept-Enckey``，工作线程只执行普通 ``requests.post``。

CNINFO F003N/F022N 的单位没有稳定的机器可读声明。本模块不会猜单位，而是把
最新 F003N 与腾讯当前总股本比较，只接受倍率 ``1`` 或 ``10000`` 中唯一一个
相对误差小于 1% 的候选。F022 仅称“人民币普通股/已流通 A 股代理”，不称中证
自由流通股本。
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
import math
import os
import tempfile
import threading
import time
from typing import Any, Callable, Iterable

import requests

from runtime_paths import data_path


CNINFO_URL = "https://webapi.cninfo.com.cn/api/stock/p_stock2215"
CACHE_VERSION = 2
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_WORKERS = 8
DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 2
UNIT_MULTIPLIERS = (1.0, 10000.0)
MAX_UNIT_RELATIVE_ERROR = 0.01
FETCH_BATCH_SIZE = 80
EMPTY_RECORD_TTL_SECONDS = 6 * 60 * 60

_request_local = threading.local()


def _pooled_post(*args, **kwargs):
    session = getattr(_request_local, "session", None)
    if session is None:
        session = requests.Session()
        _request_local.session = session
    return session.post(*args, **kwargs)


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _date_key(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)[:10].replace("-", "")
    return text if len(text) == 8 and text.isdigit() else None


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_dump(payload: dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        with open(temporary, encoding="utf-8") as handle:
            json.load(handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_cninfo_enckey() -> str:
    """Create the CNINFO request key once on the caller/main thread."""
    from akshare.stock.stock_share_changes_cninfo import (
        _get_file_content_cninfo,
        py_mini_racer,
    )

    engine = py_mini_racer.MiniRacer()
    engine.eval(_get_file_content_cninfo("cninfo.js"))
    return str(engine.call("getResCode1"))


def _headers(enckey: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Enckey": enckey,
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Content-Length": "0",
        "Host": "webapi.cninfo.com.cn",
        "Origin": "https://webapi.cninfo.com.cn",
        "Pragma": "no-cache",
        "Referer": "https://webapi.cninfo.com.cn/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }


def _fetch_one(
    code: str,
    start_date: str,
    end_date: str,
    *,
    enckey: str,
    timeout: int,
    retries: int,
    post: Callable[..., Any],
) -> tuple[str, list[dict[str, Any]], str | None]:
    params = {
        "scode": code,
        "sdate": (
            f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        ),
        "edate": f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}",
    }
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = post(
                CNINFO_URL,
                params=params,
                headers=_headers(enckey),
                timeout=timeout,
            )
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            payload = response.json()
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise ValueError("CNINFO response has no records list")
            return code, records, None
        except Exception as exc:  # partial failures must not abort the batch
            last_error = str(exc)
            if attempt < retries:
                time.sleep(0.15 * (attempt + 1))
    return code, [], last_error or "unknown CNINFO error"


def fetch_cninfo_share_records(
    codes: Iterable[str],
    start_date: str,
    end_date: str,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    enckey_factory: Callable[[], str] = build_cninfo_enckey,
    post: Callable[..., Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch raw records; MiniRacer is never created inside a worker."""
    codes = sorted({str(code).zfill(6) for code in codes})
    if not codes:
        return {}
    # Deliberately called before ThreadPoolExecutor.
    enckey = enckey_factory()
    post = post or _pooled_post
    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 12))) as pool:
        futures = {
            pool.submit(
                _fetch_one,
                code,
                start_date,
                end_date,
                enckey=enckey,
                timeout=timeout,
                retries=retries,
                post=post,
            ): code
            for code in codes
        }
        for future in as_completed(futures):
            code, records, error = future.result()
            result[code] = {"records": records, "error": error}
    return result


def _raw_records_by_code(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Accept the downloader cache and common raw checkpoint layouts."""
    if isinstance(payload, list):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in payload:
            if not isinstance(record, dict):
                continue
            code = str(
                record.get("SECCODE")
                or record.get("证券代码")
                or record.get("code")
                or ""
            ).zfill(6)
            if len(code) == 6:
                grouped.setdefault(code, []).append(record)
        return grouped
    if not isinstance(payload, dict):
        return {}
    candidate = (
        payload.get("stocks")
        or payload.get("records_by_code")
        or payload.get("data")
        or payload
    )
    if not isinstance(candidate, dict):
        return {}
    output: dict[str, list[dict[str, Any]]] = {}
    for raw_code, value in candidate.items():
        code = str(raw_code).zfill(6)
        records = value.get("records") if isinstance(value, dict) else value
        if isinstance(records, list):
            output[code] = [
                record for record in records if isinstance(record, dict)
            ]
    return output


def _calibrate_multiplier(
    records: list[dict[str, Any]],
    current_total_shares: float | None,
) -> tuple[float | None, float | None]:
    if not _finite(current_total_shares) or float(current_total_shares) <= 0:
        return None, None
    dated = [
        (
            _date_key(record.get("VARYDATE") or record.get("变动日期")),
            record,
        )
        for record in records
        if _finite(record.get("F003N") or record.get("总股本"))
    ]
    dated = [item for item in dated if item[0]]
    if not dated:
        return None, None
    _, latest = max(dated, key=lambda item: item[0])
    raw_total = float(latest.get("F003N") or latest.get("总股本"))
    current_total_shares = float(current_total_shares)
    candidates = [
        (
            multiplier,
            abs(raw_total * multiplier / current_total_shares - 1),
        )
        for multiplier in UNIT_MULTIPLIERS
    ]
    accepted = [
        item for item in candidates
        if item[1] < MAX_UNIT_RELATIVE_ERROR
    ]
    if len(accepted) != 1:
        return None, min((error for _, error in candidates), default=None)
    return accepted[0]


def normalize_stock_records(
    code: str,
    records: list[dict[str, Any]],
    current_total_shares: float | None,
    current_circulating_shares: float | None = None,
) -> dict[str, Any]:
    """Calibrate units, sanitize events and isolate ambiguous records."""
    code = str(code).zfill(6)
    if not records:
        return {
            "status": "no_records",
            "calibrated_unit_multiplier": None,
            "calibration_relative_error": None,
            "circulating_calibration_relative_error": None,
            "events": [],
            "error": None,
        }
    multiplier, error = _calibrate_multiplier(
        records, current_total_shares)
    if multiplier is None:
        return {
            "status": "unit_unresolved",
            "calibrated_unit_multiplier": None,
            "calibration_relative_error": error,
            "circulating_calibration_relative_error": None,
            "events": [],
            "error": "F003N 单位无法通过当前总股本唯一校准",
        }
    dated_circulating = [
        (
            _date_key(record.get("VARYDATE") or record.get("变动日期")),
            float(record.get("F022N") or record.get("人民币普通股")),
        )
        for record in records
        if _finite(record.get("F022N") or record.get("人民币普通股"))
    ]
    dated_circulating = [
        item for item in dated_circulating if item[0] and item[1] > 0
    ]
    circulating_error = None
    circulating_calibrated = False
    if (
        dated_circulating
        and _finite(current_circulating_shares)
        and float(current_circulating_shares) > 0
    ):
        _, latest_circulating = max(
            dated_circulating, key=lambda item: item[0])
        circulating_error = abs(
            latest_circulating * multiplier
            / float(current_circulating_shares) - 1
        )
        circulating_calibrated = (
            circulating_error < MAX_UNIT_RELATIVE_ERROR)

    events_by_date: dict[str, dict[str, Any]] = {}
    sorted_records = sorted(
        records,
        key=lambda record: (
            _date_key(record.get("VARYDATE") or record.get("变动日期")) or "",
            _date_key(record.get("DECLAREDATE") or record.get("公告日期")) or "",
        ),
    )
    for record in sorted_records:
        date = _date_key(record.get("VARYDATE") or record.get("变动日期"))
        raw_total = record.get("F003N")
        if raw_total is None:
            raw_total = record.get("总股本")
        if not date or not _finite(raw_total) or float(raw_total) <= 0:
            continue
        total_shares = float(raw_total) * multiplier
        raw_circulating = record.get("F022N")
        if raw_circulating is None:
            raw_circulating = record.get("人民币普通股")
        circulating = (
            float(raw_circulating) * multiplier
            if _finite(raw_circulating) and float(raw_circulating) > 0
            else None
        )
        # F022 can contain a different security class or malformed unit.
        if (
            not circulating_calibrated
            or (
                circulating is not None
                and circulating > total_shares * 1.01
            )
        ):
            circulating = None
        events_by_date[date] = {
            "date": date,
            "announce_date": _date_key(
                record.get("DECLAREDATE") or record.get("公告日期")),
            "total_shares": round(total_shares),
            "circulating_a_shares": (
                round(circulating) if circulating is not None else None
            ),
            "reason_code": record.get("F001V") or record.get("变动原因编码"),
            "reason": record.get("F002V") or record.get("变动原因"),
        }
    events = [events_by_date[date] for date in sorted(events_by_date)]
    return {
        "status": "ok" if events else "no_valid_events",
        "calibrated_unit_multiplier": multiplier,
        "calibration_relative_error": error,
        "circulating_calibration_relative_error": circulating_error,
        "circulating_calibrated": circulating_calibrated,
        "events": events,
        "error": None if events else "没有通过校验的股本事件",
    }


def normalize_raw_checkpoint(
    payload: Any,
    current_shares: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
    current_circulating_shares: dict[str, Any] | None = None,
) -> dict[str, Any]:
    grouped = _raw_records_by_code(payload)
    now = _iso_now()
    stocks: dict[str, dict[str, Any]] = {}
    for code, records in grouped.items():
        item = normalize_stock_records(
            code,
            records,
            current_shares.get(code),
            (current_circulating_shares or {}).get(code),
        )
        item.update({
            "fetched_at": now,
            "query_start": start_date,
            "query_end": end_date,
        })
        stocks[code] = item
    return {
        "schema_version": CACHE_VERSION,
        "source": "CNINFO p_stock2215 via AkShare-compatible request",
        "updated_at": now,
        "query_start": start_date,
        "query_end": end_date,
        "calibrated_unit": (
            "shares; F003N/F022N multiplier individually calibrated "
            "against Tencent current total shares"
        ),
        "circulating_field": "F022N 人民币普通股",
        "circulating_disclaimer": (
            "circulating_share_proxy_not_csi_free_float"
        ),
        "stocks": stocks,
    }


def import_raw_checkpoint(
    raw_path: str,
    current_shares: dict[str, Any],
    *,
    current_circulating_shares: dict[str, Any] | None = None,
    cache_path: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Merge a resumable raw downloader checkpoint into the normalized cache."""
    with open(raw_path, encoding="utf-8") as handle:
        raw_payload = json.load(handle)
    query_start = (
        start_date
        or (raw_payload.get("query_start") if isinstance(raw_payload, dict)
            else None)
        or "19900101"
    )
    query_end = (
        end_date
        or (raw_payload.get("query_end") if isinstance(raw_payload, dict)
            else None)
        or datetime.now().strftime("%Y%m%d")
    )
    query_start = str(query_start).replace("-", "")
    query_end = str(query_end).replace("-", "")
    normalized = normalize_raw_checkpoint(
        raw_payload,
        current_shares,
        current_circulating_shares=current_circulating_shares,
        start_date=query_start,
        end_date=query_end,
    )
    cache_path = cache_path or data_path(
        "market_cap_share_history_cninfo.json")
    cache = _load_cache(cache_path)
    cache.setdefault("stocks", {}).update(normalized["stocks"])
    for key in (
        "source", "updated_at", "calibrated_unit", "circulating_field",
        "circulating_disclaimer",
    ):
        cache[key] = normalized.get(key)
    cache["query_start"] = min(
        query_start, str(cache.get("query_start") or query_start))
    cache["query_end"] = max(
        query_end, str(cache.get("query_end") or query_end))
    cache["imported_raw_checkpoint"] = os.path.abspath(raw_path)
    cache["checkpoint_completed_codes"] = len(cache["stocks"])
    _atomic_dump(cache, cache_path)
    return cache


def _load_cache(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if (
            isinstance(payload, dict)
            and payload.get("schema_version") == CACHE_VERSION
            and isinstance(payload.get("stocks"), dict)
        ):
            return payload
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {
        "schema_version": CACHE_VERSION,
        "source": "CNINFO p_stock2215 via AkShare-compatible request",
        "updated_at": None,
        "query_start": None,
        "query_end": None,
        "calibrated_unit": None,
        "circulating_field": "F022N 人民币普通股",
        "circulating_disclaimer":
            "circulating_share_proxy_not_csi_free_float",
        "stocks": {},
    }


def _fresh_for_range(
    item: dict[str, Any],
    start_date: str,
    end_date: str,
    now: datetime,
) -> bool:
    fetched_at = item.get("fetched_at")
    try:
        age = (now - datetime.fromisoformat(fetched_at)).total_seconds()
    except (TypeError, ValueError):
        return False
    status = item.get("status")
    ttl = (
        EMPTY_RECORD_TTL_SECONDS
        if status in {"no_records", "no_valid_events"}
        else CACHE_TTL_SECONDS
    )
    return (
        age < ttl
        and str(item.get("query_start") or "99999999") <= start_date
        and str(item.get("query_end") or "") >= end_date
        and status in {
            "ok", "no_records", "no_valid_events", "unit_unresolved"
        }
    )


def refresh_cninfo_share_cache(
    codes: Iterable[str],
    current_shares: dict[str, Any],
    start_date: str,
    end_date: str,
    *,
    current_circulating_shares: dict[str, Any] | None = None,
    cache_path: str | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    force: bool = False,
    fetcher: Callable[..., dict[str, dict[str, Any]]] =
        fetch_cninfo_share_records,
) -> dict[str, Any]:
    """Refresh only stale/missing codes and preserve partial successes."""
    cache_path = cache_path or data_path(
        "market_cap_share_history_cninfo.json")
    cache = _load_cache(cache_path)
    stocks = cache.setdefault("stocks", {})
    now = datetime.now()
    normalized_codes = sorted({str(code).zfill(6) for code in codes})
    pending = [
        code for code in normalized_codes
        if force or not _fresh_for_range(
            stocks.get(code) or {}, start_date, end_date, now)
    ]
    def update_metadata(remaining: int) -> None:
        cache.update({
            "schema_version": CACHE_VERSION,
            "source": "CNINFO p_stock2215 via AkShare-compatible request",
            "updated_at": _iso_now(),
            "query_start": min(
                [start_date, str(cache.get("query_start") or start_date)]),
            "query_end": max(
                [end_date, str(cache.get("query_end") or end_date)]),
            "calibrated_unit": (
                "shares; multiplier 1 or 10000 accepted only when uniquely "
                "within 1% of Tencent current total shares"
            ),
            "circulating_field": "F022N 人民币普通股",
            "circulating_disclaimer":
                "circulating_share_proxy_not_csi_free_float",
            "requested_codes": len(normalized_codes),
            "checkpoint_completed_codes": (
                len(normalized_codes) - remaining),
            "checkpoint_pending_codes": remaining,
        })

    # A small batch gets a fresh Accept-Enckey and is atomically checkpointed
    # before the next batch. An interruption therefore loses at most 80 names,
    # not the entire market.
    for batch_start in range(0, len(pending), FETCH_BATCH_SIZE):
        batch = pending[batch_start:batch_start + FETCH_BATCH_SIZE]
        try:
            fetched = fetcher(
                batch,
                start_date,
                end_date,
                max_workers=max_workers,
            )
        except Exception as exc:
            fetched = {
                code: {"records": [], "error": str(exc)}
                for code in batch
            }
        fetched_at = _iso_now()
        for code in batch:
            response = fetched.get(code) or {}
            error = response.get("error")
            if error:
                previous = stocks.get(code)
                if previous and previous.get("status") == "ok":
                    previous["last_refresh_error"] = str(error)
                    continue
                stocks[code] = {
                    "status": "fetch_failed",
                    "events": [],
                    "calibrated_unit_multiplier": None,
                    "calibration_relative_error": None,
                    "circulating_calibration_relative_error": None,
                    "error": str(error),
                    "fetched_at": fetched_at,
                    "query_start": start_date,
                    "query_end": end_date,
                }
                continue
            item = normalize_stock_records(
                code,
                response.get("records") or [],
                current_shares.get(code),
                (current_circulating_shares or {}).get(code),
            )
            item.update({
                "fetched_at": fetched_at,
                "query_start": start_date,
                "query_end": end_date,
            })
            stocks[code] = item
        remaining = max(
            0, len(pending) - batch_start - len(batch))
        update_metadata(remaining)
        _atomic_dump(cache, cache_path)

    update_metadata(0)
    _atomic_dump(cache, cache_path)
    return cache


def build_point_in_time_share_payload(
    cache: dict[str, Any],
    target_dates: Iterable[str],
) -> dict[str, Any]:
    """Forward-fill the last known event on or before each market date."""
    dates = sorted({
        str(date).replace("-", "") for date in target_dates
        if len(str(date).replace("-", "")) == 8
    })
    total_shares: dict[str, dict[str, int]] = {}
    circulating: dict[str, dict[str, int]] = {}
    events_output: dict[str, list[dict[str, Any]]] = {}
    status_counts: Counter[str] = Counter()
    calibrated_multipliers: Counter[str] = Counter()
    stocks = cache.get("stocks") or {}
    for code, item in stocks.items():
        status_counts[str(item.get("status") or "unknown")] += 1
        multiplier = item.get("calibrated_unit_multiplier")
        if _finite(multiplier):
            calibrated_multipliers[str(float(multiplier))] += 1
        events = sorted(
            [
                event for event in (item.get("events") or [])
                if isinstance(event, dict) and _date_key(event.get("date"))
            ],
            key=lambda event: event["date"],
        )
        if not events:
            continue
        events_output[str(code).zfill(6)] = events
        total_by_date: dict[str, int] = {}
        circulating_by_date: dict[str, int] = {}
        position = 0
        active = None
        for date in dates:
            while position < len(events) and events[position]["date"] <= date:
                active = events[position]
                position += 1
            if active is None:
                continue
            total = active.get("total_shares")
            if _finite(total) and float(total) > 0:
                total_by_date[date] = round(float(total))
            value = active.get("circulating_a_shares")
            if _finite(value) and float(value) > 0:
                circulating_by_date[date] = round(float(value))
        if total_by_date:
            total_shares[str(code).zfill(6)] = total_by_date
        if circulating_by_date:
            circulating[str(code).zfill(6)] = circulating_by_date
    return {
        "schema_version": 1,
        "source": cache.get("source"),
        "updated_at": cache.get("updated_at"),
        "query_start": cache.get("query_start"),
        "query_end": cache.get("query_end"),
        "lookback_start": dates[0] if dates else None,
        "lookback_end": dates[-1] if dates else None,
        "calibrated_unit": cache.get("calibrated_unit"),
        "calibrated_unit_counts": dict(calibrated_multipliers),
        "status_counts": dict(status_counts),
        "total_shares": total_shares,
        "circulating_a_shares": circulating,
        "events": events_output,
        "circulating_field": cache.get("circulating_field"),
        "circulating_disclaimer": cache.get(
            "circulating_disclaimer"),
    }


def refresh_point_in_time_share_history(
    codes: Iterable[str],
    target_dates: Iterable[str],
    current_shares: dict[str, Any],
    *,
    current_circulating_shares: dict[str, Any] | None = None,
    cache_path: str | None = None,
    output_path: str | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    force: bool = False,
    fetcher: Callable[..., dict[str, dict[str, Any]]] =
        fetch_cninfo_share_records,
) -> dict[str, Any]:
    """Refresh the persistent cache and publish a forward-filled history."""
    target_dates = sorted({str(date).replace("-", "") for date in target_dates})
    if not target_dates:
        return {
            "schema_version": 1,
            "source": None,
            "total_shares": {},
            "circulating_a_shares": {},
            "events": {},
        }
    earliest = datetime.strptime(target_dates[0], "%Y%m%d")
    # Two calendar years normally contains a baseline before a 60-session
    # window while keeping the response bounded. Older events already in cache
    # remain available and the range only expands.
    query_start = (earliest - timedelta(days=730)).strftime("%Y%m%d")
    query_end = target_dates[-1]
    cache = refresh_cninfo_share_cache(
        codes,
        current_shares,
        query_start,
        query_end,
        current_circulating_shares=current_circulating_shares,
        cache_path=cache_path,
        max_workers=max_workers,
        force=force,
        fetcher=fetcher,
    )
    payload = build_point_in_time_share_payload(cache, target_dates)
    output_path = output_path or data_path(
        "market_cap_point_in_time_shares.json")
    _atomic_dump(payload, output_path)
    return payload


__all__ = [
    "build_cninfo_enckey",
    "build_point_in_time_share_payload",
    "fetch_cninfo_share_records",
    "import_raw_checkpoint",
    "normalize_raw_checkpoint",
    "normalize_stock_records",
    "refresh_cninfo_share_cache",
    "refresh_point_in_time_share_history",
]
