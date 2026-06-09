-- ============================================================================
-- Racing data (The Racing API) schema — runs in the SAME Supabase project.
-- Run once in Supabase SQL Editor. Safe to re-run.
-- Every endpoint's full JSON is also kept in a `raw` column so no field is lost.
-- ============================================================================

-- RACES (one row per race) ---------------------------------------------------
create table if not exists public.ra_races (
    race_id          text primary key,
    date             date,
    off_time         text,
    off_dt           timestamptz,
    course           text,
    course_id        text,
    region           text,
    race_name        text,
    race_class       text,
    type             text,            -- flat / hurdle / chase ...
    pattern          text,
    age_band         text,
    rating_band      text,
    sex_restriction  text,
    distance         text,
    distance_round   text,
    distance_f       text,
    prize            text,
    field_size       text,
    going            text,
    going_detailed   text,
    rail_movements   text,
    stalls           text,
    weather          text,
    surface          text,
    jumps            text,
    big_race         boolean,
    is_abandoned     boolean,
    tip              text,
    verdict          text,
    betting_forecast text,
    race_status      text,
    raw              jsonb,
    first_seen       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);
create index if not exists ra_races_date_idx   on public.ra_races (date);
create index if not exists ra_races_course_idx on public.ra_races (course_id);

-- RUNNERS (one row per horse per race) ---------------------------------------
create table if not exists public.ra_runners (
    race_id            text not null references public.ra_races(race_id) on delete cascade,
    horse_id           text not null,
    horse              text,
    number             text,
    draw               text,
    dob                text,
    age                text,
    sex                text,
    sex_code           text,
    colour             text,
    region             text,
    breeder            text,
    dam                text, dam_id text, dam_region text,
    sire               text, sire_id text, sire_region text,
    damsire            text, damsire_id text, damsire_region text,
    trainer            text, trainer_id text, trainer_location text, trainer_rtf text,
    trainer_14d_runs   text, trainer_14d_wins text, trainer_14d_percent text,
    owner              text, owner_id text,
    headgear           text, headgear_run text,
    wind_surgery       text, wind_surgery_run text,
    lbs                text,            -- weight carried
    ofr                text,            -- official rating
    rpr                text,            -- Racing Post Rating
    ts                 text,            -- Topspeed
    jockey             text, jockey_id text,
    silk_url           text,
    last_run           text,
    form               text,
    comment            text,
    spotlight          text,
    prev_trainers      jsonb,
    prev_owners        jsonb,
    quotes             jsonb,
    stable_tour        jsonb,
    medical            jsonb,
    past_results_flags jsonb,
    raw                jsonb,
    updated_at         timestamptz not null default now(),
    primary key (race_id, horse_id)
);
create index if not exists ra_runners_horse_idx   on public.ra_runners (horse_id);
create index if not exists ra_runners_trainer_idx on public.ra_runners (trainer_id);
create index if not exists ra_runners_jockey_idx  on public.ra_runners (jockey_id);

-- ODDS (one row per bookmaker price-point -> movement over time) --------------
-- odds_time = the timestamp that price became effective (from the API's per-
-- bookmaker history/updated). Upsert on it so repeated polls don't duplicate,
-- but every genuine price change is captured.
create table if not exists public.ra_odds (
    race_id     text not null,
    horse_id    text not null,
    bookmaker   text not null,
    odds_time   timestamptz not null,
    decimal     numeric,
    fractional  text,
    ew_places   text,
    ew_denom    text,
    captured_at timestamptz not null default now(),
    raw         jsonb,
    primary key (race_id, horse_id, bookmaker, odds_time)
);
create index if not exists ra_odds_race_horse_idx on public.ra_odds (race_id, horse_id);
create index if not exists ra_odds_time_idx       on public.ra_odds (odds_time);

-- RESULTS (race-level) -------------------------------------------------------
create table if not exists public.ra_results (
    race_id            text primary key,
    date               date,
    off                text,
    off_dt             timestamptz,
    course             text, course_id text, region text,
    race_name          text, type text, class text, pattern text,
    rating_band        text, age_band text, sex_rest text,
    dist text, dist_y text, dist_m text, dist_f text,
    going text, surface text, jumps text,
    winning_time_detail text,
    comments           jsonb,
    non_runners        text,
    tote_win text, tote_pl text, tote_ex text,
    tote_csf text, tote_tricast text, tote_trifecta text,
    raw                jsonb,
    scraped_at         timestamptz not null default now()
);
create index if not exists ra_results_date_idx on public.ra_results (date);

-- RESULT RUNNERS (per horse, post-race) --------------------------------------
create table if not exists public.ra_result_runners (
    race_id          text not null references public.ra_results(race_id) on delete cascade,
    horse_id         text not null,
    horse            text,
    position         text,
    sp               text,
    sp_dec           numeric,
    bsp              numeric,
    number           text,
    draw             text,
    btn              text,
    ovr_btn          text,
    age              text,
    sex              text,
    weight           text,
    weight_lbs       text,
    headgear         text,
    time             text,
    "or"             text,
    rpr              text,
    tsr              text,
    prize            text,
    jockey           text, jockey_id text, jockey_claim_lbs text,
    trainer          text, trainer_id text,
    owner            text, owner_id text,
    sire text, sire_id text, dam text, dam_id text, damsire text, damsire_id text,
    comment          text,
    silk_url         text,
    raw              jsonb,
    primary key (race_id, horse_id)
);
create index if not exists ra_result_runners_horse_idx on public.ra_result_runners (horse_id);

-- FETCH LOG ------------------------------------------------------------------
create table if not exists public.ra_fetch_runs (
    id            bigint generated always as identity primary key,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    kind          text,             -- racecards / results
    status        text,
    races         int default 0,
    runners       int default 0,
    odds_rows     int default 0,
    results       int default 0,
    errors        int default 0,
    notes         jsonb
);

-- ============================================================================
-- Helper views
-- ============================================================================

-- Odds movement per runner: first price, latest price, and drift/steam.
create or replace view public.v_ra_odds_movement as
with ranked as (
  select race_id, horse_id, bookmaker, decimal, odds_time,
         first_value(decimal) over w_asc  as first_dec,
         first_value(decimal) over w_desc as latest_dec
  from public.ra_odds
  where decimal is not null
  window w_asc  as (partition by race_id,horse_id,bookmaker order by odds_time asc),
         w_desc as (partition by race_id,horse_id,bookmaker order by odds_time desc)
)
select race_id, horse_id, bookmaker,
       min(first_dec)  as opening_dec,
       min(latest_dec) as current_dec,
       round((min(latest_dec) - min(first_dec)), 3)            as abs_move,
       round((min(latest_dec) - min(first_dec)) / nullif(min(first_dec),0) * 100, 1) as pct_move
from ranked
group by race_id, horse_id, bookmaker;

-- Best (shortest) current price per runner across all bookmakers.
create or replace view public.v_ra_best_current_odds as
select distinct on (race_id, horse_id)
       race_id, horse_id, bookmaker, decimal as best_dec, odds_time
from public.ra_odds
order by race_id, horse_id, decimal asc nulls last, odds_time desc;
