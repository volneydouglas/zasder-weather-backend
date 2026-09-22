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


# ── source watchdog (2.2) ─────────────────────────────────────────────────
# A configured cloud poller that keeps failing used to be invisible: the
# station went quiet and looked like dead hardware. One alert per episode
# names the vendor and the kind of failure; one more says it recovered.
# State rides in smart_alert_state under a pseudo-MAC so the recovery edge
# survives restarts the way the sensor edges do.

_SOURCE_KIND = "source_down"


def _source_key(name: str) -> str:
    return f"source:{name}"


def _spell_duration(ms: int) -> str:
    """"3 hours", "45 minutes", "1.5 days". Round numbers on purpose:
    nobody wants 2.317 hours in a sentence, and "60 minutes" is an hour
    said the long way."""
    minutes = max(1, int(round(ms / 60_000)))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes / 60
    if hours < 36:
        # Nearest half hour, so 90 minutes is not rounded away to "2
        # hours" and 3 hours does not arrive as "3.0".
        halves = round(hours * 2) / 2
        if halves == int(halves):
            n = int(halves)
            return f"{n} hour{'s' if n != 1 else ''}"
        return f"{halves} hours"
    days = hours / 24
    if abs(days - round(days)) < 0.25:
        n = int(round(days))
        return f"{n} day{'s' if n != 1 else ''}"
    return f"{days:.1f} days"


def _outage_ms(runs: list, now_ms: int) -> int:
    """How long the most recent unhealthy stretch lasted, reading back
    from the end of the record. Stops at the first healthy run, so a
    source that failed, recovered and failed again reports THIS outage
    and not the sum of both."""
    total = 0
    for r in reversed(runs):
        if r["verdict"] in ("ok", "unknown"):
            if total:
                break
            continue
        total += max(0, int(r["until_ms"]) - int(r["from_ms"]))
    return total


def day_summary_line(label: str, summary: dict) -> str:
    """One sentence about the last 24 hours, for the not-reporting mail.

    Never a percentage of a day the server did not see: `covered_ms` is
    the honest denominator, and when it is small the sentence says so
    rather than dressing up an hour of data as a day."""
    from . import source_history as _sh
    covered = int(summary.get("covered_ms") or 0)
    if covered < 3_600_000:
        return (f"There is less than an hour of health history for "
                f"{label}, so there is nothing useful to say about the "
                f"last day yet.")
    totals = summary.get("totals_ms") or {}
    ok = int(totals.get(_sh.OK) or 0)
    bits = []
    for verdict, phrase in ((_sh.VENDOR, "the service not answering"),
                            (_sh.DEVICE, "the station reporting nothing"),
                            (_sh.OURS, "trouble on this server")):
        span = int(totals.get(verdict) or 0)
        if span >= 60_000:
            bits.append(f"{_spell_duration(span)} of {phrase}")
    window = _spell_duration(covered)
    if not bits:
        return f"Over the last {window} of records, {label} was fine until now."
    tail = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
    return (f"Over the last {window} of records, {label} was working for "
            f"{_spell_duration(ok)} of it, with {tail}.")


def source_down_copy(label: str, kind: str, hours: float,
                     last_error: str | None) -> tuple[str, str]:
    """Title and body for a failing source, honest about whose problem it is."""
    err = f" ({last_error})" if last_error else ""
    h = f"{hours:.0f} h" if hours >= 1 else f"{hours * 60:.0f} min"
    if kind == "credentials":
        return (f"{label} is rejecting the saved credentials",
                f"{label} has refused this server's credentials for {h}{err}. "
                f"Check the key under Settings → Data & Integrations. "
                f"Your station is fine; nothing arrives until the key is.")
    if kind == "rate_limit":
        return (f"{label} is rate-limiting this server",
                f"{label} has been turning requests away for {h}{err}. "
                f"Readings resume when their quota resets.")
    if kind == "upstream":
        return (f"{label}'s service is not answering",
                f"{label}'s service has not answered this server for {h}{err}. "
                f"Your station and this server are fine; readings resume "
                f"on their own when {label} does.")
    return (f"Readings from {label} are not being stored",
            f"{label} answers, but this server has failed to store its "
            f"readings for {h}{err}. This one is on the server.")


async def check_sources(cfg, now_ms: int, deliver,
                        quiet_minutes: float | None = None) -> None:
    from . import source_history, source_status
    from .config import settings

    # 2.4 item 3: before anything is decided, this tick's verdict per
    # source goes into the rolling 24 hour record. It is written even
    # when the watchdog itself is switched off (`source_alert_minutes`
    # zero), because the record is what answers "what happened
    # overnight" afterwards, and a record with holes in it where somebody
    # had alerts off is worse than useless.
    try:
        for src in source_status.snapshot():
            if src.get("configured") and src.get("label") is not None:
                await source_history.record(
                    src["name"], source_history.verdict_for(now_ms, src), now_ms)
    except Exception:
        log.exception("source health record failed")

    minutes = settings.source_alert_minutes if quiet_minutes is None else quiet_minutes
    if not minutes or minutes <= 0:
        return
    quiet_ms = int(minutes * 60_000)
    states = await db.get_smart_alert_states()
    for src in source_status.snapshot():
        name = src["name"]
        label = src.get("label")
        if not src["configured"] or label is None:
            continue
        key = _source_key(name)
        prev = states.get((key, _SOURCE_KIND), 0)
        since = src.get("failing_since_ms")
        failing = src["consecutive_failures"] > 0 and since is not None
        if failing and not prev and now_ms - int(since) >= quiet_ms:
            hours = (now_ms - int(since)) / 3_600_000
            kind = src.get("last_error_kind") or "ours"
            title, body = source_down_copy(label, kind, hours, src.get("last_error"))
            # 2.4 item 3: say what the last day looked like, because "it
            # has been down for two hours" and "it has been flapping all
            # day" are different problems and the first sentence should
            # not have to be asked for twice.
            try:
                runs = await source_history.history(name, now_ms)
                body += "\n\n" + day_summary_line(
                    label, source_history.summarise(runs, now_ms))
            except Exception:
                log.exception("source day summary failed")
            # Same channel rule as device-down: an outage is the one thing a
            # device_down email scope exists for.
            if await deliver(cfg, f"[Zasder Weather] {title}", body, title, body,
                             email_ok=True, kind=_SOURCE_KIND, mac=None):
                await db.upsert_smart_alert_state(key, _SOURCE_KIND, 1, now_ms)
        elif prev and not failing and src["last_success_ms"] is not None:
            title = f"{label} is answering again"
            body = f"{label}'s readings are arriving again."
            # And how long it was out, which is the first thing anyone
            # wants from a recovery notice.
            try:
                runs = await source_history.history(name, now_ms)
                gone = _outage_ms(runs, now_ms)
                if gone:
                    body += f" It was out for {_spell_duration(gone)}."
            except Exception:
                log.exception("source recovery duration failed")
            if await deliver(cfg, f"[Zasder Weather] {title}", body, title, body,
                             email_ok=True, kind="source_recovered", mac=None):
                await db.upsert_smart_alert_state(key, _SOURCE_KIND, 0, now_ms)

