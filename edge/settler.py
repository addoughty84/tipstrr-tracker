#!/usr/bin/env python3
"""
Settler - the daily heartbeat (forward paper-trading).

For every active edge (status 'paper' or 'live'):
  1. Find every selection the edge's rule makes from its freeze date onward
     (only settled single tips, so each already has a result).
  2. Upsert those into edge_bets (idempotent on edge_id+race_id+horse_id).
  3. Recompute the edge's FORWARD record (bets placed after it was frozen)
     and store it on the edge.
Then print a digest and flag edges ready to promote / consider retiring.

Run daily. Safe to re-run; it only ever overwrites a bet's own result.

Requires env SUPABASE_DB_URL.
"""
import datetime as dt
import psycopg2.extras
import engine

# promotion / retirement thresholds (forward record only)
PROMOTE_MIN_N = 150
PROMOTE_MIN_ROI = 15.0
RETIRE_MIN_N = 100
RETIRE_MAX_ROI = 0.0


def active_edges(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, name, rule, status, frozen_at FROM edges WHERE status IN ('paper','live') ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return rows


def settle_edge(conn, edge):
    """Record + settle all forward selections for one edge; return forward metrics."""
    frozen = edge["frozen_at"]
    now = dt.datetime.now(dt.timezone.utc)
    sels = engine.selections(conn, edge["rule"], frozen, now)
    cur = conn.cursor()
    if sels:
        rows = [(edge["id"], s["race_id"], s["horse_id"], s["horse"], s["posted_at"],
                 s["odds"], s["stake"], s["outcome"], s["profit"]) for s in sels]
        psycopg2.extras.execute_values(cur, """
            INSERT INTO edge_bets (edge_id, race_id, horse_id, horse, posted_at, odds, stake, outcome, profit, settled)
            VALUES %s
            ON CONFLICT (edge_id, race_id, horse_id)
            DO UPDATE SET outcome=EXCLUDED.outcome, profit=EXCLUDED.profit, odds=EXCLUDED.odds, settled=true
        """, rows, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,true)")
        conn.commit()
    cur.close()
    m = engine.metrics(sels)
    cur = conn.cursor()
    cur.execute("""
        UPDATE edges SET paper_n=%s, paper_roi=%s, paper_win=%s, paper_profit=%s,
                         paper_tstat=%s, updated_at=now()
        WHERE id=%s
    """, (m["n"], m["roi"], m["win_pct"], m["profit_pts"], m["tstat"], edge["id"]))
    conn.commit()
    cur.close()
    return m


def main():
    conn = engine.connect()
    edges = active_edges(conn)
    print(f"=== Edge settler  {dt.date.today()}  ({len(edges)} active edges) ===\n")
    promote, retire = [], []
    for e in edges:
        m = settle_edge(conn, e)
        flag = ""
        if m["n"] >= PROMOTE_MIN_N and (m["roi"] or -1) >= PROMOTE_MIN_ROI:
            flag = "  >>> READY TO REVIEW FOR LIVE"
            promote.append(e["name"])
        elif m["n"] >= RETIRE_MIN_N and (m["roi"] or 0) < RETIRE_MAX_ROI:
            flag = "  <<< consider retiring"
            retire.append(e["name"])
        roi = f"{m['roi']:+.0f}%" if m["roi"] is not None else "n/a"
        print(f"[{e['status']:5}] {e['name']}")
        print(f"         forward: {m['n']} bets | {roi} ROI | "
              f"{m['win_pct'] or 0:.0f}% win | t={m['tstat']} | maxDD {m['max_drawdown']}pts{flag}")
    print("\n--- DIGEST ---")
    print(f"Active edges: {len(edges)}")
    print(f"Ready to review for live: {', '.join(promote) if promote else 'none'}")
    print(f"Consider retiring: {', '.join(retire) if retire else 'none'}")
    conn.close()


if __name__ == "__main__":
    main()
