"""Industry sentiment reversal radar built from existing causal daily features.

The page deliberately separates descriptive sentiment position from two event
frequencies: a bottom-rebound event and a top-cooldown event.  The frequencies
below are frozen research calibrations from the local 2025-05-14..2026-08-04
sample.  They are not presented as a fully out-of-sample trading probability.
"""

from __future__ import annotations

import json
import math
import os
import pickle
import re
import threading
from datetime import datetime
from typing import Any

import pandas as pd


SCHEME_LABELS = {"sw": "申万一级", "ths": "同花顺", "sw3": "申万三级"}
SCHEME_SUFFIX = {"sw": "", "ths": "_ths", "sw3": "_sw3"}
MIN_MEMBERS = {"sw": 10, "ths": 8, "sw3": 4}
HORIZONS = (1, 2, 3, 4, 5)


def _calibration(samples, dates, probabilities, strong_t5, excess_t5, ci_t5):
    return {
        "samples": samples,
        "independent_dates": dates,
        "probabilities": probabilities,
        "strong_t5": strong_t5,
        "avg_excess_t5": excess_t5,
        "ci_t5": ci_t5,
    }


# Compact, auditable output of the research run.  Keys describe mutually
# understandable conditions; the UI exposes the Chinese labels below.
RESEARCH_CALIBRATION = {
    "sw": {
        "bottom": {
            "base": _calibration(1158, 244, [25.2, 28.2, 29.6, 29.9, 31.7], 24.9, -0.131, [25.4, 38.5]),
            "confirm_not_concentrated": _calibration(141, 66, [27.7, 38.3, 40.4, 36.2, 40.4], 32.6, 0.221, [30.3, 49.3]),
            "panic_confirm": _calibration(130, 50, [31.5, 36.2, 36.2, 37.7, 40.0], 30.8, 0.195, [27.5, 49.3]),
            "confirm_active": _calibration(187, 80, [31.0, 38.5, 38.5, 36.9, 39.6], 29.9, 0.103, [29.9, 47.2]),
            "triple_confirm": _calibration(214, 84, [29.0, 36.9, 36.9, 35.0, 38.3], 29.9, 0.039, [28.9, 46.5]),
            "extreme": _calibration(629, 200, [25.4, 27.5, 27.2, 28.0, 28.3], 21.8, -0.334, [20.8, 37.4]),
        },
        "top": {
            "base": _calibration(2103, 269, [40.1, 38.0, 36.4, 36.7, 36.5], 23.9, -0.262, [31.9, 41.7]),
            "new_high_extreme": _calibration(415, 130, [41.9, 38.6, 37.1, 38.3, 39.8], 28.4, -0.270, [31.7, 48.1]),
            "new_high_contraction": _calibration(235, 99, [38.7, 37.9, 38.3, 37.9, 39.6], 27.7, -0.300, [33.2, 46.3]),
            "distribution_break": _calibration(303, 140, [34.3, 34.7, 37.6, 39.3, 39.3], 27.4, -0.456, [32.3, 46.9]),
            "euphoria": _calibration(530, 183, [46.8, 41.3, 38.3, 39.2, 37.9], 28.9, -0.235, [31.0, 44.6]),
            "euphoria_break": _calibration(127, 71, [37.0, 35.4, 37.0, 42.5, 37.8], 27.6, -0.401, [27.7, 47.9]),
            "extreme": _calibration(1000, 260, [40.9, 37.5, 36.9, 38.0, 37.9], 27.2, -0.389, [32.4, 44.0]),
            "distribution": _calibration(523, 191, [42.4, 39.6, 37.1, 36.5, 37.3], 26.0, -0.274, [31.4, 43.9]),
        },
    },
    "ths": {
        "bottom": {
            "base": _calibration(3364, 264, [26.8, 28.9, 29.3, 30.4, 31.5], 23.3, -0.321, [25.7, 37.5]),
            "absorption_turn": _calibration(29, 12, [37.9, 37.9, 55.2, 55.2, 58.6], 37.9, 0.163, [47.8, 65.5]),
            "absorption": _calibration(48, 30, [27.1, 22.9, 37.5, 45.8, 43.8], 29.2, -0.016, [27.8, 57.5]),
            "panic_confirm": _calibration(406, 93, [29.6, 32.5, 35.2, 38.9, 39.9], 28.6, -0.114, [30.2, 47.9]),
            "confirm_active": _calibration(520, 130, [29.4, 34.6, 35.8, 36.7, 39.8], 28.7, -0.080, [32.3, 46.1]),
            "triple_confirm": _calibration(650, 134, [28.3, 33.5, 34.0, 34.8, 37.8], 27.5, -0.226, [30.9, 43.9]),
            "confirm_not_concentrated": _calibration(469, 123, [29.6, 35.2, 34.5, 33.0, 37.5], 27.3, -0.287, [29.8, 44.5]),
            "extreme": _calibration(1738, 252, [26.8, 28.5, 28.4, 29.1, 31.0], 23.0, -0.375, [24.5, 37.7]),
        },
        "top": {
            "base": _calibration(5166, 269, [41.8, 40.6, 38.9, 38.7, 38.7], 26.5, -0.238, [34.8, 42.8]),
            "euphoria_break": _calibration(291, 126, [38.5, 41.2, 40.5, 41.6, 43.0], 32.0, -0.453, [35.7, 49.8]),
            "new_high_contraction": _calibration(643, 141, [41.5, 43.4, 44.0, 42.3, 42.8], 30.5, -0.297, [36.5, 48.7]),
            "new_high_extreme": _calibration(1179, 157, [43.3, 41.3, 40.1, 41.9, 42.4], 29.7, -0.245, [35.4, 49.5]),
            "distribution": _calibration(1206, 238, [45.5, 43.1, 42.6, 41.4, 41.8], 29.3, -0.266, [36.9, 46.5]),
            "euphoria": _calibration(1342, 231, [46.9, 41.4, 39.8, 40.8, 41.6], 29.9, -0.295, [36.2, 46.8]),
            "distribution_break": _calibration(726, 193, [40.5, 43.0, 41.6, 40.2, 40.9], 30.0, -0.397, [35.3, 46.6]),
            "extreme": _calibration(2384, 269, [41.8, 40.7, 38.8, 38.9, 39.4], 28.6, -0.332, [34.9, 44.1]),
        },
    },
    "sw3": {
        "bottom": {
            "base": _calibration(15206, 271, [28.2, 29.9, 30.7, 31.4, 31.8], 23.4, -0.368, [27.5, 36.1]),
            "absorption_turn": _calibration(180, 75, [28.9, 33.9, 38.9, 39.4, 39.4], 23.3, -0.255, [23.5, 52.4]),
            "panic_confirm": _calibration(1849, 182, [29.0, 31.5, 32.8, 35.9, 34.8], 24.9, -0.287, [28.5, 40.4]),
            "confirm_active": _calibration(2274, 217, [29.1, 33.0, 33.7, 34.5, 34.2], 24.5, -0.343, [28.4, 39.1]),
            "absorption": _calibration(244, 94, [23.0, 27.0, 33.2, 36.9, 34.4], 21.3, -0.322, [25.2, 42.0]),
            "confirm_not_concentrated": _calibration(1923, 197, [27.5, 33.0, 33.3, 33.4, 34.0], 24.1, -0.359, [28.0, 39.0]),
            "triple_confirm": _calibration(3126, 227, [28.0, 31.8, 32.7, 33.4, 33.5], 24.2, -0.405, [28.3, 38.2]),
            "extreme": _calibration(7404, 269, [28.7, 29.7, 30.1, 31.4, 31.4], 23.2, -0.430, [26.2, 36.9]),
        },
        "top": {
            "base": _calibration(18405, 269, [43.5, 42.5, 40.7, 40.6, 40.9], 29.2, -0.182, [37.9, 44.3]),
            "euphoria_break": _calibration(1155, 204, [40.2, 42.7, 41.9, 43.6, 44.3], 34.3, -0.303, [39.3, 49.4]),
            "euphoria": _calibration(4944, 262, [46.0, 43.8, 42.6, 41.8, 42.9], 32.5, -0.217, [38.8, 47.0]),
            "new_high_extreme": _calibration(5320, 167, [43.9, 42.6, 41.1, 42.0, 42.7], 30.5, -0.125, [38.2, 47.1]),
            "extreme": _calibration(8988, 269, [43.3, 42.2, 41.6, 41.3, 41.8], 30.9, -0.309, [38.6, 45.3]),
            "new_high_contraction": _calibration(2476, 164, [41.1, 42.4, 41.9, 42.2, 41.7], 30.6, -0.102, [37.0, 46.1]),
            "distribution": _calibration(4123, 254, [44.3, 44.5, 42.5, 40.8, 41.5], 30.1, -0.060, [37.5, 45.3]),
            "distribution_break": _calibration(2467, 250, [41.0, 42.9, 40.6, 40.5, 40.8], 29.8, -0.030, [36.8, 44.3]),
        },
    },
}


