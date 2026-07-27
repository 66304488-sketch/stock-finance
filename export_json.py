"""Export the SQLite snapshot to frontend-compatible JSON files."""
import json, os, shutil, tempfile
from datetime import datetime
from db import get_db
from runtime_paths import DATA_DIR
from index_constituents import _citic_industry

STATIC = DATA_DIR


def _source_scheme(scheme):
    """CITIC is an export-time view over the SW source records."""
    return "sw" if scheme == "citic" else scheme


def _mapped_industry(scheme, industry, is_total=False):
    if is_total or industry == "全市场合计":
        return "全市场合计"
    return _citic_industry(None, industry) if scheme == "citic" else industry


def _atomic_json_dump(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, "r", encoding="utf-8") as f:
            json.load(f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

def export_all(output_dir=None):
    db = get_db()
    output_dir = output_dir or STATIC
    os.makedirs(output_dir, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".export-", dir=output_dir)
    try:
        for direction, prefix in [("highs", "new_highs"), ("lows", "new_lows")]:
            for period in ["month", "60d", "120d", "1year", "alltime"]:
                    for scheme, suffix in [("sw", ""), ("ths", "_ths"), ("sw3", "_sw3"), ("citic", "_citic")]:
                        _make_heatmap(db, direction, period, scheme, prefix, suffix, stage)
        for scheme, suffix in [("sw", ""), ("ths", "_ths"), ("sw3", "_sw3"), ("citic", "_citic")]:
            _make_capital_flow(db, scheme, suffix, stage)
            _make_market_cap(db, scheme, suffix, stage)
            if scheme in ("sw", "ths", "sw3"):
                _make_market_cap_v2(db, scheme, suffix, stage)
        _make_market_temperature(db, stage)
        _make_crowding(db, stage)
        files = os.listdir(stage)
        for filename in files:
            with open(os.path.join(stage, filename), "r", encoding="utf-8") as handle:
                json.load(handle)
        for filename in files:
            os.replace(os.path.join(stage, filename), os.path.join(output_dir, filename))
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)

def _dates(db, table, n, scheme=None, period=None):
    q = f"SELECT DISTINCT date FROM {table} "
    clauses, p = [], []
    if scheme:
        clauses.append("scheme=?")
        p.append(scheme)
    if period:
        clauses.append("period=?")
        p.append(period)
    if clauses:
        q += "WHERE " + " AND ".join(clauses) + " "
    q += "ORDER BY date DESC LIMIT ?"; p.append(n)
    return [r[0] for r in db.conn.execute(q, p).fetchall()]

def _label(d): return f"{int(d[4:6])}月{int(d[6:8])}日"
def _full(d): return f"{d[:4]}年{int(d[4:6])}月{int(d[6:8])}日"

def _type_label(direction, period):
    labels = {
        "highs": {"month": "创20日新高", "60d": "创60日新高", "120d": "创120日新高",
                  "1year": "创一年新高", "alltime": "创收盘历史新高"},
        "lows": {"month": "创20日新低", "60d": "创60日新低", "120d": "创120日新低",
                 "1year": "创一年新低", "alltime": "创收盘近7年新低"},
    }
    return labels[direction][period]

def _make_heatmap(db, direction, period, scheme, prefix, suffix, output_dir=None):
    output_dir = output_dir or STATIC
    table = "daily_new_highs" if direction == "highs" else "daily_new_lows"
    source_scheme = _source_scheme(scheme)
    dates = _dates(db, table, 20, source_scheme, period)
    if not dates: return
    
    rows = db.conn.execute(
        f"SELECT industry, date, count, total_stocks, is_total FROM {table} "
        f"WHERE period=? AND scheme=? AND date IN ({','.join('?'*len(dates))})",
        [period, source_scheme] + dates).fetchall()
    
    ind_map = {}
    seen_totals = set()
    for ind, date, count, total, is_t in rows:
        mapped = _mapped_industry(scheme, ind, bool(is_t))
        if mapped not in ind_map:
            ind_map[mapped] = {"industry": mapped, "total": 0,
                            "daily_counts": [0]*len(dates), "is_total": bool(is_t)}
        total_key = (mapped, ind)
        if total_key not in seen_totals:
            ind_map[mapped]["total"] += int(total or 0)
            seen_totals.add(total_key)
        idx = dates.index(date)
        ind_map[mapped]["daily_counts"][idx] += int(count or 0)
    
    industries = list(ind_map.values())
    for r in industries:
        if not r["is_total"]:
            t = r["total"]
            r["ratio"] = round(r["daily_counts"][0] / t * 100, 1) if t > 0 else 0.0
    
    updated_at = db._get_meta(f"{direction}_updated") or datetime.now().isoformat()
    out = {
        "dates": [{"label": _label(d), "full_label": _full(d)} for d in dates],
        "updated_at": updated_at, "type": period,
        "type_label": _type_label(direction, period), "industries": industries,
    }
    _atomic_json_dump(out, os.path.join(output_dir, f"{prefix}_data_{period}{suffix}.json"))

    detail_rows = db.conn.execute(
        f"SELECT industry,date,code,name,price,change_pct,mcap FROM stock_details "
        f"WHERE direction=? AND period=? AND scheme=? AND date IN ({','.join('?'*len(dates))}) "
        "ORDER BY industry,date,code",
        [direction, period, source_scheme] + dates,
    ).fetchall()
    details = {}
    for industry, date, code, name, price, change_pct, mcap in detail_rows:
        mapped = _mapped_industry(scheme, industry)
        details.setdefault(mapped, {}).setdefault(_label(date), []).append({
            "code": code, "name": name or code, "price": price,
            "change_pct": change_pct or 0, "mcap": mcap,
        })
    _atomic_json_dump(details, os.path.join(output_dir, f"{prefix}_details_{period}{suffix}.json"))

