"""Each test names the decision it protects.

These are not unit tests for their own sake. Every decision from the design
session that could be silently broken by a later change has one here.

Run:  python3 -m pytest tests -q     (or: python3 tests/test_decisions.py)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCEEDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from app import db, ingest, scoring, tasks, tracking   # noqa: E402

NOW = datetime(2026, 8, 12, 12, 0, 0)


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
    con.execute("INSERT INTO staff (id,name,name_en,access) VALUES (1,'ユパン','Yupan','agent')")
    con.execute("INSERT INTO staff (id,name,name_en,access) VALUES (2,'チアキ','Chiaki','agent')")
    con.commit()
    return con


# ── D13 · duplicates merge into one person ───────────────────────────────────

def test_d13_same_email_merges_not_duplicates():
    con = fresh()
    a = ingest.upsert_lead(con, name="森田 賢二", company="青山商事", channel="business_card",
                           email="K.Morita@Aoyama-S.example")
    b = ingest.upsert_lead(con, name="森田 賢二", company="青山商事", channel="csv",
                           email="k.morita@aoyama-s.example")   # different case
    assert a["created"] is True
    assert b["created"] is False and b["merged"] is True
    assert a["lead_id"] == b["lead_id"], "same email must be the same person"
    assert db.scalar(con, "SELECT count(*) FROM leads WHERE merged_into IS NULL") == 1


def test_d13_channels_accumulate_in_order():
    con = fresh()
    r = ingest.upsert_lead(con, name="森田", channel="business_card", email="m@x.example",
                           seen_at=datetime(2026, 6, 20))
    ingest.upsert_lead(con, name="森田", channel="csv", email="m@x.example",
                       seen_at=datetime(2026, 6, 25))
    ingest.upsert_lead(con, name="森田", channel="gohighlevel", email="m@x.example",
                       seen_at=datetime(2026, 7, 1))
    chans = [c["channel"] for c in ingest.channels(con, r["lead_id"])]
    assert chans == ["business_card", "csv", "gohighlevel"], \
        "D13: three channels, in the order they arrived"


def test_d11_first_touch_never_changes():
    con = fresh()
    r = ingest.upsert_lead(con, name="森田", channel="business_card", email="m@x.example")
    ingest.upsert_lead(con, name="森田", channel="csv", email="m@x.example")
    ft = con.execute("SELECT first_touch FROM leads WHERE id=?", (r["lead_id"],)).fetchone()[0]
    assert ft == "business_card", "the dashboard attributes to first touch — it must not move"


def test_d13_phone_matches_across_formats():
    con = fresh()
    a = ingest.upsert_lead(con, name="小野 博", channel="business_card", phone="03-1234-5678")
    b = ingest.upsert_lead(con, name="小野 博", channel="showroom", phone="+81 3 1234 5678")
    assert a["lead_id"] == b["lead_id"] and b["matched_on"] == "phone"


def test_dedupe_does_not_merge_two_different_people():
    con = fresh()
    a = ingest.upsert_lead(con, name="田中 一郎", company="A社", channel="csv", email="a@x.example")
    b = ingest.upsert_lead(con, name="田中 一郎", company="B社", channel="csv", email="b@x.example")
    assert a["lead_id"] != b["lead_id"], \
        "same name, different company and email — an unwanted merge is worse than a duplicate"


# ── D8 · behaviour decays, facts do not ──────────────────────────────────────

def test_d8_behaviour_halves_at_60_days():
    assert abs(scoring.decay_factor(0) - 1.0) < 1e-9
    assert abs(scoring.decay_factor(60) - 0.5) < 1e-9
    assert scoring.decay_factor(120) == 0.0
    assert scoring.decay_factor(400) == 0.0


def test_d8_facts_never_decay():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x@x.example")["lead_id"]
    scoring.record(con, lid, "corporate_deal", occurred_at=NOW - timedelta(days=365))
    assert scoring.score(con, lid, NOW) == 30, "a company does not stop being a company"


def test_d8_old_reply_fades_to_nothing():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x2@x.example")["lead_id"]
    scoring.record(con, lid, "reply", occurred_at=NOW - timedelta(days=200))
    assert scoring.score(con, lid, NOW) == 0, \
        "a reply from six months ago must not still be worth +10"


def test_d8_quiet_lead_drops_back_under_the_threshold():
    """The behaviour Balraj actually wanted: quiet leads go back into the
    machine instead of rotting on a rep's hot list."""
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x3@x.example")["lead_id"]
    for k in ("reply", "click", "page_view", "booking_page_view", "open"):
        scoring.record(con, lid, k, occurred_at=NOW)
    assert scoring.score(con, lid, NOW) >= 20
    later = NOW + timedelta(days=130)
    assert scoring.score(con, lid, later) == 0
    assert scoring.score(con, lid, later) < scoring.threshold(con)