CONDITION_LABELS = {
    "base": "情绪极端候选",
    "extreme": "仅极端位置",
    "absorption": "高努力低响应（疑似吸收）",
    "absorption_turn": "吸收后广度与方向改善",
    "triple_confirm": "价格、广度、方向成交三重确认",
    "panic_confirm": "此前恐慌、当前三重确认",
    "confirm_active": "三重确认且活跃广度转正",
    "confirm_not_concentrated": "三重确认且成交不过度集中",
    "new_high_extreme": "20日新高占比处于历史前20%",
    "new_high_contraction": "新高极值后开始收缩",
    "euphoria": "放量普涨（情绪亢奋）",
    "distribution": "高努力低响应（疑似派发）",
    "euphoria_break": "此前亢奋、当前三重转负",
    "distribution_break": "派发后方向与广度共同恶化",
}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _read_json(path: str, required: bool = True) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError(f"{os.path.basename(path)} 格式不是对象")
        return value
    except (OSError, json.JSONDecodeError, ValueError):
        if required:
            raise
        return {}


_STOCK_DETAIL_LOCK = threading.RLock()
_STOCK_FRAME_CACHE: dict[str, Any] = {
    "signature": None, "frames": {}, "updated_at": None,
}
_INDUSTRY_MAP_CACHE: dict[str, Any] = {"signature": None, "maps": {}}
_STOCK_META_CACHE: dict[str, Any] = {"signature": None, "stocks": {}}


def _file_signature(path: str) -> tuple[str, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return os.path.realpath(path), stat.st_mtime_ns, stat.st_size


def _normalise_trade_date(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or "").strip())
    if len(digits) < 8:
        return None
    candidate = digits[:8]
    try:
        datetime.strptime(candidate, "%Y%m%d")
    except ValueError:
        return None
    return candidate


