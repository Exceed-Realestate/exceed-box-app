"""The contract test — SPEC.md/AUDIT.md canonicalisation, locked down.

AUDIT.md's root-cause finding: "the app and backend were each built against
SPEC.md but drifted, and were only ever tested in isolation." Every other
test file in this repo exercises business logic or the permission matrix —
none of them would have caught a route quietly renaming `score` to
`lead_score`, or nesting a response under `{"lead": ...}` again. This file's
only job is to assert the exact response *keys* SPEC.md/AUDIT.md declare for
every route, so that kind of drift fails a test the next time it happens,
instead of surfacing three weeks later as "the app crashed on a successful
response."

This is deliberately narrow: KEY SETS, not values, not business behaviour —
that is what test_decisions.py and test_permissions.py are for. A field
being named right and a field being computed right are two different bugs;
this file only guards the first one.

Runs with no cloud dependency, same as the other two suites.

Run:  python3 -m pytest tests -q     (or: python3 tests/test_contract.py)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCEEDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "test_contract.db")
# card scans / voice notes / CSV uploads must land in a throwaway tempdir too
# — never in the real project's data/uploads (app/media.py reads this at
# import time, so it must be set before app.api is imported below).
os.environ["EXCEEDBOX_UPLOAD_ROOT"] = tempfile.mkdtemp()
os.environ["EXCEEDBOX_DEV_AUTH"] = "1"
os.environ.pop("SUPABASE_JWT_SECRET", None)
# N1 (FABLE-AUDIT-R2.md) — see tests/test_permissions.py's identical line;
# app/auth.py's ALLOWED_EMAIL_DOMAINS is a module-level constant read once,
# by whichever test file imports app.auth first in a full `pytest tests` run.
os.environ["EXCEEDBOX_ALLOWED_EMAIL_DOMAINS"] = "test.example,contract.example,fable-fix.example,exceed-re.ae"

from fastapi.testclient import TestClient   # noqa: E402

from app import auth, db, devauth, ingest, scoring, tasks   # noqa: E402
from app.api import app                      # noqa: E402

client = TestClient(app)


# ══ canonical key sets — SPEC.md + AUDIT.md, nothing invented here ══════════

ENVELOPE_KEYS = {"data", "page", "page_size", "total"}

ME_KEYS = {"id", "email", "name", "role", "office", "permissions"}

# AUDIT.md: Task{id,lead_name,lead_score,escalation_level} — never
# {task_id,lead,score,escalation}.
TASK_KEYS = {
    "id", "type", "label_en", "label_ja", "owner_user_id", "owner_name",
    "lead_id", "lead_name", "company", "lead_score", "due_at", "days_overdue",
    "state", "reason", "escalation_level", "created_by", "created_at",
}

# GET /api/leads row — AUDIT.md: must include region, source, owner_name,
# exit_state, on top of what already existed.
LEAD_SUMMARY_KEYS = {
    "id", "name", "company", "stage", "exit_state", "exit_state_provisional",
    "source", "channels", "region", "purpose", "relationship", "score", "heat",
    "activity_note", "owner_user_id", "owner_name", "contact_redacted",
    "created_at", "updated_at",
}
LEAD_SUMMARY_KEYS_WITH_CONTACT = LEAD_SUMMARY_KEYS | {"identities"}

# GET /api/leads/{id} (and assign/stage/signal/create/edit responses) —
# AUDIT.md: a FLAT LeadDetail, never {lead, identities, regions, score}.
LEAD_DETAIL_KEYS = {
    "id", "name", "name_kana", "company", "title", "card_image_path",
    "stage", "exit_state", "exit_state_provisional", "stage_reason", "revisit_at",
    "source", "first_touch_at", "first_touch_note", "channels",
    "purpose", "relationship",
    "owner_user_id", "owner_name",
    "region", "regions",
    "score", "heat", "threshold", "hot", "score_breakdown", "decay_note",
    "activity_note",
    "contact_redacted", "identities", "consent",
    "tasks", "stage_history", "timeline",
    "created_at", "updated_at",
}
SIGNAL_EXTRA_KEYS = {"signal_score_before", "signal_score_after", "signal_crossed_threshold"}

# GET /api/leads/{id}/score — AUDIT.md: breakdown + decay_note, keep hot/threshold.
# H3 — `decay_mode` names which of the two unratified D8 interpretations
# (per_event/lead_level) produced this breakdown, always visible.
SCORE_KEYS = {"lead_id", "score", "threshold", "hot", "breakdown", "decay_note",
             "decay_mode", "summary"}

# H2 — page/page_size/has_more: pipeline columns are now paginated, not
# capped at 200 with no way past it.
PIPELINE_BUCKET_KEYS = {"key", "label", "label_ja", "count", "leads",
                        "page", "page_size", "has_more"}
PIPELINE_LEAD_CARD_KEYS = {"id", "name", "company", "score", "heat", "stage",
                           "exit_state", "exit_state_provisional", "owner_user_id", "owner_name"}

DASHBOARD_KEYS = {
    "tiles", "trend", "trend_flag", "funnel", "by_source", "by_region",
    "hot_leads", "by_rep", "source_total_matches_tile",
}
# unavailable_reason_ja added 2026-09-18: the reason line was the one English
# sentence left on an otherwise Japanese dashboard.
DASHBOARD_TILE_KEYS = {"key", "label", "label_ja", "value",
                       "unavailable_reason", "unavailable_reason_ja"}
TREND_POINT_KEYS = {"date", "leads", "meetings_booked", "in_negotiation"}
FUNNEL_STEP_KEYS = {"stage", "label", "label_ja", "count"}
BREAKDOWN_ITEM_KEYS = {"source", "label", "label_ja", "count"}   # by_source / by_region shape
TREND_FLAG_KEYS = {"message", "message_ja", "detail", "detail_ja"}
HOT_LEAD_KEYS = {"id", "name", "company", "score", "heat", "stage", "exit_state",
                 "exit_state_provisional", "owner_user_id", "owner_name", "contact_redacted"}
REP_PERFORMANCE_KEYS = {"user_id", "name", "office", "leads_owned", "tasks_open",
                        "tasks_overdue", "meetings_booked", "won"}

TEAM_MEMBER_KEYS = {
    "user_id", "name", "role", "office", "is_active",
    "tasks_open", "tasks_overdue", "tasks_escalated",
    "leads_owned", "meetings_booked", "won", "idle",
}

APP_USER_KEYS = {"id", "email", "display_name", "role", "office", "is_active", "created_at"}
CREATE_USER_EXTRA_KEYS = {"invited", "access_status", "message"}

ERROR_KEYS = {"error"}
ERROR_BODY_KEYS = {"code", "message"}

# Extended 2026-09-12: /api/health now reports the TRUE state of each
# integration (auth mode, media backend, mail provider and whether it actually
# delivers) so an Integrations screen cannot show a green tick it has not
# earned. Adding keys here is a deliberate contract change, not drift.
HEALTH_KEYS = {"ok", "leads", "events", "dialect", "open_access", "auth", "media", "mail"}

# ══ SPEC-V2 canonical key sets ═══════════════════════════════════════════════

# a generic "not configured" envelope — SendGrid/OCR/AI/GoHighLevel/Google
# Calendar/push all use exactly this shape (SPEC-V2 rule 1).
NOT_CONFIGURED_KEYS = {"configured", "reason", "manual_path"}

ACTIVITY_ITEM_KEYS = {"id", "lead_id", "lead_name", "company", "kind", "label_en", "label_ja",
                      "points", "context", "occurred_at", "time_ago", "source"}

SCORING_MODEL_KEYS = {"rules", "decay", "threshold", "crossed_this_week"}
SCORING_MODEL_RULE_KEYS = {"event_kind", "points", "decays", "label_en", "label_ja", "group"}
# H3 — `mode` names which unratified D8 interpretation is active.
SCORING_MODEL_DECAY_KEYS = {"half_life_days", "zero_days", "explanation_en", "explanation_ja", "mode"}

STEP_STATS_KEYS = {"sent", "opened", "clicked", "open_rate", "click_rate"}
SEQUENCE_SUMMARY_KEYS = {"id", "name", "active", "step_count", "total_sent", "total_opened",
                         "total_clicked", "open_rate", "click_rate"}
SEQUENCE_STEP_KEYS = {"id", "step_no", "offset_days", "subject", "purpose", "question",
                      "question_ja", "cta", "active", "stats"}
SEQUENCE_DETAIL_EXTRA_KEYS = {"steps", "editorial_rule", "exit_conditions", "never_repeat_note"}
EDITORIAL_RULE_KEYS = {"en", "ja"}
EXIT_CONDITION_KEYS = {"action", "label_en", "label_ja", "trigger_en", "trigger_ja", "provisional"}
SEQUENCE_STATS_EXTRA_KEYS = {"per_step", "duplicate_sends_detected"}
PER_STEP_STAT_KEYS = {"step_no", "subject", "sent", "opened", "clicked", "open_rate", "click_rate"}

SNS_PATTERN_KEYS = {"key", "title_en", "title_ja", "description_en", "description_ja",
                    "leads_count", "leads_sample"}
SNS_PATTERN_SAMPLE_KEYS = {"id", "name", "company", "stage", "score", "heat", "owner_name"}
SNS_FUNNEL_STEP_KEYS = {"step", "label_en", "label_ja", "count", "tracked", "source"}

BOOKING_SETTINGS_KEYS = {"jst_gst_gap_hours", "business_hours_start", "business_hours_end",
                         "slot_length_minutes", "timezone", "note", "meeting_types",
                         "public_booking_page"}
BOOKING_MEETING_TYPE_KEYS = {"key", "label_en", "label_ja", "duration_minutes", "reps"}
BOOKING_SLOTS_KEYS = {"meeting_type", "duration_minutes", "week_start", "week_end", "timezone",
                      "jst_gst_gap_hours", "slots"}
BOOKING_SLOT_KEYS = {"starts_at", "ends_at", "available_rep_user_ids"}

ASSIGNMENT_RULE_KEYS = {"id", "priority", "match_field", "match_value", "owner_user_id",
                        "owner_name", "is_fallback", "active"}
CALENDAR_KEYS = {"user_id", "user_name", "week_start", "week_end", "events", "google_calendar_sync"}
CALENDAR_EVENT_ITEM_KEYS = {"id", "lead_id", "lead_name", "type", "title", "starts_at", "ends_at", "status"}
CALENDAR_EVENT_ACTION_KEYS = {"id", "lead_id", "lead_name", "owner_user_id", "owner_name",
                              "type", "title", "starts_at", "ends_at", "status", "created_at"}

IMPORT_ANALYZE_KEYS = {"token", "columns", "guessed_mapping", "questions", "sample_rows",
                       "rows_seen", "preview_new", "preview_duplicate", "preview_capped",
                       "preview_rows_checked"}
IMPORT_COMMIT_KEYS = {"import_id", "token", "filename", "rows_seen", "rows_created", "rows_merged",
                      "rows_skipped", "consent_basis_applied", "lead_ids"}

INTEGRATION_ROW_KEYS = {"key", "label_en", "label_ja", "connected", "status_detail",
                        "what_is_needed", "last_sync_at", "record_count", "read_only", "updated_at"}
GHL_SYNC_KEYS = {"configured", "reason", "manual_path", "read_only", "last_sync_at"}

REPLY_KEYS = {"id", "lead_id", "channel", "subject", "body", "received_at", "status", "verdict",
              "human_reply_body", "sent_at", "sent_by_name", "voided_at"}
REPLY_SEND_EXTRA_KEYS = {"scoring_outcome"}

NOTE_KEYS = {"id", "lead_id", "author_user_id", "author_name", "audio_path", "duration_seconds",
             "transcript", "transcription", "created_at"}

PUSH_REGISTER_KEYS = {"registered", "platform"}
PUSH_SETTINGS_KEYS = {"notify_new_lead", "notify_hot_lead", "notify_reply",
                      "notify_task_escalation_level"}
PUSH_TEST_KEYS = {"configured", "sent", "reason", "manual_path"}

NOTIFY_REP_KEYS = {"task", "push_attempt"}
FOLLOW_KEYS = {"task"}
SET_CONTACT_DATE_EXTRA_KEYS = {"task"}


# ══ fixture ══════════════════════════════════════════════════════════════════

def fresh():
    """One of each role, a handful of leads/tasks, so every endpoint has
    something real to render — key-set assertions on an empty list still
    pass trivially, but the nested item shapes (Task, LeadSummary, ...) need
    at least one row to actually be checked."""
    db.reset()
    con = db.connect()
    for kind, pts, decays in [
            ("open", 1, 1), ("click", 3, 1), ("page_view", 3, 1),
            ("booking_page_view", 5, 1), ("reply", 10, 1),
            ("booking_completed", 20, 0), ("wants_meeting", 30, 0),
            ("corporate_deal", 30, 0), ("partnership", 30, 0), ("high_budget", 30, 0)]:
        con.execute("""INSERT INTO scoring_rules (event_kind,points,decays,label_en)
                       VALUES (?,?,?,?)""", (kind, pts, decays, kind))
    con.execute("INSERT INTO settings (key,value) VALUES ('notify_threshold','40')")

    emails = {r: "%s@contract.example" % r for r in
             ("admin", "marketing", "office_manager", "sales")}
    ids = {}
    for role, email in emails.items():
        uid = "cu-%s" % role
        con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active)
                       VALUES (?,?,?,?,1)""", (uid, email, role.title(), role))
        ids[role] = uid

    lead_id = ingest.upsert_lead(con, name="Contract Test Lead", channel="csv",
                                 company="Contract Co", email="contract-lead@test.example",
                                 phone="03-1234-5678")["lead_id"]
    con.execute("UPDATE leads SET owner_user_id=?, purpose='investment' WHERE id=?",
               (ids["sales"], lead_id))
    con.execute("INSERT INTO lead_regions (lead_id, region, inferred_from) VALUES (?,?,?)",
               (lead_id, "dubai", "contract-test"))
    con.execute("""INSERT INTO sends (send_id, lead_id, to_email) VALUES ('cx1', ?, 'contract-lead@test.example')""",
               (lead_id,))
    con.commit()

    from app import scoring
    scoring.record(con, lead_id, "reply", detail="test reply")
    scoring.record(con, lead_id, "corporate_deal", detail="test fact")

    staff_id = auth.ensure_staff_bridge(con, ids["sales"], "Sales Contract")
    task_id = tasks.create(con, lead_id, "call", reason="contract test task",
                           owner_id=staff_id, due_at="2026-08-20 10:00:00", created_by="human")
    con.commit()

    # ── SPEC-V2 fixtures — one of everything, so every new endpoint has a
    # real row to render, not just an empty list. ────────────────────────────
    con.execute("INSERT INTO sequences (id,name) VALUES (1,'Contract Test Sequence')")
    con.execute("""INSERT INTO sequence_steps (id, sequence_id, step_no, offset_days, subject,
                                                purpose, question, question_ja, cta)
                   VALUES (1,1,1,0,'Step 1 subject','purpose','Question?','質問？','CTA')""")
    con.execute("""INSERT INTO sends (send_id, lead_id, step_id, to_email, sent_at, status)
                   VALUES ('cxseq1', ?, 1, 'contract-lead@test.example', '2026-08-01 09:00:00', 'sent')""",
               (lead_id,))
    scoring.record(con, lead_id, "open", send_id="cxseq1", source="sendgrid",
                   occurred_at=datetime(2026, 8, 1, 10, 0, 0))

    con.execute("""INSERT INTO sns_patterns (key, title_ja, title_en, description_ja, description_en)
                   VALUES ('A','パターンA','Pattern A','説明','description')""")
    con.execute("INSERT INTO lead_sns_pattern (lead_id, pattern_key) VALUES (?, 'A')", (lead_id,))
    con.execute("""INSERT INTO sns_funnel_manual (step_key, count, note) VALUES
                   ('sns_post', 10, 'contract test'), ('lp_view', 20, 'contract test')""")

    con.execute("""INSERT INTO booking_settings (id, jst_gst_gap_hours, business_hours_start,
                                                  business_hours_end, slot_length_minutes, timezone, note)
                   VALUES (1, 5, '09:00', '18:00', 30, 'Asia/Tokyo', 'contract test')""")
    con.execute("""INSERT INTO booking_meeting_types (key, label_en, label_ja, duration_minutes)
                   VALUES ('consult_30','Free 30-min consultation','無料30分相談',30)""")
    con.execute("INSERT INTO booking_rep_meeting_types (user_id, meeting_type) VALUES (?, 'consult_30')",
               (ids["sales"],))

    con.execute("""INSERT INTO assignment_rules (priority, match_field, match_value, owner_user_id, is_fallback)
                   VALUES (1,'region','dubai',?,0)""", (ids["sales"],))
    con.execute("""INSERT INTO assignment_rules (priority, match_field, match_value, owner_user_id, is_fallback)
                   VALUES (99,NULL,NULL,?,1)""", (ids["admin"],))

    con.execute("""INSERT INTO calendar_events (owner_user_id, lead_id, type, title, starts_at, ends_at,
                                                created_by_user_id)
                   VALUES (?,?,?,?,?,?,?)""",
               (ids["sales"], lead_id, "consult_30", "Contract test meeting",
                "2026-08-21 10:00:00", "2026-08-21 10:30:00", ids["admin"]))

    con.execute("""INSERT INTO integrations (key, label_en, label_ja, connected, status_detail,
                                             what_is_needed, read_only)
                   VALUES ('gohighlevel','GoHighLevel','GoHighLevel',0,'not connected','API key',1)""")
    con.execute("""INSERT INTO integrations (key, label_en, label_ja, connected, status_detail,
                                             what_is_needed, read_only)
                   VALUES ('gmail','Gmail','Gmail',0,'not connected','OAuth',0)""")

    con.execute("""INSERT INTO replies (lead_id, channel, subject, body, received_at, status)
                   VALUES (?,?,?,?,?, 'received')""",
               (lead_id, "email", "Re: test", "test reply body", "2026-08-15 09:00:00"))

    con.commit()
    con.close()
    return ids, lead_id, task_id, emails


