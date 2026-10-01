"""The permission matrix (SPEC.md "Permission matrix — enforced server-side").

Every row of that table gets one test function here. Each function asserts
BOTH directions for every role touched by that row: the roles the matrix
marks ✅ get a real response, the roles it marks ❌ get a real 403 with
{error:{code,message}} — never an empty 200. "A denial that is not tested
does not count as implemented" (SPEC.md) — so every ❌ cell in the table has
an explicit assert somewhere below, with a message naming the row it protects.

Also here: pagination shape, contact-detail redaction, and the rule that a
sales user's list endpoints never contain another rep's rows.

Runs with no cloud dependency — EXCEEDBOX_DEV_AUTH=1 and no
SUPABASE_JWT_SECRET, exactly what SPEC.md requires ("Nothing about Supabase
may be required to run the tests").

Run:  python3 -m pytest tests -q     (or: python3 tests/test_permissions.py)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCEEDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "test_perms.db")
# card scans / voice notes / CSV uploads must land in a throwaway tempdir too
# — never in the real project's data/uploads (app/media.py reads this at
# import time, so it must be set before app.api is imported below).
os.environ["EXCEEDBOX_UPLOAD_ROOT"] = tempfile.mkdtemp()
os.environ["EXCEEDBOX_DEV_AUTH"] = "1"
os.environ.pop("SUPABASE_JWT_SECRET", None)
# N1 (FABLE-AUDIT-R2.md) — app/auth.py's ALLOWED_EMAIL_DOMAINS is read once
# at import time, and pytest imports every test module into one process, so
# this has to be the same string (a superset of every suite's login domain)
# in every test file that touches auth — whichever imports first "wins" the
# module-level constant otherwise. See tests/test_c1_media_security.py's
# N1 tests for the actual domain-rejection/takeover assertions.
os.environ["EXCEEDBOX_ALLOWED_EMAIL_DOMAINS"] = "test.example,contract.example,fable-fix.example,exceed-re.ae"

from fastapi.testclient import TestClient   # noqa: E402

from app import auth, db, devauth, ingest, scoring, tasks   # noqa: E402
from app.api import app                      # noqa: E402

client = TestClient(app)

ROLES = ("admin", "marketing", "office_manager", "sales")
EMAILS = {r: "%s@test.example" % r for r in ROLES}
EMAILS["sales2"] = "sales2@test.example"      # a second sales rep, for isolation tests


def fresh():
    """One admin, one marketing, one office_manager, two sales reps (so
    cross-rep isolation is provable), and six leads: one owned by each of the
    first four, one owned by the second sales rep, and one owned by nobody
    (D14 — before assignment, nobody owns anyone)."""
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

    ids = {}
    for role in ROLES:
        uid = "u-%s" % role
        con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active)
                       VALUES (?,?,?,?,1)""", (uid, EMAILS[role], role.title(), role))
        ids[role] = uid
    con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active)
                   VALUES ('u-sales2',?,?,?,1)""", (EMAILS["sales2"], "Sales Two", "sales"))
    ids["sales2"] = "u-sales2"

    leads = {}
    for key in ("admin", "marketing", "office_manager", "sales", "sales2"):
        lid = ingest.upsert_lead(con, name="Lead of %s" % key, channel="csv",
                                 email="lead-%s@test.example" % key)["lead_id"]
        con.execute("UPDATE leads SET owner_user_id=? WHERE id=?", (ids[key], lid))
        leads[key] = lid
    leads["unowned"] = ingest.upsert_lead(con, name="Unowned Lead", channel="csv",
                                          email="unowned@test.example")["lead_id"]
    con.commit()

    # ── SPEC-V2 fixtures — enough for one denial test per new capability. ────
    con.execute("INSERT INTO sequences (id,name) VALUES (1,'Perm Test Sequence')")
    con.execute("""INSERT INTO sequence_steps (id, sequence_id, step_no, offset_days, subject,
                                                purpose, question, question_ja, cta)
                   VALUES (1,1,1,0,'Subject','purpose','Q?','質問？','CTA')""")
    con.execute("""INSERT INTO sns_patterns (key, title_ja, title_en, description_ja, description_en)
                   VALUES ('A','パターンA','Pattern A','説明','description')""")
    con.execute("""INSERT INTO sns_funnel_manual (step_key, count, note) VALUES
                   ('sns_post', 5, 'perm test'), ('lp_view', 5, 'perm test')""")
    con.execute("""INSERT INTO booking_settings (id, jst_gst_gap_hours, business_hours_start,
                                                  business_hours_end, slot_length_minutes, timezone, note)
                   VALUES (1, 5, '09:00', '18:00', 30, 'Asia/Tokyo', 'perm test')""")
    con.execute("""INSERT INTO booking_meeting_types (key, label_en, label_ja, duration_minutes)
                   VALUES ('consult_30','Free 30-min consultation','無料30分相談',30)""")
    con.execute("""INSERT INTO assignment_rules (priority, match_field, match_value, owner_user_id, is_fallback)
                   VALUES (99,NULL,NULL,?,1)""", (ids["admin"],))
    con.execute("""INSERT INTO integrations (key, label_en, label_ja, connected, status_detail,
                                             what_is_needed, read_only)
                   VALUES ('gohighlevel','GoHighLevel','GoHighLevel',0,'not connected','API key',1)""")
    con.execute("""INSERT INTO replies (lead_id, channel, subject, body, received_at, status)
                   VALUES (?,'email','Re: test','test reply body','2026-08-15 09:00:00','received')""",
               (leads["sales"],))
    con.commit()
    return ids, leads


def hdr(role_key: str) -> dict:
    # N1 (FABLE-AUDIT-R2.md) — a stable sub per role/email, not a fresh
    # random uuid on every call. Realistic (a real login keeps the same
    # `sub` across requests) and required now: load_or_provision() rejects
    # a second, different sub claiming an email already bound to a first
    # one — exactly the account-takeover shape N1 closes — so minting a new
    # random sub on every call of the same role would trip that check.
    return {"Authorization": "Bearer " + devauth.mint(sub="test-sub-%s" % role_key, email=EMAILS[role_key])}


def err_code(resp) -> str:
    return resp.json()["error"]["code"]


def _task_for(con, ids: dict, owner_role: str, lead_id: int, reason: str, type_: str = "call") -> int:
    """Create a real task owned by `owner_role`, bridging their app_user to a
    staff row the same way auth.ensure_staff_bridge() does in production
    (assign/signal/task-create all call it lazily) — so these tests exercise
    the real ownership join tasks.py/api.py use, not a shortcut."""
    staff_id = auth.ensure_staff_bridge(con, ids[owner_role], EMAILS[owner_role])
    return tasks.create(con, lead_id, type_, reason=reason, owner_id=staff_id,
                        due_at=datetime(2026, 8, 20, 10, 0, 0), created_by="human")


# ── auth basics ───────────────────────────────────────────────────────────

def test_no_token_is_401_not_403():
    fresh()
    r = client.get("/api/leads")
    assert r.status_code == 401
    assert "error" in r.json()


def test_unknown_login_auto_provisions_inactive_and_fails_closed():
    """SPEC.md: fail closed — an unknown person can never see data until an
    admin activates them."""
    fresh()
    token = devauth.mint(email="never-seen-before@test.example")
    r = client.get("/api/me", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 403, "an auto-provisioned, unactivated user must be blocked"
    con = db.connect()
    row = con.execute("SELECT role, is_active FROM app_user WHERE email=?",
                      ("never-seen-before@test.example",)).fetchone()
    assert row["role"] == "sales" and row["is_active"] == 0, \
        "auto-provision defaults: role=sales, is_active=false"


def test_devtoken_reuses_a_stable_sub_across_repeated_calls():
    """N1 (FABLE-AUDIT-R2.md) — GET /api/devtoken now mints a deterministic
    `sub` per email instead of a fresh random one, precisely so repeated
    local/dev logins for the SAME person don't trip load_or_provision()'s
    new sub-mismatch rejection (the same rule that blocks the real
    account-takeover shape). Two separate devtoken calls for the same email
    must both keep working."""
    ids, leads = fresh()
    r1 = client.get("/api/devtoken?email=%s" % EMAILS["admin"])
    assert r1.status_code == 200, r1.text
    r_me1 = client.get("/api/me", headers={"Authorization": "Bearer " + r1.json()["token"]})
    assert r_me1.status_code == 200, r_me1.text

    r2 = client.get("/api/devtoken?email=%s" % EMAILS["admin"])
    assert r2.status_code == 200
    r_me2 = client.get("/api/me", headers={"Authorization": "Bearer " + r2.json()["token"]})
    assert r_me2.status_code == 200, \
        "N1: a second /api/devtoken call for the same email must not trip account_conflict"


def test_inactive_account_is_403_everywhere():
    ids, leads = fresh()
    con = db.connect()
    con.execute("UPDATE app_user SET is_active=0 WHERE id='u-sales'")
    con.commit()
    r = client.get("/api/today", headers=hdr("sales"))
    assert r.status_code == 403
    assert err_code(r) == "inactive_account"


def test_errors_are_never_a_200_with_an_error_body():
    fresh()
    r = client.get("/api/leads/99999", headers=hdr("admin"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# ── matrix row: see own tasks (all roles ✅) ─────────────────────────────────

def test_matrix_see_own_tasks_all_roles_allowed():
    """AUDIT.md tests/test_permissions.py:130 finding: this used to assert
    only 200 plus envelope keys, with no task fixture or ownership assertion
    at all — it would pass even if every role saw every task. Now: a real
    task per role, and each role's /api/today must contain THEIRS and never
    another role's (SPEC.md rule 1, exercised end to end through the HTTP
    layer, not just the SQL filter in isolation)."""
    ids, leads = fresh()
    con = db.connect()
    task_ids = {role: _task_for(con, ids, role, leads[role], reason="task for %s" % role)
               for role in ROLES}

    for role in ROLES:
        r = client.get("/api/today", headers=hdr(role))
        assert r.status_code == 200, "row 'see own tasks': %s must be allowed" % role
        body = r.json()
        assert set(body) == {"data", "page", "page_size", "total"}
        returned_ids = {t["id"] for t in body["data"]}
        assert task_ids[role] in returned_ids, \
            "row 'see own tasks': %s must see their OWN task in /api/today" % role
        for other in ROLES:
            if other != role:
                assert task_ids[other] not in returned_ids, \
                    "row 'see own tasks': %s must NOT see %s's task in /api/today" % (role, other)


# ── matrix row: see everyone's tasks (admin/office_manager ✅, others ❌) ────

def test_matrix_see_everyones_tasks():
    """AUDIT.md tests/test_permissions.py:140 finding: this used to check
    only the /api/team status code, never that the ✅ roles actually receive
    another rep's accountability data. Now: two real tasks (sales, sales2),
    and admin/office_manager's /api/team must surface BOTH reps' rows with
    real task counts — proving 'see everyone's tasks' actually works, not
    just that the door isn't locked."""
    ids, leads = fresh()
    con = db.connect()
    _task_for(con, ids, "sales", leads["sales"], reason="sales's own task")
    _task_for(con, ids, "sales2", leads["sales2"], reason="sales2's own task")

    for role in ("admin", "office_manager"):
        r = client.get("/api/team", headers=hdr(role))
        assert r.status_code == 200, "row 'see everyone's tasks': %s must be allowed" % role
        by_email = {}
        for m in r.json()["data"]:
            row = con.execute("SELECT email FROM app_user WHERE id=?", (m["user_id"],)).fetchone()
            if row:
                by_email[row["email"]] = m
        assert EMAILS["sales"] in by_email, \
            "row 'see everyone's tasks': %s must see sales's accountability row" % role
        assert EMAILS["sales2"] in by_email, \
            "row 'see everyone's tasks': %s must see sales2's accountability row" % role
        assert by_email[EMAILS["sales"]]["tasks_open"] >= 1, \
            "%s must see sales's real task count, not an empty placeholder" % role
        assert by_email[EMAILS["sales2"]]["tasks_open"] >= 1, \
            "%s must see sales2's real task count, not an empty placeholder" % role

    for role in ("marketing", "sales"):
        r = client.get("/api/team", headers=hdr(role))
        assert r.status_code == 403, "row 'see everyone's tasks': %s must be denied" % role
        assert err_code(r) == "forbidden"