def _make_capital_flow(db, scheme, suffix, output_dir=None):
    output_dir = output_dir or STATIC
    source_scheme = _source_scheme(scheme)
    dates = _dates(db, "daily_capital_flow", 20, source_scheme)
    if not dates: return
    
    rows = db.conn.execute(
        f"SELECT industry, date, turnover, net_flow, stock_count, is_total FROM daily_capital_flow "
        f"WHERE scheme=? AND date IN ({','.join('?'*len(dates))})", [source_scheme] + dates).fetchall()
    
    ind_map = {}
    for ind, date, to, nf, sc, is_t in rows:
        mapped = _mapped_industry(scheme, ind, bool(is_t))
        if mapped not in ind_map:
            ind_map[mapped] = {"industry": mapped, "daily_turnover": [0]*len(dates),
                            "daily_net_flow": [0]*len(dates), "cumulative_flow": [0]*len(dates),
                            "daily_stock_counts": [0]*len(dates), "is_total": bool(is_t)}
        idx = dates.index(date)
        ind_map[mapped]["daily_turnover"][idx] += round(to or 0)
        ind_map[mapped]["daily_net_flow"][idx] += round(nf or 0)
        ind_map[mapped]["daily_stock_counts"][idx] += int(sc or 0)
    
    industries = list(ind_map.values())
    for r in industries:
        cum = 0
        for i in range(len(dates) - 1, -1, -1):
            cum += r["daily_net_flow"][i]
            r["cumulative_flow"][i] = round(cum)
        r["turnover"] = r["daily_turnover"][0]
        r["stock_count"] = r["daily_stock_counts"][0]
    total = sum(r["turnover"] for r in industries if not r["is_total"])
    for r in industries:
        if not r["is_total"] and total > 0:
            r["share"] = round(r["turnover"] / total * 100, 1)
    
    out = {
        "dates": [{"label": _label(d), "full_label": _full(d)} for d in dates],
        "updated_at": "auto",
        "flow_method": "signed_turnover_proxy",
        "total_turnover": total,
        "industries": industries,
    }
    _atomic_json_dump(out, os.path.join(output_dir, f"capital_flow{suffix}.json"))

def _make_market_cap(db, scheme, suffix, output_dir=None):
    output_dir = output_dir or STATIC
    # The market-cap timeline consumes dates from oldest to newest.
    source_scheme = _source_scheme(scheme)
    dates = list(reversed(_dates(db, "daily_market_cap", 20, source_scheme)))
    if not dates: return
    
    rows = db.conn.execute(
        f"SELECT industry, date, mcap, stock_count, is_total FROM daily_market_cap "
        f"WHERE scheme=? AND date IN ({','.join('?'*len(dates))})", [source_scheme] + dates).fetchall()
    
    ind_map = {}
    for ind, date, mc, sc, is_t in rows:
        mapped = _mapped_industry(scheme, ind, bool(is_t))
        if mapped not in ind_map:
            ind_map[mapped] = {"industry": mapped, "daily_mcap": [0]*len(dates), "is_total": bool(is_t)}
        idx = dates.index(date)
        ind_map[mapped]["daily_mcap"][idx] += round(mc or 0)
    
    industries = list(ind_map.values())
    detail_rows = db.conn.execute(
        f"SELECT industry, date, code, name, price, change_pct, mcap FROM stock_details "
        f"WHERE direction='market_cap' AND scheme=? AND date IN ({','.join('?'*len(dates))}) "
        f"ORDER BY mcap DESC",
        [source_scheme] + dates).fetchall()
    sbd_map = {}
    for ind, ds, code, name, price, chg, mc in detail_rows:
        ind = _mapped_industry(scheme, ind)
        label = _label(ds)
        sbd_map.setdefault(ind, {}).setdefault(label, []).append({
            "code": code, "name": name or code, "close": price or 0,
            "change_pct": chg or 0, "mcap": mc or 0,
        })
    latest_label = _label(dates[-1]) if dates else ""
    for r in industries:
        r["mcap"] = r["daily_mcap"][-1]
        dm = r["daily_mcap"]
        r["change_pct"] = round((dm[-1] - dm[-2]) / dm[-2] * 100, 1) if len(dm) > 1 and dm[-2] > 0 else 0.0
        r["trend_5d"] = "—"
        r["stocks_by_date"] = sbd_map.get(r["industry"], {})
        r["stocks"] = sbd_map.get(r["industry"], {}).get(latest_label, [])[:50]
    total = sum(r["mcap"] for r in industries if not r["is_total"])
    for r in industries:
        if not r["is_total"] and total > 0:
            r["share"] = round(r["mcap"] / total * 100, 1)
    
    out = {
        "dates": [{"label": _label(d), "full_label": _full(d)} for d in dates],
        "updated_at": "auto", "total_mcap": total, "industries": industries,
    }
    _atomic_json_dump(out, os.path.join(output_dir, f"market_cap{suffix}.json"))