def _load_industry_maps(resource_dir: str) -> dict[str, dict[str, str]]:
    """Load the exact three classification maps used by the daily engines."""
    resource_dir = os.path.realpath(resource_dir)
    xlsx = os.path.join(resource_dir, "industry_stock_map.xlsx")
    ths_path = os.path.join(resource_dir, "industry_map_ths.json")
    taxonomy_path = os.path.join(resource_dir, "industry_taxonomy.json")
    signature = (
        _file_signature(xlsx),
        _file_signature(ths_path),
        _file_signature(taxonomy_path),
    )
    with _STOCK_DETAIL_LOCK:
        if _INDUSTRY_MAP_CACHE.get("signature") == signature:
            return _INDUSTRY_MAP_CACHE["maps"]

        sw: dict[str, str] = {}
        if os.path.exists(xlsx):
            frame = pd.read_excel(
                xlsx,
                sheet_name="个股行业映射",
                dtype={"股票代码": str},
            )
            if {"股票代码", "行业名称"}.issubset(frame.columns):
                for code, industry in frame[["股票代码", "行业名称"]].itertuples(index=False, name=None):
                    code = str(code or "").zfill(6)
                    label = str(industry).strip() if pd.notna(industry) else "其他"
                    if re.fullmatch(r"\d{6}", code):
                        sw[code] = label or "其他"

        ths_payload = _read_json(ths_path, required=False)
        ths_direct = {
            str(code).zfill(6): str(industry or "其他").strip()
            for code, industry in ths_payload.items()
            if re.fullmatch(r"\d{6}", str(code).zfill(6))
        }
        taxonomy_payload = _read_json(taxonomy_path, required=False)
        taxonomy = taxonomy_payload.get("stocks")
        taxonomy = taxonomy if isinstance(taxonomy, dict) else {}
        all_codes = set(sw) | set(ths_direct) | {
            str(code).zfill(6) for code in taxonomy
        }
        ths = {
            code: ths_direct.get(code) or sw.get(code) or "其他"
            for code in all_codes
        }
        sw3 = {}
        for code in all_codes:
            details = taxonomy.get(code)
            details = details if isinstance(details, dict) else {}
            sw3[code] = (
                details.get("sw_level3")
                or details.get("sw_level2")
                or details.get("sw_level1")
                or sw.get(code)
                or "其他"
            )
        maps = {"sw": sw, "ths": ths, "sw3": sw3}
        _INDUSTRY_MAP_CACHE.update({"signature": signature, "maps": maps})
        return maps


def _first_existing(data_dir: str, resource_dir: str, filename: str) -> str | None:
    for root in (data_dir, resource_dir):
        path = os.path.join(root, filename)
        if os.path.exists(path):
            return path
    return None


def _load_stock_metadata(
    data_dir: str, resource_dir: str,
) -> dict[str, dict[str, Any]]:
    """Build a local code/name/market-cap lookup without a network call."""
    filenames = ("market_cap_sw3.json", "market_cap_ths.json", "market_cap.json")
    paths = []
    for filename in filenames:
        path = _first_existing(data_dir, resource_dir, filename)
        if path:
            paths.append(path)
    signature = tuple(_file_signature(path) for path in paths)
    with _STOCK_DETAIL_LOCK:
        if _STOCK_META_CACHE.get("signature") == signature:
            return _STOCK_META_CACHE["stocks"]

        stocks: dict[str, dict[str, Any]] = {}
        for path in paths:
            payload = _read_json(path, required=False)
            snapshot_date = _normalise_trade_date(
                payload.get("trade_date") or payload.get("as_of"))
            if snapshot_date is None:
                dates = payload.get("dates") or []
                if dates and isinstance(dates[0], dict):
                    snapshot_date = _normalise_trade_date(
                        dates[0].get("full_label") or dates[0].get("label"))
            for industry_row in payload.get("industries") or []:
                for row in industry_row.get("stocks") or []:
                    code = str(row.get("code") or "").zfill(6)
                    if not re.fullmatch(r"\d{6}", code):
                        continue
                    current = stocks.setdefault(code, {})
                    if row.get("name") and not current.get("name"):
                        current["name"] = str(row["name"])
                    for source, destination in (
                        ("mcap", "mcap"),
                        ("close", "close"),
                        ("price", "close"),
                        ("change_pct", "change_pct"),
                    ):
                        value = _number(row.get(source))
                        if value is not None and current.get(destination) is None:
                            current[destination] = value
                    if snapshot_date and not current.get("data_date"):
                        current["data_date"] = snapshot_date
        _STOCK_META_CACHE.update({"signature": signature, "stocks": stocks})
        return stocks


def _load_stock_frames(
    data_dir: str, resource_dir: str,
) -> tuple[dict[str, Any], str | None, str | None]:
    cache_path = _first_existing(data_dir, resource_dir, "kline_cache.pkl")
    if not cache_path:
        return {}, None, None
    signature = _file_signature(cache_path)
    with _STOCK_DETAIL_LOCK:
        if _STOCK_FRAME_CACHE.get("signature") != signature:
            try:
                with open(cache_path, "rb") as handle:
                    payload = pickle.load(handle)
            except (OSError, EOFError, pickle.UnpicklingError):
                payload = {}
            frames = payload.get("data") if isinstance(payload, dict) else {}
            _STOCK_FRAME_CACHE.update({
                "signature": signature,
                "frames": frames if isinstance(frames, dict) else {},
                "updated_at": (
                    payload.get("updated_at") if isinstance(payload, dict) else None
                ),
            })
        return (
            _STOCK_FRAME_CACHE["frames"],
            _STOCK_FRAME_CACHE.get("updated_at"),
            cache_path,
        )


def _stock_role(
    change_pct: float | None, rvol_20: float | None,
) -> tuple[str, str]:
    if change_pct is None:
        return "行情缺失", "missing"
    volume_hot = rvol_20 is not None and rvol_20 >= 1.3
    if change_pct >= 3 and volume_hot:
        return "放量领涨", "leader_up"
    if change_pct >= 1:
        return "反弹先锋", "rebound"
    if change_pct <= -3 and volume_hot:
        return "放量领跌", "leader_down"
    if change_pct <= -1:
        return "主要拖累", "drag"
    if volume_hot:
        return "放量分歧", "divergence"
    return "跟随", "neutral"