# ── matrix row: see own leads (all roles ✅) ─────────────────────────────────

def test_matrix_see_own_leads_all_roles_allowed():
    _, leads = fresh()
    for role in ROLES:
        r = client.get("/api/leads/%d" % leads[role], headers=hdr(role))
        assert r.status_code == 200, "row 'see own leads': %s must see their own lead" % role


# ── matrix row: see all leads (admin/marketing/office_manager ✅, sales ❌) ──

def test_matrix_see_all_leads():
    _, leads = fresh()
    other = leads["office_manager"]     # a lead none of the three below own
    for role in ("admin", "marketing", "office_manager"):
        r = client.get("/api/leads/%d" % leads["sales"], headers=hdr(role))
        assert r.status_code == 200, "row 'see all leads': %s must see any lead" % role
    r = client.get("/api/leads/%d" % other, headers=hdr("sales"))
    assert r.status_code == 403, "row 'see all leads': sales must be denied someone else's lead"
    assert err_code(r) == "forbidden"


def test_sales_list_never_contains_another_reps_lead():
    """SPEC.md rule 1, and the reason it must be a SQL filter: even asking
    for a huge page size must never surface sales2's lead in sales's list."""
    ids, leads = fresh()
    r = client.get("/api/leads?page_size=200", headers=hdr("sales"))
    assert r.status_code == 200
    returned_ids = {row["id"] for row in r.json()["data"]}
    assert leads["sales2"] not in returned_ids
    assert leads["unowned"] not in returned_ids
    assert returned_ids == {leads["sales"]}
    # the ?owner= override must not let sales peek at someone else either
    r2 = client.get("/api/leads?owner=%s&page_size=200" % ids["sales2"], headers=hdr("sales"))
    returned_ids2 = {row["id"] for row in r2.json()["data"]}
    assert leads["sales2"] not in returned_ids2, "the owner= query param must not override RBAC"


