-- ═══════════════════════════════════════════════════════════════════════════
-- EXCEED BOX — Postgres migration 0002 (SPEC-V2 additions)
--
-- Additive only — 0001_init.sql is left untouched (it may already be applied
-- to a real database). Same dialect conventions as 0001: GENERATED ALWAYS AS
-- IDENTITY, TIMESTAMPTZ, real BOOLEAN, UUID app_user references.
--
-- Run with:  supabase db push
--       or:  psql "$DATABASE_URL" -f migrations/0002_v2.sql
-- ═══════════════════════════════════════════════════════════════════════════

-- ── leads: business-card image path (SPEC-V2 §6) ────────────────────────────
ALTER TABLE leads ADD COLUMN IF NOT EXISTS card_image_path TEXT;

-- ── sequence_steps: the question each step is really asking (SPEC-V2 §2) ────
ALTER TABLE sequence_steps ADD COLUMN IF NOT EXISTS question TEXT;
ALTER TABLE sequence_steps ADD COLUMN IF NOT EXISTS question_ja TEXT;

-- ── sends: "nobody ever receives the same email twice", enforced at the DB ──
ALTER TABLE sends ADD CONSTRAINT sends_lead_step_unique UNIQUE (lead_id, step_id);

-- ── imports: the two-phase analyze/commit wizard (SPEC-V2 §7) ───────────────
ALTER TABLE imports ADD COLUMN IF NOT EXISTS token TEXT UNIQUE;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS file_path TEXT;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'analyzed'
  CHECK (status IN ('analyzed','committed'));
ALTER TABLE imports ADD COLUMN IF NOT EXISTS headers_json JSONB;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS mapping_json JSONB;
ALTER TABLE imports ADD COLUMN IF NOT EXISTS imported_by_user_id UUID REFERENCES app_user(id);
ALTER TABLE imports ADD COLUMN IF NOT EXISTS committed_at TIMESTAMPTZ;

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §3 — SNS content patterns and the funnel they feed.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sns_patterns (
  key            TEXT PRIMARY KEY,
  title_ja       TEXT NOT NULL,
  title_en       TEXT NOT NULL,
  description_ja TEXT,
  description_en TEXT,
  active         BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS lead_sns_pattern (
  lead_id     BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  pattern_key TEXT NOT NULL REFERENCES sns_patterns(key),
  set_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (lead_id, pattern_key)
);

-- No analytics API is wired to any SNS platform — these two funnel steps have
-- nowhere in this codebase they could be computed live. Recorded explicitly,
-- tagged 'manual' in the API response, never invented or silently omitted.
CREATE TABLE IF NOT EXISTS sns_funnel_manual (
  step_key   TEXT PRIMARY KEY CHECK (step_key IN ('sns_post','lp_view')),
  count      INTEGER NOT NULL DEFAULT 0,
  note       TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §4 — booking settings and the calendar events booking produces.
-- No public booking page and no Google Calendar sync exist.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS booking_settings (
  id                    BIGINT PRIMARY KEY CHECK (id = 1),
  jst_gst_gap_hours     INTEGER NOT NULL DEFAULT 4,
  business_hours_start  TEXT NOT NULL DEFAULT '09:00',
  business_hours_end    TEXT NOT NULL DEFAULT '18:00',
  slot_length_minutes   INTEGER NOT NULL DEFAULT 30,
  timezone              TEXT NOT NULL DEFAULT 'Asia/Tokyo',
  note                  TEXT,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS booking_meeting_types (
  key              TEXT PRIMARY KEY,
  label_en         TEXT NOT NULL,
  label_ja         TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_rep_meeting_types (
  user_id      UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  meeting_type TEXT NOT NULL REFERENCES booking_meeting_types(key),
  PRIMARY KEY (user_id, meeting_type)
);

CREATE TABLE IF NOT EXISTS calendar_events (
  id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_user_id      UUID REFERENCES app_user(id),
  lead_id            BIGINT REFERENCES leads(id) ON DELETE CASCADE,
  type               TEXT NOT NULL CHECK (type IN
                      ('consult_30','site_inspection','showroom_visit','other')),
  title              TEXT,
  starts_at          TIMESTAMPTZ NOT NULL,
  ends_at            TIMESTAMPTZ NOT NULL,
  status             TEXT NOT NULL DEFAULT 'booked'
                     CHECK (status IN ('booked','completed','cancelled')),
  created_by_user_id UUID REFERENCES app_user(id),
  source             TEXT NOT NULL DEFAULT 'internal',
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_calendar_owner_week ON calendar_events(owner_user_id, starts_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §5 — auto-assignment rules, evaluated in priority order.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assignment_rules (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  priority      INTEGER NOT NULL,
  match_field   TEXT CHECK (match_field IN ('region','purpose','language','topic') OR match_field IS NULL),
  match_value   TEXT,
  owner_user_id UUID NOT NULL REFERENCES app_user(id),
  is_fallback   BOOLEAN NOT NULL DEFAULT FALSE,
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_assignment_rules_priority ON assignment_rules(priority);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §10 — voice notes, push, and system integration status.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_notes (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id          BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  author_user_id   UUID REFERENCES app_user(id),
  audio_path       TEXT NOT NULL,
  duration_seconds DOUBLE PRECISION,
  transcript       TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lead_notes_lead ON lead_notes(lead_id, created_at);

CREATE TABLE IF NOT EXISTS push_tokens (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id    UUID NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  token      TEXT NOT NULL,
  platform   TEXT NOT NULL CHECK (platform IN ('ios','ipados','android','web')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, token)
);

CREATE TABLE IF NOT EXISTS push_settings (
  user_id                      UUID PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
  notify_new_lead              BOOLEAN NOT NULL DEFAULT TRUE,
  notify_hot_lead              BOOLEAN NOT NULL DEFAULT TRUE,
  notify_reply                 BOOLEAN NOT NULL DEFAULT TRUE,
  notify_task_escalation_level INTEGER NOT NULL DEFAULT 2,
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS integrations (
  key            TEXT PRIMARY KEY,
  label_en       TEXT NOT NULL,
  label_ja       TEXT NOT NULL,
  connected      BOOLEAN NOT NULL DEFAULT FALSE,
  status_detail  TEXT,
  what_is_needed TEXT,
  last_sync_at   TIMESTAMPTZ,
  record_count   INTEGER,
  read_only      BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §9 — inbound replies and the human-approved reply flow.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS replies (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id               BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  channel               TEXT NOT NULL DEFAULT 'email',
  subject               TEXT,
  body                  TEXT NOT NULL,
  received_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  status                TEXT NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received','sent','voided')),
  verdict_json          JSONB,
  human_reply_body      TEXT,
  scored_event_ids_json JSONB,
  sent_at               TIMESTAMPTZ,
  sent_by_user_id       UUID REFERENCES app_user(id),
  voided_at             TIMESTAMPTZ,
  voided_by_user_id     UUID REFERENCES app_user(id),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_replies_lead ON replies(lead_id, received_at);
