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
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import akshare as ak
import pandas as pd
import requests

from runtime_paths import DATA_DIR, RESOURCE_STATIC_DIR

warnings.filterwarnings("ignore")

STATIC_DIR = RESOURCE_STATIC_DIR
CACHE_FILE = os.path.join(DATA_DIR, "kline_cache.pkl")

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

class NotTradingDayError(RuntimeError):
    """新浪行情报文日期与目标日期全部不一致，目标日大概率不是交易日"""


def fetch_spot(codes, target_date_str):
    """用 hq.sinajs.cn 批量获取最新一天 OHLCV"""
    target_dt = pd.Timestamp(
        f"{target_date_str[:4]}-{target_date_str[4:6]}-{target_date_str[6:8]}"
    )
    spot_map = {}
    stale_dated = 0  # 报文自带日期有效但与目标日期不一致的行数（节假日会返回上一交易日数据）
    matched = 0
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
                    # 新浪 hq 接口的成交量字段已经是“股”，与日 K 接口口径一致。
                    volume = float(fields[8]) if fields[8] else 0.0
                    # 报文自带行情日期（字段 30，如 2026-07-24）。节假日/停牌时新浪
                    # 返回的是上一交易日数据，不能打上 target_dt 的日期戳污染缓存。
                    quote_dt = None
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[30].strip()):
                        quote_dt = pd.Timestamp(fields[30].strip())
                    if quote_dt is not None and quote_dt != target_dt:
                        stale_dated += 1
                        continue
                    change_pct = round((close_p - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0.0
                    if code and close_p > 0:
                        if quote_dt is not None:
                            matched += 1
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

    if stale_dated:
        print(f"  新浪行情: 跳过 {stale_dated} 条非 {target_date_str} 的过期行情")
    if not spot_map and stale_dated and not matched:
        raise NotTradingDayError(
            f"新浪行情日期均为历史日期，{target_date_str} 不是交易日"
        )
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
                elif not self._cache.get("volume_scale_checked"):
                    repaired = self._repair_volume_scale_anomalies()
                    self._cache["volume_scale_checked"] = True
                    if repaired:
                        self._cache["updated_at"] = datetime.now().isoformat()
                    self._save()
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
                "volume_scale_checked": True,
            }

    def _repair_volume_scale_anomalies(self):
        """Repair dates written by the old spot-volume x100 bug."""
        if not self._cache or not self._cache.get("data"):
            return []

        daily_turnover = {}
        for df in self._cache["data"].values():
            if df is None or df.empty or not {"date", "close", "volume"}.issubset(df.columns):
                continue
            for date, close, volume in df[["date", "close", "volume"]].itertuples(index=False, name=None):
                if close and volume:
                    day = pd.Timestamp(date).strftime("%Y%m%d")
                    daily_turnover[day] = daily_turnover.get(day, 0.0) + float(close) * float(volume)

        positive = sorted(value for value in daily_turnover.values() if value > 0)
        if len(positive) < 3:
            return []
        baseline = positive[len(positive) // 2]
        bad_dates = {
            day for day, value in daily_turnover.items()
            if value > baseline * 20 and 0.1 <= (value / 100) / baseline <= 10
        }
        if not bad_dates:
            return []

        for code, df in self._cache["data"].items():
            if df is None or df.empty or "volume" not in df.columns:
                continue
            days = df["date"].dt.strftime("%Y%m%d")
            mask = days.isin(bad_dates)
            if mask.any():
                repaired = df.copy()
                repaired.loc[mask, "volume"] = repaired.loc[mask, "volume"] / 100
                self._cache["data"][code] = repaired
        print(f"[cache] 已修复成交量放大日期: {', '.join(sorted(bad_dates))}")
        return sorted(bad_dates)

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
                    # No pre-window history exists, so the event calculator must
                    # compare against rows before each target inside this window.
                    high_before.pop(code, None)
                    low_before.pop(code, None)
                cached_data[code] = window.reset_index(drop=True)

        self._cache["codes"] = set(cached_data.keys())
        self._cache["updated_at"] = datetime.now().isoformat()

    def _update_existing(self, codes, target_date_str, update_live=True):
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
        is_live_target = target_dt.normalize() == pd.Timestamp.now().normalize()
        spot_candidates = [
            c for c in stale
            if c in cached_data and cached_data[c] is not None and not cached_data[c].empty
        ]
        if is_live_target and update_live:
            same_day_codes = [
                c for c in codes
                if c in cached_data and cached_data[c] is not None and not cached_data[c].empty
                and cached_data[c]["date"].max().normalize() == target_dt.normalize()
            ]
            spot_candidates = list(dict.fromkeys(spot_candidates + same_day_codes))
        if spot_candidates and update_live:
            print(f"[cache] 用新浪实时行情快速更新 {len(spot_candidates)} 只...")
            try:
                spots = fetch_spot(spot_candidates, target_date_str)
            except NotTradingDayError as e:
                # 目标日不是交易日，保留旧缓存，等下一个交易日再增量
                print(f"[cache] {e}，跳过实时行情更新")
                spots = {}
            updated = 0
            for code, row in spots.items():
                df = cached_data[code]
                if df["date"].max() > row["date"]:
                    continue
                new_row = pd.DataFrame([row])
                combined = pd.concat([df, new_row], ignore_index=True)
                cached_data[code] = (
                    combined.drop_duplicates(subset=["date"], keep="last")
                    .sort_values("date")
                    .reset_index(drop=True)
                )
                spot_updated.add(code)
                updated += 1
            print(f"[cache] 快速更新 {updated} 只")

        # 只补真正缺失的新股/缺股；对已经有缓存但今天没拉到的股票，保留旧缓存，避免全量回补卡住。
        if missing:
            print(f"[cache] 补齐 {len(missing)} 只新/缺失股票...")
            # 与 _init_full 相同下载长历史，才能正确计算窗口之前的 alltime 边界；
            # 只拉 MAX_WINDOW_DAYS 行时无法判断历史是否被截断，会丢失边界误报“创历史新高”。
            fetched = fetch_klines_sina(missing, datalen=COLD_START_DAYS)
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
                    # 没有窗口之前的历史，清除可能残留的边界
                    high_before.pop(code, None)
                    low_before.pop(code, None)
                cached_data[code] = window.reset_index(drop=True)

        skipped_stale = [c for c in stale if c not in spot_updated]
        if skipped_stale:
            print(f"[cache] 跳过 {len(skipped_stale)} 只未能实时更新的股票，保持现有缓存，等待下次增量")

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

    def ensure(self, codes, target_date_str, need_ohlcv=False, persist=True, update_live=True):
        """
        确保缓存包含目标日期的数据，返回 {code: DataFrame}
        need_ohlcv 参数保持接口一致，当前缓存始终保存 OHLCV
        """
        self._load()
        codes = list(codes)

        if self.force_refresh or not self._cache["data"]:
            self._init_full(codes)
        else:
            self._update_existing(codes, target_date_str, update_live=update_live)

        self._slide_window()
        if persist:
            self._cache["updated_at"] = datetime.now().isoformat()
            self._save()

        return {c: self._cache["data"][c] for c in codes if c in self._cache["data"]}

    def ensure_dates(self, codes, target_dates, min_coverage=0.9):
        """Ensure each requested market date has broad coverage, backfilling gaps when needed."""
        target_dates = sorted(set(target_dates or []))
        if not target_dates:
            return {}
        data = self.ensure(codes, target_dates[-1])
        total = max(len(codes), 1)
        available_dates = {
            code: set(df["date"].dt.strftime("%Y%m%d"))
            for code, df in data.items() if df is not None and not df.empty
        }
        weak_dates = []
        for date_str in target_dates:
            covered = sum(1 for dates in available_dates.values() if date_str in dates)
            if covered / total < min_coverage:
                weak_dates.append((date_str, covered / total))
        if not weak_dates:
            return data

        summary = ", ".join(f"{d}:{ratio:.0%}" for d, ratio in weak_dates[:5])
        print(f"[cache] 交易日覆盖不足 ({summary})，回补近期历史...")
        fetched = fetch_klines_sina(list(codes), datalen=MAX_WINDOW_DAYS)
        cached_data = self._cache["data"]
        for code, fresh_df in fetched.items():
            if fresh_df is None or fresh_df.empty:
                continue
            old_df = cached_data.get(code)
            if old_df is not None and not old_df.empty:
                combined = pd.concat([old_df, fresh_df], ignore_index=True)
                combined = combined.drop_duplicates(subset=["date"], keep="last").sort_values("date")
            else:
                combined = fresh_df.sort_values("date")
            cached_data[code] = combined.reset_index(drop=True)
        self._slide_window()
        self._cache["updated_at"] = datetime.now().isoformat()
        self._save()
        return {c: cached_data[c] for c in codes if c in cached_data}

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