def _frame_stock_metrics(frame: Any, trade_date: str) -> dict[str, Any]:
    required = {"date", "close"}
    if (
        frame is None
        or getattr(frame, "empty", True)
        or not required.issubset(frame.columns)
    ):
        return {}
    dates = pd.to_datetime(frame["date"], errors="coerce")
    target = pd.Timestamp(datetime.strptime(trade_date, "%Y%m%d"))
    valid = frame.loc[dates.notna() & (dates <= target)].copy()
    if valid.empty:
        return {}
    valid["_date"] = dates.loc[valid.index]
    valid = valid.sort_values("_date")
    latest = valid.iloc[-1]
    close = _number(latest.get("close"))
    volume = _number(latest.get("volume"))
    previous_close = _number(valid.iloc[-2].get("close")) if len(valid) >= 2 else None
    change_pct = ((close / previous_close - 1) * 100) if close and previous_close else None
    base_5d = _number(valid.iloc[-6].get("close")) if len(valid) >= 6 else None
    return_5d = ((close / base_5d - 1) * 100) if close and base_5d else None
    prior_volume_mean = None
    if "volume" in valid.columns:
        prior_volumes = pd.to_numeric(
            valid.iloc[max(0, len(valid) - 21):-1]["volume"], errors="coerce")
        positive_volumes = prior_volumes[prior_volumes > 0]
        if not positive_volumes.empty:
            prior_volume_mean = float(positive_volumes.mean())
    rvol_20 = volume / prior_volume_mean if volume and prior_volume_mean else None
    recent_closes = pd.to_numeric(valid.iloc[-20:]["close"], errors="coerce").dropna()
    high_20 = float(recent_closes.max()) if not recent_closes.empty else None
    low_20 = float(recent_closes.min()) if not recent_closes.empty else None
    return {
        "data_date": latest["_date"].strftime("%Y%m%d"),
        "close": close,
        "change_pct": change_pct,
        "return_5d_pct": return_5d,
        "rvol_20": rvol_20,
        "amount": close * volume if close is not None and volume is not None else None,
        "distance_from_high_20d_pct": (
            (close / high_20 - 1) * 100 if close and high_20 else None
        ),
        "distance_from_low_20d_pct": (
            (close / low_20 - 1) * 100 if close and low_20 else None
        ),
    }


def build_sentiment_radar_stocks(
    data_dir: str,
    resource_dir: str,
    scheme: str,
    industry: str,
    trade_date: str,
) -> dict[str, Any]:
    """Return every mapped member and point-in-time metrics aligned to the radar date."""
    if scheme not in SCHEME_SUFFIX:
        raise ValueError("scheme must be sw, ths or sw3")
    industry = str(industry or "").strip()
    if not industry:
        raise ValueError("industry is required")
    trade_date = _normalise_trade_date(trade_date) or ""
    if not trade_date:
        raise ValueError("trade_date must be YYYYMMDD")

    industry_map = _load_industry_maps(resource_dir).get(scheme, {})
    member_codes = sorted(
        code for code, label in industry_map.items() if label == industry)
    if not member_codes:
        raise LookupError(f"未找到{SCHEME_LABELS[scheme]}板块：{industry}")

    metadata = _load_stock_metadata(data_dir, resource_dir)
    frames, cache_updated_at, cache_path = _load_stock_frames(
        data_dir, resource_dir)
    stocks = []
    for code in member_codes:
        metrics = _frame_stock_metrics(frames.get(code), trade_date)
        meta = metadata.get(code) or {}
        meta_date = _normalise_trade_date(meta.get("data_date"))
        if not metrics and meta_date and meta_date <= trade_date:
            metrics = {
                "data_date": meta_date,
                "close": _number(meta.get("close")),
                "change_pct": _number(meta.get("change_pct")),
                "return_5d_pct": None,
                "rvol_20": None,
                "amount": None,
                "distance_from_high_20d_pct": None,
                "distance_from_low_20d_pct": None,
            }
        role, role_code = _stock_role(
            _number(metrics.get("change_pct")),
            _number(metrics.get("rvol_20")),
        )
        stocks.append({
            "code": code,
            "name": meta.get("name") or code,
            "mcap": _number(meta.get("mcap")),
            **metrics,
            "role": role,
            "role_code": role_code,
            "stale": bool(
                metrics.get("data_date")
                and metrics.get("data_date") != trade_date
            ),
        })

    total_amount = sum(_number(row.get("amount"), 0) or 0 for row in stocks)
    for row in stocks:
        amount = _number(row.get("amount"))
        share = (
            amount / total_amount
            if amount is not None and total_amount > 0
            else None
        )
        row["amount_share_pct"] = share * 100 if share is not None else None
        change = _number(row.get("change_pct"))
        row["turnover_impact_pct"] = (
            change * share if change is not None and share is not None else None
        )
    stocks.sort(
        key=lambda row: (_number(row.get("amount"), -1) or -1, row["code"]),
        reverse=True,
    )

    quoted = [row for row in stocks if _number(row.get("change_pct")) is not None]
    up_count = sum(1 for row in quoted if (_number(row.get("change_pct"), 0) or 0) > 0)
    down_count = sum(1 for row in quoted if (_number(row.get("change_pct"), 0) or 0) < 0)
    flat_count = len(quoted) - up_count - down_count
    return {
        "schema_version": 1,
        "scheme": scheme,
        "scheme_label": SCHEME_LABELS[scheme],
        "industry": industry,
        "trade_date": trade_date,
        "member_count": len(stocks),
        "quoted_count": len(quoted),
        "coverage_pct": round(len(quoted) / len(stocks) * 100, 1) if stocks else 0,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "total_amount": total_amount or None,
        "cache_updated_at": cache_updated_at,
        "source": "本地K线缓存" if cache_path else "市值快照降级",
        "stocks": stocks,
        "methodology": {
            "alignment": "所有价格和成交指标只使用雷达信号日及此前数据",
            "return_5d": "信号日收盘价相对5个交易日前收盘价",
            "rvol_20": "信号日成交量÷此前最多20个交易日平均成交量",
            "amount_share": "个股成交额÷板块已覆盖成分股成交额",
            "turnover_impact": (
                "当日涨跌幅×板块内成交额占比；仅解释交易影响，"
                "不是指数权重贡献"
            ),
            "classification": (
                "同花顺缺失时回退申万一级；申万三级缺失时依次回退二级、一级"
            ),
        },
    }


def _percentile(current: float | None, history: list[float], minimum: int = 20) -> float | None:
    if current is None:
        return None
    clean = [value for value in history if value is not None and math.isfinite(value)]
    if len(clean) < minimum:
        return None
    lower = sum(value < current for value in clean)
    equal = sum(value == current for value in clean)
    return round((lower + 0.5 * equal) / len(clean) * 100, 1)


