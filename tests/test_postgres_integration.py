"""Real-Postgres integration tests. Skipped entirely unless DATABASE_URL is set.

The rest of the suite runs on SQLite and must keep doing so (SPEC.md: "Nothing
about Supabase may be required to run the tests"). These tests are the other
half of that promise: they prove the same code actually works on the deploy
target, which a migration file on its own does not.

    set -a; . ./.env; set +a
    .venv/bin/python -m pytest tests/test_postgres_integration.py -q

Every row written here is tagged with a run-scoped marker and deleted in
teardown, so nothing is left behind that could be mistaken for a business
record. Nothing in this file drops a table, truncates, or calls db.reset().
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# conftest.py moves DATABASE_URL out of the way so the SQLite suite cannot run
# against the real database by accident, and preserves it here. Reading only the
# bare name would make this whole file skip after `source .env`, which reads as
# a pass.
_DB_URL = (os.environ.get("DATABASE_URL")
           or os.environ.get("EXCEEDBOX_TEST_DATABASE_URL"))
if _DB_URL:
    os.environ["DATABASE_URL"] = _DB_URL

from app import db, ingest, scoring, tasks   # noqa: E402

# `app.db` resolves its dialect once, at import. If an earlier test module in
# this process already imported it without DATABASE_URL, it is wired to SQLite
# for the rest of the run and nothing here can change that — so this file must
# be run as its OWN pytest invocation, which is what the docstring above says.
#
# Skipping with that explanation beats 16 confusing errors, and beats a silent
# skip that reads as a pass.
_WHY = None
if not _DB_URL:
    _WHY = "DATABASE_URL not set — Postgres integration tests are opt-in"
elif db.DIALECT != "postgres":
    _WHY = ("app.db is bound to SQLite in this process — run this file on its "
            "own: `set -a; . ./.env; set +a; pytest tests/test_postgres_integration.py`")

pytestmark = pytest.mark.skipif(_WHY is not None, reason=_WHY or "")

# Every lead this file creates carries this in its name, so cleanup is exact
# and a stray row is obviously a test artefact rather than a real person.
PREFIX = "zzz-pgtest-"
MARKER = "%s%s" % (PREFIX, uuid.uuid4().hex[:8])


def _cleanup(con):
    """Sweeps every row this file has ever created, not only the current run's.

    Matching on the run-scoped MARKER alone left orphans behind whenever a run
    died before teardown — which is exactly when a run is most likely to have
    written rows. Matching the shared PREFIX makes each run clean up after its
    predecessors too.
    """
    rows = con.execute(
        "SELECT id FROM leads WHERE name LIKE ?", (PREFIX + "%",)).fetchall()
    for r in rows:
        lid = r["id"]
        for table in ("events", "tasks", "lead_identities", "lead_channels",
                      "lead_regions", "lead_consent", "score_threshold_crossings"):
            con.execute("DELETE FROM %s WHERE lead_id=?" % table, (lid,))
        con.execute("DELETE FROM leads WHERE id=?", (lid,))
    con.execute("DELETE FROM app_user WHERE email LIKE ?", (PREFIX + "%",))
    con.commit()
    return len(rows)


@pytest.fixture(scope="module")
def _shared():
    """One connection for the module. Opening a TLS session to the Tokyo pooler
    costs about a second, so per-test connections made this file take minutes."""
    c = db.connect()
    _cleanup(c)
    yield c
    _cleanup(c)
    c.close()


@pytest.fixture()
def con(_shared):
    yield _shared
    # A failed statement leaves the Postgres transaction aborted. Without this
    # rollback one failing test would cascade into every test after it, which
    # hides the real cause.
    try:
        _shared.rollback()
    except Exception:
        pass
    _cleanup(_shared)


def _lead(con, suffix="1", **kw):
    return ingest.upsert_lead(
        con, name="%s-%s" % (MARKER, suffix), channel="csv", **kw)["lead_id"]


# ── the adapter's own promises ───────────────────────────────────────────────

def test_dialect_is_postgres():
    assert db.DIALECT == "postgres"


def test_migrations_are_recorded_and_rerun_is_a_noop(con):
    versions = [r["version"] for r in db.all_(
        con, "SELECT version FROM schema_migrations ORDER BY version")]
    assert "0001_init" in versions
    assert db.migrate(con) == []


def test_reset_refuses_to_run_against_postgres():
    with pytest.raises(RuntimeError, match="refuses"):
        db.reset()


def test_generated_id_comes_back_as_lastrowid(con):
    lid = _lead(con, "id")
    assert isinstance(lid, int) and lid > 0
    row = db.one(con, "SELECT name FROM leads WHERE id=?", (lid,))
    assert row["name"].endswith("-id")


def test_insert_or_ignore_translates_and_does_not_raise(con):
    lid = _lead(con, "ioi")
    for _ in range(2):
        con.execute(
            "INSERT OR IGNORE INTO lead_regions (lead_id, region, inferred_from)"
            " VALUES (?,?,?)", (lid, "dubai", "test"))
    con.commit()
    n = db.scalar(con, "SELECT count(*) FROM lead_regions WHERE lead_id=?", (lid,))
    assert n == 1


def test_timestamps_read_back_as_naive_iso_text(con):
    """The scoring engine compares these against a naive utcnow(). An aware
    datetime here would raise TypeError on every score() call."""
    lid = _lead(con, "ts")
    scoring.record(con, lid, "open")
    con.commit()
    raw = db.one(con, "SELECT occurred_at FROM events WHERE lead_id=?", (lid,))["occurred_at"]
    assert isinstance(raw, str), "got %r" % type(raw)
    parsed = datetime.fromisoformat(raw.replace("Z", ""))
    assert parsed.tzinfo is None
    assert abs((datetime.utcnow() - parsed).total_seconds()) < 300


def test_datetime_now_translation_populates_defaults(con):
    lid = _lead(con, "now")
    created = db.one(con, "SELECT created_at FROM leads WHERE id=?", (lid,))["created_at"]
    assert isinstance(created, str)
    assert datetime.fromisoformat(created.replace("Z", "")) is not None


def test_booleans_read_back_as_ints(con):
    row = db.one(con, "SELECT decays FROM scoring_rules WHERE event_kind='open'")
    assert row["decays"] in (0, 1)
    assert not isinstance(row["decays"], bool)
    row2 = db.one(con, "SELECT decays FROM scoring_rules WHERE event_kind='high_budget'")
    assert row2["decays"] == 0


def test_scoring_rules_load_with_boolean_where_clause(con):
    """`WHERE active = TRUE` has to work; the engine loads no rules without it."""
    rules = scoring._rules(con)
    assert len(rules) == 10
    assert rules["reply"]["points"] == 10


# ── the business logic, end to end ───────────────────────────────────────────

def test_scoring_end_to_end_with_decay(con):
    lid = _lead(con, "score")
    scoring.record(con, lid, "reply", occurred_at=datetime.utcnow())
    scoring.record(con, lid, "corporate_deal", occurred_at=datetime.utcnow())
    con.commit()
    detail = scoring.explain(con, lid)
    assert detail["score"] == 40
    kinds = {c["kind"] for c in detail["breakdown"]}
    assert {"reply", "corporate_deal"} <= kinds

    # a behaviour point 90 days old must be decayed, a fact must not be
    old = _lead(con, "decay")
    long_ago = datetime.utcnow() - timedelta(days=90)
    scoring.record(con, old, "reply", occurred_at=long_ago)
    scoring.record(con, old, "corporate_deal", occurred_at=long_ago)
    con.commit()
    d = scoring.explain(con, old)
    by_kind = {c["kind"]: c for c in d["breakdown"]}
    assert by_kind["reply"]["points"] < 10
    assert by_kind["corporate_deal"]["points"] == 30


def test_materialized_score_agrees_with_explain(con):
    lid = _lead(con, "mat")
    scoring.record(con, lid, "booking_completed")
    con.commit()
    scoring.recompute_lead(con, lid)
    con.commit()
    stored = db.one(con, "SELECT score FROM leads WHERE id=?", (lid,))["score"]
    assert stored == scoring.explain(con, lid)["score"] == 20


def test_dedupe_merges_and_leaves_the_transaction_usable(con):
    """The savepoint case. A duplicate identity raises IntegrityError, which on
    Postgres aborts the transaction unless it is isolated — and everything after
    it in upsert_lead would then fail."""
    email = "%s@example.invalid" % MARKER
    first = ingest.upsert_lead(con, name="%s-dupe" % MARKER, channel="csv", email=email)
    second = ingest.upsert_lead(con, name="%s-dupe" % MARKER, channel="gohighlevel",
                                email=email, phone="+81-3-1234-5678")
    con.commit()
    assert first["lead_id"] == second["lead_id"]
    chans = {r["channel"] for r in db.all_(
        con, "SELECT channel FROM lead_channels WHERE lead_id=?", (first["lead_id"],))}
    assert chans == {"csv", "gohighlevel"}
    # the connection is still usable, which is the actual regression guard
    assert db.scalar(con, "SELECT count(*) FROM leads WHERE id=?", (first["lead_id"],)) == 1


def test_first_touch_never_changes(con):
    email = "%s-ft@example.invalid" % MARKER
    lid = ingest.upsert_lead(con, name="%s-ft" % MARKER, channel="csv", email=email)["lead_id"]
    before = db.one(con, "SELECT first_touch FROM leads WHERE id=?", (lid,))["first_touch"]
    ingest.upsert_lead(con, name="%s-ft" % MARKER, channel="lp_form", email=email)
    con.commit()
    after = db.one(con, "SELECT first_touch FROM leads WHERE id=?", (lid,))["first_touch"]
    assert before == after == "csv"


def test_uuid_primary_key_round_trips_as_text(con):
    uid = str(uuid.uuid4())
    con.execute(
        "INSERT INTO app_user (id, email, display_name, role, is_active)"
        " VALUES (?,?,?,?,?)",
        (uid, "%s@exceed-re.ae" % MARKER, "PG Test", "sales", False))
    con.commit()
    row = db.one(con, "SELECT id, is_active FROM app_user WHERE id=?", (uid,))
    assert row["id"] == uid, "UUID must come back as the same dashed string"
    assert row["is_active"] == 0


def test_task_due_dates_survive_a_timezone_round_trip(con):
    lid = _lead(con, "task")
    due = datetime.utcnow() + timedelta(days=2)
    tid = tasks.create(con, lid, "call", reason="pg integration",
                       due_at=due.isoformat(sep=" ", timespec="seconds"),
                       created_by="system")
    con.commit()
    assert isinstance(tid, int) and tid > 0
    row = db.one(con, "SELECT due_at FROM tasks WHERE id=?", (tid,))
    parsed = tasks._parse_due(row["due_at"])
    assert abs((parsed - due).total_seconds()) < 2


# ── durability and concurrency ───────────────────────────────────────────────

def test_rollback_discards_uncommitted_work(con):
    lid = _lead(con, "rb")
    con.commit()
    con.execute("UPDATE leads SET company=? WHERE id=?", ("SHOULD-NOT-PERSIST", lid))
    con.rollback()
    row = db.one(con, "SELECT company FROM leads WHERE id=?", (lid,))
    assert row["company"] != "SHOULD-NOT-PERSIST"


def test_data_persists_across_a_new_connection(con):
    lid = _lead(con, "persist")
    con.commit()
    other = db.connect()
    try:
        row = db.one(other, "SELECT name FROM leads WHERE id=?", (lid,))
        assert row is not None and row["name"].endswith("-persist")
    finally:
        other.close()


def test_concurrent_connections_both_commit(con):
    a, b = db.connect(), db.connect()
    try:
        ida = ingest.upsert_lead(a, name="%s-cc-a" % MARKER, channel="csv")["lead_id"]
        idb = ingest.upsert_lead(b, name="%s-cc-b" % MARKER, channel="csv")["lead_id"]
        a.commit()
        b.commit()
        assert ida != idb
        seen = db.scalar(con, "SELECT count(*) FROM leads WHERE name LIKE ?",
                         ("%" + MARKER + "-cc-%",))
        assert seen == 2
    finally:
        a.close()
        b.close()


def test_sweep_decay_runs_and_reports_a_count(con):
    lid = _lead(con, "sweep")
    scoring.record(con, lid, "open", occurred_at=datetime.utcnow() - timedelta(days=100))
    con.commit()
    touched = scoring.sweep_decay(con)
    con.commit()
    assert isinstance(touched, int) and touched >= 0


def test_literal_percent_inside_a_string_survives_translation(con):
    """`LIKE 'x%'` used to raise "only '%s','%b','%t' are allowed as
    placeholders": the translator doubled % outside quotes but not inside, and
    psycopg does not care about SQL quoting when it binds. Found while deleting
    test rows; it would have broken any inline LIKE pattern on Postgres while
    passing on SQLite."""
    lid = _lead(con, "pct")
    rows = con.execute(
        "SELECT id FROM leads WHERE name LIKE 'zzz-pgtest%'").fetchall()
    assert any(r["id"] == lid for r in rows)
    # and the mixed case: a literal % in the SQL *and* a bound parameter
    rows2 = con.execute(
        "SELECT id FROM leads WHERE name LIKE 'zzz-pgtest%' AND id = ?",
        (lid,)).fetchall()
    assert len(rows2) == 1


def test_identity_counters_are_past_every_existing_id(con):
    """Postgres does not advance an identity when a row is inserted with an
    explicit id, and the reference bootstrap does exactly that for the singleton
    config rows. The counter then still points at 1 while a row with id 1
    exists, so the FIRST sequence a user creates dies with a duplicate-key
    error. Reproduced for real on 2026-09-12 by adding a second sequence.
    """
    rows = db.all_(con, """
        SELECT c.relname AS tbl, a.attname AS col,
               pg_get_serial_sequence(quote_ident(c.relname), a.attname) AS seq
          FROM pg_class c
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
                             AND NOT a.attisdropped
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND pg_get_serial_sequence(quote_ident(c.relname), a.attname) IS NOT NULL
      ORDER BY c.relname""")
    assert rows, "expected identity columns to exist"
    behind = []
    for r in rows:
        mx = db.scalar(con, "SELECT COALESCE(max(%s), 0) FROM %s" % (r["col"], r["tbl"])) or 0
        last = db.scalar(con, "SELECT last_value FROM %s" % r["seq"])
        called = db.scalar(con, "SELECT is_called FROM %s" % r["seq"])
        nxt = (last + 1) if called else last
        if mx >= nxt:
            behind.append("%s.%s (max=%s, next=%s)" % (r["tbl"], r["col"], mx, nxt))
    assert not behind, ("identity counters behind their table — the next insert "
                        "will fail: %s. Run db.resync_identities()." % behind)


def test_a_new_row_can_actually_be_inserted_into_a_seeded_table(con):
    """The end-user-visible form of the bug above: creating a second nurture
    sequence must simply work."""
    cur = con.execute("INSERT INTO sequences (name, active) VALUES (?, TRUE)",
                      ("%s-seq" % MARKER,))
    new_id = cur.lastrowid
    assert new_id and new_id > 1
    con.execute("DELETE FROM sequences WHERE id=?", (new_id,))
    con.commit()