# ── matrix row: edit a lead they own (all roles ✅) ──────────────────────────

def test_matrix_edit_own_lead_all_roles_allowed():
    _, leads = fresh()
    for role in ROLES:
        r = client.patch("/api/leads/%d" % leads[role], headers=hdr(role),
                         json={"company": "Updated by %s" % role})
        assert r.status_code == 200, "row 'edit a lead they own': %s must be allowed" % role
        assert r.json()["company"] == "Updated by %s" % role


# ── matrix row: edit any lead (admin/office_manager ✅, marketing/sales ❌) ──

def test_matrix_edit_any_lead():
    _, leads = fresh()
    target = leads["unowned"]
    for role in ("admin", "office_manager"):
        r = client.patch("/api/leads/%d" % target, headers=hdr(role), json={"company": "X"})
        assert r.status_code == 200, "row 'edit any lead': %s must be allowed" % role
    for role in ("marketing", "sales"):
        r = client.patch("/api/leads/%d" % leads["office_manager"], headers=hdr(role),
                         json={"company": "should not happen"})
        assert r.status_code == 403, "row 'edit any lead': %s must be denied a lead they don't own" % role


# ── matrix row: assign / reassign an owner (admin/office_manager ✅) ────────

def test_matrix_assign_reassign_owner():
    ids, leads = fresh()
    for role in ("admin", "office_manager"):
        r = client.post("/api/leads/%d/assign" % leads["unowned"], headers=hdr(role),
                        json={"owner_user_id": ids["sales"]})
        assert r.status_code == 200, "row 'assign/reassign an owner': %s must be allowed" % role
    for role in ("marketing", "sales"):
        r = client.post("/api/leads/%d/assign" % leads["unowned"], headers=hdr(role),
                        json={"owner_user_id": ids["sales"]})
        assert r.status_code == 403, "row 'assign/reassign an owner': %s must be denied" % role
        assert err_code(r) == "forbidden"


# ── matrix row: move a lead's pipeline stage (admin/office_manager any,
#    sales own only, marketing never) ────────────────────────────────────────

