#!/usr/bin/env python3
"""
The Racing API collector -> Supabase.

Two modes (RACING_MODE env):
  racecards : pull /v1/racecards/pro for GB/IRE for a day, upsert races +
              runners + an odds snapshot (incl the API's per-bookmaker history).
              Run every ~15 min on race day to capture odds movement.
  results   : pull /v1/results for a day, upsert race + per-runner results.
              Run after racing.

Auth: HTTP Basic with your Racing API username/password (RACING_API_USERNAME /
RACING_API_PASSWORD). Public-data caveats don't apply here — this is your paid
key, kept as GitHub secrets, never in code.

Idempotent throughout: races/runners/results upsert on natural keys; odds upsert
on (race_id, horse_id, bookmaker, odds_time) so repeated polls never duplicate
but every genuine price change is stored.
"""
from __future__ import annotations
import os, sys, time, datetime as dt
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
RA_USER = os.environ["RACING_API_USERNAME"]
RA_PASS = os.environ["RACING_API_PASSWORD"]

BASE     = os.environ.get("RACING_API_BASE", "https://api.theracingapi.com")
MODE     = os.environ.get("RACING_MODE", "racecards")          # racecards | results
REGIONS  = [r for r in os.environ.get("REGION_CODES", "gb,ire").split(",") if r]
DATE     = os.environ.get("DATE", "")                          # YYYY-MM-DD; blank=today(UTC)
REQ_DELAY = float(os.environ.get("REQUEST_DELAY", "0.3"))      # politeness (<=5/s)
PAGE     = int(os.environ.get("PAGE_SIZE", "50"))

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, connect=5, read=5, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.auth = (RA_USER, RA_PASS)
    s.headers.update({"Accept": "application/json",
                      "User-Agent": "boothco-racing-collector/1.0"})
    return s

HTTP = make_session()
DB: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def today() -> str:
    return DATE or dt.datetime.now(dt.timezone.utc).date().isoformat()

def get(path: str, params=None) -> Any:
    r = HTTP.get(f"{BASE}{path}", params=params or {}, timeout=40)
    time.sleep(REQ_DELAY)
    if r.status_code == 401:
        raise RuntimeError(f"401 Unauthorised for {path} — check plan level / credentials")
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} {params} -> {r.status_code}: {r.text[:200]}")
    return r.json()

