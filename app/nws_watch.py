"""NWS alert relay (1.8, Pillar A): server-side polling of
api.weather.gov so severe weather reaches every channel the backend
owns — push (warning tier, breaks quiet hours), the alert history, the
digest, and webhooks — instead of living only in a foregrounded app.

Etiquette per the NWS API docs: the `?point=` query resolves warning
polygons server-side (their guidance for point consumers); a mandatory
identifying User-Agent; a 10-minute cadence per station, far inside
their allowance; failures back off silently to the next window. NWS
retired the legacy feeds in Dec 2025 — this API is the only path now.

Only Severe/Extreme severities push (the app's own NWS view still shows
everything); each alert id pushes ONCE GLOBALLY, with the seen-set
bounded and persisted in server_kv. Global, not per-station: three
stations in one backyard share one sky, and the per-station sets pushed
the same Extreme Heat Warning three times (Volney's phone, 2026-08-26).
The alert is titled by whichever station's poll surfaced it first.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from . import db
from .version import __version__

log = logging.getLogger("nws")

_INTERVAL_MS = 10 * 60_000
_UA = (f"zasder-weather-backend/{__version__} "
       "(github.com/volneydouglas/zasder-weather-backend)")
_PUSH_SEVERITIES = ("Severe", "Extreme")
# Global cap (the set now covers every station): NWS ids are long-lived
# URLs, and >100 simultaneously-active alerts for one household's points
# would be a national emergency, not a cache-pressure problem.
_SEEN_CAP = 120
_SEEN_KEY = "nws_watch.seen"

_last_poll_ms: dict[str, int] = {}


def _reset_for_tests() -> None:
    _last_poll_ms.clear()


def _coords(device: dict[str, Any]) -> tuple[float, float] | None:
    info = device.get("info") or {}
    coords = (info.get("coords") or {}).get("coords") or {}
    lat, lon = coords.get("lat"), coords.get("lon")
    if lat is None or lon is None:
        return None
    # Stored coords are operator data, not validated at write time — one
    # non-numeric record used to raise here and kill the WHOLE pass for
    # every station, every tick (R7 R10). Skip the bad station instead.
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


async def _fetch_active(lat: float, lon: float) -> list[dict[str, Any]] | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.weather.gov/alerts/active",
                params={"point": f"{lat:.4f},{lon:.4f}"},
                headers={"User-Agent": _UA,
                         "Accept": "application/geo+json"})
        if r.status_code != 200:
            return None
        feats = r.json().get("features") or []
        return [f.get("properties") or {} for f in feats]
    except Exception:
        log.debug("nws fetch failed", exc_info=True)
        return None


async def _load_seen(devices: list[dict[str, Any]]) -> list[str]:
    """The GLOBAL seen-id list. First run after the upgrade seeds it from
    the legacy per-station keys, so alerts already pushed under the old
    scheme don't re-push once as "new"."""
    raw = await db.get_kv(_SEEN_KEY)
    if raw is not None:
        try:
            seen = json.loads(raw)
            return seen if isinstance(seen, list) else []
        except ValueError:
            return []
    merged: list[str] = []
    have: set[str] = set()
    for d in devices:
        legacy = await db.get_kv(f"nws_watch.seen.{d.get('mac')}")
        if not legacy:
            continue
        try:
            ids = json.loads(legacy)
        except ValueError:
            continue
        if isinstance(ids, list):
            for aid in ids:
                if isinstance(aid, str) and aid not in have:
                    have.add(aid)
                    merged.append(aid)
    return merged


async def check(cfg, devices: list[dict[str, Any]], now_ms: int,
                deliver) -> None:
    """One monitor-tick entry point; per-station poll cadence, ONE global
    dedup set across stations."""
    seen: list[str] | None = None      # loaded lazily on the first due poll
    seen_set: set[str] = set()
    changed = False
    for d in devices:
        # Air monitors carry coords too — polling them would double every
        # NWS push for the same sky.
        if db.is_air_monitor_device(d):
            continue
        target = _coords(d)
        if target is None:
            continue
        mac = d["mac"]
        if now_ms - _last_poll_ms.get(mac, 0) < _INTERVAL_MS:
            continue
        _last_poll_ms[mac] = now_ms
        alerts = await _fetch_active(*target)
        if alerts is None:
            continue
        if seen is None:
            raw_global = await db.get_kv(_SEEN_KEY)
            seen = await _load_seen(devices)
            seen_set = set(seen)
            if raw_global is None:
                # First run under the global scheme: persist the merged
                # seed NOW and retire the legacy per-station keys — the
                # global key was only written on change, so a quiet server
                # re-read N legacy rows every tick forever (R9 T6).
                await db.set_kv(_SEEN_KEY, json.dumps(seen[-_SEEN_CAP:]))
                # Prefix sweep, not per-current-device: keys for since-
                # DELETED devices would otherwise linger forever (R10 U4).
                await db.delete_kv_prefix("nws_watch.seen.")
        name = d.get("name") or mac
        for a in alerts:
            aid = a.get("id")
            if not aid or aid in seen_set:
                continue
            if a.get("severity") not in _PUSH_SEVERITIES:
                # Non-push severities are recorded immediately — there is
                # nothing to retry.
                seen_set.add(aid)
                seen.append(aid)
                changed = True
                continue
            event = a.get("event") or "Weather alert"
            headline = (a.get("headline") or a.get("description")
                        or "")[:180]
            title = f"{name}: {event}"
            body = headline or "See the app for details."
            # Persist-after-deliver: a failed push leaves the id unseen so
            # the next tick retries; a handled one is done for good.
            if await deliver(cfg, f"[Zasder Weather] {title}", body,
                             title, body,
                             email_ok=cfg.email_scope == "all",
                             kind="nws", mac=mac):
                seen_set.add(aid)
                seen.append(aid)
                changed = True
                log.info("NWS %s pushed (surfaced by %s)", event, name)
    if changed and seen is not None:
        await db.set_kv(_SEEN_KEY, json.dumps(seen[-_SEEN_CAP:]))
