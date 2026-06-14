#!/usr/bin/env python3
"""Researcher - the LLM hypothesis generator (Phase 2).

Asks Claude to propose candidate betting-edge theories, translates the ones our
data can express into engine rules, then runs them through the SAME discipline as
the scanner (train backtest -> holdout confirm) and registers survivors as 'paper'.
Theories that need data we don't have are logged to `data_gaps` for later
enrichment, so a good idea is never lost just because we can't test it yet.

The internet/LLM supplies *hypotheses*; your data + the forward paper test supply
*truth*. Same funnel as everything else - this just feeds smarter candidates in.

Budget-capped: at most RESEARCHER_MAX_CALLS Claude calls per run (cheap Haiku model).

  python researcher.py                 # real run (needs ANTHROPIC_API_KEY)
  python researcher.py --mock          # use a built-in sample response (no API call)

Env: SUPABASE_DB_URL, ANTHROPIC_API_KEY, optional CLAUDE_MODEL,
     RESEARCHER_MAX_CALLS (default 1), RESEARCHER_MAX_TOKENS (default 2500)."""
import os, sys, json, hashlib, urllib.request
import engine, scanner

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
MAX_CALLS = int(os.environ.get("RESEARCHER_MAX_CALLS", "1"))
MAX_TOKENS = int(os.environ.get("RESEARCHER_MAX_TOKENS", "2500"))

# selection fields the engine understands (anything else -> data gap)
ALLOWED = {"pool", "consensus_min", "exclude_surface", "race_code_in",
           "dist_band", "going_group", "odds_min", "odds_max",
           "trainer", "jockey", "course", "race_class", "dow",
           "first_time_headgear", "wind_surgery_first", "days_since", "trainer_hot"}

SYSTEM = """You are a quantitative horse-racing analyst hunting for betting edges in a
database of tipster picks for UK & Ireland racing. Each pick is a horse in a race,
with the tipster, advised odds, result, and the race's conditions.

Propose CANDIDATE THEORIES that might beat the market. For each, if it can be
expressed using ONLY the available selection fields, output a machine-readable
"rule". If it needs information we do NOT have, set "rule": null and describe the
missing data in "needs_data".

Available selection fields (use only these):
- pool: "top8"  (the 8 strongest tipsters) — or omit
- consensus_min: integer (min distinct tipsters on the same horse)
- exclude_surface: ["AW"]
- race_code_in: ["Jumps"] or ["Flat"]
- dist_band: "sprint" | "mile" | "middle" | "staying"
- going_group: "soft" | "goodfirm"
- odds_min / odds_max: numbers (advised odds)
- trainer / jockey / course: exact name string
- race_class: "Class 1".."Class 6"
- dow: 0=Sun .. 6=Sat
- first_time_headgear: true  (horse wears headgear for the first time)
- wind_surgery_first: true   (first run after a wind operation)
- days_since: "recent" (<=21d) | "mid" (22-90d) | "layoff" (>90d)
- trainer_hot: true          (trainer's 14-day strike rate >= 20%)

Favour plausible, mechanism-backed angles and COMBINATIONS (e.g. odds band x going,
consensus x distance, short-priced favourites on soft ground). Avoid single obvious
slices already obviously covered. Be a sceptic: only propose things with a real
reason they'd work.

Reply with ONLY valid JSON, no prose:
{"candidates":[{"name":"short label","theory":"one sentence why it might work",
  "rule":{"selection":{...},"staking":{"type":"level","points":1}} or null,
  "needs_data": null or "what data is missing"}]}
Propose about 12 candidates."""

USER = "Propose ~12 candidate edges now. Mix of testable rules and a few that need data we lack."

MOCK = {"candidates": [
    {"name": "Consensus favs on soft", "theory": "Multiple tipsters agreeing on a shorter-priced horse on soft ground is a stamina/fitness signal.",
     "rule": {"selection": {"pool": "top8", "consensus_min": 2, "going_group": "soft", "odds_max": 8}, "staking": {"type": "level", "points": 1}}, "needs_data": None},
    {"name": "Mid-odds jumps consensus", "theory": "Agreement at mid odds in jumps avoids both unbackable favs and pure longshots.",
     "rule": {"selection": {"pool": "top8", "consensus_min": 2, "race_code_in": ["Jumps"], "odds_min": 4, "odds_max": 12}, "staking": {"type": "level", "points": 1}}, "needs_data": None},
    {"name": "First-time cheekpieces", "theory": "Headgear applied for the first time often sharpens a horse up.",
     "rule": None, "needs_data": "headgear / first-time-headgear flag per runner"},
    {"name": "Market drift shorteners", "theory": "Horses whose price shortens sharply pre-off attract informed money.",
     "rule": None, "needs_data": "price movement / odds over time before the off"},
]}


