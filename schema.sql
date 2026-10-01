-- ═══════════════════════════════════════════════════════════════════════════
-- EXCEED BOX — schema
-- Every table here encodes a decision from Exceed-Box-Build-Decisions.md.
-- Decision references (D1..D16) are noted inline so the two stay in step.
--
-- SQLite for now. Deliberately: zero setup, one file, easy to throw away while
-- the model is still moving. Nothing here is SQLite-specific enough to make the
-- move to PostgreSQL painful — that swap is a day, and it should happen before
-- anything real is loaded.
-- ═══════════════════════════════════════════════════════════════════════════

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────────
-- PEOPLE (staff). Owners of leads and tasks.
-- D14: ownership accepted; the real roster + roles are still owed by Balraj,
-- so this is deliberately thin and seeded with the four names in the demo.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staff (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  name_en     TEXT,
  role        TEXT,                       -- free text until the roster arrives
  access      TEXT NOT NULL DEFAULT 'agent'
              CHECK (access IN ('agent','manager','ceo','admin')),
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- APP_USER. Auth + RBAC identity (SPEC.md "Auth + identity"). Deliberately a
-- separate table from `staff` above: `staff` is the business roster the
-- scoring/tasks/accountability modules were built against before login
-- existed (D14 seed data, six names). `app_user` is who can sign in and what
-- role they hold. `staff_id` bridges the two, so once a person who is already
-- in `staff` gets a login, the existing task/accountability code (all keyed
-- on staff.id) keeps working with zero changes — the bridge is resolved once
-- and cached on the row rather than joined through supabase_uid every time.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS app_user (
  id            TEXT PRIMARY KEY,             -- uuid4 hex (TEXT in SQLite; uuid in Postgres)
  supabase_uid  TEXT UNIQUE,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT,
  role          TEXT NOT NULL DEFAULT 'sales'
                CHECK (role IN ('admin','marketing','office_manager','sales')),
  office        TEXT CHECK (office IN ('tokyo','dubai') OR office IS NULL),
  -- fail closed (auth spec): an unknown person auto-provisions here inactive
  -- and can see nothing until an admin flips this.
  is_active     INTEGER NOT NULL DEFAULT 1,
  staff_id      INTEGER REFERENCES staff(id),
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- LEADS. One row per PERSON.
-- D13: duplicates merge into one person. A second arrival never creates a
-- second lead — it adds a channel and, if new, an identity.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leads (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  name_kana     TEXT,
  company       TEXT,
  title         TEXT,                     -- D1: on the business card, thrown away today
  card_image_path TEXT,                   -- SPEC-V2 §6: the scanned/photographed business
                                           -- card image, stored on disk; path only, no OCR

  -- D11: source is two things, not one.
  first_touch   TEXT,                     -- never changes; what the dashboard attributes to
  first_touch_at TEXT,
  first_touch_note TEXT,                  -- "6/20 展示会", who met them

  -- ③ 関係 / WHO (D9) — derived from scoring, never hand-picked
  relationship  TEXT NOT NULL DEFAULT 'individual'
                CHECK (relationship IN ('individual','corporate','partner')),

  -- ② 目的 / WHY (D9) — discovered over time, 'unknown' is a real answer
  purpose       TEXT NOT NULL DEFAULT 'unknown'
                CHECK (purpose IN ('investment','relocation','second_home','business_base','unknown')),

  -- Status (D10). SPEC.md: six forward stages, moved through in order, plus a
  -- SEPARATE exit_state for the four ways a lead leaves the funnel without
  -- winning. A lead can be 'nurturing' AND 'unreachable' at once — exit_state
  -- records the terminal event, stage keeps the forward position it was at.
  stage         TEXT NOT NULL DEFAULT 'new'
                CHECK (stage IN ('new','nurturing','engaged','meeting_booked',
                                 'in_negotiation','won')),
  stage_at      TEXT NOT NULL DEFAULT (datetime('now')),
  stage_reason  TEXT,                     -- required by convention when exit_state='lost'
  -- ⚠️ still unratified by Balraj (SPEC.md) — implemented, flagged in the UI
  -- as provisional. NULL = still active in the forward funnel.
  exit_state    TEXT CHECK (exit_state IN ('lost','too_early','unreachable','unsubscribed')
                            OR exit_state IS NULL),
  revisit_at    TEXT,                     -- required by convention when exit_state='too_early'

  owner_id      INTEGER REFERENCES staff(id),   -- D14, legacy accountability view
  -- D14/SPEC — authoritative ownership for RBAC: what "a sales user's own
  -- leads" is filtered against in SQL. Kept in step with owner_id by
  -- app/auth.py's assignment helper rather than replacing it, since owner_id
  -- is still what tasks.py's team_status()/for_owner() read.
  owner_user_id TEXT REFERENCES app_user(id),

  merged_into   INTEGER REFERENCES leads(id),   -- set when this row loses a merge
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),

  -- H1 (FABLE-AUDIT.md) — materialized score. `scoring.explain()` stays the
  -- authoritative computation; `scoring.record()`/`void()` write this column
  -- on every event so /api/dashboard, /api/team, /api/leads and hot-leads
  -- never have to recompute score-for-every-lead in a Python loop again (was
  -- ~3 queries/lead, ~10^5 round trips at 30k leads). `scripts/sweep_decay.py`
  -- covers the other half — behaviour points that fade with no new event.
  score            INTEGER NOT NULL DEFAULT 0,
  score_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_stage  ON leads(stage) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_owner  ON leads(owner_id) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_owner_user ON leads(owner_user_id) WHERE merged_into IS NULL;
CREATE INDEX IF NOT EXISTS idx_leads_merged ON leads(merged_into);
CREATE INDEX IF NOT EXISTS idx_leads_score  ON leads(score DESC) WHERE merged_into IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- STAGE HISTORY. SPEC.md: POST /api/leads/{id}/stage "records who moved it and
-- when". `leads.stage`/`stage_at` stay the current pointer; this is the log.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_stage_history (
  id            INTEGER PRIMARY KEY,
  lead_id       INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  from_stage    TEXT,
  to_stage      TEXT NOT NULL,
  actor_user_id TEXT REFERENCES app_user(id),
  at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stage_history_lead ON lead_stage_history(lead_id, at);

-- ─────────────────────────────────────────────────────────────────────────────
-- AUDIT LOG. SPEC.md: "every write records actor_user_id — D4 requires the
-- manual +30 to be attributable." Generalised here to every mutating endpoint,
-- not just the +30. before/after are JSON text (TEXT in SQLite, JSONB in
-- Postgres — see migrations/0001_init.sql).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY,
  actor_user_id TEXT REFERENCES app_user(id),
  action        TEXT NOT NULL,
  entity        TEXT NOT NULL,
  entity_id     TEXT,
  before        TEXT,
  after         TEXT,
  at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log(actor_user_id, at);

-- ─────────────────────────────────────────────────────────────────────────────
-- IDENTITIES. Emails and phones. This is what dedupe matches on (D13).
-- A person can have several. Kept separate from leads precisely so a second
-- arrival with a new address enriches rather than duplicates.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_identities (
  id        INTEGER PRIMARY KEY,
  lead_id   INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  kind      TEXT NOT NULL CHECK (kind IN ('email','phone')),
  value     TEXT NOT NULL,                -- stored normalised (lowercased / digits only)
  is_primary INTEGER NOT NULL DEFAULT 0,
  status    TEXT NOT NULL DEFAULT 'ok'
            CHECK (status IN ('ok','bounced','invalid')),
  added_at  TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(kind, value)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- CHANNELS. D13: "put all three in a sequence — this person arrived from three
-- different channels." Grows over time. first_touch on the lead is the first
-- of these and is what the dashboard counts (D16), so the source chart totals
-- the same number as the bookings tile.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_channels (
  id         INTEGER PRIMARY KEY,
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  channel    TEXT NOT NULL CHECK (channel IN (
               'business_card','csv','gohighlevel','lp_form','sns','gmail',
               'referral','whatsapp','line','showroom','property_finder')),
  note       TEXT,
  seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(lead_id, channel)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- REGIONS. ① 地域 / WHERE (D9) — MULTI-select, which is why it is its own table.
-- Sub-categories per region (D9a) live alongside because they are region-specific.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_regions (
  id        INTEGER PRIMARY KEY,
  lead_id   INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  region    TEXT NOT NULL CHECK (region IN ('dubai','lombok','japan')),
  -- Dubai sub-axes (D9a). Lombok and Japan sub-categories are still owed by
  -- Balraj, so they are simply left NULL rather than guessed at.
  prop_status TEXT CHECK (prop_status IN ('offplan','ready','secondary')),
  prop_type   TEXT CHECK (prop_type   IN ('apartment','penthouse','villa','townhouse')),
  bedrooms    TEXT CHECK (bedrooms    IN ('studio','1br','2br','3br','4br_plus')),
  inferred_from TEXT,                     -- which click/content revealed it
  set_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(lead_id, region)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- CONSENT. D1/D11: the field that does not exist anywhere today, and the one
-- with legal weight under 特定電子メール法.
-- 'unknown' is an allowed and honest value — 25,000 CSV rows will have it.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_consent (
  lead_id     INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
  basis       TEXT NOT NULL DEFAULT 'unknown'
              CHECK (basis IN ('explicit','implied','ambiguous','unknown','withdrawn')),
  obtained_at TEXT,
  obtained_via TEXT,                      -- 'lp_form checkbox', 'card exchanged 2026-06-20', ...
  evidence    TEXT,
  withdrawn_at TEXT,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- EVENTS. Append-only. THE asset (D8 discussion): the score is only a sum over
-- this, so scoring rules can change later and history can be recomputed.
-- Nothing in this table is ever updated or deleted.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY,
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN (
               -- behaviour (D8: these decay)
               'open','click','page_view','booking_page_view','reply',
               -- facts (D8: these are permanent)
               'booking_completed','wants_meeting','corporate_deal',
               'partnership','high_budget',
               -- bookkeeping, never scored
               'imported','merged','sent','bounce','unsubscribe','stage_change')),
  detail     TEXT,                        -- url, subject, campaign, free note
  send_id    TEXT,                        -- links back to the exact email
  source     TEXT NOT NULL DEFAULT 'system'
             CHECK (source IN ('system','sendgrid','web','ai','human')),
  set_by     INTEGER REFERENCES staff(id),-- D4: a human-granted +30 must be attributable
  occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
  voided_at  TEXT,                        -- D7: the rep's one-tap "this wasn't real"
  voided_by  INTEGER REFERENCES staff(id)
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_send ON events(send_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- SCORE THRESHOLD CROSSINGS. N4 (FABLE-AUDIT-R2.md).
-- /api/scoring/model's "crossed this week" used to full-scan every live
-- lead and call scoring.score() TWICE each (now + a week ago) — ~60k
-- explain() calls at 30k leads, on an endpoint every role can view. The
-- materialized leads.score column can only answer "now", never "as of a
-- week ago", so that question has to be answered from history instead of
-- recomputed on every request. scoring.recompute_lead() is the one place
-- leads.score is ever written (record()/void()/sweep_decay()/the N3
-- self-heal on read) — it writes a row here whenever a recompute actually
-- moves a lead across `threshold`, in whichever direction. Answering
-- "how many crossed up this week" then costs one indexed query, not a scan.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS score_threshold_crossings (
  id           INTEGER PRIMARY KEY,
  lead_id      INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  crossed_at   TEXT NOT NULL DEFAULT (datetime('now')),
  direction    TEXT NOT NULL CHECK (direction IN ('up','down')),
  threshold    INTEGER NOT NULL,
  score_before INTEGER NOT NULL,
  score_after  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_score_crossings_lead ON score_threshold_crossings(lead_id);
CREATE INDEX IF NOT EXISTS idx_score_crossings_at   ON score_threshold_crossings(direction, crossed_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- SCORING RULES. D8: editable, not hardcoded — the card on the demo says
-- "tuned monthly against close rate", so it has to actually be tunable.
-- decays=1 → behaviour, halves at 60 days, zero at 120 (D8).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scoring_rules (
  event_kind TEXT PRIMARY KEY,
  points     INTEGER NOT NULL,
  decays     INTEGER NOT NULL DEFAULT 0,
  label_ja   TEXT,
  label_en   TEXT,
  active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  note  TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- TASKS. D12: six fields, not one sentence. `reason` is the field that decides
-- whether reps trust the system, so it is NOT NULL by design.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY,
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  type       TEXT NOT NULL CHECK (type IN (
               'call','email','send_material','send_booking_link','prepare',
               'visit','follow_up','set_next_date')),
  owner_id   INTEGER REFERENCES staff(id),
  due_at     TEXT,                        -- D12: a real timestamp. "today" was a word.
  state      TEXT NOT NULL DEFAULT 'open'
             CHECK (state IN ('open','done','skipped')),
  created_by TEXT NOT NULL DEFAULT 'system'
             CHECK (created_by IN ('ai','rule','human','system')),
  reason     TEXT NOT NULL,               -- "スコア92・予約ページ閲覧"
  done_at    TEXT,
  done_by    INTEGER REFERENCES staff(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(owner_id, due_at) WHERE state='open';

-- ─────────────────────────────────────────────────────────────────────────────
-- REFERRALS. D11b: a referral form, and the referrer recorded as a PERSON —
-- because someone who refers three clients is a パートナー, which is a 30-point
-- scoring rule. This table is how partners get discovered.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS referrals (
  id              INTEGER PRIMARY KEY,
  lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  referrer_staff_id INTEGER REFERENCES staff(id),   -- internal agent, from a dropdown
  referrer_lead_id  INTEGER REFERENCES leads(id),   -- an external person, as a lead
  note            TEXT,
  -- 0007: a referrer who is not yet anyone in the system. Recorded as a person
  -- rather than free text, because someone who refers three clients IS a
  -- partner — one of the 30-point rules — and that only surfaces if the
  -- referrer is a row you can count (D11b#1).
  referrer_name     TEXT,
  referrer_email    TEXT,
  relationship_note TEXT,
  created_by_user_id TEXT REFERENCES app_user(id),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (referrer_staff_id IS NOT NULL OR referrer_lead_id IS NOT NULL
         OR referrer_name IS NOT NULL)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SEQUENCES / SENDS. D1: SendGrid delivers; send_id is the string that ties an
-- email to a person and makes every passive signal attributable.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sequences (
  id     INTEGER PRIMARY KEY,
  name   TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sequence_steps (
  id           INTEGER PRIMARY KEY,
  sequence_id  INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
  step_no      INTEGER NOT NULL,
  offset_days  INTEGER NOT NULL,          -- relative to enrolment, never absolute dates
  subject      TEXT NOT NULL,
  purpose      TEXT,                      -- "the question it is really asking" (D9),
  question     TEXT,                      -- SPEC-V2 §2: the same idea, explicit EN/JA fields
  question_ja  TEXT,                      -- so the app can render both without re-deriving.
  cta          TEXT,
  active       INTEGER NOT NULL DEFAULT 1,
  UNIQUE(sequence_id, step_no)
);

CREATE TABLE IF NOT EXISTS sends (
  send_id    TEXT PRIMARY KEY,            -- e_7Kq2mB — the ID stamped into the email
  lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  step_id    INTEGER REFERENCES sequence_steps(id),
  to_email   TEXT NOT NULL,
  sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
  provider   TEXT NOT NULL DEFAULT 'sendgrid',
  status     TEXT NOT NULL DEFAULT 'queued'
             CHECK (status IN ('queued','sending','sent','delivered','bounced',
                               'dropped','failed','cancelled')),
  -- 0006: the send row IS the job. A separate jobs table would let a job and
  -- its send disagree about whether an email actually went out.
  scheduled_for TEXT,
  claimed_at    TEXT,
  claimed_by    TEXT,
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  provider_message_id TEXT,
  subject       TEXT,
  body_text     TEXT,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  -- SPEC-V2 §2: "Nobody ever receives the same email twice" — enforced at the
  -- database, not just in application code, so it can never regress silently.
  UNIQUE(lead_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_sends_lead ON sends(lead_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- IMPORTS. D11: the importer interrogates the file rather than guessing.
-- Every question it asked and every answer given is kept, because six months
-- later "where did these 2,000 people come from" is a real question.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS imports (
  id           INTEGER PRIMARY KEY,
  filename     TEXT NOT NULL,
  rows_seen    INTEGER NOT NULL DEFAULT 0,
  rows_created INTEGER NOT NULL DEFAULT 0,
  rows_merged  INTEGER NOT NULL DEFAULT 0,
  rows_skipped INTEGER NOT NULL DEFAULT 0,
  answers_json TEXT,                      -- the wizard Q&A, verbatim
  imported_by  INTEGER REFERENCES staff(id),
  -- SPEC-V2 §7: the two-phase wizard. analyze() stashes the uploaded file on
  -- disk and this row so commit() can re-read it without the client
  -- resending a potentially 25,000-row payload.
  token        TEXT UNIQUE,               -- opaque id returned by POST /api/import/analyze
  file_path    TEXT,                      -- where the uploaded CSV was stashed
  status       TEXT NOT NULL DEFAULT 'analyzed'
               CHECK (status IN ('analyzed','committed')),
  headers_json TEXT,                      -- column headers, as parsed
  mapping_json TEXT,                      -- the confirmed header -> field mapping used to commit
  imported_by_user_id TEXT REFERENCES app_user(id),
  -- .xlsx support: which sheet was chosen (D11 — the wizard asks when a
  -- workbook has more than one). NULL for CSV imports and for single-sheet
  -- workbooks (nothing to ask). commit() re-reads this same sheet.
  sheet_name   TEXT,
  committed_at TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §3 — SNS content patterns and the funnel they feed.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sns_patterns (
  key            TEXT PRIMARY KEY,        -- 'A' | 'B' | 'C' | 'D'
  title_ja       TEXT NOT NULL,
  title_en       TEXT NOT NULL,
  description_ja TEXT,
  description_en TEXT,
  active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lead_sns_pattern (
  lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  pattern_key TEXT NOT NULL REFERENCES sns_patterns(key),
  set_at      TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (lead_id, pattern_key)
);

-- Two of the five funnel steps (SNS投稿 impressions, LP/資料 views) have no
-- analytics API wired to any platform — there is nowhere in this codebase
-- that could compute them live. They are recorded here explicitly, tagged
-- 'manual' in the API response, rather than invented or silently omitted.
CREATE TABLE IF NOT EXISTS sns_funnel_manual (
  step_key   TEXT PRIMARY KEY CHECK (step_key IN ('sns_post','lp_view')),
  count      INTEGER NOT NULL DEFAULT 0,
  note       TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §4 — booking settings and the calendar events booking produces.
-- No public booking page and no Google Calendar sync exist; both are stated
-- plainly by the endpoints that touch this data, never implied.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS booking_settings (
  id                    INTEGER PRIMARY KEY CHECK (id = 1),
  -- Timezone fix (FABLE-AUDIT.md) — Tokyo (JST, UTC+9) and Dubai (GST,
  -- UTC+4) are FIVE hours apart, not four (the decision log said four,
  -- twice — verified wrong and corrected here).
  jst_gst_gap_hours     INTEGER NOT NULL DEFAULT 5,
  business_hours_start  TEXT NOT NULL DEFAULT '09:00',
  business_hours_end    TEXT NOT NULL DEFAULT '18:00',
  slot_length_minutes   INTEGER NOT NULL DEFAULT 30,
  timezone              TEXT NOT NULL DEFAULT 'Asia/Tokyo',
  note                  TEXT,
  updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS booking_meeting_types (
  key             TEXT PRIMARY KEY,       -- 'consult_30' | 'site_inspection' | 'showroom_visit'
  label_en        TEXT NOT NULL,
  label_ja        TEXT NOT NULL,
  duration_minutes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_rep_meeting_types (
  user_id      TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  meeting_type TEXT NOT NULL REFERENCES booking_meeting_types(key),
  PRIMARY KEY (user_id, meeting_type)
);

CREATE TABLE IF NOT EXISTS calendar_events (
  id                 INTEGER PRIMARY KEY,
  owner_user_id      TEXT REFERENCES app_user(id),
  lead_id            INTEGER REFERENCES leads(id) ON DELETE CASCADE,
  type               TEXT NOT NULL CHECK (type IN
                      ('consult_30','site_inspection','showroom_visit','other')),
  title              TEXT,
  starts_at          TEXT NOT NULL,
  ends_at            TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'booked'
                     CHECK (status IN ('booked','completed','cancelled')),
  created_by_user_id TEXT REFERENCES app_user(id),
  -- always 'internal' — there is no Google Calendar connection (SPEC-V2 §5).
  source             TEXT NOT NULL DEFAULT 'internal',
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  -- 0007: the public booking flow. A booking is addressed by public_ref, never
  -- by id — the ref is unguessable and grants exactly one capability: view,
  -- cancel or move THIS booking.
  public_ref         TEXT,
  booked_name        TEXT,
  booked_email       TEXT,
  booked_phone       TEXT,
  booked_note        TEXT,
  cancelled_at       TEXT,
  cancelled_by       TEXT,
  rescheduled_from   TEXT,
  -- 0008: caller-supplied idempotency token. Replaces an (email, slot, type)
  -- lookup whose every input a stranger can guess, and which returned the
  -- public_ref that lets the holder cancel the booking.
  request_id         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_request_id
  ON calendar_events(request_id) WHERE request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_public_ref
  ON calendar_events(public_ref) WHERE public_ref IS NOT NULL;
-- The double-booking guard lives in the database, not in a read-then-write:
-- two people confirming the same slot in the same second is the one case
-- application-level checking always loses.
CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_no_double_booking
  ON calendar_events(owner_user_id, starts_at) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS idx_calendar_owner_window
  ON calendar_events(owner_user_id, starts_at, status);
CREATE INDEX IF NOT EXISTS idx_calendar_owner_week ON calendar_events(owner_user_id, starts_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §5 — auto-assignment rules, evaluated in priority order.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assignment_rules (
  id            INTEGER PRIMARY KEY,
  priority      INTEGER NOT NULL,
  match_field   TEXT CHECK (match_field IN ('region','purpose','language','topic') OR match_field IS NULL),
  match_value   TEXT,
  owner_user_id TEXT NOT NULL REFERENCES app_user(id),
  is_fallback   INTEGER NOT NULL DEFAULT 0,
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assignment_rules_priority ON assignment_rules(priority);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §10 — voice notes, push, and system integration status.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lead_notes (
  id              INTEGER PRIMARY KEY,
  lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  author_user_id  TEXT REFERENCES app_user(id),
  audio_path      TEXT NOT NULL,
  duration_seconds REAL,
  -- always NULL — transcription is not wired (SPEC-V2 §10). Left as a real
  -- column so wiring it later is additive, not a schema change.
  transcript      TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lead_notes_lead ON lead_notes(lead_id, created_at);

CREATE TABLE IF NOT EXISTS push_tokens (
  id         INTEGER PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  token      TEXT NOT NULL,
  platform   TEXT NOT NULL CHECK (platform IN ('ios','ipados','android','web')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, token)
);

CREATE TABLE IF NOT EXISTS push_settings (
  user_id                        TEXT PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
  notify_new_lead                INTEGER NOT NULL DEFAULT 1,
  notify_hot_lead                INTEGER NOT NULL DEFAULT 1,
  notify_reply                   INTEGER NOT NULL DEFAULT 1,
  -- D12b: escalation level 2-3 notifies a manager.
  notify_task_escalation_level   INTEGER NOT NULL DEFAULT 2,
  updated_at                     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS integrations (
  key           TEXT PRIMARY KEY,   -- 'gohighlevel' | 'hubspot' | 'gmail' | 'google_calendar' | 'whatsapp' | 'google_drive'
  label_en      TEXT NOT NULL,
  label_ja      TEXT NOT NULL,
  connected     INTEGER NOT NULL DEFAULT 0,
  status_detail TEXT,
  what_is_needed TEXT,
  last_sync_at  TEXT,
  record_count  INTEGER,
  read_only     INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SPEC-V2 §9 — inbound replies and the human-approved reply flow. No model is
-- wired to draft anything (ai draft is never stored — see app/api.py).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS replies (
  id                   INTEGER PRIMARY KEY,
  lead_id              INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  channel              TEXT NOT NULL DEFAULT 'email',
  subject              TEXT,
  body                 TEXT NOT NULL,
  received_at          TEXT NOT NULL DEFAULT (datetime('now')),
  status               TEXT NOT NULL DEFAULT 'received'
                       CHECK (status IN ('received','sent','voided')),
  -- the four-question classifier chips (D7) — human/wants_meeting/
  -- partnership/high_budget — set by a person on approve-and-send, since no
  -- AI reads replies in this environment.
  verdict_json         TEXT,
  human_reply_body     TEXT,
  scored_event_ids_json TEXT,       -- so "this wasn't real" can void exactly these events
  sent_at              TEXT,
  sent_by_user_id      TEXT REFERENCES app_user(id),
  voided_at            TEXT,
  voided_by_user_id    TEXT REFERENCES app_user(id),
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_replies_lead ON replies(lead_id, received_at);


-- ── 0006 outbound (SQLite mirror of migrations/0006_outbound.sql) ────────────
CREATE TABLE IF NOT EXISTS nurture_enrolments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id       INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  sequence_id   INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
  enrolled_at   TEXT NOT NULL DEFAULT (datetime('now')),
  state         TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','paused','stopped','completed')),
  state_reason  TEXT,
  state_changed_at TEXT NOT NULL DEFAULT (datetime('now')),
  resume_after  TEXT,
  UNIQUE (lead_id, sequence_id)
);
CREATE INDEX IF NOT EXISTS idx_enrol_due ON nurture_enrolments(state, enrolled_at);
CREATE INDEX IF NOT EXISTS idx_enrol_lead ON nurture_enrolments(lead_id);

CREATE INDEX IF NOT EXISTS idx_sends_claimable ON sends(status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_sends_claimed ON sends(status, claimed_at);

CREATE TABLE IF NOT EXISTS email_suppressions (
  email       TEXT PRIMARY KEY,
  reason      TEXT NOT NULL
              CHECK (reason IN ('unsubscribed','bounced','complained','manual','invalid')),
  detail      TEXT,
  source      TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS worker_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job         TEXT NOT NULL,
  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  ok          INTEGER,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_worker_runs_job ON worker_runs(job, started_at DESC);


-- ── 0007 public booking + intake (SQLite mirror) ─────────────────────────────
CREATE TABLE IF NOT EXISTS web_enquiries (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id      INTEGER REFERENCES leads(id) ON DELETE SET NULL,
  form_key     TEXT NOT NULL,
  name         TEXT,
  email        TEXT,
  phone        TEXT,
  message      TEXT,
  source       TEXT,
  medium       TEXT,
  campaign     TEXT,
  landing_page TEXT,
  consent_given INTEGER NOT NULL DEFAULT 0,
  consent_text  TEXT,
  remote_hint  TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_web_enquiries_created ON web_enquiries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_web_enquiries_lead ON web_enquiries(lead_id);

CREATE TABLE IF NOT EXISTS reply_classifications (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  reply_id      INTEGER REFERENCES replies(id) ON DELETE CASCADE,
  lead_id       INTEGER REFERENCES leads(id) ON DELETE CASCADE,
  decided_by    TEXT NOT NULL CHECK (decided_by IN ('rules','ai','human')),
  model         TEXT,
  is_human      INTEGER,
  wants_meeting INTEGER,
  partnership   INTEGER,
  high_budget   INTEGER,
  evidence      TEXT,
  raw           TEXT,
  corrected_by_user_id TEXT REFERENCES app_user(id),
  corrected_at  TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reply_class_lead ON reply_classifications(lead_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_class_reply
  ON reply_classifications(reply_id) WHERE reply_id IS NOT NULL;
