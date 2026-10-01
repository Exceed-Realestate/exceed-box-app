"""File storage for uploads (SPEC-V2 §6 business-card images, §10 voice
notes, §7 CSV imports).

Two backends behind one interface:

  * **local** — files under `data/uploads/<category>/`. What the test suite and
    a laptop use. Selected when no Supabase service key is configured.
  * **supabase** — a PRIVATE Supabase Storage bucket. What a deployed instance
    uses. Selected automatically when SUPABASE_URL and SUPABASE_SECRET_KEY are
    both present, or forced with EXCEEDBOX_MEDIA_BACKEND.

Why this exists: a business-card photo is a person's name, company, phone,
email and address in one file. FABLE-AUDIT round 1 found `GET /api/media/…`
gated only on "any active login", so any rep could read any other rep's cards.
That hole is closed in api.py, and this module must not reopen it.

**The bucket is private and stays private.** Bytes are fetched server-side with
the service key and re-served by FastAPI *after* the same owner check that
guards the lead. No public URL, and deliberately no signed URL: a signed URL is
a bearer token for one file that outlives the permission check that minted it,
and it would travel through logs, referrers and chat history. The extra hop
costs a few hundred milliseconds on an internal tool and keeps exactly one
place — `api.py` — deciding who may see a customer's face.

Callers never learn which backend is live. They get a relative path back from
`save()` and hand it to `read()`. `path_for()` still exists for the local
backend only and raises on Supabase, so any code path that assumes a real file
on disk fails loudly instead of silently reading nothing.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("exceedbox.media")

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_ROOT = Path(os.environ.get("EXCEEDBOX_UPLOAD_ROOT", ROOT / "data" / "uploads"))

CATEGORIES = ("business_cards", "voice_notes", "imports")

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY") or ""
BUCKET = os.environ.get("EXCEEDBOX_MEDIA_BUCKET", "exceedbox-media")

_forced = (os.environ.get("EXCEEDBOX_MEDIA_BACKEND") or "").strip().lower()
if _forced in ("local", "supabase"):
    BACKEND = _forced
else:
    BACKEND = "supabase" if (SUPABASE_URL and SUPABASE_SECRET_KEY) else "local"

if BACKEND == "supabase" and not (SUPABASE_URL and SUPABASE_SECRET_KEY):
    raise RuntimeError(
        "EXCEEDBOX_MEDIA_BACKEND=supabase needs SUPABASE_URL and SUPABASE_SECRET_KEY")


class MediaError(RuntimeError):
    pass


# ── naming ───────────────────────────────────────────────────────────────────

def _safe_ext(filename: str) -> str:
    """Keep the extension, never the original filename — it routinely contains
    a real person's name, and it would end up in a URL."""
    ext = Path(filename or "").suffix.lower()
    if len(ext) > 10 or "/" in ext or "\\" in ext:
        return ""
    return ext


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError("unknown upload category: %s" % category)


# ── supabase storage ─────────────────────────────────────────────────────────

def _storage_request(method: str, path: str, *, data: bytes = None,
                     content_type: str = None, timeout: int = 60):
    url = "%s/storage/v1%s" % (SUPABASE_URL, path)
    headers = {
        "Authorization": "Bearer %s" % SUPABASE_SECRET_KEY,
        "apikey": SUPABASE_SECRET_KEY,
    }
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                       # DNS, TLS, timeout
        raise MediaError("storage unreachable: %s" % e)


def ensure_bucket() -> str:
    """Create the private bucket if it is absent. Idempotent, safe to call at
    startup. Returns 'created' or 'exists'."""
    status, body = _storage_request("GET", "/bucket/%s" % urllib.parse.quote(BUCKET))
    if status == 200:
        try:
            if json.loads(body).get("public"):
                log.error("Storage bucket %r is PUBLIC — customer business-card "
                          "photos would be readable by anyone with the URL.", BUCKET)
        except Exception:
            pass
        return "exists"
    status, body = _storage_request(
        "POST", "/bucket",
        data=json.dumps({"name": BUCKET, "id": BUCKET, "public": False}).encode(),
        content_type="application/json")
    if status in (200, 201):
        return "created"
    if status == 409:
        return "exists"
    raise MediaError("could not create bucket %r: %s %s" % (BUCKET, status, body[:200]))


