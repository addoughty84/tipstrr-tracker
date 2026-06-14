-- ============================================================
--  Edge engine schema  (run once in the Supabase SQL editor)
--  Additive only - creates two new tables, touches nothing else.
-- ============================================================

CREATE TABLE IF NOT EXISTS edges (
  id            text PRIMARY KEY,                 -- hash of the rule
  name          text NOT NULL,
  description   text,
  rule          jsonb NOT NULL,                   -- machine-readable selection + staking
  status        text NOT NULL DEFAULT 'paper',    -- candidate | paper | live | retired
  frozen_at     timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  bt_train      jsonb,                            -- backtest metrics on training slice
  bt_holdout    jsonb,                            -- backtest metrics on held-out slice
  paper_n       int DEFAULT 0,                    -- forward (post-freeze) record:
  paper_roi     numeric,
  paper_win     numeric,
  paper_profit  numeric DEFAULT 0,
  paper_tstat   numeric,
  updated_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS edge_bets (
  edge_id     text NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
  race_id     text NOT NULL,
  horse_id    text NOT NULL,
  horse       text,
  posted_at   timestamptz,
  odds        numeric,
  stake       numeric,
  outcome     text,                               -- won | lost | void
  profit      numeric,
  settled     boolean DEFAULT true,
  created_at  timestamptz DEFAULT now(),
  PRIMARY KEY (edge_id, race_id, horse_id)
);

CREATE INDEX IF NOT EXISTS edge_bets_edge_idx     ON edge_bets(edge_id);
CREATE INDEX IF NOT EXISTS edge_bets_posted_idx   ON edge_bets(posted_at);