def _rolling_sum(values: list[float | None], window: int) -> list[float | None]:
    output: list[float | None] = []
    for index in range(len(values)):
        chunk = values[max(0, index - window + 1): index + 1]
        output.append(sum(chunk) if len(chunk) == window and all(value is not None for value in chunk) else None)
    return output


def _breadth_map(payload: dict) -> dict[str, dict]:
    result = {}
    for row in payload.get("industries") or []:
        if row.get("is_total") or not row.get("industry"):
            continue
        total = int(_number(row.get("total"), 0) or 0)
        counts = [int(_number(value, 0) or 0) for value in row.get("daily_counts") or []]
        rates = [value / total if total > 0 else None for value in counts]
        current = rates[0] if rates else None
        previous = rates[1] if len(rates) > 1 else None
        current_pctile = _percentile(current, rates[1:])
        previous_pctile = _percentile(previous, rates[2:]) if previous is not None else None
        result[str(row["industry"])] = {
            "total": total,
            "current": current,
            "previous": previous,
            "pctile": current_pctile,
            "previous_pctile": previous_pctile,
            "change": current - previous if current is not None and previous is not None else None,
        }
    return result


def _latest_and_previous(row: dict) -> tuple[dict, dict]:
    series = [item for item in row.get("series") or [] if isinstance(item, dict)]
    current = dict(series[-1]) if series else {}
    current.update(row)
    previous = series[-2] if len(series) >= 2 else {}
    return current, previous


def _ret5_state(row: dict) -> tuple[float | None, float | None]:
    series = [item for item in row.get("series") or [] if isinstance(item, dict)]
    values = [_number(item.get("excess_return_pct")) for item in series]
    if series and str(series[-1].get("date")) == str(row.get("date")):
        values[-1] = _number(row.get("excess_return_pct"))
    elif row.get("date"):
        values.append(_number(row.get("excess_return_pct")))
    rolling = _rolling_sum(values, 5)
    current = rolling[-1] if rolling else None
    return current, _percentile(current, rolling[:-1])


def _is_capitulation(item: dict) -> bool:
    return (
        (_number(item.get("price_change_pct"), 0) or 0) < 0
        and (_number(item.get("activity_pctile"), 0) or 0) >= 80
        and (_number(item.get("breadth"), 0) or 0) <= -0.20
    )


def _is_broad_selloff(item: dict) -> bool:
    return (
        (_number(item.get("breadth"), 0) or 0) <= -0.35
        and (_number(item.get("direction_score"), 0) or 0) <= -0.20
    )


def _is_absorption(item: dict) -> bool:
    return (
        (_number(item.get("price_change_pct"), 0) or 0) < 0
        and (_number(item.get("activity_pctile"), 0) or 0) >= 70
        and (_number(item.get("efficiency_gap"), 0) or 0) <= -30
    )


def _is_euphoria(item: dict) -> bool:
    return (
        (_number(item.get("price_change_pct"), 0) or 0) > 0
        and (_number(item.get("activity_pctile"), 0) or 0) >= 80
        and (_number(item.get("breadth"), 0) or 0) >= 0.20
    )


def _is_distribution(item: dict) -> bool:
    return (
        (_number(item.get("price_change_pct"), 0) or 0) > 0
        and (_number(item.get("activity_pctile"), 0) or 0) >= 70
        and (_number(item.get("efficiency_gap"), 0) or 0) <= -30
    )


def _confidence(record: dict) -> dict:
    dates = int(record.get("independent_dates") or 0)
    if dates >= 120:
        return {"level": "stable", "label": "样本较充分"}
    if dates >= 60:
        return {"level": "research", "label": "研究样本"}
    return {"level": "insufficient", "label": "样本不足"}


def _condition_probability(
    scheme: str,
    side: str,
    matched_keys: list[str],
    members: int,
) -> dict:
    scheme_records = RESEARCH_CALIBRATION[scheme][side]
    base = scheme_records["base"]
    skipped = []
    selected_key = None
    selected = None
    for key in matched_keys:
        record = scheme_records.get(key)
        if not record:
            continue
        if record["independent_dates"] < 60:
            skipped.append({
                "key": key,
                "label": CONDITION_LABELS[key],
                "independent_dates": record["independent_dates"],
                "reason": "独立日期不足60，不显示该条件频率",
            })
            continue
        selected_key, selected = key, record
        break
    if selected is None:
        selected_key, selected = "base", base

    minimum_members = MIN_MEMBERS[scheme]
    available = members >= minimum_members
    probability_kind = "historical_frequency"
    probabilities = list(selected["probabilities"])
    strong_t5 = selected["strong_t5"]
    ci_t5 = list(selected["ci_t5"])
    shrink_weight = None
    if scheme == "sw3":
        parent = RESEARCH_CALIBRATION["sw"][side].get(selected_key) or RESEARCH_CALIBRATION["sw"][side]["base"]
        shrink_weight = members / (members + 20.0)
        probabilities = [
            round(shrink_weight * local + (1 - shrink_weight) * parent_value, 1)
            for local, parent_value in zip(probabilities, parent["probabilities"])
        ]
        strong_t5 = round(shrink_weight * strong_t5 + (1 - shrink_weight) * parent["strong_t5"], 1)
        ci_t5 = None
        probability_kind = "hierarchical_research_frequency"

    horizons = []
    for index, horizon in enumerate(HORIZONS):
        probability = probabilities[index] if available else None
        base_probability = base["probabilities"][index]
        horizons.append({
            "horizon": f"T{horizon}",
            "probability": probability,
            "base_probability": base_probability,
            "lift": round(probability / base_probability, 2) if probability is not None and base_probability else None,
        })
    return {
        "probability_available": available,
        "probability_kind": probability_kind if available else "unavailable",
        "condition_key": selected_key,
        "condition_label": CONDITION_LABELS[selected_key],
        "samples": selected["samples"],
        "independent_dates": selected["independent_dates"],
        "confidence": _confidence(selected),
        "horizons": horizons,
        "strong_t5": strong_t5 if available else None,
        "avg_excess_t5": selected["avg_excess_t5"] if available else None,
        "ci_t5": ci_t5 if available else None,
        "shrink_weight": round(shrink_weight, 3) if shrink_weight is not None else None,
        "skipped_research_conditions": skipped,
        "unavailable_reason": None if available else f"有效成分少于{minimum_members}只",
    }


