#!/usr/bin/env python3
"""
Tipstrr Hot/Cold Tracker — daily scraper (RICH mode, auth-free).

Captures, for every horse-racing tipster, the full settled-tip detail:
horse, jockey, trainer, advised odds, BSP, Rule-4, best-odds-guaranteed,
profit, finishing position and the actual race winner — plus daily rolling
ROI/profit snapshots for hot/cold detection.

Why it's bulletproof:
  * No login.   All data comes from public pages/endpoints, so there is no
                session/cookie to expire (the usual cause of dead scrapers).
  * Idempotent. Everything upserts on tipstrr's own unique ids -> re-runs,
                double-runs and missed days self-heal. No dupes, no gaps.
  * Incremental. Daily runs only fetch the per-tip page for *new* references,
                so they're cheap. A one-time FULL_BACKFILL pulls all history.
  * Fault-isolated + logged to the scrape_runs table.

Sources (all public):
  Discovery : GET /api/portfolio?filterType=0&sportId=2&...&page=N   (slugs)
  Stats     : GET /api/portfolio/{slug}?period=N                     (overview)
  Tip list  : GET /api/portfolio/{slug}/tips/completed?skip=N        (references)
  Rich tip  : GET /tipster/{slug}/tips/{reference}  -> parse embedded JSON island
"""
from __future__ import annotations
import os, sys, time, json, datetime as dt
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client, Client
from racing_filter import is_uk_irish_horse_racing, parse_fixture

# --------------------------------------------------------------------------- #
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

BASE          = os.environ.get("TIPSTRR_BASE", "https://tipstrr.com")
SPORT_ID      = int(os.environ.get("SPORT_ID", "2"))
SPORT_NAME    = os.environ.get("SPORT_NAME", "horse-racing")
PERIODS       = [int(p) for p in os.environ.get("PERIODS", "1,3,6,12").split(",")]
DISC_PERIODS  = [int(p) for p in os.environ.get("DISCOVERY_PERIODS", "1,3,6,12").split(",")]
PORTFOLIO_TYPES = [int(t) for t in os.environ.get("PORTFOLIO_TYPES", "0,1,2").split(",")]
DISC_MAX_PAGES = int(os.environ.get("DISCOVERY_MAX_PAGES", "80"))
REQ_DELAY     = float(os.environ.get("REQUEST_DELAY", "0.9"))
PAGE_SIZE     = int(os.environ.get("PAGE_SIZE", "10"))
MAX_PAGES     = int(os.environ.get("MAX_PAGES", "800"))     # completed-feed cap
FULL_BACKFILL = os.environ.get("FULL_BACKFILL", "0") == "1"
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "0"))   # 0 = no age limit (all)
SKIP_STATS    = os.environ.get("SKIP_STATS", "0") == "1"
MAX_RUNTIME_MIN = float(os.environ.get("MAX_RUNTIME_MIN", "0"))  # 0 = unlimited
ONLY_SLUGS    = [s for s in os.environ.get("ONLY_SLUGS", "").split(",") if s]
SHARD_INDEX   = int(os.environ.get("SHARD_INDEX", "0"))   # this job's slice
SHARD_COUNT   = int(os.environ.get("SHARD_COUNT", "1"))   # total slices (parallel jobs)
UK_IRE_ONLY   = os.environ.get("UK_IRE_ONLY", "1") == "1"        # store GB/IRE horse racing only
OVERLAP_DAYS  = int(os.environ.get("DAILY_OVERLAP_DAYS", "3"))   # daily re-check window (days)

RESULT_MAP = {1: "won", 2: "half-won", 3: "lost", 4: "half-lost", 5: "void"}

# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=6, connect=5, read=5, backoff_factor=2.0,
                  status_forcelist=(403, 429, 500, 502, 503, 504),
                  allowed_methods=("GET",), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        # Browser UA + headers: tipstrr fronted the API with Cloudflare, which
        # blocks non-browser user-agents (the old bot UA started returning 403).
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": BASE + "/",
    })
    return s

HTTP = make_session()
DB: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_json(path: str, **params) -> Any:
    r = HTTP.get(f"{BASE}{path}", params=params, timeout=30,
                 headers={"Accept": "application/json"})
    time.sleep(REQ_DELAY)
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} {params} -> {r.status_code}")
    return r.json()


