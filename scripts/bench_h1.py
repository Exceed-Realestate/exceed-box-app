#!/usr/bin/env python3
"""H1 (FABLE-AUDIT.md) proof — seed 30,000 leads, time the three named
endpoints. Run once against the pre-fix code and once against the fix (see
the session report for how each run was produced — this script itself is
the SAME script both times; only app/api.py and app/tasks.py were swapped
between runs).

Usage:
    python3 scripts/bench_h1.py
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCEEDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "bench_h1.db")
os.environ["EXCEEDBOX_UPLOAD_ROOT"] = tempfile.mkdtemp()
os.environ["EXCEEDBOX_DEV_AUTH"] = "1"
os.environ.pop("SUPABASE_JWT_SECRET", None)
# N1 (FABLE-AUDIT-R2.md) — app/auth.py now enforces the @exceed-re.ae
# domain lock server-side; this bench's *@bench.example logins need an
# explicit allow-list entry, read once at app.auth import time below.
os.environ["EXCEEDBOX_ALLOWED_EMAIL_DOMAINS"] = "bench.example,exceed-re.ae"

from fastapi.testclient import TestClient   # noqa: E402

from app import db, devauth, scoring   # noqa: E402
from app.api import app                # noqa: E402

N_LEADS = 30000
N_STAFF = 12
EVENT_KINDS = ["open", "click", "page_view", "booking_page_view", "reply",
              "booking_completed", "wants_meeting", "corporate_deal",
              "partnership", "high_budget"]
STAGES = ["new", "nurturing", "engaged", "meeting_booked", "in_negotiation", "won"]


def seed(con) -> None:
    random.seed(42)
    for kind, pts, decays in [
            ("open", 1, 1), ("click", 3, 1), ("page_view", 3, 1),
            ("booking_page_view", 5, 1), ("reply", 10, 1),
            ("booking_completed", 20, 0), ("wants_meeting", 30, 0),
            ("corporate_deal", 30, 0), ("partnership", 30, 0), ("high_budget", 30, 0)]:
        # decays is a real boolean on Postgres — bind a bool, not 1/0.
        con.execute("INSERT INTO scoring_rules (event_kind,points,decays,label_en) VALUES (?,?,?,?)",
                   (kind, pts, bool(decays), kind))
    con.execute("INSERT INTO settings (key,value) VALUES ('notify_threshold','40')")

    staff_ids = []
    app_user_ids = []
    for i in range(N_STAFF):
        cur = con.execute("INSERT INTO staff (name, name_en, access) VALUES (?,?,?)",
                          ("Staff %d" % i, "Staff %d" % i, "agent"))
        staff_id = cur.lastrowid
        staff_ids.append(staff_id)
        uid = "bench-staff-%d" % i
        con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active,staff_id)
                       VALUES (?,?,?,?,TRUE,?)""",
                   (uid, "staff%d@bench.example" % i, "Staff %d" % i, "sales", staff_id))
        app_user_ids.append(uid)
    admin_id = "bench-admin"
    con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active)
                   VALUES (?,?,?,?,TRUE)""", (admin_id, "admin@bench.example", "Admin", "admin"))
    con.commit()

    print("seeding %d leads..." % N_LEADS)
    t0 = time.perf_counter()
    lead_rows = []
    for i in range(N_LEADS):
        staff_idx = i % N_STAFF
        lead_rows.append((
            "Bench Lead %d" % i, "Company %d" % i, "csv",
            "2026-01-01 00:00:00", STAGES[i % len(STAGES)],
            staff_ids[staff_idx], app_user_ids[staff_idx],
        ))
    con.executemany(
        """INSERT INTO leads (name, company, first_touch, first_touch_at, stage,
                              owner_id, owner_user_id)
           VALUES (?,?,?,?,?,?,?)""", lead_rows)
    con.commit()

    lead_ids = [r[0] for r in con.execute("SELECT id FROM leads ORDER BY id")]

    print("seeding events (~3/lead)...")
    event_rows = []
    for lead_id in lead_ids:
        for _ in range(random.randint(1, 5)):
            kind = random.choice(EVENT_KINDS)
            days_ago = random.randint(0, 150)
            event_rows.append((lead_id, kind, "system",
                              "2026-08-17 00:00:00", days_ago))
    # occurred_at as a real varying timestamp so decay math has something to do
    import datetime as dt
    now = dt.datetime(2026, 8, 17)
    event_rows = [(lead_id, kind, source,
                  (now - dt.timedelta(days=days_ago)).isoformat(sep=" ", timespec="seconds"))
                 for (lead_id, kind, source, _ts, days_ago) in event_rows]
    con.executemany(
        "INSERT INTO events (lead_id, kind, source, occurred_at) VALUES (?,?,?,?)", event_rows)
    con.commit()
    print("  %d events" % len(event_rows))

    print("seeding tasks (one open task per lead, spread across staff)...")
    task_rows = []
    for i, lead_id in enumerate(lead_ids):
        staff_idx = i % N_STAFF
        task_rows.append((lead_id, "call", staff_ids[staff_idx],
                         "2026-08-20 10:00:00", "open", "human", "bench task"))
    con.executemany(
        """INSERT INTO tasks (lead_id, type, owner_id, due_at, state, created_by, reason)
           VALUES (?,?,?,?,?,?,?)""", task_rows)
    con.commit()
    print("seed done in %.1fs" % (time.perf_counter() - t0))


def main() -> None:
    db.reset()
    con = db.connect()
    seed(con)
    # Events were bulk-inserted directly (not through scoring.record(), which
    # would have paid the H1 fix's own recompute-on-write cost 90k times just
    # to seed). Materialize once here — exactly the "nightly sweep" a real
    # deployment runs (scripts/sweep_decay.py) — so leads.score reflects real
    # values in BOTH the before and after runs, not "always 0 because nobody
    # ever wrote it". Timed and reported separately: it is background-job
    # cost, never live-request cost, which is the whole point of H1.
    if "score" in [r[1] for r in con.execute("PRAGMA table_info(leads)")]:
        t0 = time.perf_counter()
        n = scoring.sweep_decay(con)
        print("sweep_decay: %d leads in %.3fs (background job, not a live request)" %
             (n, time.perf_counter() - t0))
    else:
        print("leads.score column not present — running PRE-FIX code path, no sweep to run")
    con.close()

    client = TestClient(app)
    headers = {"Authorization": "Bearer " + devauth.mint(email="admin@bench.example")}

    timings = {}
    for name, url in (("GET /api/dashboard", "/api/dashboard"),
                      ("GET /api/team", "/api/team?page_size=50"),
                      ("GET /api/leads", "/api/leads?page_size=20&sort=score")):
        t0 = time.perf_counter()
        r = client.get(url, headers=headers)
        elapsed = time.perf_counter() - t0
        timings[name] = elapsed
        print("%-22s -> %s in %.3fs" % (name, r.status_code, elapsed))
        assert r.status_code == 200, r.text

    print()
    print("N_LEADS=%d N_STAFF=%d" % (N_LEADS, N_STAFF))
    for name, elapsed in timings.items():
        print("%-22s %.3fs" % (name, elapsed))


if __name__ == "__main__":
    main()
