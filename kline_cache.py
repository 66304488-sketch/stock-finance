"""
共享 K 线缓存模块
- 统一维护股票列表、行业映射、交易日历
- 缓存最近 250 个交易日 OHLCV，满足 1year / 资金流向计算
- 同时保留 250 天窗口之前的 alltime 最高/最低 close，支持真正的 alltime 新高/新低
- 日常增量用新浪实时行情批量更新，秒级完成
"""

import json
import os
import pickle
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import akshare as ak
import pandas as pd
import requests

warnings.filterwarnings("ignore")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CACHE_FILE = os.path.join(STATIC_DIR, "kline_cache.pkl")

# 窗口要覆盖 1year (250 个交易日)，留一点余量
MAX_WINDOW_DAYS = 300
# 冷启动下载约 8 年历史，用于初始化 alltime 边界
COLD_START_DAYS = 2000

SW2021_INDUSTRY_MAP = {
    "11": "农林牧渔", "22": "基础化工", "23": "钢铁", "24": "有色金属",
    "27": "电子", "28": "汽车", "33": "家用电器", "34": "食品饮料",
    "35": "纺织服饰", "36": "轻工制造", "37": "医药生物", "41": "公用事业",
    "42": "交通运输", "43": "房地产", "45": "商贸零售", "46": "社会服务",
    "48": "银行", "49": "非银金融", "51": "综合", "61": "建筑材料",
    "62": "建筑装饰", "63": "电力设备", "64": "机械设备", "65": "国防军工",
    "71": "计算机", "72": "传媒", "73": "通信", "74": "煤炭",
    "75": "石油石化", "76": "环保", "77": "美容护理",
}


# ======================================================================
# 公共工具
# ======================================================================

def get_active_codes():
    """当前上市 A 股代码列表（不含北交所 9 开头）"""
    df = ak.stock_info_a_code_name()
    codes = [c for c in df["code"].astype(str).str.zfill(6) if not c.startswith("9")]
    return codes


def load_industry_map(active_codes=None):
    """从本地 Excel 加载申万 2021 行业映射"""
    xlsx = os.path.join(STATIC_DIR, "industry_stock_map.xlsx")
    if not os.path.exists(xlsx):
        raise FileNotFoundError(f"缺少行业映射文件: {xlsx}")
    df = pd.read_excel(xlsx, sheet_name="个股行业映射", dtype={"股票代码": str})
    df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
    if active_codes:
        active_set = set(active_codes)
        df = df[df["股票代码"].isin(active_set)]
    mapping = dict(zip(df["股票代码"], df["行业名称"]))
    return {k: (v if pd.notna(v) else "其他") for k, v in mapping.items()}


def get_trade_dates(n=20):
    """最近 N 个交易日，格式 YYYYMMDD"""
    df = ak.tool_trade_date_hist_sina()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    end = pd.Timestamp.now()
    return df[df["trade_date"] <= end].tail(n)["trade_date"].dt.strftime("%Y%m%d").tolist()


def format_date_short(date_str):
    """20250521 -> 5月21日"""
    return f"{int(date_str[4:6])}月{int(date_str[6:8])}日"


def format_date_for_query(date_str):
    """20250521 -> 2025年5月21日"""
    return f"{date_str[:4]}年{int(date_str[4:6])}月{int(date_str[6:8])}日"


def _market_prefix(code):
    return "sh" if code.startswith(("6", "9")) else "sz"


# ======================================================================
# 新浪 K 线下载
# ======================================================================

def fetch_klines_sina(codes, datalen=80, max_workers=25):
    """多线程从新浪日 K API 下载 OHLCV"""
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

    def fetch(code):
        prefix = _market_prefix(code)
        try:
            r = requests.get(
                url,
                params={"symbol": f"{prefix}{code}", "scale": 240, "ma": "no", "datalen": datalen},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
                timeout=20,
            )
            data = r.json()
            if not isinstance(data, list):
                return code, None
            rows = []
            for d in data:
                ds = d.get("day", "")
                if len(ds) != 10:
                    continue
                try:
                    rows.append({
                        "date": pd.Timestamp(ds),
                        "open": float(d["open"]),
                        "high": float(d["high"]),
                        "low": float(d["low"]),
                        "close": float(d["close"]),
                        "volume": float(d["volume"]),
                    })
                except (ValueError, KeyError):
                    continue
            if not rows:
                return code, None
            df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
            return code, df
        except Exception:
            return code, None

    result = {}
    done, t0 = 0, time.time()
    total = len(codes)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch, c): c for c in codes}
        for f in as_completed(futures):
            code, df = f.result()
            done += 1
            if df is not None and not df.empty:
                result[code] = df
            if done % 500 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                print(f"  K线: {done}/{total} ({rate:.0f}/s, 成功:{len(result)})")
    return result


