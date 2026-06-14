#!/usr/bin/env python3
"""Scanner - the edge finder (broad systematic scan).
Generates a wide candidate space (tipsters, trainers, race conditions, combos),
backtests on a TRAINING slice, confirms survivors on a HOLDOUT slice, registers
passers as 'paper'. The forward paper record (settler) is the final judge.

  python scanner.py [--dry-run] [--limit N]
Requires env SUPABASE_DB_URL."""
import sys, json, hashlib, datetime as dt
import engine

POOL8 = ["ca-bets", "shes-the-fastest", "on-target-tips", "the-profit-rocket",
         "active-betting-hub", "model-man", "equii-tensor", "racebot-uk"]

MIN_TRAIN_N = 50
MIN_TRAIN_TSTAT = 1.8
MAX_CONCENTRATION = 60
MIN_HOLDOUT_N = 20
MIN_HOLDOUT_ROI = 0.0
MIN_HOLDOUT_TSTAT = 0.8
HOLDOUT_DAYS = 45
MIN_TIPSTER_SINGLES = 120
MIN_TRAINER_TIPS = 60
MAX_CANDIDATES = 400
LEVEL = {"type": "level", "points": 1}


def qualifying_tipsters(conn):
    cur = conn.cursor()
    cur.execute("""SELECT ts.slug, ts.name, count(*) n
        FROM tips t JOIN tipsters ts ON ts.slug=t.slug
        WHERE COALESCE(t.n_selections,1)<=1 AND t.profit_points IS NOT NULL
        GROUP BY ts.slug, ts.name HAVING count(*) >= %s ORDER BY n DESC""",
        (MIN_TIPSTER_SINGLES,))
    rows = cur.fetchall(); cur.close()
    return [(r[0], r[1]) for r in rows]


def qualifying_trainers(conn):
    cur = conn.cursor()
    cur.execute("""SELECT l.trainer, count(*) n
        FROM tips t JOIN tip_legs l ON l.tip_reference=t.reference
        WHERE COALESCE(t.n_selections,1)<=1 AND t.profit_points IS NOT NULL
          AND l.trainer IS NOT NULL AND l.trainer<>''
        GROUP BY l.trainer HAVING count(*) >= %s ORDER BY n DESC""",
        (MIN_TRAINER_TIPS,))
    rows = cur.fetchall(); cur.close()
    return [r[0] for r in rows]


def _rule(**sel):
    sel = {k: v for k, v in sel.items() if v is not None}
    return {"selection": sel, "staking": LEVEL}


def _cond(sel):
    bits = []
    if sel.get("exclude_surface"): bits.append("no AW")
    if sel.get("race_code_in"): bits.append(sel["race_code_in"][0].lower())
    if sel.get("dist_band"): bits.append(sel["dist_band"])
    if sel.get("going_group"): bits.append("soft/heavy" if sel["going_group"] == "soft" else "good/firm")
    return ", ".join(bits)


