#!/usr/bin/env python3
"""
Staged, freshest-first backfill driver for the Tipstrr tracker.

Runs the scraper over ALL tipsters in widening time windows, in order:

    10 -> 30 -> 60 -> 90 -> 180 days -> lifetime

so the database always has the most recent results first, then fills in depth.
Each stage is idempotent: it only fetches the per-tip detail for tips it
doesn't already have, so:
  * later stages only add the newly-reachable older tips, and
  * if the process is interrupted, just run it again — finished stages fly past
    (already-stored tips are skipped) and it resumes where it left off.

Designed to run for as long as it takes (hours or days). Run it on a machine
that stays awake (your PC left on, or a small VPS) — NOT inside a single GitHub
Action (those cap at ~6h). The daily incremental run is what lives on GitHub
Actions; this is the one-time history fill.

Usage:
    pip install -r requirements.txt
    export SUPABASE_URL=...  SUPABASE_SERVICE_KEY=...
    python backfill.py
    # optional: override the stages
    STAGES="10,30,90,0" python backfill.py
"""
import os, sys, subprocess, time, datetime as dt

STAGES = [int(x) for x in os.environ.get("STAGES", "10,30,60,90,180,0").split(",")]
HERE = os.path.dirname(os.path.abspath(__file__))
SCRAPER = os.path.join(HERE, "scraper.py")

def label(days): return "lifetime" if days == 0 else f"{days} days"

def main():
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        if not os.environ.get(k):
            sys.exit(f"Missing required env var: {k}")
    budget = float(os.environ.get("MAX_RUNTIME_MIN", "0"))   # 0 = unlimited
    deadline = time.monotonic() + budget*60 if budget else None
    print(f"Backfill stages: {', '.join(label(d) for d in STAGES)}"
          + (f"  (budget {budget:.0f} min)" if budget else ""))
    for i, days in enumerate(STAGES):
        if deadline and time.monotonic() >= deadline:
            print("Time budget reached between stages — will resume next run.")
            return
        print(f"\n{'='*64}\n[{dt.datetime.now():%Y-%m-%d %H:%M}]  STAGE {i+1}/{len(STAGES)}: {label(days)}\n{'='*64}")
        env = dict(os.environ)
        env["BACKFILL_DAYS"] = str(days)
        env["FULL_BACKFILL"] = "0"            # incremental skip = cheap restarts
        env["SKIP_STATS"] = "0" if i == 0 else "1"   # snapshot stats once, on stage 1
        if deadline:
            remaining = (deadline - time.monotonic()) / 60
            if remaining <= 1:
                print("Time budget reached — will resume next run.")
                return
            env["MAX_RUNTIME_MIN"] = str(remaining)   # let the stage stop in time
        rc = subprocess.call([sys.executable, SCRAPER], env=env)
        if rc != 0:
            print(f"Stage '{label(days)}' exited with code {rc}. "
                  f"Re-run backfill.py to resume (finished work is skipped).")
            sys.exit(rc)
        print(f"Stage '{label(days)}' complete.")
    print("\nAll stages complete — full history captured. "
          "Switch to the daily GitHub Action for incremental updates.")

if __name__ == "__main__":
    main()