# --- tipstrr embeds its data as an HTML-escaped JSON island; decode + parse --
def _ng_unescape(x: str) -> str:
    for a, b in (("&q;", '"'), ("&s;", "'"), ("&l;", "<"),
                 ("&g;", ">"), ("&b;", "\\"), ("&n;", "\n")):
        x = x.replace(a, b)
    return x.replace("&a;", "&")

def get_state(path: str) -> dict:
    r = HTTP.get(f"{BASE}{path}", timeout=30, headers={"Accept": "text/html"})
    time.sleep(REQ_DELAY)
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} -> {r.status_code}")
    h = r.text
    i = h.find("tipstrr-state")
    gt = h.find(">", i)
    end = h.find("</script>", gt)
    return json.loads(_ng_unescape(h[gt + 1:end]))


# --------------------------------------------------------------------------- #
# Discovery (full paginated browse, union across period x type, losers incl.)
# --------------------------------------------------------------------------- #
def _crawl_browse(period: int, ptype: int, profit: int, roi: int) -> set[str]:
    slugs: set[str] = set()
    page = 1
    while page <= DISC_MAX_PAGES:
        try:
            arr = get_json("/api/portfolio", filterType=0, sportId=SPORT_ID,
                           profit=profit, roi=roi, tips=45, minOdds=1.2,
                           maxOdds=1000, portfolioType=ptype, order=0,
                           period=period, page=page)
        except Exception:
            break
        if not arr:
            break
        slugs.update(x for x in arr if isinstance(x, str))
        if len(arr) < 15:
            break
        page += 1
    return slugs

def discover_slugs() -> set[str]:
    found: set[str] = set()
    for period in DISC_PERIODS:
        for ptype in PORTFOLIO_TYPES:
            s = _crawl_browse(period, ptype, profit=-5000, roi=-100)
            if not s:
                s = _crawl_browse(period, ptype, profit=0, roi=0)
            found |= s
    return found

def known_slugs() -> set[str]:
    out, page = set(), 0
    while True:
        rows = (DB.table("tipsters").select("slug").eq("sport", SPORT_NAME)
                  .range(page*1000, page*1000+999).execute().data)
        out.update(r["slug"] for r in rows)
        if len(rows) < 1000:
            break
        page += 1
    return out


# --------------------------------------------------------------------------- #
# Per-tipster: profile + rolling stats
# --------------------------------------------------------------------------- #
def upsert_tipster(slug: str, profile: dict, is_new: bool) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    row = {"slug": slug, "name": profile.get("name"), "type": profile.get("type"),
           "sport": SPORT_NAME, "date_created": profile.get("dateCreated"),
           "active": profile.get("active"), "has_results": profile.get("hasResults"),
           "last_seen": now, "updated_at": now}
    if is_new:
        row["first_seen"] = now
    DB.table("tipsters").upsert(row, on_conflict="slug").execute()

def capture_stats(slug: str) -> int:
    captured_on = dt.datetime.now(dt.timezone.utc).date().isoformat()
    rows = []
    for period in PERIODS:
        try:
            ov = (get_json(f"/api/portfolio/{slug}", period=period).get("overview") or {})
        except Exception as e:
            print(f"  ! stats {slug}/{period}: {e}", file=sys.stderr); continue
        rows.append({"captured_on": captured_on, "slug": slug, "period": period,
                     "tips": ov.get("tips"), "roi": ov.get("roi"),
                     "profit": ov.get("profit"), "level_stake_roi": ov.get("levelStakeROI"),
                     "level_stake_profit": ov.get("levelStakeProfit"),
                     "win_percentage": ov.get("winPercentage"),
                     "avg_odds": ov.get("oddsSingles"), "raw": ov})
    if rows:
        DB.table("tipster_stats").upsert(rows, on_conflict="captured_on,slug,period").execute()
    return len(rows)


