"""App-managed cloud-source credentials (Settings → Integrations).

Env vars / Fly secrets remain the scripted-setup path; this lets the app
configure the CLOUD pollers — AmbientWeather, Davis WeatherLink, WeatherFlow
Tempest — without touching a terminal, for the user who has neither an SDR
nor a shell. Same trust model as the WU API key and the SMTP password:
values live in server_kv on the volume, the DB value wins over env, and
reads are WRITE-ONLY (status endpoints say whether a field is set, never
what it is). Required groundwork for the hosted tier, where operators have
no env access at all.

The manager owns the three pollers' lifecycle so a PUT applies immediately:
stop the old poller, rebuild from the effective (kv-over-env) credentials,
start, and re-declare source_status. Boot goes through the same path, so
app-stored credentials survive restarts identically to env ones.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import db, source_status
from .config import settings

log = logging.getLogger("integrations")

# provider -> (field, secret?, coerce). `secret` fields are write-only:
# status never returns their values, only whether they are set. Coercion
# failures surface as a 400 at the endpoint, never a stored bad value.
PROVIDERS: dict[str, list[tuple[str, bool, type]]] = {
    "awn": [("application_key", True, str), ("api_key", True, str)],
    "weatherlink": [("api_key", True, str), ("api_secret", True, str),
                    ("station_id", False, int)],
    "tempest": [("token", True, str), ("station_id", False, int),
                ("name", False, str)],
}

# Which source_status slug each provider reports under (the names the
# status page has always used).
_STATUS_SLUG = {"awn": "ambientweather", "weatherlink": "davis-cloud",
                "tempest": "tempest"}


def _kv_key(provider: str, field: str) -> str:
    return f"integration.{provider}.{field}"


def _env_values(provider: str) -> dict[str, Any]:
    if provider == "awn":
        return {"application_key": settings.aw_application_key,
                "api_key": settings.aw_api_key}
    if provider == "weatherlink":
        return {"api_key": settings.weatherlink_api_key,
                "api_secret": settings.weatherlink_api_secret,
                "station_id": settings.weatherlink_station_id}
    return {"token": settings.tempest_token,
            "station_id": settings.tempest_station_id,
            "name": settings.tempest_name}


def _required(provider: str) -> list[str]:
    """Fields that must all be present for the provider to be configured
    (optional fields — the Tempest display name — excluded)."""
    return [f for f, _, _ in PROVIDERS[provider] if f != "name"]


async def effective(provider: str) -> dict[str, Any]:
    """Merged credentials: an app-stored value wins field-by-field over env,
    matching the wu_api_key precedent (db.get_kv or settings...)."""
    env = _env_values(provider)
    out: dict[str, Any] = {}
    for field, _, coerce in PROVIDERS[provider]:
        raw = await db.get_kv(_kv_key(provider, field))
        if raw is not None and raw != "":
            try:
                out[field] = coerce(raw)
            except (TypeError, ValueError):
                # A corrupt stored value must degrade to "unset", not crash
                # the boot path that calls this.
                out[field] = None
        else:
            out[field] = env.get(field)
    return out


async def status() -> dict[str, Any]:
    """Presence only — this response is the read path for a screen that
    displays credentials, so it must never carry one."""
    out: dict[str, Any] = {}
    for provider, fields in PROVIDERS.items():
        eff = await effective(provider)
        field_state: dict[str, Any] = {}
        any_app = False
        for f, secret, _ in fields:
            stored = await db.get_kv(_kv_key(provider, f))
            src = "app" if stored not in (None, "") else (
                "env" if _env_values(provider).get(f) not in (None, "") else None)
            any_app = any_app or src == "app"
            field_state[f] = {"set": eff.get(f) not in (None, ""),
                              "source": src,
                              # Non-secret values are display-safe (a station
                              # id or name is not a credential).
                              **({} if secret or eff.get(f) in (None, "")
                                 else {"value": eff.get(f)})}
        configured = all(eff.get(f) not in (None, "") for f in _required(provider))
        out[provider] = {"configured": configured,
                         "source": ("app" if any_app else
                                    ("env" if configured else None)),
                         "fields": field_state}
    return out


async def store(provider: str, values: dict[str, Any]) -> None:
    """Store the provided fields (omitted fields keep their current value —
    the SMTP-password partial-update contract). Empty string clears a field
    back to the env fallback."""
    # Stage-then-commit: coerce EVERY field before writing ANY. Writing one
    # at a time meant a coercion failure on a later field left partial new
    # credentials in server_kv behind the 400 (CODE_REVIEW_R5 R5-23).
    staged: dict[str, str | None] = {}
    for field, _, coerce in PROVIDERS[provider]:
        if field not in values:
            continue
        v = values[field]
        if v in (None, ""):
            staged[field] = None
            continue
        # Store the COERCED form, not the raw input: a JSON 1.9 or true
        # survives int() at validation time, but str(raw) would persist
        # "1.9"/"True" — which the read path's int() then rejects, silently
        # degrading the field to None behind a 200 (CodeRabbit, 2026-08-20).
        staged[field] = str(coerce(v))
    for field, val in staged.items():
        await db.set_kv(_kv_key(provider, field), val)


async def probe(provider: str) -> str | None:
    """One cheap authenticated upstream call with the effective credentials.
    Returns None when they work, else a short human-readable reason. Wrong
    keys used to save as a silent success — "On" in the UI, the failure
    visible only at /api/sources, which no client reads (CODE_REVIEW_R5
    R5-07, the R3-21 WU-key pattern). Same serverNote-style contract as
    that fix: the PUT still persists (upstream outages must not block
    saving), but the response carries the check result for the UI.
    Exception messages from all three clients are path-only — no
    credential ever rides one (the CR-01 redaction contract)."""
    eff = await effective(provider)
    if not all(eff.get(f) not in (None, "") for f in _required(provider)):
        return None                      # unconfigured — nothing to check
    client: Any = None
    try:
        if provider == "awn":
            from .ambient_client import AmbientWeatherClient
            client = AmbientWeatherClient(eff["application_key"], eff["api_key"])
            await client.list_devices()
        elif provider == "weatherlink":
            from .weatherlink_client import WeatherLinkClient
            client = WeatherLinkClient(eff["api_key"], eff["api_secret"])
            await client.list_stations()
        else:
            from .tempest_client import TempestClient
            client = TempestClient(eff["token"])
            await client.stations()
        return None
    except Exception as e:               # noqa: BLE001 — reported, not lost
        return str(e) or type(e).__name__
    finally:
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()


async def clear(provider: str) -> None:
    for field, _, _ in PROVIDERS[provider]:
        await db.set_kv(_kv_key(provider, field), None)


class IntegrationManager:
    """Owns the cloud pollers so credential changes apply without a redeploy.

    One apply(provider) path serves boot and the PUT/DELETE endpoints alike:
    stop whatever runs, rebuild from the effective credentials, start if
    configured, and re-declare source_status either way."""

    def __init__(self) -> None:
        self._pollers: dict[str, Any] = {}
        self._clients: dict[str, Any] = {}
        # Serializes apply/teardown per provider: apply() awaits network I/O
        # (poller.start) between tearing down the old poller and registering
        # the new one, and a PUT/DELETE landing in that gap saw an empty
        # registry — its teardown was a no-op, and the in-flight apply then
        # registered a poller built from the pre-delete credentials that no
        # later teardown could reach (CODE_REVIEW_R5 R5-06). Built lazily so
        # each Lock binds to the loop that first awaits it (CLAUDE.md; the
        # suite runs one event loop per test).
        self._locks: dict[str, "asyncio.Lock"] = {}

    def _lock(self, provider: str) -> "asyncio.Lock":
        lock = self._locks.get(provider)
        if lock is None:
            lock = self._locks[provider] = asyncio.Lock()
        return lock

    async def start_all(self) -> None:
        for provider in PROVIDERS:
            await self.apply(provider)

    async def stop_all(self) -> None:
        for provider in list(PROVIDERS):
            async with self._lock(provider):
                await self._teardown(provider)

    async def _teardown(self, provider: str) -> None:
        poller = self._pollers.pop(provider, None)
        if poller is not None:
            await poller.stop()
        client = self._clients.pop(provider, None)
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    async def apply(self, provider: str) -> bool:
        """(Re)start the provider's poller from effective credentials.
        Returns True when a poller is running afterwards."""
        async with self._lock(provider):
            return await self._apply_locked(provider)

    async def _apply_locked(self, provider: str) -> bool:
        await self._teardown(provider)
        eff = await effective(provider)
        configured = all(eff.get(f) not in (None, "") for f in _required(provider))
        slug = _STATUS_SLUG[provider]
        source_status.declare(slug, configured)
        if not configured:
            log.info("%s not configured — poller stopped/disabled", provider)
            return False

        if provider == "awn":
            from .ambient_client import AmbientWeatherClient
            from .poller import Poller
            client = AmbientWeatherClient(eff["application_key"], eff["api_key"])
            poller = Poller(client)
        elif provider == "weatherlink":
            from .weatherlink_client import WeatherLinkClient
            from .weatherlink_poller import WeatherlinkPoller
            client = WeatherLinkClient(eff["api_key"], eff["api_secret"])
            poller = WeatherlinkPoller(client, eff["station_id"],
                                       settings.weatherlink_poll_interval_seconds)
        else:
            from .tempest_client import TempestClient
            from .tempest_poller import TempestPoller
            client = TempestClient(eff["token"])
            poller = TempestPoller(client, eff["station_id"],
                                   settings.tempest_poll_interval_seconds,
                                   eff.get("name"))
        try:
            await poller.start()
        except BaseException:
            # A start() that raises must not leak the client it was built
            # around — nothing has registered it yet.
            if hasattr(client, "aclose"):
                await client.aclose()
            raise
        self._clients[provider] = client
        self._pollers[provider] = poller
        log.info("%s poller started", provider)
        return True
