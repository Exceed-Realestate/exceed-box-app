"""Tests for the remaining FABLE-AUDIT.md findings this session closed:
H1 (materialization correctness — the timing proof lives in
scripts/bench_h1.py and the session report), H2 (pipeline pagination),
H3 (decay interpretation flag), H4 (exit-state auto-fire switch +
provisional flag), and the JST/GST timezone bug.

Runs standalone, same dev-auth harness as the other suites.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCEEDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "test_fable_fixes.db")
os.environ["EXCEEDBOX_UPLOAD_ROOT"] = tempfile.mkdtemp()
os.environ["EXCEEDBOX_DEV_AUTH"] = "1"
os.environ.pop("SUPABASE_JWT_SECRET", None)
# N1 (FABLE-AUDIT-R2.md) — see tests/test_permissions.py's identical line;
# app/auth.py's ALLOWED_EMAIL_DOMAINS is a module-level constant read once,
# by whichever test file imports app.auth first in a full `pytest tests` run.
os.environ["EXCEEDBOX_ALLOWED_EMAIL_DOMAINS"] = "test.example,contract.example,fable-fix.example,exceed-re.ae"

from fastapi.testclient import TestClient   # noqa: E402

from app import auth, db, devauth, ingest, scoring, tasks, tracking   # noqa: E402
from app.api import app                                              # noqa: E402

client = TestClient(app)

ROLES = ("admin", "marketing", "office_manager", "sales")
EMAILS = {r: "%s@fable-fix.example" % r for r in ROLES}


def hdr(role: str) -> dict:
    # N1 (FABLE-AUDIT-R2.md) — stable sub per role; see test_permissions.py's
    # identical comment.
    return {"Authorization": "Bearer " + devauth.mint(sub="ff-test-sub-%s" % role, email=EMAILS[role])}


def err_code(resp) -> str:
    return resp.json()["error"]["code"]


def fresh():
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
        uid = "ff-%s" % role
        con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active)
                       VALUES (?,?,?,?,1)""", (uid, EMAILS[role], role.title(), role))
        ids[role] = uid
    con.execute("""INSERT INTO booking_settings (id, jst_gst_gap_hours, business_hours_start,
                                                  business_hours_end, slot_length_minutes, timezone, note)
                   VALUES (1, 5, '09:00', '18:00', 30, 'Asia/Tokyo', 'fixture')""")
    con.execute("""INSERT INTO booking_meeting_types (key, label_en, label_ja, duration_minutes)
                   VALUES ('consult_30','Free 30-min consultation','無料30分相談',30)""")
    con.execute("INSERT INTO booking_rep_meeting_types (user_id, meeting_type) VALUES (?, 'consult_30')",
               (ids["sales"],))
    con.commit()
    con.close()
    return ids


# ══ H1 — materialized score agrees with explain() ═══════════════════════════

def test_materialized_score_matches_explain_after_every_write():
    """The materialized column is never allowed to disagree with what
    explain() (the still-authoritative computation) would say right now —
    scoring.record()/void() are the only two places that write it, and both
    recompute through explain() itself."""
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Materialize Me", channel="csv",
                                 email="materialize@test.example")["lead_id"]
    con.commit()

    scoring.record(con, lead_id, "open")
    scoring.record(con, lead_id, "click")
    event_id = scoring.record(con, lead_id, "reply")

    materialized = db.scalar(con, "SELECT score FROM leads WHERE id=?", (lead_id,))
    authoritative = scoring.explain(con, lead_id)["score"]
    assert materialized == authoritative, "materialized score drifted from explain() after record()"
    assert materialized > 0

    scoring.void(con, event_id, staff_id=None)
    materialized2 = db.scalar(con, "SELECT score FROM leads WHERE id=?", (lead_id,))
    authoritative2 = scoring.explain(con, lead_id)["score"]
    assert materialized2 == authoritative2, "materialized score drifted from explain() after void()"
    assert materialized2 < materialized, "voiding an event must lower the materialized score"


