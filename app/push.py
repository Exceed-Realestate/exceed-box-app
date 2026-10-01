"""Device push notifications.

The premise of the Today screen is that a rep is TOLD what to do rather than
asked to go looking. A notification that only exists inside the app, which they
have to open to see, is not that.

`EXCEEDBOX_PUSH_PROVIDER` defaults to `off`. With it off, a notification still
becomes a task and an in-app alert — which is what happens today — and the
Integrations screen says plainly that no device push is being sent, rather than
showing a green tick for a delivery that never happened. That distinction is the
whole reason this module exists: the previous code accepted a token, created a
task, and returned success.

Expo is the default provider because the app is an Expo build and its push
service takes the tokens the client already registers, with no APNs certificate
handling of our own. Swapping to raw APNs later changes this file only.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from . import db

log = logging.getLogger("exceedbox.push")

PROVIDER = (os.environ.get("EXCEEDBOX_PUSH_PROVIDER") or "off").strip().lower()
EXPO_ENDPOINT = "https://exp.host/--/api/v2/push/send"
EXPO_TOKEN = os.environ.get("EXPO_ACCESS_TOKEN") or ""
TIMEOUT = int(os.environ.get("EXCEEDBOX_PUSH_TIMEOUT", "20"))


class PushUnavailable(RuntimeError):
    pass


def describe() -> dict:
    d = {"provider": PROVIDER, "enabled": PROVIDER != "off"}
    problems = []
    if PROVIDER not in ("off", "expo"):
        problems.append("unknown EXCEEDBOX_PUSH_PROVIDER: %r" % PROVIDER)
    d["problems"] = problems
    d["ready"] = (PROVIDER != "off") and not problems
    d["note"] = ("Alerts appear in the app only; no device push is sent."
                 if PROVIDER == "off" else None)
    return d


def tokens_for(con, user_id: str) -> list:
    return [r["token"] for r in db.all_(
        con, "SELECT token FROM push_tokens WHERE user_id=?", (user_id,))]


def send(con, *, user_id: str, title: str, body: str, data: dict = None) -> dict:
    """Returns a report of what actually happened, never a bare success.

    Every caller records this verbatim, so "did the rep get told?" is answerable
    from the database rather than assumed. A device that has uninstalled the app
    reports DeviceNotRegistered, and that token is removed — otherwise a stale
    token quietly fails for ever and the counts look fine.
    """
    toks = tokens_for(con, user_id)

    # Report EVERY blocker, not just the first one found. "push provider is off"
    # and "no registered device" have different owners — one is an operator's
    # configuration, the other is the rep never having opened the app — and
    # someone debugging why a notification did not arrive needs both.
    blockers = []
    if PROVIDER == "off":
        blockers.append("push provider is off")
    elif PROVIDER != "expo":
        blockers.append("unknown provider %r" % PROVIDER)
    if not toks:
        blockers.append("no registered device")
    if blockers:
        return {"sent": 0, "delivered": False, "reason": "; ".join(blockers),
                "tokens": len(toks)}

    messages = [{"to": t, "title": title, "body": body, "data": data or {}}
                for t in toks]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if EXPO_TOKEN:
        headers["Authorization"] = "Bearer %s" % EXPO_TOKEN
    req = urllib.request.Request(EXPO_ENDPOINT, data=json.dumps(messages).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"sent": 0, "delivered": False, "tokens": len(toks),
                "reason": "provider %s: %s" % (
                    e.code, (e.read() or b"")[:160].decode("utf-8", "replace"))}
    except Exception as e:
        return {"sent": 0, "delivered": False, "tokens": len(toks),
                "reason": "provider unreachable: %s" % e}

    tickets = out.get("data") or []
    ok = 0
    for tok, ticket in zip(toks, tickets):
        if isinstance(ticket, dict) and ticket.get("status") == "ok":
            ok += 1
            continue
        detail = (ticket or {}).get("details", {}) if isinstance(ticket, dict) else {}
        if detail.get("error") == "DeviceNotRegistered":
            con.execute("DELETE FROM push_tokens WHERE token=?", (tok,))
            con.commit()
            log.info("removed a token for a device that no longer has the app")
    return {"sent": ok, "delivered": ok > 0, "tokens": len(toks),
            "reason": None if ok else "provider accepted none of the tokens"}
