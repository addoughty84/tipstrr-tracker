#!/usr/bin/env python3
"""Edge engine - shared core (rule -> SQL selection -> metrics).
Requires env SUPABASE_DB_URL (Postgres / Supabase pooler connection string)."""
import os, math, statistics, psycopg2, psycopg2.extras


def connect():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("Missing env SUPABASE_DB_URL")
    return psycopg2.connect(url, connect_timeout=20)


def _build_sql(rule):
    sel = rule.get("selection", {}) or {}
    where = ["COALESCE(t.n_selections,1) <= 1", "t.profit_points IS NOT NULL",
             "l.race_id IS NOT NULL", "l.horse_id IS NOT NULL AND l.horse_id <> ''",
             "t.posted_at >= %(dfrom)s", "t.posted_at <  %(dto)s"]
    params = {}
    if sel.get("pool"):
        where.append("t.slug = ANY(%(pool)s)"); params["pool"] = list(sel["pool"])
    if sel.get("exclude_surface"):
        where.append("r.surface <> ALL(%(excl)s)"); params["excl"] = list(sel["exclude_surface"])
    if sel.get("race_code_in"):
        where.append("(CASE WHEN r.type='Flat' THEN 'Flat' ELSE 'Jumps' END) = ANY(%(codes)s)")
        params["codes"] = list(sel["race_code_in"])
    if sel.get("odds_min") is not None:
        where.append("l.advised_odds >= %(omin)s"); params["omin"] = sel["odds_min"]
    if sel.get("odds_max") is not None:
        where.append("l.advised_odds <= %(omax)s"); params["omax"] = sel["odds_max"]
    if sel.get("trainer"):
        where.append("l.trainer = %(trainer)s"); params["trainer"] = sel["trainer"]
    if sel.get("jockey"):
        where.append("l.jockey = %(jockey)s"); params["jockey"] = sel["jockey"]
    if sel.get("course"):
        where.append("r.course = %(course)s"); params["course"] = sel["course"]
    if sel.get("race_class"):
        where.append("r.class = %(rclass)s"); params["rclass"] = sel["race_class"]
    if sel.get("dow") is not None:
        where.append("EXTRACT(DOW FROM r.off_dt) = %(dow)s"); params["dow"] = sel["dow"]
    distf = "NULLIF(regexp_replace(r.dist_f,'[^0-9.]','','g'),'')::numeric"
    db = sel.get("dist_band")
    if db == "sprint":   where.append(f"{distf} < 7")
    elif db == "mile":   where.append(f"{distf} >= 7 AND {distf} < 10")
    elif db == "middle": where.append(f"{distf} >= 10 AND {distf} < 14")
    elif db == "staying":where.append(f"{distf} >= 14")
    gg = sel.get("going_group")
    if gg == "soft":
        where.append("(lower(r.going) LIKE '%%soft%%' OR lower(r.going) LIKE '%%heavy%%')")
    elif gg == "goodfirm":
        where.append("(lower(r.going) LIKE '%%firm%%' OR lower(r.going) = 'good' OR lower(r.going) = 'fast')")
    cmin = int(sel.get("consensus_min", 1))
    sql = f"""
    WITH base AS (
      SELECT l.race_id, l.horse_id, max(l.horse) AS horse,
        count(DISTINCT t.slug) AS n_tip,
        bool_or(t.outcome='won') AS won, bool_or(t.outcome='void') AS vd,
        avg(l.advised_odds) AS odds, min(t.posted_at) AS posted_at
      FROM tips t
        JOIN tip_legs l   ON l.tip_reference = t.reference
        JOIN ra_results r ON r.race_id = l.race_id
      WHERE {' AND '.join(where)}
      GROUP BY l.race_id, l.horse_id
    )
    SELECT race_id, horse_id, horse, n_tip, won, vd, odds, posted_at
    FROM base WHERE n_tip >= {cmin}
    """
    return sql, params


def selections(conn, rule, dfrom, dto):
    sql, params = _build_sql(rule)
    params["dfrom"], params["dto"] = str(dfrom), str(dto)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = cur.fetchall(); cur.close()
    pts = float((rule.get("staking", {}) or {}).get("points", 1))
    out = []
    for r in rows:
        if r["vd"] and not r["won"]:
            outcome, profit = "void", 0.0
        elif r["won"]:
            outcome, profit = "won", (float(r["odds"]) - 1.0) * pts
        else:
            outcome, profit = "lost", -1.0 * pts
        out.append({"race_id": r["race_id"], "horse_id": r["horse_id"], "horse": r["horse"],
                    "n_tip": r["n_tip"], "odds": float(r["odds"]) if r["odds"] else None,
                    "posted_at": r["posted_at"], "stake": pts, "outcome": outcome, "profit": profit})
    return out


def metrics(sels):
    n = len(sels)
    if n == 0:
        return {"n": 0, "win_pct": None, "roi": None, "tstat": None,
                "profit_pts": 0.0, "max_drawdown": 0.0, "concentration": None}
    wins = sum(1 for s in sels if s["outcome"] == "won")
    staked = sum(s["stake"] for s in sels)
    profit = sum(s["profit"] for s in sels)
    rets = [s["profit"] / s["stake"] for s in sels]
    mean = sum(rets) / n
    sd = statistics.pstdev(rets) if n > 1 else 0.0
    tstat = (mean * math.sqrt(n) / sd) if sd > 0 else 0.0
    eq = peak = mdd = 0.0
    for s in sorted(sels, key=lambda x: x["posted_at"]):
        eq += s["profit"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    maxp = max((s["profit"] for s in sels), default=0.0)
    conc = (100.0 * maxp / profit) if profit > 0 else None
    return {"n": n, "win_pct": round(100.0 * wins / n, 1),
            "roi": round(100.0 * profit / staked, 1) if staked else None,
            "tstat": round(tstat, 2), "profit_pts": round(profit, 1),
            "max_drawdown": round(mdd, 1),
            "concentration": round(conc, 1) if conc is not None else None}


def backtest(conn, rule, dfrom, dto):
    sels = selections(conn, rule, dfrom, dto)
    return metrics(sels), sels
