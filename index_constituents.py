"""Intraday index constituent monitor.

Constituent membership is cached for seven days. Quotes are cached briefly so
multiple browser refreshes do not repeatedly hit the upstream batch endpoint.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import akshare as ak
import pandas as pd
import requests

from kline_cache import KlineCache, fetch_spot, get_trade_dates, load_industry_map
from runtime_paths import data_path, resource_path


INDEXES = {
    "000016": {"name": "上证50", "expected_count": 50},
    "000903": {"name": "中证100", "expected_count": 100},
    "000300": {"name": "沪深300", "expected_count": 300},
    "000688": {"name": "科创50", "expected_count": 50},
    "000905": {"name": "中证500", "expected_count": 500},
    "000852": {"name": "中证1000", "expected_count": 1000},
    "932000": {"name": "中证2000", "expected_count": 2000},
    "899050": {"name": "北证50", "expected_count": 50},
}
INDEX_QUOTE_SYMBOLS = {
    "000016": "sh000016",
    "000903": "sh000903",
    "000300": "sh000300",
    "000688": "sh000688",
    "000905": "sh000905",
    "000852": "sh000852",
    "899050": "bj899050",
}
CONSTITUENT_TTL_SECONDS = 7 * 24 * 60 * 60
INDEX_WEIGHT_TTL_SECONDS = 24 * 60 * 60
QUOTE_TTL_SECONDS = 45
LIVE_QUOTE_MAX_AGE_SECONDS = 180
DELAYED_QUOTE_MAX_AGE_SECONDS = 15 * 60
MIN_OFFICIAL_WEIGHT_COVERAGE_PCT = 90.0
INTRADAY_HISTORY_MAX_SAMPLES = 180
INTRADAY_HISTORY_MAX_AGE_SECONDS = 30 * 60
INTRADAY_WINDOWS_MINUTES = (1, 3, 5, 15)
CONSTITUENT_CACHE_FILE = data_path("index_constituents_cache.json")
INDEX_WEIGHT_CACHE_FILE = data_path("index_weight_cache.json")
INDUSTRY_TAXONOMY_FILE = resource_path("industry_taxonomy.json")

CITIC_BY_SW = {
    "农林牧渔": "农林牧渔", "基础化工": "基础化工", "钢铁": "钢铁",
    "有色金属": "有色金属", "电子": "电子", "汽车": "汽车",
    "家用电器": "家电", "食品饮料": "食品饮料", "纺织服饰": "纺织服装",
    "轻工制造": "轻工制造", "医药生物": "医药", "公用事业": "电力及公用事业",
    "交通运输": "交通运输", "房地产": "房地产", "商贸零售": "商贸零售",
    "社会服务": "消费者服务", "银行": "银行", "非银金融": "非银行金融",
    "综合": "综合", "建筑材料": "建材", "建筑装饰": "建筑",
    "电力设备": "电力设备及新能源", "机械设备": "机械", "国防军工": "国防军工",
    "计算机": "计算机", "传媒": "传媒", "通信": "通信", "煤炭": "煤炭",
    "石油石化": "石油石化", "环保": "电力及公用事业", "美容护理": "基础化工",
}
CITIC_BY_THS = {
    "专用设备": "机械", "互联网电商": "商贸零售", "元件": "电子",
    "光伏设备": "电力设备及新能源", "光学光电子": "电子", "养殖业": "农林牧渔",
    "农产品加工": "农林牧渔", "农化制品": "基础化工", "包装印刷": "轻工制造",
    "化学制品": "基础化工", "化学制药": "医药", "化学原料": "基础化工",
    "化学纤维": "基础化工", "医疗器械": "医药", "医疗服务": "医药",
    "医药商业": "医药", "半导体": "电子", "厨卫电器": "家电",
    "多元金融": "非银行金融", "家居用品": "轻工制造", "小家电": "家电",
    "小金属": "有色金属", "工业金属": "有色金属", "工程机械": "机械",
    "建筑材料": "建材", "建筑装饰": "建筑", "影视院线": "传媒",
    "房地产": "房地产", "教育": "消费者服务", "服装家纺": "纺织服装",
    "汽车零部件": "汽车", "消费电子": "电子", "物流": "交通运输",
    "环境治理": "电力及公用事业", "生物制品": "医药", "电力": "电力及公用事业",
    "电池": "电力设备及新能源", "电网设备": "电力设备及新能源", "白色家电": "家电",
    "纺织制造": "纺织服装", "综合": "综合", "美容护理": "基础化工",
    "能源金属": "有色金属", "自动化设备": "机械", "计算机设备": "计算机",
    "贵金属": "有色金属", "软件开发": "计算机", "通信服务": "通信",
    "通信设备": "通信", "通用设备": "机械", "造纸": "轻工制造",
    "金属新材料": "有色金属", "钢铁": "钢铁", "银行": "银行",
    "风电设备": "电力设备及新能源", "黑色家电": "家电",
}

_quote_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_guard = threading.Lock()
_index_locks: dict[str, threading.Lock] = {}
_weight_cache_guard = threading.Lock()
_history_frames_guard = threading.Lock()
_history_frames_cache: dict[str, pd.DataFrame] | None = None
_history_frames_signature: tuple[str, int, int] | None = None
_intraday_history_guard = threading.Lock()
_intraday_history: dict[str, deque[dict[str, Any]]] = {}


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _round_optional(value: Any, digits: int = 2) -> float | None:
    number = _to_float(value)
    return round(number, digits) if number is not None else None


def _parse_tencent_line(line: str) -> dict[str, Any] | None:
    if '="' not in line:
        return None
    symbol = line.split("_", 2)[1].split("=", 1)[0]
    raw = line.split('="', 1)[1].rstrip('";')
    fields = raw.split("~")
    if len(fields) < 47:
        return None
    previous = _to_float(fields[4])
    price = _to_float(fields[3])
    open_price = _to_float(fields[5])
    volume_lots = _to_float(fields[6])
    high = _to_float(fields[33])
    low = _to_float(fields[34])
    amount_10k = _to_float(fields[37])
    amplitude = (
        (high - low) / previous * 100
        if high is not None and low is not None and previous
        else None
    )
    vwap = (
        amount_10k * 100 / volume_lots
        if amount_10k is not None and volume_lots is not None and volume_lots > 0
        else None
    )
    return {
        "symbol": symbol,
        "code": fields[2].zfill(6),
        "name": fields[1],
        "close": price,
        "prev_close": previous,
        "open": open_price,
        "high": high,
        "low": low,
        "volume_lots": volume_lots,
        "amount_10k": amount_10k,
        "vwap": vwap,
        "change_pct": _to_float(fields[32]),
        "amplitude_pct": amplitude,
        "turnover_rate": _to_float(fields[38]),
        "pe": _to_float(fields[39]),
        # Tencent fields 44/45 are circulating/total market cap, in 亿元.
        # Keep market_cap as the legacy circulating-cap field for UI compatibility.
        "market_cap": _to_float(fields[44]),
        "total_market_cap": _to_float(fields[45]),
        "pb": _to_float(fields[46]),
        "quote_time": fields[30],
    }


def _fetch_tencent_symbols(symbols: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(symbols), 700):
        batch = symbols[start:start + 700]
        try:
            response = requests.get(
                "https://qt.gtimg.cn/q=" + ",".join(batch),
                headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
                timeout=(4, 10),
            )
            response.encoding = "gbk"
            for line in response.text.splitlines():
                parsed = _parse_tencent_line(line)
                if parsed and parsed.get("symbol") in batch:
                    result[parsed["symbol"]] = parsed
        except Exception as exc:
            print(f"  腾讯行情批次失败: {exc}")
    return result


def _is_bse_stock(code: str) -> bool:
    return code.startswith(("4", "8", "92"))


def _stock_quote_symbol(code: str) -> str:
    if _is_bse_stock(code):
        return f"bj{code}"
    return f"{'sh' if code.startswith(('6', '9')) else 'sz'}{code}"


def _fetch_tencent_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    symbols = [_stock_quote_symbol(code) for code in codes]
    raw = _fetch_tencent_symbols(symbols)
    return {quote["code"]: quote for quote in raw.values()}


def _fetch_csindex_index_quote(index_code: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            "https://www.csindex.com.cn/csindex-home/perf/index-perf-oneday",
            params={"indexCode": index_code},
            headers={
                "Referer": f"https://www.csindex.com.cn/zh-CN/indices/index-detail/{index_code}",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=(4, 10),
        )
        response.raise_for_status()
        payload = response.json()
        header = ((payload.get("data") or {}).get("intraDayHeader") or {})
        if not header.get("current"):
            return None
        quote_time = f"{header.get('tradeDate', '').replace('-', '')}{header.get('tradeTime', '').replace(':', '')}"
        return {
            "code": index_code,
            "name": INDEXES[index_code]["name"],
            "price": _round_optional(header.get("current")),
            "change_pct": _round_optional(header.get("changePct")),
            "amplitude_pct": None,
            "quote_time": quote_time or None,
        }
    except Exception as exc:
        print(f"  中证指数行情失败 {index_code}: {exc}")
        return None


def _fetch_tencent_index_quote(index_code: str) -> dict[str, Any] | None:
    symbol = INDEX_QUOTE_SYMBOLS.get(index_code)
    if symbol:
        quote = _fetch_tencent_symbols([symbol]).get(symbol)
        if quote:
            return {
                "code": index_code,
                "name": INDEXES[index_code]["name"],
                "price": quote.get("close"),
                "prev_close": quote.get("prev_close"),
                "open": quote.get("open"),
                "change_pct": quote.get("change_pct"),
                "amplitude_pct": _round_optional(quote.get("amplitude_pct")),
                "quote_time": quote.get("quote_time"),
            }
    if index_code == "932000":
        return _fetch_csindex_index_quote(index_code)
    return None


def _fetch_tencent_index_daily_quote(index_code: str) -> dict[str, Any] | None:
    """Return the latest two completed daily bars for pre-open attribution."""
    symbol = INDEX_QUOTE_SYMBOLS.get(index_code)
    if not symbol:
        return None
    try:
        response = requests.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            params={
                "_var": "kline_dayqfq",
                "param": f"{symbol},day,,,3,qfq",
                "r": "0.1",
            },
            headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
            timeout=(4, 10),
        )
        response.raise_for_status()
        payload = json.loads(response.text.split("=", 1)[-1])
        stock_data = ((payload.get("data") or {}).get(symbol) or {})
        values = stock_data.get("qfqday") or stock_data.get("day") or []
        if len(values) < 2:
            return None
        previous_row, latest_row = values[-2], values[-1]
        previous_close = _to_float(previous_row[2])
        close = _to_float(latest_row[2])
        open_price = _to_float(latest_row[1])
        high = _to_float(latest_row[3])
        low = _to_float(latest_row[4])
        if close is None or previous_close is None or previous_close <= 0:
            return None
        return {
            "code": index_code,
            "name": INDEXES[index_code]["name"],
            "price": close,
            "prev_close": previous_close,
            "open": open_price,
            "change_pct": round((close / previous_close - 1) * 100, 4),
            "amplitude_pct": _round_optional(
                (high - low) / previous_close * 100
                if high is not None and low is not None else None
            ),
            "quote_time": str(latest_row[0]).replace("-", "") + "150000",
            "quote_source": "tencent_completed_daily",
        }
    except Exception as exc:
        print(f"  腾讯指数日线失败 {index_code}: {exc}")
        return None


def _atomic_json_dump(data: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _normalize_constituents(pairs: list[tuple[Any, Any]]) -> list[dict[str, str]]:
    rows = []
    seen = set()
    for code, name in pairs:
        code = str(code or "").split(".")[0].zfill(6)
        if len(code) != 6 or not code.isdigit() or code in seen:
            continue
        seen.add(code)
        rows.append({"code": code, "name": str(name or "")})
    return rows


def _fetch_csindex_constituents(index_code: str) -> list[dict[str, str]]:
    url = (
        "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
        f"file/autofile/cons/{index_code}cons.xls"
    )
    chunk_size = 64 * 1024
    parts = []
    total_size = None
    start = 0
    while total_size is None or start < total_size:
        response = requests.get(
            url,
            headers={"Range": f"bytes={start}-{start + chunk_size - 1}", "User-Agent": "Mozilla/5.0"},
            timeout=(5, 15),
        )
        response.raise_for_status()
        if response.status_code == 200:
            parts = [response.content]
            break
        content_range = response.headers.get("Content-Range", "")
        if not content_range.startswith(f"bytes {start}-") or "/" not in content_range:
            raise RuntimeError(f"指数 {index_code} 成分文件分段响应异常")
        total_size = int(content_range.rsplit("/", 1)[1])
        parts.append(response.content)
        start += len(response.content)
        if not response.content:
            raise RuntimeError(f"指数 {index_code} 成分文件下载中断")
    content = b"".join(parts)
    if total_size is not None and len(content) != total_size:
        raise RuntimeError(f"指数 {index_code} 成分文件大小不完整: {len(content)}/{total_size}")
    frame = pd.read_excel(BytesIO(content), dtype=str)
    if frame.shape[1] < 6:
        raise RuntimeError(f"指数 {index_code} 成分文件字段不完整")

    # The official workbook currently uses fixed positions, while its labels
    # have changed between releases.
    code_col = frame.columns[4]
    name_col = frame.columns[5]
    rows = _normalize_constituents(
        list(frame[[code_col, name_col]].itertuples(index=False, name=None))
    )
    if not rows:
        raise RuntimeError(f"指数 {index_code} 未获取到成分股")
    return rows


def _fetch_sina_constituents(index_code: str) -> list[dict[str, str]]:
    expected = INDEXES[index_code]["expected_count"]
    frame = ak.index_stock_cons(index_code)
    if "品种代码" not in frame or "品种名称" not in frame:
        raise RuntimeError(f"新浪指数 {index_code} 成分字段不完整")
    rows = _normalize_constituents(
        list(frame[["品种代码", "品种名称"]].itertuples(index=False, name=None))
    )
    if len(rows) < max(1, int(expected * 0.9)):
        raise RuntimeError(f"新浪指数 {index_code} 成分数量不足: {len(rows)}/{expected}")
    return rows


def _fetch_constituents(index_code: str) -> list[dict[str, str]]:
    if index_code == "899050":
        return _fetch_sina_constituents(index_code)
    try:
        return _fetch_csindex_constituents(index_code)
    except Exception as official_error:
        try:
            return _fetch_sina_constituents(index_code)
        except Exception as fallback_error:
            raise RuntimeError(
                f"中证源失败 ({official_error})；新浪源失败 ({fallback_error})"
            ) from fallback_error


def _fetch_csindex_closeweights(index_code: str) -> dict[str, Any]:
    """Fetch the latest official close-weight sheet for one index.

    The sheet is a latest-close snapshot, not a point-in-time history feed. The
    caller therefore records its stated weight date and may drift those weights
    forward with local adjusted closes.
    """
    url = (
        "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
        f"file/autofile/closeweight/{index_code}closeweight.xls"
    )
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 20))
    response.raise_for_status()
    frame = pd.read_excel(BytesIO(response.content), dtype=str)
    if frame.empty:
        raise RuntimeError(f"指数 {index_code} 官方权重文件为空")

    def column_containing(fragment: str, fallback_position: int | None = None):
        match = next((column for column in frame.columns if fragment in str(column)), None)
        if match is None and fallback_position is not None and frame.shape[1] > fallback_position:
            match = frame.columns[fallback_position]
        return match

    date_col = column_containing("日期", 0)
    code_col = column_containing("成份券代码", 4)
    weight_col = column_containing("权重", frame.shape[1] - 1)
    if date_col is None or code_col is None or weight_col is None:
        raise RuntimeError(f"指数 {index_code} 官方权重文件字段不完整")

    normalized = []
    for date_value, code_value, weight_value in frame[
        [date_col, code_col, weight_col]
    ].itertuples(index=False, name=None):
        code = str(code_value or "").split(".")[0].zfill(6)
        date_digits = "".join(character for character in str(date_value or "") if character.isdigit())[:8]
        weight = _to_float(weight_value)
        if len(code) == 6 and code.isdigit() and len(date_digits) == 8 and weight is not None and weight > 0:
            normalized.append((date_digits, code, weight))
    if not normalized:
        raise RuntimeError(f"指数 {index_code} 官方权重文件没有有效记录")

    weight_date = max(row[0] for row in normalized)
    weights = {
        code: float(weight)
        for date_value, code, weight in normalized
        if date_value == weight_date
    }
    expected = INDEXES[index_code]["expected_count"]
    total_weight = sum(weights.values())
    if len(weights) < max(1, int(expected * 0.9)) or not 90 <= total_weight <= 110:
        raise RuntimeError(
            f"指数 {index_code} 官方权重覆盖异常: {len(weights)}/{expected}, 合计 {total_weight:.2f}%"
        )
    return {
        "weight_date": weight_date,
        "weights": weights,
        "source_url": url,
    }


def _get_official_weights(index_code: str) -> dict[str, Any]:
    """Return a 24-hour cached official close-weight payload when available."""
    with _weight_cache_guard:
        cache = _load_json(INDEX_WEIGHT_CACHE_FILE, {"version": 1, "indexes": {}})
        indexes = cache.setdefault("indexes", {})
        saved = indexes.get(index_code) or {}
        fetched_at = saved.get("fetched_at")
        age = float("inf")
        if fetched_at:
            try:
                age = time.time() - datetime.fromisoformat(fetched_at).timestamp()
            except ValueError:
                pass
        if saved.get("weights") and age < INDEX_WEIGHT_TTL_SECONDS:
            return {
                **saved,
                "cache_state": "cached",
                "warning": None,
            }

        try:
            fetched = _fetch_csindex_closeweights(index_code)
            payload = {
                **fetched,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
            indexes[index_code] = payload
            _atomic_json_dump(cache, INDEX_WEIGHT_CACHE_FILE)
            return {
                **payload,
                "cache_state": "fresh",
                "warning": None,
            }
        except Exception as exc:
            if saved.get("weights"):
                return {
                    **saved,
                    "cache_state": "stale",
                    "warning": f"官方权重更新失败，继续使用 {fetched_at} 缓存: {exc}",
                }
            return {
                "weights": {},
                "weight_date": None,
                "fetched_at": None,
                "cache_state": "unavailable",
                "warning": f"官方权重不可用，已降级到自由流通代理: {exc}",
            }


def _get_constituents(index_code: str) -> tuple[list[dict[str, str]], str | None, str | None]:
    cache = _load_json(CONSTITUENT_CACHE_FILE, {"version": 1, "indexes": {}})
    indexes = cache.setdefault("indexes", {})
    saved = indexes.get(index_code) or {}
    rows = saved.get("stocks") or []
    fetched_at = saved.get("fetched_at")
    age = float("inf")
    if fetched_at:
        try:
            age = time.time() - datetime.fromisoformat(fetched_at).timestamp()
        except ValueError:
            pass

    warning = None
    if not rows or age >= CONSTITUENT_TTL_SECONDS:
        try:
            rows = _fetch_constituents(index_code)
            fetched_at = datetime.now().isoformat(timespec="seconds")
            indexes[index_code] = {"fetched_at": fetched_at, "stocks": rows}
            _atomic_json_dump(cache, CONSTITUENT_CACHE_FILE)
        except Exception as exc:
            if not rows:
                raise RuntimeError(f"成分股获取失败: {exc}") from exc
            warning = f"成分名单更新失败，继续使用 {fetched_at} 的缓存"
    return rows, fetched_at, warning


def _load_ths_industries() -> dict[str, str]:
    raw = _load_json(resource_path("industry_map_ths.json"), {})
    return {str(code).zfill(6): (industry or "其他") for code, industry in raw.items()}


def _load_industry_taxonomy() -> dict[str, dict[str, str]]:
    raw = _load_json(INDUSTRY_TAXONOMY_FILE, {})
    stocks = raw.get("stocks", {}) if isinstance(raw, dict) else {}
    return {
        str(code).zfill(6): details
        for code, details in stocks.items()
        if isinstance(details, dict)
    }


def _load_sw_detail_map(active_codes=None) -> dict[str, str]:
    """Return the most detailed local SW label available for each stock."""
    active_codes = active_codes or []
    sw_map = load_industry_map(active_codes)
    taxonomy = _load_industry_taxonomy()
    result = {}
    for code in active_codes:
        details = taxonomy.get(code, {})
        result[code] = (
            details.get("sw_level3")
            or details.get("sw_level2")
            or details.get("sw_level1")
            or sw_map.get(code)
            or "其他"
        )
    return result


def _citic_industry(ths_industry: str | None, sw_industry: str | None) -> str:
    return CITIC_BY_THS.get(ths_industry or "") or CITIC_BY_SW.get(sw_industry or "") or "其他"


def _frame_latest_date(frame: pd.DataFrame | None) -> str | None:
    if frame is None or frame.empty or "date" not in frame.columns:
        return None
    return pd.Timestamp(frame["date"].max()).strftime("%Y%m%d")


def _required_history_date() -> str | None:
    dates = get_trade_dates(2)
    if not dates:
        return None
    today = datetime.now().strftime("%Y%m%d")
    return dates[-2] if dates[-1] == today and len(dates) > 1 else dates[-1]


def _load_history_frames(codes: list[str]) -> dict[str, pd.DataFrame]:
    """Load the large shared K-line pickle once and reload only when it changes."""
    global _history_frames_cache, _history_frames_signature

    cache = KlineCache()
    cache_path = getattr(cache, "cache_file", None)
    signature = None
    if isinstance(cache_path, (str, os.PathLike)):
        try:
            stat = os.stat(cache_path)
            signature = (os.fspath(cache_path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = (os.fspath(cache_path), 0, 0)

    # Test doubles and non-file cache implementations should not share state.
    if signature is None:
        cache._load()
        data = (cache._cache or {}).get("data") or {}
        return {code: data[code] for code in codes if code in data}

    with _history_frames_guard:
        if _history_frames_cache is None or signature != _history_frames_signature:
            cache._load()
            _history_frames_cache = (cache._cache or {}).get("data") or {}
            _history_frames_signature = signature
        data = _history_frames_cache
        return {code: data[code] for code in codes if code in data}


def _parse_quote_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) >= 14:
        try:
            return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _quote_quality(
    quote_time: Any,
    now: datetime,
    *,
    fallback: bool = False,
) -> dict[str, Any]:
    if fallback:
        parsed = _parse_quote_datetime(quote_time)
        return {
            "quote_trade_date": parsed.strftime("%Y%m%d") if parsed else None,
            "age_seconds": (
                max(0, int((now - parsed).total_seconds())) if parsed else None
            ),
            "quote_state": "fallback",
            "is_live": False,
        }
    parsed = _parse_quote_datetime(quote_time)
    if parsed is None:
        return {
            "quote_trade_date": None,
            "age_seconds": None,
            "quote_state": "unknown",
            "is_live": False,
        }
    age = int((now - parsed).total_seconds())
    trade_date = parsed.strftime("%Y%m%d")
    current_minutes = now.hour * 60 + now.minute
    is_continuous_session = (
        now.weekday() < 5
        and (
            9 * 60 + 30 <= current_minutes <= 11 * 60 + 30
            or 13 * 60 <= current_minutes <= 15 * 60
        )
    )
    if age < -300:
        state = "invalid_future"
        is_live = False
    elif trade_date != now.strftime("%Y%m%d"):
        state = "stale"
        is_live = False
    elif not is_continuous_session:
        state = "outside_session"
        is_live = False
    elif age <= LIVE_QUOTE_MAX_AGE_SECONDS:
        state = "live"
        is_live = True
    elif age <= DELAYED_QUOTE_MAX_AGE_SECONDS:
        state = "delayed"
        is_live = False
    else:
        state = "stale"
        is_live = False
    return {
        "quote_trade_date": trade_date,
        "age_seconds": max(0, age),
        "quote_state": state,
        "is_live": is_live,
    }


def _recent_returns(
    frame: pd.DataFrame | None,
    current_price: float,
    minimum_date: str | None = None,
) -> dict[str, float | None]:
    result = {f"return_{days}d": None for days in (5, 10, 20, 60)}
    if frame is None or frame.empty or "close" not in frame.columns or not current_price:
        return result
    if minimum_date and (_frame_latest_date(frame) or "") < minimum_date:
        return result
    closes = [float(value) for value in frame.sort_values("date")["close"].dropna().tolist() if value]
    if not closes:
        return result

    # If the latest cached close is already the displayed quote, that close is
    # part of the return window. Otherwise the live quote is one session newer.
    includes_current = abs(closes[-1] / current_price - 1) < 0.0001
    for days in (5, 10, 20, 60):
        offset = days + 1 if includes_current else days
        if len(closes) < offset:
            continue
        base = closes[-offset]
        if base > 0:
            result[f"return_{days}d"] = round((current_price / base - 1) * 100, 2)
    return result


def _fallback_quote(frame: pd.DataFrame | None, name: str) -> dict[str, Any] | None:
    if frame is None or frame.empty:
        return None
    ordered = frame.sort_values("date")
    latest = ordered.iloc[-1]
    close = float(latest["close"])
    previous = float(ordered.iloc[-2]["close"]) if len(ordered) > 1 else close
    high = float(latest.get("high", close) or close)
    low = float(latest.get("low", close) or close)
    return {
        "name": name,
        "close": close,
        "prev_close": previous,
        "open": float(latest.get("open", close) or close),
        "high": high,
        "low": low,
        "change_pct": round((close / previous - 1) * 100, 2) if previous else 0.0,
        "turnover_rate": None,
        "pe": None,
        "pb": None,
        "market_cap": None,
        "total_market_cap": None,
        "quote_time": pd.Timestamp(latest["date"]).strftime("%Y%m%d") + "150000",
    }


def _prefer_completed_preopen_quote(
    quote: dict[str, Any] | None,
    frame: pd.DataFrame | None,
    name: str,
    now: datetime,
) -> tuple[dict[str, Any] | None, bool]:
    """Ignore today's zeroed/indicative quote before continuous auction."""
    current_minutes = now.hour * 60 + now.minute
    if (
        quote is None
        or now.weekday() >= 5
        or current_minutes >= 9 * 60 + 30
    ):
        return quote, False
    parsed = _parse_quote_datetime(quote.get("quote_time"))
    if parsed is None or parsed.date() != now.date():
        return quote, False
    completed = _fallback_quote(frame, name)
    return (completed, True) if completed is not None else (quote, False)


