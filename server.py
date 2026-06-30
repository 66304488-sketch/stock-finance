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
import threading
from datetime import datetime
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from kline_cache import KlineCache, get_active_codes, load_industry_map
from heatmap_data import build_chat_context
from mcp_config import (
    MCP_TOKEN_MASK,
    build_anthropic_mcp_parts,
    merge_mcp_servers,
    sanitize_mcp_servers,
)

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
    yr_hi_r = round(yr_hi, 2) if yr_hi is not None else None
    yr_lo_r = round(yr_lo, 2) if yr_lo is not None else None

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
# 设置端点
# ======================================================================

CONFIG_DIR = os.path.expanduser("~/.stock-finance")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def _load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("config.json 损坏，使用默认配置")
            return {}
    return {}


def _save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


@app.get("/api/settings")
async def settings_get():
    cfg = _load_config()
    return {
        "ai_provider": cfg.get("ai_provider", "anthropic"),
        "api_key_configured": bool(cfg.get("api_key")),
        "mcp_enabled": bool(cfg.get("mcp_enabled", False)),
        "mcp_servers": sanitize_mcp_servers(cfg.get("mcp_servers") or []),
    }


@app.post("/api/settings")
async def settings_save(req: dict):
    provider = (req.get("ai_provider") or "anthropic").strip()
    key = (req.get("api_key") or "").strip()
    if provider not in ("anthropic", "deepseek"):
        raise HTTPException(400, "Invalid provider")
    cfg = _load_config()
    cfg["ai_provider"] = provider
    if key and key != MCP_TOKEN_MASK:
        cfg["api_key"] = key
    if "mcp_enabled" in req:
        cfg["mcp_enabled"] = bool(req.get("mcp_enabled"))
    if "mcp_servers" in req:
        try:
            cfg["mcp_servers"] = merge_mcp_servers(
                cfg.get("mcp_servers") or [],
                req.get("mcp_servers") or [],
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
    _save_config(cfg)
    return {"status": "ok"}


@app.post("/api/settings/run-analysis")
async def settings_run_analysis():
    """重新运行 AI 分析"""
    import subprocess, sys
    proc = subprocess.run(
        [sys.executable, "ai_analyzer.py"],
        cwd=os.path.dirname(__file__),
        capture_output=True, text=True,
        timeout=120,
    )
    return {"status": "ok", "output": proc.stdout[-500:] if proc.stdout else proc.stderr[-500:]}


@app.post("/api/intraday-scan")
async def intraday_scan(req: dict):
    """盘中实时扫描新高/新低"""
    import subprocess, sys
    window = (req.get("window") or "all").strip()
    proc = subprocess.run(
        [sys.executable, "scan_intraday.py", "--window", window],
        cwd=os.path.dirname(__file__),
        capture_output=True, text=True,
        timeout=300,
    )
    return {"status": "ok", "output": proc.stdout[-1000:] if proc.stdout else proc.stderr[-500:]}


# 数据刷新状态（带锁保证线程安全）
_refresh_status = {
    "running": False,
    "current_step": "",
    "success": None,
    "error": None,
    "progress": None,
    "steps": [],
}
_refresh_lock = threading.Lock()

DATASET_LABELS = {
    "highs": "创新高",
    "lows": "创新低",
    "capital_flow": "资金流向",
    "market_cap": "行业市值",
    "ai": "AI 分析",
    "standalone": "独立 HTML",
}
DATASET_FILES = {
    "highs": ["new_highs_data_*.json", "new_highs_details_*.json"],
    "lows": ["new_lows_data_*.json", "new_lows_details_*.json"],
    "capital_flow": ["capital_flow.json", "capital_flow_ths.json"],
    "market_cap": ["market_cap.json", "market_cap_ths.json"],
    "ai": ["ai_report_latest.json"],
    "standalone": ["industry-heatmap-standalone.html"],
}
DEFAULT_UPDATE_CONFIG = {
    "sources": {
        "highs": "sina_kline",
        "lows": "sina_kline",
        "capital_flow": "sina_kline_cache",
        "market_cap": "sina_kline_cache",
        "basics": "akshare_excel",
    },
    "frozen": {
        "highs": False,
        "lows": False,
        "capital_flow": False,
        "market_cap": False,
        "ai": False,
        "standalone": False,
    },
}
SUPPORTED_SOURCES = {
    "highs": {"sina_kline", "sina_kline_cache"},
    "lows": {"sina_kline", "sina_kline_cache"},
    "capital_flow": {"sina_kline_cache"},
    "basics": {"akshare_excel"},
}


def _manifest_path():
    return os.path.join(static_dir, "update_manifest.json")


def _history_path():
    return os.path.join(static_dir, "update_history.jsonl")


def _load_update_manifest():
    manifest = {
        "version": 1,
        "updated_at": None,
        "config": json.loads(json.dumps(DEFAULT_UPDATE_CONFIG)),
        "datasets": {},
    }
    path = _manifest_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                manifest.update(saved)
        except Exception as e:
            logger.warning("update_manifest.json 读取失败，使用默认配置: %s", e)
    cfg = manifest.setdefault("config", {})
    cfg.setdefault("sources", {}).update({
        k: cfg.get("sources", {}).get(k, v)
        for k, v in DEFAULT_UPDATE_CONFIG["sources"].items()
    })
    cfg.setdefault("frozen", {}).update({
        k: bool(cfg.get("frozen", {}).get(k, v))
        for k, v in DEFAULT_UPDATE_CONFIG["frozen"].items()
    })
    manifest.setdefault("datasets", {})
    return manifest


def _save_update_manifest(manifest):
    os.makedirs(static_dir, exist_ok=True)
    manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = _manifest_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _manifest_path())