def hdr(emails: dict, role: str) -> dict:
    # N1 (FABLE-AUDIT-R2.md) — stable sub per role; see test_permissions.py's
    # identical comment.
    return {"Authorization": "Bearer " + devauth.mint(sub="cx-test-sub-%s" % role, email=emails[role])}


def assert_keys(body: dict, expected: set, where: str):
    actual = set(body.keys())
    assert actual == expected, (
        "%s: response keys drifted from the SPEC.md contract.\n"
        "  missing: %s\n  unexpected: %s" %
        (where, sorted(expected - actual), sorted(actual - expected)))


# ══ /api/me ══════════════════════════════════════════════════════════════════

def test_contract_me():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/me", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    assert_keys(r.json(), ME_KEYS, "GET /api/me")


# ══ tasks / today ═══════════════════════════════════════════════════════════

def test_contract_today_and_task_shape():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/today", headers=hdr(emails, "sales"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/today (envelope)")
    assert body["data"], "fixture must produce at least one task"
    assert_keys(body["data"][0], TASK_KEYS, "GET /api/today (Task item)")
    # AUDIT.md — the specific wrong-name fields must be GONE, not just the
    # right ones present.
    for wrong in ("task_id", "lead", "score", "escalation"):
        assert wrong not in body["data"][0], \
            "GET /api/today: legacy field '%s' must not be present" % wrong


