"""Database access. Deliberately thin — sqlite3 from the standard library.

One connection factory, one init, a couple of row helpers. No ORM: the schema
is the design document, and an ORM would hide it.

SQLite stays the only thing the test suite ever touches (SPEC.md: "Nothing
about Supabase may be required to run the tests"). Postgres is the deploy
target — see migrations/ for the real DDL.

WHY THE ADAPTER DOES THE WORK (2026-09-10)
------------------------------------------
There are ~133 tested call sites written in SQLite idiom: "?" placeholders,
`datetime('now')`, timestamps read back as ISO *strings*, booleans read back as
1/0, `lastrowid` after an INSERT. Rewriting all of them into a second dialect
would put the tested logic at risk for no behavioural gain, so the translation
lives here instead, in one auditable place:

  * "?"                -> "%s"                (string-literal aware, not a blind sub)
  * datetime('now')    -> now()
  * INSERT OR IGNORE   -> INSERT ... ON CONFLICT DO NOTHING
  * INSERT without RETURNING -> "... RETURNING id", captured as .lastrowid
  * on read: datetime  -> naive-UTC ISO text, byte-identical in shape to what
                          SQLite's datetime('now') produces
  * on read: bool      -> 1/0,  UUID -> str,  Decimal -> float,
                          jsonb dict/list -> JSON text

The three things translation cannot safely do are fixed at the source instead,
in a form both dialects accept: `= TRUE` rather than `= 1` on real boolean
columns (SQLite has understood TRUE since 3.23), an alias on the one derived
table, and a dialect-neutral IntegrityError.

Set DATABASE_URL to point at Postgres. psycopg (v3) is only imported when
DATABASE_URL is actually set, so SQLite-only runs never pay for it.
"""
from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import json
import os
import re
import sqlite3
import uuid as _uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("EXCEEDBOX_DB", ROOT / "data" / "exceedbox.db"))
SCHEMA = ROOT / "schema.sql"
MIGRATIONS = ROOT / "migrations"

DATABASE_URL = os.environ.get("DATABASE_URL")
DIALECT = "postgres" if DATABASE_URL else "sqlite"


# ── dialect-neutral exceptions ───────────────────────────────────────────────
# ingest.py catches these around dedupe inserts. On SQLite it is
# sqlite3.IntegrityError; on Postgres it is psycopg.errors.IntegrityError, a
# different class from a different module. Callers import this tuple instead of
# naming either one.
def _integrity_errors():
    errs = [sqlite3.IntegrityError]
    if DIALECT == "postgres":
        try:
            import psycopg
            errs.append(psycopg.IntegrityError)
        except Exception:      # pragma: no cover - psycopg absent
            pass
    return tuple(errs)


IntegrityError = _integrity_errors()


# ── SQL translation ──────────────────────────────────────────────────────────
_DATETIME_NOW = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_IS_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_HAS_RETURNING = re.compile(r"\bRETURNING\b", re.IGNORECASE)
# "INSERT INTO t (id, name) VALUES" — does the column list name id?
_INSERT_NAMES_ID = re.compile(
    r"^\s*INSERT\s+INTO\s+\w+\s*\([^)]*\bid\b[^)]*\)", re.IGNORECASE)
_INSERT_COLUMNS_END = re.compile(r"(\))\s*VALUES", re.IGNORECASE)


def _qmark_to_pyformat(sql: str) -> str:
    """Replace ? with %s outside string literals, and double any literal %
    so psycopg's own formatting leaves it alone."""
    out = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == "'":
                # '' is an escaped quote inside a literal, not the end of one
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_str = False
            elif ch == "%":
                # A literal % INSIDE a string literal must be doubled too.
                # psycopg scans the whole statement for % when it binds, and
                # does not care about SQL quoting — so `LIKE 'zzz%'` raised
                # "only '%s', '%b', '%t' are allowed as placeholders". Doubling
                # only outside quotes left every inline LIKE pattern broken on
                # Postgres while working fine on SQLite.
                out.append("%%")
                i += 1
                continue
            out.append(ch)
        else:
            if ch == "'":
                in_str = True
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            elif ch == "%":
                out.append("%%")
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def translate(sql: str) -> str:
    """SQLite-idiom SQL -> Postgres. Public so the parity test can assert on it."""
    sql = _DATETIME_NOW.sub("now()", sql)
    if _INSERT_OR_IGNORE.search(sql):
        sql = _INSERT_OR_IGNORE.sub("INSERT INTO", sql)
        if "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return _qmark_to_pyformat(sql)