def _append_update_history(event):
    try:
        event = dict(event)
        event.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with open(_history_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("写入 update_history.jsonl 失败: %s", e)


def _set_refresh_status(**kwargs):
    with _refresh_lock:
        _refresh_status.update(kwargs)


def _get_trade_date_args(days):
    """取最近 N 个交易日。15 点前默认使用上一交易日，避免当天未收盘数据污染。"""
    import akshare as ak
    import pandas as pd
    now = datetime.now()
    df = ak.tool_trade_date_hist_sina()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    today = pd.Timestamp(now.date())
    last = df[df["trade_date"] <= today].iloc[-1]["trade_date"]
    if last == today and now.hour < 15:
        last = df[df["trade_date"] < today].iloc[-1]["trade_date"]
    dates = df[df["trade_date"] <= last].tail(days)["trade_date"]
    return ",".join(d.strftime("%Y%m%d") for d in dates)


def _parse_full_label(lbl):
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", lbl or "")
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return datetime.min


def _date_key(d):
    return d.get("full_label") or d.get("label") or ""


def _merge_data(old_data, new_data, max_dates=15):
    """合并两个 counts JSON，优先以 full_label 对齐，避免只用“6月25日”跨年撞车。"""
    date_map = {}
    for d in old_data.get("dates", []):
        date_map[_date_key(d)] = d
    for d in new_data.get("dates", []):
        date_map[_date_key(d)] = d
    merged_dates = sorted(
        date_map.values(),
        key=lambda d: _parse_full_label(d.get("full_label", d.get("label", ""))),
        reverse=True,
    )[:max_dates]
    labels = [d["label"] for d in merged_dates]
    keys = [_date_key(d) for d in merged_dates]

    old_rows = {r["industry"]: r for r in old_data.get("industries", [])}
    new_rows = {r["industry"]: r for r in new_data.get("industries", [])}
    old_keys = [_date_key(d) for d in old_data.get("dates", [])]
    new_keys = [_date_key(d) for d in new_data.get("dates", [])]

    merged_rows = []
    for ind in sorted(set(old_rows.keys()) | set(new_rows.keys())):
        if ind == "全市场合计":
            continue
        old_row = old_rows.get(ind)
        new_row = new_rows.get(ind)
        old_counts = {}
        if old_row:
            for i, c in enumerate(old_row.get("daily_counts", [])):
                if i < len(old_keys):
                    old_counts[old_keys[i]] = c
        new_counts = {}
        if new_row:
            for i, c in enumerate(new_row.get("daily_counts", [])):
                if i < len(new_keys):
                    new_counts[new_keys[i]] = c
        total = 0
        if new_row and new_row.get("total"):
            total = new_row["total"]
        elif old_row and old_row.get("total"):
            total = old_row["total"]
        counts = []
        for key in keys:
            if key in new_counts:
                counts.append(new_counts[key])
            elif key in old_counts:
                counts.append(old_counts[key])
            else:
                counts.append(0)
        ratio = round(counts[0] / max(total, 1) * 100, 1) if total else 0.0
        merged_rows.append({"industry": ind, "total": total, "daily_counts": counts, "ratio": ratio})

    merged_rows.sort(key=lambda r: r["daily_counts"][0] if r["daily_counts"] else 0, reverse=True)
    total_counts = [0] * len(labels)
    all_total = 0
    for r in merged_rows:
        all_total += r["total"]
        for i, c in enumerate(r["daily_counts"]):
            total_counts[i] += c
    merged_rows.append({
        "industry": "全市场合计",
        "total": all_total,
        "daily_counts": total_counts,
        "ratio": 0,
        "is_total": True,
    })
    return {
        "dates": merged_dates,
        "updated_at": new_data.get("updated_at", old_data.get("updated_at", "")),
        "type": new_data.get("type", old_data.get("type", "")),
        "type_label": new_data.get("type_label", old_data.get("type_label", "")),
        "industries": merged_rows,
    }


def _merge_details(old_details, new_details, labels):
    if not isinstance(old_details, dict):
        old_details = {}
    if not isinstance(new_details, dict):
        new_details = {}
    merged = {}
    for ind in set(old_details.keys()) | set(new_details.keys()):
        old_dd = old_details.get(ind, {})
        new_dd = new_details.get(ind, {})
        dd = {}
        for lbl in labels:
            if lbl in new_dd:
                dd[lbl] = new_dd[lbl]
            elif lbl in old_dd:
                dd[lbl] = old_dd[lbl]
            else:
                dd[lbl] = []
        merged[ind] = dd
    return merged


def _glob_dataset_files(dataset):
    import glob
    files = []
    for pattern in DATASET_FILES.get(dataset, []):
        files.extend(glob.glob(os.path.join(static_dir, pattern)))
    return sorted(set(files))


def _backup_dataset(dataset):
    import shutil
    backups = []
    for fp in _glob_dataset_files(dataset):
        backup_fp = fp + ".backup"
        shutil.copy2(fp, backup_fp)
        backups.append((fp, backup_fp))
    return backups


def _restore_backups(backups):
    import shutil
    for fp, backup_fp in backups:
        if os.path.exists(backup_fp):
            shutil.copy2(backup_fp, fp)
            os.remove(backup_fp)


def _drop_backups(backups):
    for _, backup_fp in backups:
        if os.path.exists(backup_fp):
            os.remove(backup_fp)


def _merge_back(base_name):
    fp = os.path.join(static_dir, base_name)
    backup_fp = fp + ".backup"
    details_name = base_name.replace("_data_", "_details_")
    details_fp = os.path.join(static_dir, details_name)
    details_backup_fp = details_fp + ".backup"

    if not os.path.exists(backup_fp) or not os.path.exists(fp):
        return

    with open(fp, "r", encoding="utf-8") as f:
        new_data = json.load(f)
    with open(backup_fp, "r", encoding="utf-8") as f:
        old_data = json.load(f)
    merged = _merge_data(old_data, new_data)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    os.remove(backup_fp)

    if os.path.exists(details_backup_fp) and os.path.exists(details_fp):
        with open(details_fp, "r", encoding="utf-8") as f:
            new_details = json.load(f)
        with open(details_backup_fp, "r", encoding="utf-8") as f:
            old_details = json.load(f)
        labels = [d["label"] for d in merged.get("dates", [])]
        merged_details = _merge_details(old_details, new_details, labels)
        with open(details_fp, "w", encoding="utf-8") as f:
            json.dump(merged_details, f, ensure_ascii=False)
        os.remove(details_backup_fp)

        # 从合并后的 details 重建 daily_counts，保证数据一致
        for row in merged.get("industries", []):
            if row.get("is_total"):
                continue
            ind = row["industry"]
            row["daily_counts"] = [len(merged_details.get(ind, {}).get(lbl, [])) for lbl in labels]
        # 重建全市场合计行
        total_counts = [0] * len(labels)
        for row in merged.get("industries", []):
            if not row.get("is_total"):
                for i, c in enumerate(row.get("daily_counts", [])):
                    if i < len(total_counts):
                        total_counts[i] += c
        for row in merged.get("industries", []):
            if row.get("is_total"):
                row["daily_counts"] = total_counts
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False)


def _run_refresh_step(step_name, script_args, timeout_sec=300):
    import subprocess, sys
    _set_refresh_status(current_step=f"{step_name} (运行中...)", progress=None)
    try:
        r = subprocess.run(
            [sys.executable] + script_args,
            cwd=os.path.dirname(__file__),
            capture_output=True, text=True, timeout=timeout_sec,
        )
        # 从 stdout 提取进度信息
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line: continue
            m = re.search(r'(\d+)\s*/\s*(\d+)', line)
            if m:
                cur, tot = int(m.group(1)), int(m.group(2))
                pct = min(99, int(cur / max(tot, 1) * 100))
                _set_refresh_status(
                    progress={"current": cur, "total": tot, "pct": pct},
                    current_step=f"{step_name} ({cur}/{tot}, {pct}%)",
                )
        _set_refresh_status(progress=None)
        if r.returncode != 0:
            err = r.stderr.strip()[-300:] if r.stderr else f"code={r.returncode}"
            return False, err
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"{step_name} 超时 ({timeout_sec}s)"
    except Exception as e:
        return False, str(e)


def _update_dataset_success(manifest, dataset, source, date_args, mode, note=""):
    manifest.setdefault("datasets", {})[dataset] = {
        "label": DATASET_LABELS.get(dataset, dataset),
        "source": source,
        "last_success_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_args": date_args,
        "mode": mode,
        "status": "success",
        "note": note,
    }
    _save_update_manifest(manifest)
    _append_update_history({"dataset": dataset, "status": "success", "source": source, "dates": date_args, "mode": mode})