def test_contract_task_create_and_patch():
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/tasks", headers=hdr(emails, "sales"),
                    json={"lead_id": lead_id, "type": "email", "reason": "contract create",
                          "due_at": "2026-08-21 09:00:00"})
    assert r.status_code == 200
    assert_keys(r.json(), TASK_KEYS, "POST /api/tasks")
    new_task_id = r.json()["id"]

    r2 = client.patch("/api/tasks/%d" % new_task_id, headers=hdr(emails, "sales"),
                      json={"action": "snooze", "until": "2026-08-25 09:00:00"})
    assert r2.status_code == 200
    assert_keys(r2.json(), TASK_KEYS, "PATCH /api/tasks/{id} (snooze)")
    # Timezone fix (FABLE-AUDIT.md) — due_at is no longer a naive pass-through
    # (that WAS the bug: two offices reading the same naive string disagree).
    # This test's user has no office set, which defaults to Tokyo (SPEC.md:
    # "the Japan office is the primary user") — a naive "09:00" is read as
    # 09:00 JST and stored as its real UTC instant, with an explicit offset.
    assert r2.json()["due_at"] == "2026-08-25T00:00:00+00:00"

    r3 = client.patch("/api/tasks/%d" % new_task_id, headers=hdr(emails, "sales"),
                      json={"action": "complete"})
    assert r3.status_code == 200
    assert_keys(r3.json(), TASK_KEYS, "PATCH /api/tasks/{id} (complete)")
    assert r3.json()["state"] == "done"


