"""Read-only helpers for generated industry heatmap data."""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
VALID_PERIODS = {"month", "60d", "120d", "1year", "alltime"}
DIRECTION_PREFIX = {"highs": "new_highs", "lows": "new_lows"}
DIRECTION_LABEL = {"highs": "创新高", "lows": "创新低"}


def _static_dir(static_dir: str | None = None) -> str:
    return static_dir or DEFAULT_STATIC_DIR


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_direction(direction: str) -> str:
    value = (direction or "").strip().lower()
    aliases = {
        "high": "highs",
        "highs": "highs",
        "new_highs": "highs",
        "new-highs": "highs",
        "up": "highs",
        "创新高": "highs",
        "low": "lows",
        "lows": "lows",
        "new_lows": "lows",
        "new-lows": "lows",
        "down": "lows",
        "创新低": "lows",
    }
    if value not in aliases:
        raise ValueError("direction must be highs or lows")
    return aliases[value]


def _normalize_period(period: str) -> str:
    value = (period or "month").strip().lower()
    aliases = {"20d": "month", "20": "month", "月": "month"}
    value = aliases.get(value, value)
    if value not in VALID_PERIODS:
        raise ValueError(f"period must be one of {sorted(VALID_PERIODS)}")
    return value


def _data_path(static_dir: str | None, direction: str, period: str, details: bool = False) -> str:
    direction = _normalize_direction(direction)
    period = _normalize_period(period)
    suffix = "details" if details else "data"
    return os.path.join(_static_dir(static_dir), f"{DIRECTION_PREFIX[direction]}_{suffix}_{period}.json")


def load_heatmap(static_dir: str | None = None, direction: str = "highs", period: str = "month") -> dict[str, Any]:
    """Load a generated heatmap count file."""
    return _read_json(_data_path(static_dir, direction, period))


def load_heatmap_details(static_dir: str | None = None, direction: str = "highs", period: str = "month") -> dict[str, Any]:
    """Load a generated heatmap details file."""
    return _read_json(_data_path(static_dir, direction, period, details=True))


def get_latest_report(static_dir: str | None = None) -> dict[str, Any]:
    """Load the latest AI report JSON."""
    return _read_json(os.path.join(_static_dir(static_dir), "ai_report_latest.json"))


def get_total_row(data: dict[str, Any]) -> dict[str, Any] | None:
    for row in data.get("industries", []):
        if row.get("is_total") or row.get("industry") == "全市场合计":
            return row
    return None


def get_latest_date(data: dict[str, Any]) -> str | None:
    dates = data.get("dates") or []
    if not dates:
        return None
    first = dates[0]
    if isinstance(first, dict):
        return first.get("label") or first.get("full_label")
    return str(first)


def get_market_snapshot(static_dir: str | None = None) -> dict[str, Any]:
    """Return a compact market snapshot for tools and diagnostics."""
    static_dir = _static_dir(static_dir)
    try:
        report = get_latest_report(static_dir)
        metrics = report.get("metrics", {})
        return {
            "date": report.get("date") or metrics.get("date"),
            "market_tone": report.get("market_tone") or metrics.get("market_tone"),
            "highs_month_total": metrics.get("highs_month_total"),
            "lows_month_total": metrics.get("lows_month_total"),
            "highs_alltime_total": metrics.get("highs_alltime_total"),
            "lows_alltime_total": metrics.get("lows_alltime_total"),
            "high_low_ratio": metrics.get("high_low_ratio"),
            "generated_at": report.get("generated_at"),
        }
    except Exception:
        snapshot: dict[str, Any] = {}
        for direction in ("highs", "lows"):
            try:
                data = load_heatmap(static_dir, direction, "month")
                total = get_total_row(data) or {}
                snapshot.setdefault("date", get_latest_date(data))
                snapshot[f"{direction}_month_total"] = (total.get("daily_counts") or [None])[0]
            except Exception:
                continue
        return snapshot


def get_top_industries(
    static_dir: str | None = None,
    direction: str = "highs",
    period: str = "month",
    limit: int = 10,
) -> dict[str, Any]:
    """Return top industries by latest count for a direction and period."""
    direction = _normalize_direction(direction)
    period = _normalize_period(period)
    limit = max(1, min(int(limit or 10), 50))
    data = load_heatmap(static_dir, direction, period)
    date_label = get_latest_date(data)
    rows = []
    for row in data.get("industries", []):
        if row.get("is_total"):
            continue
        counts = row.get("daily_counts") or []
        count = counts[0] if counts else 0
        total = row.get("total") or 0
        ratio = round(count / max(total, 1) * 100, 1) if total else row.get("ratio", 0)
        if count:
            rows.append({
                "industry": row.get("industry"),
                "count": count,
                "total": total,
                "ratio": ratio,
            })
    rows.sort(key=lambda item: (item.get("count") or 0, item.get("ratio") or 0), reverse=True)
    return {
        "direction": direction,
        "direction_label": DIRECTION_LABEL[direction],
        "period": period,
        "type_label": data.get("type_label"),
        "date": date_label,
        "industries": rows[:limit],
        "updated_at": data.get("updated_at"),
    }


