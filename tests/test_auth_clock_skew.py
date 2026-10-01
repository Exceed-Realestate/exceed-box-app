"""A token used the moment it is issued must not bounce off a slow server clock.

the production host's clock ran 1.2 s behind Supabase's, so a fresh token's `iat` was in
the server's future and PyJWT rejected it as "not yet valid" — the first call
after signing in failed at random. Verified here on the HS256 path, which
shares the same decode options as the JWKS path.
"""
from __future__ import annotations

import os
import sys
import time

import jwt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth  # noqa: E402

SECRET = "test-secret-for-skew-only-0123456789abcdef"


@pytest.fixture()
def hs256(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWKS_URL", None)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", SECRET)
    monkeypatch.setattr(auth, "SUPABASE_ISSUER", None)


def _token(**claims):
    now = int(time.time())
    body = {"sub": "u1", "email": "a@exceed-re.ae", "aud": auth.SUPABASE_AUD,
            "iat": now, "exp": now + 3600, **claims}
    return jwt.encode(body, SECRET, algorithm="HS256")


def test_token_issued_seconds_in_the_future_is_accepted(hs256):
    now = int(time.time())
    assert auth._decode(_token(iat=now + 5, nbf=now + 5))["sub"] == "u1"


def test_expired_token_is_still_rejected(hs256):
    now = int(time.time())
    with pytest.raises(Exception) as e:
        auth._decode(_token(iat=now - 7200, exp=now - 120))
    assert getattr(e.value, "status_code", None) == 401


def test_far_future_token_is_rejected(hs256):
    now = int(time.time())
    with pytest.raises(Exception) as e:
        auth._decode(_token(iat=now + 600, nbf=now + 600))
    assert getattr(e.value, "status_code", None) == 401
