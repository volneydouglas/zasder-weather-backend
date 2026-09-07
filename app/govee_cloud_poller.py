"""Govee cloud poller (2.0): a GoveeLife H5140 CO₂ monitor, or an
air-quality sibling, as an air device.

The H5140 is a CO₂ monitor with a Sensirion SCD4x and no particle
sensor; it talks only to Govee's cloud. This poller lists the account's
air monitors, reads each one's state on a schedule, and posts through
`/ingest/custom` exactly as the AirGradient poller does, so the readings
land in the same columns (co2, pm25 where a model has it) and the
device gets the air card, never the weather-station machinery.

What the API does and does not say, learned on hardware (govee-extract,
2026-08): values come back as `{"value": 620}` with no unit and no
timestamp; the H5140 reports temperature in FAHRENHEIT and does not say
so (20.7 °C arrived as 69.26); a listing that DOES declare a Celsius
unit is converted. `online` is 1/0 and an offline monitor's numbers
are the cloud's last cache, not air. The cloud lags the display by a
minute or two.

No timestamp means no clean "already ingested" test. A reading whose
values are identical to the last posted one is the cache repeating
itself far more often than a room that did not change by a single
ppm, so it is skipped; unchanged air is still re-posted once the last
post is REPOST_AFTER_S old, so a flat line stays a live one.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any

from . import ingest
from . import source_status
from .govee_cloud_client import GoveeCloudClient

log = logging.getLogger("zasder.govee")

SOURCE = "govee"
MIN_INTERVAL_S = 30
# Unchanged values are re-posted at least this often, so the station's
# last_seen keeps moving while the room does not.
# Well under any stale threshold a person would set: Doren's five-minute
# threshold against the old ten-minute window flapped his Govee CO2
# "stopped reporting" / "reporting again" every five minutes (2026-09-06).
REDISCOVER_AFTER_S = 10 * 60
REPOST_AFTER_S = 2 * 60
# A typed suffix shorter than this matches too easily to trust.
SUFFIX_MIN_HEX = 4
# Govee device types / capability instances that mark an air monitor.
AIR_TYPE = "devices.types.air_quality_monitor"
AIR_INSTANCES = ("carbonDioxideConcentration", "pm25")
# SKUs known to report Fahrenheit without saying so.
FAHRENHEIT_SKUS = frozenset({"H5140"})
# Keys Govee nests a scalar behind, by capability.
_NESTED_VALUE_KEYS = ("value", "currentTemperature", "currentHumidity",
                      "currentValue", "carbonDioxide")


def synth_mac(device_id: str) -> str:
    """`5D:5D:08:HH:HH:HH` from the Govee device id. Govee ids are eight
    colon-separated bytes, not a MAC; the low three bytes of a hash of
    the whole id keep the same monitor on the same address."""
    digest = hashlib.sha1(device_id.strip().upper().encode()).digest()
    return f"5D5D08{digest[0]:02X}{digest[1]:02X}{digest[2]:02X}"


def parse_device_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    out = []
    for part in str(raw).replace(";", ",").split(","):
        p = part.strip().upper()
        if p and p not in out:
            out.append(p)
    return out


def _hex_only(device_id: str) -> str:
    return "".join(c for c in device_id.upper() if c in "0123456789ABCDEF")


def resolve_device_ids(wanted: list[str], listed: list[str]) -> tuple[list[str], list[str]]:
    """Match the ids a person typed against the ids the account lists.

    Govee prints an eight-pair id (04:B5:DC:B4:D9:F2:DE:20) but the app's
    device page shows the last six pairs, so people type those (Doren,
    2026-09-06: "Can't connect", DC:B4:D9:F2:DE:20 against an account that
    had 04:B5:DC:B4:D9:F2:DE:20). An exact match wins; otherwise a typed id
    that is the SUFFIX of exactly one listed id resolves to it. Two listed
    ids sharing a suffix stay unresolved rather than guessing. Returns
    (resolved full ids in the typed order, ids that matched nothing)."""
    full = {_hex_only(i): i.upper() for i in listed if i}
    resolved: list[str] = []
    unresolved: list[str] = []
    for w in wanted:
        key = _hex_only(w)
        if not key:
            unresolved.append(w)
            continue
        if key in full:
            resolved.append(full[key])
            continue
        if len(key) < SUFFIX_MIN_HEX:
            # "20" is the tail of most ids on most accounts; refuse rather
            # than resolve a typo to a device (round-two review BE-N8).
            unresolved.append(w)
            continue
        hits = [full[k] for k in full if k.endswith(key)]
        if len(hits) == 1:
            resolved.append(hits[0])
        else:
            unresolved.append(w)
    return resolved, unresolved


def _unwrap(value: Any) -> Any:
    seen = 0
    while isinstance(value, dict) and seen < 4:
        seen += 1
        for key in _NESTED_VALUE_KEYS:
            if key in value:
                value = value[key]
                break
        else:
            return None
    return value


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def capability_values(state: dict[str, Any]) -> dict[str, Any]:
    """`instance -> scalar` from a device-state payload's capabilities."""
    out: dict[str, Any] = {}
    for cap in state.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        inst = cap.get("instance")
        if not inst or cap.get("state") is None:
            continue
        v = _unwrap(cap.get("state"))
        if v is not None:
            out[str(inst)] = v
    return out