# ── read coercion: make Postgres values look like SQLite ones ────────────────
def _coerce(value):
    if value is None or isinstance(value, (int, float, str, bytes)):
        # bool is a subclass of int, so it has to be caught before this returns
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if isinstance(value, _dt.datetime):
        # SQLite's datetime('now') yields naive UTC, "YYYY-MM-DD HH:MM:SS".
        # scoring.py compares these against a naive utcnow(), so an aware value
        # here would raise TypeError. Normalise to UTC, then drop the tzinfo.
        if value.tzinfo is not None:
            value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, _decimal.Decimal):
        return float(value)
    if isinstance(value, (dict, list)):
        # jsonb comes back parsed; SQLite stores the same thing as TEXT and
        # callers json.loads() it themselves.
        return json.dumps(value, ensure_ascii=False)
    return value


class _PgRow(dict):
    """Answers both row["col"] (used everywhere) and row[0] (used by
    db.scalar()) the way sqlite3.Row does."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def keys(self):
        return super().keys()


def _row(raw):
    if raw is None:
        return None
    return _PgRow((k, _coerce(v)) for k, v in raw.items())


class _PgCursor:
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        return _row(self._cur.fetchone())

    def fetchall(self):
        return [_row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _PgConnection:
    """Enough DB-API surface for what this codebase actually calls, plus the
    SQLite-idiom translation described in the module docstring."""

    # Which tables have an `id` column, so "RETURNING id" is only appended
    # where it is valid, and which of those are GENERATED ALWAYS identities,
    # which refuse an explicitly supplied id without OVERRIDING SYSTEM VALUE.
    # Read once per process from the catalog — cheaper and far less fragile
    # than probing each INSERT and rolling back.
    _id_tables = None
    _identity_always = None

    def __init__(self, dsn: str = None, *, pool=None):
        import psycopg
        from psycopg.rows import dict_row
        self._psycopg = psycopg
        self._pool = pool
        self._used = False          # has any statement run on this checkout yet?
        if pool is not None:
            self._con = pool.getconn()
        else:
            self._con = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        if _PgConnection._id_tables is None:
            self._load_catalog()

    def _load_catalog(self) -> None:
        cur = self._con.cursor()
        cur.execute(
            "SELECT table_name, is_identity, identity_generation"
            "  FROM information_schema.columns"
            " WHERE table_schema = 'public' AND column_name = 'id'")
        rows = cur.fetchall()
        self._con.commit()
        _PgConnection._id_tables = {r["table_name"].lower() for r in rows}
        _PgConnection._identity_always = {
            r["table_name"].lower() for r in rows
            if r["is_identity"] == "YES" and r["identity_generation"] == "ALWAYS"}

    def execute(self, sql, args=()):
        """A pooled connection can be dropped by the network while it sits
        idle — seen live on the production host as "SSL error: unexpected eof" on the very
        first query of a request (a 500 on the voice-memos panel). When that
        happens on the FIRST statement of a checkout, nothing has been done in
        the transaction yet, so the broken connection is handed back (the pool
        discards it) and the statement runs once on a fresh one. A failure
        after any statement has run is never retried: that could repeat half
        of a write."""
        try:
            result = self._execute(sql, args)
        except self._psycopg.OperationalError:
            if self._pool is None or self._used:
                raise
            broken, self._con = self._con, None
            try:
                self._pool.putconn(broken)
            except Exception:       # pragma: no cover - pool already dropped it
                pass
            self._con = self._pool.getconn()
            self._used = True
            return self._execute(sql, args)
        self._used = True
        return result

    def _execute(self, sql, args=()):
        stmt = translate(sql)
        m = _IS_INSERT.match(stmt)
        table = m.group(1).lower() if m else None
        cur = self._con.cursor()

        if table is not None and table in (self._id_tables or ()):
            # SQLite lets a caller supply an explicit rowid; a GENERATED ALWAYS
            # identity does not, unless the statement says so. The singleton
            # config rows (sequences id=1, booking_settings id=1) rely on this.
            if (table in (self._identity_always or ())
                    and _INSERT_NAMES_ID.search(stmt)
                    and "OVERRIDING" not in stmt.upper()):
                stmt = _INSERT_COLUMNS_END.sub(
                    r"\1 OVERRIDING SYSTEM VALUE VALUES", stmt, count=1)
            if not _HAS_RETURNING.search(stmt):
                cur.execute(stmt.rstrip().rstrip(";") + " RETURNING id", tuple(args))
                # ON CONFLICT DO NOTHING can insert nothing, in which case there
                # is no row to return and lastrowid is legitimately None.
                row = cur.fetchone() if cur.description else None
                return _PgCursor(cur, _coerce(row["id"]) if row else None)

        cur.execute(stmt, tuple(args))
        return _PgCursor(cur)

    def executescript(self, sql):
        """Run a multi-statement script. Postgres accepts several statements in
        one execute() as long as no parameters are involved."""
        cur = self._con.cursor()
        cur.execute(sql)
        self._con.commit()

    def savepoint(self):
        """Context manager for a nested, independently-rollback-able block.
        ingest.py's dedupe path relies on a caught IntegrityError leaving the
        surrounding transaction usable, which is true on SQLite and false on
        Postgres unless the failing statement is wrapped like this."""
        return self._con.transaction()

    def commit(self):
        self._con.commit()

    def rollback(self):
        self._con.rollback()

    def close(self):
        con, self._con = self._con, None
        if con is None:             # already closed — callers close in finally
            return
        if self._pool is not None:
            # Roll back here rather than letting the pool do it: the pool does
            # exactly the same rollback but logs a warning for every read-only
            # request (all GETs leave a transaction open), which buried real
            # errors in the API log. A broken connection skips the rollback and
            # is discarded by the pool, so uncommitted writes never leak into
            # the next caller either way.
            try:
                if not con.closed and con.info.transaction_status != 0:   # not IDLE
                    con.rollback()
            except Exception:
                pass
            self._pool.putconn(con)
        else:
            con.close()


# ── connection pool ──────────────────────────────────────────────────────────
# Opening a connection to the Supabase pooler is a TLS handshake plus auth: on
# the production host, whose traffic leaves through a VPN, that measured
# 2–3 s, and every API request opened TWO (auth.current_user, then the route).
# Screens took 4–18 s and a long-lived request died with "SSL error: unexpected
# eof". Reusing connections removes the handshake from every request.
#
# One pool per process, built lazily so each uvicorn worker (forked) gets its
# own. No per-checkout health check: it cost a full round trip (0.33 s) on
# every checkout, and the one dropped connection seen in production died
# MID-query, which a checkout check cannot catch anyway. A connection that
# breaks is discarded by the pool when returned. Sizes stay small: Supabase's session pooler caps total client
# connections, and 2 API workers + the mail worker all draw from it.
# EXCEEDBOX_DB_POOL=0 turns it off (one-shot scripts, debugging).
_pool = None
_pool_lock = __import__("threading").Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=int(os.environ.get("EXCEEDBOX_DB_POOL_MIN", "1")),
                    max_size=int(os.environ.get("EXCEEDBOX_DB_POOL_MAX", "5")),
                    kwargs={"row_factory": dict_row, "autocommit": False},
                    max_idle=300, max_lifetime=1800, timeout=30,
                    name="exceedbox", open=True)
    return _pool


def _pool_enabled() -> bool:
    return os.environ.get("EXCEEDBOX_DB_POOL", "1") != "0"


class _SqliteSavepoint:
    """No-op stand-in so callers can use con.savepoint() on either dialect.
    SQLite does not abort the transaction on a constraint violation, so there
    is nothing to isolate."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _SqliteConnection(sqlite3.Connection):
    """sqlite3.Connection is a C type and will not take an attribute, so the
    savepoint() surface that _PgConnection offers is added by subclassing."""

    def savepoint(self):
        return _SqliteSavepoint()


