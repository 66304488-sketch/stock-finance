"""
Stock Finance API — 个股财务信息聚合服务
数据源:
  A/E: Tencent(行情)  /  B: 东方财富数据中心  /  F: Sina(K线)
"""

import asyncio
import json
import math
import os
import logging
import re
from datetime import datetime
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("stock-finance")

app = FastAPI(title="Stock Finance API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================================================================
#  工具
# ======================================================================

def _em_code(code: str) -> str:
    return f"SH{code}" if code.startswith(("6", "9")) else f"SZ{code}"


# ======================================================================
# A. 实时行情  (Tencent / qt.gtimg.cn)
# ======================================================================

async def fetch_quote(code: str) -> dict:
    """通过腾讯股票API获取实时行情 + PE/市值/换手率"""
    market = "sh" if code.startswith(("6", "9")) else "sz"
    symbol = f"{market}{code}"
    url = "https://qt.gtimg.cn/q=" + symbol
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            timeout=10,
        ) as c:
            r = await c.get(url)
            raw = r.text
            # 处理 GBK 编码
            try:
                raw = raw.encode("latin1").decode("gbk")
            except Exception:
                pass
            # 提取 ~ 分隔的值
            m = re.search(r'"(.+)"', raw)
            if not m:
                return {}
            parts = m.group(1).split("~")

        def _f(i):
            v = parts[i] if i < len(parts) else ""
            return v.strip()

        name = _f(1)
        price = _f(3)
        prev_close = _f(4)
        open_ = _f(5)
        volume_lots = _f(6)  # 手
        high = _f(33)
        low = _f(34)
        change_pct = _f(32)
        turnover_rate = _f(38)
        pe = _f(39)
        market_cap = _f(44)  # 总市值 亿
        circ_market_cap = _f(45)  # 流通市值 亿
        total_shares = _f(72) if len(parts) > 72 else None

        # 涨跌幅 解析 (Tencent 的涨跌幅可能是带 % 符号)
        def _num(s):
            if not s:
                return None
            s = s.replace("%", "").strip()
            try:
                return float(s)
            except ValueError:
                return None

        cp = _num(change_pct)
        vol = _num(volume_lots)
        if vol:
            vol = int(vol * 100)  # 手 → 股

        return {
            "name": name,
            "code": code,
            "latestPrice": _num(price),
            "prevClose": _num(prev_close),
            "open": _num(open_),
            "high": _num(high),
            "low": _num(low),
            "changePercent": cp,
            "volume": vol,
            "turnoverRate": _num(turnover_rate),
            "pe": _num(pe),
            "marketCap": _num(market_cap),  # 亿
            "circMarketCap": _num(circ_market_cap),  # 亿
        }
    except Exception as e:
        logger.error("fetch_quote(%s): %s", code, e)
        return {}


# ======================================================================
# B. 财务数据  (datacenter-web.eastmoney.com)
# ======================================================================

async def _dc_get(code: str, report_name: str, sort_col: str = "NOTICE_DATE",
                  page_size: int = 8) -> list[dict]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code}")',
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortTypes": "-1",
        "sortColumns": sort_col,
    }
    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/",
        },
        timeout=15,
    ) as c:
        r = await c.get(url, params=params)
        result = r.json()
        if result.get("success"):
            return result["result"].get("data") or []
        return []


async def fetch_financials(code: str) -> dict:
    indicators, income, balance, cashflow = await asyncio.gather(
        _dc_get(code, "RPT_LICO_FN_CPD"),
        _dc_get(code, "RPT_DMSK_FN_INCOME"),
        _dc_get(code, "RPT_DMSK_FN_BALANCE"),
        _dc_get(code, "RPT_DMSK_FN_CASHFLOW"),
    )
    return dict(indicators=indicators, income=income,
                balance=balance, cashflow=cashflow)


# ======================================================================
# F. K线 / 技术指标  (Sina)
# ======================================================================