def _free_float_tier_pct(circulating_ratio: float | None) -> float | None:
    """CSI-style tier proxy from circulating/total market-cap ratio.

    This is deliberately named a proxy: circulating market cap is not identical
    to the index provider's investable free-float share count.
    """
    if circulating_ratio is None or not 0 < circulating_ratio <= 1.000001:
        return None
    ratio_pct = min(circulating_ratio * 100, 100)
    if ratio_pct <= 15:
        return float(math.ceil(ratio_pct))
    if ratio_pct <= 20:
        return 20.0
    if ratio_pct <= 30:
        return 30.0
    if ratio_pct <= 40:
        return 40.0
    if ratio_pct <= 50:
        return 50.0
    if ratio_pct <= 60:
        return 60.0
    if ratio_pct <= 70:
        return 70.0
    if ratio_pct <= 80:
        return 80.0
    return 100.0


def _close_on_or_before(frame: pd.DataFrame | None, date_value: str | None) -> float | None:
    if frame is None or frame.empty or not date_value or "date" not in frame or "close" not in frame:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce")
    eligible = frame.loc[dates <= pd.Timestamp(date_value)]
    if eligible.empty:
        return None
    return _to_float(eligible.sort_values("date").iloc[-1]["close"])


def _prepare_proxy_cap_fields(stocks: list[dict[str, Any]]) -> None:
    for stock in stocks:
        circulating_cap = _to_float(stock.get("market_cap"))
        total_cap = _to_float(stock.get("total_market_cap"))
        price = _to_float(stock.get("price"))
        previous = _to_float(stock.get("prev_close"))
        ratio = (
            circulating_cap / total_cap
            if circulating_cap is not None and total_cap is not None and total_cap > 0
            else None
        )
        if ratio is not None and not 0 < ratio <= 1.000001:
            ratio = None
        tier_pct = _free_float_tier_pct(ratio)
        previous_total_cap = (
            total_cap * previous / price
            if total_cap is not None and price and previous and price > 0
            else total_cap
        )
        adjusted_cap = (
            previous_total_cap * tier_pct / 100
            if previous_total_cap is not None and tier_pct is not None
            else None
        )
        stock["free_float_ratio_pct"] = _round_optional(
            ratio * 100 if ratio is not None else None, 3
        )
        stock["free_float_tier_pct"] = _round_optional(tier_pct, 3)
        stock["free_float_tier_proxy_cap_prev_close"] = _round_optional(adjusted_cap, 4)


