"""
SQLite 数据库 —— 替代 44 个 JSON 文件。
支持：增量追加 / 事务保护 / SW+THS 双分类 / 日期级查询
"""

import json, os, sqlite3, threading, time
from datetime import datetime, timedelta
from functools import wraps

from runtime_paths import DATA_DIR, data_path

DB_PATH = data_path("data.db")

# 热表(daily_new_highs/daily_new_lows/stock_details)保留窗口。
# 查询方最大回看：热力图/明细 20~60 个交易日，市场温度/拥挤度 250 个交易日；
# 750 个自然日(约 500+ 个交易日)留有充足余量，alltime 统计依赖 kline_cache 边界值而非这些表。
RETENTION_DAYS = 750


def _synchronized(fn):
    """多步写/迁移在实例锁内执行：check_same_thread=False 的连接被多线程共用，
    `with self.conn:` 事务跨线程交错会导致半提交。读方法不加锁
    (WAL + sqlite serialized 模式下读只见已提交状态)。"""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


class StockDB:
    """行业热力图数据库"""

    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # check_same_thread=False 的连接被多线程共用，多步写必须在锁内完成，
        # 否则 `with self.conn:` 事务跨线程交错会导致半提交
        self._lock = threading.RLock()
        self.lock = self._lock  # 暴露给 server 的 restore 路径
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # 并发读写
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @_synchronized
    def _migrate(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_new_highs (
                date TEXT NOT NULL,      -- YYYYMMDD
                period TEXT NOT NULL,    -- month/60d/120d/1year/alltime
                scheme TEXT NOT NULL,    -- sw/ths
                industry TEXT NOT NULL,
                count INTEGER NOT NULL,
                total_stocks INTEGER,
                is_total INTEGER DEFAULT 0,
                PRIMARY KEY (date, period, scheme, industry)
            );
            CREATE TABLE IF NOT EXISTS daily_new_lows (
                date TEXT NOT NULL,
                period TEXT NOT NULL,
                scheme TEXT NOT NULL,
                industry TEXT NOT NULL,
                count INTEGER NOT NULL,
                total_stocks INTEGER,
                is_total INTEGER DEFAULT 0,
                PRIMARY KEY (date, period, scheme, industry)
            );
            CREATE TABLE IF NOT EXISTS daily_capital_flow (
                date TEXT NOT NULL,
                scheme TEXT NOT NULL,
                industry TEXT NOT NULL,
                turnover REAL NOT NULL,
                net_flow REAL DEFAULT 0,
                stock_count INTEGER DEFAULT 0,
                is_total INTEGER DEFAULT 0,
                PRIMARY KEY (date, scheme, industry)
            );
            CREATE TABLE IF NOT EXISTS daily_market_cap (
                date TEXT NOT NULL,
                scheme TEXT NOT NULL,
                industry TEXT NOT NULL,
                mcap REAL NOT NULL,
                stock_count INTEGER DEFAULT 0,
                is_total INTEGER DEFAULT 0,
                PRIMARY KEY (date, scheme, industry)
            );
            CREATE TABLE IF NOT EXISTS stock_details (
                date TEXT NOT NULL,
                direction TEXT NOT NULL,  -- highs/lows
                period TEXT NOT NULL,
                scheme TEXT NOT NULL,
                industry TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                price REAL,
                change_pct REAL,
                mcap REAL,
                PRIMARY KEY (date, direction, period, scheme, industry, code)
            );
            CREATE TABLE IF NOT EXISTS daily_market_temperature (
                date TEXT PRIMARY KEY,  -- YYYYMMDD
                stocks INTEGER,
                up INTEGER, down INTEGER, flat INTEGER,
                limit_up INTEGER, limit_down INTEGER,
                big_up INTEGER, big_down INTEGER,
                amount REAL,
                highs_total INTEGER, lows_total INTEGER,
                net_flow REAL,
                mcap_change_pct REAL,
                temperature REAL
            );
            CREATE TABLE IF NOT EXISTS daily_index_quote (
                date TEXT NOT NULL,     -- YYYYMMDD
                symbol TEXT NOT NULL,   -- sh000001/sz399006/...
                close REAL NOT NULL,
                PRIMARY KEY (date, symbol)
            );
            CREATE TABLE IF NOT EXISTS daily_crowding (
                date TEXT PRIMARY KEY,  -- YYYYMMDD
                total_amount REAL,
                cr5 REAL, cr10 REAL, hhi REAL,
                top10_stock_share REAL, top50_stock_share REAL
            );
            CREATE TABLE IF NOT EXISTS daily_industry_crowding (
                date TEXT NOT NULL,
                industry TEXT NOT NULL,
                amount REAL,
                share REAL,
                PRIMARY KEY (date, industry)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.commit()
        self._repair_capital_flow_scale_anomalies()

    def _repair_capital_flow_scale_anomalies(self):
        """Repair capital-flow dates produced by the old spot-volume x100 bug."""
        schemes = [row[0] for row in self.conn.execute(
            "SELECT DISTINCT scheme FROM daily_capital_flow"
        ).fetchall()]
        repaired = []
        for scheme in schemes:
            totals = self.conn.execute(
                "SELECT date, turnover FROM daily_capital_flow "
                "WHERE scheme=? AND is_total=1 AND turnover>0 ORDER BY date",
                [scheme],
            ).fetchall()
            values = sorted(float(turnover) for _, turnover in totals)
            if len(values) < 3:
                continue
            baseline = values[len(values) // 2]
            bad_dates = [
                date for date, turnover in totals
                if float(turnover) > baseline * 20
                and 0.1 <= (float(turnover) / 100) / baseline <= 10
            ]
            if not bad_dates:
                continue
            placeholders = ",".join("?" for _ in bad_dates)
            with self.conn:
                self.conn.execute(
                    "UPDATE daily_capital_flow SET turnover=turnover/100, net_flow=net_flow/100 "
                    f"WHERE scheme=? AND date IN ({placeholders})",
                    [scheme] + bad_dates,
                )
            repaired.extend(f"{scheme}:{date}" for date in bad_dates)
        if repaired:
            print(f"[db] 已修复资金流放大日期: {', '.join(repaired)}")

    # ==================== 写入 ====================

    @_synchronized
    def insert_highs_lows(self, records, direction="highs"):
        """批量写入新高/新低。records = [{date, period, scheme, industry, count, total_stocks, is_total}]"""
        table = "daily_new_highs" if direction == "highs" else "daily_new_lows"
        with self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO {table} (date,period,scheme,industry,count,total_stocks,is_total) "
                f"VALUES (:date,:period,:scheme,:industry,:count,:total_stocks,:is_total)",
                records,
            )
        self._set_meta(f"{direction}_updated", datetime.now().isoformat())

    @_synchronized
    def replace_heatmap_slice(self, records, detail_records, direction, period, scheme, dates):
        """Atomically replace counts and details for one calculated heatmap slice."""
        table = "daily_new_highs" if direction == "highs" else "daily_new_lows"
        dates = list(dict.fromkeys(dates or []))
        if not dates:
            return
        placeholders = ",".join("?" for _ in dates)
        with self.conn:
            self.conn.execute(
                f"DELETE FROM {table} WHERE period=? AND scheme=? AND date IN ({placeholders})",
                [period, scheme] + dates,
            )
            self.conn.execute(
                f"DELETE FROM stock_details WHERE direction=? AND period=? AND scheme=? "
                f"AND date IN ({placeholders})",
                [direction, period, scheme] + dates,
            )
            if records:
                self.conn.executemany(
                    f"INSERT INTO {table} (date,period,scheme,industry,count,total_stocks,is_total) "
                    f"VALUES (:date,:period,:scheme,:industry,:count,:total_stocks,:is_total)",
                    records,
                )
            if detail_records:
                self.conn.executemany(
                    "INSERT INTO stock_details "
                    "(date,direction,period,scheme,industry,code,name,price,change_pct,mcap) "
                    "VALUES (:date,:direction,:period,:scheme,:industry,:code,:name,:price,:change_pct,:mcap)",
                    detail_records,
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                (f"{direction}_updated", datetime.now().isoformat()),
            )

    @_synchronized
    def replace_heatmap_batch(self, slices):
        """Replace multiple heatmap slices in one transaction."""
        with self.conn:
            # 热表保留窗口先删后插:若先插后删,回填早于 cutoff 的历史日期会
            # 在同一事务内被立即删除。目标日取本次写入日期与库内最大日期的较大者,
            # 回填旧日期时不会误删新数据。
            anchors = [d for item in slices for d in (item.get("dates") or [])]
            for t in ("daily_new_highs", "daily_new_lows"):
                row = self.conn.execute(f"SELECT MAX(date) FROM {t}").fetchone()
                if row and row[0]:
                    anchors.append(row[0])
            if anchors:
                cutoff = (datetime.strptime(max(anchors), "%Y%m%d")
                          - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")
                self.conn.execute("DELETE FROM daily_new_highs WHERE date < ?", [cutoff])
                self.conn.execute("DELETE FROM daily_new_lows WHERE date < ?", [cutoff])
                self.conn.execute("DELETE FROM stock_details WHERE date < ?", [cutoff])
            for item in slices:
                direction = item["direction"]
                period = item["period"]
                scheme = item["scheme"]
                dates = list(dict.fromkeys(item.get("dates") or []))
                if not dates:
                    continue
                table = "daily_new_highs" if direction == "highs" else "daily_new_lows"
                placeholders = ",".join("?" for _ in dates)
                self.conn.execute(
                    f"DELETE FROM {table} WHERE period=? AND scheme=? AND date IN ({placeholders})",
                    [period, scheme] + dates,
                )
                self.conn.execute(
                    f"DELETE FROM stock_details WHERE direction=? AND period=? AND scheme=? "
                    f"AND date IN ({placeholders})",
                    [direction, period, scheme] + dates,
                )
                if item.get("records"):
                    self.conn.executemany(
                        f"INSERT INTO {table} (date,period,scheme,industry,count,total_stocks,is_total) "
                        f"VALUES (:date,:period,:scheme,:industry,:count,:total_stocks,:is_total)",
                        item["records"],
                    )
                if item.get("detail_records"):
                    self.conn.executemany(
                        "INSERT INTO stock_details "
                        "(date,direction,period,scheme,industry,code,name,price,change_pct,mcap) "
                        "VALUES (:date,:direction,:period,:scheme,:industry,:code,:name,:price,:change_pct,:mcap)",
                        item["detail_records"],
                    )
            for direction in {item["direction"] for item in slices}:
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                    (f"{direction}_updated", datetime.now().isoformat()),
                )

    @_synchronized
    def replace_capital_flow_batch(self, records, dates_by_scheme):
        with self.conn:
            for scheme, dates in dates_by_scheme.items():
                dates = list(dict.fromkeys(dates))
                if dates:
                    placeholders = ",".join("?" for _ in dates)
                    self.conn.execute(
                        f"DELETE FROM daily_capital_flow WHERE scheme=? AND date IN ({placeholders})",
                        [scheme] + dates,
                    )
            self.conn.executemany(
                "INSERT INTO daily_capital_flow (date,scheme,industry,turnover,net_flow,stock_count,is_total) "
                "VALUES (:date,:scheme,:industry,:turnover,:net_flow,:stock_count,:is_total)",
                records,
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                ("capital_flow_updated", datetime.now().isoformat()),
            )

    @_synchronized
    def replace_market_temperature(self, records):
        """全量重建市场温度表(kline cache 是唯一数据源,重算幂等)。"""
        with self.conn:
            self.conn.execute("DELETE FROM daily_market_temperature")
            self.conn.executemany(
                "INSERT INTO daily_market_temperature "
                "(date,stocks,up,down,flat,limit_up,limit_down,big_up,big_down,"
                "amount,highs_total,lows_total,net_flow,mcap_change_pct,temperature) "
                "VALUES (:date,:stocks,:up,:down,:flat,:limit_up,:limit_down,:big_up,:big_down,"
                ":amount,:highs_total,:lows_total,:net_flow,:mcap_change_pct,:temperature)",
                records,
            )
        self._set_meta("market_temperature_updated", datetime.now().isoformat())

    def get_market_temperature(self, n_dates=250):
        """按日期升序返回最近 N 天温度数据。"""
        rows = self.conn.execute(
            "SELECT * FROM daily_market_temperature ORDER BY date DESC LIMIT ?",
            [n_dates],
        ).fetchall()
        cols = ["date", "stocks", "up", "down", "flat", "limit_up", "limit_down",
                "big_up", "big_down", "amount", "highs_total", "lows_total",
                "net_flow", "mcap_change_pct", "temperature"]
        result = [dict(zip(cols, row)) for row in reversed(rows)]
        return {
            "dates": [{"label": self._format_label(r["date"]),
                       "full_label": self._format_full(r["date"])} for r in result],
            "rows": result,
            "updated_at": self._get_meta("market_temperature_updated"),
        }

    @_synchronized
    def replace_crowding(self, market_records, industry_records):
        """全量重建拥挤度表(kline cache 是唯一数据源,重算幂等)。"""
        for record in industry_records:
            record["amount"] = round(record["amount"])
        with self.conn:
            self.conn.execute("DELETE FROM daily_crowding")
            self.conn.executemany(
                "INSERT INTO daily_crowding "
                "(date,total_amount,cr5,cr10,hhi,top10_stock_share,top50_stock_share) "
                "VALUES (:date,:total_amount,:cr5,:cr10,:hhi,:top10_stock_share,:top50_stock_share)",
                market_records,
            )
            self.conn.execute("DELETE FROM daily_industry_crowding")
            self.conn.executemany(
                "INSERT INTO daily_industry_crowding (date,industry,amount,share) "
                "VALUES (:date,:industry,:amount,:share)",
                industry_records,
            )
        self._set_meta("crowding_updated", datetime.now().isoformat())

    def get_crowding(self, n_dates=250):
        """拥挤度数据:市场集中度序列 + 各行业占比序列/最新分位数。"""
        dates = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT date FROM daily_crowding ORDER BY date DESC LIMIT ?",
            [n_dates]).fetchall()][::-1]
        if not dates:
            return {"dates": [], "market": [], "industries": []}
        placeholders = ",".join("?" * len(dates))
        market = [
            {"date": d, "total_amount": ta, "cr5": cr5, "cr10": cr10, "hhi": hhi,
             "top10_stock_share": t10, "top50_stock_share": t50}
            for d, ta, cr5, cr10, hhi, t10, t50 in self.conn.execute(
                f"SELECT date,total_amount,cr5,cr10,hhi,top10_stock_share,top50_stock_share "
                f"FROM daily_crowding WHERE date IN ({placeholders}) ORDER BY date", dates).fetchall()
        ]
        ind_map = {}
        for industry, date, amount, share in self.conn.execute(
                f"SELECT industry, date, amount, share FROM daily_industry_crowding "
                f"WHERE date IN ({placeholders})", dates).fetchall():
            entry = ind_map.setdefault(industry, {"industry": industry,
                                                  "daily_shares": [None] * len(dates),
                                                  "daily_amounts": [0] * len(dates)})
            idx = dates.index(date)
            entry["daily_shares"][idx] = round(share * 100, 3)
            entry["daily_amounts"][idx] = round(amount)
        industries = []
        for entry in ind_map.values():
            shares = [s for s in entry["daily_shares"] if s is not None]
            if not shares:
                continue
            latest = shares[-1]
            entry["share"] = latest
            entry["amount"] = next((a for s, a in zip(reversed(entry["daily_shares"]),
                                                      reversed(entry["daily_amounts"])) if s is not None), 0)
            entry["pctile"] = round(sum(1 for s in shares if s <= latest) / len(shares) * 100, 1)
            tail5 = [s for s in entry["daily_shares"][-5:] if s is not None]
            entry["avg5"] = round(sum(tail5) / len(tail5), 3) if tail5 else latest
            industries.append(entry)
        industries.sort(key=lambda x: -x["pctile"])
        return {
            "dates": [{"label": self._format_label(d), "full_label": self._format_full(d)} for d in dates],
            "market": market,
            "industries": industries,
            "updated_at": self._get_meta("crowding_updated"),
        }

    @_synchronized
    def replace_index_quotes(self, records):
        """全量重建指数日线表(数据源自带全历史,重算幂等)。"""
        with self.conn:
            self.conn.execute("DELETE FROM daily_index_quote")
            self.conn.executemany(
                "INSERT INTO daily_index_quote (date,symbol,close) VALUES (:date,:symbol,:close)",
                records,
            )

    def get_index_quotes(self, n_dates=250):
        """按符号分组返回最近 N 天指数收盘,{symbol: [{date, close}]} 按日期升序。"""
        symbols = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT symbol FROM daily_index_quote").fetchall()]
        result = {}
        for symbol in symbols:
            rows = self.conn.execute(
                "SELECT date, close FROM daily_index_quote WHERE symbol=? "
                "ORDER BY date DESC LIMIT ?", [symbol, n_dates]).fetchall()
            result[symbol] = [{"date": d, "close": c} for d, c in reversed(rows)]
        return result

    @_synchronized
    def replace_market_cap_batch(self, records, detail_records, dates_by_scheme):
        with self.conn:
            for scheme, dates in dates_by_scheme.items():
                dates = list(dict.fromkeys(dates))
                if not dates:
                    continue
                placeholders = ",".join("?" for _ in dates)
                self.conn.execute(
                    f"DELETE FROM daily_market_cap WHERE scheme=? AND date IN ({placeholders})",
                    [scheme] + dates,
                )
                self.conn.execute(
                    "DELETE FROM stock_details WHERE direction='market_cap' AND period='daily' "
                    f"AND scheme=? AND date IN ({placeholders})",
                    [scheme] + dates,
                )
            self.conn.executemany(
                "INSERT INTO daily_market_cap (date,scheme,industry,mcap,stock_count,is_total) "
                "VALUES (:date,:scheme,:industry,:mcap,:stock_count,:is_total)",
                records,
            )
            if detail_records:
                self.conn.executemany(
                    "INSERT INTO stock_details "
                    "(date,direction,period,scheme,industry,code,name,price,change_pct,mcap) "
                    "VALUES (:date,:direction,:period,:scheme,:industry,:code,:name,:price,:change_pct,:mcap)",
                    detail_records,
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                ("market_cap_updated", datetime.now().isoformat()),
            )

    @_synchronized
    def insert_capital_flow(self, records):
        """records = [{date, scheme, industry, turnover, net_flow, stock_count, is_total}]"""
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO daily_capital_flow (date,scheme,industry,turnover,net_flow,stock_count,is_total) "
                "VALUES (:date,:scheme,:industry,:turnover,:net_flow,:stock_count,:is_total)",
                records,
            )
        self._set_meta("capital_flow_updated", datetime.now().isoformat())

    @_synchronized
    def insert_market_cap(self, records):
        """records = [{date, scheme, industry, mcap, stock_count, is_total}]"""
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO daily_market_cap (date,scheme,industry,mcap,stock_count,is_total) "
                "VALUES (:date,:scheme,:industry,:mcap,:stock_count,:is_total)",
                records,
            )
        self._set_meta("market_cap_updated", datetime.now().isoformat())

    @_synchronized
    def insert_stock_details(self, records):
        """records = [{date, direction, period, scheme, industry, code, name, price, change_pct, mcap}]"""
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO stock_details (date,direction,period,scheme,industry,code,name,price,change_pct,mcap) "
                "VALUES (:date,:direction,:period,:scheme,:industry,:code,:name,:price,:change_pct,:mcap)",
                records,
            )
        self._set_meta("details_updated", datetime.now().isoformat())

    # ==================== 读取 ====================

    def get_heatmap_data(self, direction="highs", period="month", scheme="sw", n_dates=20):
        """返回与旧 JSON 兼容的热力图数据"""
        table = "daily_new_highs" if direction == "highs" else "daily_new_lows"
        dates = self._get_recent_dates(table, n_dates, scheme=scheme, period=period)
        if not dates:
            return {"dates": [], "industries": [], "updated_at": None}

        rows = self.conn.execute(
            f"SELECT industry, date, count, total_stocks, is_total FROM {table} "
            f"WHERE period=? AND scheme=? AND date IN ({','.join('?'*len(dates))}) "
            f"ORDER BY is_total, count DESC",
            [period, scheme] + dates,
        ).fetchall()

        # 构建旧格式
        industries_map = {}
        for ind, date, count, total, is_total in rows:
            if ind not in industries_map:
                industries_map[ind] = {"industry": ind, "total": total or 0,
                                        "daily_counts": [0]*len(dates), "is_total": bool(is_total)}
            idx = dates.index(date)
            if idx >= 0:
                industries_map[ind]["daily_counts"][idx] = count or 0

        industries = list(industries_map.values())
        for r in industries:
            if not r.get("is_total") and r.get("total", 0) > 0 and r.get("daily_counts"):
                r["ratio"] = round(r["daily_counts"][-1] / r["total"] * 100, 1)
            elif not r.get("is_total"):
                r["ratio"] = 0.0

        # 日期从近到远排列
        date_info = [{"label": self._format_label(d), "full_label": self._format_full(d)} for d in reversed(dates)]
        for r in industries:
            r["daily_counts"] = list(reversed(r["daily_counts"]))
        return {"dates": date_info, "industries": industries,
                "updated_at": self._get_meta(f"{direction}_updated")}

    def get_capital_flow_data(self, scheme="sw", n_dates=20):
        """返回与旧 JSON 兼容的资金流向数据"""
        dates = self._get_recent_dates("daily_capital_flow", n_dates, scheme=scheme)
        if not dates:
            return {"dates": [], "industries": [], "total_turnover": 0}

        rows = self.conn.execute(
            "SELECT industry, date, turnover, net_flow, stock_count, is_total FROM daily_capital_flow "
            f"WHERE scheme=? AND date IN ({','.join('?'*len(dates))}) ORDER BY is_total, turnover DESC",
            [scheme] + dates,
        ).fetchall()

        ind_map = {}
        for ind, date, to, nf, sc, is_t in rows:
            if ind not in ind_map:
                ind_map[ind] = {"industry": ind, "daily_turnover": [0]*len(dates),
                                "daily_net_flow": [0]*len(dates), "daily_stock_counts": [0]*len(dates),
                                "is_total": bool(is_t)}
            idx = dates.index(date)
            ind_map[ind]["daily_turnover"][idx] = round(to)
            ind_map[ind]["daily_net_flow"][idx] = round(nf)
            ind_map[ind]["daily_stock_counts"][idx] = sc or 0

        industries = list(ind_map.values())
        for r in industries:
            # dates 升序,最新值在末尾
            r["turnover"] = r["daily_turnover"][-1]
            r["stock_count"] = r["daily_stock_counts"][-1]
            if not r["is_total"]:
                total = sum(x["daily_turnover"][-1] for x in industries if not x.get("is_total"))
                r["share"] = round(r["turnover"] / max(total, 1) * 100, 1)

        date_info = [{"label": self._format_label(d), "full_label": self._format_full(d)} for d in dates]
        total_to = sum(r["turnover"] for r in industries if r.get("is_total")) or \
                   sum(r["turnover"] for r in industries if not r.get("is_total"))
        return {"dates": date_info, "industries": industries,
                "total_turnover": total_to, "updated_at": self._get_meta("capital_flow_updated")}

    def get_market_cap_data(self, scheme="sw", n_dates=20):
        """返回与旧 JSON 兼容的市值数据"""
        dates = self._get_recent_dates("daily_market_cap", n_dates, scheme=scheme)
        if not dates:
            return {"dates": [], "industries": [], "total_mcap": 0}

        rows = self.conn.execute(
            "SELECT industry, date, mcap, stock_count, is_total FROM daily_market_cap "
            f"WHERE scheme=? AND date IN ({','.join('?'*len(dates))}) ORDER BY is_total, mcap DESC",
            [scheme] + dates,
        ).fetchall()

        ind_map = {}
        for ind, date, mc, sc, is_t in rows:
            if ind not in ind_map:
                ind_map[ind] = {"industry": ind, "daily_mcap": [0]*len(dates),
                                "is_total": bool(is_t)}
            idx = dates.index(date)
            ind_map[ind]["daily_mcap"][idx] = round(mc)

        industries = list(ind_map.values())
        for r in industries:
            # dates 升序,最新值在末尾
            r["mcap"] = r["daily_mcap"][-1]
            r["change_pct"] = None
            if not r["is_total"]:
                total = sum(x["daily_mcap"][-1] for x in industries if not x.get("is_total"))
                r["share"] = round(r["mcap"] / max(total, 1) * 100, 1)

        date_info = [{"label": self._format_label(d), "full_label": self._format_full(d)} for d in dates]
        total_mc = sum(r["mcap"] for r in industries if r.get("is_total")) or \
                   sum(r["mcap"] for r in industries if not r.get("is_total"))
        return {"dates": date_info, "industries": industries,
                "total_mcap": total_mc, "updated_at": self._get_meta("market_cap_updated")}

    def get_stock_details(self, date, direction="highs", period="month", scheme="sw", industry=None):
        """获取某日期的个股明细"""
        q = "SELECT code, name, price, change_pct, mcap FROM stock_details WHERE date=? AND direction=? AND period=? AND scheme=?"
        params = [date, direction, period, scheme]
        if industry:
            q += " AND industry=?"
            params.append(industry)
        rows = self.conn.execute(q, params).fetchall()
        return [{"code": r[0], "name": r[1], "price": r[2], "change_pct": r[3], "mcap": r[4]} for r in rows]

    def get_missing_dates(self, direction="highs", period="month", scheme="sw", dates=None):
        """检查哪些日期缺失"""
        table = "daily_new_highs" if direction == "highs" else \
                "daily_new_lows" if direction == "lows" else \
                "daily_capital_flow" if direction == "capital_flow" else "daily_market_cap"
        if direction in ("highs", "lows"):
            existing = set(r[0] for r in self.conn.execute(
                f"SELECT DISTINCT date FROM {table} WHERE period=? AND scheme=?", [period, scheme]
            ).fetchall())
        else:
            existing = set(r[0] for r in self.conn.execute(
                f"SELECT DISTINCT date FROM {table} WHERE scheme=?", [scheme]
            ).fetchall())
        return [d for d in (dates or []) if d not in existing] if dates else []

    # ==================== 迁移：JSON → SQLite ====================

    @_synchronized
    def import_from_json(self, static_dir=None):
        """一次性导入所有 JSON 数据到 SQLite。已存在则跳过。"""
        static_dir = static_dir or DATA_DIR
        if self._get_meta("imported"):
            print("[db] 已导入过，跳过")
            return

        print("[db] 正在从 JSON 迁移...")
        t0 = time.time()

        # 导入新高/新低
        for direction, prefix in [("highs", "new_highs"), ("lows", "new_lows")]:
            for period in ["month", "60d", "120d", "1year", "alltime"]:
                for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
                    path = os.path.join(static_dir, f"{prefix}_data_{period}{suffix}.json")
                    if not os.path.exists(path):
                        continue
                    data = json.load(open(path, encoding="utf-8"))
                    records = []
                    date_keys = []
                    for d in data.get("dates", []):
                        m = __import__("re").match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", d.get("full_label", ""))
                        date_keys.append(f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else "")
                    for row in data.get("industries", []):
                        for i, dk in enumerate(date_keys):
                            if dk and i < len(row.get("daily_counts", [])):
                                records.append({
                                    "date": dk, "period": period, "scheme": scheme,
                                    "industry": row["industry"],
                                    "count": row["daily_counts"][i],
                                    "total_stocks": row.get("total"),
                                    "is_total": 1 if row.get("is_total") else 0,
                                })
                    if records:
                        self.insert_highs_lows(records, direction)
                        print(f"  {direction}/{period}/{scheme}: {len(records)} 条")

        # 导入资金流向
        for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
            path = os.path.join(static_dir, f"capital_flow{suffix}.json")
            if not os.path.exists(path):
                continue
            data = json.load(open(path, encoding="utf-8"))
            records = []
            date_keys = []
            for d in data.get("dates", []):
                m = __import__("re").match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", d.get("full_label", ""))
                date_keys.append(f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else "")
            n_dates = len(date_keys)
            for row in data.get("industries", []):
                # 数组可能比 dates 短，按 dates 长度补 0，避免 IndexError 中断迁移
                daily_to = (row.get("daily_turnover") or []) + [0] * n_dates
                daily_nf = (row.get("daily_net_flow") or []) + [0] * n_dates
                daily_sc = (row.get("daily_stock_counts") or []) + [0] * n_dates
                for i, dk in enumerate(date_keys):
                    if dk:
                        records.append({
                            "date": dk, "scheme": scheme, "industry": row["industry"],
                            "turnover": daily_to[i],
                            "net_flow": daily_nf[i],
                            "stock_count": daily_sc[i],
                            "is_total": 1 if row.get("is_total") else 0,
                        })
            if records:
                self.insert_capital_flow(records)
                print(f"  capital_flow/{scheme}: {len(records)} 条")

        # 导入市值
        for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
            path = os.path.join(static_dir, f"market_cap{suffix}.json")
            if not os.path.exists(path):
                continue
            data = json.load(open(path, encoding="utf-8"))
            records = []
            date_keys = []
            for d in data.get("dates", []):
                m = __import__("re").match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", d.get("full_label", ""))
                date_keys.append(f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else "")
            n_dates = len(date_keys)
            for row in data.get("industries", []):
                # 数组可能比 dates 短，按 dates 长度补 0，避免 IndexError 中断迁移
                daily_mc = (row.get("daily_mcap") or []) + [0] * n_dates
                daily_sc = (row.get("daily_stock_counts") or []) + [0] * n_dates
                for i, dk in enumerate(date_keys):
                    if dk:
                        records.append({
                            "date": dk, "scheme": scheme, "industry": row["industry"],
                            "mcap": daily_mc[i],
                            "stock_count": daily_sc[i],
                            "is_total": 1 if row.get("is_total") else 0,
                        })
            if records:
                self.insert_market_cap(records)
                print(f"  market_cap/{scheme}: {len(records)} 条")

        self._set_meta("imported", "1")
        print(f"[db] 迁移完成 ({time.time()-t0:.1f}s)")

    # ==================== 辅助 ====================

    def _get_recent_dates(self, table, n, scheme=None, period=None):
        q = f"SELECT DISTINCT date FROM {table} "
        params, clauses = [], []
        if scheme:
            clauses.append("scheme=?")
            params.append(scheme)
        if period:
            clauses.append("period=?")
            params.append(period)
        if clauses:
            q += "WHERE " + " AND ".join(clauses) + " "
        q += "ORDER BY date DESC LIMIT ?"
        params.append(n)
        return [r[0] for r in self.conn.execute(q, params).fetchall()][::-1]  # 升序

    @_synchronized
    def _set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def _get_meta(self, key):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", [key]).fetchone()
        return r[0] if r else None

    @staticmethod
    def _format_label(date_str):
        return f"{int(date_str[4:6])}月{int(date_str[6:8])}日"

    @staticmethod
    def _format_full(date_str):
        return f"{date_str[:4]}年{int(date_str[4:6])}月{int(date_str[6:8])}日"

    @_synchronized
    def close(self):
        self.conn.close()

    @_synchronized
    def backup_to(self, destination):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()

    # ==================== 导出：SQLite → JSON ====================

    def export_to_json(self, static_dir=None):
        """将 SQLite 数据导出为 JSON 文件（兼容前端直接读取）"""
        from export_json import export_all
        return export_all(static_dir or DATA_DIR)


# ==================== 单例 ====================
_db = None
_db_lock = threading.Lock()

def get_db():
    global _db
    with _db_lock:  # 防多线程并发双重创建
        if _db is not None:
            try:
                _db.conn.execute("SELECT 1")
            except Exception:
                _db = None
        if _db is None:
            _db = StockDB()
        return _db


def reset_db():
    """Close and forget the process-wide connection before replacing data.db."""
    global _db
    with _db_lock:
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass
        _db = None
