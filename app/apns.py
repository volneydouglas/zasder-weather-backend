"""APNs push — token-based (.p8 / ES256) auth.

Sends alert pushes to registered iOS devices using a JWT signed with the APNs
Auth Key, over HTTP/2 to Apple. Best-effort: disabled unless apns_configured;
failures are logged and dead tokens (410 / BadDeviceToken) are pruned. Wired
into the alert monitor alongside email so a device-down alert can also push.

The .p8 is an EC P-256 private key; PyJWT[crypto] signs the provider JWT.
"""
import logging
import time

import httpx
import jwt

from . import db
from .config import settings

log = logging.getLogger("apns")

_HOSTS = {
    "sandbox": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}

# APNs accepts a provider JWT for up to 1h; refresh well before that.
_jwt_cache: tuple[str, float] | None = None
_JWT_TTL = 50 * 60


def make_jwt(team_id: str, key_id: str, key_p8: str, now: float | None = None) -> str:
    """ES256 provider JWT for APNs. Pure — unit-tested with an ephemeral key."""
    return jwt.encode(
        {"iss": team_id, "iat": int(now or time.time())},
        key_p8, algorithm="ES256", headers={"kid": key_id},
    )


def _provider_jwt() -> str:
    global _jwt_cache
    now = time.time()
    if _jwt_cache and now - _jwt_cache[1] < _JWT_TTL:
        return _jwt_cache[0]
    tok = make_jwt(settings.apns_team_id, settings.apns_key_id, settings.apns_key_p8, now)
    _jwt_cache = (tok, now)
    return tok


def build_payload(title: str, body: str) -> dict:
    """Standard alert aps payload."""
    return {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}


async def send_to_all(title: str, body: str) -> dict:
    """Push to every registered token (each to the host matching its build env).
    Prunes dead tokens. Returns a summary dict. No-op if APNs isn't configured."""
    if not settings.apns_configured:
        return {"sent": 0, "skipped": "apns not configured"}
    tokens = await db.list_push_tokens()
    if not tokens:
        return {"sent": 0, "pruned": 0, "total": 0}
    payload = build_payload(title, body)
    headers = {
        "authorization": f"bearer {_provider_jwt()}",
        "apns-topic": settings.apns_topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    sent = pruned = failed = 0
    async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
        for t in tokens:
            tok = t["token"]
            host = _HOSTS.get(t.get("env") or settings.apns_env, _HOSTS["sandbox"])
            try:
                r = await client.post(f"{host}/3/device/{tok}", headers=headers, json=payload)
            except Exception as e:
                failed += 1
                log.warning("apns post failed for %s…: %s", tok[:8], e)
                continue
            if r.status_code == 200:
                sent += 1
                continue
            reason = ""
            try:
                reason = r.json().get("reason", "")
            except Exception:
                pass
            if r.status_code == 410 or reason in (
                    "BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                await db.remove_push_token(tok)
                pruned += 1
                log.info("pruned dead token %s… (%s %s)", tok[:8], r.status_code, reason)
            else:
                failed += 1
                log.warning("apns %s for %s…: %s", r.status_code, tok[:8], reason or r.text[:120])
    return {"sent": sent, "pruned": pruned, "failed": failed, "total": len(tokens)}
