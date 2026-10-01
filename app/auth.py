"""Auth + RBAC (SPEC.md "Auth + identity", "RBAC — the heart of this task").

Two verification paths, chosen once at import time from the environment:

  SUPABASE_JWT_SECRET set   → verify real Supabase JWTs, HS256. Production.
  EXCEEDBOX_DEV_AUTH=1      → accept devauth.py's local tokens instead, only
                              if SUPABASE_JWT_SECRET is NOT set. Local/tests.
  neither                   → every request is unauthenticated. Fails closed:
                              there is no way to mint an accepted token, so
                              nothing behind current_user() is reachable.

current_user() is the one FastAPI dependency every protected route takes.
Capability checks (require()) are also here so app/api.py stays about HTTP
shapes, not policy — the permission matrix in SPEC.md lives in one place
(ROLE_CAPS below) and nowhere else.

N1 (FABLE-AUDIT-R2.md, HIGH) — D20's "@exceed-re.ae only" login rule used to
live nowhere in this file: the signature was verified and that was it, so
the domain lock rested entirely on Supabase project config this repo does
not control. Two things fix that, both in current_user()/load_or_provision()
below, not left to config: (1) every token's email domain is checked
server-side against ALLOWED_EMAIL_DOMAINS — a real 403, not a hope — before
anything else happens; (2) accounts are identified by the token's stable
`sub`, never adopted by a matching `email` claim onto an account that
already belongs to a different `sub` (that was the account-takeover path —
a validly-signed token bearing someone else's email could inherit their
active account and role). Email is a provisioning attribute (which inactive
row to activate into, the first time a real person logs in); `sub` is the
identity key.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

import jwt
from fastapi import Header, HTTPException

from . import devauth

log = logging.getLogger("exceedbox.auth")

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

# Asymmetric verification (2026-09-10). Supabase now signs project JWTs with
# per-project ES256 keys published at /auth/v1/.well-known/jwks.json; the
# shared HS256 secret is the legacy path and is absent on new projects. The
# exceed-box project was checked directly and publishes exactly one ES256 key,
# so HS256-only verification would reject every real token it issues.
#
# SUPABASE_URL is all that is needed: the JWKS URL and the expected issuer are
# both derived from it, which removes the "issuer was never checked" gap at the
# same time — a token from another Supabase project no longer even parses.
JWKS_ALGORITHMS = ["ES256", "RS256", "EdDSA"]


def resolve_auth_config(env=None) -> dict:
    """Work out the verification configuration from an environment mapping.

    Pure, so the rules below can be tested without reloading this module and
    without mutating os.environ — reloading a module that FastAPI has already
    bound route dependencies to is a good way to break unrelated tests.
    """
    env = os.environ if env is None else env
    url = (env.get("SUPABASE_URL") or "").rstrip("/")
    secret = env.get("SUPABASE_JWT_SECRET") or ""
    issuer = env.get("EXCEEDBOX_SUPABASE_ISSUER") or (
        ("%s/auth/v1" % url) if url else "")
    jwks = env.get("EXCEEDBOX_SUPABASE_JWKS_URL") or (
        ("%s/auth/v1/.well-known/jwks.json" % url) if url else "")
    # Dev auth is refused whenever ANY real verification path is configured,
    # not just the legacy secret — otherwise pointing the service at a real
    # Supabase project while EXCEEDBOX_DEV_AUTH lingered in the environment
    # would leave the unauthenticated token minter live in production.
    real = bool(secret or jwks)
    return {
        "url": url,
        "secret": secret,
        "issuer": issuer,
        "jwks_url": jwks,
        "real_auth_configured": real,
        "dev_auth_enabled": env.get("EXCEEDBOX_DEV_AUTH") == "1" and not real,
    }


_CFG = resolve_auth_config()
SUPABASE_URL = _CFG["url"]
SUPABASE_ISSUER = _CFG["issuer"]
SUPABASE_JWKS_URL = _CFG["jwks_url"]
_REAL_AUTH_CONFIGURED = _CFG["real_auth_configured"]
DEV_AUTH_ENABLED = _CFG["dev_auth_enabled"]

# N1 — the allowed login domain(s), server-side and configurable, never left
# to Supabase project config alone. D20/D21: exceed-re.ae is the one real
# domain; EXCEEDBOX_ALLOWED_EMAIL_DOMAINS (comma-separated) lets tests and
# any future second domain override it without a code change.
ALLOWED_EMAIL_DOMAINS = tuple(
    d.strip().lower().lstrip("@")
    for d in os.environ.get("EXCEEDBOX_ALLOWED_EMAIL_DOMAINS", "exceed-re.ae").split(",")
    if d.strip())

# N8 (FABLE-AUDIT-R2.md, LOW) — the JWT decode used to pass verify_aud:False,
# so a token minted for a different audience/project sharing the same HS256
# secret would be accepted. Supabase's own tokens (and devauth.mint()'s
# dev-shaped ones) carry aud="authenticated"; verified properly now.
SUPABASE_AUD = os.environ.get("EXCEEDBOX_SUPABASE_AUD", "authenticated")

if os.environ.get("EXCEEDBOX_DEV_AUTH") == "1" and _REAL_AUTH_CONFIGURED:
    log.warning("EXCEEDBOX_DEV_AUTH=1 is set, but real Supabase verification is also "
                "configured — dev auth is IGNORED and the /api/devtoken minter is off. "
                "This is correct for production; unset SUPABASE_URL and "
                "SUPABASE_JWT_SECRET locally if you meant to use dev auth.")
elif DEV_AUTH_ENABLED:
    log.warning("=" * 78)
    log.warning("EXCEEDBOX_DEV_AUTH=1 — accepting locally-minted dev tokens, NOT real "
                "Supabase JWTs. This must never be set in production.")
    log.warning("=" * 78)
elif not _REAL_AUTH_CONFIGURED:
    log.warning("Neither SUPABASE_URL nor SUPABASE_JWT_SECRET is set, and "
                "EXCEEDBOX_DEV_AUTH is not '1' — every request will be rejected as "
                "unauthenticated. Set SUPABASE_URL for a real project.")
elif not SUPABASE_ISSUER:
    log.warning("A JWKS URL is configured but no issuer could be derived — tokens will "
                "be accepted from ANY issuer that this key set can verify. Set "
                "SUPABASE_URL (preferred) or EXCEEDBOX_SUPABASE_ISSUER.")


ROLES = ("admin", "marketing", "office_manager", "sales")

# ── the permission matrix, SPEC.md § "Permission matrix — enforced server-side" ──
# One capability per matrix row. Endpoint code calls require(user, "cap") and
# gets a real 403 — never an empty 200 — when the caller's role lacks it.
ROLE_CAPS = {
    "admin": {
        "tasks.own", "tasks.all",
        "leads.own", "leads.all",
        "leads.edit.own", "leads.edit.any",
        "assign",
        "leads.stage.own", "leads.stage.any",
        "leads.signal.own", "leads.signal.any",
        "dashboard.company", "dashboard.per_rep",
        "import.csv",
        "consent.own", "consent.any",
        "users.manage",
        # SPEC-V2 §Roles — new screens
        "nurture.view", "nurture.edit",
        "sns.view",
        "booking.manage",
        "assignment.manage",
        "integrations.view", "integrations.sync",
    },
    "marketing": {
        "tasks.own",
        "leads.own", "leads.all",
        "leads.edit.own",
        "dashboard.company",
        "import.csv",
        "consent.own",              # only on leads they own — aggregate only otherwise
        "nurture.view", "nurture.edit",
        "sns.view",
        "integrations.view",
    },
    "office_manager": {
        "tasks.own", "tasks.all",
        "leads.own", "leads.all",
        "leads.edit.own", "leads.edit.any",
        "assign",
        "leads.stage.own", "leads.stage.any",
        "leads.signal.own", "leads.signal.any",
        "dashboard.company", "dashboard.per_rep",
        "consent.own", "consent.any",
        "nurture.view",
        "sns.view",
        "booking.manage",
        "assignment.manage",
        "integrations.view",
    },
    "sales": {
        "tasks.own",
        "leads.own",
        "leads.edit.own",
        "leads.stage.own",
        "leads.signal.own",
        "consent.own",
        # no nurture/sns/booking/assignment/integrations capability at all —
        # SPEC-V2 §Roles: "❌ no tab" for all four.
    },
}


@dataclass
class CurrentUser:
    id: str
    email: str
    display_name: Optional[str]
    role: str
    office: Optional[str]
    is_active: bool
    staff_id: Optional[int]
    caps: set = field(default_factory=set)

    def can(self, cap: str) -> bool:
        return cap in self.caps

    def as_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.display_name,
                "role": self.role, "office": self.office,
                "permissions": sorted(self.caps)}


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


_jwk_client = None


def _jwks() -> "jwt.PyJWKClient":
    """Lazily built, then reused. PyJWKClient caches keys in memory and only
    refetches when it meets a `kid` it has not seen, which is what makes key
    rotation a non-event rather than an outage."""
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = jwt.PyJWKClient(SUPABASE_JWKS_URL, cache_keys=True,
                                      lifespan=600)
    return _jwk_client


JWT_LEEWAY_SECONDS = int(os.environ.get("EXCEEDBOX_JWT_LEEWAY_SECONDS", "30"))


def _decode(token: str) -> dict:
    """Verify a bearer token. Raises _err on failure.

    Three paths, in order of preference:

      1. JWKS / asymmetric (ES256, RS256, EdDSA) — what Supabase actually issues
         today. Signature, issuer, audience and expiry are all verified.
      2. HS256 shared secret — the legacy path, kept for self-hosted GoTrue and
         for projects created before asymmetric signing. Issuer is verified too
         when SUPABASE_ISSUER is known.
      3. Dev tokens — local only, and only when NEITHER real path is configured.

    Every path verifies `aud` (N8) and, now, `iss`: without the issuer check a
    validly-signed token from a *different* Supabase project was accepted on
    its signature alone.
    """
    verify_iss = bool(SUPABASE_ISSUER)
    # leeway: the production host's clock measured 1.2 s behind (2026-09-14), so a token
    # used the instant Supabase issued it carried an `iat` in this server's
    # future and was rejected as "not yet valid" — the first request after
    # sign-in failed at random. 30 s of skew tolerance on iat/nbf/exp is the
    # usual allowance; expiry is still enforced.
    common = dict(audience=SUPABASE_AUD, leeway=JWT_LEEWAY_SECONDS,
                  options={"verify_aud": True, "verify_exp": True,
                           "verify_iss": verify_iss})
    if verify_iss:
        common["issuer"] = SUPABASE_ISSUER

    if SUPABASE_JWKS_URL:
        try:
            key = _jwks().get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=JWKS_ALGORITHMS, **common)
        except jwt.PyJWTError as e:
            # An HS256 token cannot be verified with a JWKS key. If the legacy
            # secret is also configured, fall through rather than reject — a
            # project mid-migration issues both shapes.
            if not SUPABASE_JWT_SECRET:
                raise _err(401, "invalid_token", "Supabase token rejected: %s" % e)
        except Exception as e:                      # JWKS endpoint unreachable
            if not SUPABASE_JWT_SECRET:
                log.error("JWKS fetch failed for %s: %s", SUPABASE_JWKS_URL, e)
                raise _err(503, "auth_unavailable",
                           "Could not reach the token signing keys. This is a "
                           "server-side outage, not a bad login.")

    if SUPABASE_JWT_SECRET:
        try:
            return jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"],
                              **common)
        except jwt.PyJWTError as e:
            raise _err(401, "invalid_token", "Supabase token rejected: %s" % e)

    if DEV_AUTH_ENABLED:
        try:
            return devauth.decode(token, audience=SUPABASE_AUD)
        except jwt.PyJWTError as e:
            raise _err(401, "invalid_token", "dev token rejected: %s" % e)

    raise _err(401, "auth_not_configured",
               "No Supabase verification is configured (set SUPABASE_URL, or "
               "SUPABASE_JWT_SECRET for a legacy project) and dev auth is off — "
               "no token can be accepted.")


def _email_domain_allowed(email: Optional[str]) -> bool:
    if not email or "@" not in email:
        return False
    return email.rsplit("@", 1)[1].strip().lower() in ALLOWED_EMAIL_DOMAINS


def ensure_staff_bridge(con, app_user_id: str, display_name: str = None) -> int:
    """Lazily create the staff row a login needs so tasks.py/scoring.py (both
    keyed on staff.id) keep working untouched for a person who only exists as
    an app_user so far. Idempotent per app_user."""
    row = con.execute("SELECT staff_id FROM app_user WHERE id=?", (app_user_id,)).fetchone()
    if row and row["staff_id"]:
        return row["staff_id"]
    access = {"admin": "admin", "office_manager": "manager"}.get(
        con.execute("SELECT role FROM app_user WHERE id=?", (app_user_id,)).fetchone()["role"],
        "agent")
    cur = con.execute("INSERT INTO staff (name, name_en, access) VALUES (?,?,?)",
                      (display_name or "?", display_name, access))
    staff_id = cur.lastrowid
    con.execute("UPDATE app_user SET staff_id=? WHERE id=?", (staff_id, app_user_id))
    con.commit()
    return staff_id


def load_or_provision(con, *, sub: str, email: str, name: str = None) -> CurrentUser:
    """Map a verified token to an app_user row. `sub` is the identity key —
    checked FIRST and always trusted once found. `email` is only ever a
    provisioning attribute: the fallback join key the FIRST time a real
    person with no `sub` on file yet logs in (SPEC.md), never a way to
    re-identify an account that already has a `sub`. An unknown person
    auto-provisions inactive with role 'sales' — fail closed, they can see
    nothing until an admin activates them.

    N1 (FABLE-AUDIT-R2.md) — the old version looked up by email whenever
    `sub` didn't match and silently adopted the incoming `sub` onto whatever
    row it found, even if that row already belonged to a DIFFERENT `sub`.
    That was the account-takeover path: a validly-signed token bearing an
    active staffer's email, minted by a different subject, inherited their
    account and role. Now: adoption only happens onto a row that has never
    had a `sub` (a real first login) and is recorded in audit_log; a row
    that already has a different `sub` is a conflict, not a match, and is
    rejected outright rather than silently reused or duplicated (email is
    UNIQUE, so a duplicate insert isn't even possible)."""
    row = con.execute("SELECT * FROM app_user WHERE supabase_uid=?", (sub,)).fetchone()
    if not row and email:
        existing = con.execute("SELECT * FROM app_user WHERE email=?", (email,)).fetchone()
        if existing and existing["supabase_uid"] and existing["supabase_uid"] != sub:
            # Someone else's account, already bound to a different subject.
            # This is exactly the takeover shape N1 closes — never adopt.
            log.error("REJECTED possible account-takeover attempt: token sub=%s claims "
                      "email=%s, which already belongs to app_user %s bound to a "
                      "different sub", sub, email, existing["id"])
            raise _err(403, "account_conflict",
                      "This email is already bound to a different account. Contact an admin.")
        if existing and not existing["supabase_uid"]:
            con.execute("UPDATE app_user SET supabase_uid=? WHERE id=?", (sub, existing["id"]))
            audit(con, None, "adopt_supabase_uid", "app_user", existing["id"],
                 before={"supabase_uid": None}, after={"supabase_uid": sub, "email": email})
            row = con.execute("SELECT * FROM app_user WHERE id=?", (existing["id"],)).fetchone()

    if not row:
        new_id = str(uuid.uuid4())
        con.execute(
            """INSERT INTO app_user (id, supabase_uid, email, display_name, role, is_active)
               VALUES (?,?,?,?,?,FALSE)""",
            (new_id, sub, email, name or (email.split("@")[0] if email else "unknown"), "sales"))
        con.commit()
        row = con.execute("SELECT * FROM app_user WHERE id=?", (new_id,)).fetchone()
        log.warning("auto-provisioned unknown login %s as inactive sales — an admin must "
                    "activate before they can see anything", email)

    is_active = bool(row["is_active"])
    role = row["role"]
    return CurrentUser(
        id=row["id"], email=row["email"], display_name=row["display_name"],
        role=role, office=row["office"], is_active=is_active,
        staff_id=row["staff_id"],
        caps=ROLE_CAPS.get(role, set()) if is_active else set(),
    )


# ── short-lived user cache ───────────────────────────────────────────────────
# Every protected request resolved its app_user row with its own database
# checkout — on the production host a checkout + BEGIN + SELECT + rollback is ~1.3 s before
# the route has done anything. The row changes only when an admin edits a user,
# and edit_user() calls forget_users(), so this process sees its own edits at
# once. The OTHER uvicorn worker can serve a stale role or active flag for at
# most AUTH_CACHE_SECONDS — the accepted cost. The signature, expiry and domain
# checks above still run on every request; only the row lookup is cached.
# SQLite (the test suite) defaults to 0 so tests that flip a role see it
# immediately.
import threading as _threading
import time as _time

_user_cache: dict = {}
_user_cache_lock = _threading.Lock()


def _cache_seconds() -> float:
    from . import db
    default = "30" if db.DIALECT == "postgres" else "0"
    return float(os.environ.get("EXCEEDBOX_AUTH_CACHE_SECONDS", default))


def _cached_user(sub, email):
    ttl = _cache_seconds()
    if ttl <= 0:
        return None
    with _user_cache_lock:
        hit = _user_cache.get((sub, email))
    if hit and _time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


def _remember_user(sub, email, user) -> None:
    if _cache_seconds() <= 0:
        return
    with _user_cache_lock:
        if len(_user_cache) > 1000:        # bounded: a few hundred staff at most
            _user_cache.clear()
        _user_cache[(sub, email)] = (_time.monotonic(), user)


def forget_users() -> None:
    """Drop every cached user. Call after anything that changes app_user."""
    with _user_cache_lock:
        _user_cache.clear()


def open_access_email() -> Optional[str]:
    """EXCEEDBOX_OPEN_ACCESS_EMAIL — Balraj, 2026-10-01: "remove login from
    exceed box". When set, a request WITHOUT a token is treated as this
    existing app_user (e.g. qa-admin@exceed-re.ae) instead of a 401, so the
    staff tool opens with no sign-in. A request that DOES carry a token is
    still verified normally. Unset = login required again. Never leave it
    on once real customer leads are in the database."""
    v = (os.environ.get("EXCEEDBOX_OPEN_ACCESS_EMAIL") or "").strip().lower()
    return v or None


def _open_access_user() -> Optional[CurrentUser]:
    email = open_access_email()
    if not email:
        return None
    cached = _cached_user("open-access", email)
    if cached is not None:
        return cached
    from . import db
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM app_user WHERE lower(email)=?", (email,)).fetchone()
        if not row or not row["supabase_uid"]:
            log.error("EXCEEDBOX_OPEN_ACCESS_EMAIL=%s has no bound app_user row; "
                      "falling back to login required", email)
            return None
        user = load_or_provision(con, sub=row["supabase_uid"], email=row["email"])
    finally:
        con.close()
    if not user.is_active:
        return None
    _remember_user("open-access", email, user)
    return user


def current_user(authorization: Optional[str] = Header(None)) -> CurrentUser:
    """The one dependency every protected route takes. Attaches con-free —
    each caller still opens its own db connection; this only needs one to
    resolve the user, and closes it immediately.

    N1 (FABLE-AUDIT-R2.md) — the domain lock (D20: @exceed-re.ae only) is
    enforced HERE, server-side, on every token, unconditionally — never left
    to Supabase project config to be the only gate. Checked before touching
    the database at all: an out-of-domain token gets a real 403 and never
    reaches load_or_provision, so it cannot even auto-provision an inactive
    row for an address that was never allowed to sign in."""
    if not authorization or not authorization.lower().startswith("bearer "):
        open_user = _open_access_user()
        if open_user is not None:
            return open_user
        raise _err(401, "missing_token", "Authorization: Bearer <token> is required.")
    token = authorization.split(" ", 1)[1].strip()
    claims = _decode(token)
    sub = claims.get("sub")
    email = claims.get("email")
    name = claims.get("name") or claims.get("user_metadata", {}).get("full_name")
    if not sub:
        raise _err(401, "invalid_token", "Token has no sub claim.")
    if not _email_domain_allowed(email):
        raise _err(403, "domain_not_allowed",
                  "This account's email domain is not permitted to sign in. Allowed: %s"
                  % ", ".join(ALLOWED_EMAIL_DOMAINS))
    # email_verified is not present on every token shape this repo accepts
    # (devauth's dev tokens don't set it), so it is only ever a hard reject
    # when a token EXPLICITLY says the address is unverified — never
    # required to be present, only trusted when it says no.
    if claims.get("email_verified") is False:
        raise _err(403, "email_not_verified", "This account's email is not verified.")

    user = _cached_user(sub, email)
    if user is None:
        from . import db
        con = db.connect()
        try:
            user = load_or_provision(con, sub=sub, email=email, name=name)
        finally:
            con.close()
        _remember_user(sub, email, user)

    if not user.is_active:
        raise _err(403, "inactive_account",
                  "Your account exists but has not been activated by an admin yet.")
    return user


def require(user: CurrentUser, cap: str) -> None:
    """Raise a real 403 — never an empty 200 — when the caller's role lacks
    the capability the endpoint needs."""
    if not user.can(cap):
        raise _err(403, "forbidden",
                  "Role '%s' does not have capability '%s'." % (user.role, cap))


def audit(con, actor: Optional[CurrentUser], action: str, entity: str, entity_id,
         before=None, after=None) -> None:
    """Every mutating endpoint calls this. D4: the manual +30 must be
    attributable; SPEC.md generalises that to every write."""
    import json
    con.execute(
        """INSERT INTO audit_log (actor_user_id, action, entity, entity_id, before, after)
           VALUES (?,?,?,?,?,?)""",
        (actor.id if actor else None, action, entity, str(entity_id) if entity_id is not None else None,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None))
    con.commit()