# ── D7 · reply detection, rules before AI ────────────────────────────────────

def test_d7_rules_catch_machine_replies_for_free():
    for headers, sender, subject in [
            ({"Auto-Submitted": "auto-replied"}, "a@x.jp", "Re: hello"),
            ({}, "MAILER-DAEMON@x.jp", "Undeliverable"),
            ({}, "a@x.jp", "自動応答: 不在にしております"),
            ({}, "a@x.jp", "Out of Office"),
            ({"Precedence": "bulk"}, "a@x.jp", "Re: ドバイ")]:
        machine, why = tracking.looks_automated(headers, sender, subject)
        assert machine, "should have been filtered: %s %s" % (sender, subject)


def test_d7_a_real_reply_survives_the_rules():
    machine, _ = tracking.looks_automated({}, "k.morita@aoyama-s.example",
                                          "Re: ドバイは本当に高いのか？")
    assert not machine


def test_d7_nothing_scores_until_ai_confirms_a_human():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x4@x.example")["lead_id"]
    r = tracking.record_reply(con, lid, subject="Re: hi", sender="k@x.example")
    assert r["scored"] is False and r.get("queued") is True
    assert scoring.score(con, lid) == 0


def test_d7_one_ai_pass_answers_all_four_questions():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x5@x.example")["lead_id"]
    r = tracking.record_reply(
        con, lid, subject="Re: ロンボク", sender="k@x.example",
        ai_verdict={"human": True, "wants_meeting": True,
                    "partnership": False, "high_budget": True})
    assert r["scored"] is True
    assert set(r["events"]) == {"reply", "wants_meeting", "high_budget"}
    assert r["score"] == 70            # 10 + 30 + 30, all fresh
    assert r["crossed"] is True


def test_d5a_partnership_sets_relationship_automatically():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x6@x.example")["lead_id"]
    tracking.record_reply(con, lid, subject="協業のご相談", sender="p@x.example",
                          ai_verdict={"human": True, "partnership": True})
    rel = con.execute("SELECT relationship FROM leads WHERE id=?", (lid,)).fetchone()[0]
    assert rel == "partner", "D9: 関係 is derived from scoring, never hand-picked"


# ── D4 · a human-granted +30 must be attributable ────────────────────────────

def test_d4_manual_thirty_pointer_records_who_set_it():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="whatsapp", email="x7@x.example")["lead_id"]
    scoring.record(con, lid, "wants_meeting", detail="said so on WhatsApp",
                   source="human", set_by=1)
    ex = scoring.explain(con, lid)
    c = [c for c in ex["components"] if c["kind"] == "wants_meeting"][0]
    assert c["source"] == "human" and c["set_by"] == 1, \
        "not blocked — made visible, so the lever is attributable"