def test_contract_task_create_requires_due_at_and_reason():
    """SPEC.md/AUDIT.md: due_at and reason are REQUIRED on create."""
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/tasks", headers=hdr(emails, "sales"),
                    json={"lead_id": lead_id, "type": "email", "reason": "no due_at"})
    assert r.status_code == 422
    assert_keys(r.json(), ERROR_KEYS, "POST /api/tasks (422 body)")
    assert_keys(r.json()["error"], ERROR_BODY_KEYS, "POST /api/tasks (error object)")

    r2 = client.post("/api/tasks", headers=hdr(emails, "sales"),
                     json={"lead_id": lead_id, "type": "email", "due_at": "2026-08-21 09:00:00"})
    assert r2.status_code == 422


# ══ leads ═══════════════════════════════════════════════════════════════════

def test_contract_leads_list():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/leads?page_size=50", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/leads (envelope)")
    assert body["data"]
    row = next(x for x in body["data"] if x["id"] == lead_id)
    assert_keys(row, LEAD_SUMMARY_KEYS_WITH_CONTACT, "GET /api/leads (LeadSummary, contact visible)")
    assert row["region"] == ["dubai"]
    assert row["source"] == "csv"
    assert row["owner_name"] is not None
    assert row["exit_state"] is None

    # marketing does not own this lead — redacted row must drop `identities`
    r2 = client.get("/api/leads?page_size=50", headers=hdr(emails, "marketing"))
    row2 = next(x for x in r2.json()["data"] if x["id"] == lead_id)
    assert_keys(row2, LEAD_SUMMARY_KEYS, "GET /api/leads (LeadSummary, contact redacted)")
    assert row2["contact_redacted"] is True


def test_contract_lead_detail_and_mutations():
    ids, lead_id, task_id, emails = fresh()

    r = client.get("/api/leads/%d" % lead_id, headers=hdr(emails, "admin"))
    assert r.status_code == 200
    assert_keys(r.json(), LEAD_DETAIL_KEYS, "GET /api/leads/{id}")
    for wrong in ("lead", "identities_nested", "regions_nested"):
        pass  # the real check is the exact-set assertion above — `lead` as a
              # nesting key would show up as an unexpected key there.
    assert "score_breakdown" in r.json() and isinstance(r.json()["score_breakdown"], list)

    r2 = client.patch("/api/leads/%d" % lead_id, headers=hdr(emails, "admin"),
                      json={"company": "Contract Co Renamed"})
    assert r2.status_code == 200
    assert_keys(r2.json(), LEAD_DETAIL_KEYS, "PATCH /api/leads/{id}")
    assert r2.json()["company"] == "Contract Co Renamed"

    new_owner = ids["office_manager"]
    r3 = client.post("/api/leads/%d/assign" % lead_id, headers=hdr(emails, "admin"),
                     json={"owner_user_id": new_owner})
    assert r3.status_code == 200
    assert_keys(r3.json(), LEAD_DETAIL_KEYS, "POST /api/leads/{id}/assign")
    assert r3.json()["owner_user_id"] == new_owner

    r4 = client.post("/api/leads/%d/stage" % lead_id, headers=hdr(emails, "admin"),
                     json={"stage": "engaged"})
    assert r4.status_code == 200
    assert_keys(r4.json(), LEAD_DETAIL_KEYS, "POST /api/leads/{id}/stage (forward)")
    assert r4.json()["stage"] == "engaged" and r4.json()["exit_state"] is None

    r5 = client.post("/api/leads/%d/stage" % lead_id, headers=hdr(emails, "admin"),
                     json={"stage": "unreachable"})
    assert r5.status_code == 200
    assert_keys(r5.json(), LEAD_DETAIL_KEYS, "POST /api/leads/{id}/stage (exit)")
    assert r5.json()["exit_state"] == "unreachable"

    r6 = client.post("/api/leads/%d/signal" % lead_id, headers=hdr(emails, "admin"),
                     json={"type": "wants_meeting", "note": "contract test"})
    assert r6.status_code == 200
    assert_keys(r6.json(), LEAD_DETAIL_KEYS | SIGNAL_EXTRA_KEYS, "POST /api/leads/{id}/signal")


def test_contract_lead_create():
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/leads", headers=hdr(emails, "admin"),
                    json={"name": "Contract Created Lead", "source": "lp_form",
                          "region": ["dubai", "japan"], "purpose": "relocation",
                          "email": "contract-created@test.example"})
    assert r.status_code == 200
    assert_keys(r.json(), LEAD_DETAIL_KEYS, "POST /api/leads")
    assert r.json()["region"] == ["dubai", "japan"]
    assert r.json()["purpose"] == "relocation"
    assert r.json()["source"] == "lp_form"