def _signal_block(
    scheme: str,
    side: str,
    metrics: dict,
    flags: dict,
    members: int,
) -> dict:
    setup = flags[f"{side}_setup"]
    ret5_value = metrics.get("ret5_excess_pct")
    ret5_percentile = metrics.get("ret5_percentile")
    ret5_text = f"{ret5_value:+.2f}%" if ret5_value is not None else "—"
    percentile_text = f"P{ret5_percentile:.0f}" if ret5_percentile is not None else "历史不足"
    if side == "bottom":
        if not setup:
            stage, label = "neutral", "未进入底部候选"
        elif flags["triple_positive"]:
            stage, label = "confirmed", "反弹确认"
        elif flags["absorption_turn"]:
            stage, label = "testing", "修复试探"
        elif flags["absorption"] or flags["new_low_contraction"]:
            stage, label = "exhaustion", "卖压衰竭"
        else:
            stage, label = "falling", "接飞刀风险"
        matched = []
        if flags["absorption_turn"]:
            matched.append("absorption_turn")
        if flags["prior_panic"] and flags["triple_positive"]:
            matched.append("panic_confirm")
        if flags["triple_positive"] and flags["active_positive"]:
            matched.append("confirm_active")
        if flags["triple_positive"] and flags["not_concentrated"]:
            matched.append("confirm_not_concentrated")
        if flags["triple_positive"]:
            matched.append("triple_confirm")
        if flags["absorption"]:
            matched.append("absorption")
        if metrics.get("ret5_percentile") is not None and metrics["ret5_percentile"] <= 10:
            matched.append("extreme")
        matched.append("base")
        confirmation_count = sum((flags["price_positive"], flags["breadth_positive"], flags["direction_positive"]))
        evidence = [
            f"近5日超额 {ret5_text} · 自身{percentile_text}",
            f"价格延伸 {metrics.get('price_extension_pct', 0):+.1f}%",
        ]
        if flags["absorption"]:
            evidence.append("高成交努力但价格继续下跌的响应下降，出现吸收线索")
        if flags["new_low_contraction"]:
            evidence.append("20日新低覆盖率从极值开始收缩")
        if flags["triple_positive"]:
            evidence.append("价格、上涨广度、方向成交三项共同转正")
        if flags["active_positive"]:
            evidence.append("异常成交股票的方向广度转正")
        counter = []
        if not flags["price_positive"]:
            counter.append("价格尚未转正")
        if not flags["breadth_positive"]:
            counter.append("上涨家数广度不足15%")
        if not flags["direction_positive"]:
            counter.append("方向成交尚未转正")
        if not flags["not_concentrated"]:
            counter.append("成交贡献集中，反弹可能只由少数龙头驱动")
    else:
        if not setup:
            stage, label = "neutral", "未进入顶部候选"
        elif flags["euphoria_break"]:
            stage, label = "confirmed", "退潮确认"
        elif flags["distribution_break"] or flags["new_high_contraction"]:
            stage, label = "warning", "派发预警"
        elif flags["distribution"]:
            stage, label = "exhaustion", "上涨衰竭"
        else:
            stage, label = "euphoria", "亢奋观察"
        matched = []
        if flags["euphoria_break"]:
            matched.append("euphoria_break")
        if flags["distribution_break"]:
            matched.append("distribution_break")
        if flags["new_high_contraction"]:
            matched.append("new_high_contraction")
        if flags["new_high_extreme"]:
            matched.append("new_high_extreme")
        if flags["distribution"]:
            matched.append("distribution")
        if flags["euphoria"]:
            matched.append("euphoria")
        if (metrics.get("ret5_percentile") or 0) >= 90:
            matched.append("extreme")
        matched.append("base")
        confirmation_count = sum((flags["price_negative"], flags["breadth_negative"], flags["direction_negative"]))
        evidence = [
            f"近5日超额 {ret5_text} · 自身{percentile_text}",
            f"价格延伸 {metrics.get('price_extension_pct', 0):+.1f}%",
        ]
        if flags["new_high_extreme"]:
            evidence.append("20日新高覆盖率处于自身历史前20%")
        if flags["new_high_contraction"]:
            evidence.append("新高覆盖率从极值开始收缩")
        if flags["distribution"]:
            evidence.append("成交仍热但价格响应偏弱，出现派发线索")
        if flags["euphoria_break"]:
            evidence.append("此前亢奋后，价格、广度、方向成交共同转负")
        counter = []
        if not flags["price_negative"]:
            counter.append("价格尚未转负")
        if not flags["breadth_negative"]:
            counter.append("下跌家数广度尚未超过15%")
        if not flags["direction_negative"]:
            counter.append("方向成交尚未转负")
        if not flags["new_high_contraction"]:
            counter.append("新高覆盖尚未确认收缩")

    probability = _condition_probability(scheme, side, matched, members) if setup else {
        "probability_available": False,
        "probability_kind": "not_in_setup",
        "condition_key": None,
        "condition_label": "不适用",
        "samples": 0,
        "independent_dates": 0,
        "confidence": {"level": "none", "label": "未触发"},
        "horizons": [{"horizon": f"T{h}", "probability": None, "base_probability": None, "lift": None} for h in HORIZONS],
        "strong_t5": None,
        "avg_excess_t5": None,
        "ci_t5": None,
        "shrink_weight": None,
        "skipped_research_conditions": [],
        "unavailable_reason": "当前不满足情绪极端候选条件",
    }
    probability.update({
        "eligible": setup,
        "stage": stage,
        "stage_label": label,
        "confirmation_count": confirmation_count,
        "evidence": evidence,
        "counter_evidence": counter,
    })
    return probability