def test_matrix_move_stage():
    _, leads = fresh()
    for role in ("admin", "office_manager"):
        r = client.post("/api/leads/%d/stage" % leads["sales"], headers=hdr(role),
                        json={"stage": "nurturing"})
        assert r.status_code == 200, "row 'move stage': %s must move ANY lead's stage" % role

    r = client.post("/api/leads/%d/stage" % leads["sales"], headers=hdr("sales"),
                    json={"stage": "nurturing"})
    assert r.status_code == 200, "row 'move stage': sales must move their OWN lead's stage"

    r = client.post("/api/leads/%d/stage" % leads["sales2"], headers=hdr("sales"),
                    json={"stage": "nurturing"})
    assert r.status_code == 403, "row 'move stage': sales must be denied on a lead they don't own"

    r = client.post("/api/leads/%d/stage" % leads["marketing"], headers=hdr("marketing"),
                    json={"stage": "nurturing"})
    assert r.status_code == 403, "row 'move stage': marketing is denied even on a lead they own"


# ── matrix row: grant the manual +30 (admin/office_manager any, sales own,
#    marketing never) ────────────────────────────────────────────────────────

def test_matrix_grant_signal():
    _, leads = fresh()
    for role in ("admin", "office_manager"):
        r = client.post("/api/leads/%d/signal" % leads["sales"], headers=hdr(role),
                        json={"type": "wants_meeting", "note": "asked to meet"})
        assert r.status_code == 200, "row 'grant manual +30': %s must grant it on ANY lead" % role

    r = client.post("/api/leads/%d/signal" % leads["sales"], headers=hdr("sales"),
                    json={"type": "wants_meeting", "note": "asked to meet"})
    assert r.status_code == 200, "row 'grant manual +30': sales must grant it on their OWN lead"

    r = client.post("/api/leads/%d/signal" % leads["sales2"], headers=hdr("sales"),
                    json={"type": "wants_meeting", "note": "nope"})
    assert r.status_code == 403, "row 'grant manual +30': sales denied on a lead they don't own"

    r = client.post("/api/leads/%d/signal" % leads["marketing"], headers=hdr("marketing"),
                    json={"type": "wants_meeting", "note": "nope"})
    assert r.status_code == 403, "row 'grant manual +30': marketing is denied even on their own lead"


def test_d4_signal_records_actor_via_audit_log():
    """D4: a human-granted +30 must be attributable."""
    ids, leads = fresh()
    client.post("/api/leads/%d/signal" % leads["sales"], headers=hdr("sales"),
               json={"type": "wants_meeting", "note": "in person"})
    con = db.connect()
    row = con.execute("""SELECT actor_user_id, action, entity FROM audit_log
                         WHERE entity='lead' AND entity_id=? AND action='signal'""",
                      (str(leads["sales"]),)).fetchone()
    assert row is not None and row["actor_user_id"] == ids["sales"]


# ── AUDIT.md critical leaks — reproduced here, then closed ──────────────────
#
# Both were found by an adversarial audit against a temporary DB, no
# production/local data. app/api.py:269 returned every task on a lead
# regardless of owner; app/api.py:589 let a caller create a task on a lead
# they could not even view. Priority-1 fixes for both; these are the tests
# that prove they stay closed.

def test_leak_task_create_requires_lead_access():
    """api.py:589 (pre-fix): task creation checked the lead EXISTED but not
    that the caller may VIEW it. Reproduced by the audit: sales created a
    task on sales2's lead (200) and then received that foreign lead through
    /api/today. Closed: creating a task now requires the same lead access
    the read path enforces (SPEC.md rule 1)."""
    ids, leads = fresh()
    r = client.post("/api/tasks", headers=hdr("sales"),
                    json={"lead_id": leads["sales2"], "type": "call",
                          "reason": "should be denied — sales2's lead", "due_at": "2026-08-20 10:00:00"})
    assert r.status_code == 403, \
        "sales must be denied creating a task on a lead they cannot view (sales2's)"
    assert err_code(r) == "forbidden"

    con = db.connect()
    n = db.scalar(con, "SELECT count(*) FROM tasks WHERE lead_id=?", (leads["sales2"],))
    assert n == 0, "no task must have been created against the foreign lead"

    # the original reproduction: the leak surfaced the foreign lead's name
    # through /api/today once the task existed. With creation denied, it
    # can never get that far.
    r2 = client.get("/api/today", headers=hdr("sales"))
    assert r2.status_code == 200
    lead_ids_seen = {t["lead_id"] for t in r2.json()["data"]}
    assert leads["sales2"] not in lead_ids_seen, \
        "sales must never receive sales2's lead through /api/today, even indirectly via a task"

    # sanity check the positive path still works: sales CAN create a task on
    # their own lead.
    r3 = client.post("/api/tasks", headers=hdr("sales"),
                     json={"lead_id": leads["sales"], "type": "call",
                           "reason": "allowed — sales's own lead", "due_at": "2026-08-20 10:00:00"})
    assert r3.status_code == 200
    assert r3.json()["lead_id"] == leads["sales"]


