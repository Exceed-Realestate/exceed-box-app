-- 0008 — replace an email-keyed idempotency check with a caller-supplied token.
--
-- 0007 made `confirm` return an existing booking when (email, slot, type)
-- matched, so that a customer double-clicking the button got one appointment
-- rather than two. That view includes `public_ref` — the token that lets anyone
-- holding it cancel or move the booking — plus the person's name and note.
--
-- Every input to that lookup is guessable by a stranger. Meeting types are
-- listed publicly, the slot list publishes roughly 270 exact start times, and
-- an email address is not a secret. So: guess an address, walk the slots, and
-- you are handed somebody's reference and can cancel their appointment.
--
-- The fix is to key idempotency on something only the actual submitter has. The
-- booking page generates a random request id once per attempt; a retry carries
-- the same one, a stranger cannot produce it, and it reveals nothing if seen.
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS request_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_request_id
  ON calendar_events(request_id) WHERE request_id IS NOT NULL;