def test_contract_lead_create_missing_source_is_422_not_500():
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/leads", headers=hdr(emails, "admin"), json={"name": "No Source"})
    assert r.status_code == 422, "must be a structured 422, never a 500 KeyError"
    assert_keys(r.json(), ERROR_KEYS, "POST /api/leads (missing source)")


def test_contract_score():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/leads/%d/score" % lead_id, headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, SCORE_KEYS, "GET /api/leads/{id}/score")
    assert "components" not in body, "must be renamed to `breakdown`, not kept as `components`"
    assert isinstance(body["breakdown"], list) and body["breakdown"]
    assert set(body["breakdown"][0]) >= {"kind", "label_en", "points", "occurred_at"}


# ══ pipeline ═════════════════════════════════════════════════════════════════

def test_contract_pipeline():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/pipeline", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    # H2 — page/page_size are new top-level pagination metadata; "buckets" is
    # still the core shape (never {stages:[...]}).
    assert set(body.keys()) == {"buckets", "page", "page_size"}, \
        "GET /api/pipeline must be {buckets:[...], page, page_size}, not {stages:[...]}"
    assert len(body["buckets"]) == 6, "SPEC.md D10: six forward stages"
    for bucket in body["buckets"]:
        assert_keys(bucket, PIPELINE_BUCKET_KEYS, "GET /api/pipeline (bucket)")
        assert bucket["key"] in ("new", "nurturing", "engaged", "meeting_booked",
                                 "in_negotiation", "won")
        assert bucket["label_ja"], "every pipeline bucket needs a Japanese label"
        if bucket["leads"]:
            assert_keys(bucket["leads"][0], PIPELINE_LEAD_CARD_KEYS, "GET /api/pipeline (lead card)")


# ══ dashboard ═══════════════════════════════════════════════════════════════

def test_contract_dashboard():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/dashboard", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, DASHBOARD_KEYS, "GET /api/dashboard")
    assert "kpis" not in body and "flags" not in body, \
        "must be renamed to tiles/trend_flag, not kept as kpis/flags"

    for tile in body["tiles"]:
        assert_keys(tile, DASHBOARD_TILE_KEYS, "GET /api/dashboard (tile)")
        assert tile["key"] in ("total_leads", "first_sends", "meetings_booked",
                               "in_negotiation", "won", "expected_revenue")
    for point in body["trend"]:
        assert_keys(point, TREND_POINT_KEYS, "GET /api/dashboard (trend point)")
    for step in body["funnel"]:
        assert_keys(step, FUNNEL_STEP_KEYS, "GET /api/dashboard (funnel step)")
        assert "n" not in step, "count rows must not be bare {n} (AUDIT.md)"
    for row in body["by_source"]:
        assert_keys(row, BREAKDOWN_ITEM_KEYS, "GET /api/dashboard (by_source row)")
        assert "n" not in row, "count rows must not be bare {n} (AUDIT.md)"
    for row in body["by_region"]:
        assert_keys(row, BREAKDOWN_ITEM_KEYS, "GET /api/dashboard (by_region row)")
    if body["trend_flag"] is not None:
        assert_keys(body["trend_flag"], TREND_FLAG_KEYS, "GET /api/dashboard (trend_flag)")
    for hot in body["hot_leads"]:
        assert_keys(hot, HOT_LEAD_KEYS, "GET /api/dashboard (hot_leads item)")

    # admin has dashboard.per_rep — by_rep must be populated, not null
    assert body["by_rep"] is not None
    if body["by_rep"]:
        assert_keys(body["by_rep"][0], REP_PERFORMANCE_KEYS, "GET /api/dashboard (by_rep item)")

    # marketing lacks dashboard.per_rep — by_rep must be null (SPEC.md)
    r2 = client.get("/api/dashboard", headers=hdr(emails, "marketing"))
    assert r2.status_code == 200
    assert r2.json()["by_rep"] is None, \
        "by_rep must be null when the caller lacks dashboard.per_rep (SPEC.md)"


# ══ team ═════════════════════════════════════════════════════════════════════

def test_contract_team():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/team", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS | {"digest"}, "GET /api/team (envelope)")
    assert body["data"], "fixture's sales rep (bridged to staff) must appear"
    assert_keys(body["data"][0], TEAM_MEMBER_KEYS, "GET /api/team (TeamMember)")
    for wrong in ("staff_id", "open", "overdue", "worst_escalation"):
        assert wrong not in body["data"][0], \
            "GET /api/team: legacy field '%s' must not leak into the API response" % wrong


# ══ users ════════════════════════════════════════════════════════════════════

def test_contract_users():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/users", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/users (envelope)")
    assert_keys(body["data"][0], APP_USER_KEYS, "GET /api/users (AppUser)")

    r2 = client.post("/api/users", headers=hdr(emails, "admin"),
                     json={"email": "contract-newhire@test.example", "role": "sales"})
    assert r2.status_code == 200
    assert_keys(r2.json(), APP_USER_KEYS | CREATE_USER_EXTRA_KEYS, "POST /api/users")
    # AUDIT.md high finding: never claim a completed invitation for an
    # action that did not happen.
    assert r2.json()["invited"] is False
    new_id = r2.json()["id"]

    r3 = client.patch("/api/users/%s" % new_id, headers=hdr(emails, "admin"),
                      json={"is_active": True})
    assert r3.status_code == 200
    assert_keys(r3.json(), APP_USER_KEYS, "PATCH /api/users/{id}")


# ══ errors / health ═════════════════════════════════════════════════════════

def test_contract_error_shape():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/leads/999999", headers=hdr(emails, "admin"))
    assert r.status_code == 404
    assert_keys(r.json(), ERROR_KEYS, "404 body")
    assert_keys(r.json()["error"], ERROR_BODY_KEYS, "404 error object")

    r2 = client.get("/api/leads")   # no Authorization header
    assert r2.status_code == 401
    assert_keys(r2.json(), ERROR_KEYS, "401 body")
    assert_keys(r2.json()["error"], ERROR_BODY_KEYS, "401 error object")


def test_contract_health():
    fresh()
    r = client.get("/api/health")
    assert r.status_code == 200
    assert_keys(r.json(), HEALTH_KEYS, "GET /api/health")


# ══ SPEC-V2 §1 — activity / scoring model ═══════════════════════════════════

