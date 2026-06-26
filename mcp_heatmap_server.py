#!/usr/bin/env python3
"""Local stdio MCP server for the industry heatmap data.

Example client config:
{
  "mcpServers": {
    "industry-heatmap": {
      "command": "python3",
      "args": ["/Users/linyixin/Desktop/jinhua/stock-finance/mcp_heatmap_server.py"]
    }
  }
}
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

import heatmap_data as hd

STATIC_DIR = os.environ.get("STOCK_FINANCE_STATIC_DIR", hd.DEFAULT_STATIC_DIR)
mcp = FastMCP("industry-heatmap")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def get_market_snapshot() -> str:
    """获取行业热力图当前市场快照，包括日期、市场基调、新高/新低总量。"""
    return _json(hd.get_market_snapshot(STATIC_DIR))


@mcp.tool()
def get_top_industries(direction: str = "highs", period: str = "month", limit: int = 10) -> str:
    """按最新交易日获取创新高/创新低最集中的行业。

    Args:
        direction: highs/创新高 或 lows/创新低。
        period: month, 60d, 120d, 1year, alltime。
        limit: 返回行业数量，默认 10。
    """
    return _json(hd.get_top_industries(STATIC_DIR, direction, period, limit))


@mcp.tool()
def get_industry_detail(
    industry: str,
    direction: str = "highs",
    period: str = "month",
    date_label: str | None = None,
    limit: int = 100,
) -> str:
    """获取某行业在某日期的创新高/创新低股票明细。

    Args:
        industry: 申万行业名，例如 电子、医药生物、银行。
        direction: highs/创新高 或 lows/创新低。
        period: month, 60d, 120d, 1year, alltime。
        date_label: 日期标签，例如 6月25日；不填则使用最新日期。
        limit: 返回股票数量上限。
    """
    return _json(hd.get_industry_detail(STATIC_DIR, industry, direction, period, date_label, limit))


@mcp.tool()
def get_latest_ai_report() -> str:
    """获取行业热力图生成的最新 AI 日报 JSON。"""
    return _json(hd.get_latest_report(STATIC_DIR))


@mcp.tool()
def get_capital_flow_summary(limit: int = 10) -> str:
    """获取资金流向摘要和成交额最高的行业排名。"""
    return _json(hd.get_capital_flow_summary(STATIC_DIR, limit))


if __name__ == "__main__":
    mcp.run()