def test_leak_lead_detail_tasks_filtered_by_caller_ownership():
    """api.py:269 (pre-fix): lead detail returned EVERY task tied to the
    lead_id, regardless of who owned each task. Reproduced by the audit: a
    sales owner received another rep's task. Closed: filtered by caller
    ownership in SQL unless the caller has tasks.all — proven here with two
    tasks on the SAME lead, owned by two different reps."""
    ids, leads = fresh()
    con = db.connect()
    own_task = _task_for(con, ids, "sales", leads["sales"], reason="sales's own task")
    foreign_task = _task_for(con, ids, "sales2", leads["sales"], reason="sales2's task, same lead")

    r = client.get("/api/leads/%d" % leads["sales"], headers=hdr("sales"))
    assert r.status_code == 200
    seen_ids = {t["id"] for t in r.json()["tasks"]}
    assert own_task in seen_ids, "sales must see their own task on their own lead"
    assert foreign_task not in seen_ids, \
        "sales must NOT see sales2's task, even though it is embedded in a lead sales owns"

    # office_manager has tasks.all — must see both, proving the filter is
    # capability-gated, not just "only ever the lead owner's tasks".
    r2 = client.get("/api/leads/%d" % leads["sales"], headers=hdr("office_manager"))
    assert r2.status_code == 200
    seen_ids2 = {t["id"] for t in r2.json()["tasks"]}
    assert own_task in seen_ids2 and foreign_task in seen_ids2, \
        "office_manager (tasks.all) must see every task on the lead"


def test_leak_marketing_cannot_read_tasks_via_lead_detail():
    """AUDIT.md: 'marketing can likewise bypass the /api/team 403 by reading
    tasks embedded in any lead.' marketing has leads.all (can open any lead)
    but not tasks.all — so the tasks array on someone else's lead must be
    empty, proving /api/team's 403 cannot be routed around this way."""
    ids, leads = fresh()
    con = db.connect()
    _task_for(con, ids, "sales", leads["sales"], reason="sales's task")

    r = client.get("/api/team", headers=hdr("marketing"))
    assert r.status_code == 403, "sanity check: marketing really is denied /api/team directly"

    r2 = client.get("/api/leads/%d" % leads["sales"], headers=hdr("marketing"))
    assert r2.status_code == 200, "marketing has leads.all — can open the lead itself"
    assert r2.json()["tasks"] == [], \
        "marketing must not read another rep's task through lead detail — that is the /api/team bypass"


# ── matrix row: dashboard company-wide KPIs (admin/marketing/office_manager
#    ✅, sales ❌) ─────────────────────────────────────────────────────────────

def test_matrix_dashboard_company_kpis():
    fresh()
    for role in ("admin", "marketing", "office_manager"):
        r = client.get("/api/dashboard", headers=hdr(role))
        assert r.status_code == 200, "row 'dashboard company-wide KPIs': %s must be allowed" % role
    r = client.get("/api/dashboard", headers=hdr("sales"))
    assert r.status_code == 403, "row 'dashboard company-wide KPIs': sales must be denied"
    assert err_code(r) == "forbidden"


# ── matrix row: dashboard per-rep performance (admin/office_manager ✅) ─────

def test_matrix_dashboard_per_rep():
    fresh()
    for role in ("admin", "office_manager"):
        r = client.get("/api/team", headers=hdr(role))
        assert r.status_code == 200, "row 'dashboard per-rep performance': %s must be allowed" % role
    for role in ("marketing", "sales"):
        r = client.get("/api/team", headers=hdr(role))
        assert r.status_code == 403, "row 'dashboard per-rep performance': %s must be denied" % role


# ── matrix row: import CSV / create campaigns (admin/marketing ✅) ──────────

def test_matrix_import_csv_create_campaigns():
    """AUDIT.md tests/test_permissions.py:314 finding: this test was named
    'import CSV/create campaigns' but only ever posted one lead — it never
    exercised the CSV wizard, and would pass even if that door had no
    permission check at all. Now exercises BOTH real doors gated by the
    import.csv capability: the CSV wizard's question endpoint
    (POST /api/import/questions), and lead creation — 'the same door a CSV
    importer or LP form integration walks through row by row' per
    app/api.py's own comment on that route."""
    fresh()
    for role in ("admin", "marketing"):
        r = client.post("/api/import/questions", headers=hdr(role),
                        json={"headers": ["name", "email", "不明な列"]})
        assert r.status_code == 200, \
            "row 'import CSV/create campaigns': %s must reach the CSV wizard" % role
        assert "questions" in r.json() and "guessed_mapping" in r.json()

        r = client.post("/api/leads", headers=hdr(role),
                        json={"name": "New Lead %s" % role, "source": "csv",
                              "email": "new-%s@test.example" % role})
        assert r.status_code == 200, "row 'import CSV/create campaigns': %s must be allowed" % role

    for role in ("office_manager", "sales"):
        r = client.post("/api/import/questions", headers=hdr(role), json={"headers": ["name"]})
        assert r.status_code == 403, \
            "row 'import CSV/create campaigns': %s must be denied the CSV wizard" % role
        assert err_code(r) == "forbidden"

        r = client.post("/api/leads", headers=hdr(role),
                        json={"name": "Nope", "source": "csv", "email": "nope2-%s@test.example" % role})
        assert r.status_code == 403, "row 'import CSV/create campaigns': %s must be denied" % role
        assert err_code(r) == "forbidden"


# ── matrix row: see consent + contact details ────────────────────────────────
# admin ✅ · marketing ❌ (aggregate only, unless they own it) · office_manager
# ✅ · sales ✅ (own leads only)