def test_contract_activity():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/activity", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/activity (envelope)")
    assert body["data"], "fixture's open/reply events must appear"
    assert_keys(body["data"][0], ACTIVITY_ITEM_KEYS, "GET /api/activity (item)")

    # SPEC-V2 §Roles: sales sees only its own leads' activity.
    r2 = client.get("/api/activity", headers=hdr(emails, "sales"))
    assert r2.status_code == 200
    for item in r2.json()["data"]:
        assert item["lead_id"] == lead_id


def test_contract_scoring_model():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/scoring/model", headers=hdr(emails, "sales"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, SCORING_MODEL_KEYS, "GET /api/scoring/model")
    assert body["rules"]
    assert_keys(body["rules"][0], SCORING_MODEL_RULE_KEYS, "GET /api/scoring/model (rule)")
    assert {r["group"] for r in body["rules"]} <= {"behaviour", "fact"}
    assert_keys(body["decay"], SCORING_MODEL_DECAY_KEYS, "GET /api/scoring/model (decay)")
    assert body["threshold"] == 40


# ══ SPEC-V2 §2 — nurture / sequences ═════════════════════════════════════════

def test_contract_sequences():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/sequences", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/sequences (envelope)")
    assert body["data"]
    assert_keys(body["data"][0], SEQUENCE_SUMMARY_KEYS, "GET /api/sequences (item)")

    r2 = client.get("/api/sequences/1", headers=hdr(emails, "office_manager"))
    assert r2.status_code == 200
    detail = r2.json()
    assert_keys(detail, SEQUENCE_SUMMARY_KEYS | SEQUENCE_DETAIL_EXTRA_KEYS, "GET /api/sequences/{id}")
    assert detail["steps"]
    assert_keys(detail["steps"][0], SEQUENCE_STEP_KEYS, "GET /api/sequences/{id} (step)")
    assert_keys(detail["steps"][0]["stats"], STEP_STATS_KEYS, "GET /api/sequences/{id} (step stats)")
    assert_keys(detail["editorial_rule"], EDITORIAL_RULE_KEYS, "GET /api/sequences/{id} (editorial_rule)")
    assert detail["exit_conditions"]
    for cond in detail["exit_conditions"]:
        assert_keys(cond, EXIT_CONDITION_KEYS, "GET /api/sequences/{id} (exit_condition)")
        assert cond["provisional"] is True, "D17: exit conditions are provisional, not ratified"

    r3 = client.get("/api/sequences/1/stats", headers=hdr(emails, "marketing"))
    assert r3.status_code == 200
    stats = r3.json()
    assert_keys(stats, SEQUENCE_SUMMARY_KEYS | SEQUENCE_STATS_EXTRA_KEYS, "GET /api/sequences/{id}/stats")
    assert stats["duplicate_sends_detected"] == 0, \
        "sends.(lead_id,step_id) is UNIQUE — this must always be provably zero"
    assert_keys(stats["per_step"][0], PER_STEP_STAT_KEYS, "GET /api/sequences/{id}/stats (per_step)")

    r4 = client.patch("/api/sequences/1/steps/1", headers=hdr(emails, "marketing"),
                      json={"subject": "Updated subject"})
    assert r4.status_code == 200
    assert_keys(r4.json(), SEQUENCE_STEP_KEYS, "PATCH /api/sequences/{id}/steps/{n}")
    assert r4.json()["subject"] == "Updated subject"

    # office_manager may view but not edit (SPEC-V2 §Roles: "view" only)
    r5 = client.patch("/api/sequences/1/steps/1", headers=hdr(emails, "office_manager"),
                      json={"subject": "should be denied"})
    assert r5.status_code == 403

    # sales has no nurture access at all ("❌ no tab")
    r6 = client.get("/api/sequences", headers=hdr(emails, "sales"))
    assert r6.status_code == 403
    r7 = client.get("/api/sequences/1", headers=hdr(emails, "sales"))
    assert r7.status_code == 403
    r8 = client.get("/api/sequences/1/stats", headers=hdr(emails, "sales"))
    assert r8.status_code == 403


# ══ SPEC-V2 §3 — SNS ══════════════════════════════════════════════════════════

def test_contract_sns():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/sns/patterns", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/sns/patterns (envelope)")
    assert body["data"]
    assert_keys(body["data"][0], SNS_PATTERN_KEYS, "GET /api/sns/patterns (item)")
    assert body["data"][0]["leads_sample"]
    assert_keys(body["data"][0]["leads_sample"][0], SNS_PATTERN_SAMPLE_KEYS,
               "GET /api/sns/patterns (leads_sample item)")

    r2 = client.get("/api/sns/funnel", headers=hdr(emails, "office_manager"))
    assert r2.status_code == 200
    funnel = r2.json()
    assert set(funnel.keys()) == {"steps"}
    assert len(funnel["steps"]) == 5
    for step in funnel["steps"]:
        assert_keys(step, SNS_FUNNEL_STEP_KEYS, "GET /api/sns/funnel (step)")
    manual_steps = {s["step"] for s in funnel["steps"] if not s["tracked"]}
    assert manual_steps == {"sns_post", "lp_view"}, \
        "the two steps with no SNS analytics API wired must be tracked=false, source='manual'"

    r3 = client.get("/api/sns/patterns", headers=hdr(emails, "sales"))
    assert r3.status_code == 403


# ══ SPEC-V2 §4 — booking ══════════════════════════════════════════════════════

def test_contract_booking():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/booking/settings", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, BOOKING_SETTINGS_KEYS, "GET /api/booking/settings")
    assert_keys(body["meeting_types"][0], BOOKING_MEETING_TYPE_KEYS,
               "GET /api/booking/settings (meeting type)")
    assert_keys(body["public_booking_page"], NOT_CONFIGURED_KEYS,
               "GET /api/booking/settings (public_booking_page)")
    assert body["public_booking_page"]["configured"] is False, \
        "the public booking page is out of scope and must never claim to be live"

    r2 = client.patch("/api/booking/settings", headers=hdr(emails, "office_manager"),
                      json={"note": "updated by contract test"})
    assert r2.status_code == 200
    assert r2.json()["note"] == "updated by contract test"

    r3 = client.get("/api/booking/slots?meeting_type=consult_30&week=2026-08-17",
                    headers=hdr(emails, "admin"))
    assert r3.status_code == 200
    slots_body = r3.json()
    assert_keys(slots_body, BOOKING_SLOTS_KEYS, "GET /api/booking/slots")
    assert slots_body["slots"], "the fixture rep must have at least one free slot in the week"
    assert_keys(slots_body["slots"][0], BOOKING_SLOT_KEYS, "GET /api/booking/slots (slot)")

    for role in ("marketing", "sales"):
        r4 = client.get("/api/booking/settings", headers=hdr(emails, role))
        assert r4.status_code == 403, "row 'Booking settings': %s must be denied" % role