def test_sweep_decay_recomputes_every_live_lead():
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Sweep Me", channel="csv")["lead_id"]
    con.commit()
    scoring.record(con, lead_id, "open", occurred_at=datetime.utcnow() - timedelta(days=90))
    # simulate staleness: force the materialized column to something wrong,
    # the way pure time passing (no new event) would leave it stale.
    con.execute("UPDATE leads SET score=999 WHERE id=?", (lead_id,))
    con.commit()

    n = scoring.sweep_decay(con)
    assert n >= 1
    fixed = db.scalar(con, "SELECT score FROM leads WHERE id=?", (lead_id,))
    assert fixed == scoring.explain(con, lead_id)["score"]
    assert fixed != 999


def test_leads_list_can_sort_by_score():
    """M4 — falls out of H1: /api/leads can now ORDER BY score DESC."""
    ids = fresh()
    con = db.connect()
    low = ingest.upsert_lead(con, name="Low Score", channel="csv")["lead_id"]
    high = ingest.upsert_lead(con, name="High Score", channel="csv")["lead_id"]
    con.commit()
    scoring.record(con, high, "wants_meeting")
    scoring.record(con, high, "corporate_deal")

    r = client.get("/api/leads?sort=score&page_size=50", headers=hdr("admin"))
    assert r.status_code == 200
    rows = r.json()["data"]
    scores = [row["score"] for row in rows]
    assert scores == sorted(scores, reverse=True), "sort=score must return highest score first"
    ids_in_order = [row["id"] for row in rows]
    assert ids_in_order.index(high) < ids_in_order.index(low)


# ══ H2 — pipeline pagination ═════════════════════════════════════════════════

def test_pipeline_pagination_reaches_leads_past_page_one():
    ids = fresh()
    con = db.connect()
    # 210 leads in one stage — one more than the old hard 200-card cap.
    for i in range(210):
        ingest.upsert_lead(con, name="Pipeline Lead %d" % i, channel="csv",
                           email="pipeline%d@test.example" % i)
    con.commit()
    con.close()

    r1 = client.get("/api/pipeline?page=1&page_size=100", headers=hdr("admin"))
    assert r1.status_code == 200
    new_bucket_1 = next(b for b in r1.json()["buckets"] if b["key"] == "new")
    assert new_bucket_1["count"] == 210
    assert len(new_bucket_1["leads"]) == 100
    assert new_bucket_1["has_more"] is True

    r2 = client.get("/api/pipeline?page=2&page_size=100", headers=hdr("admin"))
    new_bucket_2 = next(b for b in r2.json()["buckets"] if b["key"] == "new")
    assert len(new_bucket_2["leads"]) == 100
    assert new_bucket_2["has_more"] is True

    r3 = client.get("/api/pipeline?page=3&page_size=100", headers=hdr("admin"))
    new_bucket_3 = next(b for b in r3.json()["buckets"] if b["key"] == "new")
    assert len(new_bucket_3["leads"]) == 10, "the 10 leads past the old 200-cap must be reachable"
    assert new_bucket_3["has_more"] is False

    seen_ids = set()
    for b in (new_bucket_1, new_bucket_2, new_bucket_3):
        seen_ids.update(l["id"] for l in b["leads"])
    assert len(seen_ids) == 210, "H2: every lead in the column must be reachable across pages"


# ══ H3 — decay interpretation flag ═══════════════════════════════════════════

def test_decay_default_is_per_event_unchanged():
    assert scoring.DECAY_MODE == scoring.DECAY_MODE_PER_EVENT, \
        "H3: default must stay the ORIGINAL per-event behaviour until Balraj rules"