def get_industry_detail(
    static_dir: str | None = None,
    industry: str = "",
    direction: str = "highs",
    period: str = "month",
    date_label: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return stock details for an industry/date cell."""
    direction = _normalize_direction(direction)
    period = _normalize_period(period)
    industry = (industry or "").strip()
    if not industry:
        raise ValueError("industry is required")
    data = load_heatmap(static_dir, direction, period)
    details = load_heatmap_details(static_dir, direction, period)
    date_label = date_label or get_latest_date(data)
    stocks = ((details.get(industry) or {}).get(date_label or "") or [])
    limit = max(1, min(int(limit or 100), 500))
    return {
        "industry": industry,
        "direction": direction,
        "direction_label": DIRECTION_LABEL[direction],
        "period": period,
        "date": date_label,
        "count": len(stocks),
        "stocks": stocks[:limit],
        "truncated": len(stocks) > limit,
    }


def get_capital_flow_summary(static_dir: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Return a compact capital-flow ranking."""
    limit = max(1, min(int(limit or 10), 50))
    data = _read_json(os.path.join(_static_dir(static_dir), "capital_flow.json"))
    rows = []
    for row in data.get("industries", []):
        if row.get("is_total") or row.get("industry") == "全市场合计":
            continue
        rows.append({
            "industry": row.get("industry"),
            "turnover": row.get("turnover"),
            "share": row.get("share"),
            "change_pct": row.get("change_pct"),
            "trend_5d": row.get("trend_5d"),
            "signal": row.get("signal"),
            "stock_count": row.get("stock_count"),
        })
    rows.sort(key=lambda item: item.get("turnover") or 0, reverse=True)
    return {
        "dates": data.get("dates", [])[-5:],
        "updated_at": data.get("updated_at"),
        "total_turnover": data.get("total_turnover"),
        "industries": rows[:limit],
    }


def build_chat_context(static_dir: str | None = None) -> str:
    """Build a short Chinese market context for the embedded AI chat."""
    static_dir = _static_dir(static_dir)
    lines: list[str] = []
    try:
        report = get_latest_report(static_dir)
        metrics = report.get("metrics", {})
        lines.append(f"日期: {metrics.get('date', report.get('date', '未知'))}")
        lines.append(f"市场基调: {metrics.get('market_tone', report.get('market_tone', '未知'))}")
        lines.append(f"全市场20日新低: {metrics.get('lows_month_total', '?')}只")
        lines.append(f"一年新低: {metrics.get('lows_1year_total', '?')}只")
        lines.append(f"近7年新低: {metrics.get('lows_alltime_total', '?')}只")
        lines.append(f"历史新高: {metrics.get('highs_alltime_total', '?')}只")
        lines.append(f"新高/新低比: {metrics.get('high_low_ratio', '?')}")

        industries = metrics.get("industries", {})
        if industries:
            lines.append("\n行业概览 (20日新高%/新低%):")
            sorted_items = sorted(
                industries.items(),
                key=lambda x: -(x[1].get("highs_20d_ratio", 0) + x[1].get("lows_20d_ratio", 0)),
            )
            for ind, stats in sorted_items[:15]:
                high_ratio = stats.get("highs_20d_ratio", 0)
                low_ratio = stats.get("lows_20d_ratio", 0)
                lines.append(f"  {ind}: 新高{high_ratio:.0f}% / 新低{low_ratio:.0f}%")
        return "\n".join(lines)
    except Exception:
        pass

    try:
        data = load_heatmap(static_dir, "lows", "month")
        date_label = get_latest_date(data) or "?"
        lines.append(f"日期: {date_label}")
        rows = sorted(
            data.get("industries", []),
            key=lambda x: -((x.get("daily_counts") or [0])[0]),
        )
        for row in rows:
            if not row.get("is_total"):
                count = (row.get("daily_counts") or [0])[0]
                lines.append(f"  {row.get('industry')}: 新低{count}只 / 共{row.get('total', '?')}只")
    except Exception:
        lines.append("(数据暂不可用)")
    return "\n".join(lines)