def _update_dataset_failure(manifest, dataset, source, date_args, mode, error):
    manifest.setdefault("datasets", {})[dataset] = {
        "label": DATASET_LABELS.get(dataset, dataset),
        "source": source,
        "last_failure_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date_args": date_args,
        "mode": mode,
        "status": "failed",
        "error": error,
    }
    _save_update_manifest(manifest)
    _append_update_history({"dataset": dataset, "status": "failed", "source": source, "dates": date_args, "mode": mode, "error": error})


@app.get("/api/update-config")
async def update_config_get():
    """返回数据更新配置、冻结状态和最近同步 manifest。"""
    manifest = _load_update_manifest()
    return {
        "config": manifest.get("config", {}),
        "datasets": manifest.get("datasets", {}),
        "supported_sources": {k: sorted(v) for k, v in SUPPORTED_SOURCES.items()},
    }


@app.post("/api/update-config")
async def update_config_save(req: dict):
    """保存按数据集的数据源和冻结配置。"""
    manifest = _load_update_manifest()
    cfg = manifest.setdefault("config", {})
    sources = cfg.setdefault("sources", {})
    frozen = cfg.setdefault("frozen", {})

    for dataset, source in (req or {}).get("sources", {}).items():
        if dataset in SUPPORTED_SOURCES and source in SUPPORTED_SOURCES[dataset]:
            sources[dataset] = source
    for dataset, value in (req or {}).get("frozen", {}).items():
        if dataset in DEFAULT_UPDATE_CONFIG["frozen"]:
            frozen[dataset] = bool(value)

    _save_update_manifest(manifest)
    return {"status": "ok", "config": manifest["config"]}