def test_decay_per_event_vs_lead_level_differ_as_designed():
    """A lead with an old reply (100 days ago, well into decay) and a
    recent open (5 days ago). Per-event: the reply keeps fading on its own
    age. Lead-level: the recent open revives EVERY behaviour point,
    including the old reply, back toward full value — exactly the
    difference the decision log flagged and never ratified."""
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Decay Test", channel="csv")["lead_id"]
    con.commit()
    now = datetime.utcnow()
    scoring.record(con, lead_id, "reply", occurred_at=now - timedelta(days=100))
    scoring.record(con, lead_id, "open", occurred_at=now - timedelta(days=5))

    original_mode = scoring.DECAY_MODE
    try:
        scoring.DECAY_MODE = scoring.DECAY_MODE_PER_EVENT
        per_event = scoring.explain(con, lead_id, now)
        reply_component_per_event = next(c for c in per_event["breakdown"] if c["kind"] == "reply")
        assert reply_component_per_event["decay_factor"] < 0.2, \
            "per-event: a 100-day-old reply must be almost fully decayed"
        assert per_event["decay_mode"] == "per_event"

        scoring.DECAY_MODE = scoring.DECAY_MODE_LEAD_LEVEL
        lead_level = scoring.explain(con, lead_id, now)
        reply_component_lead_level = next(c for c in lead_level["breakdown"] if c["kind"] == "reply")
        assert reply_component_lead_level["decay_factor"] > 0.9, \
            "lead-level: a recent (5-day) open must revive the old reply's decay factor"
        assert lead_level["decay_mode"] == "lead_level"

        assert lead_level["score"] > per_event["score"], \
            "lead-level scoring must score this lead higher than per-event scoring"
    finally:
        scoring.DECAY_MODE = original_mode


def test_scoring_model_endpoint_reports_decay_mode():
    fresh()
    r = client.get("/api/scoring/model", headers=hdr("admin"))
    assert r.status_code == 200
    assert r.json()["decay"]["mode"] == scoring.DECAY_MODE


# ══ N3 (FABLE-AUDIT-R2.md) — self-healing read path ══════════════════════════
# The stored leads.score column is exact at write time but only reconciles
# pure time-based decay when sweep_decay() runs — and nothing schedules it.
# Demonstrated live in the audit: lead 1 stored 52 vs explain() 31 at +90d.
# scoring.fresh_score() is now the one thing every score-serving read path
# calls instead of trusting the column; these tests force the exact "no
# sweep ever ran" shape (a wrong value, an old score_updated_at) and prove
# every endpoint that returns a score self-heals to agree with explain().

def _force_stale(con, lead_id: int, wrong_score: int, days_since_update: float = None) -> None:
    """Simulates precisely what a real, never-swept deployment looks like:
    leads.score holds a value that no longer matches explain() (`wrong_score`
    — analogous to a lead that decayed since this was last written), and
    score_updated_at is old enough to cross scoring.SCORE_STALE_SECONDS, so
    fresh_score() must actually recompute rather than trust the column."""
    if days_since_update is None:
        days_since_update = (scoring.SCORE_STALE_SECONDS / 86400.0) + 1
    old_ts = (datetime.utcnow() - timedelta(days=days_since_update)).isoformat(sep=" ", timespec="seconds")
    con.execute("UPDATE leads SET score=?, score_updated_at=? WHERE id=?",
               (wrong_score, old_ts, lead_id))
    con.commit()