def connect():
    if DIALECT == "postgres":
        if _pool_enabled():
            return _PgConnection(pool=_get_pool())
        return _PgConnection(DATABASE_URL)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), factory=_SqliteConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(con=None) -> None:
    """Create the schema. Idempotent.

    On SQLite this runs schema.sql (the test fixture). On Postgres it runs the
    migration runner instead — schema.sql is SQLite DDL and would fail there.
    """
    if DIALECT == "postgres":
        migrate(con)
        return
    own = con is None
    con = con or connect()
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    if own:
        con.close()


# ── migration runner (Postgres) ──────────────────────────────────────────────
def resync_identities(con=None, verbose: bool = False) -> list:
    """Advance every identity/serial counter past the largest id in its table.

    Postgres does NOT advance an identity when a row is inserted with an
    explicit id — and the reference-data bootstrap does exactly that for the
    singleton config rows (a sequence, booking settings). The counter therefore
    still points at 1 while a row with id 1 exists, and the FIRST genuine insert
    a user makes fails with a duplicate-key error.

    That is a day-one production failure with a confusing message, and it is
    invisible until someone tries to create something. It was found by trying to
    add a second nurture sequence.

    Idempotent and safe to run at any time; a no-op on SQLite.
    """
    own = con is None
    con = con or connect()
    fixed = []
    try:
        if DIALECT != "postgres":
            return fixed
        rows = all_(con, """
            SELECT c.relname AS tbl, a.attname AS col,
                   pg_get_serial_sequence(quote_ident(c.relname), a.attname) AS seq
              FROM pg_class c
              JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
                                 AND NOT a.attisdropped
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
               AND pg_get_serial_sequence(quote_ident(c.relname), a.attname) IS NOT NULL
          ORDER BY c.relname""")
        for r in rows:
            tbl, col, seq = r["tbl"], r["col"], r["seq"]
            mx = scalar(con, "SELECT COALESCE(max(%s), 0) FROM %s" % (col, tbl)) or 0
            last = scalar(con, "SELECT last_value FROM %s" % seq)
            called = scalar(con, "SELECT is_called FROM %s" % seq)
            nxt = (last + 1) if called else last
            if mx >= nxt:
                con.execute("SELECT setval(%s, %s, true)" % ("'" + seq + "'", int(mx)))
                fixed.append("%s.%s -> %d" % (tbl, col, mx + 1))
                if verbose:
                    print("resynced %s.%s: next id was %s, now %d"
                          % (tbl, col, nxt, mx + 1))
        con.commit()
        return fixed
    finally:
        if own:
            con.close()


