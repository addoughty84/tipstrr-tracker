#!/usr/bin/env python3
"""
Tip-leg matcher  ->  links tip_legs to the Racing API race_id + horse_id.

Runs entirely off data already in Supabase (ra_races / ra_runners, populated by
racing.py every 15 min) - NO Racing API calls, so it can never be rate-limited
or blocked. Idempotent and self-healing: only touches legs that aren't settled
to a match yet, and retries 'pending' legs until their racecard appears.

Strategy (proven, horse-first - a horse name is near-unique across a day):
  1. Normalise the horse name (drop country suffix, punctuation, case).
  2. Exact normalised match within the SAME RACE DATE's runners -> done.
  3. >1 same-named horse that day -> disambiguate by course, then off-time.
  4. No exact hit -> fuzzy (difflib) with a high cut-off, confirmed by
     course/time, else flagged - we flag rather than guess. Money rides on it.

match_status written to tip_legs:
  matched     - race_id + horse_id set, confident
  ambiguous   - >1 plausible horse, left for review (no id written)
  no_match    - race date is in the past and no card was ever found (terminal)
  pending     - no card yet but the race is recent/future -> retried next run
  non_runner  - leg flagged a non-runner (still matched to the race if possible)

Env: SUPABASE_DB_URL (Postgres pooler string, same as the edge engine).
Usage: python match_legs.py [--dry-run] [--days N] [--verbose]
"""
import os, re, sys, difflib, datetime as dt
import psycopg2, psycopg2.extras

FUZZY_CUTOFF = float(os.environ.get("MATCH_FUZZY_CUTOFF", "0.84"))
SURE_CUTOFF  = float(os.environ.get("MATCH_SURE_CUTOFF", "0.92"))
COURSE_RATIO = 0.75
LOOKBACK_DAYS = int(os.environ.get("MATCH_LOOKBACK_DAYS", "10"))
PENDING_GRACE_DAYS = int(os.environ.get("MATCH_PENDING_GRACE_DAYS", "2"))

COUNTRY_SUFFIX = re.compile(r"\s*\(([A-Z]{2,3})\)\s*$", re.IGNORECASE)


def connect():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("Missing env SUPABASE_DB_URL")
    conn = psycopg2.connect(url, connect_timeout=20)
    conn.autocommit = True
    return conn


def norm(s):
    """Normalise a horse / course name for comparison."""
    if not s:
        return ""
    s = COUNTRY_SUFFIX.sub("", str(s).strip())
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_time(t):
    """'5:15', '17:15', '5.15pm' -> 12h form so local/UTC & 12/24h compare equal."""
    if not t:
        return ""
    m = re.search(r"(\d{1,2})[:.](\d{2})", str(t))
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h % 12:02d}:{mi:02d}"


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def leg_date(fixture_reference, posted_date):
    """Race date: prefer the fixtureReference (YYYY-MM-DD-HHMM-course-hash)."""
    if fixture_reference:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(fixture_reference))
        if m:
            return m.group(0)
    return str(posted_date) if posted_date else None


def load_runners(conn, dates):
    """All GB/IRE runners for the given race dates, grouped by date."""
    by_date = {}
    if not dates:
        return by_date
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT ra.date::text AS date, ra.race_id, ra.course, ra.off_time,
               rr.horse_id, rr.horse
        FROM ra_runners rr
        JOIN ra_races ra ON ra.race_id = rr.race_id
        WHERE ra.date::text = ANY(%(dates)s)
    """, {"dates": list(dates)})
    for r in cur.fetchall():
        by_date.setdefault(r["date"], []).append({
            "race_id": r["race_id"], "horse_id": r["horse_id"],
            "horse": r["horse"], "nhorse": norm(r["horse"]),
            "course": r["course"], "ncourse": norm(r["course"]),
            "ntime": norm_time(r["off_time"]),
        })
    cur.close()
    return by_date


def confirmed(leg, runner):
    nc = norm(leg.get("course") or "")
    if nc and ratio(nc, runner["ncourse"]) >= COURSE_RATIO:
        return True
    tt = norm_time(leg.get("race_time") or "")
    return bool(tt) and tt == runner["ntime"]


def narrow(leg, runners):
    nc = norm(leg.get("course") or "")
    if nc:
        bc = [r for r in runners if ratio(nc, r["ncourse"]) >= COURSE_RATIO]
        if len(bc) == 1:
            return bc
        if bc:
            runners = bc
    tt = norm_time(leg.get("race_time") or "")
    if tt:
        bt = [r for r in runners if r["ntime"] == tt]
        if len(bt) == 1:
            return bt
        if bt:
            runners = bt
    return runners


def best_match(leg, runners):
    """Return (runner|None, confidence, status)."""
    nh = norm(leg.get("horse", ""))
    if not nh or not runners:
        return None, 0.0, "none"

    exact = [r for r in runners if r["nhorse"] == nh]
    if len(exact) == 1:
        return exact[0], 1.0, "matched"
    if len(exact) > 1:
        nz = narrow(leg, exact)
        if len(nz) == 1:
            return nz[0], 0.97, "matched"
        return None, 0.5, "ambiguous"

    names = {r["nhorse"] for r in runners}
    close = difflib.get_close_matches(nh, names, n=5, cutoff=FUZZY_CUTOFF)
    if not close:
        return None, 0.0, "none"
    fuzzy = [r for r in runners if r["nhorse"] in close]
    best = max(ratio(nh, r["nhorse"]) for r in fuzzy)
    top = [r for r in fuzzy if ratio(nh, r["nhorse"]) >= best - 0.02]
    if len(top) > 1:
        top = narrow(leg, top)
    if len(top) == 1 and (best >= SURE_CUTOFF or confirmed(leg, top[0])):
        return top[0], round(best, 3), "matched"
    return None, round(best * 0.6, 3), "ambiguous"


def fetch_unmatched(conn, days):
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT l.id, l.course, l.race_time, l.fixture_reference, l.horse,
               l.non_runner, t.posted_at::date AS posted_date
        FROM tip_legs l
        JOIN tips t ON t.reference = l.tip_reference
        WHERE (l.match_status IS NULL OR l.match_status IN ('pending'))
          AND l.horse IS NOT NULL AND l.horse <> ''
          AND t.posted_at::date >= %s
        ORDER BY t.posted_at
    """, (cutoff,))
    rows = cur.fetchall()
    cur.close()
    return rows