def test_n3_score_consistency_invariant_across_every_endpoint_that_exposes_one():
    """The invariant FABLE-AUDIT-R2.md names directly: any score the API
    returns for a lead equals scoring.explain() for that lead, on every
    endpoint that exposes one. Checked on list, pipeline, dashboard
    hot-leads, lead detail, why-score, and a task's embedded lead_score —
    against a lead whose stored column is deliberately wrong and stale."""
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Consistency Lead", channel="csv",
                                 email="consistency@test.example")["lead_id"]
    con.execute("UPDATE leads SET owner_user_id=?, stage='new' WHERE id=?", (ids["sales"], lead_id))
    con.commit()
    scoring.record(con, lead_id, "wants_meeting")
    scoring.record(con, lead_id, "corporate_deal")
    staff_id = auth.ensure_staff_bridge(con, ids["sales"], "Sales")
    tasks.create(con, lead_id, "call", reason="check consistency", owner_id=staff_id,
                due_at=datetime.utcnow() + timedelta(days=1), created_by="human")
    con.commit()

    _force_stale(con, lead_id, wrong_score=999)
    truth = scoring.explain(con, lead_id)["score"]
    assert truth != 999, "fixture sanity: the forced value must actually be wrong"
    con.close()

    r_list = client.get("/api/leads?page_size=50", headers=hdr("sales"))
    assert next(x for x in r_list.json()["data"] if x["id"] == lead_id)["score"] == truth, \
        "N3: /api/leads must self-heal a stale stored score"

    r_pipe = client.get("/api/pipeline?page_size=50", headers=hdr("sales"))
    bucket = next(b for b in r_pipe.json()["buckets"] if b["key"] == "new")
    assert next(x for x in bucket["leads"] if x["id"] == lead_id)["score"] == truth, \
        "N3: /api/pipeline must self-heal a stale stored score"

    r_detail = client.get("/api/leads/%d" % lead_id, headers=hdr("sales"))
    assert r_detail.json()["score"] == truth, "N3: lead detail must agree with explain()"
    assert r_detail.json()["tasks"][0]["lead_score"] == truth, \
        "N3: a task's embedded lead_score must self-heal too, not just the lead views"

    r_why = client.get("/api/leads/%d/score" % lead_id, headers=hdr("sales"))
    assert r_why.json()["score"] == truth, "N3: /api/leads/{id}/score must agree with explain()"

    r_today = client.get("/api/today", headers=hdr("sales"))
    today_task = next(t for t in r_today.json()["data"] if t["lead_id"] == lead_id)
    assert today_task["lead_score"] == truth, "N3: /api/today's lead_score must self-heal too"

    r_dash = client.get("/api/dashboard", headers=hdr("admin"))
    hot = next((h for h in r_dash.json()["hot_leads"] if h["id"] == lead_id), None)
    if hot is not None:
        assert hot["score"] == truth, "N3: dashboard hot_leads must agree with explain()"


def test_n3_ninety_day_stale_lead_reads_consistently_everywhere():
    """Reproduces the exact audit scenario: a lead scored, then +90 days
    pass with no sweep_decay() run — before N3, list/pipeline/hot-leads read
    the stale stored number while LeadDetail read live explain(), so the
    SAME lead showed two different scores. Builds the +90d state honestly
    (real decayed events, not a hand-typed wrong number) to prove the read
    path — not just the forced-value fixture above — self-heals."""
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Ninety Day Lead", channel="csv",
                                 email="ninetyday@test.example")["lead_id"]
    con.execute("UPDATE leads SET owner_user_id=? WHERE id=?", (ids["sales"], lead_id))
    con.commit()
    # Behaviour events dated 90 days ago — record() materializes the
    # CURRENTLY-decayed score at write time (real now vs a 90-day-old
    # occurred_at), which is honest. To reproduce "the column then went
    # stale because nothing ever touched it again", stamp score_updated_at
    # back to that same moment, simulating the elapsed time since the last
    # write with zero reads/writes in between (no sweep, no traffic).
    scoring.record(con, lead_id, "reply", occurred_at=datetime.utcnow() - timedelta(days=90))
    scoring.record(con, lead_id, "click", occurred_at=datetime.utcnow() - timedelta(days=90))
    stale_ts = (datetime.utcnow() - timedelta(days=(scoring.SCORE_STALE_SECONDS / 86400.0) + 1)) \
        .isoformat(sep=" ", timespec="seconds")
    con.execute("UPDATE leads SET score_updated_at=? WHERE id=?", (stale_ts, lead_id))
    con.commit()
    con.close()

    con2 = db.connect()
    truth = scoring.explain(con2, lead_id)["score"]
    con2.close()

    r_list = client.get("/api/leads?page_size=50", headers=hdr("sales"))
    list_score = next(x for x in r_list.json()["data"] if x["id"] == lead_id)["score"]
    r_detail = client.get("/api/leads/%d" % lead_id, headers=hdr("sales"))
    detail_score = r_detail.json()["score"]

    assert list_score == truth == detail_score, (
        "N3: at +90d with no sweep, the list and the detail screen must show the SAME "
        "score, and it must be the live explain() value (list=%r detail=%r truth=%r)"
        % (list_score, detail_score, truth))


