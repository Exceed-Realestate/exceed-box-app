"""Asymmetric (JWKS) token verification, issuer pinning, and the dev-auth lock.

Hermetic: a real ES256 keypair is generated in-process and served from a
throwaway HTTP server on localhost, so these run offline and in CI. No Supabase
project is contacted.

Why this file exists: the exceed-box Supabase project publishes exactly one
ES256 key and issues ES256 tokens. The verification code understood only the
legacy HS256 shared secret, so it would have rejected every real login. It also
never checked `iss`, so a validly-signed token from any *other* Supabase project
was accepted on its signature alone.

Note on isolation: this file never reloads app.auth and never mutates
os.environ. Reloading a module that FastAPI has already bound route
dependencies to broke 92 unrelated tests when it was written that way.
Configuration rules are tested through the pure `resolve_auth_config()`, and
`_decode` is tested by patching module attributes and restoring them.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# app.auth resolves its configuration once, at import. This file sorts first
# alphabetically, so whatever it leaves in os.environ is what the whole suite
# gets — set the same dev-auth flag every other test module sets, or all of
# them fail with 401s that have nothing to do with this file.
os.environ.setdefault("EXCEEDBOX_DEV_AUTH", "1")

from app import auth   # noqa: E402

KID = "test-key-%s" % uuid.uuid4().hex[:8]
ISSUER = "https://example-project.supabase.co/auth/v1"


def _make_key():
    priv = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "ES256"})
    return priv, {"keys": [jwk]}


PRIVATE_KEY, JWKS_DOC = _make_key()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                  # noqa: N802
        body = json.dumps(JWKS_DOC).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                         # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def jwks_url():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    yield "http://127.0.0.1:%d/jwks.json" % srv.server_port
    srv.shutdown()


@contextmanager
def configured(*, jwks=None, issuer=ISSUER, secret=None, dev=False):
    """Point app.auth at a given configuration for the duration of the block,
    then put every attribute back exactly as it was."""
    names = ("SUPABASE_JWKS_URL", "SUPABASE_ISSUER", "SUPABASE_JWT_SECRET",
             "DEV_AUTH_ENABLED", "_REAL_AUTH_CONFIGURED", "_jwk_client")
    saved = {n: getattr(auth, n) for n in names}
    auth.SUPABASE_JWKS_URL = jwks or ""
    auth.SUPABASE_ISSUER = issuer or ""
    auth.SUPABASE_JWT_SECRET = secret or None
    auth._REAL_AUTH_CONFIGURED = bool(jwks or secret)
    auth.DEV_AUTH_ENABLED = dev and not auth._REAL_AUTH_CONFIGURED
    auth._jwk_client = None
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(auth, n, v)


def _token(*, issuer=ISSUER, aud="authenticated", email="someone@exceed-re.ae",
           expires_in=3600, kid=KID, key=None):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid5(uuid.NAMESPACE_DNS, email)),
        "email": email,
        "aud": aud,
        "iss": issuer,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    return jwt.encode(payload, key or PRIVATE_KEY, algorithm="ES256",
                      headers={"kid": kid})


# ── the real Supabase shape ──────────────────────────────────────────────────

def test_es256_token_is_accepted(jwks_url):
    with configured(jwks=jwks_url):
        claims = auth._decode(_token())
    assert claims["email"] == "someone@exceed-re.ae"


def test_hs256_only_config_would_have_rejected_it():
    """The regression this file exists for: with only the legacy secret set, a
    genuine ES256 Supabase token is refused."""
    with configured(jwks=None, secret="legacy-secret"):
        with pytest.raises(Exception) as e:
            auth._decode(_token())
    assert "invalid_token" in str(e.value.detail)


# ── issuer pinning ───────────────────────────────────────────────────────────

def test_token_from_another_supabase_project_is_rejected(jwks_url):
    """Same signing key, different issuer. Before the issuer check this passed
    on the signature alone."""
    with configured(jwks=jwks_url):
        with pytest.raises(Exception) as e:
            auth._decode(_token(issuer="https://someone-elses.supabase.co/auth/v1"))
    assert "invalid_token" in str(e.value.detail)


def test_wrong_audience_is_rejected(jwks_url):
    with configured(jwks=jwks_url):
        with pytest.raises(Exception):
            auth._decode(_token(aud="anon"))


def test_expired_token_is_rejected(jwks_url):
    with configured(jwks=jwks_url):
        with pytest.raises(Exception):
            auth._decode(_token(expires_in=-30))


def test_token_signed_by_an_unknown_key_is_rejected(jwks_url):
    other_priv, _ = _make_key()
    with configured(jwks=jwks_url):
        with pytest.raises(Exception):
            auth._decode(_token(key=other_priv))


def test_nothing_configured_means_no_token_is_accepted():
    with configured(jwks=None, issuer=None, secret=None, dev=False):
        with pytest.raises(Exception) as e:
            auth._decode(_token())
    assert "auth_not_configured" in str(e.value.detail)


# ── configuration derivation (pure — no module state touched) ────────────────

def test_issuer_and_jwks_are_derived_from_supabase_url():
    cfg = auth.resolve_auth_config({"SUPABASE_URL": "https://abc123.supabase.co"})
    assert cfg["issuer"] == "https://abc123.supabase.co/auth/v1"
    assert cfg["jwks_url"] == \
        "https://abc123.supabase.co/auth/v1/.well-known/jwks.json"


def test_trailing_slash_on_supabase_url_does_not_double_up():
    cfg = auth.resolve_auth_config({"SUPABASE_URL": "https://abc123.supabase.co/"})
    assert cfg["issuer"] == "https://abc123.supabase.co/auth/v1"


def test_dev_auth_is_off_when_jwks_is_configured():
    cfg = auth.resolve_auth_config({"SUPABASE_URL": "https://abc.supabase.co",
                                    "EXCEEDBOX_DEV_AUTH": "1"})
    assert cfg["dev_auth_enabled"] is False


def test_dev_auth_is_off_when_only_the_legacy_secret_is_configured():
    cfg = auth.resolve_auth_config({"SUPABASE_JWT_SECRET": "legacy",
                                    "EXCEEDBOX_DEV_AUTH": "1"})
    assert cfg["dev_auth_enabled"] is False


def test_dev_auth_is_on_only_when_nothing_real_is_configured():
    cfg = auth.resolve_auth_config({"EXCEEDBOX_DEV_AUTH": "1"})
    assert cfg["dev_auth_enabled"] is True
    assert cfg["real_auth_configured"] is False


def test_explicit_overrides_beat_the_derived_values():
    cfg = auth.resolve_auth_config({
        "SUPABASE_URL": "https://abc.supabase.co",
        "EXCEEDBOX_SUPABASE_ISSUER": "https://custom/auth/v1",
        "EXCEEDBOX_SUPABASE_JWKS_URL": "https://custom/keys.json"})
    assert cfg["issuer"] == "https://custom/auth/v1"
    assert cfg["jwks_url"] == "https://custom/keys.json"
