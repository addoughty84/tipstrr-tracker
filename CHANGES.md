# Changes — UK/Ireland horse-racing-only filter + parse fix

## What was wrong

1. **Column-shift parse bug** (`scraper.py`, `parse_tip`). For a fixtureReference
   like `2026-06-08-0645-canterbury-3ad86`, the old code did
   `course = fxref.split("-")[3]` (→ `0645`, the *time*) and
   `race_time = ...[2]` (→ `08`, the *day*). The real course (`canterbury`) was
   dropped. Result: `tip_legs.course` held a time and `race_time` held a day,
   so those legs could never match the Racing API.

2. **No sport/region filter.** Horse-racing tipsters also post football and
   overseas tips. Those legs were stored too, polluting `tips`/`tip_legs` and
   skewing tipster ROI/strike-rate. Across all history: ~2,811 football +
   ~1,054 overseas + ~375 unparseable legs vs ~44,102 genuine GB/IRE legs.

## What changed

- **New `racing_filter.py`** — a tested, dependency-free module that decides
  whether a leg is GB/IRE horse racing, using a course whitelist derived from
  the Racing API's own GB/IRE course names (so spellings match the results feed).
  Football is detected by the `-vs-` fixtureReference shape; overseas racing by
  course not in the whitelist.

- **`scraper.py`**
  - `parse_tip` now parses `fixtureReference` correctly via
    `parse_fixture(...)` → `course` is the real course slug, `race_time` is
    proper `HH:MM`. (Bug #1 fixed.)
  - Each parsed tip gets an `accept` flag = *every* leg is GB/IRE racing.
  - The main loop skips storing any tip that isn't GB/IRE racing
    (football / overseas / unparseable) → nothing non-UK/IRE is added. (Bug #2.)
  - Daily `list_references` now stops based on a date overlap window
    (`DAILY_OVERLAP_DAYS`, default 3) instead of "first fully-known page". This
    keeps daily runs cheap even though skipped (non-stored) refs would otherwise
    defeat the old page-based stop.

## New environment variables (both optional)

| Var | Default | Meaning |
|-----|---------|---------|
| `UK_IRE_ONLY` | `1` | `1` = store GB/IRE horse racing only. Set `0` for old behaviour. |
| `DAILY_OVERLAP_DAYS` | `3` | Days of recent tips a daily run re-checks before stopping. |

No schema change. No new dependencies.

## Deploy

1. Commit `racing_filter.py`, the updated `scraper.py`, `test_racing_filter.py`
   and `sql/cleanup_non_uk_ire.sql`.
2. The existing GitHub Action picks up `scraper.py` as-is; defaults mean the
   filter is on automatically. Add `UK_IRE_ONLY` / `DAILY_OVERLAP_DAYS` to the
   workflow env only if you want non-default values.
3. Run the tests: `python test_racing_filter.py` (18 cases, all from real data).

## Clean up existing data (optional)

`sql/cleanup_non_uk_ire.sql` previews then deletes the historical football/
overseas legs and any tips left with no legs. **Back up first**, run the
preview, then uncomment the DELETE block.