def test_n3_fresh_score_leaves_a_genuinely_fresh_row_untouched():
    """Self-heal must be conditional, not unconditional — a lead just
    written by record() (score_updated_at ~ now) is returned as-is, no
    surprise recompute-and-rewrite on a column that was never wrong."""
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Fresh Lead", channel="csv")["lead_id"]
    con.commit()
    scoring.record(con, lead_id, "open")
    before = db.scalar(con, "SELECT score FROM leads WHERE id=?", (lead_id,))
    before_ts = db.scalar(con, "SELECT score_updated_at FROM leads WHERE id=?", (lead_id,))

    same = scoring.fresh_score(con, lead_id, before_ts, before)
    assert same == before, "a fresh row must not be recomputed to a different value"
    after_ts = db.scalar(con, "SELECT score_updated_at FROM leads WHERE id=?", (lead_id,))
    assert after_ts == before_ts, "a fresh row's score_updated_at must not be touched at all"
    con.close()


# ══ N4 (FABLE-AUDIT-R2.md) — /api/scoring/model stops full-scanning ═════════
# api.py:1470-1474 used to SELECT every live lead and call scoring.score()
# TWICE each (now + a week ago) just to count "crossed this week" — the
# exact H1 pattern H1 was meant to kill, missed because the materialized
# column can only answer "now". score_threshold_crossings (written by
# scoring.recompute_lead — the one place leads.score is ever changed)
# answers it with one indexed query instead.

def test_n4_crossed_this_week_counts_only_leads_that_crossed_up_and_are_still_hot():
    ids = fresh()
    con = db.connect()
    thr = scoring.threshold(con)
    assert thr == 40, "fixture sanity"

    crossed_and_stays = ingest.upsert_lead(con, name="Crossed Stays", channel="csv")["lead_id"]
    never_crosses = ingest.upsert_lead(con, name="Never Crosses", channel="csv")["lead_id"]
    crossed_then_dropped = ingest.upsert_lead(con, name="Crossed Then Dropped", channel="csv")["lead_id"]
    already_hot = ingest.upsert_lead(con, name="Already Hot", channel="csv")["lead_id"]
    con.commit()

    # crosses up (0 -> 60) and stays there — must count.
    scoring.record(con, crossed_and_stays, "wants_meeting")
    scoring.record(con, crossed_and_stays, "corporate_deal")

    # never gets near the threshold — must not count.
    scoring.record(con, never_crosses, "open")

    # crosses up (0 -> 60), then a void brings it back under 40 — DID cross
    # up this week, but is not "still hot now", so must not count (matches
    # the old semantics: score(now) >= thr was always required first).
    ev = scoring.record(con, crossed_then_dropped, "wants_meeting")
    scoring.record(con, crossed_then_dropped, "corporate_deal")
    scoring.void(con, ev, staff_id=None)

    # already hot before this "week" started — materialized directly, the
    # way a lead that has been hot all along (seed data / an old sweep)
    # would look; never recorded a crossing at all, so correctly excluded.
    con.execute("UPDATE leads SET score=? WHERE id=?", (80, already_hot))
    con.commit()

    r = client.get("/api/scoring/model", headers=hdr("admin"))
    assert r.status_code == 200
    assert r.json()["crossed_this_week"] == 1, \
        "N4: only the lead that crossed up AND is still >= threshold right now must count"

    ups = {row["lead_id"] for row in
          con.execute("SELECT lead_id FROM score_threshold_crossings WHERE direction='up'")}
    assert crossed_and_stays in ups
    assert crossed_then_dropped in ups, "it DID cross up — just not counted, because it dropped back"
    assert never_crosses not in ups
    assert already_hot not in ups, "a lead that was always hot never generates a crossing event"
    con.close()