def test_d7_void_removes_points_but_keeps_history():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x8@x.example")["lead_id"]
    eid = scoring.record(con, lid, "reply")
    assert scoring.score(con, lid) == 10
    scoring.void(con, eid, staff_id=1)
    assert scoring.score(con, lid) == 0
    assert db.scalar(con, "SELECT count(*) FROM events WHERE id=?", (eid,)) == 1


# ── D8 · notification fires on the crossing, not on every event ──────────────

def test_notify_fires_once_on_crossing():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="x9@x.example")["lead_id"]
    before = scoring.score(con, lid)
    scoring.record(con, lid, "corporate_deal")
    after = scoring.score(con, lid)
    assert scoring.crossed_threshold(con, lid, before, after) is False   # 0 → 30
    before, _ = after, scoring.record(con, lid, "reply")
    after = scoring.score(con, lid)
    assert scoring.crossed_threshold(con, lid, before, after) is True    # 30 → 40
    before, _ = after, scoring.record(con, lid, "open")
    assert scoring.crossed_threshold(con, lid, before, scoring.score(con, lid)) is False


# ── D12b · escalation is score × lateness, not lateness alone ────────────────

def test_d12b_urgency_is_weighted_by_score_not_time_alone():
    # same lateness, different value → different urgency
    assert tasks.urgency(3, 92) > tasks.urgency(3, 20)
    # and it shows up in the level as soon as a band boundary is crossed
    assert tasks.escalation(4, 92) > tasks.escalation(4, 20)
    # a hot lead one day late already registers; a cold one does not yet
    assert tasks.escalation(1, 92) >= 1
    assert tasks.escalation(0.5, 20) == 0


def test_d12b_nothing_escalates_before_it_is_due():
    assert tasks.escalation(days_overdue=0, score=100) == 0
    assert tasks.escalation(days_overdue=-2, score=100) == 0


# ── D12 · a task without a reason is refused ─────────────────────────────────

def test_d12_reason_is_mandatory():
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="xa@x.example")["lead_id"]
    try:
        tasks.create(con, lid, "call", reason="")
        assert False, "should have refused"
    except ValueError:
        pass


def test_d12a_idle_rep_is_visible():
    """The row that matters most is the empty one."""
    con = fresh()
    lid = ingest.upsert_lead(con, name="X", channel="csv", email="xb@x.example")["lead_id"]
    con.execute("UPDATE leads SET owner_id=2 WHERE id=?", (lid,))
    con.commit()
    team = {t["name_en"]: t for t in tasks.team_status(con)}
    assert team["Chiaki"]["idle"] is True, "owns a lead, has no tasks — must be visible"
    assert team["Yupan"]["idle"] is False, "owns nothing, so not idle — just empty"


# ── D11 · consent, and the direction it may move ─────────────────────────────

def test_d11_consent_defaults_per_channel():
    con = fresh()
    a = ingest.upsert_lead(con, name="A", channel="csv", email="a1@x.example")["lead_id"]
    b = ingest.upsert_lead(con, name="B", channel="lp_form", email="b1@x.example")["lead_id"]
    c = ingest.upsert_lead(con, name="C", channel="business_card", email="c1@x.example")["lead_id"]
    g = lambda i: con.execute("SELECT basis FROM lead_consent WHERE lead_id=?", (i,)).fetchone()[0]
    assert g(a) == "unknown"      # 25,000 CSV rows, honestly labelled
    assert g(b) == "explicit"
    assert g(c) == "ambiguous"


def test_d11_consent_can_improve_but_never_silently_downgrade():
    con = fresh()
    lid = ingest.upsert_lead(con, name="A", channel="csv", email="up@x.example")["lead_id"]
    ingest.upsert_lead(con, name="A", channel="lp_form", email="up@x.example")
    g = con.execute("SELECT basis FROM lead_consent WHERE lead_id=?", (lid,)).fetchone()[0]
    assert g == "explicit", "filling in a form is stronger than an unknown CSV row"
    ingest.upsert_lead(con, name="A", channel="csv", email="up@x.example")
    g = con.execute("SELECT basis FROM lead_consent WHERE lead_id=?", (lid,)).fetchone()[0]
    assert g == "explicit", "a later CSV row must not wipe out real consent"