def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def ts(x):
    if not x:
        return None
    try:
        return dt.datetime.fromisoformat(str(x).replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None

# --------------------------------------------------------------------------- #
def run_racecards() -> dict:
    day = today()
    races = runners = odds_rows = 0
    skip = 0
    while True:
        params = [("date", day), ("limit", PAGE), ("skip", skip)]
        params += [("region_codes", r) for r in REGIONS]
        data = get("/v1/racecards/pro", params)
        cards = data.get("racecards", []) if isinstance(data, dict) else []
        if not cards:
            break
        race_rows, runner_rows, odds_batch = [], [], []
        for rc in cards:
            race_rows.append({
                "race_id": rc.get("race_id"), "date": rc.get("date"),
                "off_time": rc.get("off_time"), "off_dt": ts(rc.get("off_dt")),
                "course": rc.get("course"), "course_id": rc.get("course_id"),
                "region": rc.get("region"), "race_name": rc.get("race_name"),
                "race_class": rc.get("race_class"), "type": rc.get("type"),
                "pattern": rc.get("pattern"), "age_band": rc.get("age_band"),
                "rating_band": rc.get("rating_band"), "sex_restriction": rc.get("sex_restriction"),
                "distance": rc.get("distance"), "distance_round": rc.get("distance_round"),
                "distance_f": rc.get("distance_f"), "prize": rc.get("prize"),
                "field_size": rc.get("field_size"), "going": rc.get("going"),
                "going_detailed": rc.get("going_detailed"), "rail_movements": rc.get("rail_movements"),
                "stalls": rc.get("stalls"), "weather": rc.get("weather"),
                "surface": rc.get("surface"), "jumps": rc.get("jumps"),
                "big_race": rc.get("big_race"), "is_abandoned": rc.get("is_abandoned"),
                "tip": rc.get("tip"), "verdict": rc.get("verdict"),
                "betting_forecast": rc.get("betting_forecast"), "race_status": rc.get("race_status"),
                "raw": rc, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
            for rn in rc.get("runners", []) or []:
                t14 = rn.get("trainer_14_days") or {}
                runner_rows.append({
                    "race_id": rc.get("race_id"), "horse_id": rn.get("horse_id"),
                    "horse": rn.get("horse"), "number": rn.get("number"), "draw": rn.get("draw"),
                    "dob": rn.get("dob"), "age": rn.get("age"), "sex": rn.get("sex"),
                    "sex_code": rn.get("sex_code"), "colour": rn.get("colour"),
                    "region": rn.get("region"), "breeder": rn.get("breeder"),
                    "dam": rn.get("dam"), "dam_id": rn.get("dam_id"), "dam_region": rn.get("dam_region"),
                    "sire": rn.get("sire"), "sire_id": rn.get("sire_id"), "sire_region": rn.get("sire_region"),
                    "damsire": rn.get("damsire"), "damsire_id": rn.get("damsire_id"), "damsire_region": rn.get("damsire_region"),
                    "trainer": rn.get("trainer"), "trainer_id": rn.get("trainer_id"),
                    "trainer_location": rn.get("trainer_location"), "trainer_rtf": rn.get("trainer_rtf"),
                    "trainer_14d_runs": t14.get("runs"), "trainer_14d_wins": t14.get("wins"),
                    "trainer_14d_percent": t14.get("percent"),
                    "owner": rn.get("owner"), "owner_id": rn.get("owner_id"),
                    "headgear": rn.get("headgear"), "headgear_run": rn.get("headgear_run"),
                    "wind_surgery": rn.get("wind_surgery"), "wind_surgery_run": rn.get("wind_surgery_run"),
                    "lbs": rn.get("lbs"), "ofr": rn.get("ofr"), "rpr": rn.get("rpr"), "ts": rn.get("ts"),
                    "jockey": rn.get("jockey"), "jockey_id": rn.get("jockey_id"),
                    "silk_url": rn.get("silk_url"), "last_run": rn.get("last_run"),
                    "form": rn.get("form"), "comment": rn.get("comment"), "spotlight": rn.get("spotlight"),
                    "prev_trainers": rn.get("prev_trainers"), "prev_owners": rn.get("prev_owners"),
                    "quotes": rn.get("quotes"), "stable_tour": rn.get("stable_tour"),
                    "medical": rn.get("medical"), "past_results_flags": rn.get("past_results_flags"),
                    "raw": rn, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
                # odds: one entry per bookmaker, each with current price + history
                for ob in rn.get("odds", []) or []:
                    bm = ob.get("bookmaker")
                    if not bm:
                        continue
                    seen = {}
                    cur_t = ts(ob.get("updated")) or dt.datetime.now(dt.timezone.utc).isoformat()
                    seen[cur_t] = {"decimal": num(ob.get("decimal")), "fractional": ob.get("fractional")}
                    for h in ob.get("history", []) or []:
                        ht = ts(h.get("changed_at"))
                        if ht:
                            seen[ht] = {"decimal": num(h.get("decimal")), "fractional": h.get("fractional")}
                    for otime, vals in seen.items():
                        odds_batch.append({
                            "race_id": rc.get("race_id"), "horse_id": rn.get("horse_id"),
                            "bookmaker": bm, "odds_time": otime,
                            "decimal": vals["decimal"], "fractional": vals["fractional"],
                            "ew_places": ob.get("ew_places"), "ew_denom": ob.get("ew_denom"),
                            "raw": ob,
                        })
        if race_rows:
            DB.table("ra_races").upsert(race_rows, on_conflict="race_id").execute()
            races += len(race_rows)
        if runner_rows:
            DB.table("ra_runners").upsert(runner_rows, on_conflict="race_id,horse_id").execute()
            runners += len(runner_rows)
        # odds upsert in chunks
        for i in range(0, len(odds_batch), 500):
            DB.table("ra_odds").upsert(odds_batch[i:i+500],
                                       on_conflict="race_id,horse_id,bookmaker,odds_time").execute()
        odds_rows += len(odds_batch)
        if len(cards) < PAGE:
            break
        skip += len(cards)
    return {"kind": "racecards", "races": races, "runners": runners, "odds_rows": odds_rows}

# --------------------------------------------------------------------------- #
def run_results() -> dict:
    day = today()
    results = runners = 0
    skip = 0
    while True:
        params = [("start_date", day), ("end_date", day), ("limit", PAGE), ("skip", skip)]
        params += [("region", r) for r in REGIONS]
        data = get("/v1/results", params)
        res = data.get("results", []) if isinstance(data, dict) else []
        if not res:
            break
        rrows, runrows = [], []
        for rr in res:
            rrows.append({
                "race_id": rr.get("race_id"), "date": rr.get("date"), "off": rr.get("off"),
                "off_dt": ts(rr.get("off_dt")), "course": rr.get("course"),
                "course_id": rr.get("course_id"), "region": rr.get("region"),
                "race_name": rr.get("race_name"), "type": rr.get("type"), "class": rr.get("class"),
                "pattern": rr.get("pattern"), "rating_band": rr.get("rating_band"),
                "age_band": rr.get("age_band"), "sex_rest": rr.get("sex_rest"),
                "dist": rr.get("dist"), "dist_y": rr.get("dist_y"), "dist_m": rr.get("dist_m"),
                "dist_f": rr.get("dist_f"), "going": rr.get("going"), "surface": rr.get("surface"),
                "jumps": rr.get("jumps"), "winning_time_detail": rr.get("winning_time_detail"),
                "comments": rr.get("comments"), "non_runners": rr.get("non_runners"),
                "tote_win": rr.get("tote_win"), "tote_pl": rr.get("tote_pl"), "tote_ex": rr.get("tote_ex"),
                "tote_csf": rr.get("tote_csf"), "tote_tricast": rr.get("tote_tricast"),
                "tote_trifecta": rr.get("tote_trifecta"), "raw": rr,
            })
            for rn in rr.get("runners", []) or []:
                runrows.append({
                    "race_id": rr.get("race_id"), "horse_id": rn.get("horse_id"), "horse": rn.get("horse"),
                    "position": rn.get("position"), "sp": rn.get("sp"), "sp_dec": num(rn.get("sp_dec")),
                    "bsp": num(rn.get("bsp")), "number": rn.get("number"), "draw": rn.get("draw"),
                    "btn": rn.get("btn"), "ovr_btn": rn.get("ovr_btn"), "age": rn.get("age"),
                    "sex": rn.get("sex"), "weight": rn.get("weight"), "weight_lbs": rn.get("weight_lbs"),
                    "headgear": rn.get("headgear"), "time": rn.get("time"), "or": rn.get("or"),
                    "rpr": rn.get("rpr"), "tsr": rn.get("tsr"), "prize": rn.get("prize"),
                    "jockey": rn.get("jockey"), "jockey_id": rn.get("jockey_id"),
                    "jockey_claim_lbs": rn.get("jockey_claim_lbs"),
                    "trainer": rn.get("trainer"), "trainer_id": rn.get("trainer_id"),
                    "owner": rn.get("owner"), "owner_id": rn.get("owner_id"),
                    "sire": rn.get("sire"), "sire_id": rn.get("sire_id"),
                    "dam": rn.get("dam"), "dam_id": rn.get("dam_id"),
                    "damsire": rn.get("damsire"), "damsire_id": rn.get("damsire_id"),
                    "comment": rn.get("comment"), "silk_url": rn.get("silk_url"), "raw": rn,
                })
        if rrows:
            DB.table("ra_results").upsert(rrows, on_conflict="race_id").execute()
            results += len(rrows)
        if runrows:
            DB.table("ra_result_runners").upsert(runrows, on_conflict="race_id,horse_id").execute()
            runners += len(runrows)
        if len(res) < PAGE:
            break
        skip += len(res)
    return {"kind": "results", "results": results, "runners": runners}

# --------------------------------------------------------------------------- #
def main() -> int:
    run = DB.table("ra_fetch_runs").insert({"kind": MODE, "status": "running"}).execute().data[0]
    rid = run["id"]
    try:
        out = run_racecards() if MODE == "racecards" else run_results()
        DB.table("ra_fetch_runs").update({
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "ok",
            "races": out.get("races", 0), "runners": out.get("runners", 0),
            "odds_rows": out.get("odds_rows", 0), "results": out.get("results", 0),
            "notes": out,
        }).eq("id", rid).execute()
        print(f"OK [{MODE}] {today()}: {out}")
        return 0
    except Exception as e:
        DB.table("ra_fetch_runs").update({
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "failed", "errors": 1, "notes": {"error": str(e)[:500]},
        }).eq("id", rid).execute()
        print(f"FAILED [{MODE}]: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
