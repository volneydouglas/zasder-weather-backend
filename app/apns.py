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
    # `now is None`, not `now or ...`: now=0 is a legitimate pinned iat (epoch)
    # and the falsy-default form silently substituted wall-clock time for it.
    return jwt.encode(
        {"iss": team_id, "iat": int(time.time() if now is None else now)},
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


def build_live_activity_start(attributes_type: str, attributes: dict,
                              content_state: dict, title: str, body: str,
                              now_s: int, stale_s: int, dismiss_s: int) -> dict:
    """ActivityKit push-to-start payload (1.7, nowcast phase 2). Start-only
    by design: the Activity's countdown renders client-side from the epoch
    in content-state (Text(timerInterval:)), and stale/dismissal dates make
    it expire on its own — so one push runs the whole lifecycle with no
    per-activity update-token round trip."""
    return {"aps": {
        "timestamp": now_s,
        "event": "start",
        "content-state": content_state,
        "attributes-type": attributes_type,
        "attributes": attributes,
        "alert": {"title": title, "body": body},
        "stale-date": stale_s,
        "dismissal-date": dismiss_s,
    }}


def build_live_activity_update(content_state: dict, now_s: int,
                                event: str = "update",
                                stale_s: int | None = None,
                                dismiss_s: int | None = None,
                                alert: tuple[str, str] | None = None) -> dict:
    """ActivityKit update/end payload (1.8, Storm Watch / Heat Day). Unlike
    the one-shot start above, these Activities track live station numbers,
    so the server pushes fresh content-state to each activity's own update
    token. `alert` (title, body) makes an update audible — used only for
    the final "storm ended" beat; routine number refreshes stay silent."""
    aps: dict = {"timestamp": now_s, "event": event,
                 "content-state": content_state}
    if stale_s is not None:
        aps["stale-date"] = stale_s
    if dismiss_s is not None:
        aps["dismissal-date"] = dismiss_s
    if alert is not None:
        aps["alert"] = {"title": alert[0], "body": alert[1]}
    return {"aps": aps}


def _resolve_env(t: dict) -> tuple[str | None, bool]:
    """(env, came_from_the_token) for one token row.

    env is None when neither the token nor APNS_ENV names a host we know —
    the caller must then refuse to send rather than default to a host."""
    stored = (t.get("env") or "").strip()
    if stored in _HOSTS:
        return stored, True
    configured = (settings.apns_env or "").strip()
    if configured in _HOSTS:
        return configured, False
    return None, False


async def _push_tokens(tokens: list[dict], title: str, body: str,
                       payload: dict | None = None,
                       push_type: str = "alert") -> dict:
    """Sign with the local APNs key and POST to Apple for each token. `tokens`
    is a list of {token, env?} dicts. Returns {sent, dead, failed} where `dead`
    lists tokens Apple says are gone (caller prunes). Does NOT touch the DB —
    shared by send_to_all (own-key path) and the hosted relay.

    `payload`/`push_type` override the standard alert shape — the Live
    Activity path sends a prebuilt ActivityKit payload with push-type
    `liveactivity`, whose topic carries Apple's mandatory suffix."""
    payload = payload if payload is not None else build_payload(title, body)
    topic = settings.apns_topic
    if push_type == "liveactivity":
        topic = f"{topic}.push-type.liveactivity"
    elif push_type == "widgets":
        # iOS 26 push-updated widgets: same mandatory-suffix rule.
        topic = f"{topic}.push-type.widgets"
    headers = {
        "authorization": f"bearer {_provider_jwt()}",
        "apns-topic": topic,
        "apns-push-type": push_type,
        "apns-priority": "10",
    }
    sent = failed = 0
    dead: list[str] = []
    async with httpx.AsyncClient(http2=True, timeout=15.0) as client:
        for t in tokens:
            tok = t["token"]
            env, env_from_token = _resolve_env(t)
            if env is None:
                # Fail loudly instead of guessing a host. Guessing used to mean
                # sandbox, and a production token POSTed to the sandbox host
                # comes back 400 BadDeviceToken — which this function reads as
                # "dead" and send_to_all then DELETES. One misspelled APNS_ENV
                # silently wiped every registered token.
                failed += 1
                log.error("APNS_ENV is %r — expected 'sandbox' or 'production'; "
                          "refusing to guess a host for %s…",
                          settings.apns_env, tok[:8])
                continue
            host = _HOSTS[env]
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
            if reason == "BadDeviceToken" and not env_from_token:
                # BadDeviceToken also means "right token, wrong environment".
                # We only sent to `env` because the token didn't record its
                # own, so we can't tell the two apart — and pruning on a guess
                # is unrecoverable, while a failed send is retried next tick.
                failed += 1
                log.warning("apns BadDeviceToken for %s… on the %s host; the "
                            "token has no stored env, so it is NOT being "
                            "pruned — check APNS_ENV", tok[:8], env)
            elif r.status_code == 410 or reason in (
                    "BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                dead.append(tok)
                log.info("dead token %s… (%s %s)", tok[:8], r.status_code, reason)
            else:
                failed += 1
                log.warning("apns %s for %s…: %s", r.status_code, tok[:8], reason or r.text[:120])
    return {"sent": sent, "dead": dead, "failed": failed}


async def _push_via_relay(tokens: list[str], title: str, body: str,
                          url: str, token: str,
                          la_payload: dict | None = None,
                          push_type: str = "liveactivity") -> dict:
    """Send through a shared relay instead of signing locally. For self-hosters
    who don't run their own APNs key: the relay holds the key, fans out to
    Apple, and returns dead tokens for us to prune. POSTs only {tokens, title,
    body, env} — the relay enforces that shape."""
    env = (settings.apns_env or "").strip()
    if env not in _HOSTS:
        # Same refuse-to-guess semantics as _resolve_env on the own-key path:
        # a misspelled APNS_ENV used to silently coerce to "production", the
        # relay then stamped every token with the wrong environment, Apple
        # answered BadDeviceToken, and send_to_all pruned them all.
        log.error("APNS_ENV is %r — expected 'sandbox' or 'production'; "
                  "refusing to relay-push %d token(s)",
                  settings.apns_env, len(tokens))
        return {"sent": 0, "dead": [], "failed": len(tokens)}
    payload = {"tokens": tokens, "title": title, "body": body, "env": env}
    if la_payload is not None:
        # Live Activity through the relay: the raw ActivityKit payload rides
        # alongside; a pre-1.7 relay rejects unknown fields loudly rather
        # than delivering it as a mangled alert (Pydantic extra=forbid), so
        # the caller sees failed, not silent wrong-shape pushes.
        payload["push_type"] = push_type
        payload["payload"] = la_payload
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
    try:
        data = r.json()
    except ValueError:
        # A 200 with a non-JSON body (middlebox, captive portal, a broken
        # relay) is a protocol error, not a batch of dead tokens.
        log.warning("relay push returned 200 with a non-JSON body")
        return {"sent": 0, "dead": [], "failed": len(tokens)}
    return {"sent": data.get("sent", 0), "dead": data.get("dead", []),
            "failed": data.get("failed", 0)}


async def effective_relay() -> tuple[str | None, str | None]:
    """Resolve the relay (url, token) DB-over-env — the app-managed config wins
    over env defaults, mirroring how SMTP is resolved for email alerts."""
    cfg = await db.get_push_relay() or {}
    url = cfg.get("url") or settings.apns_relay_url
    token = cfg.get("token") or settings.apns_relay_token
    return url, token


async def send_live_activity_start(payload: dict, title: str, body: str,
                                   activity: str = "rain") -> dict:
    """Fan a prebuilt ActivityKit start payload out to every push-to-start
    token registered for `activity` — the tokens are per-attributes-type,
    so cross-sending starts the wrong Activity. Own APNs key preferred,
    hosted relay otherwise; prunes dead tokens like send_to_all."""
    rows = await db.list_live_activity_tokens("start", activity=activity)
    if not rows:
        return {"sent": 0, "dead": [], "failed": 0}
    if settings.apns_configured:
        res = await _push_tokens(rows, title, body,
                                 payload=payload, push_type="liveactivity")
    else:
        url, relay_token = await effective_relay()
        if not (url and relay_token):
            return {"sent": 0, "dead": [], "failed": len(rows)}
        res = await _push_via_relay([r["token"] for r in rows], title, body,
                                    url, relay_token, la_payload=payload)
    for tok in res.get("dead", []):
        await db.remove_live_activity_token(tok)
    return res


async def send_live_activity_update(activity: str, payload: dict,
                                    title: str, body: str) -> dict:
    """Fan an ActivityKit update payload out to every update token
    registered for `activity` ('rain' | 'storm' | 'heat'). Same transport
    split and dead-token pruning as send_live_activity_start."""
    rows = await db.list_live_activity_tokens("update", activity=activity)
    if not rows:
        return {"sent": 0, "dead": [], "failed": 0}
    if settings.apns_configured:
        res = await _push_tokens(rows, title, body,
                                 payload=payload, push_type="liveactivity")
    else:
        url, relay_token = await effective_relay()
        if not (url and relay_token):
            return {"sent": 0, "dead": [], "failed": len(rows)}
        res = await _push_via_relay([r["token"] for r in rows], title, body,
                                    url, relay_token, la_payload=payload)
    for tok in res.get("dead", []):
        await db.remove_live_activity_token(tok)
    return res


async def send_widget_refresh() -> dict:
    """iOS 26 push-updated widgets: tell every registered widget push token
    that content changed, so the Lock/Home Screen refreshes on the server's
    schedule instead of WidgetKit's guesses. The payload is the documented
    minimal shape; the OS treats it as a reload hint, not a notification."""
    rows = await db.list_live_activity_tokens("widgets")
    if not rows:
        return {"sent": 0, "dead": [], "failed": 0}
    payload = {"aps": {"content-changed": True}}
    # The relay's schema requires non-empty title/body (they feed its
    # logging/quota surface); a reload hint has no user-facing text, so
    # these placeholders never render anywhere.
    if settings.apns_configured:
        res = await _push_tokens(rows, "widgets", "refresh", payload=payload,
                                 push_type="widgets")
    else:
        url, relay_token = await effective_relay()
        if not (url and relay_token):
            return {"sent": 0, "dead": [], "failed": len(rows)}
        res = await _push_via_relay([r["token"] for r in rows],
                                    "widgets", "refresh",
                                    url, relay_token, la_payload=payload,
                                    push_type="widgets")
    for tok in res.get("dead", []):
        await db.remove_live_activity_token(tok)
    return res


async def push_configured() -> bool:
    """True if push can deliver — a local APNs key OR a resolved relay OR FCM."""
    if settings.apns_configured:
        return True
    from . import fcm
    if fcm.fcm_configured():
        return True
    url, token = await effective_relay()
    return bool(url and token)


async def send_to_all(title: str, body: str) -> dict:
    """Push to every registered token, split by platform:
      * iOS tokens → local APNs key (preferred) or the hosted relay.
      * Android tokens → FCM (HTTP v1).
    Prunes dead tokens from whichever path reports them. No-op per platform
    when that platform's push isn't configured."""
    from . import fcm
    own = settings.apns_configured
    relay_url, relay_token = await effective_relay()
    relay = bool(relay_url and relay_token)
    fcm_on = fcm.fcm_configured()

    tokens = await db.list_push_tokens()
    if not tokens:
        return {"sent": 0, "pruned": 0, "total": 0}
    ios = [t for t in tokens if (t.get("platform") or "ios") != "android"]
    android = [t for t in tokens if t.get("platform") == "android"]

    sent = failed = pruned = 0
    dead: list[str] = []

    # iOS
    if ios and own:
        res = await _push_tokens(ios, title, body)
        sent += res.get("sent", 0)
        failed += res.get("failed", 0)
        dead += res.get("dead", [])
    elif ios and relay:
        # The relay call stamps ONE env onto the whole batch (APNS_ENV), so a
        # token that recorded a DIFFERENT env at registration would be sent to
        # the wrong Apple host, come back BadDeviceToken, and get pruned —
        # the exact token-wipe the own-key path's _resolve_env fix closed.
        # Skip mismatched tokens (undeliverable via this relay env anyway).
        resolved = (settings.apns_env or "").strip()
        sendable: list[dict] = []
        for t in ios:
            stored = (t.get("env") or "").strip()
            if stored in _HOSTS and resolved in _HOSTS and stored != resolved:
                failed += 1
                log.warning("token %s… registered env=%s but APNS_ENV=%s — "
                            "skipping (relay sends one env per batch); it is "
                            "NOT being pruned", t["token"][:8], stored, resolved)
                continue
            sendable.append(t)
        if sendable:
            res = await _push_via_relay([t["token"] for t in sendable], title,
                                        body, relay_url, relay_token)  # type: ignore[arg-type]
            sent += res.get("sent", 0)
            failed += res.get("failed", 0)
            # Prune only tokens whose env was their OWN stored value. For a
            # token with no recorded env, the batch env was a guess from
            # APNS_ENV — "dead" may just mean "wrong environment", and pruning
            # on a guess is unrecoverable while a failed send retries.
            env_known = {t["token"] for t in sendable
                         if (t.get("env") or "").strip() in _HOSTS}
            for tok in res.get("dead", []):
                if tok in env_known:
                    dead.append(tok)
                else:
                    failed += 1
                    log.warning("relay reported %s… dead, but its env was "
                                "guessed from APNS_ENV — not pruning; check "
                                "APNS_ENV", tok[:8])

    # Android
    if android and fcm_on:
        res = await fcm.push_tokens_fcm([t["token"] for t in android], title, body)
        sent += res.get("sent", 0); failed += res.get("failed", 0)
        dead += res.get("dead", [])

    for tok in dead:
        await db.remove_push_token(tok)
        pruned += 1

    if sent == 0 and failed == 0 and pruned == 0:
        return {"sent": 0, "skipped": "no push channel for the registered tokens"}
    return {"sent": sent, "pruned": pruned, "failed": failed, "total": len(tokens)}