# ======================================================================
# 新浪实时行情批量更新（仅最新一天）
# ======================================================================

def fetch_spot(codes, target_date_str):
    """用 hq.sinajs.cn 批量获取最新一天 OHLCV"""
    target_dt = pd.Timestamp(
        f"{target_date_str[:4]}-{target_date_str[4:6]}-{target_date_str[6:8]}"
    )
    spot_map = {}
    batch_size = 800
    total_batches = (len(codes) + batch_size - 1) // batch_size

    for batch_i in range(total_batches):
        batch = codes[batch_i * batch_size : (batch_i + 1) * batch_size]
        sina_codes = [f"{_market_prefix(c)}{c}" for c in batch]
        url = "http://hq.sinajs.cn/list=" + ",".join(sina_codes)
        try:
            resp = requests.get(
                url,
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=20,
            )
            resp.encoding = "gbk"
            for line in resp.text.strip().split("\n"):
                if not line or "=" not in line:
                    continue
                try:
                    _, data = line.split('="', 1)
                    data = data.rstrip('";')
                    fields = data.split(",")
                    if len(fields) < 33:
                        continue
                    code_str = line.split("hq_str_")[1].split("=")[0]
                    code = code_str[2:]
                    name = fields[0] if fields[0] else ""
                    open_p = float(fields[1]) if fields[1] else 0.0
                    prev_close = float(fields[2]) if fields[2] else 0.0
                    close_p = float(fields[3]) if fields[3] else 0.0
                    high_p = float(fields[4]) if fields[4] else 0.0
                    low_p = float(fields[5]) if fields[5] else 0.0
                    volume = float(fields[8]) * 100 if fields[8] else 0.0  # 手 -> 股
                    change_pct = round((close_p - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0.0
                    if code and close_p > 0:
                        spot_map[code] = {
                            "date": target_dt,
                            "name": name,
                            "open": open_p,
                            "high": high_p,
                            "low": low_p,
                            "close": close_p,
                            "volume": volume,
                            "prev_close": prev_close,
                            "change_pct": change_pct,
                        }
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            print(f"  新浪行情批次 {batch_i + 1}/{total_batches} 失败: {e}")

    return spot_map


# ======================================================================
# 缓存管理
# ======================================================================

class KlineCache:
    def __init__(self, cache_file=CACHE_FILE, force_refresh=False):
        self.cache_file = cache_file
        self.force_refresh = force_refresh
        self._cache = None

    def _load(self):
        if self._cache is not None:
            return
        if not self.force_refresh and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "rb") as f:
                    self._cache = pickle.load(f)
                if self._cache.get("version") != 2:
                    print("[cache] 缓存版本不兼容，重建")
                    self._cache = None
            except Exception as e:
                print(f"[cache] 读取缓存失败，重建: {e}")
                self._cache = None
        if self._cache is None:
            self._cache = {
                "version": 2,
                "updated_at": "",
                "codes": set(),
                "data": {},
                "alltime_high_before": {},
                "alltime_low_before": {},
            }

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        tmp = self.cache_file + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(self._cache, f)
        os.replace(tmp, self.cache_file)

    def _init_full(self, codes):
        """冷启动：下载长历史，保留窗口，初始化 alltime 边界"""
        print(f"[cache] 冷启动: 下载 {len(codes)} 只股票 {COLD_START_DAYS} 天历史...")
        cached_data = self._cache["data"]
        high_before = self._cache["alltime_high_before"]
        low_before = self._cache["alltime_low_before"]

        chunk_size = 1000
        total = len(codes)
        for start in range(0, total, chunk_size):
            chunk = codes[start : start + chunk_size]
            print(f"[cache] 批次 {start // chunk_size + 1}/{(total - 1) // chunk_size + 1} ({len(chunk)} 只)")
            fetched = fetch_klines_sina(chunk, datalen=COLD_START_DAYS)
            for code, df in fetched.items():
                if df is None or df.empty:
                    continue
                if len(df) > MAX_WINDOW_DAYS:
                    before = df.iloc[:-MAX_WINDOW_DAYS]
                    window = df.iloc[-MAX_WINDOW_DAYS:].copy()
                    high_before[code] = float(before["close"].max())
                    low_before[code] = float(before["close"].min())
                else:
                    window = df.copy()
                    # 短历史股票：将窗口内的极值设为 alltime 边界
                    # 这样 alltime 新高/新低可以在窗口内被正确检测
                    high_before[code] = float(window["close"].max())
                    low_before[code] = float(window["close"].min())
                cached_data[code] = window.reset_index(drop=True)

        self._cache["codes"] = set(cached_data.keys())
        self._cache["updated_at"] = datetime.now().isoformat()

    def _update_existing(self, codes, target_date_str):
        """增量更新：优先用实时行情补最新一天，只补真正缺失的股票。"""
        target_dt = pd.Timestamp(
            f"{target_date_str[:4]}-{target_date_str[4:6]}-{target_date_str[6:8]}"
        )
        cached_data = self._cache["data"]
        high_before = self._cache["alltime_high_before"]
        low_before = self._cache["alltime_low_before"]
        cached_codes = self._cache["codes"]

        missing = []
        stale = []
        for c in codes:
            if c not in cached_codes or c not in cached_data:
                missing.append(c)
            else:
                df = cached_data[c]
                if df is None or df.empty or df["date"].max() < target_dt:
                    stale.append(c)

        spot_updated = set()
        spot_candidates = [c for c in stale if c in cached_data and cached_data[c] is not None and not cached_data[c].empty]
        if spot_candidates:
            print(f"[cache] 用新浪实时行情快速更新 {len(spot_candidates)} 只...")
            spots = fetch_spot(spot_candidates, target_date_str)
            updated = 0
            for code, row in spots.items():
                df = cached_data[code]
                if df["date"].max() >= row["date"]:
                    continue
                new_row = pd.DataFrame([row])
                cached_data[code] = pd.concat([df, new_row], ignore_index=True)
                spot_updated.add(code)
                updated += 1
            print(f"[cache] 快速更新 {updated} 只")

        # 只补真正缺失的新股/缺股；对已经有缓存但今天没拉到的股票，保留旧缓存，避免全量回补卡住。
        if missing:
            print(f"[cache] 补齐 {len(missing)} 只新/缺失股票...")
            fetched = fetch_klines_sina(missing, datalen=MAX_WINDOW_DAYS)
            for code, df in fetched.items():
                if df is None or df.empty:
                    continue
                old_df = cached_data.get(code)
                if old_df is not None and not old_df.empty:
                    # 把即将被替换掉的旧窗口合并进 alltime 边界，避免历史极值丢失
                    old_high = float(old_df["close"].max())
                    old_low = float(old_df["close"].min())
                    high_before[code] = max(high_before.get(code, old_high), old_high)
                    low_before[code] = min(low_before.get(code, old_low), old_low)
                cached_data[code] = df
                # 新下载的短历史股票：将窗口内极值设为边界
                if code not in high_before:
                    high_before[code] = float(df["close"].max())
                    low_before[code] = float(df["close"].min())

        skipped_stale = [c for c in stale if c not in spot_updated]
        if skipped_stale:
            print(f"[cache] 跳过 {len(skipped_stale)} 只未能实时更新的股票，保持现有缓存，等待下次增量")

        # 新上市股票初始化边界
        for code in codes:
            if code in cached_data and code not in high_before:
                df = cached_data[code]
                if df is not None and not df.empty:
                    high_before[code] = float(df["close"].max())
                    low_before[code] = float(df["close"].min())

        self._cache["codes"] = set(codes)

    def _slide_window(self):
        """窗口太长时滑动，并把移出的数据合并到 alltime 边界"""
        cached_data = self._cache["data"]
        high_before = self._cache["alltime_high_before"]
        low_before = self._cache["alltime_low_before"]

        for code, df in list(cached_data.items()):
            if df is None or df.empty:
                continue
            if len(df) > MAX_WINDOW_DAYS:
                dropped = df.iloc[: len(df) - MAX_WINDOW_DAYS]
                old_high = high_before.get(code)
                old_low = low_before.get(code)
                new_high = float(dropped["close"].max())
                new_low = float(dropped["close"].min())
                high_before[code] = max(old_high, new_high) if old_high is not None else new_high
                low_before[code] = min(old_low, new_low) if old_low is not None else new_low
                cached_data[code] = df.iloc[-MAX_WINDOW_DAYS:].reset_index(drop=True)

    def ensure(self, codes, target_date_str, need_ohlcv=False):
        """
        确保缓存包含目标日期的数据，返回 {code: DataFrame}
        need_ohlcv 参数保持接口一致，当前缓存始终保存 OHLCV
        """
        self._load()
        codes = list(codes)

        if self.force_refresh or not self._cache["data"]:
            self._init_full(codes)
        else:
            self._update_existing(codes, target_date_str)

        self._slide_window()
        self._cache["updated_at"] = datetime.now().isoformat()
        self._save()

        return {c: self._cache["data"][c] for c in codes if c in self._cache["data"]}

    @property
    def alltime_high_before(self):
        self._load()
        return self._cache.get("alltime_high_before", {})

    @property
    def alltime_low_before(self):
        self._load()
        return self._cache.get("alltime_low_before", {})


if __name__ == "__main__":
    # 简单自测
    codes = get_active_codes()
    print(f"活跃 A 股: {len(codes)} 只")
    ind_map = load_industry_map(codes)
    codes_with_ind = [c for c in codes if c in ind_map]
    print(f"有行业分类: {len(codes_with_ind)} 只")
    dates = get_trade_dates(2)
    print(f"最近交易日: {dates}")