@app.post("/api/update-config/freeze")
async def update_config_freeze(req: dict):
    dataset = (req or {}).get("dataset")
    if dataset not in DEFAULT_UPDATE_CONFIG["frozen"]:
        raise HTTPException(400, "未知数据集")
    manifest = _load_update_manifest()
    manifest.setdefault("config", {}).setdefault("frozen", {})[dataset] = bool((req or {}).get("frozen", True))
    _save_update_manifest(manifest)
    return {"status": "ok", "dataset": dataset, "frozen": manifest["config"]["frozen"][dataset]}


@app.post("/api/refresh-capital-flow")
async def refresh_capital_flow():
    """兼容旧按钮：只更新资金流向。"""
    return await refresh_data({"days": 1, "datasets": ["capital_flow"]})


@app.post("/api/refresh-data")
async def refresh_data(req: dict = None):
    """启动可分数据集的数据更新流水线。默认增量更新新高、新低、资金流向。"""
    import threading
    req = req or {}
    days = max(1, min(int(req.get("days", 1)), 30))
    requested = req.get("datasets") or ["highs", "lows", "capital_flow"]
    datasets = [d for d in requested if d in DATASET_LABELS]
    mode = req.get("mode") or "incremental"
    if mode not in ("incremental", "missing", "rebuild"):
        raise HTTPException(400, "mode 必须是 incremental/missing/rebuild")
    force_rebuild = bool(req.get("force_rebuild")) or mode == "rebuild"

    with _refresh_lock:
        if _refresh_status["running"]:
            return {"status": "already_running"}
        _refresh_status.update({
            "running": True,
            "current_step": "初始化...",
            "success": None,
            "error": None,
            "progress": None,
            "steps": [],
        })

    def run_pipeline():
        manifest = _load_update_manifest()
        cfg = manifest.setdefault("config", {})
        frozen = cfg.setdefault("frozen", {})
        sources = cfg.setdefault("sources", {})
        steps = []
        try:
            date_args = _get_trade_date_args(days)
            latest_date = date_args.split(",")[-1]
            types = ["month", "60d", "120d", "1year", "alltime"]

            # 新高/新低共用 K 线预热；资金流向脚本会自行按缺失日期决定是否触碰缓存，避免重复 ensure。
            market_datasets = [d for d in datasets if d in ("highs", "lows") and not frozen.get(d)]
            if market_datasets:
                _set_refresh_status(current_step="预热 K 线缓存...")
                active_codes = get_active_codes()
                industry_map = load_industry_map(active_codes)
                codes_with_industry = [c for c in active_codes if c in industry_map]
                cache = KlineCache(force_refresh=force_rebuild)
                cache.ensure(codes_with_industry, latest_date)
                _set_refresh_status(current_step="K 线缓存预热完成")

            for dataset in datasets:
                label = DATASET_LABELS[dataset]
                source = sources.get(dataset) or sources.get("basics") or "default"
                if frozen.get(dataset) and not force_rebuild:
                    steps.append({"dataset": dataset, "label": label, "status": "skipped", "reason": "已冻结"})
                    continue

                backups = _backup_dataset(dataset)
                try:
                    if dataset == "highs":
                        for scheme in ["sw", "ths"]:
                            scheme_label = "申万" if scheme == "sw" else "同花顺"
                            args = ["fetch_new_highs.py", "--type", "all", "--dates", date_args,
                                    "--industry-scheme", scheme]
                            if force_rebuild:
                                args.append("--force-refresh")
                            ok, err = _run_refresh_step(f"{label} ({scheme_label}, 最近{days}天)", args, 900)
                            if not ok:
                                raise RuntimeError(err)
                            sfx = "_ths" if scheme == "ths" else ""
                            for t in types:
                                _merge_back(f"new_highs_data_{t}{sfx}.json")
                    elif dataset == "lows":
                        for scheme in ["sw", "ths"]:
                            scheme_label = "申万" if scheme == "sw" else "同花顺"
                            args = ["fetch_new_lows.py", "--type", "all", "--dates", date_args,
                                    "--industry-scheme", scheme]
                            if force_rebuild:
                                args.append("--force-refresh")
                            ok, err = _run_refresh_step(f"{label} ({scheme_label}, 最近{days}天)", args, 600)
                            if not ok:
                                raise RuntimeError(err)
                            sfx = "_ths" if scheme == "ths" else ""
                            for t in types:
                                _merge_back(f"new_lows_data_{t}{sfx}.json")
                    elif dataset == "capital_flow":
                        for scheme in ["sw", "ths"]:
                            args = ["fetch_capital_flow.py", "--dates", date_args, "--mode", mode, "--industry-scheme", scheme]
                            if force_rebuild:
                                args.append("--force-refresh")
                            ok, err = _run_refresh_step(f"{label} ({scheme}, 最近{days}天, {mode})", args, 300)
                            if not ok:
                                raise RuntimeError(err)
                    elif dataset == "market_cap":
                        # 同时更新申万和同花顺
                        for scheme in ["sw", "ths"]:
                            args = ["fetch_market_cap.py", "--dates", date_args, "--mode", mode, "--industry-scheme", scheme]
                            if force_rebuild:
                                args.append("--force-refresh")
                            ok, err = _run_refresh_step(f"{label} ({scheme}, 最近{days}天, {mode})", args, 300)
                            if not ok:
                                raise RuntimeError(err)
                    elif dataset == "ai":
                        ok, err = _run_refresh_step(label, ["ai_analyzer.py"], 120)
                        if not ok:
                            raise RuntimeError(err)
                    elif dataset == "standalone":
                        ok, err = _run_refresh_step(label, ["generate_standalone.py"], 60)
                        if not ok:
                            raise RuntimeError(err)
                    _drop_backups(backups)
                    # 更新多周期计数文件（只要更新了 highs/lows）
                    if dataset in ("highs", "lows"):
                        try:
                            _run_refresh_step("多周期计数", ["build_period_counts.py"], 30)
                        except Exception:
                            pass  # 非关键，失败不影响主流程
                    _update_dataset_success(manifest, dataset, source, date_args, mode)
                    steps.append({"dataset": dataset, "label": label, "status": "success"})
                except Exception as e:
                    _restore_backups(backups)
                    err = str(e)
                    _update_dataset_failure(manifest, dataset, source, date_args, mode, err)
                    steps.append({"dataset": dataset, "label": label, "status": "failed", "error": err})
                    _set_refresh_status(error=f"{label} 失败: {err}", success=False, running=False, steps=steps)
                    return

            _set_refresh_status(success=True, current_step="完成", running=False, steps=steps)
        except Exception as e:
            _set_refresh_status(error=f"流水线异常: {str(e)}", success=False, running=False, steps=steps)

    threading.Thread(target=run_pipeline, daemon=True).start()
    return {"status": "started", "datasets": datasets, "mode": mode}


