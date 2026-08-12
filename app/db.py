"""Database access. Deliberately thin — sqlite3 from the standard library.

One connection factory, one init, a couple of row helpers. No ORM: the schema
is the design document, and an ORM would hide it.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("EXCEEDBOX_DB", ROOT / "data" / "exceedbox.db"))
SCHEMA = ROOT / "schema.sql"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init(con: sqlite3.Connection | None = None) -> None:
    """Create the schema. Idempotent."""
    own = con is None
    con = con or connect()
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.commit()
    if own:
        con.close()


def reset() -> None:
    """Drop the file and rebuild. Only used by tests and seed."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    init()


def one(con: sqlite3.Connection, sql: str, args=()) -> sqlite3.Row | None:
    return con.execute(sql, args).fetchone()


def all_(con: sqlite3.Connection, sql: str, args=()) -> list:
    return con.execute(sql, args).fetchall()


def scalar(con: sqlite3.Connection, sql: str, args=(), default=0):
    row = con.execute(sql, args).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]
