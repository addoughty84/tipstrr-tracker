# Edge Engine — Phase 1 (backtest + forward paper-trading)

An automated, always-on system that finds betting edges in your tipster + Racing
API data, registers them, and **paper-trades them forward** on data they were
never fitted to — so you can tell real edges from lucky backtests *before* any
real money is involved. Runs hands-off on GitHub Actions.

## What's in here

| File | Role |
|---|---|
| `engine.py` | Shared core: turns an edge *rule* into a SQL selection, prices the bets, computes metrics (ROI, win%, t-stat, drawdown, concentration). The single source of truth used by both backtester and settler. |
| `scanner.py` | **The edge finder.** Generates candidate edges, backtests each on a *training* slice, confirms survivors on a *held-out* slice, registers the ones that pass. Idempotent (rule-hashed ids). Runs weekly. |
| `settler.py` | **The daily heartbeat.** For each active edge, records the new bets its rule makes, settles them from `ra_results`, updates the forward record, prints a digest + promote/retire flags. Runs daily. |
| `schema.sql` | Creates the `edges` and `edge_bets` tables. |
| `workflows/` | The two GitHub Actions (`edge-daily.yml`, `edge-weekly.yml`). |

## How it works (one paragraph)

An **edge** is a machine-readable rule (e.g. *"back any horse ≥2 of these 8
tipsters tip, excluding All-Weather"*). The scanner only discovers on older
history and must confirm on newer history it couldn't see — anything that looks
great in training but fades on the holdout is binned automatically. Survivors
are **frozen** with a timestamp and set to `paper`. From then on the daily
settler applies each frozen rule to newly-settled tips, records the hypothetical
bet, pulls the result, and banks the P&L — building a genuine forward record on
data that didn't exist when the edge was minted. Nothing here touches real
money; promotion to live is a manual, human-approved step later.

## One-time setup

1. **Tables** — already created in your Supabase. (To recreate elsewhere, run
   `schema.sql` in the SQL editor.)
2. **Secret** — in GitHub: repo **Settings → Secrets and variables → Actions →
   New repository secret**, name `SUPABASE_DB_URL`, value:
   ```
   postgresql://postgres.kotktltdhrfbcjqgvyei:YOUR-DB-PASSWORD@aws-0-eu-west-1.pooler.supabase.com:6543/postgres
   ```
   (same DB password you use elsewhere — the transaction-pooler string.)
3. **Files** — put `engine.py`, `scanner.py`, `settler.py`, `requirements.txt`,
   `schema.sql` in a folder called `edge/` at the repo root, and copy the two
   files from `workflows/` into `.github/workflows/`.
4. Commit. The daily settler runs at 08:00 UTC, the weekly scanner Mondays
   06:00 UTC. Both have a manual **Run workflow** button too.

## Edges currently registered (frozen today — forward record starts now)

- **Consensus ≥2 of the 8** — back any horse ≥2 of the stable tip.
- **Consensus ≥2, no All-Weather** — same, dropping the AW leak. *(strongest)*
- **Solo: CA BETS, no All-Weather** — all CA BETS singles except AW.

The stable of 8: CA BETS, She's the fastest, On Target Tips, The Profit Rocket,
ACTIVE Betting Hub, Model Man, Equii Tensor, RaceBot UK.

## Watching it

Everything lives in the `edges` and `edge_bets` tables, so you can read it from
the Supabase dashboard, ask Claude any time ("how are the edges doing?"), or
point a dashboard at it. The settler also prints a digest each run (visible in
the Actions log) flagging edges *ready to review for live* or *to retire*.

## Adjusting

- **Gates** (sample size, t-stat bar, concentration limit, holdout window) live
  at the top of `scanner.py`.
- **Promote / retire thresholds** live at the top of `settler.py`.
- **New candidate families** — extend `gen_candidates()` in `scanner.py`. This
  is where Phase 2 (LLM-proposed and web-researched angles) will plug in: it
  just feeds more candidate rules into the same disciplined train/holdout/paper
  pipeline.

## The golden rule

A backtest is almost always profitable — that's the trap, not the proof. **Trust
the forward paper record, not the backtest.** An edge earns real money only after
it holds up on bets placed *after* it was frozen.
