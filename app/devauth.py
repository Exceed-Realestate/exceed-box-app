"""Dev-only token minting so the app and the test suite can run with zero
cloud dependency (SPEC.md: "Nothing about Supabase may be required to run the
tests").

This is NOT wired into production by default. It only does anything at all
when `EXCEEDBOX_DEV_AUTH=1` is set — that gate lives in app/auth.py, which is
where verification happens; this module only mints tokens, and minting a
token nobody will accept is harmless. The two things that make it impossible
to mis-flip in production:

  1. auth.py refuses to verify a dev token unless EXCEEDBOX_DEV_AUTH=1.
  2. if SUPABASE_JWT_SECRET is set (i.e. this looks like a real deployment),
     auth.py verifies against that and never falls back to the dev secret,
     regardless of EXCEEDBOX_DEV_AUTH.

DEV_SECRET is deliberately not a real secret — it is checked into the repo,
named "insecure", and only meaningful when EXCEEDBOX_DEV_AUTH=1 AND no real
SUPABASE_JWT_SECRET is configured.
"""
from __future__ import annotations

import time
import uuid

import jwt

DEV_SECRET = "exceedbox-local-dev-insecure-do-not-use-in-prod"
DEV_ISSUER = "exceedbox-devauth"


def mint(*, sub: str = None, email: str, ttl_seconds: int = 3600) -> str:
    """Mint a local dev JWT shaped like a Supabase one (sub + email claims).

    Used by seed.py (to print login tokens for manual testing) and by
    tests/test_permissions.py (to log in as each of the four roles without a
    real Supabase project). sub defaults to a fresh uuid4 — pass a stable one
    to mint tokens for the same person twice.
    """
    now = int(time.time())
    payload = {
        "sub": sub or str(uuid.uuid4()),
        "email": email,
        "aud": "authenticated",
        "iss": DEV_ISSUER,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, DEV_SECRET, algorithm="HS256")


def decode(token: str, audience: str = "authenticated") -> dict:
    """Raises jwt exceptions on a bad/expired token — caller (auth.py) turns
    those into a 401. N8 (FABLE-AUDIT-R2.md) — verifies `aud` like the real
    Supabase path does now, since mint() always sets it; a dev token is only
    ever meant to be as permissive as the real thing, never more."""
    return jwt.decode(token, DEV_SECRET, algorithms=["HS256"],
                      audience=audience, options={"verify_aud": True})