@app.get("/api/refresh-data/status")
async def refresh_data_status():
    with _refresh_lock:
        r = dict(_refresh_status)
    return {
        "running": r["running"],
        "current_step": r["current_step"],
        "success": r["success"],
        "error": r.get("error"),
        "progress": r.get("progress"),
        "steps": r.get("steps", []),
        "manifest": _load_update_manifest(),
        "message": "数据更新完成" if r["success"] else (r.get("error") or ""),
    }


# ======================================================================
# 数据备份 / 还原 / 清空
# ======================================================================

def _get_backup_dir():
    """读取用户设置的备份目录，默认 ~/.stock-finance/backups/"""
    cfg = _load_config()
    return cfg.get("backup_dir") or os.path.join(CONFIG_DIR, "backups")
DATA_GLOB = ["new_highs_data_*.json", "new_lows_data_*.json",
             "new_highs_details_*.json", "new_lows_details_*.json",
             "ai_report_latest.json", "all_klines.pkl"]


@app.get("/api/backup/settings")
async def backup_settings():
    """返回备份目录设置"""
    return {"backup_dir": _load_config().get("backup_dir") or os.path.join(CONFIG_DIR, "backups")}


@app.post("/api/backup/settings")
async def backup_settings_save(req: dict):
    """保存备份目录"""
    path = (req or {}).get("backup_dir", "").strip()
    if not path:
        raise HTTPException(400, "路径不能为空")
    if not os.path.isabs(path):
        raise HTTPException(400, "请使用绝对路径")
    cfg = _load_config()
    cfg["backup_dir"] = path
    _save_config(cfg)
    return {"status": "ok", "backup_dir": path}


