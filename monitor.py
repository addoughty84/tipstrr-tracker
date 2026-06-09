#!/usr/bin/env python3
"""
Staleness monitor for the Tipstrr tracker.

Reads the most recent row in `scrape_runs` and FAILS (exit 1) if the pipeline
looks unhealthy — which makes the GitHub Action fail, and GitHub emails you.
This catches the gap GitHub's own failure emails miss: a pipeline that runs
"successfully" but quietly stops producing data.

Healthy = latest run finished within STALE_HOURS, status not 'failed',
and it wrote at least some tips or stats.
"""
import os, sys, datetime as dt
import requests

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
STALE_HOURS = float(os.environ.get("STALE_HOURS", "30"))

h = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
r = requests.get(f"{URL}/rest/v1/scrape_runs",
                 params={"select": "*", "order": "started_at.desc", "limit": "1"},
                 headers=h, timeout=30)
r.raise_for_status()
rows = r.json()

problems = []
if not rows:
    problems.append("no scrape_runs rows exist yet")
else:
    run = rows[0]
    fin = run.get("finished_at")
    status = run.get("status")
    produced = (run.get("tips_upserted") or 0) + (run.get("stats_upserted") or 0)
    if not fin:
        problems.append("latest run never finished (still running or crashed)")
    else:
        age_h = (dt.datetime.now(dt.timezone.utc)
                 - dt.datetime.fromisoformat(fin.replace("Z", "+00:00"))
                 ).total_seconds() / 3600
        if age_h > STALE_HOURS:
            problems.append(f"latest run finished {age_h:.1f}h ago (> {STALE_HOURS}h limit)")
    if status == "failed":
        problems.append("latest run status = failed")
    if produced == 0:
        problems.append("latest run wrote 0 tips and 0 stats")

if problems:
    print("STALENESS ALERT — tipstrr pipeline may have stopped:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)

print(f"OK: pipeline healthy (latest run within {STALE_HOURS}h, data flowing).")