def main():
    dry = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else LOOKBACK_DAYS
    conn = connect()

    legs = fetch_unmatched(conn, days)
    for l in legs:
        l["date"] = leg_date(l["fixture_reference"], l["posted_date"])
    dates = {l["date"] for l in legs if l["date"]}
    runners = load_runners(conn, dates)
    today = dt.date.today()

    updates = []
    tally = {"matched": 0, "ambiguous": 0, "no_match": 0, "pending": 0, "non_runner": 0}
    samples = {"matched": [], "ambiguous": [], "no_match": []}

    for l in legs:
        day_runners = runners.get(l["date"], [])
        runner, conf, status = best_match(l, day_runners)

        if status == "matched":
            st = "non_runner" if l.get("non_runner") else "matched"
            tally[st] += 1
            updates.append((l["id"], runner["race_id"], runner["horse_id"],
                            st, conf, runner["horse"]))
            if len(samples["matched"]) < 12:
                samples["matched"].append(
                    f"{l['horse']} @ {l['course']} {l['race_time']} -> {runner['horse']} "
                    f"({runner['race_id']}/{runner['horse_id']}) conf={conf}")
        elif status == "ambiguous":
            tally["ambiguous"] += 1
            updates.append((l["id"], None, None, "ambiguous", conf, None))
            if len(samples["ambiguous"]) < 8:
                samples["ambiguous"].append(f"{l['horse']} @ {l['course']} {l['race_time']} ({l['date']})")
        else:
            try:
                rd = dt.date.fromisoformat(l["date"]) if l["date"] else None
            except Exception:
                rd = None
            recent = rd is None or rd >= today - dt.timedelta(days=PENDING_GRACE_DAYS)
            st = "pending" if recent else "no_match"
            tally[st] += 1
            updates.append((l["id"], None, None, st, conf, None))
            if st == "no_match" and len(samples["no_match"]) < 10:
                samples["no_match"].append(f"{l['horse']} @ {l['course']} {l['race_time']} ({l['date']})")

    print(f"Examined {len(legs)} unmatched legs across {len(dates)} race dates.")
    for k in ("matched", "non_runner", "ambiguous", "pending", "no_match"):
        print(f"  {k:10}: {tally[k]}")
    if verbose:
        for k in ("matched", "ambiguous", "no_match"):
            if samples[k]:
                print(f"\n--- sample {k} ---")
                for s in samples[k]:
                    print("  " + s)

    if dry:
        print("\n[dry-run] no writes.")
        conn.close()
        return

    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        UPDATE tip_legs AS l SET
          race_id = d.race_id, horse_id = d.horse_id,
          match_status = d.match_status, match_confidence = d.match_confidence,
          ra_horse = d.ra_horse
        FROM (VALUES %s) AS d(id, race_id, horse_id, match_status, match_confidence, ra_horse)
        WHERE l.id = d.id
    """, updates, template="(%s,%s,%s,%s,%s,%s)")
    conn.commit()
    cur.close()
    print(f"\nWrote {len(updates)} leg updates.")
    conn.close()


if __name__ == "__main__":
    main()
