-- ═══════════════════════════════════════════════════════════════════════════
-- EXCEED BOX — Postgres migration 0003
--
-- FABLE-AUDIT.md fixes: H1 (materialize leads.score) and the JST/GST
-- timezone bug (booking_settings.jst_gst_gap_hours was 4, should be 5).
-- Additive/corrective only — 0001/0002 are left untouched.
--
-- Run with:  supabase db push
--       or:  psql "$DATABASE_URL" -f migrations/0003_score_materialize_and_tz_fix.sql
-- ═══════════════════════════════════════════════════════════════════════════

-- ── H1: materialized score ────────────────────────────────────────────────
-- scoring.explain() (app/scoring.py) stays the authoritative computation;
-- app/scoring.py's record()/void() write this column on every event so
-- /api/dashboard, /api/team, /api/leads and hot-leads never recompute
-- score-for-every-lead in a Python loop again. scripts/sweep_decay.py covers
-- pure time-based decay (no new event).
ALTER TABLE leads ADD COLUMN IF NOT EXISTS score INTEGER NOT NULL DEFAULT 0;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS score_updated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC) WHERE merged_into IS NULL;

-- ── Timezone fix: JST↔GST gap was wrong ──────────────────────────────────
-- Tokyo (JST, UTC+9) and Dubai (GST, UTC+4) are FIVE hours apart, not four —
-- the decision log said four twice; verified wrong. Corrects both the
-- column default for any row created after this migration and any existing
-- row still carrying the old (wrong) value of 4.
ALTER TABLE booking_settings ALTER COLUMN jst_gst_gap_hours SET DEFAULT 5;
UPDATE booking_settings SET jst_gst_gap_hours = 5 WHERE jst_gst_gap_hours = 4;