def test_matrix_contact_details_redaction():
    _, leads = fresh()
    # admin and office_manager: never redacted, on any lead
    for role in ("admin", "office_manager"):
        r = client.get("/api/leads/%d" % leads["sales"], headers=hdr(role))
        assert r.json()["contact_redacted"] is False, \
            "row 'consent+contact': %s must see contact on any lead" % role

    # sales: own lead → visible; nothing else is even reachable (403, tested above)
    r = client.get("/api/leads/%d" % leads["sales"], headers=hdr("sales"))
    assert r.json()["contact_redacted"] is False
    assert r.json()["identities"], "sales must see phone/email on their own lead"

    # marketing: sees ANY lead (leads.all) but contact is redacted unless owned
    r = client.get("/api/leads/%d" % leads["sales"], headers=hdr("marketing"))
    assert r.status_code == 200
    assert r.json()["contact_redacted"] is True, \
        "row 'consent+contact': marketing must NOT see phone/email on a lead they don't own"
    assert r.json()["identities"] == []

    r = client.get("/api/leads/%d" % leads["marketing"], headers=hdr("marketing"))
    assert r.json()["contact_redacted"] is False, \
        "marketing sees contact on a lead they DO own — redaction is per-lead, not per-role"


def test_redaction_also_applies_to_the_list_endpoint():
    _, leads = fresh()
    r = client.get("/api/leads?page_size=200", headers=hdr("marketing"))
    by_id = {row["id"]: row for row in r.json()["data"]}
    assert by_id[leads["sales"]]["contact_redacted"] is True
    assert "identities" not in by_id[leads["sales"]]
    assert by_id[leads["marketing"]]["contact_redacted"] is False
    assert "identities" in by_id[leads["marketing"]]


# ── matrix row: manage users + roles (admin only) ───────────────────────────

def test_matrix_manage_users():
    fresh()
    r = client.get("/api/users", headers=hdr("admin"))
    assert r.status_code == 200, "row 'manage users + roles': admin must be allowed (GET)"
    r = client.post("/api/users", headers=hdr("admin"),
                    json={"email": "newhire@test.example", "role": "sales"})
    assert r.status_code == 200, "row 'manage users + roles': admin must be allowed (POST)"
    new_id = r.json()["id"]
    r = client.patch("/api/users/%s" % new_id, headers=hdr("admin"), json={"is_active": True})
    assert r.status_code == 200, "row 'manage users + roles': admin must be allowed (PATCH)"

    for role in ("marketing", "office_manager", "sales"):
        for method, path, body in (
                ("get", "/api/users", None),
                ("post", "/api/users", {"email": "x@test.example", "role": "sales"}),
                ("patch", "/api/users/u-sales", {"is_active": False})):
            r = getattr(client, method)(path, headers=hdr(role), json=body) if body is not None \
                else getattr(client, method)(path, headers=hdr(role))
            assert r.status_code == 403, \
                "row 'manage users + roles': %s must be denied %s %s" % (role, method, path)
            assert err_code(r) == "forbidden"