def _make_market_cap_v2(db, scheme, suffix, output_dir=None):
    """Export V2 only when stock details can support breadth/style metrics.

    A fresh installation initially imports legacy aggregate rows without stock
    details. Skipping here preserves the bundled V2 seed instead of replacing
    it with a low-quality aggregate-only payload during an unrelated refresh.
    """
    from market_cap_structure import build_market_cap_structure_payload

    output_dir = output_dir or STATIC
    latest = db.conn.execute(
        "SELECT MAX(date) FROM daily_market_cap "
        "WHERE scheme=? AND is_total=1",
        [scheme],
    ).fetchone()[0]
    if not latest:
        return
    total = db.conn.execute(
        "SELECT mcap FROM daily_market_cap "
        "WHERE scheme=? AND date=? AND is_total=1",
        [scheme, latest],
    ).fetchone()
    detail = db.conn.execute(
        "SELECT COALESCE(SUM(mcap),0),COUNT(*) FROM stock_details "
        "WHERE direction='market_cap' AND period='daily' "
        "AND scheme=? AND date=?",
        [scheme, latest],
    ).fetchone()
    total_value = float(total[0] or 0) if total else 0.0
    detail_value = float(detail[0] or 0) if detail else 0.0
    detail_count = int(detail[1] or 0) if detail else 0
    if (
        total_value <= 0
        or detail_count <= 0
        or detail_value / total_value < 0.90
    ):
        return
    payload = build_market_cap_structure_payload(
        db,
        scheme=scheme,
        n_dates=60,
    )
    if not payload.get("dates"):
        return
    _atomic_json_dump(
        payload,
        os.path.join(output_dir, f"market_cap_v2{suffix}.json"),
    )


def _make_market_temperature(db, output_dir=None):
    output_dir = output_dir or STATIC
    data = db.get_market_temperature(n_dates=250)
    if not data["rows"]:
        return
    from market_temperature import INDEX_SYMBOLS
    quotes = db.get_index_quotes(n_dates=250)
    data["indices"] = {
        symbol: {"name": INDEX_SYMBOLS.get(symbol, symbol), "points": points}
        for symbol, points in quotes.items() if points
    }
    _atomic_json_dump(data, os.path.join(output_dir, "market_temperature.json"))

def _make_crowding(db, output_dir=None):
    output_dir = output_dir or STATIC
    database_data = db.get_crowding(n_dates=250)
    database_market = database_data.get("market") or []
    database_date = (
        database_market[-1].get("date") if database_market else None
    )
    updated_at = db._get_meta("crowding_updated")
    for suffix in ("", "_ths", "_sw3"):
        data = None
        detail_path = os.path.join(
            DATA_DIR, f"crowding_detail{suffix}.json")
        try:
            with open(detail_path, encoding="utf-8") as handle:
                detail = json.load(handle)
            detail_market = (
                detail.get("market") if isinstance(detail, dict) else None
            )
            if (
                detail_market
                and (database_date is None
                     or detail_market[-1].get("date") == database_date)
            ):
                data = detail
        except (OSError, json.JSONDecodeError, TypeError, IndexError):
            pass
        if data is None and not suffix and database_market:
            data = dict(database_data)
        if not data or not data.get("market"):
            continue
        data["updated_at"] = updated_at or data.get("updated_at")
        _atomic_json_dump(
            data, os.path.join(output_dir, f"crowding{suffix}.json"))

if __name__ == "__main__":
    export_all()
    print("✅ export done")