def _normalize_weight_values(raw_values: dict[str, float]) -> dict[str, float]:
    total = sum(value for value in raw_values.values() if value > 0)
    if total <= 0:
        return {}
    return {code: value / total * 100 for code, value in raw_values.items() if value > 0}


def _assign_index_weights(
    stocks: list[dict[str, Any]],
    history: dict[str, pd.DataFrame],
    official: dict[str, Any],
) -> dict[str, Any]:
    """Assign previous-close and current proxy weights with transparent lineage."""
    _prepare_proxy_cap_fields(stocks)
    by_code = {stock["code"]: stock for stock in stocks}
    official_weights = {
        str(code).zfill(6): float(value)
        for code, value in (official.get("weights") or {}).items()
        if _to_float(value) is not None and float(value) > 0
    }
    official_total = sum(official_weights.values())
    matched_official = {
        code: weight for code, weight in official_weights.items() if code in by_code
    }
    matched_weight_pct = (
        sum(matched_official.values()) / official_total * 100 if official_total else 0.0
    )

    previous_weights: dict[str, float] = {}
    drifted_codes: set[str] = set()
    drifted_official_weight = 0.0
    weight_date = official.get("weight_date")
    source = "free_float_tier_proxy"
    is_official = False

    if matched_official and matched_weight_pct >= MIN_OFFICIAL_WEIGHT_COVERAGE_PCT:
        raw = {}
        for code, base_weight in matched_official.items():
            stock = by_code[code]
            base_close = _close_on_or_before(history.get(code), weight_date)
            previous = _to_float(stock.get("prev_close"))
            if base_close and previous and base_close > 0:
                raw[code] = base_weight * previous / base_close
                drifted_codes.add(code)
                drifted_official_weight += base_weight
            else:
                raw[code] = base_weight
        previous_weights = _normalize_weight_values(raw)
        source = "csindex_closeweight_drifted"
        is_official = True
    else:
        proxy_raw = {
            stock["code"]: float(stock["free_float_tier_proxy_cap_prev_close"])
            for stock in stocks
            if _to_float(stock.get("free_float_tier_proxy_cap_prev_close")) is not None
            and float(stock["free_float_tier_proxy_cap_prev_close"]) > 0
        }
        previous_weights = _normalize_weight_values(proxy_raw)

    current_raw = {}
    for stock in stocks:
        code = stock["code"]
        weight = previous_weights.get(code)
        price = _to_float(stock.get("price"))
        previous = _to_float(stock.get("prev_close"))
        if weight is not None:
            current_raw[code] = weight * (price / previous if price and previous and previous > 0 else 1.0)
    current_weights = _normalize_weight_values(current_raw)

    for stock in stocks:
        code = stock["code"]
        previous_weight = previous_weights.get(code)
        stock["previous_close_weight_pct"] = _round_optional(previous_weight, 6)
        stock["weight_pct"] = _round_optional(previous_weight, 6)
        stock["dynamic_weight_pct"] = _round_optional(current_weights.get(code), 6)
        if previous_weight is None:
            stock["weight_source"] = "unavailable"
        elif is_official:
            stock["weight_source"] = (
                "csindex_closeweight_drifted"
                if code in drifted_codes else "csindex_closeweight"
            )
        else:
            stock["weight_source"] = "free_float_tier_proxy"

    proxy_count = sum(
        1 for stock in stocks if stock.get("free_float_tier_proxy_cap_prev_close") is not None
    )
    drift_coverage = (
        drifted_official_weight / official_total * 100 if official_total else 0.0
    )
    return {
        "weight_source": source,
        "is_official_source": is_official,
        "weight_date": weight_date if is_official else None,
        "weight_file_fetched_at": official.get("fetched_at"),
        "weight_cache_state": official.get("cache_state"),
        "official_constituent_match_weight_pct": round(matched_weight_pct, 3),
        "drift_coverage_pct": round(drift_coverage, 3),
        "weighted_constituents": len(previous_weights),
        "stock_coverage_pct": round(len(previous_weights) / max(len(stocks), 1) * 100, 3),
        "proxy_input_coverage_pct": round(proxy_count / max(len(stocks), 1) * 100, 3),
        "normalized_weight_sum_pct": round(sum(previous_weights.values()), 6),
        "method": (
            "中证指数最近收盘官方权重，以权重日复权收盘至前收盘的价格比漂移后归一化"
            if is_official else
            "总市值×自由流通比例分级靠档系数，以前收盘市值归一化的代理权重"
        ),
        "disclaimer": (
            "官方收盘权重经价格漂移后的盘中近似，未显式重放公司行动、调样和权重上限变化"
            if is_official else
            "free_float_tier_proxy 不是指数公司官方权重；流通市值不等于可投资自由流通股本"
        ),
        "tier_rules": {
            "le_15pct": "向上取整至1个百分点",
            "15_to_80pct": "向上靠档至20/30/40/50/60/70/80%",
            "gt_80pct": "100%",
        },
    }