# ── the interface callers use ────────────────────────────────────────────────

def save(category: str, filename: str, data: bytes) -> str:
    """Store `data` under a fresh uuid4 name. Returns the relative path that
    goes in the database — never an absolute path, which would break the day
    this moves host, and never the caller's filename."""
    _check_category(category)
    rel = "%s/%s%s" % (category, uuid.uuid4().hex, _safe_ext(filename))

    if BACKEND == "local":
        dest = UPLOAD_ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return rel

    status, body = _storage_request(
        "POST", "/object/%s/%s" % (urllib.parse.quote(BUCKET), rel),
        data=data, content_type="application/octet-stream")
    if status not in (200, 201):
        raise MediaError("upload failed: %s %s" % (status, body[:200]))
    return rel


def read(relative_path: str) -> bytes:
    """Fetch the bytes back. Backend-agnostic — this is what api.py serves,
    AFTER its own owner check."""
    rel = _normalise(relative_path)
    if BACKEND == "local":
        return path_for(rel).read_bytes()
    status, body = _storage_request(
        "GET", "/object/%s/%s" % (urllib.parse.quote(BUCKET), rel))
    if status == 200:
        return body
    if status in (400, 404):
        raise FileNotFoundError(rel)
    raise MediaError("download failed: %s %s" % (status, body[:200]))


def exists(relative_path: str) -> bool:
    try:
        rel = _normalise(relative_path)
    except ValueError:
        return False
    if BACKEND == "local":
        return (UPLOAD_ROOT / rel).is_file()
    status, _ = _storage_request(
        "GET", "/object/info/%s/%s" % (urllib.parse.quote(BUCKET), rel))
    return status == 200


def delete(relative_path: str) -> bool:
    rel = _normalise(relative_path)
    if BACKEND == "local":
        p = UPLOAD_ROOT / rel
        if p.is_file():
            p.unlink()
            return True
        return False
    status, _ = _storage_request(
        "DELETE", "/object/%s/%s" % (urllib.parse.quote(BUCKET), rel))
    return status == 200


def _normalise(relative_path: str) -> str:
    """Reject anything that is not `<known category>/<flat name>`.

    Both backends need this. On local it stops `../../etc/passwd`; on Supabase
    it stops a stored path being coaxed into another bucket's namespace. The
    check is on the shape of the stored value, so a corrupted database row
    cannot turn into a file read either.
    """
    rel = str(relative_path or "").strip().lstrip("/")
    parts = rel.split("/")
    if len(parts) != 2 or not parts[1] or parts[1] in (".", ".."):
        raise ValueError("bad media path: %r" % relative_path)
    _check_category(parts[0])
    if "\\" in rel or ".." in parts[1]:
        raise ValueError("bad media path: %r" % relative_path)
    return rel


def path_for(relative_path: str) -> Path:
    """Local backend only. Raises on Supabase rather than returning a path that
    does not exist — a caller that needs a real file must be changed to use
    `read()`, and should fail loudly until it is."""
    if BACKEND != "local":
        raise MediaError(
            "path_for() is local-only; this instance stores media in Supabase "
            "Storage. Use media.read() instead.")
    rel = _normalise(relative_path)
    p = (UPLOAD_ROOT / rel).resolve()
    root = UPLOAD_ROOT.resolve()
    if root not in p.parents and p != root:
        raise ValueError("path escapes upload root")
    return p


def describe() -> dict:
    """For /api/health and the Integrations screen — the true state, so a
    screen never claims storage is healthy when it is not configured."""
    d = {"backend": BACKEND}
    if BACKEND == "local":
        d["root"] = str(UPLOAD_ROOT)
    else:
        d["bucket"] = BUCKET
        d["url"] = SUPABASE_URL
    return d
