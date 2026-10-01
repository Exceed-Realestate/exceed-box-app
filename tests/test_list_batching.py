"""The batched list helpers must return exactly what the per-row helpers did.

/api/leads, /api/pipeline and /api/team were rewritten from one query per row
to a handful of grouped queries, purely for speed on a high-latency database
link. Nothing a screen shows may change: this compares the batched output with
the original single-lead helpers over every lead in the demo seed, and the
team rows with a straightforward per-staffer recount.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import api, db, ingest, tasks  # noqa: E402


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    # Its own database file, named explicitly for both the seed subprocess
    # and this process: other tests repoint db.DB_PATH, so relying on the
    # inherited EXCEEDBOX_DB seeded one file and read another.
    path = tmp_path_factory.mktemp("batching") / "seeded.db"
    saved = db.DB_PATH
    db.DB_PATH = path
    subprocess.run([sys.executable, os.path.join(ROOT, "seed.py")], check=True,
                   env={**os.environ, "EXCEEDBOX_DB": str(path)}, cwd=ROOT,
                   stdout=subprocess.DEVNULL)
    c = db.connect()
    try:
        yield c
    finally:
        c.close()
        db.DB_PATH = saved


def test_lead_extras_match_the_single_lead_helpers(con):
    ids = [r["id"] for r in con.execute("SELECT id FROM leads ORDER BY id")]
    assert ids, "seed produced no leads"
    batch = api._lead_extras(con, ids, identities_for=ids)
    for i in ids:
        x = batch[i]
        assert x["channels"] == ingest.channels(con, i), i
        assert x["region"] == api._region_list(con, i), i
        assert x["activity_note"] == api.activity_note(con, i), i
        single = [dict(r) for r in con.execute(
            "SELECT kind,value,status FROM lead_identities WHERE lead_id=?", (i,))]
        key = lambda d: (d["kind"], d["value"])  # noqa: E731
        assert sorted(x["identities"], key=key) == sorted(single, key=key), i


def test_identities_only_for_the_leads_asked(con):
    ids = [r["id"] for r in con.execute("SELECT id FROM leads ORDER BY id LIMIT 4")]
    batch = api._lead_extras(con, ids, identities_for=ids[:1])
    assert all(batch[i]["identities"] == [] for i in ids[1:])


def test_owner_names_match(con):
    owners = [r["owner_user_id"] for r in con.execute("SELECT owner_user_id FROM leads")]
    names = api._owner_names(con, owners + [None, "no-such-user"])
    for o in set(owners):
        assert names.get(o) == api._owner_name(con, o)


def test_empty_inputs_run_no_query(con):
    assert api._lead_extras(con, []) == {}
    assert api._owner_names(con, [None]) == {}


def test_team_status_numbers_match_a_per_staffer_recount(con):
    rows = tasks.team_status(con)
    assert rows
    for r in rows:
        sid = r["staff_id"]
        q = lambda sql: con.execute(sql, (sid,)).fetchone()[0]  # noqa: E731
        assert r["leads_owned"] == q(
            "SELECT count(*) FROM leads WHERE owner_id=? AND merged_into IS NULL")
        assert r["meetings_booked"] == q(
            "SELECT count(*) FROM leads WHERE owner_id=? AND merged_into IS NULL "
            "AND stage IN ('meeting_booked','in_negotiation','won')")
        assert r["won"] == q(
            "SELECT count(*) FROM leads WHERE owner_id=? AND merged_into IS NULL AND stage='won'")
        assert r["tasks_open"] == q(
            "SELECT count(*) FROM tasks t JOIN leads l ON l.id=t.lead_id "
            "WHERE t.owner_id=? AND t.state='open'")
        assert isinstance(r["leads_owned"], int) and isinstance(r["won"], int)


def test_digest_reuses_a_passed_team(con):
    team = tasks.team_status(con)
    assert tasks.escalations_digest(con, team=team)["needs_attention"] == \
        tasks.escalations_digest(con)["needs_attention"]