def _industry_snapshot(
    scheme: str,
    row: dict,
    high: dict,
    low: dict,
) -> dict:
    current, previous = _latest_and_previous(row)
    ret5, ret5_percentile = _ret5_state(row)
    price = _number(current.get("price_change_pct"), 0) or 0
    breadth = _number(current.get("breadth"), 0) or 0
    direction = _number(current.get("direction_score"), 0) or 0
    active_direction = _number(current.get("active_direction_breadth"), 0) or 0
    price_extension = _number(current.get("price_extension"), 0) or 0
    price_extension_pctile = _number(current.get("price_extension_pctile"), 50) or 50
    internal_top5_pctile = _number(current.get("internal_top5_pctile"), 50) or 50
    previous_breadth = _number(previous.get("breadth"), breadth) or 0
    previous_direction = _number(previous.get("direction_score"), direction) or 0
    breadth_change = breadth - previous_breadth
    direction_change = direction - previous_direction
    members = int(_number(current.get("eligible_stocks"), current.get("traded_stocks")) or 0)

    high_rate = _number(high.get("current"))
    low_rate = _number(low.get("current"))
    high_pctile = _number(high.get("pctile"))
    low_pctile = _number(low.get("pctile"))
    new_high_contraction = bool(
        high.get("previous_pctile") is not None
        and high["previous_pctile"] >= 80
        and high.get("change") is not None
        and high["change"] < 0
    )
    new_low_contraction = bool(
        low.get("previous_pctile") is not None
        and low["previous_pctile"] >= 80
        and low.get("change") is not None
        and low["change"] < 0
    )

    bottom_setup = bool(ret5 is not None and ret5 < 0 and ret5_percentile is not None and ret5_percentile <= 30 and price_extension < 0)
    top_setup = bool(ret5 is not None and ret5 > 0 and ret5_percentile is not None and ret5_percentile >= 70 and price_extension > 0)
    triple_positive = price > 0 and breadth >= 0.15 and direction >= 0.15
    triple_negative = price < 0 and breadth <= -0.15 and direction <= -0.15
    absorption = _is_absorption(current) or _is_absorption(previous)
    distribution = _is_distribution(current) or _is_distribution(previous)
    prior_panic = _is_capitulation(previous) or _is_broad_selloff(previous)
    prior_euphoria = _is_euphoria(previous)
    euphoria = _is_euphoria(current)
    absorption_turn = absorption and breadth_change >= 0.15 and direction_change >= 0.15
    distribution_break = distribution and breadth_change <= -0.15 and direction_change <= -0.15

    flags = {
        "bottom_setup": bottom_setup,
        "top_setup": top_setup,
        "price_positive": price > 0,
        "breadth_positive": breadth >= 0.15,
        "direction_positive": direction >= 0.15,
        "triple_positive": triple_positive,
        "price_negative": price < 0,
        "breadth_negative": breadth <= -0.15,
        "direction_negative": direction <= -0.15,
        "triple_negative": triple_negative,
        "active_positive": active_direction >= 0.05,
        "not_concentrated": internal_top5_pctile <= 70,
        "absorption": absorption,
        "absorption_turn": absorption_turn,
        "prior_panic": prior_panic,
        "new_low_contraction": new_low_contraction,
        "new_high_extreme": bool(high_pctile is not None and high_pctile >= 80),
        "new_high_contraction": new_high_contraction,
        "distribution": distribution,
        "distribution_break": distribution_break,
        "euphoria": euphoria,
        "euphoria_break": prior_euphoria and triple_negative,
    }
    position_components = []
    if ret5_percentile is not None:
        position_components.append(((ret5_percentile - 50) * 2, 0.50))
    position_components.extend([
        ((price_extension_pctile - 50) * 2, 0.25),
        (breadth * 100, 0.15),
        (direction * 100, 0.10),
    ])
    weight = sum(item[1] for item in position_components)
    sentiment_position = round(_clamp(sum(value * item_weight for value, item_weight in position_components) / max(weight, 0.01), -100, 100), 1)
    if sentiment_position <= -60:
        sentiment_label = "极度恐慌"
    elif sentiment_position <= -25:
        sentiment_label = "偏恐慌"
    elif sentiment_position >= 60:
        sentiment_label = "极度亢奋"
    elif sentiment_position >= 25:
        sentiment_label = "偏亢奋"
    else:
        sentiment_label = "中性"

    metrics = {
        "price_change_pct": round(price, 3),
        "breadth_pct": round(breadth * 100, 1),
        "direction_score_pct": round(direction * 100, 1),
        "active_direction_breadth_pct": round(active_direction * 100, 1),
        "activity_percentile": _number(current.get("activity_pctile")),
        "efficiency_gap": _number(current.get("efficiency_gap")),
        "internal_top5_percentile": internal_top5_pctile,
        "price_extension_pct": round(price_extension * 100, 2),
        "price_extension_percentile": price_extension_pctile,
        "ret5_excess_pct": round(ret5, 3) if ret5 is not None else None,
        "ret5_percentile": ret5_percentile,
        "breadth_change_pp": round(breadth_change * 100, 1),
        "direction_change_pp": round(direction_change * 100, 1),
        "new_high_rate_pct": round(high_rate * 100, 2) if high_rate is not None else None,
        "new_low_rate_pct": round(low_rate * 100, 2) if low_rate is not None else None,
        "new_high_percentile": high_pctile,
        "new_low_percentile": low_pctile,
    }
    bottom = _signal_block(scheme, "bottom", metrics, flags, members)
    top = _signal_block(scheme, "top", metrics, flags, members)

    danger_level, danger_label, danger_message = "normal", "正常", "暂未出现反转确认"
    if top["stage"] == "confirmed":
        danger_level, danger_label, danger_message = "danger", "退潮危险", "此前亢奋后出现价格、广度与方向成交共同破坏"
    elif top["stage"] in {"warning", "exhaustion"}:
        danger_level, danger_label, danger_message = "warning", "高位预警", "高位扩张能力下降，等待退潮确认"
    elif bottom["stage"] == "falling":
        danger_level, danger_label, danger_message = "warning", "接飞刀风险", "情绪虽弱，但卖压尚未衰竭或反向确认"
    elif bottom["stage"] == "confirmed":
        danger_level, danger_label, danger_message = "opportunity", "反弹确认", "底部候选已出现三重反向确认"

    primary_probability = max(
        [item.get("probability") or 0 for item in bottom["horizons"] + top["horizons"]]
    )
    stage_rank = {
        "confirmed": 5, "warning": 4, "exhaustion": 3,
        "testing": 3, "falling": 2, "euphoria": 2, "neutral": 0,
    }
    radar_rank = max(stage_rank.get(bottom["stage"], 0), stage_rank.get(top["stage"], 0)) * 100 + primary_probability
    return {
        "industry": str(row.get("industry") or "其他"),
        "members": members,
        "sentiment_position": sentiment_position,
        "sentiment_label": sentiment_label,
        "bottom": bottom,
        "top": top,
        "danger": {"level": danger_level, "label": danger_label, "message": danger_message},
        "metrics": metrics,
        "flags": flags,
        "radar_rank": round(radar_rank, 2),
    }