def migrate(con=None, verbose: bool = False) -> list:
    """Apply every migrations/*.sql not yet recorded, in filename order.

    Each file runs inside its own transaction and is recorded in
    schema_migrations, so a re-run is a no-op and a failure leaves the previous
    migrations applied and this one not. Returns the list newly applied.
    """
    if DIALECT != "postgres":
        raise RuntimeError("migrate() is for Postgres; SQLite uses init()")
    own = con is None
    con = con or connect()
    try:
        con.executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in con.execute(
            "SELECT version FROM schema_migrations").fetchall()}
        applied = []
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            con.executescript(sql)
            con.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            con.commit()
            applied.append(version)
            if verbose:
                print("applied %s" % version)
        return applied
    finally:
        if own:
            con.close()


def reset() -> None:
    """Drop the file and rebuild. SQLite only — tests and seed.

    Deliberately refuses on Postgres: this is called by the test suite and by
    seed.py, and neither should ever be able to wipe a real database.
    """
    if DIALECT == "postgres":
        raise RuntimeError(
            "db.reset() refuses to run against Postgres. It drops everything, "
            "and DATABASE_URL is set. Unset it to reset the local SQLite file.")
    if DB_PATH.exists():
        DB_PATH.unlink()
    init()


def one(con, sql: str, args=()):
    return con.execute(sql, args).fetchone()


def all_(con, sql: str, args=()) -> list:
    return con.execute(sql, args).fetchall()


def scalar(con, sql: str, args=(), default=0):
    row = con.execute(sql, args).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]
