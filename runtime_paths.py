"""Runtime paths shared by the Electron app and Python entry points."""

from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_STATIC_DIR = os.path.join(PROJECT_ROOT, "static")

RESOURCE_STATIC_DIR = os.path.abspath(
    os.environ.get("STOCK_FINANCE_RESOURCE_DIR", _DEFAULT_STATIC_DIR)
)
DATA_DIR = os.path.abspath(
    os.environ.get("STOCK_FINANCE_DATA_DIR")
    or os.environ.get("STOCK_FINANCE_STATIC_DIR")
    or _DEFAULT_STATIC_DIR
)
USER_DATA_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("STOCK_FINANCE_USER_DATA_DIR", "~/.stock-finance"))
)
LEGACY_USER_DATA_DIR = os.path.abspath(os.path.expanduser("~/.stock-finance"))

SEED_PATTERNS = (
    "new_highs_data_*.json",
    "new_highs_details_*.json",
    "new_lows_data_*.json",
    "new_lows_details_*.json",
    "intraday_highs_*.json",
    "intraday_lows_*.json",
    "intraday_history*.json",
    "intraday_temperature*.json",
    "market_temperature.json",
    "crowding*.json",
    "capital_flow*.json",
    "market_cap*.json",
    "margin_financing*.json",
    "highs_period_counts.json",
    "lows_period_counts.json",
    "stock_shares.json",
    "stock_pe.json",
    "ai_report_latest.json",
    "update_manifest.json",
    "industry_etf_map_ths.json",
    "industry_etf_map_sw3.json",
    "etf_snapshot.json",
    "etf_recommend_ths.json",
    "etf_recommend_sw3.json",
    "etf_prediction_log.jsonl",
    "momentum_etf*.json",
    "momentum_*.json",
    "etf_backtest.json",
    "index_constituents_cache.json",
    "index_weight_cache.json",
)
INITIALIZED_MARKER = ".stock-finance-initialized"
ETF_MODEL_SEED_FILES = (
    "etf_recommend_sw3.json",
    "etf_snapshot.json",
    "etf_backtest.json",
    "momentum_dynamic_pool.json",
    "momentum_etf.json",
)
CROWDING_SCHEME_SEED_FILES = (
    "crowding_ths.json",
    "crowding_sw3.json",
    "crowding_detail_ths.json",
    "crowding_detail_sw3.json",
)
CAPITAL_FLOW_V2_SEED_FILES = (
    "capital_flow_v2.json",
    "capital_flow_v2_ths.json",
    "capital_flow_v2_sw3.json",
)
MARKET_CAP_V2_SEED_FILES = (
    "market_cap_v2.json",
    "market_cap_v2_ths.json",
    "market_cap_v2_sw3.json",
    "market_cap_share_history_cninfo.json",
    "market_cap_point_in_time_shares.json",
)
MARGIN_FINANCING_SEED_FILES = (
    "margin_financing.json",
    "margin_financing_ths.json",
    "margin_financing_sw3.json",
)


def resource_path(*parts: str) -> str:
    return os.path.join(RESOURCE_STATIC_DIR, *parts)


def data_path(*parts: str) -> str:
    return os.path.join(DATA_DIR, *parts)


def user_data_path(*parts: str) -> str:
    return os.path.join(USER_DATA_DIR, *parts)


def _copy_seed_file(source: str, destination: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".seed-", dir=DATA_DIR)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _json_field(path: str, field: str):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload.get(field) if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _refresh_etf_model_seeds(copied: list[str]) -> None:
    """模型升级时刷新派生缓存，保留用户池、设置和原始市场数据。"""
    bundled_recommendation = os.path.join(RESOURCE_STATIC_DIR, "etf_recommend_sw3.json")
    runtime_recommendation = os.path.join(DATA_DIR, "etf_recommend_sw3.json")
    bundled_version = _json_field(bundled_recommendation, "model_version")
    if not bundled_version or _json_field(runtime_recommendation, "model_version") == bundled_version:
        return
    for filename in ETF_MODEL_SEED_FILES:
        source = os.path.join(RESOURCE_STATIC_DIR, filename)
        if not os.path.isfile(source):
            continue
        _copy_seed_file(source, os.path.join(DATA_DIR, filename))
        if filename not in copied:
            copied.append(filename)