# ══ SPEC-V2 §5 — assignment rules / calendar ═════════════════════════════════

def test_contract_assignment_and_calendar():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/assignment/rules?sample_lead_id=%d" % lead_id, headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS | {"evaluated_for_sample_lead_id", "winning_rule_id"},
               "GET /api/assignment/rules (with sample)")
    assert_keys(body["data"][0], ASSIGNMENT_RULE_KEYS | {"fires_for_sample"},
               "GET /api/assignment/rules (rule, with sample)")
    assert body["winning_rule_id"] is not None, "the region=dubai rule must fire for the fixture lead"

    r2 = client.put("/api/assignment/rules", headers=hdr(emails, "office_manager"),
                    json={"rules": [
                        {"priority": 1, "match_field": "region", "match_value": "dubai",
                         "owner_user_id": ids["sales"], "is_fallback": False},
                        {"priority": 99, "owner_user_id": ids["admin"], "is_fallback": True},
                    ]})
    assert r2.status_code == 200
    assert_keys(r2.json(), ENVELOPE_KEYS, "PUT /api/assignment/rules (envelope)")
    assert_keys(r2.json()["data"][0], ASSIGNMENT_RULE_KEYS, "PUT /api/assignment/rules (rule)")

    r3 = client.get("/api/calendar?user=%s&week=2026-08-17" % ids["sales"], headers=hdr(emails, "admin"))
    assert r3.status_code == 200
    cal = r3.json()
    assert_keys(cal, CALENDAR_KEYS, "GET /api/calendar")
    assert cal["events"]
    assert_keys(cal["events"][0], CALENDAR_EVENT_ITEM_KEYS, "GET /api/calendar (event)")
    assert_keys(cal["google_calendar_sync"], NOT_CONFIGURED_KEYS, "GET /api/calendar (google_calendar_sync)")
    assert cal["google_calendar_sync"]["configured"] is False, \
        "no Google Calendar connection exists — must never claim otherwise"

    for role in ("marketing", "sales"):
        r4 = client.get("/api/assignment/rules", headers=hdr(emails, role))
        assert r4.status_code == 403, "row 'Assignment rules': %s must be denied" % role
        r5 = client.get("/api/calendar", headers=hdr(emails, role))
        assert r5.status_code == 403, "row 'Assign' (calendar): %s must be denied" % role


# ══ SPEC-V2 §6 — card scan ════════════════════════════════════════════════════