def gen_candidates(conn):
    surfaces = [None, ["AW"]]
    codes = [None, ["Jumps"], ["Flat"]]
    dbands = ["sprint", "mile", "middle", "staying"]
    goings = ["soft", "goodfirm"]

    for cmin in (2, 3):
        for excl in surfaces:
            for code in codes:
                sel = dict(pool=POOL8, consensus_min=cmin)
                if excl: sel["exclude_surface"] = excl
                if code: sel["race_code_in"] = code
                c = _cond(sel)
                lbl = (">=%d of 8" % cmin) + (", " + c if c else "")
                yield "Consensus: " + lbl, "Back horse where " + lbl + ".", _rule(**sel)
    for db in dbands:
        yield "Consensus: >=2 of 8, " + db, ">=2 of 8 agree, " + db + ".", _rule(pool=POOL8, consensus_min=2, dist_band=db)
    for gg in goings:
        g = "soft/heavy" if gg == "soft" else "good/firm"
        yield "Consensus: >=2 of 8, " + g, ">=2 of 8 agree, " + g + ".", _rule(pool=POOL8, consensus_min=2, going_group=gg)

    for code in codes[1:]:
        c = code[0].lower()
        yield "All tips: " + c, "Back every tip in " + c + " races.", _rule(race_code_in=code)
    for db in dbands:
        yield "All tips: " + db, "Back every tip at " + db + " distances.", _rule(dist_band=db)
    for gg in goings:
        g = "soft/heavy" if gg == "soft" else "good/firm"
        yield "All tips: " + g, "Back every tip on " + g + " going.", _rule(going_group=gg)

    for slug, nm in qualifying_tipsters(conn):
        yield "Solo: " + nm, "Back all " + nm + " singles.", _rule(pool=[slug], consensus_min=1)
        yield "Solo: " + nm + ", no AW", "Back all " + nm + " singles except AW.", _rule(pool=[slug], consensus_min=1, exclude_surface=["AW"])
        yield "Solo: " + nm + ", jumps", "Back " + nm + " singles in jumps.", _rule(pool=[slug], consensus_min=1, race_code_in=["Jumps"])

    for tr in qualifying_trainers(conn):
        yield "Trainer: " + tr, "Back any tip on a " + tr + " runner.", _rule(trainer=tr)
        yield "Trainer: " + tr + ", no AW", "Back any tip on a " + tr + " runner except AW.", _rule(trainer=tr, exclude_surface=["AW"])


def edge_id(rule):
    return "edge_" + hashlib.sha1(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:10]


def data_range(conn):
    cur = conn.cursor()
    cur.execute("SELECT min(posted_at)::date, max(posted_at)::date FROM tips WHERE profit_points IS NOT NULL")
    lo, hi = cur.fetchone(); cur.close()
    return lo, hi


def register(conn, e):
    cur = conn.cursor()
    cur.execute("""INSERT INTO edges (id, name, description, rule, status, frozen_at, bt_train, bt_holdout)
        VALUES (%s,%s,%s,%s,'paper',now(),%s,%s) ON CONFLICT (id) DO NOTHING""",
        (e["id"], e["name"], e["description"], json.dumps(e["rule"]),
         json.dumps(e["train"]), json.dumps(e["holdout"])))
    ok = cur.rowcount > 0
    conn.commit(); cur.close()
    return ok


def main():
    dry = "--dry-run" in sys.argv
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    conn = engine.connect()
    lo, hi = data_range(conn)
    split = hi - dt.timedelta(days=HOLDOUT_DAYS)
    print("Data %s -> %s. Train < %s <= Holdout. dry_run=%s" % (lo, hi, split, dry))

    evaluated = passed = registered = 0
    cap = limit or MAX_CANDIDATES
    for name, desc, rule in gen_candidates(conn):
        if evaluated >= cap:
            break
        evaluated += 1
        try:
            tr, _ = engine.backtest(conn, rule, lo, split)
        except Exception as ex:
            print("  err " + name + ": " + str(ex)); continue
        if tr["n"] < MIN_TRAIN_N or (tr["tstat"] or 0) < MIN_TRAIN_TSTAT \
           or (tr["roi"] or -1) <= 0 \
           or (tr["concentration"] is not None and tr["concentration"] > MAX_CONCENTRATION):
            continue
        ho, _ = engine.backtest(conn, rule, split, hi + dt.timedelta(days=1))
        if ho["n"] < MIN_HOLDOUT_N or (ho["roi"] or -1) < MIN_HOLDOUT_ROI \
           or (ho["tstat"] or 0) < MIN_HOLDOUT_TSTAT:
            continue
        passed += 1
        e = {"id": edge_id(rule), "name": name, "description": desc, "rule": rule, "train": tr, "holdout": ho}
        print("  PASS %s: train %s%% (n=%s,t=%s) | holdout %s%% (n=%s,t=%s)"
              % (name, tr["roi"], tr["n"], tr["tstat"], ho["roi"], ho["n"], ho["tstat"]))
        if not dry and register(conn, e):
            registered += 1

    print("\nEvaluated %d candidates. %d passed train+holdout. %d newly registered%s."
          % (evaluated, passed, registered, " (dry-run)" if dry else ""))
    conn.close()


if __name__ == "__main__":
    main()
