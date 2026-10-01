"""C1 (FABLE-AUDIT.md, CRITICAL) — the media PII leak.

`GET /api/media/{category}/{filename}` used to gate on nothing but "any
active login" — verified live by Fable: a marketing user and a non-owner
sales rep both got HTTP 200 on someone else's business-card image (name +
company + phone + email + address in one file), fully defeating both the
contact-redaction rule (SPEC.md rule 2) and sales SQL isolation (rule 1).

This file proves the closed version:
  - a non-owner sales rep gets 403 on a business card AND a voice note
    belonging to a lead they don't own,
  - marketing (which has leads.all but never raw contact info it doesn't
    own) gets 403 on the same two files,
  - the owning sales rep and admin both still get 200,
  - `card_image_path` is null on a redacted LeadDetail (api.py:318's other
    half of the same bug) so there is nothing to even try fetching,
  - a non-existent/unmapped file is 404, not 403 (nothing to probe).

Runs standalone, same dev-auth harness as the other suites.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EXCEEDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "test_c1_media.db")
os.environ["EXCEEDBOX_UPLOAD_ROOT"] = tempfile.mkdtemp()
os.environ["EXCEEDBOX_DEV_AUTH"] = "1"
os.environ.pop("SUPABASE_JWT_SECRET", None)
# N1 (FABLE-AUDIT-R2.md) — see tests/test_permissions.py's identical line;
# this repeats it because app/auth.py's ALLOWED_EMAIL_DOMAINS is a
# module-level constant read once, by whichever test file imports app.auth
# first in a full `pytest tests` run.
os.environ["EXCEEDBOX_ALLOWED_EMAIL_DOMAINS"] = "test.example,contract.example,fable-fix.example,exceed-re.ae"

from fastapi.testclient import TestClient   # noqa: E402

from app import auth, db, devauth   # noqa: E402
from app.api import app       # noqa: E402

client = TestClient(app)

EMAILS = {
    "admin": "c1-admin@test.example",
    "marketing": "c1-marketing@test.example",
    "marketing2": "c1-marketing-other@test.example",   # N7 — a second marketing login,
                                                        # to prove import.csv alone isn't enough
    "sales": "c1-sales-owner@test.example",     # owns the lead under test
    "sales2": "c1-sales-other@test.example",    # does NOT own it
}


def hdr(role: str) -> dict:
    # N1 (FABLE-AUDIT-R2.md) — stable sub per role; see test_permissions.py's
    # identical comment. The N1-specific tests below mint their own tokens
    # directly with deliberately DIFFERENT subs to exercise the conflict.
    return {"Authorization": "Bearer " + devauth.mint(sub="c1-test-sub-%s" % role, email=EMAILS[role])}


def err_code(resp) -> str:
    return resp.json()["error"]["code"]


def fresh():
    db.reset()
    con = db.connect()
    con.execute("INSERT INTO settings (key,value) VALUES ('notify_threshold','40')")
    ids = {}
    role_col_for = {"sales2": "sales", "marketing2": "marketing"}
    for role, email in EMAILS.items():
        uid = "c1-%s" % role
        role_col = role_col_for.get(role, role)
        con.execute("""INSERT INTO app_user (id,email,display_name,role,is_active)
                       VALUES (?,?,?,?,1)""", (uid, email, role.title(), role_col))
        ids[role] = uid
    con.commit()
    con.close()
    return ids


def _scan_card_owned_by(owner_role: str) -> dict:
    """Creates a lead + business-card image through the real capture flow
    (POST /api/leads/scan), owned by `owner_role`. Returns the LeadDetail."""
    r = client.post("/api/leads/scan", headers=hdr(owner_role),
                    data={"name": "C1 Card Test — %s" % owner_role,
                          "company": "Acme Corp", "email": "c1-card-%s@example.com" % owner_role,
                          "phone": "090-1111-2222"},
                    files={"image": ("card.jpg", b"\xff\xd8\xff\xe0-fake-jpeg-bytes", "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()


def _add_voice_note_owned_by(owner_role: str, lead_id: int) -> dict:
    r = client.post("/api/leads/%d/notes" % lead_id, headers=hdr(owner_role),
                    files={"audio": ("note.m4a", b"fake-audio-bytes", "audio/m4a")})
    assert r.status_code == 200, r.text
    return r.json()


# ── the leak, closed ─────────────────────────────────────────────────────────

def test_media_business_card_denied_to_non_owner_sales_and_marketing():
    fresh()
    lead = _scan_card_owned_by("sales")
    card_path = lead["card_image_path"]
    assert card_path, "fixture must actually produce a card_image_path"
    url = "/api/media/%s" % card_path

    r_sales2 = client.get(url, headers=hdr("sales2"))
    assert r_sales2.status_code == 403, \
        "C1: a non-owner sales rep must be denied another rep's business-card image"
    assert err_code(r_sales2) == "forbidden"

    r_marketing = client.get(url, headers=hdr("marketing"))
    assert r_marketing.status_code == 403, \
        "C1: marketing must be denied a business-card image on a lead it does not own"
    assert err_code(r_marketing) == "forbidden"

    r_owner = client.get(url, headers=hdr("sales"))
    assert r_owner.status_code == 200, "the owning sales rep must still be able to see their own card"

    r_admin = client.get(url, headers=hdr("admin"))
    assert r_admin.status_code == 200, "admin must still be able to see any card"


def test_media_voice_note_denied_to_non_owner_sales_and_marketing():
    """N2 (FABLE-AUDIT-R2.md) — voice notes used to gate on _can_view_lead
    (leads.all), one tier weaker than the business card image, so marketing
    (leads.all but only consent.own) got a live 200 on a rep's spoken memo —
    the exact PII (name/phone/email said out loud) that contact redaction
    exists to withhold from marketing one field over. Gated on
    _can_see_contact now, same rule as business_cards, on purpose: a voice
    memo is contact-class data, not merely lead-class data."""
    fresh()
    lead = _scan_card_owned_by("sales")
    note = _add_voice_note_owned_by("sales", lead["id"])
    audio_path = note["audio_path"]
    url = "/api/media/%s" % audio_path

    r_sales2 = client.get(url, headers=hdr("sales2"))
    assert r_sales2.status_code == 403, \
        "C1: a non-owner sales rep (no leads.all) must be denied another rep's voice note"
    assert err_code(r_sales2) == "forbidden"

    r_marketing = client.get(url, headers=hdr("marketing"))
    assert r_marketing.status_code == 403, \
        "N2: marketing has leads.all but only consent.own — must be denied a voice note on " \
        "a lead it does not own, exactly like the business card image"
    assert err_code(r_marketing) == "forbidden"

    r_owner = client.get(url, headers=hdr("sales"))
    assert r_owner.status_code == 200

    r_admin = client.get(url, headers=hdr("admin"))
    assert r_admin.status_code == 200


def test_media_unmapped_or_missing_file_is_404_not_403():
    """Nothing to probe: a URL that never mapped to a real, owned file reads
    exactly like a URL that maps to nothing."""
    fresh()
    r = client.get("/api/media/business_cards/does-not-exist.jpg", headers=hdr("sales2"))
    assert r.status_code == 404
    assert err_code(r) == "not_found"


def test_media_imports_category_gated_to_import_capability_and_uploader():
    """The 'imports' category can hold up to ~25,000 rows of the same class
    of PII as a card image — same leak, larger scale, if left open the way
    business_cards used to be. Two layers now (N7, FABLE-AUDIT-R2.md): the
    import.csv capability (sales has none), AND — since import.csv is
    shared by both marketing and admin — per-uploader ownership, the same
    way lead ownership gates the other two categories. Otherwise any
    marketing/admin login could read ANY import file by uuid, a cross-lead
    PII dump at up to 25,000 rows, the moment a filename leaked any other
    way. Goes through the real POST /api/import/analyze so an `imports` row
    with a real imported_by_user_id actually exists (not just a bare file
    on disk)."""
    fresh()
    r = client.post("/api/import/analyze", headers=hdr("marketing"),
                    files={"file": ("leads.csv", b"name,email\nTaro,taro@example.com\n", "text/csv")})
    assert r.status_code == 200, r.text
    # the analyze() response deliberately never hands back file_path/rel_path
    # (N7) — look the real stored path up the same way get_media does, via
    # the imports table, to build the URL a determined caller would have to
    # reconstruct some other way (this test is not proving that path is
    # discoverable, only that IF a filename is known, access is still gated).
    con = db.connect()
    row = con.execute("SELECT file_path FROM imports WHERE token=?", (r.json()["token"],)).fetchone()
    con.close()
    url = "/api/media/%s" % row["file_path"]

    r_sales = client.get(url, headers=hdr("sales"))
    assert r_sales.status_code == 403, "sales has no import.csv capability"
    assert err_code(r_sales) == "forbidden"

    r_other_marketing = client.get(url, headers=hdr("marketing2"))
    assert r_other_marketing.status_code == 403, \
        "N7: a DIFFERENT marketing login has import.csv but did not upload this file — must be denied"
    assert err_code(r_other_marketing) == "forbidden"

    r_uploader = client.get(url, headers=hdr("marketing"))
    assert r_uploader.status_code == 200, "the marketing user who actually uploaded it must be allowed"

    r_admin = client.get(url, headers=hdr("admin"))
    assert r_admin.status_code == 200, "admin (users.manage) may see any import, same as any lead"


# ── the other half of C1: card_image_path must be redacted, not just gated ──

def test_card_image_path_redacted_on_lead_detail_when_contact_is_redacted():
    """api.py:318 (original) handed card_image_path to marketing even on a
    contact_redacted:true lead — the path was leaked one field before the
    file itself, so gating GET /api/media alone would not have been enough."""
    fresh()
    lead = _scan_card_owned_by("sales")
    assert lead["card_image_path"], "the scanning rep must see their own card path"

    r = client.get("/api/leads/%d" % lead["id"], headers=hdr("marketing"))
    assert r.status_code == 200
    body = r.json()
    assert body["contact_redacted"] is True, "marketing must not own this lead's contact info"
    assert body["card_image_path"] is None, \
        "C1: card_image_path must be redacted exactly when contact is redacted"

    r_owner = client.get("/api/leads/%d" % lead["id"], headers=hdr("sales"))
    assert r_owner.json()["card_image_path"] == lead["card_image_path"], \
        "the owner must still see their own card path"


# ── N1 (FABLE-AUDIT-R2.md, HIGH) — domain lock + sub-identity, server-side ──

def test_n1_non_exceed_domain_token_is_rejected():
    """A validly-signed token whose email is outside EXCEEDBOX_ALLOWED_EMAIL_
    DOMAINS must never reach load_or_provision at all — real 403, no
    auto-provisioned row, no DB touched for that address."""
    fresh()
    token = devauth.mint(sub="n1-outsider-sub", email="attacker@evil.example")
    r = client.get("/api/me", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 403, \
        "N1: a token from a domain outside the allow-list must be rejected server-side"
    assert err_code(r) == "domain_not_allowed"

    con = db.connect()
    row = con.execute("SELECT 1 FROM app_user WHERE email=?", ("attacker@evil.example",)).fetchone()
    con.close()
    assert row is None, "N1: a rejected-domain login must not even auto-provision an inactive row"


def test_n1_sub_mismatch_cannot_take_over_an_existing_account():
    """The account-takeover shape N1 closes: a first, legitimate login binds
    sales's row to sub S1. A second token, bearing sales's EXACT email but a
    DIFFERENT sub (S2) — e.g. an attacker who obtained a validly-signed
    token with a spoofed/duplicated email claim — must be rejected outright,
    never silently adopted onto sales's active account and role."""
    fresh()
    real_sub = "n1-sales-real-sub"
    r1 = client.get("/api/me", headers={"Authorization": "Bearer " +
                    devauth.mint(sub=real_sub, email=EMAILS["sales"])})
    assert r1.status_code == 200, r1.text
    assert r1.json()["role"] == "sales"

    con = db.connect()
    bound = con.execute("SELECT supabase_uid FROM app_user WHERE email=?",
                        (EMAILS["sales"],)).fetchone()
    assert bound["supabase_uid"] == real_sub, "the first real login must have bound sub -> row"
    con.close()

    attacker_sub = "n1-attacker-different-sub"
    r2 = client.get("/api/me", headers={"Authorization": "Bearer " +
                    devauth.mint(sub=attacker_sub, email=EMAILS["sales"])})
    assert r2.status_code == 403, \
        "N1: a different sub bearing the same email must not take over the bound account"
    assert err_code(r2) == "account_conflict"

    con = db.connect()
    still_bound = con.execute("SELECT supabase_uid FROM app_user WHERE email=?",
                              (EMAILS["sales"],)).fetchone()
    con.close()
    assert still_bound["supabase_uid"] == real_sub, \
        "N1: the account must still belong to the real sub after the attempted takeover"


