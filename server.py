"""
Stock Finance API — 个股财务信息聚合服务
数据源:
  A/E: Tencent(行情)  /  B: 东方财富数据中心  /  F: Sina(K线)
"""

import asyncio
import copy
import json
import math
import os
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from heatmap_data import build_chat_context
from db import get_db, reset_db, DB_PATH
from runtime_paths import (
    DATA_DIR,
    RESOURCE_STATIC_DIR,
    SEED_PATTERNS,
    USER_DATA_DIR,
    initialize_data_dir,
    is_runtime_data_file,
)
from mcp_config import (
    MCP_TOKEN_MASK,
    build_anthropic_mcp_parts,
    merge_mcp_servers,
    sanitize_mcp_servers,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("stock-finance")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """初始化可写数据目录并在首次运行时迁移 JSON → SQLite。"""
    try:
        initialize_data_dir()
        if os.path.exists(CONFIG_FILE):
            os.chmod(CONFIG_FILE, 0o600)
        db = get_db()
        db.import_from_json(DATA_DIR)
    except Exception as e:
        logger.warning("DB 初始化失败: %s", e)
    try:
        yield
    finally:
        reset_db()


app = FastAPI(title="Stock Finance API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def reject_nonlocal_api_writes(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and not re.match(r"^https?://(?:127\.0\.0\.1|localhost)(?::\d+)?(?:/|$)", source):
            return HTMLResponse("Forbidden", status_code=403)
    return await call_next(request)

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
                raise RuntimeError("行情响应格式异常")
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
        # Tencent qt: field 44 is circulating market cap and field 45 is
        # total market cap (both in 亿元). Keep this aligned with the
        # market-cap structure page, which uses total shares for total mcap.
        circ_market_cap = _f(44)
        market_cap = _f(45)
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
        raise


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
        raise
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
        raise


# ======================================================================
# E. 盈利预测
# ======================================================================

async def fetch_forecast(code: str) -> dict:
    """通过 emweb session 获取盈利预测"""
    em = _em_code(code)
    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        timeout=10,
        follow_redirects=True,
    ) as session:
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
    return {}


# ======================================================================
# API 端点
# ======================================================================

@app.get("/api/stock/{code}")
async def get_stock_all(code: str):
    code = code.strip().upper().replace("SH", "").replace("SZ", "")
    if not code or not code.isdigit():
        raise HTTPException(400, "请输入6位股票代码")

    upstream_names = ["quote", "financials", "shareholders", "forecast", "technicals"]
    results = await asyncio.gather(
        fetch_quote(code),
        fetch_financials(code),
        fetch_shareholders(code),
        fetch_forecast(code),
        calc_technicals(code),
        return_exceptions=True,
    )
    errors = []
    values = []
    for name, result in zip(upstream_names, results):
        if isinstance(result, Exception):
            logger.error("%s 上游失败(%s): %s", name, code, result)
            errors.append(name)
            values.append(
                dict(indicators=[], income=[], balance=[], cashflow=[])
                if name == "financials" else {}
            )
        else:
            values.append(result)
    quote, fin, sh, fc, tech = values
    dupont = calc_dupont(fin)
    return {
        "code": code,
        "quote": quote,
        "financials": fin,
        "dupont": dupont,
        "shareholders": sh,
        "forecast": fc,
        "technicals": tech,
        "errors": errors,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ======================================================================
# 设置端点
# ======================================================================

CONFIG_DIR = USER_DATA_DIR
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# 保护 config.json / update_manifest.json 的写，避免 pipeline 线程与 HTTP 处理并发互踩
_state_file_lock = threading.Lock()


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
    with _state_file_lock:
        fd, temporary = __import__("tempfile").mkstemp(prefix=".config-", dir=CONFIG_DIR)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, CONFIG_FILE)
            os.chmod(CONFIG_FILE, 0o600)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


# 本机客户端令牌：破坏性接口（清空/还原/改备份目录）必须在 X-SF-Token 头中携带。
# 仅监听 127.0.0.1 且 CORS 限定 localhost，因此 GET /api/client-token 只会发给本机前端。
_CLIENT_TOKEN = secrets.token_hex(16)


@app.get("/api/client-token")
async def client_token():
    """返回本实例的客户端令牌，供前端在破坏性请求头 X-SF-Token 中携带。"""
    return {"token": _CLIENT_TOKEN}


def _require_client_token(request: Request):
    if request.headers.get("x-sf-token") != _CLIENT_TOKEN:
        raise HTTPException(403, "缺少或无效的 X-SF-Token 头")


_PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _resolve_api_key(cfg: dict, provider: str) -> str:
    """按 provider 取密钥，绝不把 A 服务商的 key 发给 B 服务商。

    优先级：对应服务商的环境变量 → cfg["api_keys"][provider]
    → 向后兼容旧的单一 cfg["api_key"]（仅当它属于当前 provider）。
    """
    env_var = _PROVIDER_ENV_VARS.get(provider)
    if env_var:
        key = os.environ.get(env_var)
        if key:
            return key
    key = (cfg.get("api_keys") or {}).get(provider)
    if key:
        return key
    if provider == cfg.get("ai_provider", "anthropic"):
        return cfg.get("api_key", "") or ""
    return ""


@app.get("/api/settings")
async def settings_get():
    cfg = _load_config()
    provider = cfg.get("ai_provider", "anthropic")
    return {
        "ai_provider": provider,
        "api_key_configured": bool(
            cfg.get("api_key") or (cfg.get("api_keys") or {}).get(provider)
        ),
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
        cfg.setdefault("api_keys", {})[provider] = key
        cfg["api_key"] = key  # 兼容旧读取方：始终镜像当前 provider 的 key
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
    script_path = os.path.join(os.path.dirname(__file__), "ai_analyzer.py")
    if not os.path.exists(script_path):
        raise HTTPException(500, f"分析脚本不存在: {script_path}")
    try:
        proc = await asyncio.to_thread(
            subprocess.run, [sys.executable, script_path],
            cwd=os.path.dirname(__file__), capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "分析超时（超过 120 秒）")
    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout or "分析进程异常退出").strip()
        raise HTTPException(500, error[-500:])

    source = "unknown"
    report_path = os.path.join(data_dir, "ai_report_latest.json")
    try:
        with open(report_path, encoding="utf-8") as f:
            source = json.load(f).get("analysis_source") or "unknown"
    except (OSError, json.JSONDecodeError):
        pass
    output = proc.stdout[-500:] if proc.stdout else proc.stderr[-500:]
    return {"status": "ok", "analysis_source": source, "output": output}


_intraday_lock = threading.Lock()
_intraday_status = {
    "running": False,
    "success": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
    "scan_time": None,
    "coverage": None,
}
_custom_snapshot_lock = threading.Lock()
_custom_snapshot_cache = {}


def _normalize_date_label(value):
    match = re.match(r"(\d{4})年0?(\d{1,2})月0?(\d{1,2})日", value or "")
    if not match:
        return None
    return f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"


def _json_file(filename, default=None):
    try:
        with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _latest_trade_date_from_file(filename):
    data = _json_file(filename, {}) or {}
    dates = data.get("dates") or []
    return _normalize_date_label(dates[0].get("full_label", "")) if dates else None


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _now_shanghai():
    """交易时段判断统一用 Asia/Shanghai，与 Electron 调度器一致。"""
    return datetime.now(_SHANGHAI_TZ)


def _session_phase(now=None):
    now = now or _now_shanghai()
    if now.weekday() >= 5:
        return "closed"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 25:
        return "preopen"
    if minutes <= 11 * 60 + 30 or 13 * 60 <= minutes <= 15 * 60 + 5:
        return "trading"
    if minutes < 13 * 60:
        return "lunch"
    if minutes <= 17 * 60 + 30:
        return "awaiting_close"
    return "closed"


def _is_continuous_trading(now=None):
    now = now or _now_shanghai()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (
        9 * 60 + 30 <= minutes <= 11 * 60 + 30
        or 13 * 60 <= minutes <= 15 * 60
    )


def _intraday_paths(window, scheme):
    from scan_intraday import window_key
    suffix = {"sw": "", "ths": "_ths", "sw3": "_sw3", "citic": "_citic"}[scheme]
    key = window_key(window)
    return (
        os.path.join(DATA_DIR, f"intraday_highs_{key}{suffix}.json"),
        os.path.join(DATA_DIR, f"intraday_lows_{key}{suffix}.json"),
    )


def _try_begin_intraday_scan():
    """锁内 check-and-set：同一时刻只有一个调用方能启动盘中扫描。"""
    with _intraday_lock:
        if _intraday_status["running"]:
            return False
        _intraday_status.update({
            "running": True, "success": None, "error": None,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
        })
        return True


def _run_intraday_scan(windows, schemes):
    """执行盘中扫描。调用前必须先通过 _try_begin_intraday_scan 占位。"""
    from scan_intraday import run_scan
    try:
        result = run_scan(windows=windows, schemes=schemes, output_dir=DATA_DIR)
    except Exception as exc:
        logger.exception("盘中扫描失败")
        with _intraday_lock:
            _intraday_status.update({
                "running": False, "success": False, "error": str(exc),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
        raise
    with _intraday_lock:
        _intraday_status.update({
            "running": False, "success": True, "error": None,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "scan_time": result.get("scan_time"),
            "coverage": result.get("coverage"),
        })
    return result


def _parse_intraday_request(req):
    from scan_intraday import PRESET_WINDOWS, parse_window
    raw = req.get("windows", req.get("window", "all"))
    if raw == "all":
        windows = list(PRESET_WINDOWS.values())
    elif isinstance(raw, list):
        windows = [parse_window(value) for value in raw]
    else:
        windows = [parse_window(raw)]
    scheme = req.get("scheme", "all")
    if scheme not in ("sw", "ths", "sw3", "citic", "all"):
        raise ValueError("scheme must be sw, ths, sw3, citic or all")
    schemes = ["sw", "ths", "sw3"] if scheme == "all" else [scheme]
    return windows, schemes


@app.post("/api/intraday-scan")
async def intraday_scan(req: dict = None):
    """Start a non-blocking intraday scan."""
    req = req or {}
    try:
        windows, schemes = _parse_intraday_request(req)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    if not _try_begin_intraday_scan():
        with _intraday_lock:
            return {"status": "already_running", **_intraday_status}

    def runner():
        try:
            _run_intraday_scan(windows, schemes)
        except Exception:
            pass

    threading.Thread(target=runner, daemon=True).start()
    return {"status": "started", "windows": windows, "schemes": schemes}


@app.get("/api/intraday-scan/status")
async def intraday_scan_status():
    return dict(_intraday_status)


@app.get("/api/market-session")
async def market_session():
    now = _now_shanghai()
    today = now.strftime("%Y%m%d")
    phase = _session_phase(now)
    daily_latest = _latest_trade_date_from_file("new_highs_data_month.json")
    intraday = _json_file("intraday_highs_20d.json", {}) or {}
    intraday_latest = intraday.get("trade_date") or _latest_trade_date_from_file("intraday_highs_20d.json")
    if phase == "trading":
        recommended = "intraday"
    elif phase in ("lunch", "awaiting_close") and intraday_latest == today and daily_latest != today:
        recommended = "intraday"
    else:
        recommended = "daily"
    return {
        "phase": phase,
        "continuous_trading": _is_continuous_trading(now),
        "recommended_mode": recommended,
        "now": now.isoformat(timespec="seconds"),
        "today": today,
        "daily_latest": daily_latest,
        "intraday_latest": intraday_latest,
        "close_confirmed": daily_latest == today,
        "intraday_status": dict(_intraday_status),
    }


@app.get("/api/index-constituents")
async def index_constituents(index: str = "000300", refresh: bool = False):
    """Return one index's constituents and an evidence-gated futures monitor."""
    from index_constituents import get_index_snapshot
    from index_futures import INDEX_FUTURES, get_product_overview

    try:
        snapshot = await asyncio.to_thread(
            _load_index_snapshot_serialized,
            get_index_snapshot,
            index,
            refresh,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("指数成分数据获取失败")
        raise HTTPException(503, str(exc))

    product = next(
        (
            code
            for code, config in INDEX_FUTURES.items()
            if config["index_code"] == index
        ),
        None,
    )
    futures_product = None
    if product:
        try:
            futures_product = await asyncio.to_thread(
                get_product_overview,
                product,
                snapshot.get("index_quote"),
            )
        except Exception as exc:
            logger.warning("%s 期指行情暂不可用: %s", product, exc)

    result = copy.deepcopy(snapshot)
    if futures_product:
        result["futures"] = futures_product.get("quote")
        result["futures_overview"] = futures_product
    result["monitor"] = _build_index_futures_monitor(result, futures_product)
    result["quality"] = result["monitor"]["quality"]
    return result


_INDEX_FUTURES_OVERVIEW_CODES = {
    "IH": "000016",
    "IF": "000300",
    "IC": "000905",
    "IM": "000852",
}
_index_snapshot_api_lock = threading.Lock()


def _load_index_snapshot_serialized(getter, index_code: str, refresh: bool):
    """Serialize akshare-backed builds; its embedded JS runtime is not thread-safe."""
    with _index_snapshot_api_lock:
        return getter(index_code, refresh)


def _load_index_overview_snapshots(getter, refresh: bool):
    with _index_snapshot_api_lock:
        return [
            getter(index_code, refresh)
            for index_code in _INDEX_FUTURES_OVERVIEW_CODES.values()
        ]


def _monitor_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _monitor_round(value, digits=2):
    number = _monitor_number(value)
    return round(number, digits) if number is not None else None


def _build_index_futures_monitor(snapshot: dict, futures_product: dict | None) -> dict:
    """Combine four independent evidence layers without inventing a forecast."""
    replication = snapshot.get("replication") or {}
    breadth = snapshot.get("breadth") or {}
    structure = snapshot.get("driver_concentration") or {}
    intraday = snapshot.get("intraday") or {}
    windows = intraday.get("windows") or {}
    one_minute = windows.get("1m") or {}
    five_minute = windows.get("5m") or {}
    quote = (futures_product or {}).get("quote") or {}
    futures_history = quote.get("history") or {}
    futures_five_minute = futures_history.get("5m") or {}

    index_previous = _monitor_number(replication.get("index_prev_close"))

    def bp_to_points(value):
        basis_points = _monitor_number(value)
        if basis_points is None or index_previous is None:
            return None
        return round(index_previous * basis_points / 10000, 4)

    impulse_1m_bp = _monitor_number(one_minute.get("replicated_return_change_bp"))
    impulse_5m_bp = _monitor_number(five_minute.get("replicated_return_change_bp"))
    impulse_1m = bp_to_points(impulse_1m_bp)
    impulse_5m = bp_to_points(impulse_5m_bp)
    acceleration = (
        round(impulse_1m - impulse_5m / 5, 4)
        if impulse_1m is not None and impulse_5m is not None
        else None
    )

    weighted_breadth = _monitor_number(breadth.get("weighted_advance_pct"))
    equal_breadth = _monitor_number(breadth.get("equal_weight_advance_pct"))
    above_vwap = _monitor_number(breadth.get("weighted_above_vwap_pct"))
    concentration = _monitor_number(
        structure.get("top5_abs_contribution_share_pct")
    )
    effective_drivers = _monitor_number(structure.get("effective_driver_count"))
    residual = _monitor_number(replication.get("replication_residual_bp"))
    live_weight = _monitor_number(replication.get("effective_live_weight_pct"))
    confidence_level = str(replication.get("confidence") or "unavailable")
    confidence_score = {
        "high": 95,
        "medium": 78,
        "low": 48,
        "unavailable": 0,
    }.get(confidence_level, 0)
    if live_weight is not None:
        confidence_score = min(confidence_score, max(0, min(100, live_weight)))
    signal_ready = bool(replication.get("signal_ready"))

    spot_5m_pct = _monitor_number(five_minute.get("official_return_change_bp"))
    if spot_5m_pct is not None:
        spot_5m_pct /= 100
    futures_5m_pct = _monitor_number(
        futures_five_minute.get("futures_return_pct")
    )
    basis_change = _monitor_number(futures_five_minute.get("basis_change"))
    lead_lag = None
    if spot_5m_pct is not None and futures_5m_pct is not None:
        meaningful_spot = abs(spot_5m_pct) >= 0.02
        meaningful_future = abs(futures_5m_pct) >= 0.02
        if (
            meaningful_spot
            and meaningful_future
            and spot_5m_pct * futures_5m_pct < 0
        ):
            lead_lag = "期现背离"
        elif meaningful_spot and meaningful_future:
            if abs(futures_5m_pct) > abs(spot_5m_pct) + 0.03:
                lead_lag = "期货领先同向"
            else:
                lead_lag = "期现同向确认"
        else:
            lead_lag = "等待确认"

    evidence = []
    risks = []
    if impulse_5m is not None:
        evidence.append(f"5分钟成分贡献 {impulse_5m:+.2f} 点")
    if weighted_breadth is not None:
        evidence.append(f"上涨成分权重 {weighted_breadth:.1f}%")
    if concentration is not None:
        evidence.append(f"Top5绝对贡献占比 {concentration:.1f}%")
    if residual is not None:
        evidence.append(f"指数复制残差 {residual:+.1f}bp")
    if quote.get("contract"):
        basis = _monitor_number(quote.get("basis"))
        basis_text = f"，基差 {basis:+.2f}" if basis is not None else ""
        evidence.append(f"期指主力 {quote['contract']}{basis_text}")

    if not signal_ready:
        risks.append("行情新鲜度、有效权重或复制残差未通过质量门，禁止输出方向结论")
    if residual is not None and abs(residual) > 8:
        risks.append(f"复制残差 {residual:+.1f}bp 超过 8bp")
    if live_weight is not None and live_weight < 95:
        risks.append(f"实时有效权重仅 {live_weight:.1f}%")
    freshness = quote.get("freshness") or {}
    freshness_stale = (
        freshness.get("stale")
        if isinstance(freshness, dict)
        else freshness == "stale"
    )
    if quote and (quote.get("stale") or freshness_stale):
        risks.append("期指行情已过期，仅作最近状态展示")
    if quote.get("basis_quality") == "time_misaligned":
        skew = _monitor_number(quote.get("quote_skew_seconds"))
        detail = f"（相差 {skew:.0f} 秒）" if skew is not None else ""
        risks.append(f"期现时间戳未对齐{detail}，当前基差不可用于盘中判断")
    if concentration is not None and concentration >= 65:
        risks.append("Top5贡献过度集中，指数方向容易被少数权重股反转")
    if lead_lag == "期现背离":
        risks.append("期货与现货5分钟方向背离，趋势尚未确认")

    if not signal_ready:
        state_label = "证据不足·仅供观察"
        summary = "当前数据未通过可交易质量门；缺失值不按零值处理。"
    elif impulse_5m_bp is None:
        state_label = "样本积累中"
        summary = "等待至少5分钟连续快照，再判断方向、广度与期现是否共振。"
    else:
        rising = impulse_5m_bp >= 5
        falling = impulse_5m_bp <= -5
        broad_up = weighted_breadth is not None and weighted_breadth >= 55
        broad_down = weighted_breadth is not None and weighted_breadth <= 45
        if rising and broad_up:
            state_label = (
                "上行增强·期现确认"
                if lead_lag in {"期现同向确认", "期货领先同向"}
                else "上行增强·等待期货确认"
            )
        elif falling and broad_down:
            state_label = (
                "下行增强·期现确认"
                if lead_lag in {"期现同向确认", "期货领先同向"}
                else "下行增强·等待期货确认"
            )
        elif (rising and broad_down) or (falling and broad_up):
            state_label = "方向与广度背离"
        else:
            state_label = "震荡·等待方向"
        summary = "状态来自短窗贡献、加权广度、驱动集中度和期现确认四层证据。"

    if concentration is None:
        structure_label = None
    elif concentration >= 65:
        structure_label = "高度集中"
    elif concentration >= 45:
        structure_label = "中度集中"
    else:
        structure_label = "分散驱动"

    return {
        "state_label": state_label,
        "summary": summary,
        "evidence": evidence,
        "risks": risks,
        "impulse_1m": impulse_1m,
        "impulse_5m": impulse_5m,
        "impulse_1m_bp": _monitor_round(impulse_1m_bp, 4),
        "impulse_5m_bp": _monitor_round(impulse_5m_bp, 4),
        "acceleration": acceleration,
        "weighted_breadth": _monitor_round(weighted_breadth, 3),
        "equal_breadth": _monitor_round(equal_breadth, 3),
        "above_vwap_weight": _monitor_round(above_vwap, 3),
        "top5_concentration": _monitor_round(concentration, 3),
        "effective_drivers": _monitor_round(effective_drivers, 2),
        "structure_label": structure_label,
        "futures": quote or None,
        "basis_change": _monitor_round(basis_change, 4),
        "lead_lag_label": lead_lag,
        "quality": {
            "confidence": round(confidence_score, 1),
            "confidence_level": confidence_level,
            "signal_ready": signal_ready,
            "quote_time": (snapshot.get("index_quote") or {}).get("quote_time"),
            "effective_live_weight_pct": _monitor_round(live_weight, 3),
            "replication_residual_bp": _monitor_round(residual, 3),
        },
        "methodology": {
            "forecast": "不输出单因子涨跌预测，只展示可证伪的盘中状态",
            "basis": "原始基差不代表方向，仅观察同期限变化与期现是否同步",
            "quality_gate": replication.get("quality_gate"),
        },
    }


@app.get("/api/index-futures-overview")
async def index_futures_overview(refresh: bool = False):
    """Return four futures cards with spot internals and one batched futures fetch."""
    from index_constituents import get_index_snapshot
    from index_futures import get_index_futures_overview

    try:
        snapshots_list = await asyncio.to_thread(
            _load_index_overview_snapshots,
            get_index_snapshot,
            refresh,
        )
        snapshots = {
            snapshot["index"]["code"]: snapshot for snapshot in snapshots_list
        }
        spot_quotes = {
            index_code: snapshot.get("index_quote")
            for index_code, snapshot in snapshots.items()
        }
        futures = await asyncio.to_thread(
            get_index_futures_overview,
            spot_quotes,
        )
    except Exception as exc:
        logger.exception("四大期指总览获取失败")
        raise HTTPException(503, str(exc))

    cards = []
    products = futures.get("products") or {}
    for product, index_code in _INDEX_FUTURES_OVERVIEW_CODES.items():
        snapshot = snapshots.get(index_code) or {}
        futures_product = products.get(product) or {}
        monitor = _build_index_futures_monitor(snapshot, futures_product)
        cards.append({
            "contract": product,
            "index_code": index_code,
            "index_name": (snapshot.get("index") or {}).get("name"),
            "spot": snapshot.get("index_quote"),
            "futures": futures_product.get("quote"),
            "futures_overview": futures_product,
            "monitor": monitor,
            "quality": monitor["quality"],
            "replication": snapshot.get("replication"),
            "breadth": snapshot.get("breadth"),
            "driver_concentration": snapshot.get("driver_concentration"),
            "intraday": snapshot.get("intraday"),
        })
    return {
        "generated_at": futures.get("generated_at"),
        "source": futures.get("source"),
        "stale": futures.get("stale"),
        "cards": cards,
        "warnings": futures.get("warnings") or [],
        "methodology": futures.get("methodology") or {},
    }


@app.get("/api/intraday-snapshot")
async def intraday_snapshot(window: int = 20, scheme: str = "sw", refresh: bool = True):
    from scan_intraday import parse_window
    try:
        window = parse_window(window)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    if scheme not in ("sw", "ths", "sw3", "citic"):
        raise HTTPException(400, "scheme must be sw, ths, sw3 or citic")
    high_path, low_path = _intraday_paths(window, scheme)
    existing = os.path.exists(high_path) and os.path.exists(low_path)
    newest_mtime = min(os.path.getmtime(high_path), os.path.getmtime(low_path)) if existing else 0
    stale = time.time() - newest_mtime > 75
    should_refresh = refresh and _session_phase() == "trading" and (stale or not existing)
    refresh_error = None
    if should_refresh and _try_begin_intraday_scan():
        if existing:
            def refresh_in_background():
                try:
                    _run_intraday_scan([window], [scheme])
                except Exception:
                    pass
            threading.Thread(target=refresh_in_background, daemon=True).start()
        else:
            try:
                await asyncio.to_thread(_run_intraday_scan, [window], [scheme])
            except Exception as exc:
                refresh_error = str(exc)
    highs = _json_file(os.path.basename(high_path), {})
    lows = _json_file(os.path.basename(low_path), {})
    if not highs or not lows:
        raise HTTPException(503, refresh_error or "盘中快照尚未生成")
    suffix = {"sw": "", "ths": "_ths", "sw3": "_sw3", "citic": "_citic"}[scheme]
    history = _json_file(f"intraday_history{suffix}.json", {}) or {}
    return {
        "window_days": window,
        "scheme": scheme,
        "highs": highs,
        "lows": lows,
        "history": history,
        # 与刷新触发同阈值(75s):75-120s 窗口内前端不再误以为数据新鲜
        "stale": stale if existing else True,
        "refreshing": bool(should_refresh),
        "refresh_error": refresh_error,
    }


@app.get("/api/heatmap-opportunities")
async def heatmap_opportunities(
    period: str = "month",
    scheme: str = "sw",
    mode: str = "daily",
    window: int = 20,
):
    """Return evidence-gated industry states; invalid data disables scoring."""
    from heatmap_opportunity import (
        SCHEME_SUFFIX,
        build_opportunity_snapshot,
        load_opportunity_snapshot,
    )

    if scheme not in SCHEME_SUFFIX:
        raise HTTPException(400, "scheme must be sw, ths or sw3")
    if mode not in ("daily", "intraday"):
        raise HTTPException(400, "mode must be daily or intraday")
    if not 5 <= window <= 250:
        raise HTTPException(400, "window must be between 5 and 250")
    try:
        if mode == "daily" and period == "custom":
            from update_engine import build_custom_heatmap_snapshot

            custom = await asyncio.to_thread(
                build_custom_heatmap_snapshot, window, scheme
            )
            suffix = SCHEME_SUFFIX[scheme]
            flow = _json_file(f"capital_flow_v2{suffix}.json", None)
            return build_opportunity_snapshot(
                custom.get("highs") or {},
                custom.get("lows") or {},
                flow,
                scheme=scheme,
                period=period,
                mode=mode,
            )
        stale = False
        if mode == "intraday":
            high_path, low_path = _intraday_paths(window, scheme)
            stale = (
                not os.path.exists(high_path)
                or not os.path.exists(low_path)
                or time.time() - min(
                    os.path.getmtime(high_path), os.path.getmtime(low_path)
                ) > 120
            )
        return await asyncio.to_thread(
            load_opportunity_snapshot,
            DATA_DIR,
            scheme=scheme,
            period=period,
            mode=mode,
            window=window,
            stale=stale,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        logger.exception("热力图机会状态计算失败")
        raise HTTPException(500, f"热力图机会状态计算失败: {exc}")


@app.get("/api/opportunity-summary")
async def opportunity_summary(
    period: str = "month",
    scheme: str = "sw3",
    mode: str = "auto",
):
    """Cross-validate independent evidence domains into auditable candidates."""
    from heatmap_opportunity import PERIODS, SCHEME_SUFFIX
    from opportunity_summary import build_opportunity_summary

    if scheme not in SCHEME_SUFFIX:
        raise HTTPException(400, "scheme must be sw, ths or sw3")
    if period not in PERIODS:
        raise HTTPException(
            400, "period must be month, 60d, 120d, 1year or alltime"
        )
    if mode not in ("auto", "daily", "intraday"):
        raise HTTPException(400, "mode must be auto, daily or intraday")
    if mode == "intraday" and period == "alltime":
        raise HTTPException(400, "alltime is only available in daily mode")

    session_info = await market_session()
    session = session_info["phase"]
    effective_mode = (
        session_info["recommended_mode"] if mode == "auto" else mode
    )
    mode_note = None
    if mode == "auto" and effective_mode == "intraday" and period == "alltime":
        effective_mode = "daily"
        mode_note = "历史周期仅使用收盘数据"
    try:
        result = await asyncio.to_thread(
            build_opportunity_summary,
            DATA_DIR,
            scheme=scheme,
            period=period,
            mode=effective_mode,
        )
        if isinstance(result, dict):
            request_meta = result.setdefault("request", {})
            request_meta.update(
                {
                    "scheme": scheme,
                    "period": period,
                    "requested_mode": mode,
                    "effective_mode": effective_mode,
                    "session": session,
                    "close_confirmed": session_info["close_confirmed"],
                    "mode_note": mode_note,
                }
            )
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        logger.exception("机会汇总计算失败")
        raise HTTPException(500, f"机会汇总计算失败: {exc}")


# ======================================================================
# 统一决策中枢：环境、变化、传导、执行、组合、提醒与概率可信度
# ======================================================================

@app.get("/api/decision-center")
async def decision_center(
    scheme: str = "sw3",
    period: str = "month",
    mode: str = "daily",
):
    """把现有证据层组织成可审计的短线决策闭环。"""
    if scheme not in ("sw", "ths", "sw3"):
        raise HTTPException(400, "行业分类仅支持 sw、ths、sw3")
    if period not in ("month", "60d", "120d", "1year", "alltime"):
        raise HTTPException(400, "period 参数无效")
    if mode not in ("daily", "intraday"):
        raise HTTPException(400, "mode 仅支持 daily、intraday")
    if mode == "intraday" and period == "alltime":
        raise HTTPException(400, "盘中模式不支持 alltime")
    try:
        from decision_intelligence import build_decision_center

        missing_info = await asyncio.to_thread(_get_missing_info)
        expected_date = missing_info.get("latest_trade_date")

        return await asyncio.to_thread(
            build_decision_center,
            data_dir,
            scheme,
            period,
            mode,
            expected_date,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("决策中枢计算失败")
        raise HTTPException(500, f"决策中枢计算失败: {exc}") from exc


@app.get("/api/decision-center/industry")
async def decision_center_industry(
    industry: str,
    scheme: str = "sw3",
    period: str = "month",
    mode: str = "daily",
):
    """返回单一行业的变化、驱动、结构、交易计划和传导关系。"""
    if scheme not in ("sw", "ths", "sw3"):
        raise HTTPException(400, "行业分类仅支持 sw、ths、sw3")
    try:
        from decision_intelligence import build_industry_decision

        missing_info = await asyncio.to_thread(_get_missing_info)
        expected_date = missing_info.get("latest_trade_date")

        return await asyncio.to_thread(
            build_industry_decision,
            data_dir,
            industry,
            scheme,
            period,
            mode,
            expected_date,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("行业决策详情计算失败")
        raise HTTPException(500, f"行业决策详情计算失败: {exc}") from exc


@app.get("/api/decision-center/portfolio")
async def decision_center_portfolio(
    industries: str = "",
    scheme: str = "sw3",
):
    """计算用户观察篮子的相关性、共享龙头与拥挤重叠。"""
    if scheme not in ("sw", "ths", "sw3"):
        raise HTTPException(400, "行业分类仅支持 sw、ths、sw3")
    selected = [item.strip() for item in industries.split(",") if item.strip()]
    if len(selected) > 20:
        raise HTTPException(400, "一次最多分析20个行业")
    try:
        from decision_intelligence import build_portfolio_risk

        return await asyncio.to_thread(
            build_portfolio_risk,
            data_dir,
            selected,
            scheme,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("组合风险计算失败")
        raise HTTPException(500, f"组合风险计算失败: {exc}") from exc


@app.get("/api/custom-heatmap")
async def custom_heatmap(window: int = 20, scheme: str = "sw"):
    if not 5 <= window <= 250:
        raise HTTPException(400, "window must be between 5 and 250")
    if scheme not in ("sw", "ths", "sw3", "citic"):
        raise HTTPException(400, "scheme must be sw, ths, sw3 or citic")
    latest = _latest_trade_date_from_file("new_highs_data_month.json") or "unknown"
    key = (window, scheme, latest)
    cached = _custom_snapshot_cache.get(key)
    if cached and time.time() - cached[0] < 1800:
        return cached[1]

    def build():
        from update_engine import build_custom_heatmap_snapshot
        with _custom_snapshot_lock:
            second = _custom_snapshot_cache.get(key)
            if second and time.time() - second[0] < 1800:
                return second[1]
            value = build_custom_heatmap_snapshot(window, scheme)
            _custom_snapshot_cache.clear()
            _custom_snapshot_cache[key] = (time.time(), value)
            return value

    try:
        return await asyncio.to_thread(build)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("自定义周期计算失败")
        raise HTTPException(500, f"自定义周期计算失败: {exc}")


# 数据刷新状态（带锁保证线程安全）
_refresh_status = {
    "running": False,
    "current_step": "",
    "success": None,
    "error": None,
    "progress": None,
    "steps": [],
    "warnings": [],
}
_refresh_lock = threading.Lock()

DATASET_LABELS = {
    "highs": "创新高",
    "lows": "创新低",
    "capital_flow": "成交动能",
    "margin_financing": "融资融券",
    "market_cap": "行业市值",
    "ai": "AI 分析",
    "standalone": "独立 HTML",
    "etf": "ETF热点候选",
}
DATASET_FILES = {
    "highs": ["new_highs_data_*.json", "new_highs_details_*.json"],
    "lows": ["new_lows_data_*.json", "new_lows_details_*.json"],
    "capital_flow": ["capital_flow.json", "capital_flow_ths.json"],
    "margin_financing": [
        "margin_financing.json", "margin_financing_ths.json", "margin_financing_sw3.json",
    ],
    "market_cap": [
        "market_cap.json", "market_cap_ths.json", "market_cap_sw3.json",
        "market_cap_v2.json", "market_cap_v2_ths.json",
        "market_cap_v2_sw3.json",
        "market_cap_share_history_cninfo.json",
        "market_cap_point_in_time_shares.json",
    ],
    "ai": ["ai_report_latest.json"],
    "standalone": ["industry-heatmap-standalone.html"],
    "etf": ["etf_recommend_sw3.json"],
}
DEFAULT_UPDATE_CONFIG = {
    "selected_datasets": [
        "highs", "lows", "capital_flow", "margin_financing", "etf", "market_cap",
    ],
    "update_mode": "incremental",
    "refresh_days": 1,
    "sources": {
        "highs": "sina_kline",
        "lows": "sina_kline",
        "capital_flow": "sina_kline_cache",
        "margin_financing": "exchange_public_data",
        "market_cap": "sina_kline_cache",
        "basics": "akshare_excel",
    },
    "frozen": {
        "highs": False,
        "lows": False,
        "capital_flow": False,
        "margin_financing": False,
        "market_cap": False,
        "ai": False,
        "standalone": False,
    },
}
SUPPORTED_SOURCES = {
    "highs": {"sina_kline", "sina_kline_cache"},
    "lows": {"sina_kline", "sina_kline_cache"},
    "capital_flow": {"sina_kline_cache"},
    "margin_financing": {"exchange_public_data"},
    "market_cap": {"sina_kline_cache"},
    "basics": {"akshare_excel"},
}


def _manifest_path():
    return os.path.join(data_dir, "update_manifest.json")


def _history_path():
    return os.path.join(data_dir, "update_history.jsonl")


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
    cfg.setdefault("selected_datasets", list(DEFAULT_UPDATE_CONFIG["selected_datasets"]))
    cfg.setdefault("update_mode", DEFAULT_UPDATE_CONFIG["update_mode"])
    cfg.setdefault("refresh_days", DEFAULT_UPDATE_CONFIG["refresh_days"])
    manifest.setdefault("datasets", {})
    return manifest


def _save_update_manifest(manifest):
    os.makedirs(data_dir, exist_ok=True)
    with _state_file_lock:
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


_trade_calendar_lock = threading.Lock()
_trade_calendar_cache = {"loaded_at": 0.0, "dates": []}
_missing_info_lock = threading.Lock()
_missing_info_cache = {"loaded_at": 0.0, "value": None}


def _get_trade_calendar():
    """获取并缓存交易日历，网络异常时优先使用上次成功结果。"""
    with _trade_calendar_lock:
        cached = _trade_calendar_cache["dates"]
        if cached and time.monotonic() - _trade_calendar_cache["loaded_at"] < 6 * 3600:
            return cached
        try:
            import akshare as ak
            import pandas as pd
            df = ak.tool_trade_date_hist_sina()
            dates = list(pd.to_datetime(df["trade_date"]).sort_values())
            if not dates:
                raise RuntimeError("交易日历为空")
            _trade_calendar_cache.update(loaded_at=time.monotonic(), dates=dates)
            return dates
        except Exception:
            if cached:
                logger.warning("交易日历刷新失败，继续使用缓存", exc_info=True)
                return cached
            raise


def _get_trade_date_args(days, now=None):
    """取最近 N 个已完成交易日；15:10 前不使用当天临时收盘。"""
    import pandas as pd
    now = now or datetime.now()
    dates = _get_trade_calendar()
    today = pd.Timestamp(now.date())
    completed = [date for date in dates if date <= today]
    if completed and completed[-1] == today and (now.hour, now.minute) < (15, 10):
        completed = completed[:-1]
    if not completed:
        raise RuntimeError("没有可用的已完成交易日")
    n = max(days, 1)
    return ",".join(d.strftime("%Y%m%d") for d in completed[-n:])


def _invalidate_missing_info_cache():
    with _missing_info_lock:
        _missing_info_cache.update(loaded_at=0.0, value=None)


def _get_missing_info(force=False):
    """扫描现有数据，返回各数据集的缺失日期信息"""
    import json as _json, re as _re
    with _missing_info_lock:
        cached = _missing_info_cache["value"]
        if not force and cached is not None and time.monotonic() - _missing_info_cache["loaded_at"] < 30:
            return copy.deepcopy(cached)

    recent_dates = _get_trade_date_args(20).split(",")
    latest = recent_dates[-1] if recent_dates else None
    info = {"latest_trade_date": latest, "datasets": {}}

    checks = {
        "highs": "new_highs_data_month.json",
        "lows": "new_lows_data_month.json",
        "capital_flow": "capital_flow.json",
        "margin_financing": "margin_financing.json",
        "market_cap": "market_cap.json",
    }
    for ds, fname in checks.items():
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            info["datasets"][ds] = {"status": "missing", "last_date": None}
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            dates = data.get("dates", [])
            if not dates:
                info["datasets"][ds] = {"status": "empty", "last_date": None}
                continue
            existing = set()
            for d in dates:
                m = _re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", d.get("full_label", ""))
                if m:
                    existing.add(f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}")
            # 两融明细由沪深交易所分别发布，两所的最新日期偶尔会错开。
            # 只要本地数据已等于两所共同完整日，就不应该被通用交易日
            # 逻辑永久标记为“需更新”。
            if ds == "margin_financing":
                exchange_latest = data.get("source", {}).get("exchange_latest", {})
                common_date = exchange_latest.get("complete") or data.get("latest_date")
                published = [
                    exchange_latest.get(exchange)
                    for exchange in ("sse", "szse")
                    if exchange_latest.get(exchange)
                ]
                if (
                    common_date
                    and common_date in existing
                    and (not published or common_date == min(published))
                ):
                    info["datasets"][ds] = {
                        "status": "up_to_date",
                        "last_date": common_date,
                        "exchange_latest": exchange_latest,
                        "source_lag": bool(latest and common_date < latest),
                    }
                    continue
            if latest and latest in existing:
                info["datasets"][ds] = {"status": "up_to_date", "last_date": latest}
            else:
                missing_dates = [d for d in recent_dates if d not in existing]
                info["datasets"][ds] = {
                    "status": "needs_update",
                    "last_date": max(existing) if existing else None,
                    "missing_count": len(missing_dates),
                }
        except Exception as e:
            info["datasets"][ds] = {"status": "error", "error": str(e)}

    with _missing_info_lock:
        _missing_info_cache.update(loaded_at=time.monotonic(), value=copy.deepcopy(info))
    return info


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
        files.extend(glob.glob(os.path.join(data_dir, pattern)))
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
    fp = os.path.join(data_dir, base_name)
    backup_fp = fp + ".backup"
    details_name = base_name.replace("_data_", "_details_")
    details_fp = os.path.join(data_dir, details_name)
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
    # 构建完整脚本路径
    if script_args and not os.path.isabs(script_args[0]):
        script_args = [os.path.join(os.path.dirname(__file__), script_args[0])] + script_args[1:]
    try:
        r = subprocess.run(
            [sys.executable] + script_args,
            cwd=os.path.dirname(__file__),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_sec,
        )
        _set_refresh_status(progress=None)
        if r.returncode != 0:
            return False, f"退出码 {r.returncode}"
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
    selected = (req or {}).get("selected_datasets")
    if isinstance(selected, list):
        cfg["selected_datasets"] = [item for item in selected if item in DATASET_LABELS]
    mode = (req or {}).get("update_mode")
    if mode in ("auto", "incremental", "missing", "rebuild"):
        cfg["update_mode"] = mode
    try:
        refresh_days = int((req or {}).get("refresh_days"))
        if refresh_days in (1, 5, 10, 20):
            cfg["refresh_days"] = refresh_days
    except (TypeError, ValueError):
        pass

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
    """兼容旧按钮：只更新成交动能。"""
    return await refresh_data({"days": 1, "datasets": ["capital_flow"]})


def _resolve_refresh_datasets(req: dict, strategy: dict | None = None) -> list[str]:
    """Resolve one dataset policy for UI, menu, scheduler and startup catch-up.

    An explicitly supplied list always wins.  Callers that omit ``datasets``
    use the saved strategy instead of a second hard-coded default list.
    """
    if "datasets" in req:
        requested = req.get("datasets")
    else:
        strategy = strategy or _load_update_manifest().get("config", {})
        requested = strategy.get("selected_datasets")
        if requested is None:
            requested = list(DEFAULT_UPDATE_CONFIG["selected_datasets"])
    if not isinstance(requested, list):
        raise HTTPException(400, "datasets 必须是列表")
    if not requested:
        raise HTTPException(400, "至少选择一个数据集")
    unknown = [dataset for dataset in requested if dataset not in DATASET_LABELS]
    if unknown:
        raise HTTPException(400, "未知数据集: " + ", ".join(map(str, unknown)))
    return list(dict.fromkeys(requested))


@app.post("/api/refresh-data")
async def refresh_data(req: dict = None):
    """启动可分数据集的数据更新流水线。未传 datasets 时使用已保存策略。"""
    import threading
    req = req or {}
    strategy = _load_update_manifest().get("config", {})
    try:
        days = int(req["days"] if "days" in req else strategy.get("refresh_days", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "days 必须是 1 到 30 的整数")
    if days < 1 or days > 30:
        raise HTTPException(400, "days 必须是 1 到 30 的整数")
    datasets = _resolve_refresh_datasets(req, strategy)
    mode = req["mode"] if "mode" in req else strategy.get("update_mode", "auto")
    if mode not in ("auto", "incremental", "missing", "rebuild"):
        raise HTTPException(400, "mode 必须是 auto/incremental/missing/rebuild")
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
            "warnings": [],
        })

    def run_pipeline():
        manifest = _load_update_manifest()
        cfg = manifest.setdefault("config", {})
        frozen = cfg.setdefault("frozen", {})
        sources = cfg.setdefault("sources", {})
        steps = []
        failures = []
        warnings = []
        try:
            date_args = _get_trade_date_args(days)
            types = ["month", "60d", "120d", "1year", "alltime"]

            market_datasets = [d for d in datasets if d in ("highs", "lows") and not frozen.get(d)]

            # 使用统一更新引擎（无子进程，直接写入 SQLite）
            from update_engine import (
                update_highs_lows, update_capital_flow, update_market_cap
            )
            target_dates_list = date_args.split(",")
            heatmap_target_dates = target_dates_list
            if market_datasets and mode in ("auto", "missing") and not force_rebuild:
                db = get_db()
                missing_dates = set()
                for direction in market_datasets:
                    for period in types:
                        for scheme in ("sw", "ths", "sw3"):
                            missing_dates.update(
                                db.get_missing_dates(direction, period, scheme, target_dates_list)
                            )
                heatmap_target_dates = sorted(missing_dates)

            heatmap_done = False
            coverage = {}
            for dataset in datasets:
                label = DATASET_LABELS[dataset]
                source = sources.get(dataset) or sources.get("basics") or "default"
                if frozen.get(dataset) and not force_rebuild:
                    steps.append({"dataset": dataset, "label": label, "status": "skipped", "reason": "已冻结"})
                    continue

                try:
                    if dataset in ("highs", "lows"):
                        if not heatmap_done:
                            directions = [d for d in market_datasets if d in ("highs", "lows")]
                            if heatmap_target_dates:
                                _set_refresh_status(current_step="新高/新低 (计算中...)")
                                result = update_highs_lows(
                                    heatmap_target_dates, schemes=["sw", "ths", "sw3"], periods=types,
                                    directions=directions, force_refresh=force_rebuild,
                                )
                                coverage = result.get("coverage", {})
                            else:
                                coverage = {"ratio": 1.0}
                            heatmap_done = True
                    elif dataset == "capital_flow":
                        _set_refresh_status(current_step=f"{label} (计算中...)")
                        update_capital_flow(target_dates_list, schemes=["sw", "ths", "sw3"])
                    elif dataset == "margin_financing":
                        _set_refresh_status(current_step=f"{label} (交易所明细计算中...)")
                        from margin_financing import update_margin_financing
                        update_margin_financing(target_dates_list, schemes=["sw", "ths", "sw3"])
                    elif dataset == "market_cap":
                        _set_refresh_status(current_step=f"{label} (计算中...)")
                        update_market_cap(target_dates_list, schemes=["sw", "ths", "sw3"])
                    elif dataset == "etf":
                        _set_refresh_status(current_step=f"{label} (计算中...)")
                        # 行业推荐与动量评分各自独立，互不因对方失败而跳过
                        first_err = None
                        try:
                            from etf_recommend import update_etf_recommend
                            update_etf_recommend()
                        except Exception as e1:
                            first_err = e1
                        try:
                            _set_refresh_status(current_step="动量ETF (计算中...)")
                            from momentum_etf import update_momentum_etf
                            update_momentum_etf()
                        except Exception as e2:
                            first_err = first_err or e2
                        if first_err:
                            raise first_err
                    elif dataset == "ai":
                        __import__("export_json").export_all()
                        ok, err = _run_refresh_step(label, ["ai_analyzer.py"], 120)
                        if not ok:
                            raise RuntimeError(err)
                    elif dataset == "standalone":
                        ok, err = _run_refresh_step(label, ["generate_standalone.py"], 60)
                        if not ok:
                            raise RuntimeError(err)
                    note = ""
                    if dataset in ("highs", "lows") and heatmap_done:
                        note = (f"K线覆盖率 {coverage.get('ratio', 0):.1%}"
                                if heatmap_target_dates else "无需更新")
                    _update_dataset_success(manifest, dataset, source, date_args, mode, note=note)
                    steps.append({"dataset": dataset, "label": label, "status": "success"})
                except Exception as e:
                    err = str(e)
                    _update_dataset_failure(manifest, dataset, source, date_args, mode, err)
                    steps.append({"dataset": dataset, "label": label, "status": "failed", "error": err})
                    if dataset == "etf":
                        # ETF 推荐依赖外部行情接口，失败不阻断主流程
                        warnings.append(f"{label} 失败: {err}")
                        continue
                    # 单数据集失败不中断流水线：记录后继续，最后仍导出 JSON，
                    # 避免 SQLite 已更新而前端 JSON 永远停在旧数据。
                    failures.append(f"{label}: {err}")
                    continue

            # 导出 JSON 供前端读取
            _set_refresh_status(current_step="发布前端数据...")
            try:
                from market_temperature import update_market_temperature
                update_market_temperature()
            except Exception as e:
                print(f"市场温度计算失败(不影响主流程): {e}")
            try:
                from crowding import update_crowding
                update_crowding()
            except Exception as e:
                print(f"拥挤度计算失败(不影响主流程): {e}")
            # ETF V3 将温度与拥挤风险作为上下文；只有风险数据落盘后重建，
            # 才能保证页面显示的是同一批次信息。
            if "etf" in datasets:
                try:
                    _set_refresh_status(current_step="ETF热点候选 (同步最新风险...)")
                    from etf_recommend import build_recommendations
                    build_recommendations()
                except Exception as e:
                    print(f"ETF热点候选重建失败(不影响主流程): {e}")
                try:
                    _set_refresh_status(current_step="动量ETF (同步候选池...)")
                    from momentum_etf import update_momentum_etf
                    update_momentum_etf()
                except Exception as e:
                    print(f"动量ETF同步失败(不影响主流程): {e}")
            __import__("export_json").export_all()
            # 明细弹窗的配套产物:多周期计数与个股 PE,失败不阻断主流程
            try:
                from build_period_counts import main as build_counts
                build_counts()
            except Exception as e:
                print(f"周期计数生成失败(不影响主流程): {e}")
            try:
                from fetch_stock_pe import update_stock_pe
                update_stock_pe()
            except Exception as e:
                print(f"个股PE刷新失败(不影响主流程): {e}")
            if ("highs" in datasets or "lows" in datasets) and "ai" not in datasets:
                ok, err = _run_refresh_step("每日市场简报", ["ai_analyzer.py", "--metrics-only"], 120)
                if ok:
                    _update_dataset_success(manifest, "ai", "local_metrics", date_args, mode, note="随收盘数据自动重建")
                    steps.append({"dataset": "ai", "label": "每日市场简报", "status": "success"})
                else:
                    # 简报失败降级为 warning，不影响数据更新本身的成功状态
                    logger.warning("每日市场简报失败(降级为警告): %s", err)
                    warnings.append(f"每日市场简报失败: {err}")
                    steps.append({"dataset": "ai", "label": "每日市场简报", "status": "warning", "error": err})
            if failures:
                _set_refresh_status(
                    success=False, running=False, steps=steps, warnings=warnings,
                    current_step="完成(部分数据集失败)",
                    error="部分数据集失败: " + "; ".join(failures),
                )
            else:
                _set_refresh_status(success=True, current_step="完成", running=False,
                                    steps=steps, warnings=warnings)
        except Exception as e:
            _set_refresh_status(error=f"流水线异常: {str(e)}", success=False,
                                running=False, steps=steps, warnings=warnings)
        finally:
            _invalidate_missing_info_cache()

    threading.Thread(target=run_pipeline, daemon=True).start()
    return {"status": "started", "datasets": datasets, "mode": mode}


@app.get("/api/refresh-data/status")
async def refresh_data_status():
    with _refresh_lock:
        r = dict(_refresh_status)
    try:
        missing = await asyncio.to_thread(_get_missing_info)
    except Exception as exc:
        logger.warning("读取缺失数据状态失败: %s", exc)
        missing = {"latest_trade_date": None, "datasets": {}, "error": str(exc)}
    return {
        "running": r["running"],
        "current_step": r["current_step"],
        "success": r["success"],
        "error": r.get("error"),
        "progress": r.get("progress"),
        "steps": r.get("steps", []),
        "warnings": r.get("warnings", []),
        "manifest": _load_update_manifest(),
        "missing": missing,
        "message": "数据更新完成" if r["success"] else (r.get("error") or ""),
    }


@app.get("/api/refresh-data/check")
async def refresh_data_check():
    """检查哪些数据集需要更新（不实际运行）"""
    return await asyncio.to_thread(_get_missing_info, True)


# ======================================================================
# 数据备份 / 还原 / 清空
# ======================================================================

def _get_backup_dir():
    """读取用户设置的备份目录，默认 ~/.stock-finance/backups/"""
    cfg = _load_config()
    return cfg.get("backup_dir") or os.path.join(CONFIG_DIR, "backups")


DATA_GLOB = list(SEED_PATTERNS) + ["kline_cache.pkl", "all_klines.pkl", "update_history.jsonl"]
PRESERVED_CONFIG_FILES = {
    "momentum_etf_pool.json",
    "industry_etf_map_sw3.json",
    "industry_etf_map_ths.json",
}


def _is_backup_file(name):
    import fnmatch
    if not name or os.path.basename(name) != name:
        return False
    return name in {"data.db", "config.json", "history.db"} or any(
        fnmatch.fnmatch(name, pattern) for pattern in DATA_GLOB
    )


def _latest_backup_snapshot():
    """返回最新版本化快照；兼容旧版根目录平铺备份。"""
    root = _get_backup_dir()
    if not os.path.isdir(root):
        return None
    snapshots = []
    for name in os.listdir(root):
        candidate = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(candidate):
            continue
        if any(_is_backup_file(item) for item in os.listdir(candidate)):
            snapshots.append(candidate)
    if snapshots:
        return max(snapshots, key=lambda path: (os.path.getmtime(path), os.path.basename(path)))
    if any(_is_backup_file(name) for name in os.listdir(root)):
        return root
    return None


def _ensure_data_idle():
    with _refresh_lock:
        refreshing = bool(_refresh_status.get("running"))
    with _intraday_lock:
        scanning = bool(_intraday_status.get("running"))
    if refreshing or scanning:
        raise HTTPException(409, "数据正在更新或盘中扫描，请完成后再操作")


def _backup_data_sync():
    import glob, shutil, tempfile
    backup_root = _get_backup_dir()
    os.makedirs(backup_root, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix=".snapshot-", dir=backup_root)
    saved = []
    try:
        db_path = os.path.join(data_dir, "data.db")
        if os.path.exists(db_path):
            get_db().backup_to(os.path.join(temporary, "data.db"))
            saved.append("data.db")
        for pattern in DATA_GLOB:
            for source in glob.glob(os.path.join(data_dir, pattern)):
                if not os.path.isfile(source):
                    continue
                name = os.path.basename(source)
                shutil.copy2(source, os.path.join(temporary, name))
                saved.append(name)
        for name in ("config.json", "history.db"):
            source = os.path.join(USER_DATA_DIR, name)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(temporary, name))
                saved.append(name)

        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = os.path.join(backup_root, base)
        suffix = 1
        while os.path.exists(destination):
            destination = os.path.join(backup_root, f"{base}-{suffix}")
            suffix += 1
        os.replace(temporary, destination)
        return {"files": sorted(set(saved)), "snapshot": destination}
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _reset_db_safely():
    """关闭并丢弃全局 DB 连接；若 db 实例带实例锁则在锁内执行。"""
    import db as _db_module
    instance = getattr(_db_module, "_db", None)
    lock = getattr(instance, "lock", None) if instance is not None else None
    if lock is None:
        reset_db()
    else:
        with lock:
            reset_db()


def _restore_data_sync():
    import shutil
    backup_dir = _latest_backup_snapshot()
    if not backup_dir:
        raise FileNotFoundError("没有可还原的备份快照")
    files = [
        name for name in os.listdir(backup_dir)
        if _is_backup_file(name) and os.path.isfile(os.path.join(backup_dir, name))
    ]
    if "data.db" in files:
        _reset_db_safely()
    restored = []
    for name in files:
        source = os.path.join(backup_dir, name)
        destination_dir = USER_DATA_DIR if name in {"config.json", "history.db"} else data_dir
        os.makedirs(destination_dir, exist_ok=True)
        shutil.copy2(source, os.path.join(destination_dir, name))
        restored.append(name)
    if "config.json" in files:
        os.chmod(CONFIG_FILE, 0o600)
    if "data.db" in files:
        from export_json import export_all
        get_db()
        export_all(data_dir)
    return {"files": restored, "snapshot": backup_dir}


def _clear_data_sync():
    import glob
    _reset_db_safely()
    deleted = []
    for pattern in DATA_GLOB + ["data.db", "data.db-wal", "data.db-shm"]:
        for path in glob.glob(os.path.join(data_dir, pattern)):
            if os.path.basename(path) in PRESERVED_CONFIG_FILES:
                continue
            try:
                os.remove(path)
                deleted.append(os.path.basename(path))
            except FileNotFoundError:
                pass
    history = os.path.join(USER_DATA_DIR, "history.db")
    if os.path.exists(history):
        os.remove(history)
        deleted.append("history.db")
    return sorted(set(deleted))


@app.get("/api/backup/settings")
async def backup_settings():
    """返回备份目录设置"""
    return {"backup_dir": _load_config().get("backup_dir") or os.path.join(CONFIG_DIR, "backups")}


@app.post("/api/backup/settings")
async def backup_settings_save(req: dict, request: Request):
    """保存备份目录"""
    _require_client_token(request)
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
    """将所有数据文件写入独立的时间戳快照。"""
    _ensure_data_idle()
    result = await asyncio.to_thread(_backup_data_sync)
    return {
        "status": "ok",
        "saved": len(result["files"]),
        "files": result["files"],
        "snapshot": result["snapshot"],
    }


@app.post("/api/restore")
async def restore_data(request: Request):
    """从最近备份还原数据文件"""
    _require_client_token(request)
    _ensure_data_idle()
    if not _latest_backup_snapshot():
        raise HTTPException(404, "没有找到可还原的备份快照")
    try:
        result = await asyncio.to_thread(_restore_data_sync)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    _invalidate_missing_info_cache()
    return {
        "status": "ok",
        "restored": len(result["files"]),
        "files": result["files"],
        "snapshot": result["snapshot"],
    }


@app.post("/api/clear-data")
async def clear_data(request: Request):
    """清空所有数据文件（新高/新低/AI报告/K线缓存）"""
    _require_client_token(request)
    _ensure_data_idle()
    deleted = await asyncio.to_thread(_clear_data_sync)
    _invalidate_missing_info_cache()
    return {"status": "ok", "deleted": len(deleted), "files": deleted}


@app.get("/api/backup/status")
async def backup_status():
    """查看备份状态"""
    import time as _time
    snapshot = _latest_backup_snapshot()
    if not snapshot:
        return {"exists": False, "files": [], "time": None}
    files = sorted(
        name for name in os.listdir(snapshot)
        if _is_backup_file(name) and os.path.isfile(os.path.join(snapshot, name))
    )
    mtime = os.path.getmtime(snapshot) if files else None
    return {"exists": True, "count": len(files),
            "snapshot": snapshot,
            "time": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(mtime)) if mtime else None}


# ======================================================================
# ======================================================================
# 资金流向端点
# ======================================================================

@app.get("/api/capital-flow")
async def capital_flow(scheme: str = "sw"):
    """返回行业成交动能数据。scheme=sw(申万一级)/ths(同花顺细分)/sw3(申万三级)"""
    import json as _json
    if scheme not in ("sw", "ths", "sw3", "citic"):
        raise HTTPException(400, "scheme must be sw, ths, sw3 or citic")
    suffix = "" if scheme == "sw" else f"_{scheme}"
    candidates = (
        [f"capital_flow_v2{suffix}.json", f"capital_flow{suffix}.json"]
        if scheme != "citic"
        else [f"capital_flow{suffix}.json"]
    )
    path = next(
        (os.path.join(data_dir, filename) for filename in candidates
         if os.path.exists(os.path.join(data_dir, filename))),
        None,
    )
    if path is None:
        raise HTTPException(
            404, f"成交动能数据尚未生成: {' / '.join(candidates)}")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


# ======================================================================
# 行业市值端点
# ======================================================================

@app.get("/api/market-cap")
async def market_cap(scheme: str = "sw"):
    """返回行业市值变化数据。scheme=sw(申万一级)/ths(同花顺细分)/sw3(申万三级)"""
    import json as _json
    if scheme not in ("sw", "ths", "sw3", "citic"):
        raise HTTPException(400, "scheme must be sw, ths, sw3 or citic")
    suffix = "" if scheme == "sw" else f"_{scheme}"
    candidates = (
        [f"market_cap_v2{suffix}.json", f"market_cap{suffix}.json"]
        if scheme != "citic"
        else [f"market_cap{suffix}.json"]
    )
    path = next(
        (
            os.path.join(data_dir, filename)
            for filename in candidates
            if os.path.exists(os.path.join(data_dir, filename))
        ),
        None,
    )
    if path is None:
        raise HTTPException(
            404, f"市值数据尚未生成: {' / '.join(candidates)}")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


# ======================================================================
# 行业融资融券端点
# ======================================================================

@app.get("/api/margin-financing")
async def margin_financing(scheme: str = "sw"):
    """返回行业融资融券数据。scheme=sw/ths/sw3。"""
    import json as _json
    suffixes = {"sw": "", "ths": "_ths", "sw3": "_sw3"}
    if scheme not in suffixes:
        raise HTTPException(400, "行业分类仅支持 sw、ths、sw3")
    filename = f"margin_financing{suffixes[scheme]}.json"
    path = os.path.join(data_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "融资融券数据尚未生成，请先在设置中更新")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


# ======================================================================
# 大盘冷热(市场温度)端点
# ======================================================================

def _market_temperature_quality(history):
    """读取温度数据源时间戳（含阻塞 SQLite 读，须放到工作线程执行）。"""
    quality = {"source_dates": {}}
    try:
        db = get_db()
        source_queries = {
            "highs_lows": (
                "SELECT MIN(latest) FROM ("
                "SELECT MAX(date) AS latest FROM daily_new_highs WHERE scheme='sw' AND is_total=1 AND period='60d' "
                "UNION ALL "
                "SELECT MAX(date) AS latest FROM daily_new_lows WHERE scheme='sw' AND is_total=1 AND period='60d'"
                ")"
            ),
            "flow": "SELECT MAX(date) FROM daily_capital_flow WHERE scheme='sw' AND is_total=1",
            "mcap": "SELECT MAX(date) FROM daily_market_cap WHERE scheme='sw' AND is_total=1",
        }
        for key, query in source_queries.items():
            row = db.conn.execute(query).fetchone()
            quality["source_dates"][key] = row[0] if row else None
        actual_updated = db._get_meta("market_temperature_updated")
        if actual_updated:
            history["updated_at"] = actual_updated
        quality["history_updated_at"] = actual_updated
    except Exception as exc:
        logger.warning("市场强度质量信息读取失败: %s", exc)
    return quality


@app.get("/api/market-temperature")
async def market_temperature():
    """历史日度温度(market_temperature.json) + 盘中实时温度(intraday_temperature.json)"""
    import json as _json
    history = None
    hist_path = os.path.join(data_dir, "market_temperature.json")
    if os.path.exists(hist_path):
        with open(hist_path, "r", encoding="utf-8") as f:
            history = _json.load(f)
    if not history or not history.get("rows"):
        raise HTTPException(404, "市场温度数据尚未生成，请先运行数据更新")
    # 交易时段内盘中温度过期(>75s)则后台触发一次盘中扫描(复用现有锁,scan 会顺带重建温度)
    intraday_path = os.path.join(data_dir, "intraday_temperature.json")
    stale = not os.path.exists(intraday_path) or time.time() - os.path.getmtime(intraday_path) > 75
    if stale and _session_phase() == "trading" and _try_begin_intraday_scan():
        def refresh_temperature_in_background():
            try:
                _run_intraday_scan([20, 60, 120, 250], ["sw", "ths", "sw3"])
            except Exception:
                pass
        threading.Thread(target=refresh_temperature_in_background, daemon=True).start()
    intraday = _json_file("intraday_temperature.json", None)
    intraday_history = _json_file("intraday_temperature_history.json", None)
    from market_temperature import DAILY_WEIGHTS, INTRADAY_WEIGHTS, INDEX_SYMBOLS
    quality = await asyncio.to_thread(_market_temperature_quality, history)
    return {
        "history": history,
        "intraday": intraday,
        "intraday_history": intraday_history,
        "weights": {"daily": DAILY_WEIGHTS, "intraday": INTRADAY_WEIGHTS, "indices": INDEX_SYMBOLS},
        "quality": quality,
        "session": _session_phase(),
        "now": datetime.now().isoformat(timespec="seconds"),
    }


# ======================================================================
# 板块情绪反转雷达
# ======================================================================

@app.get("/api/sentiment-radar")
async def sentiment_radar(scheme: str = "sw"):
    """底部反弹与顶部退潮分离的板块情绪研究雷达。"""
    if scheme not in ("sw", "ths", "sw3"):
        raise HTTPException(400, "行业分类仅支持 sw、ths、sw3")
    try:
        from sentiment_radar import build_sentiment_radar

        return await asyncio.to_thread(
            build_sentiment_radar,
            data_dir,
            scheme,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "雷达底层数据尚未生成，请先更新成交动能") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(503, f"雷达底层数据不可用: {exc}") from exc
    except Exception as exc:
        logger.exception("板块情绪雷达计算失败")
        raise HTTPException(500, f"板块情绪雷达计算失败: {exc}")


@app.get("/api/sentiment-radar/stocks")
async def sentiment_radar_stocks(
    industry: str,
    scheme: str = "sw",
    trade_date: str = "",
):
    """返回与雷达信号日对齐的完整板块成分和个股交易指标。"""
    if scheme not in ("sw", "ths", "sw3"):
        raise HTTPException(400, "行业分类仅支持 sw、ths、sw3")
    try:
        from sentiment_radar import build_sentiment_radar_stocks

        return await asyncio.to_thread(
            build_sentiment_radar_stocks,
            data_dir,
            resource_static_dir,
            scheme,
            industry,
            trade_date,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("雷达板块个股加载失败")
        raise HTTPException(500, f"雷达板块个股加载失败: {exc}") from exc


# ======================================================================
# 交易拥挤度端点
# ======================================================================

@app.get("/api/crowding")
async def crowding(scheme: str = "sw"):
    """交易拥挤度：按热力图的三套行业口径返回独立计算结果。"""
    import json as _json
    suffixes = {"sw": "", "ths": "_ths", "sw3": "_sw3"}
    if scheme not in suffixes:
        raise HTTPException(400, "行业分类仅支持 sw、ths、sw3")
    path = os.path.join(data_dir, f"crowding{suffixes[scheme]}.json")
    if not os.path.exists(path):
        raise HTTPException(
            404, f"{scheme} 拥挤度数据尚未生成，请先运行数据更新")
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


# ======================================================================
# 动量ETF（五福5.2 收盘信号版）
# ======================================================================

MOMENTUM_POOL_FILE = "momentum_etf_pool.json"
MOMENTUM_PARAM_RULES = {
    "lookback_days": (2, 250, True),
    "score_threshold_ratio": (0, 1, False),
    "holdings_num": (1, 20, True),
    "r2_threshold": (0, 1, False),
    "ma_lookback": (1, 250, True),
    "ma_threshold": (0.5, 1.5, False),
    "volume_lookback": (1, 60, True),
    "volume_threshold": (0.01, 20, False),
    "loss": (0.5, 1, False),
    "weak_ma_lookback": (2, 250, True),
    "max_weak_days": (1, 250, True),
    "min_avg_amount": (0, 10_000_000_000_000, False),
    "global_threshold_divisor": (0.01, 1_000_000_000, False),
}


def _validate_momentum_params(incoming, current):
    params = dict(current or {})
    for key, value in incoming.items():
        if key in ("score_range_min", "score_range_max"):
            continue
        rule = MOMENTUM_PARAM_RULES.get(key)
        if not rule:
            raise HTTPException(400, f"未知动量参数: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise HTTPException(400, f"{key} 必须是有限数字")
        minimum, maximum, integer_only = rule
        if value < minimum or value > maximum:
            raise HTTPException(400, f"{key} 必须在 {minimum} 到 {maximum} 之间")
        if integer_only and not float(value).is_integer():
            raise HTTPException(400, f"{key} 必须是整数")
        params[key] = int(value) if integer_only else float(value)

    current_range = params.get("score_range", [0, 5])
    minimum = incoming.get("score_range_min", current_range[0])
    maximum = incoming.get("score_range_max", current_range[1])
    for label, value in (("score_range_min", minimum), ("score_range_max", maximum)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise HTTPException(400, f"{label} 必须是有限数字")
    if minimum >= maximum:
        raise HTTPException(400, "得分下限必须小于得分上限")
    params["score_range"] = [float(minimum), float(maximum)]
    return params


@app.get("/api/momentum-etf/config")
async def get_momentum_etf_config():
    path = os.path.join(data_dir, MOMENTUM_POOL_FILE)
    if not os.path.exists(path):
        raise HTTPException(404, "动量ETF池配置不存在")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/momentum-etf/config")
async def save_momentum_etf_config(req: dict):
    path = os.path.join(data_dir, MOMENTUM_POOL_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {"version": 1, "defensive_etf": {"code": "511880", "name": "银华日利"}, "params": {}}

    for pool_key in ("global_pool", "china_pool"):
        if pool_key not in req:
            continue
        pool = req[pool_key]
        if not isinstance(pool, list):
            raise HTTPException(400, f"{pool_key} 必须是列表")
        cleaned, seen = [], set()
        for item in pool:
            code = str((item or {}).get("code", "")).strip()
            if not re.fullmatch(r"\d{6}", code):
                raise HTTPException(400, f"无效ETF代码: {code or item}")
            if code in seen:
                continue
            seen.add(code)
            cleaned.append({"code": code, "name": str((item or {}).get("name", "")).strip()})
        cfg[pool_key] = cleaned

    if isinstance(req.get("params"), dict):
        cfg["params"] = _validate_momentum_params(req["params"], cfg.get("params"))

    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=MOMENTUM_POOL_FILE + ".", suffix=".tmp", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return {"status": "ok",
            "global": len(cfg.get("global_pool", [])),
            "china": len(cfg.get("china_pool", []))}


@app.post("/api/momentum-etf/recalc")
async def recalc_momentum_etf():
    """同步重算动量ETF评分（约20-60秒）"""
    from momentum_etf import update_momentum_etf
    try:
        result = await asyncio.to_thread(update_momentum_etf)
    except Exception as e:
        raise HTTPException(500, f"动量ETF重算失败: {e}")
    target = result.get("target") or (result.get("variants", {}).get("strategy", {}).get("target"))
    return {"status": "ok",
            "is_weak": result.get("is_weak"),
            "target": f"{target['name']}({target['code']})" if target else None}


@app.post("/api/etf-backtest/recalc")
async def recalc_etf_backtest():
    """同步重算ETF推荐回测（约1-2分钟）"""
    from etf_backtest import run_backtest
    try:
        result = await asyncio.to_thread(run_backtest)
    except Exception as e:
        raise HTTPException(500, f"回测计算失败: {e}")
    return {"status": "ok",
            "industry_picks": result["industry"]["summary"]["total"],
            "momentum_picks": result["momentum"]["summary"]["total"],
            "intraday": result.get("intraday", {}).get("summary", {})}


# ======================================================================
# AI 市场分析端点
# ======================================================================

@app.get("/api/report/latest")
async def ai_report_latest():
    """返回最新的 AI 市场分析报告"""
    import json as _json
    report_path = os.path.join(data_dir, "ai_report_latest.json")
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
        messages = [{"role": "user", "content": question}]

        cfg = _load_config()
        provider = cfg.get("ai_provider", "anthropic")
        api_key = _resolve_api_key(cfg, provider)
        if not api_key:
            yield f"data: {_json.dumps({'error': '未设置 API Key，请在设置页面配置'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        try:
            if provider == "deepseek":
                from openai import OpenAI
                client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
                stream = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "system", "content": system_prompt}] + messages,
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
    return build_chat_context(data_dir)


# ======================================================================
# 运行时信息
# ======================================================================

@app.get("/api/runtime-info")
async def runtime_info():
    """给桌面端确认当前 8001 端口是否属于同一应用实例；设置页展示数据路径。"""
    return {
        "project_root": os.path.realpath(os.path.dirname(__file__)),
        "resource_static_dir": os.path.realpath(RESOURCE_STATIC_DIR),
        "data_dir": os.path.realpath(DATA_DIR),
        "user_data_dir": os.path.realpath(USER_DATA_DIR),
        "db_path": os.path.realpath(DB_PATH),
    }


# ======================================================================
# 前端
# ======================================================================

data_dir = DATA_DIR
resource_static_dir = RESOURCE_STATIC_DIR


def _safe_static_path(root: str, filename: str) -> str | None:
    safe = os.path.normpath(filename)
    if os.path.isabs(safe) or ".." in safe.split(os.sep):
        return None
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(os.path.join(root_real, safe))
    if path_real == root_real or path_real.startswith(root_real + os.sep):
        return path_real
    return None


# 页面/脚本/样式不发缓存头会被 Chromium 启发式缓存,升级后用户看到的仍是旧 UI;
# no-cache 表示每次校验,ETag/Last-Modified 仍让未变文件走 304。
_STATIC_HEADERS = {"Cache-Control": "no-cache"}


@app.get("/{filename:path}")
async def serve_static(filename: str):
    # 仅允许明确的运行时数据文件从用户数据目录公开。
    if is_runtime_data_file(filename):
        data_path = _safe_static_path(data_dir, filename)
        if data_path and os.path.isfile(data_path):
            return FileResponse(data_path, headers=_STATIC_HEADERS)
        raise HTTPException(404, "Not found")

    resource_path = _safe_static_path(resource_static_dir, filename)
    if resource_path and os.path.isfile(resource_path):
        return FileResponse(resource_path, headers=_STATIC_HEADERS)

    # 页面可省略 .html 后缀。
    if resource_path:
        html_path = resource_path + ".html"
        if os.path.isfile(html_path):
            return FileResponse(html_path, headers=_STATIC_HEADERS)

    if filename in ("", "/", "index"):
        index_path = os.path.join(resource_static_dir, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path, headers=_STATIC_HEADERS)
    raise HTTPException(404, "Not found")

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8001, reload=False)
