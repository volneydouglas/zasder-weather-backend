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


async def _push_tokens(tokens: list[dict], title: str, body: str) -> dict:
    """Sign with the local APNs key and POST to Apple for each token. `tokens`
    is a list of {token, env?} dicts. Returns {sent, dead, failed} where `dead`
    lists tokens Apple says are gone (caller prunes). Does NOT touch the DB —
    shared by send_to_all (own-key path) and the hosted relay."""
    payload = build_payload(title, body)
    headers = {
        "authorization": f"bearer {_provider_jwt()}",
        "apns-topic": settings.apns_topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    sent = failed = 0
    dead: list[str] = []
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
                dead.append(tok)
                log.info("dead token %s… (%s %s)", tok[:8], r.status_code, reason)
            else:
                failed += 1
                log.warning("apns %s for %s…: %s", r.status_code, tok[:8], reason or r.text[:120])
    return {"sent": sent, "dead": dead, "failed": failed}


async def _push_via_relay(tokens: list[str], title: str, body: str,
                          url: str, token: str) -> dict:
    """Send through a shared relay instead of signing locally. For self-hosters
    who don't run their own APNs key: the relay holds the key, fans out to
    Apple, and returns dead tokens for us to prune. POSTs only {tokens, title,
    body, env} — the relay enforces that shape."""
    env = settings.apns_env if settings.apns_env in ("sandbox", "production") else "production"
    payload = {"tokens": tokens, "title": title, "body": body, "env": env}
    headers = {"authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, headers=headers, json=payload)
    except Exception as e:
        log.warning("relay push failed: %s", e)
        return {"sent": 0, "dead": [], "failed": len(tokens)}
    if r.status_code != 200:
        log.warning("relay push %s: %s", r.status_code, r.text[:200])
        return {"sent": 0, "dead": [], "failed": len(tokens)}
    data = r.json()
    return {"sent": data.get("sent", 0), "dead": data.get("dead", []),
            "failed": data.get("failed", 0)}


async def effective_relay() -> tuple[str | None, str | None]:
    """Resolve the relay (url, token) DB-over-env — the app-managed config wins
    over env defaults, mirroring how SMTP is resolved for email alerts."""
    cfg = await db.get_push_relay() or {}
    url = cfg.get("url") or settings.apns_relay_url
    token = cfg.get("token") or settings.apns_relay_token
    return url, token


async def push_configured() -> bool:
    """True if push can deliver — a local APNs key OR a resolved relay."""
    if settings.apns_configured:
        return True
    url, token = await effective_relay()
    return bool(url and token)


async def send_to_all(title: str, body: str) -> dict:
    """Push to every registered token. Prefers a local APNs key (most direct);
    falls back to a hosted relay if one is configured (env or app-managed).
    Prunes dead tokens. No-op if neither push path is configured."""
    own = settings.apns_configured
    relay_url, relay_token = await effective_relay()
    relay = bool(relay_url and relay_token)
    if not (own or relay):
        return {"sent": 0, "skipped": "apns not configured"}
    tokens = await db.list_push_tokens()
    if not tokens:
        return {"sent": 0, "pruned": 0, "total": 0}
    if own:
        res = await _push_tokens(tokens, title, body)
    else:
        res = await _push_via_relay([t["token"] for t in tokens], title, body,
                                    relay_url, relay_token)  # type: ignore[arg-type]
    pruned = 0
    for tok in res.get("dead", []):
        await db.remove_push_token(tok)
        pruned += 1
    return {"sent": res.get("sent", 0), "pruned": pruned,
            "failed": res.get("failed", 0), "total": len(tokens)}