def call_llm():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("Missing env ANTHROPIC_API_KEY")
    body = json.dumps({"model": MODEL, "max_tokens": MAX_TOKENS, "temperature": 1,
                       "system": SYSTEM, "messages": [{"role": "user", "content": USER}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read())
    text = data["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`"); text = text[text.find("{"):text.rfind("}") + 1]
    return json.loads(text)


def valid_rule(rule):
    """Return cleaned rule if every selection key is supported, else None."""
    if not isinstance(rule, dict):
        return None
    sel = rule.get("selection") or {}
    if not sel or any(k not in ALLOWED for k in sel):
        return None
    if sel.get("pool") == "top8":
        sel = dict(sel, pool=scanner.POOL8)
    elif sel.get("pool"):           # named pool we can't resolve
        return None
    return {"selection": sel, "staking": rule.get("staking") or scanner.LEVEL}


def gap_id(theory):
    return "gap_" + hashlib.sha1(theory.encode()).hexdigest()[:10]


def log_gap(conn, c):
    cur = conn.cursor()
    cur.execute("""INSERT INTO data_gaps (id, theory, needs_data) VALUES (%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (gap_id(c.get("name", "") + (c.get("needs_data") or "")),
                 c.get("name", "") + " — " + (c.get("theory") or ""), c.get("needs_data")))
    ok = cur.rowcount > 0; conn.commit(); cur.close()
    return ok


def passes(tr, ho):
    if tr["n"] < scanner.MIN_TRAIN_N or (tr["tstat"] or 0) < scanner.MIN_TRAIN_TSTAT \
       or (tr["roi"] or -1) <= 0 \
       or (tr["concentration"] is not None and tr["concentration"] > scanner.MAX_CONCENTRATION):
        return False
    if ho["n"] < scanner.MIN_HOLDOUT_N or (ho["roi"] or -1) < scanner.MIN_HOLDOUT_ROI \
       or (ho["tstat"] or 0) < scanner.MIN_HOLDOUT_TSTAT:
        return False
    return True


def main():
    mock = "--mock" in sys.argv
    import datetime as dt
    conn = engine.connect()
    lo, hi = scanner.data_range(conn)
    split = hi - dt.timedelta(days=scanner.HOLDOUT_DAYS)

    resp = MOCK if mock else None
    calls = 0
    if not mock:
        try:
            resp = call_llm(); calls = 1
        except Exception as ex:
            print("LLM call failed: " + str(ex)); resp = {"candidates": []}
    cands = resp.get("candidates", [])
    print("Researcher: %d candidates from %s (calls=%d)\n" % (len(cands), "MOCK" if mock else MODEL, calls))

    registered = gaps = tested = 0
    for c in cands:
        rule = valid_rule(c.get("rule"))
        if rule is None:
            if log_gap(conn, c):
                gaps += 1
                print("  GAP  %s -> needs: %s" % (c.get("name"), c.get("needs_data")))
            continue
        tested += 1
        try:
            tr, _ = engine.backtest(conn, rule, lo, split)
            ho, _ = engine.backtest(conn, rule, split, hi + dt.timedelta(days=1))
        except Exception as ex:
            print("  err  %s: %s" % (c.get("name"), ex)); continue
        if not passes(tr, ho):
            print("  fail %s: train n=%s roi=%s t=%s | holdout n=%s roi=%s" %
                  (c.get("name"), tr["n"], tr["roi"], tr["tstat"], ho["n"], ho["roi"]))
            continue
        e = {"id": scanner.edge_id(rule), "name": "AI: " + (c.get("name") or "edge"),
             "description": c.get("theory") or "", "rule": rule, "train": tr, "holdout": ho}
        if scanner.register(conn, e):
            registered += 1
            print("  PASS %s: train %s%% | holdout %s%% (n=%s)" %
                  (c.get("name"), tr["roi"], ho["roi"], ho["n"]))

    print("\nTested %d rules, registered %d new edges, logged %d data gaps." % (tested, registered, gaps))
    conn.close()


if __name__ == "__main__":
    main()