def test_activating_a_pending_user_via_patch_users():
    """The fail-closed loop actually closes: an admin can flip is_active."""
    fresh()
    con = db.connect()
    con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active)
                   VALUES ('u-pending','pending@test.example','Pending','sales',0)""")
    con.commit()
    token = devauth.mint(email="pending@test.example")
    r = client.get("/api/me", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 403

    client.patch("/api/users/u-pending", headers=hdr("admin"), json={"is_active": True})
    r = client.get("/api/me", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200


# ── pagination shape ─────────────────────────────────────────────────────────

def test_pagination_shape_and_slicing():
    fresh()
    con = db.connect()
    for i in range(25):
        lid = ingest.upsert_lead(con, name="Bulk %d" % i, channel="csv",
                                 email="bulk%d@test.example" % i)["lead_id"]
        con.execute("UPDATE leads SET owner_user_id='u-admin' WHERE id=?", (lid,))
    con.commit()

    r = client.get("/api/leads?page=1&page_size=10", headers=hdr("admin"))
    body = r.json()
    assert set(body) == {"data", "page", "page_size", "total"}
    assert body["page"] == 1 and body["page_size"] == 10
    assert len(body["data"]) == 10
    assert body["total"] >= 25

    r2 = client.get("/api/leads?page=2&page_size=10", headers=hdr("admin"))
    ids_page1 = {x["id"] for x in body["data"]}
    ids_page2 = {x["id"] for x in r2.json()["data"]}
    assert ids_page1.isdisjoint(ids_page2), "page 2 must not repeat page 1's rows"


# ══════════════════════════════════════════════════════════════════════════
# SPEC-V2 — one denial test per role per new capability.
# ══════════════════════════════════════════════════════════════════════════

# ── row 'Tracking (activity feed)': admin/marketing/office_manager see ALL,
# sales sees only its own leads' — SQL role filtering, on this new surface. ──

def test_matrix_activity_feed_role_scope():
    ids, leads = fresh()
    con = db.connect()
    scoring.record(con, leads["sales"], "open", occurred_at=datetime(2026, 8, 10))
    scoring.record(con, leads["sales2"], "click", occurred_at=datetime(2026, 8, 11))
    con.commit()

    for role in ("admin", "marketing", "office_manager"):
        r = client.get("/api/activity?page_size=200", headers=hdr(role))
        assert r.status_code == 200, "row 'Tracking (activity feed)': %s must see all" % role
        lead_ids_seen = {row["lead_id"] for row in r.json()["data"]}
        assert leads["sales"] in lead_ids_seen and leads["sales2"] in lead_ids_seen, \
            "%s must see every lead's activity, not just some" % role

    r2 = client.get("/api/activity?page_size=200", headers=hdr("sales"))
    assert r2.status_code == 200
    lead_ids_seen2 = {row["lead_id"] for row in r2.json()["data"]}
    assert leads["sales"] in lead_ids_seen2
    assert leads["sales2"] not in lead_ids_seen2, \
        "row 'Tracking (activity feed)': sales must never see another rep's activity"


# ── row 'Nurture (sequences)': admin ✅ · marketing ✅ edit · office_manager
# view · sales ❌ no tab. ──────────────────────────────────────────────────

def test_matrix_nurture_view():
    fresh()
    for role in ("admin", "marketing", "office_manager"):
        for path in ("/api/sequences", "/api/sequences/1", "/api/sequences/1/stats"):
            r = client.get(path, headers=hdr(role))
            assert r.status_code == 200, "row 'Nurture': %s must be allowed %s" % (role, path)
    for path in ("/api/sequences", "/api/sequences/1", "/api/sequences/1/stats"):
        r = client.get(path, headers=hdr("sales"))
        assert r.status_code == 403, "row 'Nurture': sales must be denied %s" % path
        assert err_code(r) == "forbidden"


def test_matrix_nurture_edit():
    fresh()
    for role in ("admin", "marketing"):
        r = client.patch("/api/sequences/1/steps/1", headers=hdr(role),
                         json={"subject": "edited by %s" % role})
        assert r.status_code == 200, "row 'Nurture' edit: %s must be allowed" % role
    for role in ("office_manager", "sales"):
        r = client.patch("/api/sequences/1/steps/1", headers=hdr(role),
                         json={"subject": "should be denied"})
        assert r.status_code == 403, "row 'Nurture' edit: %s must be denied (view-only or no tab)" % role
        assert err_code(r) == "forbidden"


# ── row 'SNS': admin ✅ · marketing ✅ edit · office_manager view · sales ❌. ──

def test_matrix_sns_view():
    fresh()
    for role in ("admin", "marketing", "office_manager"):
        r = client.get("/api/sns/patterns", headers=hdr(role))
        assert r.status_code == 200, "row 'SNS': %s must be allowed" % role
        r2 = client.get("/api/sns/funnel", headers=hdr(role))
        assert r2.status_code == 200, "row 'SNS' funnel: %s must be allowed" % role
    r3 = client.get("/api/sns/patterns", headers=hdr("sales"))
    assert r3.status_code == 403, "row 'SNS': sales must be denied"
    assert err_code(r3) == "forbidden"
    r4 = client.get("/api/sns/funnel", headers=hdr("sales"))
    assert r4.status_code == 403, "row 'SNS' funnel: sales must be denied"


# ── row 'Booking settings': admin ✅ · marketing ❌ · office_manager ✅ · sales ❌. ──

def test_matrix_booking_manage():
    fresh()
    for role in ("admin", "office_manager"):
        r = client.get("/api/booking/settings", headers=hdr(role))
        assert r.status_code == 200, "row 'Booking settings': %s must be allowed" % role
        r2 = client.get("/api/booking/slots?meeting_type=consult_30", headers=hdr(role))
        assert r2.status_code == 200, "row 'Booking settings' (slots): %s must be allowed" % role
    for role in ("marketing", "sales"):
        r3 = client.get("/api/booking/settings", headers=hdr(role))
        assert r3.status_code == 403, "row 'Booking settings': %s must be denied" % role
        assert err_code(r3) == "forbidden"
        r4 = client.patch("/api/booking/settings", headers=hdr(role), json={"note": "nope"})
        assert r4.status_code == 403, "row 'Booking settings' (PATCH): %s must be denied" % role
        r5 = client.get("/api/booking/slots", headers=hdr(role))
        assert r5.status_code == 403, "row 'Booking settings' (slots): %s must be denied" % role


# ── row 'Assignment rules': admin ✅ · marketing ❌ · office_manager ✅ · sales ❌. ──

def test_matrix_assignment_manage():
    fresh()
    for role in ("admin", "office_manager"):
        r = client.get("/api/assignment/rules", headers=hdr(role))
        assert r.status_code == 200, "row 'Assignment rules': %s must be allowed" % role
        r2 = client.get("/api/calendar", headers=hdr(role))
        assert r2.status_code == 200, "row 'Assign' (calendar): %s must be allowed" % role
    for role in ("marketing", "sales"):
        r3 = client.get("/api/assignment/rules", headers=hdr(role))
        assert r3.status_code == 403, "row 'Assignment rules': %s must be denied" % role
        assert err_code(r3) == "forbidden"
        r4 = client.put("/api/assignment/rules", headers=hdr(role), json={"rules": []})
        assert r4.status_code == 403, "row 'Assignment rules' (PUT): %s must be denied" % role
        r5 = client.get("/api/calendar", headers=hdr(role))
        assert r5.status_code == 403, "row 'Assign' (calendar): %s must be denied" % role


# ── row 'Integrations (arch)': admin ✅ · marketing view · office_manager
# view · sales ❌. Sync (a write) is admin-only, stricter than the view row. ──

def test_matrix_integrations_view():
    fresh()
    for role in ("admin", "marketing", "office_manager"):
        r = client.get("/api/integrations", headers=hdr(role))
        assert r.status_code == 200, "row 'Integrations': %s must be allowed" % role
    r2 = client.get("/api/integrations", headers=hdr("sales"))
    assert r2.status_code == 403, "row 'Integrations': sales has no tab at all"
    assert err_code(r2) == "forbidden"


def test_matrix_integrations_sync():
    fresh()
    r = client.post("/api/integrations/gohighlevel/sync", headers=hdr("admin"))
    assert r.status_code == 200, "row 'Integrations' sync: admin must be allowed"
    for role in ("marketing", "office_manager", "sales"):
        r2 = client.post("/api/integrations/gohighlevel/sync", headers=hdr(role))
        assert r2.status_code == 403, "row 'Integrations' sync: %s must be denied (view != sync)" % role
        assert err_code(r2) == "forbidden"


# ── row 'CSV import': admin ✅ · marketing ✅ · office_manager ❌ · sales ❌ —
# exercised on the real v2 analyze/commit endpoints, not just the old wizard
# question endpoint. ──────────────────────────────────────────────────────

def test_matrix_import_v2_endpoints():
    fresh()
    csv_bytes = b"name,email\nX Y,xy@test.example\n"
    for role in ("admin", "marketing"):
        r = client.post("/api/import/analyze", headers=hdr(role),
                        files={"file": ("t.csv", csv_bytes, "text/csv")})
        assert r.status_code == 200, "row 'CSV import': %s must reach analyze" % role
        token = r.json()["token"]
        r2 = client.post("/api/import/commit", headers=hdr(role),
                         json={"token": token, "mapping": r.json()["guessed_mapping"], "answers": {}})
        assert r2.status_code == 200, "row 'CSV import': %s must reach commit" % role

    for role in ("office_manager", "sales"):
        r3 = client.post("/api/import/analyze", headers=hdr(role),
                         files={"file": ("t.csv", csv_bytes, "text/csv")})
        assert r3.status_code == 403, "row 'CSV import': %s must be denied analyze" % role
        assert err_code(r3) == "forbidden"
        r4 = client.post("/api/import/commit", headers=hdr(role),
                         json={"token": "does-not-matter", "mapping": {}, "answers": {}})
        assert r4.status_code == 403, "row 'CSV import': %s must be denied commit" % role


# ── AI reply approve-and-send can award the manual +30 — gated with the
# SAME capability as SPEC.md's 'grant the manual +30' row: admin/
# office_manager any lead, sales own only, marketing NEVER (even their own). ──

def test_matrix_reply_send_manual_plus30():
    ids, leads = fresh()
    con = db.connect()
    reply_own = con.execute(
        "INSERT INTO replies (lead_id, channel, subject, body, status) VALUES (?,'email','s','b','received')",
        (leads["sales"],)).lastrowid
    reply_foreign = con.execute(
        "INSERT INTO replies (lead_id, channel, subject, body, status) VALUES (?,'email','s','b','received')",
        (leads["sales2"],)).lastrowid
    reply_marketing_own = con.execute(
        "INSERT INTO replies (lead_id, channel, subject, body, status) VALUES (?,'email','s','b','received')",
        (leads["marketing"],)).lastrowid
    con.commit()

    r = client.post("/api/replies/%d/send" % reply_own, headers=hdr("sales"),
                    json={"text": "ok", "verdict": {"human": True, "wants_meeting": True}})
    assert r.status_code == 200, "row 'grant manual +30': sales must be allowed on their OWN lead"

    r2 = client.post("/api/replies/%d/send" % reply_foreign, headers=hdr("sales"),
                     json={"text": "nope", "verdict": {"human": True, "wants_meeting": True}})
    assert r2.status_code == 403, "row 'grant manual +30': sales must be denied on sales2's lead"
    assert err_code(r2) == "forbidden"

    r3 = client.post("/api/replies/%d/send" % reply_marketing_own, headers=hdr("marketing"),
                     json={"text": "nope", "verdict": {"human": True, "wants_meeting": True}})
    assert r3.status_code == 403, "row 'grant manual +30': marketing is denied even on their own lead"
    assert err_code(r3) == "forbidden"


# ── voice notes reuse 'see own leads' / 'edit a lead they own' — proven
# denied cross-rep, the same class of leak AUDIT.md found on tasks. ──────────

def test_leak_voice_notes_require_lead_access():
    ids, leads = fresh()
    r = client.post("/api/leads/%d/notes" % leads["sales2"], headers=hdr("sales"),
                    files={"audio": ("n.m4a", b"fake-audio", "audio/m4a")})
    assert r.status_code == 403, "sales must be denied adding a note to sales2's lead"
    assert err_code(r) == "forbidden"

    r2 = client.get("/api/leads/%d/notes" % leads["sales2"], headers=hdr("sales"))
    assert r2.status_code == 403, "sales must be denied reading notes on sales2's lead"
    assert err_code(r2) == "forbidden"


# ── lead-drawer actions reuse 'edit a lead they own' — proven denied across
# all five on a lead the caller does not own. ────────────────────────────────

def test_leak_lead_drawer_actions_require_lead_access():
    ids, leads = fresh()
    foreign = leads["sales2"]
    for path, body in (
            ("/api/leads/%d/notify-rep" % foreign, {}),
            ("/api/leads/%d/showroom-visit" % foreign, {}),
            ("/api/leads/%d/site-inspection" % foreign, {}),
            ("/api/leads/%d/set-contact-date" % foreign, {"date": "2026-09-01"}),
            ("/api/leads/%d/follow" % foreign, {}),
    ):
        r = client.post(path, headers=hdr("sales"), json=body)
        assert r.status_code == 403, "sales must be denied %s on a lead they don't own" % path
        assert err_code(r) == "forbidden"


# ── row 'Card scan': ALL FOUR roles ✅ — a genuinely different door than
# 'CSV import' (admin/marketing only). Confirms nobody is accidentally
# denied. ─────────────────────────────────────────────────────────────────

def test_matrix_card_scan_all_roles_allowed():
    fresh()
    for role in ROLES:
        r = client.post("/api/leads/scan", headers=hdr(role),
                        data={"name": "Scan by %s" % role},
                        files={"image": ("c.jpg", b"\xff\xd8\xff\xe0-x", "image/jpeg")})
        assert r.status_code == 200, "row 'Card scan': %s must be allowed" % role


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
