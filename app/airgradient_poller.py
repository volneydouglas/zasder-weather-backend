"""AirGradient cloud poller (1.8).

Each AirGradient location (Volney has an outdoor O-1PST and an indoor
I-9PSL) becomes its own device in the standard observations table, fed
through the same ingest path every other source uses.

Field policy:
- `*_corrected` values preferred over raw — AirGradient's published
  correction algorithms (EPA PM, temperature/humidity compensation) are
  what their own dashboard shows; the raw values are the fallback for
  older firmware that doesn't send corrected ones.
- Temperature arrives in °C and is stored °F (units are API-native,
  CLAUDE.md). PM in µg/m³, CO₂ in ppm, TVOC/NOx as Sensirion index
  values — all stored as-is; there is no imperial anything for these.
- Indoor locations map temp/humidity to the INDOOR fields (tempinf /
  humidityin), outdoor ones to tempf/humidity — so an indoor monitor
  never impersonates an outdoor station in records or alerts.

Synthetic MAC scheme, next tag in the relay family (01 AcuRite, 02 Fine
Offset, 05 Davis, 06 WeatherFlow):
  5D:5D:07:HH:HH:HH   07 = AirGradient, HH:HH:HH = locationId.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from . import ingest, source_status
from .airgradient_client import AirGradientClient

log = logging.getLogger("airgradient-poller")

SOURCE = "airgradient"


def synth_mac(location_id: int) -> str:
    lid = int(location_id) & 0xFFFFFF
    return f"5D5D07{(lid >> 16) & 0xFF:02X}{(lid >> 8) & 0xFF:02X}{lid & 0xFF:02X}"


def _num(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _corrected(d: dict[str, Any], key: str) -> float | None:
    """Prefer the corrected series; fall back to raw."""
    v = _num(d, f"{key}_corrected")
    return v if v is not None else _num(d, key)


def c_to_f(c: float | None) -> float | None:
    return None if c is None else round(c * 9.0 / 5.0 + 32.0, 1)


def build_payload(loc: dict[str, Any]) -> dict[str, Any] | None:
    """One location's current measures → an /ingest/custom payload.
    Returns None without a usable id or timestamp."""
    lid = loc.get("locationId")
    if not isinstance(lid, int):
        return None
    ts_iso = loc.get("timestamp")
    if not ts_iso or not isinstance(ts_iso, str):
        return None
    # Normalize through UTC so ingest's parser sees a clean ISO instant.
    try:
        t = datetime.fromisoformat(ts_iso[:-1] if ts_iso.endswith("Z") else ts_iso)
        t = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    indoor_unit = (loc.get("locationType") == "indoor")
    temp_f = c_to_f(_corrected(loc, "atmp"))
    hum = _corrected(loc, "rhum")
    # AirGradient's compensation can overshoot 100 in saturated air, and
    # ingest's (0,100) band would then DROP humidity entirely — clamp the
    # overshoot back to the physical ceiling instead (R8).
    if hum is not None and hum > 100:
        hum = 100.0

    climate: dict[str, Any] = {}
    if temp_f is not None:
        climate["tempf"] = temp_f
    if hum is not None:
        climate["humidity"] = round(hum)

    air: dict[str, Any] = {}
    for src, dest in (("pm01", "pm1"), ("pm02", "pm25"), ("pm10", "pm10"),
                      ("rco2", "co2")):
        v = _corrected(loc, src)
        if v is not None:
            air[dest] = v
    for src, dest in (("tvocIndex", "tvoc_index"), ("noxIndex", "nox_index")):
        v = _num(loc, src)
        if v is not None:
            air[dest] = v

    if not climate and not air:
        return None

    device: dict[str, Any] = {"id": synth_mac(lid)}
    name = loc.get("locationName")
    if isinstance(name, str) and name.strip():
        device["name"] = name.strip()
    lat, lon = _num(loc, "latitude"), _num(loc, "longitude")
    if lat is not None and lon is not None:
        device["coords"] = {"lat": lat, "lon": lon}

    payload: dict[str, Any] = {
        "device": device,
        "timestamp_utc": t.isoformat(timespec="seconds"),
        "source": SOURCE,
    }
    if climate:
        payload["indoor" if indoor_unit else "outdoor"] = climate
    if air:
        payload["air"] = air
    return payload


SOURCE_LOCAL = "airgradient-local"


def _local_pref(m: dict[str, Any], key: str) -> float | None:
    """Local firmware spells the corrected series `<key>Compensated`
    (the cloud says `_corrected`); prefer it, fall back to raw."""
    v = _num(m, f"{key}Compensated")
    return v if v is not None else _num(m, key)


def build_local_payload(m: dict[str, Any],
                        now_iso: str) -> dict[str, Any] | None:
    """One monitor's local /measures/current → an /ingest/custom payload.

    The local response has no timestamp — the reading is 'now' by
    construction, so the caller stamps it. Missing readings arrive
    OMITTED (verified live 2026-08), and stay omitted here. The device id
    is `serialno` — the monitor's own MAC, so a station keeps its row
    across LAN/cloud... it doesn't: the cloud path keys on locationId
    (5D:5D:07:…). Pick ONE path per monitor and stay on it."""
    serial = m.get("serialno")
    if not isinstance(serial, str) or len(serial.strip()) < 6:
        return None
    model = m.get("model") if isinstance(m.get("model"), str) else None
    # O-series is the outdoor family; everything else (I-series, unknown)
    # maps indoor — an indoor monitor must never impersonate an outdoor
    # station in records or alerts, and unknown errs the safe way.
    indoor_unit = not (model or "").upper().startswith("O-")

    temp_f = c_to_f(_local_pref(m, "atmp"))
    hum = _local_pref(m, "rhum")
    if hum is not None and hum > 100:
        hum = 100.0        # same compensation-overshoot clamp as the cloud

    climate: dict[str, Any] = {}
    if temp_f is not None:
        climate["tempf"] = temp_f
    if hum is not None:
        climate["humidity"] = round(hum)

    air: dict[str, Any] = {}
    for src, dest in (("pm01", "pm1"), ("pm02", "pm25"), ("pm10", "pm10"),
                      ("rco2", "co2")):
        v = _local_pref(m, src)
        if v is not None:
            air[dest] = v
    for src, dest in (("tvocIndex", "tvoc_index"), ("noxIndex", "nox_index")):
        v = _num(m, src)
        if v is not None:
            air[dest] = v

    if not climate and not air:
        return None

    device: dict[str, Any] = {"id": serial.strip()}
    if model:
        device["model"] = model
    payload: dict[str, Any] = {
        "device": device,
        "timestamp_utc": now_iso,
        # SOURCE_LOCAL, matching the status accounting — rows stamped
        # "airgradient" while /api/sources tracked "airgradient-local"
        # made the two paths untraceable to each other (R11).
        "source": SOURCE_LOCAL,
    }
    if climate:
        payload["indoor" if indoor_unit else "outdoor"] = climate
    if air:
        payload["air"] = air
    return payload


class AirGradientLocalPoller:
    """Background task (1.9): poll each configured LAN host every N seconds
    and ingest. Env-only (AIRGRADIENT_LOCAL_HOSTS) — a LAN-reachable
    backend is a docker-compose install, where env is the native config."""

    def __init__(self, client, hosts: list[str], interval_s: int = 120):
        self._client = client
        self._hosts = [h.strip() for h in hosts if h.strip()]
        self._interval_s = max(30, int(interval_s))
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(),
                                         name="airgradient-local-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("airgradient LAN poller: %d host(s) every %ds",
                 len(self._hosts), self._interval_s)
        while not self._stop.is_set():
            stored, reached, first_err = 0, 0, None
            for host in self._hosts:
                # Per-host isolation: one unplugged monitor must not skip
                # its siblings or mark the whole source failed.
                try:
                    m = await self._client.measures_current(host)
                    reached += 1
                    now_iso = (datetime.now(timezone.utc)
                               .isoformat(timespec="seconds"))
                    payload = build_local_payload(m, now_iso)
                    if payload is None:
                        continue
                    result = await ingest._do_ingest(payload)  # type: ignore[attr-defined]
                    stored += int((result or {}).get("inserted", 0))
                except Exception as e:
                    first_err = first_err or str(e)
                    log.warning("airgradient LAN host %s failed: %s",
                                host, source_status.redact(str(e)))
            if reached > 0 and first_err is None:
                # Rows STORED, not fetched (the tempest_poller lesson).
                source_status.record_success(SOURCE_LOCAL, rows=stored)
            elif first_err is not None:
                # Even with siblings reached: a clean record_success here
                # cleared last_error and made one unplugged monitor
                # invisible in /api/sources (R11). Rows from the healthy
                # hosts still stored above — only the STATUS reports the
                # partial failure.
                if reached > 0:
                    first_err = (f"{reached}/{len(self._hosts)} host(s) ok; "
                                 f"first failure: {first_err}")
                source_status.record_failure(SOURCE_LOCAL, first_err)
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass


class AirGradientPoller:
    """Background task: poll the AirGradient cloud every N seconds and
    ingest every location's current measures."""

    def __init__(self, client: AirGradientClient, interval_s: int = 120) -> None:
        self._client = client
        self._interval_s = max(30, int(interval_s))
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="airgradient-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("airgradient poller running every %ds", self._interval_s)
        while not self._stop.is_set():
            try:
                locations = await self._client.measures_current()
            except Exception as e:
                source_status.record_failure(SOURCE, str(e))
                log.warning("AirGradient poll failed: %s",
                            source_status.redact(str(e)))
                locations = None
            if locations is not None:
                stored = 0
                for loc in locations:
                    # Per-location isolation (R8 S6): one malformed
                    # location must not skip its siblings or mark the
                    # whole source failed when the fetch itself was fine.
                    try:
                        payload = build_payload(loc)
                        if payload is None:
                            continue
                        result = await ingest._do_ingest(payload)  # type: ignore[attr-defined]
                        stored += int((result or {}).get("inserted", 0))
                    except Exception:
                        log.warning("airgradient location %s ingest failed",
                                    loc.get("locationId"), exc_info=True)
                # Rows STORED, not fetched (the tempest_poller lesson): the
                # write throttle can legitimately reject a too-soon reading.
                source_status.record_success(SOURCE, rows=stored)
                log.debug("airgradient: %d location(s), %d stored",
                          len(locations), stored)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass
