#!/usr/bin/env python3
"""N4 (FABLE-AUDIT-R2.md) proof — /api/scoring/model's "crossed this week"
used to SELECT every live lead and call scoring.score() TWICE each (now +
a week ago): the exact H1 pattern H1 was meant to kill, missed because the
materialized leads.score column can only ever answer "now". Fixed by
recording threshold crossings as events (score_threshold_crossings, written
by scoring.recompute_lead — the one place leads.score is ever changed) and
answering the question with one indexed query.

This script seeds the same 30,000-lead/~90k-event shape scripts/bench_h1.py
uses, times the CURRENT `/api/scoring/model` (one indexed query), and also
times the OLD O(n) double-scan directly — inline, since that code path no
longer exists in app/api.py — so both numbers come from the same seed data
in the same run.

Usage:
    python3 scripts/bench_n4.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCEEDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "bench_n4.db")
os.environ["EXCEEDBOX_UPLOAD_ROOT"] = tempfile.mkdtemp()
os.environ["EXCEEDBOX_DEV_AUTH"] = "1"
os.environ.pop("SUPABASE_JWT_SECRET", None)
# N1 (FABLE-AUDIT-R2.md) — app/auth.py now enforces the @exceed-re.ae
# domain lock server-side; this bench's admin@bench.example login needs an
# explicit allow-list entry, read once at app.auth import time below.
os.environ["EXCEEDBOX_ALLOWED_EMAIL_DOMAINS"] = "bench.example,exceed-re.ae"

from fastapi.testclient import TestClient   # noqa: E402

from app import db, devauth, scoring   # noqa: E402
from app.api import app                # noqa: E402

# Reuse bench_h1's seeding exactly (same N_LEADS/N_STAFF/shape) so the two
# benchmarks are directly comparable and this file doesn't re-fork the
# fixture generator.
import bench_h1   # noqa: E402


def old_full_scan_crossed_this_week(con, thr: int) -> int:
    """The exact pre-fix logic (api.py:1470-1474 before N4), reproduced here
    verbatim for timing comparison — every live lead, scoring.score() called
    TWICE (now + a week ago)."""
    import datetime as dt
    now = dt.datetime.utcnow()
    week_ago = now - dt.timedelta(days=7)
    crossed = 0
    for r in con.execute("SELECT id FROM leads WHERE merged_into IS NULL"):
        if scoring.score(con, r["id"], now) < thr:
            continue
        if scoring.score(con, r["id"], week_ago) < thr:
            crossed += 1
    return crossed


def main() -> None:
    db.reset()
    con = db.connect()
    bench_h1.seed(con)

    print("materializing scores via sweep_decay (the deployed nightly job, not a live request)...")
    t0 = time.perf_counter()
    scoring.sweep_decay(con)
    print("  sweep_decay: %.3fs" % (time.perf_counter() - t0))

    # CAVEAT on the NEW number this produces: the 90k events here were bulk
    # INSERTed directly (not through scoring.record(), which would pay the
    # H1 fix's per-write recompute cost 90k times just to seed — see
    # bench_h1.py's own comment on this), so leads.score sits at its schema
    # default (0) for all 30,000 leads until the sweep_decay() call above
    # computes each one for the very first time. That first-ever
    # materialization necessarily reads as "0 -> real score" to
    # recompute_lead()'s crossing detector, timestamped now — indistinguishable
    # from a genuine crossing that just happened, because nothing was ever
    # materialized before it to compare against. A live deployment doesn't
    # have this artifact after its first night (crossings only get logged
    # for TRUE before/after transitions from then on), but a freshly-seeded
    # benchmark does. This inflates crossed_this_week here; it is a property
    # of this seeding shortcut, not of the endpoint — the endpoint's actual
    # counting logic (crossed up, still hot, correctly excludes leads that
    # were always hot or that crossed back down) is proven against real
    # event-by-event traffic by tests/test_fable_fixes.py's test_n4_* tests.
    # What this script actually measures, and the only thing N4 is about, is
    # the QUERY COST at 30k leads: one indexed lookup vs. a 60k-call scan.
    thr = scoring.threshold(con)
    con.close()

    client = TestClient(app)
    headers = {"Authorization": "Bearer " + devauth.mint(email="admin@bench.example")}

    print()
    print("timing NEW /api/scoring/model (one indexed query against score_threshold_crossings)...")
    t0 = time.perf_counter()
    r = client.get("/api/scoring/model", headers=headers)
    new_elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    print("  -> %s in %.4fs, crossed_this_week=%s" %
         (r.status_code, new_elapsed, r.json()["crossed_this_week"]))

    print()
    print("timing OLD approach (full scan, scoring.score() x2 per live lead) on the SAME data...")
    con2 = db.connect()
    t0 = time.perf_counter()
    old_count = old_full_scan_crossed_this_week(con2, thr)
    old_elapsed = time.perf_counter() - t0
    con2.close()
    print("  -> %.4fs, crossed_this_week=%s" % (old_elapsed, old_count))

    print()
    print("N_LEADS=%d" % bench_h1.N_LEADS)
    print("%-45s %.4fs" % ("OLD (full scan, score() x2/lead)", old_elapsed))
    print("%-45s %.4fs" % ("NEW (indexed query on crossings table)", new_elapsed))
    if new_elapsed > 0:
        print("speedup: %.0fx" % (old_elapsed / new_elapsed))


if __name__ == "__main__":
    main()
