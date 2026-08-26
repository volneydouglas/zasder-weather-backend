"""Opt-in self-update — a guest instance replaces its own image with the
official release, a couple of days after that release ships.

Self-hosters don't watch release feeds. The UpdateChecker already tells them
a newer version exists (status-page banner, /api/version); this closes the
loop for instances that opt in, using the platform the backend already runs
on: a Fly machine may replace ITS OWN image through the Machines API, and
Fly restarts it into the new release (whose boot migrations then run — the
established upgrade path).

Enabling requires BOTH:
  * AUTO_UPDATE=1
  * FLY_API_TOKEN — an app-scoped deploy token (`fly tokens create deploy`),
    set as a secret on the instance. Without it the feature is inert.

Safety model, in order of importance:
  * MATURITY DELAY (default 48h, AUTO_UPDATE_MIN_AGE_HOURS): a release must
    have been visible to THIS instance for two days before it is applied,
    so a bad release can be yanked before any auto-updater touches it. Age
    is measured from when this instance FIRST SAW the tag (persisted in
    server_kv, so restarts don't reset the clock) — not from GitHub
    metadata, which would need the rate-limited REST API that 403s on
    Fly's shared egress IPs (see updates.py).
  * SAME MAJOR ONLY, NEVER DOWNGRADE: a major bump is allowed to have
    manual steps; it waits for a human.
  * IMAGE MUST EXIST: the registry manifest is verified (anonymous pull
    token) before the machine config is touched — a release whose image
    was never published must not brick the instance.
  * ONE ATTEMPT PER VERSION PER DAY: the attempt is recorded first, so a
    restart mid-update cannot loop.

What this deliberately does NOT protect against: an official image that
starts and then misbehaves. The maturity delay is the mitigation; beyond
it, that is what a release process is for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

from . import db
from .updates import is_newer, parse_version
from .version import __version__

log = logging.getLogger("self_update")

_CHECK_INTERVAL_S = 6 * 3600          # re-evaluate a few times a day
_STARTUP_DELAY_S = 120                # never compete with boot
_KV_FIRST_SEEN = "auto_update_first_seen"      # {"tag": ..., "ms": ...}
_KV_LAST_ATTEMPT = "auto_update_last_attempt"  # {"tag": ..., "ms": ...}
_ATTEMPT_COOLDOWN_MS = 24 * 3600 * 1000

_DEFAULT_IMAGE_REPO = "ghcr.io/volneydouglas/zasder-weather-backend"


def _enabled() -> bool:
    return (os.environ.get("AUTO_UPDATE", "").strip().lower()
            in ("1", "true", "on", "yes"))


def _fly_token() -> str | None:
    return os.environ.get("FLY_API_TOKEN", "").strip() or None


# Deploy tokens (`fly tokens create deploy`) are macaroons — 'fm2_...' or a
# comma-joined discharge set — and Fly's API takes those under the FlyV1
# scheme, not Bearer; flyctl makes exactly this split. Setup scripts store
# the flyctl output verbatim, which already carries a 'FlyV1 ' prefix, so
# strip any scheme before choosing one. Bearer stays for OAuth tokens.
_MACAROON_PREFIXES = ("fm1r", "fm1a", "fm2")


def _auth_header(token: str) -> str:
    tok = token.strip()
    lowered = tok.lower()
    for scheme in ("flyv1 ", "bearer "):
        if lowered.startswith(scheme):
            tok = tok[len(scheme):].strip()
            lowered = tok.lower()
    first = tok.split(",", 1)[0].strip()
    if first.partition("_")[0] in _MACAROON_PREFIXES:
        return f"FlyV1 {tok}"
    return f"Bearer {tok}"


def _image_repo() -> str:
    return (os.environ.get("AUTO_UPDATE_IMAGE_REPO", "").strip()
            or _DEFAULT_IMAGE_REPO)


def _min_age_ms() -> int:
    try:
        hours = float(os.environ.get("AUTO_UPDATE_MIN_AGE_HOURS", "48"))
    except ValueError:
        hours = 48.0
    return int(max(hours, 0) * 3_600_000)


def eligible(latest: str | None, current: str,
             first_seen_ms: int | None, now_ms: int,
             min_age_ms: int) -> tuple[bool, str]:
    """Pure decision: may `latest` be auto-applied over `current`?
    Returns (ok, reason) — the reason is logged either way, so an operator
    can always answer "why hasn't it updated yet?" from the logs."""
    if not latest:
        return False, "no release visible"
    if not is_newer(latest, current):
        return False, f"{latest} is not newer than {current}"
    lmaj, cmaj = parse_version(latest)[0], parse_version(current)[0]
    if lmaj != cmaj:
        return False, (f"major bump {cmaj}→{lmaj} needs a human "
                       "(may carry manual steps)")
    if first_seen_ms is None:
        return False, "first sighting — maturity clock starts now"
    age = now_ms - first_seen_ms
    if age < min_age_ms:
        return False, (f"release seen {age / 3_600_000:.1f}h ago, "
                       f"waiting for {min_age_ms / 3_600_000:.0f}h maturity")
    return True, "eligible"


async def _kv_get(key: str) -> dict[str, Any] | None:
    raw = await db.get_kv(key)
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


_MANIFEST_ACCEPT = ("application/vnd.oci.image.index.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json, "
                    "application/vnd.docker.distribution.manifest.v2+json")


async def image_exists(image_repo: str, tag: str) -> bool:
    """HEAD the manifest via the standard registry v2 token challenge. The
    anonymous flow starts with an unauthenticated request whose 401 carries
    WWW-Authenticate: Bearer realm=…,service=… — the token endpoint is NOT
    the registry host (Docker Hub uses auth.docker.io), so guessing
    https://{host}/token only happened to work for GHCR (CodeRabbit,
    2026-08-20). False on ANY doubt — the caller must not update onto an
    unverifiable image."""
    try:
        host, _, path = image_repo.partition("/")
        url = f"https://{host}/v2/{path}/manifests/{tag}"
        async with httpx.AsyncClient(timeout=15) as client:
            head = await client.head(url, headers={"Accept": _MANIFEST_ACCEPT})
            if head.status_code == 200:      # registry allows anonymous reads
                return True
            if head.status_code != 401:
                return False
            challenge = head.headers.get("www-authenticate", "")
            fields = dict(
                (k.strip(), v.strip('"')) for k, _, v in
                (part.partition("=") for part in
                 challenge.removeprefix("Bearer ").split(",")) if v)
            realm = fields.get("realm")
            if not realm:
                return False
            params = {"scope": f"repository:{path}:pull"}
            if fields.get("service"):
                params["service"] = fields["service"]
            tok = await client.get(realm, params=params)
            token = tok.json().get("token") if tok.status_code == 200 else None
            if not token:
                return False
            head = await client.head(url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": _MANIFEST_ACCEPT,
            })
            return head.status_code == 200
    except Exception as e:  # noqa: BLE001 — doubt means no
        log.info("image check failed for %s:%s: %s", image_repo, tag, e)
        return False


async def apply_update(tag: str) -> bool:
    """Point this machine at the release image via the Machines API. Fly
    restarts the machine as part of the update, so a True return may never
    be observed — the intent is logged first for exactly that reason."""
    app_name = os.environ.get("FLY_APP_NAME", "").strip()
    machine_id = os.environ.get("FLY_MACHINE_ID", "").strip()
    token = _fly_token()
    if not (app_name and machine_id and token):
        log.warning("auto-update: missing FLY_APP_NAME/FLY_MACHINE_ID/"
                    "FLY_API_TOKEN — cannot self-update")
        return False
    image = f"{_image_repo()}:{tag}"
    base = f"https://api.machines.dev/v1/apps/{app_name}/machines/{machine_id}"
    headers = {"Authorization": _auth_header(token)}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            cur = await client.get(base, headers=headers)
            if cur.status_code != 200:
                log.warning("auto-update: machine read failed (HTTP %s)",
                            cur.status_code)
                return False
            config = cur.json().get("config") or {}
            if not config:
                # POSTing {"config": {"image": ...}} alone would REPLACE the
                # whole machine config — env, services, mounts, checks — and
                # restart a machine that can no longer serve. Worse than not
                # updating (CodeRabbit, 2026-08-20); hold instead.
                log.warning("auto-update: machine returned an empty config — "
                            "holding rather than replacing it wholesale")
                return False
            config["image"] = image
            log.warning("auto-update: applying %s → %s (machine restarts)",
                        __version__, image)
            resp = await client.post(base, headers=headers,
                                     json={"config": config})
            if resp.status_code not in (200, 201):
                log.warning("auto-update: machine update failed (HTTP %s): %s",
                            resp.status_code, resp.text[:200])
                return False
            return True
    except Exception as e:  # noqa: BLE001 — never take serving down over this
        log.warning("auto-update: %s", e)
        return False


class SelfUpdater:
    """Background task riding on UpdateChecker's result."""

    def __init__(self, app):
        self.app = app
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not _enabled():
            return
        if not _fly_token():
            log.warning("AUTO_UPDATE=1 but FLY_API_TOKEN is not set — "
                        "self-update stays inert (create one with "
                        "`fly tokens create deploy` and set it as a secret)")
            return
        from .updates import _enabled as _check_enabled
        if not _check_enabled():
            # The updater rides on UpdateChecker's result: with the check
            # off it never learns a release exists and silently never
            # updates — say so once at boot (CODE_REVIEW_R5 R5-22).
            log.warning("AUTO_UPDATE=1 but UPDATE_CHECK=0 — self-update "
                        "stays inert (it needs the release check to see "
                        "new versions)")
            return
        log.info("auto-update enabled (maturity %dh, images %s)",
                 _min_age_ms() // 3_600_000, _image_repo())
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await asyncio.sleep(_STARTUP_DELAY_S)
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("auto-update tick failed")
            await asyncio.sleep(_CHECK_INTERVAL_S)

    async def _tick(self) -> None:
        info = getattr(self.app.state, "update_info", None) or {}
        latest = info.get("latest")
        now_ms = int(time.time() * 1000)

        # Track when THIS tag was first seen; a new tag restarts the clock.
        first_seen = await _kv_get(_KV_FIRST_SEEN)
        if latest and (not first_seen or first_seen.get("tag") != latest):
            await db.set_kv(_KV_FIRST_SEEN, json.dumps(
                {"tag": latest, "ms": now_ms}))
            first_seen = {"tag": latest, "ms": now_ms}

        ok, reason = eligible(
            latest, __version__,
            first_seen.get("ms") if first_seen else None,
            now_ms, _min_age_ms())
        if not ok:
            log.info("auto-update: %s", reason)
            return

        attempt = await _kv_get(_KV_LAST_ATTEMPT)
        if (attempt and attempt.get("tag") == latest
                and now_ms - int(attempt.get("ms") or 0) < _ATTEMPT_COOLDOWN_MS):
            log.info("auto-update: already attempted v%s recently", latest)
            return

        # The publish workflow's semver tags carry NO leading v
        # (docker/metadata-action {{version}}): the image for release
        # v1.6.1 is ...:1.6.1.
        tag = latest
        if not await image_exists(_image_repo(), tag):
            log.warning("auto-update: release v%s has no published image at "
                        "%s — holding", latest, _image_repo())
            return

        # Record the attempt BEFORE applying: the machine restarts out from
        # under us on success, and a crash mid-apply must not retry-loop.
        await db.set_kv(_KV_LAST_ATTEMPT, json.dumps(
            {"tag": latest, "ms": now_ms}))
        await apply_update(tag)
