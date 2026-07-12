"""Firebase Cloud Messaging (FCM) sender — Android push, HTTP v1 API.

The Android app registers its FCM registration token at /api/push/register
with platform="android"; the alert monitor then delivers through here in
parallel with APNs for iOS.

Auth is a Google service-account OAuth2 flow (no new deps — PyJWT[crypto] +
httpx are already required):
  1. Sign a short-lived JWT with the service-account private key (RS256),
     scoped to firebase.messaging.
  2. Exchange it at the token URI for a ~1h bearer access token (cached).
  3. POST each message to
     https://fcm.googleapis.com/v1/projects/<project>/messages:send

Config (Fly secrets):
  FCM_SERVICE_ACCOUNT_JSON  — the full service-account key JSON (string), OR
  FCM_SERVICE_ACCOUNT_FILE  — path to that JSON on disk.
project_id is read from the key JSON. Unset ⇒ FCM disabled (no-op).
"""

import json
import logging
import os
import time
from typing import Any

import httpx
import jwt

log = logging.getLogger("fcm")

_TOKEN_URI_DEFAULT = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Cached OAuth2 access token: (token, expiry_epoch)
_access: tuple[str, float] | None = None


def _service_account() -> dict | None:
    raw = os.environ.get("FCM_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        path = os.environ.get("FCM_SERVICE_ACCOUNT_FILE", "").strip()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = f.read()
    if not raw:
        return None
    try:
        sa = json.loads(raw)
    except json.JSONDecodeError:
        log.error("FCM service account JSON is not valid JSON")
        return None
    if not (sa.get("client_email") and sa.get("private_key") and sa.get("project_id")):
        log.error("FCM service account JSON missing client_email/private_key/project_id")
        return None
    return sa


def fcm_configured() -> bool:
    return _service_account() is not None


async def _access_token(sa: dict) -> str | None:
    global _access
    now = time.time()
    if _access and _access[1] - 60 > now:
        return _access[0]
    token_uri = sa.get("token_uri") or _TOKEN_URI_DEFAULT
    claim = {
        "iss": sa["client_email"],
        "scope": _SCOPE,
        "aud": token_uri,
        "iat": int(now),
        "exp": int(now) + 3600,
    }
    assertion = jwt.encode(claim, sa["private_key"], algorithm="RS256")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(token_uri, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            })
    except Exception as e:  # noqa: BLE001
        log.error("FCM token exchange failed: %s", e)
        return None
    if resp.status_code != 200:
        log.error("FCM token exchange HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    tok = data.get("access_token")
    if not tok:
        return None
    _access = (tok, now + float(data.get("expires_in", 3600)))
    return tok


async def push_tokens_fcm(tokens: list[str], title: str, body: str) -> dict[str, Any]:
    """Send a notification to each FCM token. Returns {sent, dead, failed}.
    `dead` are tokens FCM reports as unregistered/invalid (caller prunes)."""
    sa = _service_account()
    if sa is None:
        return {"sent": 0, "dead": [], "failed": 0, "skipped": "fcm not configured"}
    access = await _access_token(sa)
    if not access:
        return {"sent": 0, "dead": [], "failed": len(tokens)}

    url = f"https://fcm.googleapis.com/v1/projects/{sa['project_id']}/messages:send"
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
    sent, failed, dead = 0, 0, []
    async with httpx.AsyncClient(timeout=10) as client:
        for token in tokens:
            payload = {"message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "android": {"priority": "high"},
            }}
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except Exception as e:  # noqa: BLE001
                log.warning("FCM send error: %s", e)
                failed += 1
                continue
            if resp.status_code == 200:
                sent += 1
            elif resp.status_code in (400, 403, 404):
                # UNREGISTERED / invalid token → prune. Other 400s (bad payload)
                # are our bug, not a dead token, but pruning a persistently
                # rejected token is safe since it can't receive anyway.
                errcode = ""
                try:
                    errcode = resp.json().get("error", {}).get("status", "")
                except Exception:  # noqa: BLE001
                    pass
                if errcode in ("UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND") or resp.status_code == 404:
                    dead.append(token)
                else:
                    failed += 1
                log.info("FCM %s for token …%s (%s)", resp.status_code, token[-8:], errcode)
            else:
                failed += 1
                log.warning("FCM HTTP %s: %s", resp.status_code, resp.text[:160])
    return {"sent": sent, "dead": dead, "failed": failed}
