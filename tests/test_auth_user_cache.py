"""The short-lived user cache in auth.current_user.

It exists to save a database round trip per request. What must never happen:
an admin deactivates or demotes someone and this process keeps honouring the
old row. edit_user() clears the cache, and the cache is keyed on (sub, email)
so one person's row can never answer for another's token.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, db, devauth  # noqa: E402


def _make_user(role="admin", active=True):
    sub, email = str(uuid.uuid4()), f"u{uuid.uuid4().hex[:8]}@exceed-re.ae"
    uid = str(uuid.uuid4())
    c = db.connect()
    db.init(c)
    c.execute("INSERT INTO app_user (id, supabase_uid, email, display_name, role, is_active) "
              "VALUES (?,?,?,?,?,?)", (uid, sub, email, "T", role, active))
    c.commit()
    c.close()
    return uid, sub, email


def _set(uid, **fields):
    c = db.connect()
    c.execute("UPDATE app_user SET %s WHERE id=?" % ", ".join(f"{k}=?" for k in fields),
              list(fields.values()) + [uid])
    c.commit()
    c.close()


def _resolve(sub, email):
    return auth.current_user("Bearer " + devauth.mint(sub=sub, email=email))


def test_off_by_default_on_sqlite(monkeypatch):
    monkeypatch.delenv("EXCEEDBOX_AUTH_CACHE_SECONDS", raising=False)
    assert auth._cache_seconds() == 0


def test_cached_row_is_reused_within_ttl(monkeypatch):
    monkeypatch.setenv("EXCEEDBOX_AUTH_CACHE_SECONDS", "60")
    auth.forget_users()
    uid, sub, email = _make_user(role="admin")
    assert _resolve(sub, email).role == "admin"
    _set(uid, role="sales")          # changed behind the cache's back
    assert _resolve(sub, email).role == "admin"


def test_forget_users_makes_an_edit_visible_immediately(monkeypatch):
    monkeypatch.setenv("EXCEEDBOX_AUTH_CACHE_SECONDS", "60")
    auth.forget_users()
    uid, sub, email = _make_user(role="admin", active=True)
    _resolve(sub, email)
    _set(uid, is_active=False)
    auth.forget_users()              # what edit_user() does after its UPDATE
    try:
        _resolve(sub, email)
        raise AssertionError("a deactivated user was still let in")
    except Exception as e:
        assert getattr(e, "status_code", None) == 403


def test_one_users_cache_entry_never_answers_for_another(monkeypatch):
    monkeypatch.setenv("EXCEEDBOX_AUTH_CACHE_SECONDS", "60")
    auth.forget_users()
    _, sub_a, email_a = _make_user(role="admin")
    _, sub_b, email_b = _make_user(role="sales")
    assert _resolve(sub_a, email_a).role == "admin"
    assert _resolve(sub_b, email_b).role == "sales"
    # same sub, different email claim → not a cache hit
    assert auth._cached_user(sub_a, email_b) is None