@app.post("/api/backup")
async def backup_data():
    """备份所有数据文件到 ~/.stock-finance/backups/"""
    import shutil, glob
    os.makedirs(_get_backup_dir(), exist_ok=True)
    saved = []
    for pattern in DATA_GLOB:
        for fp in glob.glob(os.path.join(static_dir, pattern)):
            base = os.path.basename(fp)
            dest = os.path.join(_get_backup_dir(), base)
            shutil.copy2(fp, dest)
            saved.append(base)
    return {"status": "ok", "saved": len(saved), "files": saved}


@app.post("/api/restore")
async def restore_data():
    """从最近备份还原数据文件"""
    import shutil
    if not os.path.exists(_get_backup_dir()):
        raise HTTPException(404, "没有找到备份目录")
    files = os.listdir(_get_backup_dir())
    if not files:
        raise HTTPException(404, "备份目录为空")
    restored = []
    for fname in files:
        src = os.path.join(_get_backup_dir(), fname)
        dst = os.path.join(static_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            restored.append(fname)
    return {"status": "ok", "restored": len(restored), "files": restored}


@app.post("/api/clear-data")
async def clear_data():
    """清空所有数据文件（新高/新低/AI报告/K线缓存）"""
    import glob
    deleted = []
    for pattern in DATA_GLOB:
        for fp in glob.glob(os.path.join(static_dir, pattern)):
            os.remove(fp)
            deleted.append(os.path.basename(fp))
    return {"status": "ok", "deleted": len(deleted), "files": deleted}


@app.get("/api/backup/status")
async def backup_status():
    """查看备份状态"""
    import time as _time
    if not os.path.exists(_get_backup_dir()):
        return {"exists": False, "files": [], "time": None}
    files = sorted(os.listdir(_get_backup_dir()))
    mtime = os.path.getmtime(_get_backup_dir()) if files else None
    return {"exists": True, "count": len(files),
            "time": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(mtime)) if mtime else None}


# ======================================================================
# ======================================================================
# 资金流向端点
# ======================================================================