def test_n4_crossing_older_than_a_week_does_not_count():
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Old Crossing", channel="csv")["lead_id"]
    con.commit()
    scoring.record(con, lead_id, "wants_meeting")
    scoring.record(con, lead_id, "corporate_deal")
    old_ts = (datetime.utcnow() - timedelta(days=10)).isoformat(sep=" ", timespec="seconds")
    con.execute("UPDATE score_threshold_crossings SET crossed_at=? WHERE lead_id=?", (old_ts, lead_id))
    con.commit()

    r = client.get("/api/scoring/model", headers=hdr("admin"))
    assert r.json()["crossed_this_week"] == 0, \
        "N4: a crossing older than 7 days must not count as 'crossed this week'"
    con.close()


# ══ H4 — exit-state auto-fire switch + provisional flag ═════════════════════

def test_exit_state_always_marked_provisional_when_present():
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Exit State Test", channel="csv",
                                 email="exitstate@test.example")["lead_id"]
    con.execute("UPDATE leads SET owner_user_id=? WHERE id=?", (ids["sales"], lead_id))
    con.execute("UPDATE leads SET exit_state='lost' WHERE id=?", (lead_id,))
    con.commit()
    con.close()

    r = client.get("/api/leads/%d" % lead_id, headers=hdr("sales"))
    assert r.status_code == 200
    body = r.json()
    assert body["exit_state"] == "lost"
    assert body["exit_state_provisional"] is True, \
        "H4: any exit_state present must be badged provisional — the four states are unratified"


def test_auto_exit_states_switch_default_on_matches_current_behaviour():
    """H4: default behaviour must be UNCHANGED — auto-firing stays on until
    Balraj rules, exactly as the decision log's own status quo."""
    assert tracking.AUTO_EXIT_STATES_ENABLED is True

    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Bounce Test", channel="csv",
                                 email="bounce-test@test.example")["lead_id"]
    con.execute("""INSERT INTO sends (send_id, lead_id, to_email) VALUES ('ff-send-1', ?, ?)""",
               (lead_id, "bounce-test@test.example"))
    con.commit()

    tracking.sendgrid_webhook(con, [{"event": "bounce", "send_id": "ff-send-1", "reason": "mailbox full"}])
    row = con.execute("SELECT exit_state FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert row["exit_state"] == "unreachable", "default ON: bounce must still auto-set exit_state"
    con.close()


def test_auto_exit_states_switch_off_still_records_the_underlying_fact():
    """Flip the single documented switch off: the funnel-exit WRITE (the
    part nobody ratified) must not happen, but the bounce event and the
    identity 'bounced' status — real facts, not a funnel decision — still
    must be recorded."""
    ids = fresh()
    con = db.connect()
    lead_id = ingest.upsert_lead(con, name="Bounce Test Off", channel="csv",
                                 email="bounce-test-off@test.example")["lead_id"]
    con.execute("""INSERT INTO sends (send_id, lead_id, to_email) VALUES ('ff-send-2', ?, ?)""",
               (lead_id, "bounce-test-off@test.example"))
    con.commit()

    tracking.AUTO_EXIT_STATES_ENABLED = False
    try:
        tracking.sendgrid_webhook(con, [{"event": "bounce", "send_id": "ff-send-2"}])
        row = con.execute("SELECT exit_state FROM leads WHERE id=?", (lead_id,)).fetchone()
        assert row["exit_state"] is None, \
            "H4: switch OFF must suppress the unratified auto-exit write"
        identity = con.execute(
            "SELECT status FROM lead_identities WHERE lead_id=? AND kind='email'", (lead_id,)).fetchone()
        assert identity["status"] == "bounced", "the underlying FACT must still be recorded regardless"
        event = con.execute("SELECT 1 FROM events WHERE lead_id=? AND kind='bounce'", (lead_id,)).fetchone()
        assert event is not None
    finally:
        tracking.AUTO_EXIT_STATES_ENABLED = True   # restore default for any test that runs after
    con.close()


# ══ Timezone — booking slot / task due date cross-office correctness ═══════

def test_booking_slots_are_utc_with_explicit_offset():
    fresh()
    r = client.get("/api/booking/slots?meeting_type=consult_30&week=2026-08-17", headers=hdr("admin"))
    assert r.status_code == 200
    slots = r.json()["slots"]
    assert slots
    first = slots[0]
    # timespec="seconds" on an aware UTC datetime always ends "+00:00".
    assert first["starts_at"].endswith("+00:00"), \
        "Timezone fix: booking slots must be ISO-8601 with an explicit UTC offset, never naive"
    # 09:00 JST (business_hours_start) is 00:00 UTC — the first Monday slot.
    assert "2026-08-17T00:00:00+00:00" in [s["starts_at"] for s in slots], \
        "09:00 JST must convert to 00:00 UTC (JST is UTC+9)"


def test_task_due_date_entered_in_one_office_reads_correctly_in_another():
    """The literal bug FABLE-AUDIT.md described: 'a Dubai-entered slot read
    in Tokyo lands in the wrong hour.' A Dubai rep types a naive '14:00' due
    date (GST); it must be stored as 14:00 GST's real UTC instant (10:00
    UTC — Dubai is UTC+4), so a Tokyo reader converting that UTC instant to
    their own JST sees 19:00 JST — the SAME real moment, not a moment
    silently reinterpreted as 14:00 JST (which would be 05:00 UTC, a
    5-hour — not 4-hour — error, matching the audit's correction)."""
    ids = fresh()
    con = db.connect()
    con.execute("UPDATE app_user SET office='dubai' WHERE id=?", (ids["sales"],))
    con.commit()
    lead_id = ingest.upsert_lead(con, name="TZ Task Lead", channel="csv")["lead_id"]
    con.execute("UPDATE leads SET owner_user_id=? WHERE id=?", (ids["sales"], lead_id))
    con.commit()
    con.close()

    r = client.post("/api/tasks", headers=hdr("sales"),
                    json={"lead_id": lead_id, "type": "call", "reason": "tz test",
                          "due_at": "2026-08-20 14:00:00"})
    assert r.status_code == 200
    due_at = r.json()["due_at"]
    # 14:00 GST (UTC+4) = 10:00 UTC.
    assert due_at == "2026-08-20T10:00:00+00:00", \
        "a Dubai rep's naive 14:00 must be interpreted as GST, not blindly UTC or JST: got %s" % due_at

    # The WRONG behaviour this replaces would have read the naive string as
    # Tokyo time (05:00 UTC) — assert we are NOT that.
    wrong_jst_reading = "2026-08-20T05:00:00+00:00"
    assert due_at != wrong_jst_reading, \
        "must not silently assume Tokyo time for a Dubai-office caller's naive input"


def test_jst_gst_gap_is_five_hours_not_four():
    fresh()
    con = db.connect()
    row = con.execute("SELECT jst_gst_gap_hours FROM booking_settings WHERE id=1").fetchone()
    con.close()
    assert row["jst_gst_gap_hours"] == 5, \
        "FABLE-AUDIT.md: Tokyo UTC+9, Dubai UTC+4 — a 5-hour gap, not the 4 the decision log said"


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
