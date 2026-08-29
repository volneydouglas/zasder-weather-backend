"""Station health watchdogs (1.8, Pillar D): the gap every vendor's
reviews complain about — "my station said fine while a sensor had been
dead for a week."

Three watchers, all edge-triggered through the smart-alert state table
(kind strings are namespaced, e.g. "battery:battout"):

- BATTERY: known low-battery conventions only — Ambient/Ecowitt-style
  `batt*` integer flags where 0 = low, and the firmware's
  `battery_outdoor: "low"`. No guessing at vendor voltage scales: a
  convention we can't read is silence, not a claim.
- SENSOR WENT QUIET: the device is reporting but a field that used to
  carry readings has been null for ≥3 h (dead solar head, unplugged
  T/H probe). Last-seen stamps ride server_kv, refreshed for free when
  the reading is present — the expensive scan-back query runs at most
  once per field, on first sight.
- FLATLINE: a stuck sensor telling the same lie for hours — humidity
  pegged at exactly 100 or 0 for 6 h, or a day with not one gust above
  zero (a seized anemometer; calm desert NIGHTS exist, calm desert
  DAYS-and-nights don't). Checked hourly, aggregates bounded to the
  window.

Recovery messages fire once when the condition clears, so the fix is
as visible as the failure.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

from . import db

log = logging.getLogger("health")

_SENSOR_FIELDS = ("tempf", "humidity", "windspeedmph", "solarradiation",
                  "baromrelin", "uv")
_SENSOR_QUIET_MS = 3 * 3_600_000
_FLATLINE_CHECK_MS = 3_600_000            # hourly
_HUM_FLAT_MS = 6 * 3_600_000
_WIND_FLAT_MS = 24 * 3_600_000
# A flatline window counts as covered if its oldest reading falls within
# this margin of the window start (stations miss a poll now and then).
_COVER_MARGIN_MS = 30 * 60_000

_BATT_LOW_FIELDS = ("battout", "battin", "batt1", "batt2", "batt3",
                    "batt4", "batt5", "batt6", "batt7", "batt8",
                    "wh57batt", "wh65batt", "batt_co2", "batt_25",
                    # 1.9: the AWN-native name the Ecowitt adapter maps
                    # wh57batt onto (AWN cloud rows use it directly too),
                    # plus every flag the adapter's voltage/binary
                    # normalizer emits under its vendor name — a low WH40
                    # beside a healthy WS90 array otherwise alerted nobody
                    # (CodeRabbit, PR #33).
                    "batt_lightning", "wh40batt", "wh68batt", "wh80batt",
                    "wh90batt")

_last_flatline_ms: dict[str, int] = {}


def _reset_for_tests() -> None:
    _last_flatline_ms.clear()


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def battery_low_fields(last: dict[str, Any]) -> list[str]:
    """Pure: which battery flags read LOW under conventions we trust."""
    low = []
    for f in _BATT_LOW_FIELDS:
        v = last.get(f)
        if v is None:
            continue
        fv = _f(v)
        if fv is not None and fv == 0.0:
            low.append(f)
    if str(last.get("battery_outdoor") or "").strip().lower() == "low":
        low.append("battery_outdoor")
    return low


async def check(cfg, devices: list[dict[str, Any]], now_ms: int,
                deliver) -> None:
    states = await db.get_smart_alert_states()
    for d in devices:
        last = d.get("lastData") or {}
        if not last:
            continue
        obs_ms = last.get("dateutc")
        obs_ms = int(obs_ms) if isinstance(obs_ms, (int, float)) else now_ms
        # A device that is itself stale belongs to the device-down alert;
        # sensor-level claims about it would be noise on noise.
        if now_ms - obs_ms > 30 * 60_000:
            continue
        mac = d["mac"]
        name = d.get("name") or mac

        # ── battery ────────────────────────────────────────────────────
        low = set(battery_low_fields(last))
        for f in _BATT_LOW_FIELDS + ("battery_outdoor",):
            kind = f"battery:{f}"
            prev = states.get((mac, kind), 0)
            if f in low and not prev:
                title = f"{name}: Sensor battery low"
                body = (f"The '{f}' battery flag reads low. Swap it soon — "
                        f"a dead sensor fails silently.")
                if await deliver(cfg, f"[Zasder Weather] {title}", body,
                                 title, body,
                                 email_ok=cfg.email_scope == "all",
                                 kind="battery", mac=mac):
                    await db.upsert_smart_alert_state(mac, kind, 1, now_ms)
            elif f not in low and prev and last.get(f) is not None:
                # The docstring always promised recovery notices; the clear
                # edge used to reset silently (R7 finding 5). Info tier —
                # good news never breaks quiet hours. Persist-after-deliver
                # like every other edge here.
                title = f"{name}: Sensor battery recovered"
                if await deliver(cfg, f"[Zasder Weather] {title}",
                                 f"The '{f}' battery flag reads normal again.",
                                 title, "Battery recovered",
                                 email_ok=cfg.email_scope == "all",
                                 kind="battery_recovered", mac=mac):
                    await db.upsert_smart_alert_state(mac, kind, 0, now_ms)

        # ── sensor went quiet ─────────────────────────────────────────
        key = f"health.last_seen.{mac}"
        raw = await db.get_kv(key)
        try:
            seen = json.loads(raw) if raw else {}
            if not isinstance(seen, dict):
                seen = {}
        except ValueError:
            seen = {}
        dirty = False
        for f in _SENSOR_FIELDS:
            kind = f"sensor:{f}"
            prev = states.get((mac, kind), 0)
            if _f(last.get(f)) is not None:
                if seen.get(f) != obs_ms:
                    seen[f] = obs_ms
                    dirty = True
                if prev:
                    title = f"{name}: {f} is reporting again"
                    # Persist only after delivery is handled — the release
                    # invariant (reviewer P2 family): a transient SMTP/APNs
                    # failure must retry next tick, not vanish.
                    if await deliver(cfg, f"[Zasder Weather] {title}",
                                     f"The {f} sensor recovered.",
                                     title, "Sensor recovered",
                                     email_ok=cfg.email_scope == "all",
                                     kind="sensor_recovered", mac=mac):
                        await db.upsert_smart_alert_state(mac, kind, 0, now_ms)
                continue
            last_seen = seen.get(f)
            if last_seen is None:
                # Never seen this field carry a value: a station without
                # the sensor, not a dead one. Absent is not broken.
                continue
            if now_ms - int(last_seen) >= _SENSOR_QUIET_MS and not prev:
                hours = (now_ms - int(last_seen)) / 3_600_000
                title = f"{name}: {f} sensor went quiet"
                body = (f"The station is reporting, but {f} has carried no "
                        f"reading for {hours:.0f} h. Check that sensor.")
                if await deliver(cfg, f"[Zasder Weather] {title}", body,
                                 title, body,
                                 email_ok=cfg.email_scope == "all",
                                 kind="sensor_quiet", mac=mac):
                    await db.upsert_smart_alert_state(mac, kind, 1, now_ms)
        if dirty:
            await db.set_kv(key, json.dumps(seen))

        # ── flatline (hourly) ─────────────────────────────────────────
        if now_ms - _last_flatline_ms.get(mac, 0) < _FLATLINE_CHECK_MS:
            continue
        _last_flatline_ms[mac] = now_ms
        hum = await db.field_min_max(mac, "humidity",
                                     now_ms - _HUM_FLAT_MS, now_ms)
        kind = "flatline:humidity"
        prev = states.get((mac, kind), 0)
        # Coverage gate: the pegged value must span (most of) the window,
        # not just the few rows a freshly-online station has produced.
        flat = (hum is not None and hum[0] == hum[1]
                and hum[0] in (0.0, 100.0)
                and hum[2] <= now_ms - _HUM_FLAT_MS + _COVER_MARGIN_MS)
        if flat and not prev:
            title = f"{name}: Humidity sensor looks stuck"
            body = (f"Humidity has read exactly {hum[0]:g}% for six hours "
                    f"straight — pegged sensors lie confidently.")
            if await deliver(cfg, f"[Zasder Weather] {title}", body,
                             title, body,
                             email_ok=cfg.email_scope == "all",
                             kind="flatline", mac=mac):
                await db.upsert_smart_alert_state(mac, kind, 1, now_ms)
        elif not flat and prev:
            await db.upsert_smart_alert_state(mac, kind, 0, now_ms)

        gust = await db.field_min_max(mac, "windgustmph",
                                      now_ms - _WIND_FLAT_MS, now_ms)
        kind = "flatline:wind"
        prev = states.get((mac, kind), 0)
        flat = (gust is not None and gust[1] == 0.0
                and gust[2] <= now_ms - _WIND_FLAT_MS + _COVER_MARGIN_MS)
        if flat and not prev:
            title = f"{name}: Anemometer may be seized"
            body = ("Not a single gust above zero in 24 hours. Calm nights "
                    "happen; a full windless day usually means the cups "
                    "aren't turning.")
            if await deliver(cfg, f"[Zasder Weather] {title}", body,
                             title, body,
                             email_ok=cfg.email_scope == "all",
                             kind="flatline", mac=mac):
                await db.upsert_smart_alert_state(mac, kind, 1, now_ms)
        elif not flat and prev:
            await db.upsert_smart_alert_state(mac, kind, 0, now_ms)
