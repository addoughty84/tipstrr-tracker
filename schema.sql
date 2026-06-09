-- ============================================================================
-- Tipstrr Hot/Cold Tracker — Supabase schema (RICH mode)
-- Run once in Supabase: SQL Editor -> paste -> Run. Safe to re-run.
-- ============================================================================

-- 1) TIPSTERS -----------------------------------------------------------------
create table if not exists public.tipsters (
    slug          text primary key,
    name          text,
    type          int,
    sport         text default 'horse-racing',
    date_created  timestamptz,
    active        boolean,
    has_results   boolean,
    first_seen    timestamptz not null default now(),   -- when WE first saw them
    last_seen     timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

-- 2) TIPS ---------------------------------------------------------------------
-- One row per settled tip (single OR accumulator headline). "reference" is
-- tipstrr's unique id -> upsert on it => fully idempotent (no dupes, no gaps).
-- Stakes/profit are in POINTS (advised units). Your £ = points * your stake.
create table if not exists public.tips (
    reference        text primary key,
    slug             text not null references public.tipsters(slug) on delete cascade,
    posted_at        timestamptz,
    bet_type         int,                 -- tipstrr type code
    bet_kind         text,                -- single / double / treble / trixie / ...
    n_selections     int,
    stake_points     numeric,
    advised_odds     numeric,             -- combined odds for the bet
    profit_points    numeric,
    bsp_profit_points numeric,
    result_code      int,                 -- 1 won,2 half-won,3 lost,4 half-lost,5 void
    outcome          text,
    free             boolean,
    raw              jsonb,
    scraped_at       timestamptz not null default now()
);
create index if not exists tips_slug_idx      on public.tips (slug);
create index if not exists tips_posted_at_idx on public.tips (posted_at desc);
create index if not exists tips_outcome_idx   on public.tips (outcome);

-- 3) TIP_LEGS -----------------------------------------------------------------
-- One row per selection (singles have 1 leg; accumulators have several).
-- Carries the horse + connections + result + finishing position + race winner,
-- which is exactly what you join against horse-racing data to pick bets.
create table if not exists public.tip_legs (
    id                bigint generated always as identity primary key,
    tip_reference     text not null references public.tips(reference) on delete cascade,
    slug              text not null,
    leg_index         int  not null,
    fixture_reference text,              -- e.g. 2026-06-09-1430-southwell-99298
    course            text,
    race_time         text,
    horse             text,
    jockey            text,
    trainer           text,
    advised_odds      numeric,
    bsp               numeric,
    rule4             boolean,
    best_odds_guaranteed boolean,
    leg_result_code   int,
    leg_outcome       text,
    finish_position   int,
    non_runner        boolean,
    winner_horse      text,              -- who actually won that race
    raw               jsonb,
    unique (tip_reference, leg_index)
);
create index if not exists tip_legs_tip_idx    on public.tip_legs (tip_reference);
create index if not exists tip_legs_horse_idx  on public.tip_legs (horse);
create index if not exists tip_legs_fixture_idx on public.tip_legs (fixture_reference);

-- 4) TIPSTER_STATS ------------------------------------------------------------
-- Daily rolling-form snapshot per period window (the hot/cold engine).
create table if not exists public.tipster_stats (
    id                 bigint generated always as identity primary key,
    captured_on        date not null default (now() at time zone 'utc')::date,
    slug               text not null references public.tipsters(slug) on delete cascade,
    period             int  not null,     -- months: 1,3,6,12
    tips               int,
    roi                numeric,
    profit             numeric,
    level_stake_roi    numeric,
    level_stake_profit numeric,
    win_percentage     numeric,
    avg_odds           numeric,
    raw                jsonb,
    captured_at        timestamptz not null default now(),
    unique (captured_on, slug, period)
);
create index if not exists tipster_stats_slug_idx   on public.tipster_stats (slug);
create index if not exists tipster_stats_period_idx on public.tipster_stats (captured_on, period);

-- 5) SCRAPE_RUNS --------------------------------------------------------------
create table if not exists public.scrape_runs (
    id               bigint generated always as identity primary key,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    status           text,
    tipsters_seen    int default 0,
    new_tipsters     int default 0,
    tips_upserted    int default 0,
    legs_upserted    int default 0,
    stats_upserted   int default 0,
    errors           int default 0,
    notes            jsonb
);

-- ============================================================================
-- VIEWS
-- ============================================================================
create or replace view public.v_latest_stats as
select distinct on (slug, period)
       slug, period, captured_on, roi, profit, level_stake_roi,
       win_percentage, tips, avg_odds
from public.tipster_stats
order by slug, period, captured_on desc;

create or replace view public.v_hot_cold as
select s.slug, t.name,
       s1.roi as roi_1mo, s3.roi as roi_3mo, s12.roi as roi_12mo,
       round(coalesce(s1.roi,0)-coalesce(s12.roi,0),2) as heat_1_vs_12,
       s1.tips as tips_1mo
from (select distinct slug from public.v_latest_stats) s
join public.tipsters t on t.slug=s.slug
left join public.v_latest_stats s1  on s1.slug=s.slug and s1.period=1
left join public.v_latest_stats s3  on s3.slug=s.slug and s3.period=3
left join public.v_latest_stats s12 on s12.slug=s.slug and s12.period=12
order by heat_1_vs_12 desc nulls last;