def _seed_new_crowding_schemes(copied: list[str]) -> None:
    """Add newly introduced classifications to an existing data directory."""
    for filename in CROWDING_SCHEME_SEED_FILES:
        source = os.path.join(RESOURCE_STATIC_DIR, filename)
        destination = os.path.join(DATA_DIR, filename)
        if not os.path.isfile(source) or os.path.exists(destination):
            continue
        _copy_seed_file(source, destination)
        copied.append(filename)


def _seed_capital_flow_v2(copied: list[str]) -> None:
    """Give existing installations the new momentum model without overwrites."""
    for filename in CAPITAL_FLOW_V2_SEED_FILES:
        source = os.path.join(RESOURCE_STATIC_DIR, filename)
        destination = os.path.join(DATA_DIR, filename)
        if not os.path.isfile(source) or os.path.exists(destination):
            continue
        _copy_seed_file(source, destination)
        copied.append(filename)


def _seed_market_cap_v2(copied: list[str]) -> None:
    """Add the structure model and dated-share cache without overwrites."""
    for filename in MARKET_CAP_V2_SEED_FILES:
        source = os.path.join(RESOURCE_STATIC_DIR, filename)
        destination = os.path.join(DATA_DIR, filename)
        if not os.path.isfile(source) or os.path.exists(destination):
            continue
        _copy_seed_file(source, destination)
        copied.append(filename)


def _seed_margin_financing(copied: list[str]) -> None:
    """Seed the newly added margin page for existing installations."""
    for filename in MARGIN_FINANCING_SEED_FILES:
        source = os.path.join(RESOURCE_STATIC_DIR, filename)
        destination = os.path.join(DATA_DIR, filename)
        if not os.path.isfile(source) or os.path.exists(destination):
            continue
        _copy_seed_file(source, destination)
        copied.append(filename)


def initialize_data_dir() -> list[str]:
    """Seed a new data directory without replacing existing user data."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    # Preserve settings and AI history when moving from ~/.stock-finance to
    # Electron's standard userData directory.
    if os.path.realpath(USER_DATA_DIR) != os.path.realpath(LEGACY_USER_DATA_DIR):
        for filename in ("config.json", "history.db"):
            source = os.path.join(LEGACY_USER_DATA_DIR, filename)
            destination = os.path.join(USER_DATA_DIR, filename)
            if os.path.isfile(source) and not os.path.exists(destination):
                shutil.copy2(source, destination)

    if os.path.realpath(DATA_DIR) == os.path.realpath(RESOURCE_STATIC_DIR):
        return []

    copied = []
    _refresh_etf_model_seeds(copied)
    _seed_new_crowding_schemes(copied)
    _seed_capital_flow_v2(copied)
    _seed_market_cap_v2(copied)
    _seed_margin_financing(copied)

    marker = os.path.join(DATA_DIR, INITIALIZED_MARKER)
    if os.path.exists(marker):
        return copied

    for pattern in SEED_PATTERNS:
        for source in glob.glob(os.path.join(RESOURCE_STATIC_DIR, pattern)):
            if not os.path.isfile(source):
                continue
            destination = os.path.join(DATA_DIR, os.path.basename(source))
            if os.path.exists(destination):
                continue
            _copy_seed_file(source, destination)
            copied.append(os.path.basename(source))
    Path(marker).touch(mode=0o600, exist_ok=True)
    return copied


def is_runtime_data_file(filename: str) -> bool:
    """Return whether a root-level file is safe to serve from DATA_DIR."""
    if not filename or Path(filename).name != filename:
        return False
    if filename == "industry-heatmap-standalone.html":
        return True
    return any(Path(filename).match(pattern) for pattern in SEED_PATTERNS) or filename == "update_history.jsonl"