def build_sentiment_radar(data_dir: str, scheme: str = "sw") -> dict:
    """Build a lightweight current radar snapshot from persisted daily data."""
    if scheme not in SCHEME_SUFFIX:
        raise ValueError("scheme must be sw, ths or sw3")
    suffix = SCHEME_SUFFIX[scheme]
    flow_path = os.path.join(data_dir, f"capital_flow_v2{suffix}.json")
    flow = _read_json(flow_path)
    highs = _read_json(os.path.join(data_dir, f"new_highs_data_month{suffix}.json"), required=False)
    lows = _read_json(os.path.join(data_dir, f"new_lows_data_month{suffix}.json"), required=False)
    temperature = _read_json(os.path.join(data_dir, "market_temperature.json"), required=False)
    high_map = _breadth_map(highs)
    low_map = _breadth_map(lows)

    rows = []
    for row in flow.get("industries") or []:
        industry = str(row.get("industry") or "")
        if not industry or industry == "全市场合计":
            continue
        rows.append(_industry_snapshot(
            scheme,
            row,
            high_map.get(industry, {}),
            low_map.get(industry, {}),
        ))
    rows.sort(key=lambda item: (item["radar_rank"], abs(item["sentiment_position"])), reverse=True)

    warnings = [
        "当前展示的是历史条件频率，不是完整样本外校准后的交易概率",
        "板块收益使用成分股等权研究代理，不能直接等同可交易行业指数或ETF收益",
        "当前行业映射回溯历史，存在分类前视与存续样本偏差",
    ]
    classification = ((flow.get("data_quality") or {}).get("classification") or {})
    total = int(_number(classification.get("total"), 0) or 0)
    fallback = int(_number(classification.get("fallback"), 0) or 0)
    fallback_ratio = fallback / total if total else 0
    if scheme == "ths" and fallback_ratio >= 0.20:
        warnings.append(f"同花顺直接分类不足，{fallback_ratio:.1%}股票回退到申万一级")
    if scheme == "sw3":
        warnings.append("申万三级按有效成分数使用κ=20向申万一级同类条件收缩")
    if not high_map or not low_map:
        warnings.append("20日新高/新低历史缺失，尾部宽度证据不可用")

    bottom_counts: dict[str, int] = {}
    top_counts: dict[str, int] = {}
    danger_counts: dict[str, int] = {}
    for item in rows:
        bottom_counts[item["bottom"]["stage"]] = bottom_counts.get(item["bottom"]["stage"], 0) + 1
        top_counts[item["top"]["stage"]] = top_counts.get(item["top"]["stage"], 0) + 1
        danger_counts[item["danger"]["level"]] = danger_counts.get(item["danger"]["level"], 0) + 1

    temperature_rows = [row for row in temperature.get("rows") or [] if isinstance(row, dict)]
    temperature_latest = max(
        temperature_rows,
        key=lambda row: str(row.get("date") or ""),
        default={},
    )
    temperature_value = _number(temperature_latest.get("temperature"))
    if temperature_value is None:
        temperature_label = "环境未知"
    elif temperature_value >= 70:
        temperature_label = "高参与"
    elif temperature_value >= 55:
        temperature_label = "偏暖"
    elif temperature_value <= 30:
        temperature_label = "低参与"
    elif temperature_value <= 45:
        temperature_label = "偏冷"
    else:
        temperature_label = "中性"

    coverage = ((flow.get("data_quality") or {}).get("coverage") or {})
    return {
        "schema_version": 1,
        "model_version": "sentiment-radar-research-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": flow.get("trade_date") or flow.get("as_of"),
        "scheme": scheme,
        "scheme_label": SCHEME_LABELS[scheme],
        "quality": {
            "status": "research",
            "label": "研究频率",
            "coverage": coverage,
            "classification": classification,
            "history_days": ((flow.get("data_quality") or {}).get("history_days")),
            "breadth_history_days": len(highs.get("dates") or []),
            "warnings": warnings,
        },
        "market": {
            "industries": len(rows),
            "bottom_stages": bottom_counts,
            "top_stages": top_counts,
            "danger_levels": danger_counts,
            "temperature": temperature_value,
            "temperature_label": temperature_label,
            "temperature_date": temperature_latest.get("date"),
            "temperature_note": "市场参与强度只作环境背景，不直接改写行业反转频率",
        },
        "industries": rows,
        "methodology": {
            "signal_time": "信号日收盘",
            "entry": "下一交易日开盘",
            "horizons": ["T1", "T2", "T3", "T4", "T5"],
            "cost_bps": 20,
            "ordinary_reversal": "扣成本绝对收益与相对全市场收益方向均正确",
            "strong_reversal": "普通反转且横截面进入前/后30%",
            "sentiment_position": "近5日相对位置50%＋价格延伸25%＋涨跌广度15%＋方向成交10%",
            "probability_status": "2025-05-14至2026-08-04历史条件频率；尚非完整walk-forward校准概率",
            "causal_rule": "信号特征及分位只使用当日和此前数据",
        },
    }