def test_contract_lead_scan():
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/leads/scan", headers=hdr(emails, "sales"),
                    data={"name": "Scanned Person", "company": "Scan Co",
                          "email": "scanned@test.example", "consent_given": "true"},
                    files={"image": ("card.jpg", b"\xff\xd8\xff\xe0-fake-jpeg-bytes", "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, LEAD_DETAIL_KEYS | {"extraction"}, "POST /api/leads/scan")
    assert_keys(body["extraction"], NOT_CONFIGURED_KEYS, "POST /api/leads/scan (extraction)")
    assert body["extraction"]["configured"] is False, "no OCR provider is wired — must never invent fields"
    assert body["card_image_path"]
    assert body["consent"]["basis"] == "explicit"

    # SPEC-V2 §Roles: all four roles may scan a card.
    for role in ("admin", "marketing", "office_manager"):
        r2 = client.post("/api/leads/scan", headers=hdr(emails, role),
                         data={"name": "Another Scan (%s)" % role},
                         files={"image": ("card.jpg", b"\xff\xd8\xff\xe0-fake", "image/jpeg")})
        assert r2.status_code == 200, "row 'Card scan': %s must be allowed" % role


# ══ SPEC-V2 §7 — CSV / Excel import ═══════════════════════════════════════════

def test_contract_import_analyze_and_commit():
    ids, lead_id, task_id, emails = fresh()
    csv_bytes = "name,email,company\n山田 太郎,taro@test.example,Taro Co\n".encode("utf-8")
    r = client.post("/api/import/analyze", headers=hdr(emails, "marketing"),
                    files={"file": ("leads.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, IMPORT_ANALYZE_KEYS, "POST /api/import/analyze")
    assert body["rows_seen"] == 1
    token = body["token"]

    r2 = client.post("/api/import/commit", headers=hdr(emails, "marketing"),
                     json={"token": token, "mapping": body["guessed_mapping"],
                           "answers": {"consent_basis": "unknown"}})
    assert r2.status_code == 200
    assert_keys(r2.json(), IMPORT_COMMIT_KEYS, "POST /api/import/commit")
    assert r2.json()["rows_created"] == 1
    assert r2.json()["consent_basis_applied"] == "unknown", \
        "consent defaults to unknown unless the human running the import says otherwise (SPEC-V2 §7)"

    for role in ("office_manager", "sales"):
        r3 = client.post("/api/import/analyze", headers=hdr(emails, role),
                         files={"file": ("x.csv", b"name\nX\n", "text/csv")})
        assert r3.status_code == 403, "row 'CSV import': %s must be denied" % role


# ══ SPEC-V2 §8 — integrations ═════════════════════════════════════════════════

def test_contract_integrations():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/integrations", headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/integrations (envelope)")
    assert body["data"]
    assert_keys(body["data"][0], INTEGRATION_ROW_KEYS, "GET /api/integrations (row)")
    assert all(row["connected"] is False for row in body["data"]), \
        "no integration may claim to be connected — none are wired in this environment"

    r2 = client.post("/api/integrations/gohighlevel/sync", headers=hdr(emails, "admin"))
    assert r2.status_code == 200
    assert_keys(r2.json(), GHL_SYNC_KEYS, "POST /api/integrations/gohighlevel/sync")
    assert r2.json()["configured"] is False, "must never fake a GoHighLevel sync"
    assert r2.json()["read_only"] is True

    r3 = client.post("/api/integrations/gohighlevel/sync", headers=hdr(emails, "marketing"))
    assert r3.status_code == 403, "row 'Integrations': marketing may view but not sync"

    r4 = client.get("/api/integrations", headers=hdr(emails, "sales"))
    assert r4.status_code == 403, "row 'Integrations': sales has no tab at all"


# ══ SPEC-V2 §9 — AI reply drafting ════════════════════════════════════════════

def test_contract_replies():
    ids, lead_id, task_id, emails = fresh()
    r = client.get("/api/leads/%d/replies" % lead_id, headers=hdr(emails, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, ENVELOPE_KEYS, "GET /api/leads/{id}/replies (envelope)")
    assert body["data"]
    assert_keys(body["data"][0], REPLY_KEYS, "GET /api/leads/{id}/replies (item)")
    reply_id = body["data"][0]["id"]

    r2 = client.post("/api/replies/%d/draft" % reply_id, headers=hdr(emails, "admin"))
    assert r2.status_code == 200
    assert_keys(r2.json(), NOT_CONFIGURED_KEYS, "POST /api/replies/{id}/draft")
    assert r2.json()["configured"] is False, "no AI model is wired — must never fabricate a draft"

    r3 = client.post("/api/replies/%d/send" % reply_id, headers=hdr(emails, "admin"),
                     json={"text": "Thanks — let's talk next week.",
                           "verdict": {"human": True, "wants_meeting": True,
                                      "partnership": False, "high_budget": False}})
    assert r3.status_code == 200
    assert_keys(r3.json(), REPLY_KEYS | REPLY_SEND_EXTRA_KEYS, "POST /api/replies/{id}/send")
    assert r3.json()["status"] == "sent"

    r4 = client.post("/api/replies/%d/send" % reply_id, headers=hdr(emails, "admin"),
                     json={"action": "not_real"})
    assert r4.status_code == 200
    assert_keys(r4.json(), REPLY_KEYS, "POST /api/replies/{id}/send (not_real)")
    assert r4.json()["status"] == "voided", "D7: 'this wasn't real' must remove the points"


# ══ SPEC-V2 §10 — voice notes / push / lead drawer actions ═══════════════════

def test_contract_voice_notes():
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/leads/%d/notes" % lead_id, headers=hdr(emails, "sales"),
                    data={"duration_seconds": "12.5"},
                    files={"audio": ("note.m4a", b"fake-audio-bytes", "audio/m4a")})
    assert r.status_code == 200
    body = r.json()
    assert_keys(body, NOTE_KEYS, "POST /api/leads/{id}/notes")
    assert_keys(body["transcription"], NOT_CONFIGURED_KEYS, "POST /api/leads/{id}/notes (transcription)")
    assert body["transcription"]["configured"] is False
    assert body["transcript"] is None, "transcription is not wired — must never invent one"

    r2 = client.get("/api/leads/%d/notes" % lead_id, headers=hdr(emails, "sales"))
    assert r2.status_code == 200
    body2 = r2.json()
    assert_keys(body2, ENVELOPE_KEYS, "GET /api/leads/{id}/notes (envelope)")
    assert_keys(body2["data"][0], NOTE_KEYS, "GET /api/leads/{id}/notes (item)")


def test_contract_push():
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/push/register", headers=hdr(emails, "sales"),
                    json={"token": "ExponentPushToken[contract-test]", "platform": "ios"})
    assert r.status_code == 200
    assert_keys(r.json(), PUSH_REGISTER_KEYS, "POST /api/push/register")

    r2 = client.get("/api/push/settings", headers=hdr(emails, "sales"))
    assert r2.status_code == 200
    assert_keys(r2.json(), PUSH_SETTINGS_KEYS, "GET /api/push/settings")

    r3 = client.patch("/api/push/settings", headers=hdr(emails, "sales"), json={"notify_hot_lead": False})
    assert r3.status_code == 200
    assert_keys(r3.json(), PUSH_SETTINGS_KEYS, "PATCH /api/push/settings")
    assert r3.json()["notify_hot_lead"] is False

    r4 = client.post("/api/push/test", headers=hdr(emails, "sales"))
    assert r4.status_code == 200
    assert_keys(r4.json(), PUSH_TEST_KEYS, "POST /api/push/test")
    assert r4.json()["configured"] is False, "no push provider is configured in this environment"


def test_contract_lead_drawer_actions():
    ids, lead_id, task_id, emails = fresh()
    r = client.post("/api/leads/%d/notify-rep" % lead_id, headers=hdr(emails, "admin"),
                    json={"note": "please call them back"})
    assert r.status_code == 200
    assert_keys(r.json(), NOTIFY_REP_KEYS, "POST /api/leads/{id}/notify-rep")
    assert_keys(r.json()["task"], TASK_KEYS, "POST /api/leads/{id}/notify-rep (task)")
    assert_keys(r.json()["push_attempt"], NOT_CONFIGURED_KEYS | {"recipient_has_token"},
               "POST /api/leads/{id}/notify-rep (push_attempt)")

    r2 = client.post("/api/leads/%d/showroom-visit" % lead_id, headers=hdr(emails, "sales"),
                     json={"note": "来店対応"})
    assert r2.status_code == 200
    assert_keys(r2.json(), CALENDAR_EVENT_ACTION_KEYS, "POST /api/leads/{id}/showroom-visit")

    r3 = client.post("/api/leads/%d/site-inspection" % lead_id, headers=hdr(emails, "sales"),
                     json={"region": "lombok"})
    assert r3.status_code == 200
    assert_keys(r3.json(), CALENDAR_EVENT_ACTION_KEYS, "POST /api/leads/{id}/site-inspection")

    r4 = client.post("/api/leads/%d/set-contact-date" % lead_id, headers=hdr(emails, "sales"),
                     json={"date": "2026-09-01", "note": "call back in September"})
    assert r4.status_code == 200
    assert_keys(r4.json(), LEAD_DETAIL_KEYS | SET_CONTACT_DATE_EXTRA_KEYS,
               "POST /api/leads/{id}/set-contact-date")
    assert r4.json()["revisit_at"] == "2026-09-01"

    r5 = client.post("/api/leads/%d/follow" % lead_id, headers=hdr(emails, "sales"),
                     json={"note": "check back in a week"})
    assert r5.status_code == 200
    assert_keys(r5.json(), FOLLOW_KEYS, "POST /api/leads/{id}/follow")
    assert_keys(r5.json()["task"], TASK_KEYS, "POST /api/leads/{id}/follow (task)")

    # marketing does not own this lead -> denied on every drawer action
    r6 = client.post("/api/leads/%d/follow" % lead_id, headers=hdr(emails, "marketing"), json={})
    assert r6.status_code == 403


if __name__ == "__main__":
    import traceback
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print("  PASS  %s" % name)
            passed += 1
        except Exception:
            print("  FAIL  %s" % name)
            traceback.print_exc()
            failed += 1
    print("\n%d passed, %d failed, %d total" % (passed, failed, len(fns)))
    sys.exit(1 if failed else 0)
