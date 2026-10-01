-- 0007 — the public booking flow, plus the two intake doors the funnel needs.
--
-- Until now every booking route required an internal login, so the "book a
-- consultation" button in a nurture email had nowhere to land. The proposal
-- puts this at the centre of the funnel: pick a topic, pick a time, get routed
-- to the right person, without a salesperson touching anything.
--
-- Three security commitments, since these endpoints are reachable by anyone:
--
--   1. A booking is addressed by an unguessable `public_ref`, never by its id.
--      The ref grants exactly one capability — view, cancel or move THIS
--      booking — and reveals nothing about any other customer or employee.
--   2. Slot listings never carry a rep's name or id. A stranger may learn that
--      10:00 on Thursday is free; they may not learn who works here.
--   3. A double booking is prevented by the DATABASE, not by re-reading before
--      writing. Two people confirming the same slot in the same second is the
--      one case application-level checking always loses.

ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS public_ref TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS booked_name TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS booked_email TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS booked_phone TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS booked_note TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS cancelled_by TEXT;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS rescheduled_from TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_public_ref
  ON calendar_events(public_ref) WHERE public_ref IS NOT NULL;

-- The double-booking guard. A partial unique index over (owner, start) counting
-- only live bookings: a cancelled slot frees up, a booked one cannot be taken
-- twice however close the two requests land.
CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_no_double_booking
  ON calendar_events(owner_user_id, starts_at) WHERE status = 'booked';

CREATE INDEX IF NOT EXISTS idx_calendar_owner_window
  ON calendar_events(owner_user_id, starts_at, status);

-- ── website / landing-page enquiries ─────────────────────────────────────────
-- The proposal lists LP and form enquiries as first-class sources. There was no
-- door for them at all: the dashboard could report a source it could not receive.
CREATE TABLE IF NOT EXISTS web_enquiries (
  id           BIGSERIAL PRIMARY KEY,
  lead_id      BIGINT REFERENCES leads(id) ON DELETE SET NULL,
  form_key     TEXT NOT NULL,
  name         TEXT,
  email        TEXT,
  phone        TEXT,
  message      TEXT,
  -- campaign attribution, straight off the query string
  source       TEXT,
  medium       TEXT,
  campaign     TEXT,
  landing_page TEXT,
  -- consent is captured at the form, timestamped, and never inferred
  consent_given BOOLEAN NOT NULL DEFAULT FALSE,
  consent_text  TEXT,
  remote_hint  TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_web_enquiries_created ON web_enquiries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_web_enquiries_lead ON web_enquiries(lead_id);

-- ── referrals ────────────────────────────────────────────────────────────────
-- D11b#1: someone who refers three clients IS a partner, which is one of the
-- 30-point scoring rules. Recording the referrer as a person rather than as
-- free text is how partners get discovered instead of guessed at.
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referrer_lead_id BIGINT
  REFERENCES leads(id) ON DELETE SET NULL;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referrer_name TEXT;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS referrer_email TEXT;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS relationship_note TEXT;
-- app_user.id is a real uuid on Postgres (TEXT only in the SQLite fixture), so
-- every FK to it must be uuid or the constraint cannot be created at all.
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS created_by_user_id UUID
  REFERENCES app_user(id);

-- ── AI reply classification ──────────────────────────────────────────────────
-- One row per classification attempt, kept so a wrong call can be corrected by
-- a human and the correction is visible rather than silently overwriting.
CREATE TABLE IF NOT EXISTS reply_classifications (
  id            BIGSERIAL PRIMARY KEY,
  reply_id      BIGINT REFERENCES replies(id) ON DELETE CASCADE,
  lead_id       BIGINT REFERENCES leads(id) ON DELETE CASCADE,
  -- 'rules' when the cheap header layer decided; 'ai' when a model was asked
  decided_by    TEXT NOT NULL CHECK (decided_by IN ('rules','ai','human')),
  model         TEXT,
  is_human      BOOLEAN,
  wants_meeting BOOLEAN,
  partnership   BOOLEAN,
  high_budget   BOOLEAN,
  -- the model's own words, kept as evidence for the score explanation
  evidence      TEXT,
  raw           TEXT,
  corrected_by_user_id UUID REFERENCES app_user(id),
  corrected_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reply_class_lead ON reply_classifications(lead_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_class_reply
  ON reply_classifications(reply_id) WHERE reply_id IS NOT NULL;
