"""Media storage: path safety on both backends, and the Supabase round trip.

The path-safety tests run everywhere. The Supabase tests are skipped unless
SUPABASE_URL and SUPABASE_SECRET_KEY are set, the same opt-in shape the Postgres
integration file uses.

Why the path tests matter as much as the round trip: a business-card photo is a
person's name, company, phone, email and address in one file. `_normalise()` is
what stops a stored path from being coaxed into reading something else, and it
now has to hold on a remote object store as well as on local disk, where the
old `..`-resolution guard no longer applies because there is no filesystem.
"""
from __future__ import annotations

import importlib
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import media  # noqa: E402

# conftest.py strips the real Supabase variables so the offline suite cannot
# accidentally reach the live project, but preserves them under EXCEEDBOX_TEST_*
# precisely so this file can opt back in. Reading only SUPABASE_URL here would
# make the live tests skip silently, which reads as a pass.
_LIVE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("EXCEEDBOX_TEST_SUPABASE_URL")
_LIVE_KEY = (os.environ.get("SUPABASE_SECRET_KEY")
             or os.environ.get("EXCEEDBOX_TEST_SUPABASE_SECRET_KEY"))
LIVE = bool(_LIVE_URL and _LIVE_KEY)


def _live_media():
    """app.media resolves its backend at import; restore the real credentials
    and reload so the remote backend is genuinely exercised."""
    os.environ["SUPABASE_URL"] = _LIVE_URL
    os.environ["SUPABASE_SECRET_KEY"] = _LIVE_KEY
    return importlib.reload(media)


# ── path safety (both backends, no network) ──────────────────────────────────

@pytest.mark.parametrize("bad", [
    "business_cards/../../etc/passwd",
    "business_cards/..",
    "business_cards/.",
    "../business_cards/x.jpg",
    "business_cards/sub/dir.jpg",
    "business_cards/",
    "business_cards",
    "",
    "secrets/x.jpg",             # not a known category
    "voice_notes/a\\b.m4a",      # backslash
])
def test_bad_paths_are_refused(bad):
    with pytest.raises(ValueError):
        media._normalise(bad)


@pytest.mark.parametrize("good", [
    "business_cards/abc123.jpg",
    "voice_notes/deadbeef.m4a",
    "imports/0123456789abcdef.xlsx",
])
def test_good_paths_pass(good):
    assert media._normalise(good) == good


def test_original_filename_never_survives():
    """The filename routinely contains a real person's name and would end up in
    a URL. Only the extension is kept."""
    ext = media._safe_ext("Morita Kenji business card FINAL.JPG")
    assert ext == ".jpg"
    assert media._safe_ext("no-extension") == ""
    assert media._safe_ext("x" + ".averyveryverylongextension") == ""
    assert media._safe_ext("a/b.jpg") == ".jpg"


def test_unknown_category_refused_on_save():
    with pytest.raises(ValueError):
        media.save("secrets", "x.jpg", b"x")


# ── local backend ────────────────────────────────────────────────────────────

def test_local_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "BACKEND", "local")
    monkeypatch.setattr(media, "UPLOAD_ROOT", tmp_path)
    rel = media.save("voice_notes", "memo.m4a", b"audio-bytes")
    assert rel.startswith("voice_notes/")
    assert media.exists(rel)
    assert media.read(rel) == b"audio-bytes"
    assert media.path_for(rel).is_file()
    assert media.delete(rel) is True
    assert media.exists(rel) is False


def test_path_for_raises_on_supabase_backend(monkeypatch):
    """A caller that assumes a real file must fail loudly, not read nothing."""
    monkeypatch.setattr(media, "BACKEND", "supabase")
    with pytest.raises(media.MediaError, match="local-only"):
        media.path_for("business_cards/x.jpg")


# ── supabase backend (opt-in) ────────────────────────────────────────────────

pytestmark_live = pytest.mark.skipif(
    not LIVE, reason="SUPABASE_URL/SUPABASE_SECRET_KEY not set — storage tests are opt-in")


@pytestmark_live
def test_supabase_round_trip_and_cleanup():
    mod = _live_media()
    assert mod.BACKEND == "supabase", "expected the supabase backend with keys set"
    assert mod.ensure_bucket() in ("created", "exists")

    payload = ("probe-%s" % uuid.uuid4().hex).encode()
    rel = mod.save("business_cards", "probe.jpg", payload)
    try:
        assert mod.exists(rel)
        assert mod.read(rel) == payload
    finally:
        assert mod.delete(rel) is True
    assert mod.exists(rel) is False
    with pytest.raises(FileNotFoundError):
        mod.read(rel)


@pytestmark_live
def test_supabase_bucket_is_private():
    """The whole access model rests on this. If the bucket were public, every
    customer business card would be readable by anyone holding a URL, and the
    owner checks in api.py would be decoration."""
    import json
    import urllib.request

    mod = _live_media()
    status, body = mod._storage_request("GET", "/bucket/%s" % mod.BUCKET)
    assert status == 200, body[:200]
    assert json.loads(body).get("public") is False, "bucket must not be public"

    payload = b"private-bytes-%s" % uuid.uuid4().hex.encode()
    rel = mod.save("business_cards", "priv.jpg", payload)
    try:
        url = "%s/storage/v1/object/public/%s/%s" % (mod.SUPABASE_URL, mod.BUCKET, rel)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                got = r.read()
            assert got != payload, "PUBLIC URL RETURNED THE FILE — bucket is not private"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 401, 403, 404), "unexpected status %s" % e.code
    finally:
        mod.delete(rel)


def teardown_module(_):
    """Put app.media back on the local backend for whatever runs next."""
    for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY"):
        os.environ.pop(k, None)
    importlib.reload(media)