# --------------------------------------------------------------------------- #
# Per-tipster: reference list (cheap) + rich per-tip parse
# --------------------------------------------------------------------------- #
def _parse_dt(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None

def list_references(slug: str, stop_known: set[str], since: dt.datetime | None = None) -> list[dict]:
    """Newest-first list of completed tips.

    * BACKFILL_DAYS > 0  -> walk down until tips are older than the window
      cutoff, returning everything inside the window (ignores the known-stop so
      we can reach older, not-yet-stored tips below the recent ones).
    * else (daily)       -> stop once we hit a page we've already stored.
    """
    cutoff = None
    if BACKFILL_DAYS > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=BACKFILL_DAYS)
    refs, skip = [], 0
    for _ in range(MAX_PAGES):
        page = get_json(f"/api/portfolio/{slug}/tips/completed", skip=skip)
        if not page:
            break
        if cutoff is not None:
            in_window = [x for x in page if (_parse_dt(x.get("date")) or cutoff) >= cutoff]
            refs.extend(in_window)
            if len(in_window) < len(page):        # crossed the cutoff -> done
                break
        elif since is not None:
            # Daily: stop once tips fall older than the recent overlap window.
            # Robust to skipped (non-GB/IRE) refs that are never stored.
            in_window = [x for x in page if (_parse_dt(x.get("date")) or since) >= since]
            refs.extend(in_window)
            if len(in_window) < len(page):
                break
        else:
            refs.extend(page)
            if (not FULL_BACKFILL and stop_known
                    and skip > 0 and {x["reference"] for x in page} <= stop_known):
                break
        if len(page) < PAGE_SIZE:
            break
        skip += len(page)
    return refs

_BET_KIND = {"double": "double", "treble": "treble", "trixie": "trixie",
             "patent": "patent", "yankee": "yankee", "lucky": "lucky",
             "accumulator": "accumulator", "fold": "accumulator"}

def _bet_kind(ref: str, n_legs: int) -> str:
    low = ref.lower()
    for token, kind in _BET_KIND.items():
        if token in low:
            return kind
    return "single" if n_legs <= 1 else "multiple"

def parse_tip(slug: str, ref: str) -> dict | None:
    d = get_state(f"/tipster/{slug}/tips/{ref}")
    cache = d.get("PORTFOLIO_TIP_CACHED", {})
    rec = cache.get(f"{slug}_{ref}") or next((v for k, v in cache.items() if ref in k), None)
    if not rec:
        return None
    bets = rec.get("tipBet") or []
    items = rec.get("tipBetItem") or []
    fixtures = d.get("FIXTURE", {})
    b0 = bets[0] if bets else {}
    legs = []
    for idx, it in enumerate(items):
        horse = it.get("participant") or {}
        fx = fixtures.get(it.get("fixtureReference"), {}) or {}
        parts = fx.get("participants", []) if isinstance(fx, dict) else []
        winner = next((p.get("name") for p in parts if str(p.get("position")) == "1"), None)
        pos = next((p.get("position") for p in parts if p.get("name") == horse.get("name")), None)
        fxref = it.get("fixtureReference") or ""
        fxinfo = parse_fixture(fxref)            # correct date/time/course (fixes column-shift bug)
        course = fxinfo["course_slug"]
        rtime = fxinfo["time"]
        legs.append({
            "leg_index": idx, "fixture_reference": fxref or None,
            "course": course, "race_time": rtime,
            "horse": it.get("betText") or horse.get("name"),
            "jockey": horse.get("jockey"), "trainer": horse.get("trainer"),
            "advised_odds": it.get("createdOdds"), "bsp": it.get("bsp"),
            "rule4": it.get("rule4"), "best_odds_guaranteed": it.get("bestOddsGuaranteed"),
            "leg_result_code": it.get("result"),
            "leg_outcome": RESULT_MAP.get(it.get("result"), "other"),
            "finish_position": pos, "non_runner": horse.get("nonRunner"),
            "winner_horse": winner, "raw": it,
        })
    accept = bool(legs) and all(
        is_uk_irish_horse_racing(fixture_reference=lg["fixture_reference"])[0] for lg in legs
    )
    return {"bets": bets, "headline": b0, "legs": legs, "accept": accept}

def store_tip(slug: str, meta: dict, parsed: dict | None) -> int:
    ref = meta["reference"]
    head = (parsed or {}).get("headline", {}) if parsed else {}
    legs = (parsed or {}).get("legs", []) if parsed else []
    code = meta.get("result")
    tip_row = {
        "reference": ref, "slug": slug, "posted_at": meta.get("date"),
        "bet_type": meta.get("type"), "bet_kind": _bet_kind(ref, len(legs)),
        "n_selections": len(legs) or None,
        "stake_points": head.get("stake"), "advised_odds": head.get("odds"),
        "profit_points": head.get("profit"), "bsp_profit_points": head.get("bspProfit"),
        "result_code": code, "outcome": RESULT_MAP.get(code, "other"),
        "free": meta.get("free"), "raw": meta,
    }
    DB.table("tips").upsert(tip_row, on_conflict="reference").execute()
    if legs:
        for lg in legs:
            lg["tip_reference"] = ref; lg["slug"] = slug
        DB.table("tip_legs").upsert(legs, on_conflict="tip_reference,leg_index").execute()
    return len(legs)


