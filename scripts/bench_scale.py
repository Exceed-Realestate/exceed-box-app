#!/usr/bin/env python3
"""Scale check at 30,000 leads, against real Postgres, in a throwaway schema.

Why a schema and not the real tables: 30k synthetic leads in `public` would sit
next to real business records, and cleaning them up afterwards means 30k deletes
over a trans-continental link. A dedicated `bench` schema is created, migrated,
loaded, measured, and then removed with a single DROP SCHEMA ... CASCADE. It
cannot leave a stray row behind that someone later mistakes for a customer.

Why timings are reported two ways:

  * **server ms** comes from EXPLAIN ANALYZE, so it is the database's own
    execution time. This is the number that predicts production, where the API
    will sit next to the database.
  * **wall ms** is what this laptop sees from Dubai to a Tokyo database, so it
    carries ~150-250 ms of round trip per query. It is reported for honesty, not
    as a performance verdict — do not read it as "the dashboard takes a second".

    python scripts/bench_scale.py --leads 30000
    python scripts/bench_scale.py --drop-only      # clean up after a crash
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
import uuid as _uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCHEMA = os.environ.get("EXCEEDBOX_BENCH_SCHEMA", "bench")

_url = os.environ.get("DATABASE_URL") or os.environ.get("EXCEEDBOX_TEST_DATABASE_URL")
if not _url:
    raise SystemExit("DATABASE_URL is required")
os.environ["DATABASE_URL"] = _url

from app import db  # noqa: E402


def _bench_connection():
    """A connection pinned to the bench schema.

    `?options=-csearch_path%3Dbench` in the URL does NOT work through Supabase's
    pooler — it silently reports the default search_path, and every statement
    then runs against `public`. Verified on 2026-09-12. The only reliable way is
    an explicit SET on the session, followed by a check: without the check, a
    failed SET means 30,000 synthetic leads land in the real tables.
    """
    con = db.connect()
    # `public` must stay on the path: the citext EXTENSION is installed there
    # (checked — not in `extensions`, despite that schema existing), and a
    # CREATE TABLE with a citext column fails with "type does not exist"
    # without it. Tables are still created in the FIRST entry on the path, and
    # the current_schema() guard below is what proves that is the bench schema
    # and not public.
    con.execute("SET search_path TO %s, public, extensions" % SCHEMA)
    con.commit()
    got = con.execute("SHOW search_path").fetchone()[0]
    if SCHEMA not in str(got):
        con.close()
        raise SystemExit("refusing to run: search_path is %r, not %r — this would "
                         "write synthetic data into the live tables" % (got, SCHEMA))
    where = con.execute("SELECT current_schema()").fetchone()[0]
    if str(where) != SCHEMA:
        con.close()
        raise SystemExit("refusing to run: current_schema() is %r" % where)
    return con

FIRST = ["Kenji", "Yuki", "Haruto", "Aoi", "Sota", "Rin", "Hina", "Ren", "Mei", "Kaito"]
LAST = ["Sato", "Suzuki", "Takahashi", "Tanaka", "Ito", "Watanabe", "Yamamoto",
        "Nakamura", "Kobayashi", "Kato"]
STAGES = ["new", "nurturing", "engaged", "meeting_booked", "in_negotiation", "won"]
SOURCES = ["csv", "business_card", "sns", "landing_page", "referral", "gohighlevel"]


def _admin_connection():
    """A connection on the default search_path, for CREATE/DROP SCHEMA."""
    import psycopg
    return psycopg.connect(_url, autocommit=True)


def drop_schema():
    with _admin_connection() as c:
        c.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
    print("dropped schema %s" % SCHEMA)


def create_schema():
    with _admin_connection() as c:
        c.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        c.execute("CREATE SCHEMA %s" % SCHEMA)
    print("created schema %s" % SCHEMA)


def seed(con, n_leads: int, n_staff: int = 8):
    """Bulk load. Multi-row inserts, not one statement per row — at Tokyo
    latency a per-row loop would take hours and measure the network, not the
    database."""
    t0 = time.time()
    con.execute("INSERT INTO sequences (id, name, active)"
                " OVERRIDING SYSTEM VALUE VALUES (1, 'bench', TRUE)")
    for i in range(1, 8):
        con.execute("INSERT INTO sequence_steps (sequence_id, step_no, offset_days,"
                    " subject, active) VALUES (1,?,?,?,TRUE)",
                    (i, (i - 1) * 7, "bench step %d" % i))
    for kind, pts, decays in [("open", 1, True), ("click", 3, True),
                              ("page_view", 3, True), ("booking_page_view", 5, True),
                              ("reply", 10, True), ("booking_completed", 20, False),
                              ("wants_meeting", 30, False), ("corporate_deal", 30, False),
                              ("partnership", 30, False), ("high_budget", 30, False)]:
        con.execute("INSERT INTO scoring_rules (event_kind, points, decays, label_en,"
                    " active) VALUES (?,?,?,?,TRUE)", (kind, pts, decays, kind))

    staff_ids = []
    for i in range(n_staff):
        cur = con.execute("INSERT INTO staff (name, name_en, access) VALUES (?,?,?)",
                          ("Bench %d" % i, "Bench %d" % i, "agent"))
        staff_ids.append(cur.lastrowid)
    users = []
    for i, sid in enumerate(staff_ids):
        # app_user.id is a real uuid column — a readable string like
        # "bench-user-0" is rejected outright.
        uid = str(_uuid.uuid5(_uuid.NAMESPACE_DNS, "exceedbox-bench-user-%d" % i))
        con.execute("INSERT INTO app_user (id, email, display_name, role, is_active,"
                    " staff_id) VALUES (?,?,?,?,TRUE,?)",
                    (uid, "bench%d@bench.invalid" % i, "Bench %d" % i, "sales", sid))
        users.append(uid)
    con.commit()

    rnd = random.Random(20260912)
    BATCH = 1000
    for start in range(0, n_leads, BATCH):
        rows, args = [], []
        for i in range(start, min(start + BATCH, n_leads)):
            rows.append("(?,?,?,?,?,?,?)")
            args += ["%s %s" % (rnd.choice(FIRST), rnd.choice(LAST)),
                     "Bench Co %d" % (i % 900),
                     rnd.choice(SOURCES),
                     rnd.choice(STAGES),
                     rnd.choice(users),
                     rnd.randint(0, 100),
                     rnd.choice(["investment", "relocation", "second_home",
                                 "business_base", "unknown"])]
        con.execute(
            "INSERT INTO leads (name, company, first_touch, stage, owner_user_id,"
            " score, purpose) VALUES " + ",".join(rows), tuple(args))
        con.commit()
        print("  leads %d/%d" % (min(start + BATCH, n_leads), n_leads), flush=True)

    ids = [r["id"] for r in db.all_(con, "SELECT id FROM leads")]
    for start in range(0, len(ids), BATCH):
        chunk = ids[start:start + BATCH]
        rows, args = [], []
        for lid in chunk:
            rows.append("(?,'email',?,TRUE,'ok')")
            args += [lid, "bench-%d@bench.invalid" % lid]
        con.execute("INSERT INTO lead_identities (lead_id, kind, value, is_primary,"
                    " status) VALUES " + ",".join(rows), tuple(args))
        # ~3 behavioural events each, which is what the score and the dashboard read
        rows, args = [], []
        for lid in chunk:
            for _ in range(rnd.randint(1, 4)):
                rows.append("(?,?,now() - (? || ' days')::interval)")
                args += [lid, rnd.choice(["open", "click", "page_view", "reply"]),
                         str(rnd.randint(0, 180))]
        con.execute("INSERT INTO events (lead_id, kind, occurred_at) VALUES "
                    + ",".join(rows), tuple(args))
        con.commit()
        print("  identities+events %d/%d" % (min(start + BATCH, len(ids)), len(ids)),
              flush=True)

    print("seeded in %.1fs" % (time.time() - t0))
    return users


def _explain_ms(con, sql, args=()):
    """Server-side execution time, excluding the trans-continental round trip."""
    rows = con.execute("EXPLAIN (ANALYZE, TIMING ON, FORMAT JSON) " + sql,
                       args).fetchall()
    plan = rows[0][0]
    if isinstance(plan, str):
        import json
        plan = json.loads(plan)
    return float(plan[0]["Execution Time"])


def measure(con, users):
    owner = users[0]
    queries = [
        ("dashboard: total leads", "SELECT count(*) FROM leads", ()),
        ("dashboard: by stage",
         "SELECT stage, count(*) FROM leads GROUP BY stage", ()),
        ("dashboard: by source",
         "SELECT first_touch, count(*) FROM leads GROUP BY first_touch", ()),
        ("dashboard: per rep",
         "SELECT owner_user_id, count(*), avg(score) FROM leads"
         " GROUP BY owner_user_id", ()),
        ("leads list, page 1 (hot first)",
         "SELECT * FROM leads ORDER BY score DESC, id DESC LIMIT 50", ()),
        ("leads list, page 200",
         "SELECT * FROM leads ORDER BY score DESC, id DESC LIMIT 50 OFFSET 10000", ()),
        ("leads list, one rep only",
         "SELECT * FROM leads WHERE owner_user_id=? ORDER BY score DESC LIMIT 50",
         (owner,)),
        ("search by name",
         "SELECT * FROM leads WHERE name ILIKE ? LIMIT 50", ("%Tanaka%",)),
        ("search by company",
         "SELECT * FROM leads WHERE company ILIKE ? LIMIT 50", ("%Bench Co 42%",)),
        ("hot leads (score >= 40)",
         "SELECT * FROM leads WHERE score >= 40 ORDER BY score DESC LIMIT 50", ()),
        ("events for one lead",
         "SELECT * FROM events WHERE lead_id=(SELECT min(id) FROM leads)", ()),
        ("pipeline column, capped",
         "SELECT * FROM leads WHERE stage='nurturing' ORDER BY score DESC LIMIT 200", ()),
    ]
    print("\n%-34s %10s %10s" % ("query", "server ms", "wall ms"))
    print("-" * 58)
    worst = []
    for label, sql, args in queries:
        t = time.time()
        con.execute(sql, args).fetchall()
        wall = (time.time() - t) * 1000
        try:
            server = _explain_ms(con, sql, args)
        except Exception as e:
            print("%-34s  EXPLAIN failed: %s" % (label, str(e)[:40]))
            continue
        print("%-34s %10.1f %10.0f" % (label, server, wall))
        worst.append((server, label))
    worst.sort(reverse=True)
    print("\nslowest server-side: %s" % ", ".join("%s (%.0f ms)" % (l, s)
                                                  for s, l in worst[:3]))
    return worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leads", type=int, default=30000)
    ap.add_argument("--keep", action="store_true", help="do not drop the schema")
    ap.add_argument("--drop-only", action="store_true")
    a = ap.parse_args()

    if a.drop_only:
        drop_schema()
        return 0

    create_schema()
    con = _bench_connection()
    print("pinned to schema: %s" % SCHEMA)
    try:
        applied = db.migrate(con)
        print("migrations applied into %s: %d" % (SCHEMA, len(applied)))
        users = seed(con, a.leads)
        print("\nrow counts: leads=%s identities=%s events=%s" % (
            db.scalar(con, "SELECT count(*) FROM leads"),
            db.scalar(con, "SELECT count(*) FROM lead_identities"),
            db.scalar(con, "SELECT count(*) FROM events")))
        con.execute("ANALYZE")
        con.commit()
        measure(con, users)
    finally:
        con.close()
        if not a.keep:
            drop_schema()
    return 0


if __name__ == "__main__":
    sys.exit(main())
