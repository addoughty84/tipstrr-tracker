#!/usr/bin/env python3
"""
Edge engine - shared core for the BoothCo tipster edge system.

One source of truth for "what does this edge select and how did it do".
Both the backtester (history) and the settler (forward paper-trading) call
the SAME functions here, so a backtest and a live paper record are computed
identically - no drift between them.

An EDGE is a machine-readable rule, e.g.:
  {
    "selection": {
      "pool": ["ca-bets", "shes-the-fastest", ...],   # tipster slugs, or null = all
      "consensus_min": 2,                              # >= this many distinct tipsters on the horse
      "exclude_surface": ["AW"],                       # drop these going-surfaces
      "race_code_in": ["Jumps"],                       # null, ["Jumps"], or ["Flat"]
      "odds_min": null, "odds_max": null
    },
    "staking": {"type": "level", "points": 1}
  }

Selections are at (race_id, horse_id) granularity - one bet per horse-in-race.
Only matched, settled SINGLE tips are considered (singles only by design).

Requires env SUPABASE_DB_URL (Postgres / Supabase pooler connection string).
"""
import os
import math
import statistics
import psycopg2
import psycopg2.extras


def connect():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("Missing env SUPABASE_DB_URL (Supabase pooler connection string)")
    return psycopg2.connect(url, connect_timeout=20)


def _build_sql(rule):
    sel = rule.get("selection", {}) or {}
    where = [
        "COALESCE(t.n_selections,1) <= 1",          # singles only
        "t.profit_points IS NOT NULL",              # settled
        "l.race_id IS NOT NULL",
        "l.horse_id IS NOT NULL AND l.horse_id <> ''",
        "t.posted_at >= %(dfrom)s",
        "t.posted_at <  %(dto)s",
    ]
    params = {}
    if sel.get("pool"):
        where.append("t.slug = ANY(%(pool)s)")
        params["pool"] = list(sel["pool"])
    if sel.get("exclude_surface"):
        where.append("r.surface <> ALL(%(excl)s)")
        params["excl"] = list(sel["exclude_surface"])
    if sel.get("race_code_in"):
        where.append("(CASE WHEN r.type='Flat' THEN 'Flat' ELSE 'Jumps' END) = ANY(%(codes)s)")
        params["codes"] = list(sel["race_code_in"])
    if sel.get("odds_min") is not None:
        where.append("l.advised_odds >= %(omin)s")
        params["omin"] = sel["odds_min"]
    if sel.get("odds_max") is not None:
        where.append("l.advised_odds <= %(omax)s")
        params["omax"] = sel["odds_max"]
    cmin = int(sel.get("consensus_min", 1))
    sql = f"""
    WITH base AS (
      SELECT l.race_id, l.horse_id,
        max(l.horse)                AS horse,
        count(DISTINCT t.slug)      AS n_tip,
        bool_or(t.outcome='won')    AS won,
        bool_or(t.outcome='void')   AS vd,
        avg(l.advised_odds)         AS odds,
        min(t.posted_at)            AS posted_at
      FROM tips t
        JOIN tip_legs l   ON l.tip_reference = t.reference
        JOIN ra_results r ON r.race_id = l.race_id
      WHERE {' AND '.join(where)}
      GROUP BY l.race_id, l.horse_id
    )
    SELECT race_id, horse_id, horse, n_tip, won, vd, odds, posted_at
    FROM base
    WHERE n_tip >= {cmin}
    """
    return sql, params


def selections(conn, rule, dfrom, dto):
    """Return the list of bets an edge would place between dfrom and dto."""
    sql, params = _build_sql(rule)
    params["dfrom"], params["dto"] = str(dfrom), str(dto)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    pts = float((rule.get("staking", {}) or {}).get("points", 1))
    out = []
    for r in rows:
        if r["vd"] and not r["won"]:
            outcome, profit = "void", 0.0
        elif r["won"]:
            outcome, profit = "won", (float(r["odds"]) - 1.0) * pts
        else:
            outcome, profit = "lost", -1.0 * pts
        out.append({
            "race_id": r["race_id"], "horse_id": r["horse_id"], "horse": r["horse"],
            "n_tip": r["n_tip"], "odds": float(r["odds"]) if r["odds"] else None,
            "posted_at": r["posted_at"], "stake": pts, "outcome": outcome, "profit": profit,
        })
    return out


def metrics(sels):
    """Compute performance metrics for a list of selections."""
    n = len(sels)
    if n == 0:
        return {"n": 0, "win_pct": None, "roi": None, "tstat": None,
                "profit_pts": 0.0, "max_drawdown": 0.0, "concentration": None}
    wins = sum(1 for s in sels if s["outcome"] == "won")
    staked = sum(s["stake"] for s in sels)
    profit = sum(s["profit"] for s in sels)
    rets = [s["profit"] / s["stake"] for s in sels]      # per-bet return on stake
    mean = sum(rets) / n
    sd = statistics.pstdev(rets) if n > 1 else 0.0
    tstat = (mean * math.sqrt(n) / sd) if sd > 0 else 0.0
    # equity-curve max drawdown, in chronological order
    eq = peak = mdd = 0.0
    for s in sorted(sels, key=lambda x: x["posted_at"]):
        eq += s["profit"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    maxp = max((s["profit"] for s in sels), default=0.0)
    conc = (100.0 * maxp / profit) if profit > 0 else None
    return {
        "n": n,
        "win_pct": round(100.0 * wins / n, 1),
        "roi": round(100.0 * profit / staked, 1) if staked else None,
        "tstat": round(tstat, 2),
        "profit_pts": round(profit, 1),
        "max_drawdown": round(mdd, 1),
        "concentration": round(conc, 1) if conc is not None else None,
    }


def backtest(conn, rule, dfrom, dto):
    """Convenience: selections + metrics for a date window."""
    sels = selections(conn, rule, dfrom, dto)
    return metrics(sels), sels


if __name__ == "__main__":
    # quick self-test against live data
    import json, sys
    rule = {
        "selection": {
            "pool": ["ca-bets", "shes-the-fastest", "on-target-tips", "the-profit-rocket",
                     "active-betting-hub", "model-man", "equii-tensor", "racebot-uk"],
            "consensus_min": 2, "exclude_surface": ["AW"]
        },
        "staking": {"type": "level", "points": 1}
    }
    conn = connect()
    m, _ = backtest(conn, rule, "2020-01-01", "2027-01-01")
    print(json.dumps(m, indent=2, default=str))