# --------------------------------------------------------------------------- #
def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    run_id = DB.table("scrape_runs").insert({"status": "running"}).execute().data[0]["id"]
    errors = tips_up = legs_up = stats_up = skipped = 0
    notes: list[str] = []

    known = known_slugs()
    discovered = set(ONLY_SLUGS) if ONLY_SLUGS else discover_slugs()
    targets = sorted(discovered | known)
    if SHARD_COUNT > 1:
        targets = [t for i, t in enumerate(targets) if i % SHARD_COUNT == SHARD_INDEX]
    new_slugs = (discovered - known) & set(targets)
    print(f"Discovered {len(discovered)}, new {len(new_slugs)}, tracking {len(targets)}.")

    _t0 = time.monotonic()
    stopped_early = False
    for n, slug in enumerate(targets, 1):
        if MAX_RUNTIME_MIN and (time.monotonic() - _t0) / 60 >= MAX_RUNTIME_MIN:
            stopped_early = True
            notes.append(f"time budget {MAX_RUNTIME_MIN}min reached at {n}/{len(targets)}")
            print(f"Time budget reached at {n}/{len(targets)} — stopping cleanly.")
            break
        try:
            profile = get_json(f"/api/portfolio/{slug}")
            upsert_tipster(slug, profile, is_new=(slug in new_slugs))
            if not SKIP_STATS:
                stats_up += capture_stats(slug)

            # all references we already have for this slug (so restarts are cheap)
            existing = set()
            since = None
            if not FULL_BACKFILL:
                pg = 0
                while True:
                    rows = (DB.table("tips").select("reference").eq("slug", slug)
                              .range(pg*1000, pg*1000+999).execute().data)
                    existing.update(r["reference"] for r in rows)
                    if len(rows) < 1000:
                        break
                    pg += 1
                # newest stored tip -> only walk back a small overlap window each day
                latest = (DB.table("tips").select("posted_at").eq("slug", slug)
                            .order("posted_at", desc=True).limit(1).execute().data)
                if latest and latest[0].get("posted_at"):
                    _lp = _parse_dt(latest[0]["posted_at"])
                    if _lp:
                        since = _lp - dt.timedelta(days=OVERLAP_DAYS)

            for meta in list_references(slug, existing, since):
                ref = meta["reference"]
                if ref in existing and not FULL_BACKFILL:
                    continue                      # already have the rich detail
                try:
                    parsed = parse_tip(slug, ref)
                except Exception as e:
                    parsed = None
                    notes.append(f"{ref}: parse {e}")
                if UK_IRE_ONLY and not (parsed and parsed.get("accept")):
                    skipped += 1              # football / overseas / unparseable -> not stored
                    continue
                legs_up += store_tip(slug, meta, parsed)
                tips_up += 1
            if n % 10 == 0:
                print(f"  ...{n}/{len(targets)} (tips+={tips_up})")
        except Exception as e:
            errors += 1; notes.append(f"{slug}: {e}")
            print(f"  ! {slug}: {e}", file=sys.stderr)

    status = "ok" if errors == 0 else ("partial" if errors < len(targets) else "failed")
    DB.table("scrape_runs").update({
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(), "status": status,
        "tipsters_seen": len(targets), "new_tipsters": len(new_slugs),
        "tips_upserted": tips_up, "legs_upserted": legs_up, "stats_upserted": stats_up,
        "errors": errors, "notes": {"new_slugs": sorted(new_slugs)[:200],
                                    "errors": notes[:50], "skipped_non_uk_ire": skipped},
    }).eq("id", run_id).execute()
    dur = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    print(f"Done {dur:.0f}s status={status} tips+={tips_up} legs+={legs_up} "
          f"stats+={stats_up} skipped={skipped} new={len(new_slugs)} errors={errors}")
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
