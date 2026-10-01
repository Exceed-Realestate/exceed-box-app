"""Voice-memo transcription.

The Today screen offers a voice memo so a rep can leave a note without typing —
the whole "入力ではなく、行動を通知" idea from the proposal. The recording and
storage worked; the transcript never did, and the screen said so.

Same contract as the mailer and the reply reader: `EXCEEDBOX_TRANSCRIBE_PROVIDER`
defaults to `off`, no key is a refusal rather than a silent fallback, and a
provider failure leaves the memo playable with an honest "not transcribed"
rather than an empty transcript that looks like silence.

Whisper is the default provider because the audio is short, the languages are
Japanese and English mixed, and the cost is per minute of audio rather than per
seat. Nothing here uploads anything unless a key is deliberately configured.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("exceedbox.voice")

PROVIDER = (os.environ.get("EXCEEDBOX_TRANSCRIBE_PROVIDER") or "off").strip().lower()
API_KEY = os.environ.get("OPENAI_API_KEY") or ""
MODEL = os.environ.get("EXCEEDBOX_TRANSCRIBE_MODEL") or "whisper-1"
TIMEOUT = int(os.environ.get("EXCEEDBOX_TRANSCRIBE_TIMEOUT", "120"))
MAX_BYTES = int(os.environ.get("EXCEEDBOX_TRANSCRIBE_MAX_BYTES", str(24 * 1024 * 1024)))

ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"


class TranscribeUnavailable(RuntimeError):
    pass


def describe() -> dict:
    d = {"provider": PROVIDER, "model": MODEL if PROVIDER != "off" else None,
         "enabled": PROVIDER != "off"}
    problems = []
    if PROVIDER == "whisper" and not API_KEY:
        problems.append("OPENAI_API_KEY missing")
    if PROVIDER not in ("off", "whisper"):
        problems.append("unknown EXCEEDBOX_TRANSCRIBE_PROVIDER: %r" % PROVIDER)
    d["problems"] = problems
    d["ready"] = (PROVIDER != "off") and not problems
    d["note"] = ("Voice memos are recorded and stored, not transcribed."
                 if PROVIDER == "off" else None)
    return d


def _multipart(fields: dict, filename: str, data: bytes) -> tuple:
    boundary = "----exceedbox%s" % uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode())
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                % (boundary, filename)).encode())
    out.append(data)
    out.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


def transcribe(data: bytes, filename: str = "memo.m4a", language: str = None) -> dict:
    """Returns {'text', 'model', 'language'} or raises TranscribeUnavailable.

    `language` is left unset by default on purpose: a Tokyo rep's memo mixes
    Japanese and English constantly, and pinning it to one makes the other
    come out as nonsense rather than as a second language.
    """
    if PROVIDER == "off":
        raise TranscribeUnavailable("transcription is switched off")
    if PROVIDER != "whisper":
        raise TranscribeUnavailable("unknown provider %r" % PROVIDER)
    if not API_KEY:
        raise TranscribeUnavailable("OPENAI_API_KEY is not set")
    if not data:
        raise TranscribeUnavailable("empty recording")
    if len(data) > MAX_BYTES:
        raise TranscribeUnavailable("recording is larger than the provider limit")

    fields = {"model": MODEL, "response_format": "json"}
    if language:
        fields["language"] = language
    body, content_type = _multipart(fields, filename, data)
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Authorization": "Bearer %s" % API_KEY, "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise TranscribeUnavailable(
            "provider %s: %s" % (e.code, (e.read() or b"")[:200].decode("utf-8", "replace")))
    except Exception as e:
        raise TranscribeUnavailable("provider unreachable: %s" % e)
    text = (out.get("text") or "").strip()
    if not text:
        raise TranscribeUnavailable("provider returned no text")
    return {"text": text, "model": MODEL, "language": out.get("language")}