def is_air_monitor_listing(dev: dict[str, Any]) -> bool:
    if dev.get("type") == AIR_TYPE:
        return True
    insts = {c.get("instance") for c in (dev.get("capabilities") or [])
             if isinstance(c, dict)}
    return any(i in insts for i in AIR_INSTANCES)


def temperature_is_celsius(dev: dict[str, Any]) -> bool:
    """True only when the listing DECLARES Celsius for sensorTemperature.
    The H5140 declares nothing and reports Fahrenheit."""
    for cap in dev.get("capabilities") or []:
        if not isinstance(cap, dict) or cap.get("instance") != "sensorTemperature":
            continue
        unit = str(((cap.get("parameters") or {}).get("unit")) or "").lower()
        if "celsius" in unit:
            return True
    return False


def build_payload(device_id: str, listing: dict[str, Any],
                  state: dict[str, Any], name: str | None = None,
                  now: datetime | None = None) -> dict[str, Any] | None:
    """One device's state → an /ingest/custom payload, or None when the
    monitor is offline or reports nothing usable."""
    values = capability_values(state)
    online = values.get("online")
    if online is not None and _num(online) == 0.0:
        return None
    air: dict[str, Any] = {}
    co2 = _num(values.get("carbonDioxideConcentration"))
    if co2 is not None:
        air["co2"] = co2
    pm = _num(values.get("pm25"))
    if pm is not None:
        air["pm25"] = pm
    climate: dict[str, Any] = {}
    t = _num(values.get("sensorTemperature"))
    if t is not None:
        if temperature_is_celsius(listing) and str(listing.get("sku")) not in FAHRENHEIT_SKUS:
            t = t * 9.0 / 5.0 + 32.0
        climate["tempf"] = round(t, 1)
    h = _num(values.get("sensorHumidity"))
    if h is not None:
        climate["humidity"] = round(min(max(h, 0.0), 100.0))
    if not air and not climate:
        return None
    device: dict[str, Any] = {"id": synth_mac(device_id)}
    label = name or listing.get("deviceName")
    if isinstance(label, str) and label.strip():
        device["name"] = label.strip()
    sku = listing.get("sku")
    if isinstance(sku, str) and sku.strip():
        device["model"] = sku.strip()
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload: dict[str, Any] = {
        "device": device,
        "timestamp_utc": stamp.isoformat(timespec="seconds"),
        "source": SOURCE,
    }
    # A CO₂ monitor lives indoors: its temperature is the room's, never
    # the sky's, so it must not impersonate an outdoor station.
    if climate:
        payload["indoor"] = climate
    if air:
        payload["air"] = air
    return payload


def fingerprint(payload: dict[str, Any]) -> str:
    parts = []
    for block in ("air", "indoor"):
        for k, v in sorted((payload.get(block) or {}).items()):
            parts.append(f"{block}.{k}={v}")
    return "|".join(parts)


