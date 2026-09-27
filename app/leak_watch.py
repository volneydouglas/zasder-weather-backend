"""Water leak sensors (2.5, C2).

Ecowitt WH55 leak detectors report on channels 1 to 4, ingested since 1.9
as `leak1`-`leak4` (0 dry, 1 leaking; the cloud poller drops its 2 =
offline, and anything else here is read as no claim at all). They were
stored and never alerted on, which for a sensor whose only job is to say
"there is water on the floor" is the whole feature missing.

A leak is `warning` tier: it breaks quiet hours and arrives Time Sensitive,
because water under a water heater at 3 a.m. is exactly the alert that
must not wait for morning. The dry-again edge is `info`.

Deliberately NOT behind the smart-alerts switch. That switch gates DERIVED
weather opinions (frost, heat, pressure drops) and is off by default on a
new install; a leak detector is a sensor the owner bought to be told, and
an alert that is silently off by default is the failure this module
exists to end. It runs whenever any alert channel is open, like the
device-down watch.

Edge-triggered through smart_alert_states (kind `leak:<channel>`), persist
after deliver, like every other edge in the monitor.
"""
from __future__ import annotations

from typing import Any

from . import db

CHANNELS = (1, 2, 3, 4)
# A leaking reading older than this belongs to the device-down alert.
_STALE_MS = 30 * 60_000


def leak_channels(last: dict[str, Any]) -> dict[int, bool]:
    """Pure: channel → leaking, for the channels that made a claim."""
    out: dict[int, bool] = {}
    for ch in CHANNELS:
        v = last.get(f"leak{ch}")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v == 1:
            out[ch] = True
        elif v == 0:
            out[ch] = False
    return out


async def check(cfg, devices: list[dict[str, Any]], now_ms: int,
                deliver) -> None:
    states = await db.get_smart_alert_states()
    for d in devices:
        last = d.get("lastData") or {}
        channels = leak_channels(last)
        if not channels:
            continue
        obs_ms = last.get("dateutc")
        obs_ms = int(obs_ms) if isinstance(obs_ms, (int, float)) else now_ms
        if now_ms - obs_ms > _STALE_MS:
            continue
        mac = d["mac"]
        name = d.get("name") or mac
        for ch, leaking in channels.items():
            kind = f"leak:{ch}"
            prev = states.get((mac, kind), 0)
            where = f"leak sensor {ch}" if len(channels) > 1 else "the leak sensor"
            if leaking and not prev:
                title = f"{name}: Water leak detected"
                body = (f"Water detected at {where}. Check it now; this "
                        f"alert repeats only when it dries and gets wet again.")
                if await deliver(cfg, f"[Zasder Weather] {title}", body,
                                 title, f"Water detected at {where}.",
                                 kind="leak", mac=mac):
                    await db.upsert_smart_alert_state(mac, kind, 1, now_ms)
            elif not leaking and prev:
                title = f"{name}: Leak sensor dry again"
                body = f"{where[0].upper() + where[1:]} reads dry again."
                if await deliver(cfg, f"[Zasder Weather] {title}", body,
                                 title, body, kind="leak_cleared", mac=mac):
                    await db.upsert_smart_alert_state(mac, kind, 0, now_ms)
