"""EXCEEDBOX_OPEN_ACCESS_EMAIL — the no-login switch (Balraj, 2026-10-01).

With it set, a request with no token is that existing user instead of a 401.
With it unset, the login requirement is exactly as before. A request that
does carry a token is still verified, so a bad token is never waved through.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, db  # noqa: E402


def _make_user(role="admin", active=True, bound=True):
    sub = str(uuid.uuid4()) if bound else None
    email = f"open{uuid.uuid4().hex[:8]}@exceed-re.ae"
    c = db.connect()
    db.init(c)
    c.execute("INSERT INTO app_user (id, supabase_uid, email, display_name, role, is_active) "
              "VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), sub, email, "Open", role, active))
    c.commit()
    c.close()
    return email


def test_no_token_is_401_when_switch_is_off(monkeypatch):
    monkeypatch.delenv("EXCEEDBOX_OPEN_ACCESS_EMAIL", raising=False)
    with pytest.raises(Exception) as e:
        auth.current_user(None)
    assert getattr(e.value, "status_code", None) == 401


def test_no_token_becomes_the_configured_user_when_switch_is_on(monkeypatch):
    email = _make_user(role="admin")
    monkeypatch.setenv("EXCEEDBOX_OPEN_ACCESS_EMAIL", email)
    auth.forget_users()
    user = auth.current_user(None)
    assert user.email == email and user.role == "admin"


def test_a_bad_token_is_still_rejected_when_switch_is_on(monkeypatch):
    monkeypatch.setenv("EXCEEDBOX_OPEN_ACCESS_EMAIL", _make_user())
    auth.forget_users()
    with pytest.raises(Exception) as e:
        auth.current_user("Bearer not-a-real-token")
    assert getattr(e.value, "status_code", None) == 401


@pytest.mark.parametrize("kwargs", [{"active": False}, {"bound": False}])
def test_falls_back_to_login_for_an_unusable_user(monkeypatch, kwargs):
    monkeypatch.setenv("EXCEEDBOX_OPEN_ACCESS_EMAIL", _make_user(**kwargs))
    auth.forget_users()
    with pytest.raises(Exception) as e:
        auth.current_user(None)
    assert getattr(e.value, "status_code", None) == 401