def _fetch_bse_histories(codes: list[str], max_workers: int = 12) -> dict[str, pd.DataFrame]:
    start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    def fetch(code: str) -> tuple[str, pd.DataFrame | None]:
        symbol = _stock_quote_symbol(code)
        try:
            response = requests.get(
                "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                params={
                    "_var": "kline_dayqfq",
                    "param": f"{symbol},day,{start},{end},140,qfq",
                    "r": "0.1",
                },
                headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
                timeout=(4, 12),
            )
            response.raise_for_status()
            raw = response.text.split("=", 1)[-1]
            payload = json.loads(raw)
            stock_data = ((payload.get("data") or {}).get(symbol) or {})
            values = stock_data.get("qfqday") or stock_data.get("day") or []
            rows = []
            for value in values:
                if len(value) < 6:
                    continue
                rows.append({
                    "date": pd.Timestamp(value[0]),
                    "open": float(value[1]),
                    "close": float(value[2]),
                    "high": float(value[3]),
                    "low": float(value[4]),
                    "volume": float(value[5]),
                })
            if not rows:
                return code, None
            return code, pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        except Exception:
            return code, None

    result: dict[str, pd.DataFrame] = {}
    if not codes:
        return result
    with ThreadPoolExecutor(max_workers=min(max_workers, len(codes))) as pool:
        futures = [pool.submit(fetch, code) for code in codes]
        for future in as_completed(futures):
            code, frame = future.result()
            if frame is not None and not frame.empty:
                result[code] = frame
    return result


