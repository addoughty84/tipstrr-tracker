# Tipstrr Hot/Cold Tracker (rich, auth-free)

A bulletproof daily pipeline that downloads **every horse-racing tipster's full
settled-tip detail** from tipstrr.com into Supabase, so you can see who's hot,
who's cold, and analyse the actual picks (horse, odds, profit, finishing
position) against your own racing data.

No Lovable. No flaky cron. **No login required.**

## Why this is bulletproof (what Lovable got wrong)
- **No auth.** Everything comes from public pages/endpoints — there's no session
  cookie to expire, the #1 cause of dead scrapers.
- **Idempotent.** Every tip/leg/stat upserts on tipstrr's own unique ids, so
  re-runs, double-runs and missed days self-heal. No duplicates, ever, no gaps.
- **Incremental.** Daily runs only fetch the detail page for *new* tips, so
  they're quick. A one-time `FULL_BACKFILL` pulls all history.
- **Fault-isolated & logged.** One tipster failing never aborts the run; every
  run is recorded in `scrape_runs`.

## What it captures
| Table | Contents |
|------|----------|
| `tipsters` | every tipster ever seen (`first_seen`/`last_seen` flag new ones) |
| `tips` | one row per settled tip: kind (single/treble/trixie…), stake (pts), advised odds, profit (pts), BSP profit, outcome |
| `tip_legs` | one row per selection: **horse, jockey, trainer, advised odds, BSP, Rule-4, BOG, result, finishing position, and the race's actual winner** |
| `tipster_stats` | daily ROI/profit/win% snapshot per 1/3/6/12-month window — the hot/cold engine |
| `scrape_runs` | audit log of every run |

Result codes: 1 won, 2 half-won, 3 lost, 4 half-lost, 5 void/non-runner.
Stakes & profit are in **points** (advised units); your £ = points × your stake.

## How the data is obtained
- **Discover** tipsters: paginated browse list, unioned across period × type
  (~130–140 HR tipsters; anyone seen is kept forever so coverage only grows).
- **Stats**: `/api/portfolio/{slug}?period=N`.
- **Tip references**: `/api/portfolio/{slug}/tips/completed?skip=N` (full history).
- **Rich detail**: each tip's own page `/tipster/{slug}/tips/{reference}` embeds a
  JSON data island that the scraper decodes — odds, profit, horse, connections,
  fixture result. One lightweight page per tip.

## Setup (~15 min, once)
1. **Supabase** → SQL Editor → paste `schema.sql` → Run. Then Settings → API,
   copy the **Project URL** and **`service_role`** key.
2. **GitHub** → new repo with these files → Settings → Secrets → Actions:
   `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
3. **First run = staged backfill** (freshest-first). On a machine that stays
   awake (your PC or a small VPS), run:
   ```bash
   export SUPABASE_URL=...  SUPABASE_SERVICE_KEY=...
   python backfill.py
   ```
   This sweeps ALL tipsters in widening windows — **10 → 30 → 60 → 90 → 180 days
   → lifetime** — so the latest results land first and depth fills in behind
   them. It can take days; that's fine. It's idempotent, so if it stops just run
   it again and finished stages fly past. (Don't run this inside one GitHub
   Action — they cap at ~6h. This is a run-it-once-on-a-server job.)
4. After backfill, the **daily GitHub Action** at **23:30 UTC** keeps it current
   (incremental, fast — only new tips, which always land on page 1).

GitHub emails you automatically if a scheduled run fails; the scraper exits
non-zero on total failure to trigger that.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env          # add Supabase URL + service key
set -a; source .env; set +a
FULL_BACKFILL=1 ONLY_SLUGS=money-bags python scraper.py   # test one tipster
```

## Asking the data questions
```sql
-- who's hottest right now (recent vs long-term ROI gap)
select * from v_hot_cold where tips_1mo >= 10 order by heat_1_vs_12 desc limit 25;

-- a tipster's last-14-day form
select outcome, count(*) from tips
where slug='money-bags' and posted_at > now() - interval '14 days'
group by outcome;

-- every horse a tipster tipped, with result and finishing position
select t.posted_at, l.horse, l.jockey, l.advised_odds, l.leg_outcome,
       l.finish_position, l.winner_horse
from tip_legs l join tips t on t.reference=l.tip_reference
where l.slug='money-bags' order by t.posted_at desc limit 50;

-- strongest tipsters at a given course / odds band, etc. (join to your racing data)
```

## Tuning / notes
- Discovery scope: `DISCOVERY_PERIODS`, `PORTFOLIO_TYPES`. Inactive tipsters with
  no recent results sit outside the browse filters but are picked up the moment
  they post again.
- `REQUEST_DELAY` (default 0.4s) throttles requests — keep it polite.
- Personal research tool; data is tipstrr's, use within their terms.
  Past performance ≠ future results. Bet responsibly.