def test_n1_first_login_still_adopts_a_never_bound_row_and_is_audited():
    """The legitimate path N1 must NOT break: a row that has never had a
    supabase_uid (created by seed.py/POST /api/users before the person's
    first real login) still adopts the incoming sub on first sign-in, and
    that adoption is attributable in audit_log — not silent."""
    fresh()
    sub = "n1-first-login-sub"
    r = client.get("/api/me", headers={"Authorization": "Bearer " +
                   devauth.mint(sub=sub, email=EMAILS["admin"])})
    assert r.status_code == 200, r.text

    con = db.connect()
    row = con.execute("SELECT id, supabase_uid FROM app_user WHERE email=?",
                      (EMAILS["admin"],)).fetchone()
    assert row["supabase_uid"] == sub
    logged = con.execute(
        "SELECT * FROM audit_log WHERE action='adopt_supabase_uid' AND entity_id=?",
        (row["id"],)).fetchone()
    con.close()
    assert logged is not None, "N1: adopting a sub onto an existing row must be recorded in audit_log"


# ── N8 (FABLE-AUDIT-R2.md, LOW) — `aud` is actually verified now ────────────

def test_n8_token_with_wrong_audience_is_rejected():
    """verify_aud used to be False — a token minted for a different
    audience/project sharing the same HS256 secret would have been accepted
    regardless. Now aud is checked like every other JWT claim."""
    fresh()
    import jwt as pyjwt
    from app import devauth as devauth_mod
    bad = pyjwt.encode(
        {"sub": "n8-wrong-aud-sub", "email": EMAILS["admin"], "aud": "some-other-project",
         "iss": devauth_mod.DEV_ISSUER, "iat": 0, "exp": 9999999999},
        devauth_mod.DEV_SECRET, algorithm="HS256")
    r = client.get("/api/me", headers={"Authorization": "Bearer " + bad})
    assert r.status_code == 401, "N8: a token minted for a different audience must be rejected"
    assert err_code(r) == "invalid_token"


# ── revert-and-fail proof lives in the report, not in a committed test —
# see FABLE-AUDIT-FIX-REPORT (this session's final message): the get_media
# access checks were manually reverted, this file was re-run and the first
# three tests above failed exactly as C1 describes (200 instead of 403),
# then the fix was restored and this file was re-run green again. ──────────


if __name__ == "__main__":
    import traceback
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print("  PASS  %s" % name)
            passed += 1
        except Exception:
            print("  FAIL  %s" % name)
            traceback.print_exc()
            failed += 1
    print("\n%d passed, %d failed, %d total" % (passed, failed, len(fns)))
