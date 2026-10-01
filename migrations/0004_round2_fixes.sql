-- ═══════════════════════════════════════════════════════════════════════════
-- EXCEED BOX — Postgres migration 0004
--
-- FABLE-AUDIT-R2.md fixes: N4 (score threshold crossings, so
-- /api/scoring/model stops full-scanning every live lead twice to answer
-- "crossed this week"). Additive/corrective only — 0001-0003 are left
-- untouched.
--
-- N1/N2/N3/N8 in that audit are code-only fixes (app/auth.py, app/api.py,
-- app/scoring.py, app/tasks.py) and need no schema change — app_user
-- already has supabase_uid/email, and leads.score/score_updated_at already
-- exist from 0003.
--
-- Run with:  supabase db push
--       or:  psql "$DATABASE_URL" -f migrations/0004_round2_fixes.sql
-- ═══════════════════════════════════════════════════════════════════════════

-- ── N4: score threshold crossing history ──────────────────────────────────
-- scoring.recompute_lead() (app/scoring.py) — the one place leads.score is
-- ever written, whether by record()/void(), sweep_decay(), or N3's lazy
-- self-heal on read — writes one row here whenever a recompute moves a
-- lead's score across `threshold` in either direction. /api/scoring/model
-- then answers "how many crossed up this week" with one indexed query
-- against this table instead of scoring every live lead twice (now +
-- week-ago) in a Python loop.
CREATE TABLE IF NOT EXISTS score_threshold_crossings (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id      BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  crossed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  direction    TEXT NOT NULL CHECK (direction IN ('up','down')),
  threshold    INTEGER NOT NULL,
  score_before INTEGER NOT NULL,
  score_after  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_score_crossings_lead ON score_threshold_crossings(lead_id);
CREATE INDEX IF NOT EXISTS idx_score_crossings_at   ON score_threshold_crossings(direction, crossed_at);