@app.get("/api/capital-flow")
async def capital_flow(scheme: str = "sw"):
    """返回行业资金流向数据。scheme=sw(申万)/ths(同花顺)"""
    import json as _json
    filename = "capital_flow.json" if scheme == "sw" else f"capital_flow_{scheme}.json"
    path = os.path.join(static_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(404, f"资金流向数据尚未生成: {filename}")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


# ======================================================================
# 行业市值端点
# ======================================================================

@app.get("/api/market-cap")
async def market_cap(scheme: str = "sw"):
    """返回行业市值变化数据。scheme=sw(申万)/ths(同花顺)"""
    import json as _json
    filename = "market_cap.json" if scheme == "sw" else f"market_cap_{scheme}.json"
    path = os.path.join(static_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(404, f"市值数据尚未生成: {filename}")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


# ======================================================================
# AI 市场分析端点
# ======================================================================

@app.get("/api/report/latest")
async def ai_report_latest():
    """返回最新的 AI 市场分析报告"""
    import json as _json
    report_path = os.path.join(static_dir, "ai_report_latest.json")
    if not os.path.exists(report_path):
        raise HTTPException(404, "报告尚未生成，请运行 ai_analyzer.py")
    with open(report_path, "r", encoding="utf-8") as f:
        return _json.load(f)


@app.post("/api/chat")
async def ai_chat(req: dict):
    """AI 对话接口 (SSE 流式)"""
    question = (req.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")

    async def generate():
        import json as _json

        # 加载当前市场数据作为上下文
        data_ctx = _build_chat_context()

        system_prompt = f"""你是A股市场分析助手，嵌入在"行业热力图"桌面app中。
你的数据来自实盘行情聚合。回答问题时要具体引用数据中的数字。
优先基于下方"当前市场数据快照"回答。
如果用户明确要求查询外部知识库、研报、第三方系统或当前数据没有的信息，且 MCP 工具可用，可以调用 MCP；回答时请区分"本地热力图数据"与"外部 MCP 数据"。
如果用户问的问题本地数据和可用工具都无法覆盖，就诚实说"这超出了我当前数据的范围"。
使用中文，简洁直接。

当前市场数据快照:
{data_ctx}"""
        messages = [
            {"role": "user", "content": question},
        ]

        cfg = _load_config()
        api_key = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("api_key", "")
        if not api_key:
            yield f"data: {_json.dumps({'error': '未设置 API Key，请在设置页面配置'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        provider = cfg.get("ai_provider", "anthropic")

        try:
            if provider == "deepseek":
                from openai import OpenAI
                client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
                stream = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages,
                    max_tokens=1500,
                    stream=True,
                )
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield f"data: {_json.dumps({'delta': delta})}\n\n"
            else:
                from anthropic import Anthropic
                client = Anthropic(api_key=api_key)
                mcp_parts = build_anthropic_mcp_parts(cfg)
                if mcp_parts["mcp_servers"]:
                    with client.beta.messages.stream(
                        model="claude-opus-4-8",
                        max_tokens=1500,
                        system=system_prompt,
                        messages=messages,
                        betas=mcp_parts["betas"],
                        mcp_servers=mcp_parts["mcp_servers"],
                        tools=mcp_parts["tools"],
                    ) as stream:
                        for text in stream.text_stream:
                            yield f"data: {_json.dumps({'delta': text})}\n\n"
                else:
                    with client.messages.stream(
                        model="claude-opus-4-8",
                        max_tokens=1500,
                        system=system_prompt,
                        messages=messages,
                    ) as stream:
                        for text in stream.text_stream:
                            yield f"data: {_json.dumps({'delta': text})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'error': f'AI 调用失败: {str(e)}'})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def _build_chat_context() -> str:
    """构建注入对话上下文的市场数据摘要"""
    return build_chat_context(static_dir)


# ======================================================================
# 运行时信息
# ======================================================================

@app.get("/api/runtime-info")
async def runtime_info():
    """给桌面端确认当前 8001 端口是否是同一份应用资源。"""
    root_dir = os.path.dirname(__file__)
    return {
        "project_root": os.path.realpath(root_dir),
        "static_dir": os.path.realpath(os.path.join(root_dir, "static")),
    }


# ======================================================================
# 前端
# ======================================================================

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    # 通配路由：所有未被 API 匹配的路径 → 静态文件
    @app.get("/{filename:path}")
    async def serve_static(filename: str):
        # 防止路径遍历攻击
        safe = os.path.normpath(filename)
        if safe.startswith("..") or os.path.isabs(safe):
            raise HTTPException(404, "Not found")
        file_path = os.path.realpath(os.path.join(static_dir, safe))
        if not file_path.startswith(os.path.realpath(static_dir) + os.sep):
            raise HTTPException(404, "Not found")
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # 尝试加 .html 后缀
        html_path = file_path + ".html"
        if os.path.isfile(html_path) and html_path.startswith(os.path.realpath(static_dir) + os.sep):
            return FileResponse(html_path)
        # 根路径 / 返回 index.html
        if filename in ("", "/", "index"):
            index_path = os.path.join(static_dir, "index.html")
            if os.path.isfile(index_path):
                return FileResponse(index_path)
        raise HTTPException(404, "Not found")

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=False)