async def fetch_kline_sina(code: str, limit: int = 300) -> list[dict]:
    market = "sh" if code.startswith(("6", "9")) else "sz"
    symbol = f"{market}{code}"
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={limit}")
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            timeout=10,
        ) as c:
            r = await c.get(url)
            data = r.json()
            if isinstance(data, list):
                return [
                    {
                        "date": row["day"],
                        "open": float(row["open"]),
                        "close": float(row["close"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "volume": float(row["volume"]),
                    }
                    for row in data
                    if all(k in row for k in ("day", "open", "close", "high", "low", "volume"))
                ]
    except Exception as e:
        logger.error("fetch_kline_sina(%s): %s", code, e)
    return []


def _ema(s: list[float], p: int) -> list[float]:
    k = 2.0 / (p + 1)
    r = [s[0]]
    for i in range(1, len(s)):
        r.append((s[i] - r[-1]) * k + r[-1])
    return r


async def calc_technicals(code: str) -> dict:
    kl = await fetch_kline_sina(code)
    if not kl:
        return {}
    n = len(kl)
    cl = [k["close"] for k in kl]
    hi = [k["high"] for k in kl]

    def ma(p: int):
        return round(sum(cl[-p:]) / p, 2) if n >= p else None

    def macd():
        if n < 26:
            return {}
        e12 = _ema(cl, 12)
        e26 = _ema(cl, 26)
        dif = e12[-1] - e26[-1]
        dif_s = [e12[i] - e26[i] for i in range(n)]
        dea_l = _ema(dif_s[-9:], 9) if len(dif_s) >= 9 else [0]
        dea = dea_l[-1]
        return {"dif": round(dif, 4), "dea": round(dea, 4),
                "macd": round(2 * (dif - dea), 4)}

    def rsi(p: int = 14):
        if n < p + 1:
            return None
        g, ls = [], []
        for i in range(n - p, n):
            ch = cl[i] - cl[i - 1]
            g.append(max(ch, 0))
            ls.append(max(-ch, 0))
        ag = sum(g) / p
        al = sum(ls) / p
        return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)

    def bias(p: int = 20):
        if n < p:
            return None
        sma = sum(cl[-p:]) / p
        return round((cl[-1] - sma) / sma * 100, 2)

    def vol20():
        if n < 21:
            return None
        lr = [math.log(cl[i] / cl[i - 1]) for i in range(n - 20, n)]
        m = sum(lr) / len(lr)
        return round(math.sqrt(sum((x - m) ** 2 for x in lr) / len(lr)) * 100, 2)

    def max_dd():
        mx, dd = hi[0], 0.0
        for h in hi:
            mx = max(mx, h)
            dd = max(dd, (mx - h) / mx * 100)
        return round(dd, 2)

    yr_hi = max(k["high"] for k in kl[-260:]) if n >= 260 else max(k["high"] for k in kl)
    yr_lo = min(k["low"] for k in kl[-260:]) if n >= 260 else min(k["low"] for k in kl)
    yr_hi_r = round(yr_hi, 2) if yr_hi else None
    yr_lo_r = round(yr_lo, 2) if yr_lo else None

    return {
        "ma5": ma(5),
        "ma10": ma(10),
        "ma20": ma(20),
        "macd": macd(),
        "rsi14": rsi(14),
        "bias20": bias(20),
        "volatility20": vol20(),
        "maxDrawdown1y": max_dd(),
        "yearHigh": yr_hi_r,
        "yearLow": yr_lo_r,
    }


# ======================================================================
# C. 杜邦拆解
# ======================================================================

def calc_dupont(fin: dict) -> dict:
    inc = fin.get("income", [])
    bal = fin.get("balance", [])
    if not inc or not bal:
        return {}

    def _ann(arr):
        for x in arr:
            d = (x.get("REPORT_DATE") or "").replace("T00:00:00", "")
            if d.endswith("12-31"):
                return x
        return arr[0] if arr else {}

    ii, bb = _ann(inc), _ann(bal)
    np_ = ii.get("PARENT_NETPROFIT")
    rev = ii.get("TOTAL_OPERATE_INCOME")
    ta = bb.get("TOTAL_ASSETS")
    eq = bb.get("TOTAL_EQUITY")
    r = {}
    if rev and np_:
        r["netProfitMargin"] = round(np_ / rev * 100, 2)
    if rev and ta:
        r["assetTurnover"] = round(rev / ta, 4)
    if ta and eq:
        r["equityMultiplier"] = round(ta / eq, 4)
    return r


# ======================================================================
# D. 股东人数
# ======================================================================

async def fetch_shareholders(code: str) -> dict:
    """从 data.eastmoney.com/gdhs 页面提取股东人数"""
    url = f"https://data.eastmoney.com/gdhs/{code}.html"
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            timeout=10, follow_redirects=True,
        ) as c:
            r = await c.get(url)
            html = r.text

            # 匹配 JSON 数据中的 HOLDER_NUM
            nums_str = re.findall(r'"HOLDER_NUM"\s*:\s*"?(\d+)"?', html)
            dates_str = re.findall(r'"END_DATE"\s*:\s*"([\d\-]+)"', html)
            if nums_str and len(nums_str) >= 1:
                latest = int(nums_str[0])
                prev = int(nums_str[1]) if len(nums_str) > 1 else 0
                ratio = round((latest - prev) / prev * 100, 2) if prev else None
                return {
                    "latestCount": latest,
                    "prevCount": prev or None,
                    "changeRatio": ratio,
                    "endDate": dates_str[0] if dates_str else None,
                }
            return {}
    except Exception as e:
        logger.error("fetch_shareholders(%s): %s", code, e)
        return {}


