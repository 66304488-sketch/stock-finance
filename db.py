"""
SQLite 数据库 —— 替代 44 个 JSON 文件。
支持：增量追加 / 事务保护 / SW+THS 双分类 / 日期级查询
"""

import json, os, sqlite3, time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "static", "data.db")


class StockDB:
    """行业热力图数据库"""

    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # 并发读写
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

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
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.commit()

    # ==================== 写入 ====================

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

    def insert_capital_flow(self, records):
        """records = [{date, scheme, industry, turnover, net_flow, stock_count, is_total}]"""
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO daily_capital_flow (date,scheme,industry,turnover,net_flow,stock_count,is_total) "
                "VALUES (:date,:scheme,:industry,:turnover,:net_flow,:stock_count,:is_total)",
                records,
            )
        self._set_meta("capital_flow_updated", datetime.now().isoformat())

    def insert_market_cap(self, records):
        """records = [{date, scheme, industry, mcap, stock_count, is_total}]"""
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO daily_market_cap (date,scheme,industry,mcap,stock_count,is_total) "
                "VALUES (:date,:scheme,:industry,:mcap,:stock_count,:is_total)",
                records,
            )
        self._set_meta("market_cap_updated", datetime.now().isoformat())

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
        dates = self._get_recent_dates(table, n_dates, scheme=scheme)
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
                r["ratio"] = round(r["daily_counts"][0] / r["total"] * 100, 1)
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
            r["turnover"] = r["daily_turnover"][0]
            r["stock_count"] = r["daily_stock_counts"][0]
            if not r["is_total"]:
                total = sum(x["daily_turnover"][0] for x in industries if not x.get("is_total"))
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
            r["mcap"] = r["daily_mcap"][0]
            r["change_pct"] = None
            if not r["is_total"]:
                total = sum(x["daily_mcap"][0] for x in industries if not x.get("is_total"))
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
        table = "daily_new_highs" if direction in ("highs", "lows") else \
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

    def import_from_json(self, static_dir=None):
        """一次性导入所有 JSON 数据到 SQLite。已存在则跳过。"""
        static_dir = static_dir or os.path.join(os.path.dirname(__file__), "static")
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
                    labels = [d["label"] for d in data.get("dates", [])]
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
            for row in data.get("industries", []):
                for i, dk in enumerate(date_keys):
                    if dk:
                        records.append({
                            "date": dk, "scheme": scheme, "industry": row["industry"],
                            "turnover": (row.get("daily_turnover", []) + [0])[i],
                            "net_flow": (row.get("daily_net_flow", []) + [0])[i],
                            "stock_count": (row.get("daily_stock_counts", []) + [0])[i],
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
            for row in data.get("industries", []):
                for i, dk in enumerate(date_keys):
                    if dk:
                        records.append({
                            "date": dk, "scheme": scheme, "industry": row["industry"],
                            "mcap": (row.get("daily_mcap", []) + [0])[i],
                            "stock_count": (row.get("daily_stock_counts", []) + [0])[i] if row.get("daily_stock_counts") else 0,
                            "is_total": 1 if row.get("is_total") else 0,
                        })
            if records:
                self.insert_market_cap(records)
                print(f"  market_cap/{scheme}: {len(records)} 条")

        self._set_meta("imported", "1")
        print(f"[db] 迁移完成 ({time.time()-t0:.1f}s)")

    # ==================== 辅助 ====================

    def _get_recent_dates(self, table, n, scheme=None):
        q = f"SELECT DISTINCT date FROM {table} "
        params = []
        if scheme:
            q += "WHERE scheme=? "
            params.append(scheme)
        q += "ORDER BY date DESC LIMIT ?"
        params.append(n)
        return [r[0] for r in self.conn.execute(q, params).fetchall()][::-1]  # 升序

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

    def close(self):
        self.conn.close()

    # ==================== 导出：SQLite → JSON ====================

    def export_to_json(self, static_dir=None):
        """将 SQLite 数据导出为 JSON 文件（兼容前端直接读取）"""
        static_dir = static_dir or os.path.join(os.path.dirname(__file__), "static")
        os.makedirs(static_dir, exist_ok=True)
        import re

        # 导出新高/新低
        for direction, prefix in [("highs", "new_highs"), ("lows", "new_lows")]:
            table = "daily_new_highs" if direction == "highs" else "daily_new_lows"
            for period in ["month", "60d", "120d", "1year", "alltime"]:
                for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
                    dates = [r[0] for r in self.conn.execute(
                        f"SELECT DISTINCT date FROM {table} WHERE period=? AND scheme=? ORDER BY date DESC LIMIT 20",
                        [period, scheme]
                    ).fetchall()][::-1]
                    if not dates:
                        continue
                    rows = self.conn.execute(
                        f"SELECT industry, date, count, total_stocks, is_total FROM {table} "
                        f"WHERE period=? AND scheme=? AND date IN ({','.join('?'*len(dates))}) "
                        f"ORDER BY is_total, count DESC",
                        [period, scheme] + dates,
                    ).fetchall()

                    ind_map = {}
                    for ind, date, count, total, is_t in rows:
                        if ind not in ind_map:
                            ind_map[ind] = {"industry": ind, "total": total or 0,
                                            "daily_counts": [0]*len(dates), "is_total": bool(is_t)}
                        idx = dates.index(date)
                        ind_map[ind]["daily_counts"][idx] = count or 0

                    industries = list(ind_map.values())
                    for r in industries:
                        if not r.get("is_total") and r.get("total", 0) > 0 and r.get("daily_counts"):
                            r["ratio"] = round(r["daily_counts"][0] / r["total"] * 100, 1)
                        elif not r.get("is_total"):
                            r["ratio"] = 0.0

                    # 日期从近到远
                    for r in industries:
                        r["daily_counts"] = list(reversed(r["daily_counts"]))
                    dates_rev = list(reversed(dates))
                    out = {
                        "dates": [{"label": StockDB._format_label(d),
                                   "full_label": StockDB._format_full(d)} for d in dates_rev],
                        "updated_at": datetime.now().isoformat(),
                        "type": period,
                        "industries": industries,
                    }
                    with open(os.path.join(static_dir, f"{prefix}_data_{period}{suffix}.json"), "w", encoding="utf-8") as f:
                        json.dump(out, f, ensure_ascii=False, indent=2)

        # 导出资金流向
        for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
            dates = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT date FROM daily_capital_flow WHERE scheme=? ORDER BY date DESC LIMIT 20",
                [scheme]).fetchall()][::-1]
            if not dates:
                continue
            rows = self.conn.execute(
                f"SELECT industry, date, turnover, net_flow, stock_count, is_total FROM daily_capital_flow "
                f"WHERE scheme=? AND date IN ({','.join('?'*len(dates))}) ORDER BY is_total, turnover DESC",
                [scheme] + dates,
            ).fetchall()

            ind_map = {}
            for ind, date, to, nf, sc, is_t in rows:
                if ind not in ind_map:
                    ind_map[ind] = {"industry": ind, "daily_turnover": [0]*len(dates),
                                    "daily_net_flow": [0]*len(dates), "cumulative_flow": [0]*len(dates),
                                    "daily_stock_counts": [0]*len(dates), "is_total": bool(is_t)}
                idx = dates.index(date)
                ind_map[ind]["daily_turnover"][idx] = round(to)
                ind_map[ind]["daily_net_flow"][idx] = round(nf)
                ind_map[ind]["daily_stock_counts"][idx] = sc or 0

            industries = list(ind_map.values())
            for r in industries:
                cum = 0
                for i in range(len(dates)):
                    cum += r["daily_net_flow"][i]
                    r["cumulative_flow"][i] = round(cum)
                r["turnover"] = r["daily_turnover"][-1]
                r["stock_count"] = r["daily_stock_counts"][-1]
            total = sum(x["turnover"] for x in industries if not x.get("is_total"))
            for r in industries:
                if not r["is_total"] and total > 0:
                    r["share"] = round(r["turnover"] / total * 100, 1)

            # 日期从近到远
            for r in industries:
                for k in ["daily_turnover", "daily_net_flow", "cumulative_flow", "daily_stock_counts"]:
                    if k in r:
                        r[k] = list(reversed(r[k]))
            dates_rev = list(reversed(dates))
            out = {
                "dates": [{"label": StockDB._format_label(d),
                           "full_label": StockDB._format_full(d)} for d in dates_rev],
                "updated_at": datetime.now().isoformat(),
                "total_turnover": sum(r["turnover"] for r in industries if r.get("is_total")) or
                                  sum(r["turnover"] for r in industries if not r.get("is_total")),
                "industries": industries,
            }
            with open(os.path.join(static_dir, f"capital_flow{suffix}.json"), "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)

        # 导出市值
        for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
            dates = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT date FROM daily_market_cap WHERE scheme=? ORDER BY date DESC LIMIT 20",
                [scheme]).fetchall()][::-1]
            if not dates:
                continue
            rows = self.conn.execute(
                f"SELECT industry, date, mcap, stock_count, is_total FROM daily_market_cap "
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
                r["daily_mcap"] = list(reversed(r["daily_mcap"]))
            dates_rev = list(reversed(dates))
            for r in industries:
                r["mcap"] = r["daily_mcap"][-1]
                r["change_pct"] = None
                r["trend_5d"] = "—"
                r["stocks"] = []
                r["stocks_by_date"] = {}
            total = sum(x["mcap"] for x in industries if not x.get("is_total"))
            for r in industries:
                if not r["is_total"] and total > 0:
                    r["share"] = round(r["mcap"] / total * 100, 1)

            out = {
                "dates": [{"label": StockDB._format_label(d),
                           "full_label": StockDB._format_full(d)} for d in dates_rev],
                "updated_at": datetime.now().isoformat(),
                "total_mcap": sum(r["mcap"] for r in industries if r.get("is_total")) or
                              sum(r["mcap"] for r in industries if not r.get("is_total")),
                "industries": industries,
            }
            with open(os.path.join(static_dir, f"market_cap{suffix}.json"), "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)

    def close(self):
        self.conn.close()


# ==================== 单例 ====================
_db = None

def get_db():
    global _db
    if _db is not None:
        try:
            _db.conn.execute("SELECT 1")
        except Exception:
            _db = None
    if _db is None:
        _db = StockDB()
    return _db