class GoveeCloudPoller:
    def __init__(self, client: GoveeCloudClient, interval_s: int = 60,
                 devices: list[str] | str | None = None,
                 name: str | None = None) -> None:
        self._client = client
        self._interval_s = max(MIN_INTERVAL_S, int(interval_s))
        self._wanted = (parse_device_ids(devices) if isinstance(devices, str)
                        else [d.upper() for d in (devices or [])])
        self._name = name
        self._devices: dict[str, dict[str, Any]] = {}     # id -> listing
        # Typed ids the account did not list: reported as failures and
        # re-resolved every tick, never polled (CodeRabbit, PR #36).
        self._unresolved: list[str] = []
        # Re-resolution of a typed id the account did not list runs at
        # most every REDISCOVER_AFTER_S, not every tick (round-three
        # review BE-F12: a typo cost 1,440 listing calls a day).
        self._last_discover_mono: float = -1e9
        self._last_print: dict[str, tuple[str, float]] = {}
        self._task = None
        import asyncio
        self._stop = asyncio.Event()

    def _label(self, device_id: str) -> str | None:
        return self._name if self._name and len(self._devices) == 1 else None

    async def _discover(self) -> None:
        self._last_discover_mono = time.monotonic()
        try:
            listed = await self._client.list_devices()
        except Exception as e:                       # noqa: BLE001
            log.warning("govee device list failed: %s", e)
            listed = []
        found = {str(d.get("device", "")).upper(): d for d in listed
                 if d.get("device") and is_air_monitor_listing(d)}
        if self._wanted:
            # A typed suffix polls the full id it resolves to; an id the
            # account does not list is kept as typed so the failure shows.
            resolved, unresolved = resolve_device_ids(self._wanted, list(found))
            self._devices = {i: found[i] for i in resolved}
            self._unresolved = list(unresolved)
            for i in unresolved:
                log.warning("govee device %s is not on this account", i)
        else:
            self._devices = found
        for i, d in self._devices.items():
            log.info("govee device %s: sku=%s name=%s", i, d.get("sku"),
                     d.get("deviceName"))

    async def start(self) -> None:
        """Register the task and return; discovery runs inside it
        (app/poller_lifecycle.py)."""
        import asyncio
        self._task = asyncio.create_task(self._run(), name="govee-poller")

    async def stop(self) -> None:
        from .poller_lifecycle import reap
        self._stop.set()
        task, self._task = self._task, None
        await reap(task, "govee-poller")

    async def _poll_one(self, device_id: str, listing: dict[str, Any]) -> int:
        sku = str(listing.get("sku") or "")
        if not sku:
            log.debug("govee %s: no sku in listing — skipping", device_id)
            return 0
        state = await self._client.device_state(sku, device_id)
        payload = build_payload(device_id, listing, state, self._label(device_id))
        if payload is None:
            log.debug("govee %s: offline or empty — skipping", device_id)
            return 0
        print_ = fingerprint(payload)
        last = self._last_print.get(device_id)
        now = time.monotonic()
        if last and last[0] == print_ and now - last[1] < REPOST_AFTER_S:
            log.debug("govee %s: unchanged — skipping", device_id)
            return 0
        result = await ingest._do_ingest(payload)   # type: ignore[attr-defined]
        self._last_print[device_id] = (print_, now)
        a = payload.get("air", {})
        log.info("ingested Govee %s: co2=%s pm25=%s tempf=%s", device_id,
                 a.get("co2"), a.get("pm25"),
                 payload.get("indoor", {}).get("tempf"))
        return int((result or {}).get("inserted", 0))

    async def _tick(self) -> None:
        need = not self._devices or bool(self._unresolved)
        if need and (self._last_discover_mono < 0
                     or time.monotonic() - self._last_discover_mono >= REDISCOVER_AFTER_S):
            # A typed id the account did not list may appear later (a
            # monitor added to the account after the config was saved);
            # asked again every ten minutes, not every tick.
            await self._discover()
        rows, failures = 0, 0
        last_error: str | None = None
        if self._unresolved:
            failures += len(self._unresolved)
            last_error = f"device {self._unresolved[0]} is not on this Govee account"
        for device_id, listing in list(self._devices.items()):
            try:
                rows += await self._poll_one(device_id, listing)
            except Exception as e:                   # noqa: BLE001
                failures += 1
                log.warning("govee poll failed for %s: %s", device_id, e)
                last_error = str(e)
        if failures:
            # Partial failures report as failures (the AirGradient R11
            # rule): a clean record_success wiped the other device's
            # last_error (2.1 pre-release review BE-9).
            note = last_error or "unknown"
            if rows:
                total = len(self._devices) + len(self._unresolved)
                note = (f"{total - failures}/{total} "
                        f"device(s) ok; first failure: {note}")
            source_status.record_failure(SOURCE, note)
        else:
            source_status.record_success(SOURCE, rows=rows)

    async def _run(self) -> None:
        import asyncio
        try:
            await self._discover()
        except asyncio.CancelledError:
            raise
        except Exception:                            # noqa: BLE001
            log.exception("govee discovery failed; the tick retries")
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:                        # noqa: BLE001
                log.exception("govee tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass
