#!/usr/bin/env python3
"""
Scanner - the edge finder.

Generates candidate edges, backtests each on a TRAINING slice of history,
applies statistical gates, then CONFIRMS survivors on a HOLDOUT slice the
scanner was never allowed to discover on. Edges that pass both are registered
into the `edges` table as status 'paper' and frozen with a timestamp, so the
daily settler starts building a genuine forward record from that point.

Run weekly (or manually). Idempotent: edges are keyed by a hash of their rule,
so re-running never duplicates and never overwrites an existing edge's freeze
date or forward record.

  python scanner.py              # scan, register survivors
  python scanner.py --dry-run    # scan and print, write nothing

Requires env SUPABASE_DB_URL.
"""
import sys
import json
import hashlib
import datetime as dt
import engine

# ---- the 8-tipster stable (slugs) -----------------------------------------
POOL8 = ["ca-bets", "shes-the-fastest", "on-target-tips", "the-profit-rocket",
         "active-betting-hub", "model-man", "equii-tensor", "racebot-uk"]
SOLO = {"ca-bets": "CA BETS", "shes-the-fastest": "She's the fastest",
        "on-target-tips": "On Target Tips", "the-profit-rocket": "The Profit Rocket"}

# ---- gates -----------------------------------------------------------------
MIN_TRAIN_N = 80      # need a real sample to discover on
MIN_TRAIN_TSTAT = 2.0 # statistically distinguishable from luck
MAX_CONCENTRATION = 50  # not driven by one big winner
MIN_HOLDOUT_N = 25    # need enough out-of-sample bets to confirm
HOLDOUT_DAYS = 45     # the most-recent N days are held out for confirmation


def gen_candidates():
    """Yield (name, description, rule) candidate edges."""
    base_pool = dict(pool=POOL8)
    # consensus family over the 8-stable
    for cmin in (2, 3):
        for excl in ([], ["AW"]):
            for code in (None, ["Jumps"]):
                sel = dict(base_pool, consensus_min=cmin)
                if excl:
                    sel["exclude_surface"] = excl
                if code:
                    sel["race_code_in"] = code
                bits = [f"≥{cmin} of the 8 agree"]
                if excl:
                    bits.append("no All-Weather")
                if code:
                    bits.append("jumps only")
                name = "Consensus: " + ", ".join(bits)
                yield name, "Back any horse where " + ", ".join(bits) + ".", \
                    {"selection": sel, "staking": {"type": "level", "points": 1}}
    # solo strong tipsters, no All-Weather
    for slug, nm in SOLO.items():
        yield f"Solo: {nm} (no AW)", f"Back all {nm} singles except All-Weather.", \
            {"selection": {"pool": [slug], "consensus_min": 1, "exclude_surface": ["AW"]},
             "staking": {"type": "level", "points": 1}}


def edge_id(rule):
    h = hashlib.sha1(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:10]
    return "edge_" + h


def data_range(conn):
    cur = conn.cursor()
    cur.execute("SELECT min(posted_at)::date, max(posted_at)::date FROM tips WHERE profit_points IS NOT NULL")
    lo, hi = cur.fetchone()
    cur.close()
    return lo, hi


def register(conn, edge):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO edges (id, name, description, rule, status, frozen_at, bt_train, bt_holdout)
        VALUES (%s,%s,%s,%s,'paper',now(),%s,%s)
        ON CONFLICT (id) DO NOTHING
    """, (edge["id"], edge["name"], edge["description"], json.dumps(edge["rule"]),
          json.dumps(edge["train"]), json.dumps(edge["holdout"])))
    inserted = cur.rowcount > 0
    conn.commit()
    cur.close()
    return inserted


def main():
    dry = "--dry-run" in sys.argv
    conn = engine.connect()
    lo, hi = data_range(conn)
    split = hi - dt.timedelta(days=HOLDOUT_DAYS)
    print(f"Data {lo} -> {hi}. Train < {split} <= Holdout. dry_run={dry}\n")

    passed, registered = [], 0
    for name, desc, rule in gen_candidates():
        tr, _ = engine.backtest(conn, rule, lo, split)
        # gate on training slice
        if tr["n"] < MIN_TRAIN_N or (tr["tstat"] or 0) < MIN_TRAIN_TSTAT \
           or (tr["roi"] or -1) <= 0 \
           or (tr["concentration"] is not None and tr["concentration"] > MAX_CONCENTRATION):
            print(f"  reject (train)  {name}: n={tr['n']} roi={tr['roi']} t={tr['tstat']} conc={tr['concentration']}")
            continue
        # confirm on holdout
        ho, _ = engine.backtest(conn, rule, split, hi + dt.timedelta(days=1))
        if ho["n"] < MIN_HOLDOUT_N or (ho["roi"] or -1) <= 0:
            print(f"  FAIL holdout    {name}: train_roi={tr['roi']} | holdout n={ho['n']} roi={ho['roi']}")
            continue
        eid = edge_id(rule)
        print(f"  PASS  {name}: train roi={tr['roi']}% (n={tr['n']}, t={tr['tstat']}) | "
              f"holdout roi={ho['roi']}% (n={ho['n']})  -> {eid}")
        edge = {"id": eid, "name": name, "description": desc, "rule": rule,
                "train": tr, "holdout": ho}
        passed.append(edge)
        if not dry:
            if register(conn, edge):
                registered += 1

    print(f"\n{len(passed)} edges passed train+holdout. {registered} newly registered"
          + (" (dry-run: nothing written)" if dry else "") + ".")
    conn.close()


if __name__ == "__main__":
    main()