def test_unsubscribe_is_a_hard_stop():
    con = fresh()
    lid = ingest.upsert_lead(con, name="A", channel="lp_form", email="u@x.example")["lead_id"]
    con.execute("""INSERT INTO sends (send_id, lead_id, to_email) VALUES ('s1',?,'u@x.example')""",
                (lid,))
    con.commit()
    tracking.sendgrid_webhook(con, [{"event": "unsubscribe", "send_id": "s1"}])
    row = con.execute("""SELECT l.stage, c.basis FROM leads l
                         JOIN lead_consent c ON c.lead_id=l.id WHERE l.id=?""", (lid,)).fetchone()
    assert row["stage"] == "unsubscribed" and row["basis"] == "withdrawn", \
        "特定電子メール法 — enforced by the system, not by a note someone writes"


def test_bounce_marks_unreachable():
    con = fresh()
    lid = ingest.upsert_lead(con, name="A", channel="csv", email="dead@x.example")["lead_id"]
    con.execute("INSERT INTO sends (send_id, lead_id, to_email) VALUES ('s2',?,'dead@x.example')",
                (lid,))
    con.commit()
    tracking.sendgrid_webhook(con, [{"event": "bounce", "send_id": "s2", "reason": "550"}])
    st = con.execute("SELECT stage FROM leads WHERE id=?", (lid,)).fetchone()[0]
    assert st == "unreachable", "D10: a dead address is an end state, not limbo"


# ── D1 · the four passive signals ────────────────────────────────────────────

def test_d1_pixel_click_and_page_view_all_score():
    con = fresh()
    lid = ingest.upsert_lead(con, name="A", channel="lp_form", email="p@x.example")["lead_id"]
    con.execute("INSERT INTO sends (send_id, lead_id, to_email) VALUES ('e_7Kq2mB',?,'p@x.example')",
                (lid,))
    con.commit()
    tracking.open_pixel(con, "e_7Kq2mB")
    assert scoring.score(con, lid) == 1
    dest = tracking.click(con, "e_7Kq2mB", "/dubai-cost")
    assert "k=e_7Kq2mB" in dest, "the redirect must carry the ID onto the website"
    assert scoring.score(con, lid) == 4
    tracking.page_view(con, "e_7Kq2mB", "/dubai-cost")
    assert scoring.score(con, lid) == 7
    tracking.page_view(con, "e_7Kq2mB", "/book")
    assert scoring.score(con, lid) == 12, "the booking page is worth 5, not 3"


def test_d1_unknown_visitor_scores_nothing():
    con = fresh()
    assert tracking.page_view(con, "not-a-real-id", "/dubai-cost") is None, \
        "somebody arriving from Google is unknown and unscoreable"


# ── D16 · the source chart must total the bookings tile ──────────────────────

def test_d16_source_attribution_counts_people_not_channels():
    con = fresh()
    r = ingest.upsert_lead(con, name="森田", channel="business_card", email="s@x.example")
    ingest.upsert_lead(con, name="森田", channel="csv", email="s@x.example")
    ingest.upsert_lead(con, name="森田", channel="gohighlevel", email="s@x.example")
    con.execute("UPDATE leads SET stage='booked' WHERE id=?", (r["lead_id"],))
    con.commit()
    by_source = con.execute(
        """SELECT first_touch, count(*) n FROM leads
            WHERE merged_into IS NULL AND stage IN ('booked','negotiating','won')
            GROUP BY first_touch""").fetchall()
    booked = db.scalar(con, "SELECT count(*) FROM leads WHERE stage='booked'")
    assert sum(r["n"] for r in by_source) == booked == 1, \
        "one booking, one source — the demo's chart summed 55 against a tile of 30"


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
