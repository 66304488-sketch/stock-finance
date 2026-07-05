"""干净的 JSON 导出——不依赖 buggy db.export_to_json()"""
import json, os
from db import get_db

STATIC = os.path.join(os.path.dirname(__file__), "static")

def export_all():
    db = get_db()
    
    for direction, prefix in [("highs", "new_highs"), ("lows", "new_lows")]:
        for period in ["month", "60d", "120d", "1year", "alltime"]:
            for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
                _make_heatmap(db, direction, period, scheme, prefix, suffix)
    
    for scheme, suffix in [("sw", ""), ("ths", "_ths")]:
        _make_capital_flow(db, scheme, suffix)
        _make_market_cap(db, scheme, suffix)
    
    pass  # keep open

def _dates(db, table, n, scheme=None):
    q = f"SELECT DISTINCT date FROM {table} "
    p = []
    if scheme: q += "WHERE scheme=? "; p.append(scheme)
    q += "ORDER BY date DESC LIMIT ?"; p.append(n)
    return [r[0] for r in db.conn.execute(q, p).fetchall()]

def _label(d): return f"{int(d[4:6])}月{int(d[6:8])}日"
def _full(d): return f"{d[:4]}年{int(d[4:6])}月{int(d[6:8])}日"

def _make_heatmap(db, direction, period, scheme, prefix, suffix):
    table = "daily_new_highs" if direction == "highs" else "daily_new_lows"
    dates = _dates(db, table, 20, scheme)
    if not dates: return
    
    rows = db.conn.execute(
        f"SELECT industry, date, count, total_stocks, is_total FROM {table} "
        f"WHERE period=? AND scheme=? AND date IN ({','.join('?'*len(dates))})",
        [period, scheme] + dates).fetchall()
    
    ind_map = {}
    for ind, date, count, total, is_t in rows:
        if ind not in ind_map:
            ind_map[ind] = {"industry": ind, "total": int(total or 0),
                            "daily_counts": [0]*len(dates), "is_total": bool(is_t)}
        idx = dates.index(date)
        ind_map[ind]["daily_counts"][idx] = int(count or 0)
    
    industries = list(ind_map.values())
    for r in industries:
        if not r["is_total"]:
            t = r["total"]
            r["ratio"] = round(r["daily_counts"][0] / t * 100, 1) if t > 0 else 0.0
    
    out = {
        "dates": [{"label": _label(d), "full_label": _full(d)} for d in dates],
        "updated_at": "auto", "type": period, "industries": industries,
    }
    json.dump(out, open(os.path.join(STATIC, f"{prefix}_data_{period}{suffix}.json"), "w"),
              ensure_ascii=False, indent=2)

def _make_capital_flow(db, scheme, suffix):
    dates = _dates(db, "daily_capital_flow", 20, scheme)
    if not dates: return
    
    rows = db.conn.execute(
        f"SELECT industry, date, turnover, net_flow, stock_count, is_total FROM daily_capital_flow "
        f"WHERE scheme=? AND date IN ({','.join('?'*len(dates))})", [scheme] + dates).fetchall()
    
    ind_map = {}
    for ind, date, to, nf, sc, is_t in rows:
        if ind not in ind_map:
            ind_map[ind] = {"industry": ind, "daily_turnover": [0]*len(dates),
                            "daily_net_flow": [0]*len(dates), "cumulative_flow": [0]*len(dates),
                            "daily_stock_counts": [0]*len(dates), "is_total": bool(is_t)}
        idx = dates.index(date)
        ind_map[ind]["daily_turnover"][idx] = round(to)
        ind_map[ind]["daily_net_flow"][idx] = round(nf)
        ind_map[ind]["daily_stock_counts"][idx] = int(sc or 0)
    
    industries = list(ind_map.values())
    for r in industries:
        cum = 0
        for i in range(len(dates)):
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
        "total_turnover": total,
        "industries": industries,
    }
    json.dump(out, open(os.path.join(STATIC, f"capital_flow{suffix}.json"), "w"),
              ensure_ascii=False, indent=2)

def _make_market_cap(db, scheme, suffix):
    dates = _dates(db, "daily_market_cap", 20, scheme)
    if not dates: return
    
    rows = db.conn.execute(
        f"SELECT industry, date, mcap, stock_count, is_total FROM daily_market_cap "
        f"WHERE scheme=? AND date IN ({','.join('?'*len(dates))})", [scheme] + dates).fetchall()
    
    ind_map = {}
    for ind, date, mc, sc, is_t in rows:
        if ind not in ind_map:
            ind_map[ind] = {"industry": ind, "daily_mcap": [0]*len(dates), "is_total": bool(is_t)}
        idx = dates.index(date)
        ind_map[ind]["daily_mcap"][idx] = round(mc)
    
    industries = list(ind_map.values())
    detail_rows = db.conn.execute(
        f"SELECT industry, date, code, name, price, change_pct, mcap FROM stock_details "
        f"WHERE direction='market_cap' AND scheme=? AND date IN ({','.join('?'*len(dates))}) "
        f"ORDER BY mcap DESC",
        [scheme] + dates).fetchall()
    sbd_map = {}
    for ind, ds, code, name, price, chg, mc in detail_rows:
        label = _label(ds)
        sbd_map.setdefault(ind, {}).setdefault(label, []).append({
            "code": code, "name": name or code, "close": price or 0,
            "change_pct": chg or 0, "mcap": mc or 0,
        })
    latest_label = _label(dates[0]) if dates else ""
    for r in industries:
        r["mcap"] = r["daily_mcap"][0]
        dm = r["daily_mcap"]
        r["change_pct"] = round((dm[0] - dm[1]) / dm[1] * 100, 1) if len(dm) > 1 and dm[1] > 0 else 0.0
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
    json.dump(out, open(os.path.join(STATIC, f"market_cap{suffix}.json"), "w"),
              ensure_ascii=False, indent=2)

if __name__ == "__main__":
    export_all()
    print("✅ export done")