def _index_previous_close(index_quote: dict[str, Any] | None) -> float | None:
    if not index_quote:
        return None
    price = _to_float(index_quote.get("price"))
    previous = _to_float(index_quote.get("prev_close"))
    change_pct = _to_float(index_quote.get("change_pct"))
    if price and previous and previous > 0:
        calculated_change = (price / previous - 1) * 100
        if change_pct is None or abs(calculated_change - change_pct) <= 0.05:
            return previous
    denominator = 1 + change_pct / 100 if change_pct is not None else None
    return price / denominator if price and denominator and denominator > 0 else None


def _reconciled_return(
    price: float,
    previous: float | None,
    quoted_change_pct: float | None,
) -> tuple[float | None, float | None]:
    """Reconcile pre-open vendor fields that mix yesterday's quote and today's base.

    Some free quote endpoints advance ``prev_close`` before their displayed
    quote timestamp advances. When the price/previous return conflicts with the
    endpoint's stated return, the timestamp-aligned stated return is used and
    the matching previous close is derived.
    """
    calculated = (
        (price / previous - 1) * 100 if previous is not None and previous > 0 else None
    )
    if quoted_change_pct is None:
        return calculated, previous
    if calculated is None or abs(calculated - quoted_change_pct) > 0.05:
        denominator = 1 + quoted_change_pct / 100
        reconciled_previous = price / denominator if denominator > 0 else previous
        return quoted_change_pct, reconciled_previous
    return calculated, previous


def _weighted_condition(
    stocks: list[dict[str, Any]],
    eligibility,
    condition,
) -> tuple[float | None, float]:
    eligible = [
        stock for stock in stocks
        if _to_float(stock.get("weight_pct")) is not None and eligibility(stock)
    ]
    denominator = sum(float(stock["weight_pct"]) for stock in eligible)
    if denominator <= 0:
        return None, 0.0
    numerator = sum(float(stock["weight_pct"]) for stock in eligible if condition(stock))
    return numerator / denominator * 100, denominator