# ======================================================================
# E. 盈利预测
# ======================================================================

async def fetch_forecast(code: str) -> dict:
    """通过 emweb session 获取盈利预测"""
    em = _em_code(code)
    session = httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        timeout=10,
        follow_redirects=True,
    )
    try:
        await session.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/ResearchNew/Index?type=web&code=" + em
        )
        r = await session.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/ResearchNew/ResearchForecastAjax",
            params={"code": em},
        )
        if r.status_code == 200:
            result = r.json()
            arr = result.get("data") or []
            out = {}
            for item in arr:
                yr = item.get("REPORT_DATE_YEAR")
                eps = item.get("AVG_EPS")
                cnt = item.get("FORECAST_COUNT")
                tag = {2026: "2026E", 2027: "2027E", 2028: "2028E"}.get(yr)
                if tag:
                    out[f"eps{tag}"] = eps
                    out[f"forecastCount{yr}"] = cnt
            if out:
                return out
    except Exception as e:
        logger.warning("fetch_forecast(%s): %s", code, e)
    finally:
        await session.aclose()
    return {}


# ======================================================================
# API 端点
# ======================================================================

@app.get("/api/stock/{code}")
async def get_stock_all(code: str):
    code = code.strip().upper().replace("SH", "").replace("SZ", "")
    if not code or not code.isdigit():
        raise HTTPException(400, "请输入6位股票代码")

    quote, fin, sh, fc, tech = await asyncio.gather(
        fetch_quote(code),
        fetch_financials(code),
        fetch_shareholders(code),
        fetch_forecast(code),
        calc_technicals(code),
    )
    dupont = calc_dupont(fin)
    return {
        "code": code,
        "quote": quote,
        "financials": fin,
        "dupont": dupont,
        "shareholders": sh,
        "forecast": fc,
        "technicals": tech,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ======================================================================
# 前端
# ======================================================================

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    # 通配路由：所有未被 API 匹配的路径 → 静态文件
    @app.get("/{filename:path}")
    async def serve_static(filename: str):
        file_path = os.path.join(static_dir, filename)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # 尝试加 .html 后缀
        html_path = os.path.join(static_dir, filename + ".html")
        if os.path.isfile(html_path):
            return FileResponse(html_path)
        # 根路径 / 返回 index.html
        if filename in ("", "/", "index"):
            index_path = os.path.join(static_dir, "index.html")
            if os.path.isfile(index_path):
                return FileResponse(index_path)
        raise HTTPException(404, "Not found")

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=True)
