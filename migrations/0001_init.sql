-- ═══════════════════════════════════════════════════════════════════════════
-- EXCEED BOX — Postgres migration 0001 (initial schema)
--
-- A straight port of schema.sql (the SQLite test fixture) to real Postgres
-- DDL. Same tables, same decisions (D1..D24 inline), same constraints. The
-- differences are purely dialect: INTEGER PRIMARY KEY -> GENERATED ALWAYS AS
-- IDENTITY, TEXT timestamps -> TIMESTAMPTZ, SQLite's boolean-as-INTEGER ->
-- real BOOLEAN, TEXT uuid -> UUID with gen_random_uuid(), and jsonb for the
-- audit log instead of TEXT.
--
-- Run with:  supabase db push          (recommended — tracks migrations)
--       or:  psql "$DATABASE_URL" -f migrations/0001_init.sql
--
-- Requires the pgcrypto extension for gen_random_uuid() — Supabase Postgres
-- ships it enabled by default; the CREATE EXTENSION below is a no-op there
-- and a real requirement on a bare Postgres.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- citext gives case-insensitive UNIQUE emails without hand-rolled lower()
-- indexes, matching SPEC.md's `email citext unique`.
CREATE EXTENSION IF NOT EXISTS citext;

-- ─────────────────────────────────────────────────────────────────────────────
-- PEOPLE (staff). Owners of leads and tasks. D14.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staff (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        TEXT NOT NULL,
  name_en     TEXT,
  role        TEXT,
  access      TEXT NOT NULL DEFAULT 'agent'
              CHECK (access IN ('agent','manager','ceo','admin')),
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- APP_USER. Auth + RBAC identity (SPEC.md). See schema.sql for the full
-- rationale on why this is separate from `staff`.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_user (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  supabase_uid  UUID UNIQUE,
  email         CITEXT NOT NULL UNIQUE,
  display_name  TEXT,
  role          TEXT NOT NULL DEFAULT 'sales'
                CHECK (role IN ('admin','marketing','office_manager','sales')),
  office        TEXT CHECK (office IN ('tokyo','dubai') OR office IS NULL),
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  staff_id      BIGINT REFERENCES staff(id),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- LEADS. One row per PERSON. D13.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leads (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name             TEXT NOT NULL,
  name_kana        TEXT,
  company          TEXT,
  title            TEXT,

  first_touch      TEXT,
  first_touch_at   TIMESTAMPTZ,
  first_touch_note TEXT,

  relationship     TEXT NOT NULL DEFAULT 'individual'
                   CHECK (relationship IN ('individual','corporate','partner')),

  purpose          TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (purpose IN ('investment','relocation','second_home','business_base','unknown')),

  -- SPEC.md: six forward stages, plus a SEPARATE exit_state for the four ways
  -- a lead leaves the funnel without winning (still unratified by Balraj —
  -- implemented, flagged in the UI as provisional).
  stage            TEXT NOT NULL DEFAULT 'new'
                   CHECK (stage IN ('new','nurturing','engaged','meeting_booked',
                                    'in_negotiation','won')),
  stage_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  stage_reason     TEXT,
  exit_state       TEXT CHECK (exit_state IN ('lost','too_early','unreachable','unsubscribed')
                              OR exit_state IS NULL),
  revisit_at       TIMESTAMPTZ,

  owner_id         BIGINT REFERENCES staff(id),
  owner_user_id    UUID REFERENCES app_user(id),

  merged_into      BIGINT REFERENCES leads(id),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leads_stage       ON leads(stage) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_owner       ON leads(owner_id) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_owner_user  ON leads(owner_user_id) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_merged      ON leads(merged_into);

-- ─────────────────────────────────────────────────────────────────────────────
-- STAGE HISTORY. POST /api/leads/{id}/stage records who moved it and when.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_stage_history (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id       BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  from_stage    TEXT,
  to_stage      TEXT NOT NULL,
  actor_user_id UUID REFERENCES app_user(id),
  at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stage_history_lead ON lead_stage_history(lead_id, at);

-- ─────────────────────────────────────────────────────────────────────────────
-- AUDIT LOG. Every mutating endpoint writes one (D4 attributability, generalised).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor_user_id UUID REFERENCES app_user(id),
  action        TEXT NOT NULL,
  entity        TEXT NOT NULL,
  entity_id     TEXT,
  before        JSONB,
  after         JSONB,
  at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log(actor_user_id, at);

-- ─────────────────────────────────────────────────────────────────────────────
-- IDENTITIES. D13 dedupe match keys.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_identities (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id    BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN ('email','phone')),
  value      TEXT NOT NULL,
  is_primary BOOLEAN NOT NULL DEFAULT FALSE,
  status     TEXT NOT NULL DEFAULT 'ok'
             CHECK (status IN ('ok','bounced','invalid')),
  added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(kind, value)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- CHANNELS. D13.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_channels (
  id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  channel TEXT NOT NULL CHECK (channel IN (
            'business_card','csv','gohighlevel','lp_form','sns','gmail',
            'referral','whatsapp','line','showroom','property_finder')),
  note    TEXT,
  seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(lead_id, channel)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- REGIONS. D9 / D9a.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_regions (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id       BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  region        TEXT NOT NULL CHECK (region IN ('dubai','lombok','japan')),
  prop_status   TEXT CHECK (prop_status IN ('offplan','ready','secondary')),
  prop_type     TEXT CHECK (prop_type   IN ('apartment','penthouse','villa','townhouse')),
  bedrooms      TEXT CHECK (bedrooms    IN ('studio','1br','2br','3br','4br_plus')),
  inferred_from TEXT,
  set_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(lead_id, region)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- CONSENT. 特定電子メール法.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_consent (
  lead_id      BIGINT PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
  basis        TEXT NOT NULL DEFAULT 'unknown'
               CHECK (basis IN ('explicit','implied','ambiguous','unknown','withdrawn')),
  obtained_at  TIMESTAMPTZ,
  obtained_via TEXT,
  evidence     TEXT,
  withdrawn_at TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- EVENTS. Append-only, THE asset (D8).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id     BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN (
                'open','click','page_view','booking_page_view','reply',
                'booking_completed','wants_meeting','corporate_deal',
                'partnership','high_budget',
                'imported','merged','sent','bounce','unsubscribe','stage_change')),
  detail      TEXT,
  send_id     TEXT,
  source      TEXT NOT NULL DEFAULT 'system'
              CHECK (source IN ('system','sendgrid','web','ai','human')),
  set_by      BIGINT REFERENCES staff(id),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  voided_at   TIMESTAMPTZ,
  voided_by   BIGINT REFERENCES staff(id)
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_send ON events(send_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- SCORING RULES. D8.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scoring_rules (
  event_kind TEXT PRIMARY KEY,
  points     INTEGER NOT NULL,
  decays     BOOLEAN NOT NULL DEFAULT FALSE,
  label_ja   TEXT,
  label_en   TEXT,
  active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  note  TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- TASKS. D12.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id    BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  type       TEXT NOT NULL CHECK (type IN (
               'call','email','send_material','send_booking_link','prepare',
               'visit','follow_up','set_next_date')),
  owner_id   BIGINT REFERENCES staff(id),
  due_at     TIMESTAMPTZ,
  state      TEXT NOT NULL DEFAULT 'open'
             CHECK (state IN ('open','done','skipped')),
  created_by TEXT NOT NULL DEFAULT 'system'
             CHECK (created_by IN ('ai','rule','human','system')),
  reason     TEXT NOT NULL,
  done_at    TIMESTAMPTZ,
  done_by    BIGINT REFERENCES staff(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(owner_id, due_at) WHERE state='open';

-- ─────────────────────────────────────────────────────────────────────────────
-- REFERRALS. D11b.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referrals (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id           BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  referrer_staff_id BIGINT REFERENCES staff(id),
  referrer_lead_id  BIGINT REFERENCES leads(id),
  note              TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (referrer_staff_id IS NOT NULL OR referrer_lead_id IS NOT NULL)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SEQUENCES / SENDS. D1.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sequences (
  id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name   TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS sequence_steps (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sequence_id BIGINT NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
  step_no     INTEGER NOT NULL,
  offset_days INTEGER NOT NULL,
  subject     TEXT NOT NULL,
  purpose     TEXT,
  cta         TEXT,
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE(sequence_id, step_no)
);

CREATE TABLE IF NOT EXISTS sends (
  send_id  TEXT PRIMARY KEY,
  lead_id  BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  step_id  BIGINT REFERENCES sequence_steps(id),
  to_email TEXT NOT NULL,
  sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  provider TEXT NOT NULL DEFAULT 'sendgrid',
  status   TEXT NOT NULL DEFAULT 'queued'
           CHECK (status IN ('queued','sent','delivered','bounced','dropped'))
);
CREATE INDEX IF NOT EXISTS idx_sends_lead ON sends(lead_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- IMPORTS. D11.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS imports (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  filename     TEXT NOT NULL,
  rows_seen    INTEGER NOT NULL DEFAULT 0,
  rows_created INTEGER NOT NULL DEFAULT 0,
  rows_merged  INTEGER NOT NULL DEFAULT 0,
  rows_skipped INTEGER NOT NULL DEFAULT 0,
  answers_json JSONB,
  imported_by  BIGINT REFERENCES staff(id),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