def _industry_contributions(
    stocks: list[dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for stock in stocks:
        industry = stock.get(field) or "其他"
        group = groups.setdefault(industry, {
            "industry": industry,
            "stocks": 0,
            "weighted_stocks": 0,
            "weight_pct": 0.0,
            "dynamic_weight_pct": 0.0,
            "contribution_bp": 0.0,
            "contribution_points": 0.0,
            "live_weight_pct": 0.0,
        })
        group["stocks"] += 1
        weight = _to_float(stock.get("weight_pct"))
        dynamic_weight = _to_float(stock.get("dynamic_weight_pct"))
        contribution_bp = _to_float(stock.get("contribution_bp"))
        contribution_points = _to_float(stock.get("contribution_points"))
        if weight is not None:
            group["weighted_stocks"] += 1
            group["weight_pct"] += weight
            if stock.get("is_live"):
                group["live_weight_pct"] += weight
        if dynamic_weight is not None:
            group["dynamic_weight_pct"] += dynamic_weight
        if contribution_bp is not None:
            group["contribution_bp"] += contribution_bp
        if contribution_points is not None:
            group["contribution_points"] += contribution_points
    rows = []
    for group in groups.values():
        for key in (
            "weight_pct", "dynamic_weight_pct", "contribution_bp",
            "contribution_points", "live_weight_pct",
        ):
            group[key] = round(group[key], 4)
        rows.append(group)
    return sorted(rows, key=lambda row: (-abs(row["contribution_bp"]), row["industry"]))


def _compute_index_metrics(
    stocks: list[dict[str, Any]],
    index_quote: dict[str, Any] | None,
    weight_model: dict[str, Any],
) -> dict[str, Any]:
    index_previous = _index_previous_close(index_quote)
    for stock in stocks:
        weight = _to_float(stock.get("weight_pct"))
        stock_return = _to_float(stock.get("_return_pct"))
        contribution_bp = (
            weight * stock_return if weight is not None and stock_return is not None else None
        )
        stock["contribution_bp"] = _round_optional(contribution_bp, 4)
        stock["contribution_points"] = _round_optional(
            index_previous * contribution_bp / 10000
            if index_previous is not None and contribution_bp is not None else None,
            4,
        )

    contributions = [
        (stock, float(stock["contribution_bp"]))
        for stock in stocks if _to_float(stock.get("contribution_bp")) is not None
    ]
    replicated_return_pct = sum(value for _, value in contributions) / 100
    actual_return_pct = _to_float((index_quote or {}).get("change_pct"))
    residual_bp = (
        (actual_return_pct - replicated_return_pct) * 100
        if actual_return_pct is not None and contributions else None
    )
    contribution_weight_pct = sum(
        float(stock["weight_pct"]) for stock, _ in contributions
    )
    quote_weight_pct = sum(
        float(stock["weight_pct"]) for stock in stocks
        if _to_float(stock.get("weight_pct")) is not None
        and stock.get("quote_state") != "fallback"
    )
    live_weight_pct = sum(
        float(stock["weight_pct"]) for stock in stocks
        if _to_float(stock.get("weight_pct")) is not None and stock.get("is_live")
    )

    weighted_advance, return_coverage = _weighted_condition(
        stocks,
        lambda stock: _to_float(stock.get("_return_pct")) is not None,
        lambda stock: float(stock["_return_pct"]) > 0.005,
    )
    weighted_decline, _ = _weighted_condition(
        stocks,
        lambda stock: _to_float(stock.get("_return_pct")) is not None,
        lambda stock: float(stock["_return_pct"]) < -0.005,
    )
    weighted_above_open, open_coverage = _weighted_condition(
        stocks,
        lambda stock: _to_float(stock.get("open")) not in (None, 0)
        and _to_float(stock.get("price")) is not None,
        lambda stock: float(stock["price"]) > float(stock["open"]),
    )
    weighted_above_vwap, vwap_coverage = _weighted_condition(
        stocks,
        lambda stock: _to_float(stock.get("vwap")) not in (None, 0)
        and _to_float(stock.get("price")) is not None,
        lambda stock: float(stock["price"]) > float(stock["vwap"]),
    )
    valid_returns = [
        float(stock["_return_pct"])
        for stock in stocks if _to_float(stock.get("_return_pct")) is not None
    ]
    equal_advance = (
        sum(value > 0.005 for value in valid_returns) / len(valid_returns) * 100
        if valid_returns else None
    )

    absolute = sorted(
        ((stock, abs(value)) for stock, value in contributions if abs(value) > 0),
        key=lambda item: item[1],
        reverse=True,
    )
    absolute_total = sum(value for _, value in absolute)
    shares = [value / absolute_total for _, value in absolute] if absolute_total else []
    hhi = sum(value * value for value in shares) if shares else None

    def top_share(limit: int) -> float | None:
        return sum(value for _, value in absolute[:limit]) / absolute_total * 100 if absolute_total else None

    def driver(stock: dict[str, Any], contribution: float) -> dict[str, Any]:
        return {
            "code": stock["code"],
            "name": stock["name"],
            "industry": stock.get("industry"),
            "weight_pct": stock.get("weight_pct"),
            "change_pct": stock.get("change_pct"),
            "contribution_bp": round(contribution, 4),
            "contribution_points": stock.get("contribution_points"),
        }

    positive = sorted(
        ((stock, value) for stock, value in contributions if value > 0),
        key=lambda item: item[1],
        reverse=True,
    )
    negative = sorted(
        ((stock, value) for stock, value in contributions if value < 0),
        key=lambda item: item[1],
    )

    residual_abs = abs(residual_bp) if residual_bp is not None else None
    if residual_abs is None or contribution_weight_pct < 90:
        confidence = "unavailable"
    elif residual_abs <= 3 and contribution_weight_pct >= 98:
        confidence = "high"
    elif residual_abs <= 8 and contribution_weight_pct >= 95:
        confidence = "medium"
    else:
        confidence = "low"
    if not weight_model.get("is_official_source") and confidence == "high":
        confidence = "medium"
    index_quote_live = bool((index_quote or {}).get("is_live"))
    signal_ready = (
        confidence in {"high", "medium"}
        and live_weight_pct >= 95
        and index_quote_live
        and residual_abs is not None
        and residual_abs <= 8
    )

    replication = {
        "replicated_return_pct": round(replicated_return_pct, 4) if contributions else None,
        "official_index_return_pct": _round_optional(actual_return_pct, 4),
        "replication_residual_bp": _round_optional(residual_bp, 3),
        "index_prev_close": _round_optional(index_previous, 4),
        "contribution_sum_bp": round(sum(value for _, value in contributions), 4),
        "contribution_sum_points": _round_optional(
            sum(
                float(stock["contribution_points"])
                for stock, _ in contributions
                if _to_float(stock.get("contribution_points")) is not None
            ),
            4,
        ),
        "contribution_weight_coverage_pct": round(contribution_weight_pct, 3),
        "quote_weight_coverage_pct": round(quote_weight_pct, 3),
        "effective_live_weight_pct": round(live_weight_pct, 3),
        "confidence": confidence,
        "signal_ready": signal_ready,
        "quality_gate": {
            "max_residual_bp": 8.0,
            "min_contribution_weight_pct": 95.0,
            "min_effective_live_weight_pct": 95.0,
            "index_quote_live": index_quote_live,
            "passed": signal_ready,
        },
    }
    breadth = {
        "weighted_advance_pct": _round_optional(weighted_advance, 3),
        "weighted_decline_pct": _round_optional(weighted_decline, 3),
        "weighted_flat_pct": _round_optional(
            100 - weighted_advance - weighted_decline
            if weighted_advance is not None and weighted_decline is not None else None,
            3,
        ),
        "equal_weight_advance_pct": _round_optional(equal_advance, 3),
        "weighted_above_open_pct": _round_optional(weighted_above_open, 3),
        "weighted_above_vwap_pct": _round_optional(weighted_above_vwap, 3),
        "return_weight_coverage_pct": round(return_coverage, 3),
        "open_weight_coverage_pct": round(open_coverage, 3),
        "vwap_weight_coverage_pct": round(vwap_coverage, 3),
    }
    concentration = {
        "top5_abs_contribution_share_pct": _round_optional(top_share(5), 3),
        "top10_abs_contribution_share_pct": _round_optional(top_share(10), 3),
        "absolute_contribution_hhi": _round_optional(hhi, 6),
        "effective_driver_count": _round_optional(1 / hhi if hhi else None, 2),
        "positive_contribution_bp": round(sum(value for _, value in positive), 4),
        "negative_contribution_bp": round(sum(value for _, value in negative), 4),
        "top_positive": [driver(stock, value) for stock, value in positive[:10]],
        "top_negative": [driver(stock, value) for stock, value in negative[:10]],
    }
    industries = {
        "ths": _industry_contributions(stocks, "industry_ths"),
        "sw": _industry_contributions(stocks, "industry_sw"),
        "sw3": _industry_contributions(stocks, "industry_sw_detail"),
        "citic": _industry_contributions(stocks, "industry_citic"),
    }
    return {
        "replication": replication,
        "breadth": breadth,
        "driver_concentration": concentration,
        "industry_contributions": industries,
    }


def _history_reference(
    samples: list[dict[str, Any]],
    target_timestamp: float,
    window_seconds: int,
) -> dict[str, Any] | None:
    if not samples:
        return None
    candidate = min(samples, key=lambda sample: abs(sample["timestamp"] - target_timestamp))
    tolerance = max(45, window_seconds * 0.4)
    return candidate if abs(candidate["timestamp"] - target_timestamp) <= tolerance else None


def _attach_intraday_history(
    index_code: str,
    stocks: list[dict[str, Any]],
    metrics: dict[str, Any],
    now: datetime,
    *,
    record: bool,
) -> dict[str, Any]:
    timestamp = now.timestamp()
    trade_date = now.strftime("%Y%m%d")
    current_summary = {
        "replicated_return_pct": metrics["replication"].get("replicated_return_pct"),
        "official_index_return_pct": metrics["replication"].get("official_index_return_pct"),
        "replication_residual_bp": metrics["replication"].get("replication_residual_bp"),
        "weighted_advance_pct": metrics["breadth"].get("weighted_advance_pct"),
        "effective_live_weight_pct": metrics["replication"].get("effective_live_weight_pct"),
    }
    current_stock_values = {
        stock["code"]: (
            _to_float(stock.get("price")),
            _to_float(stock.get("contribution_bp")),
        )
        for stock in stocks
        if _to_float(stock.get("price")) is not None
    }

    for stock in stocks:
        for minutes in INTRADAY_WINDOWS_MINUTES:
            stock[f"return_{minutes}m_pct"] = None
            stock[f"contribution_change_{minutes}m_bp"] = None
            stock[f"contribution_delta_{minutes}m_bp"] = None

    with _intraday_history_guard:
        history = _intraday_history.setdefault(
            index_code, deque(maxlen=INTRADAY_HISTORY_MAX_SAMPLES)
        )
        while history and (
            history[0]["trade_date"] != trade_date
            or timestamp - history[0]["timestamp"] > INTRADAY_HISTORY_MAX_AGE_SECONDS
        ):
            history.popleft()
        samples = list(history)
        horizons = {}
        stocks_by_code = {stock["code"]: stock for stock in stocks}
        for minutes in INTRADAY_WINDOWS_MINUTES:
            seconds = minutes * 60
            reference = _history_reference(samples, timestamp - seconds, seconds)
            label = f"{minutes}m"
            if reference is None:
                horizons[label] = {
                    "observed_seconds": None,
                    "contribution_change_bp": None,
                    "replicated_return_change_bp": None,
                    "official_return_change_bp": None,
                    "replication_residual_change_bp": None,
                    "weighted_advance_change_pct": None,
                    "matched_stocks": 0,
                }
                continue
            observed = timestamp - reference["timestamp"]
            matched = 0
            for code, stock in stocks_by_code.items():
                previous_values = reference["stocks"].get(code)
                if previous_values is None:
                    continue
                previous_price, previous_contribution = previous_values
                price = _to_float(stock.get("price"))
                contribution = _to_float(stock.get("contribution_bp"))
                if price is not None and previous_price and previous_price > 0:
                    stock[f"return_{minutes}m_pct"] = round(
                        (price / previous_price - 1) * 100, 4
                    )
                if contribution is not None and previous_contribution is not None:
                    contribution_change = round(
                        contribution - previous_contribution, 4
                    )
                    stock[f"contribution_change_{minutes}m_bp"] = contribution_change
                    stock[f"contribution_delta_{minutes}m_bp"] = contribution_change
                matched += 1

            previous_summary = reference["summary"]

            def delta_bp(key: str, *, already_bp: bool = False):
                current = _to_float(current_summary.get(key))
                previous = _to_float(previous_summary.get(key))
                if current is None or previous is None:
                    return None
                return round(current - previous if already_bp else (current - previous) * 100, 4)

            current_advance = _to_float(current_summary.get("weighted_advance_pct"))
            previous_advance = _to_float(previous_summary.get("weighted_advance_pct"))
            horizons[label] = {
                "observed_seconds": round(observed, 1),
                "contribution_change_bp": delta_bp("replicated_return_pct"),
                "replicated_return_change_bp": delta_bp("replicated_return_pct"),
                "official_return_change_bp": delta_bp("official_index_return_pct"),
                "replication_residual_change_bp": delta_bp(
                    "replication_residual_bp", already_bp=True
                ),
                "weighted_advance_change_pct": _round_optional(
                    current_advance - previous_advance
                    if current_advance is not None and previous_advance is not None else None,
                    4,
                ),
                "matched_stocks": matched,
            }

        if record:
            sample = {
                "timestamp": timestamp,
                "trade_date": trade_date,
                "updated_at": now.isoformat(timespec="seconds"),
                "stocks": current_stock_values,
                "summary": current_summary,
            }
            if history and abs(history[-1]["timestamp"] - timestamp) < 1:
                history[-1] = sample
            else:
                history.append(sample)

        return {
            "storage": "memory",
            "trade_date": trade_date,
            "sample_count": len(history),
            "oldest_at": history[0]["updated_at"] if history else None,
            "latest_at": history[-1]["updated_at"] if history else None,
            "recorded_current": record,
            "windows": horizons,
            "series": [
                {
                    "updated_at": sample["updated_at"],
                    **sample["summary"],
                }
                for sample in history
            ],
        }


def build_index_snapshot(index_code: str, now: datetime | None = None) -> dict[str, Any]:
    if index_code not in INDEXES:
        raise ValueError("unsupported index code")
    now = now or datetime.now()

    constituents, constituent_updated_at, warning = _get_constituents(index_code)
    codes = [item["code"] for item in constituents]
    names = {item["code"]: item.get("name", "") for item in constituents}
    ths_industries = _load_ths_industries()
    sw_industries = load_industry_map(codes)
    taxonomy = _load_industry_taxonomy()

    history = _load_history_frames(codes)
    bse_codes = [code for code in codes if _is_bse_stock(code)]
    history.update(_fetch_bse_histories(bse_codes))
    required_history_date = _required_history_date()
    history_dates = {
        code: _frame_latest_date(frame) for code, frame in history.items()
    }
    history_covered = sum(
        1 for date in history_dates.values()
        if date and (not required_history_date or date >= required_history_date)
    )
    today = now.strftime("%Y%m%d")
    sina_codes = [code for code in codes if not _is_bse_stock(code)]
    sina_quotes = fetch_spot(sina_codes, today)
    valuation_quotes = _fetch_tencent_quotes(codes)
    market_quotes = dict(sina_quotes)
    for code, quote in valuation_quotes.items():
        market_quotes.setdefault(code, quote)

    stocks = []
    received_count = 0
    fallback_count = 0
    for code in codes:
        frame = history.get(code)
        quote = market_quotes.get(code)
        quote_source = "sina" if code in sina_quotes else "tencent"
        is_fallback = quote is None
        if is_fallback:
            quote = _fallback_quote(frame, names[code])
            quote_source = "history"
            fallback_count += int(quote is not None)
        else:
            received_count += 1
            quote, used_completed_quote = _prefer_completed_preopen_quote(
                quote,
                frame,
                names[code],
                now,
            )
            if used_completed_quote:
                quote_source = "history_preopen"
                is_fallback = True
                fallback_count += 1
        if not quote:
            continue
        valuation = valuation_quotes.get(code) or {}
        price = float(quote.get("close") or valuation.get("close") or 0)
        previous = _to_float(quote.get("prev_close") or valuation.get("prev_close"))
        if price <= 0:
            continue
        quote_time = quote.get("quote_time") or valuation.get("quote_time")
        quality = _quote_quality(quote_time, now, fallback=is_fallback)
        quoted_change = _to_float(
            quote.get("change_pct")
            if quote_source.startswith("history")
            else (
                valuation.get("change_pct")
                if valuation.get("change_pct") is not None
                else quote.get("change_pct")
            )
        )
        change_pct, previous = _reconciled_return(price, previous, quoted_change)
        open_price = _to_float(
            quote.get("open") if quote.get("open") is not None else valuation.get("open")
        )
        high = float(quote.get("high") or valuation.get("high") or price)
        low = float(quote.get("low") or valuation.get("low") or price)
        amplitude = (high - low) / previous * 100 if previous else None
        ths_industry = ths_industries.get(code)
        details = taxonomy.get(code) or {}
        sw_industry = details.get("sw_level1") or sw_industries.get(code, "其他")
        display_industry = ths_industry or sw_industry
        stocks.append({
            "code": code,
            "name": quote.get("name") or names[code],
            "industry": display_industry,
            "industry_source": "ths" if ths_industry else "sw_fallback",
            "industry_ths": display_industry,
            "industry_sw": sw_industry,
            "industry_sw_detail": details.get("sw_level3") or details.get("sw_level2") or sw_industry,
            "industry_citic": _citic_industry(ths_industry, sw_industry),
            "sw_level2": details.get("sw_level2") or None,
            "sw_level3": details.get("sw_level3") or None,
            "price": round(price, 2),
            "prev_close": _round_optional(previous),
            "open": _round_optional(open_price),
            "change_pct": _round_optional(change_pct) if change_pct is not None else None,
            "_return_pct": change_pct,
            "amplitude_pct": round(amplitude, 2) if amplitude is not None else None,
            "volume_lots": _round_optional(valuation.get("volume_lots", quote.get("volume_lots")), 2),
            "amount_10k": _round_optional(valuation.get("amount_10k", quote.get("amount_10k")), 2),
            "vwap": _round_optional(valuation.get("vwap", quote.get("vwap")), 4),
            "turnover_rate": _round_optional(valuation.get("turnover_rate", quote.get("turnover_rate"))),
            "pe": _round_optional(valuation.get("pe", quote.get("pe"))),
            "pb": _round_optional(valuation.get("pb", quote.get("pb"))),
            "market_cap": _round_optional(valuation.get("market_cap", quote.get("market_cap"))),
            "circulating_market_cap": _round_optional(
                valuation.get("market_cap", quote.get("market_cap"))
            ),
            "total_market_cap": _round_optional(
                valuation.get("total_market_cap", quote.get("total_market_cap"))
            ),
            "quote_source": quote_source,
            "quote_time": quote_time,
            **quality,
            "history_date": history_dates.get(code),
            **_recent_returns(frame, price, required_history_date),
        })

    official_weights = _get_official_weights(index_code)
    weight_model = _assign_index_weights(stocks, history, official_weights)
    index_quote = _fetch_tencent_index_quote(index_code)
    current_minutes = now.hour * 60 + now.minute
    if (
        now.weekday() < 5
        and current_minutes < 9 * 60 + 30
    ):
        completed_index_quote = _fetch_tencent_index_daily_quote(index_code)
        if completed_index_quote is not None:
            index_quote = completed_index_quote
    if index_quote is not None:
        index_quote = {
            **index_quote,
            **_quote_quality(index_quote.get("quote_time"), now),
        }
    metrics = _compute_index_metrics(stocks, index_quote, weight_model)
    live_count = sum(bool(stock.get("is_live")) for stock in stocks)
    quote_state_counts: dict[str, int] = {}
    for stock in stocks:
        state = stock.get("quote_state") or "unknown"
        quote_state_counts[state] = quote_state_counts.get(state, 0) + 1
    source = "intraday" if live_count else "latest_close"
    intraday = _attach_intraday_history(
        index_code,
        stocks,
        metrics,
        now,
        record=live_count > 0,
    )

    ths_count = sum(1 for stock in stocks if stock["industry_source"] == "ths")
    detail_count = sum(1 for stock in stocks if stock["sw_level2"] or stock["sw_level3"])
    citic_count = sum(1 for stock in stocks if stock["industry_citic"] != "其他")
    coverage_warning = None
    if ths_count < len(stocks):
        coverage_warning = (
            f"现有同花顺行业映射覆盖 {ths_count}/{len(stocks)} 只，"
            "其余成分暂按申万一级行业补充并单独标识"
        )
    warnings = [
        message for message in (
            warning,
            coverage_warning,
            official_weights.get("warning"),
        ) if message
    ]
    if history_covered < len(codes):
        warnings.append(
            f"近期涨跌幅日线基准要求截至 {required_history_date or '未知'}，"
            f"当前覆盖 {history_covered}/{len(codes)} 只；缺失值已隐藏"
        )
    if live_count < received_count:
        warnings.append(
            f"收到 {received_count} 只行情，其中仅 {live_count} 只满足"
            f"{LIVE_QUOTE_MAX_AGE_SECONDS}秒盘中新鲜度要求"
        )
    for stock in stocks:
        stock.pop("_return_pct", None)

    return {
        "schema_version": 2,
        "index": {"code": index_code, **INDEXES[index_code]},
        "indexes": [{"code": code, **info} for code, info in INDEXES.items()],
        "updated_at": now.isoformat(timespec="seconds"),
        "constituent_updated_at": constituent_updated_at,
        "history_required_date": required_history_date,
        "source": source,
        "cache_state": "fresh",
        "index_quote": index_quote,
        "warning": "；".join(warnings) or None,
        "methodology": {
            "weight": weight_model,
            "contribution": {
                "formula": "previous_close_weight_pct × stock_return_pct = contribution_bp",
                "points_formula": "index_prev_close × contribution_bp / 10000",
                "official_weight_note": "官方文件为最近收盘权重；价格漂移与盘中动态权重均为近似重建",
            },
            "freshness": {
                "live_max_age_seconds": LIVE_QUOTE_MAX_AGE_SECONDS,
                "delayed_max_age_seconds": DELAYED_QUOTE_MAX_AGE_SECONDS,
                "continuous_sessions": ["09:30-11:30", "13:00-15:00"],
                "states": [
                    "live", "delayed", "stale", "outside_session",
                    "unknown", "fallback", "invalid_future",
                ],
            },
        },
        "weight_model": weight_model,
        **metrics,
        "intraday": intraday,
        "intraday_series": intraday["series"],
        "classifications": {
            "ths": {"label": "同花顺", "field": "industry_ths"},
            "sw": {"label": "申万一级", "field": "industry_sw"},
            "sw3": {"label": "申万三级", "field": "industry_sw_detail", "note": "缺少三级数据时回退至二级或一级"},
            "citic": {
                "label": "中信口径",
                "field": "industry_citic",
                "note": "按同花顺细分行业优先、申万一级兜底映射至中信一级口径",
            },
        },
        "coverage": {
            "constituents": len(codes),
            "quotes": len(stocks),
            "live_quotes": live_count,
            "received_quotes": received_count,
            "fallback_quotes": fallback_count,
            "effective_live_ratio": round(live_count / max(len(codes), 1), 4),
            "effective_live_weight_pct": metrics["replication"]["effective_live_weight_pct"],
            "quote_weight_coverage_pct": metrics["replication"]["quote_weight_coverage_pct"],
            "quote_states": quote_state_counts,
            "ths_industries": ths_count,
            "citic_industries": citic_count,
            "sw_details": detail_count,
            "history": history_covered,
        },
        "stocks": stocks,
    }


def get_index_snapshot(index_code: str, force: bool = False) -> dict[str, Any]:
    now = time.time()
    with _cache_guard:
        cached = _quote_cache.get(index_code)
        if cached and not force and now - cached[0] < QUOTE_TTL_SECONDS:
            return cached[1]
        index_lock = _index_locks.setdefault(index_code, threading.Lock())

    with index_lock:
        now = time.time()
        with _cache_guard:
            cached = _quote_cache.get(index_code)
            if cached and not force and now - cached[0] < QUOTE_TTL_SECONDS:
                return cached[1]
        try:
            snapshot = build_index_snapshot(index_code)
        except Exception as exc:
            with _cache_guard:
                cached = _quote_cache.get(index_code)
            if not cached:
                raise
            stale = dict(cached[1])
            stale["cache_state"] = "stale_last_good"
            stale["served_at"] = datetime.now().isoformat(timespec="seconds")
            stale["last_good_age_seconds"] = round(time.time() - cached[0], 1)
            stale["stale_reason"] = str(exc)
            stale["warning"] = "；".join(
                message for message in (
                    stale.get("warning"),
                    f"实时刷新失败，继续使用上一份成功快照: {exc}",
                ) if message
            )
            return stale
        with _cache_guard:
            _quote_cache[index_code] = (time.time(), snapshot)
        return snapshot
