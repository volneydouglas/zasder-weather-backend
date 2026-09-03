"""Ecowitt cloud poller.

Pulls the latest reading for each device on an ecowitt.net account every N
seconds and feeds it through the same ingest path the SDR relays and the
Tempest / Davis cloud pollers use, so Ecowitt readings land in the standard
observations table.

This runs on the BACKEND for the same reason the Tempest poller does: the
vendor API is reachable from anywhere, and the backend is the always-on
half of this system. It exists because the gateway's own upload (Path G,
/ingest/ecowitt) is plain HTTP and cannot reach an HTTPS-only host without
a LAN forwarder; on a LAN backend the local path is still the one to use
(16 s cadence, no vendor cloud in the loop).

Device identity: the REAL gateway MAC from /device/list, not a synthetic
one — the cloud tells us the hardware address, so there is nothing to
invent. The local path never sees the MAC (only an MD5 PASSKEY) and keys
its rows EC:EC:xx, so a gateway pointed at both doors appears as two
devices. Documented, deliberate: users pick one path.

Timestamps: the API repeats the last reading until the station reports
again (its window is two hours), so a reading whose timestamp equals the
one last ingested is skipped rather than re-posted every minute.

Transform reuse: the cloud response is the gateway's "Customized" upload
seen through a different door — same sensors, same battery conventions,
same two rain families. The rain / battery / channel leaves are translated
into the wire vocabulary ecowitt.py already reads and handed to its pure
functions, so the tipping-gauge-beats-piezo rule, the 2.4 V array line and
the channel names have exactly one home.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, ecowitt, ingest, source_status
from .ecowitt_cloud_client import EcowittCloudClient, native, to_float


log = logging.getLogger("ecowitt-cloud-poller")

SOURCE = "ecowitt-cloud"
# The station uploads to ecowitt.net once a minute at best, and the doc
# publishes no request quota beyond a 45001 "over the limit" code — so the
# floor is half the data cadence, and the default is the cadence itself.
MIN_INTERVAL_S = 30
# One day is the largest window the history endpoint allows at 5-minute
# resolution; the bootstrap asks for exactly that.
BOOTSTRAP_WINDOW = timedelta(hours=24)


def _device_zone(meta: dict[str, Any], mac: str = "") -> timezone | Any:
    """The zone Ecowitt keeps this device's clock in, or UTC (logged once
    per bootstrap) when the list entry names none or names one this host
    does not know."""
    from zoneinfo import ZoneInfo
    name = (meta or {}).get("date_zone_id")
    if name:
        try:
            return ZoneInfo(str(name))
        except Exception:
            log.warning("ecowitt %s: unknown date_zone_id %r; bootstrapping "
                        "in UTC", mac, name)
    return timezone.utc


# ── leaf readers ─────────────────────────────────────────────────────────

def _group(data: dict[str, Any], name: str) -> dict[str, Any]:
    g = data.get(name)
    return g if isinstance(g, dict) else {}


def _leaf(group: dict[str, Any], key: str, kind: str = "plain") -> float | None:
    """Value of one {"time","unit","value"} leaf in the stored unit, or
    None when the leaf is absent, blank or non-numeric."""
    leaf = group.get(key)
    if not isinstance(leaf, dict):
        return None
    return native(kind, to_float(leaf.get("value")), leaf.get("unit"))


def _leaf_time(group: dict[str, Any], key: str) -> int | None:
    leaf = group.get(key)
    if not isinstance(leaf, dict):
        return None
    t = to_float(leaf.get("time"))
    return int(t) if t is not None and t > 0 else None


# ── wire-vocabulary translation (reuses ecowitt.py) ──────────────────────

_RAIN_KEYS = ("rain_rate", "hourly", "event", "daily", "weekly", "monthly",
              "yearly")
# API leaf → Ecowitt wire key, tipping gauge family.
_RAIN_TIPPING = {"rain_rate": "rainratein", "hourly": "hourlyrainin",
                 "event": "eventrainin", "daily": "dailyrainin",
                 "weekly": "weeklyrainin", "monthly": "monthlyrainin",
                 "yearly": "yearlyrainin"}
# …and the WS90 haptic family.
_RAIN_PIEZO = {"rain_rate": "rrain_piezo", "hourly": "hrain_piezo",
               "event": "erain_piezo", "daily": "drain_piezo",
               "weekly": "wrain_piezo", "monthly": "mrain_piezo",
               "yearly": "yrain_piezo"}

# battery.<leaf> → wire key. The doc names the sensor each leaf belongs to
# and its convention (volts / 1-is-low flag / 0-5 level); ecowitt._batteries
# already knows every one of these wire keys and its convention.
_BATTERY_WIRE = {
    "haptic_array_battery": "wh90batt",   # WS90, volts
    "sonic_array": "wh80batt",            # WS80, volts
    "wind_sensor": "wh68batt",            # WS68, volts
    "sensor_array": "wh65batt",           # WH65/WH24 array, flag
    "outdoor_t_rh_sensor": "wh26batt",    # WH26/WH32 outdoor T/RH, flag
    "t_rh_p_sensor": "wh25batt",          # WH25 indoor T/RH/P, flag
    "rainfall_sensor": "wh40batt",        # WH40 gauge, volts
    "lightning_sensor": "wh57batt",       # WH57, 0-5 level
    **{f"pm25_sensor_ch{i}": f"pm25batt{i}" for i in range(1, 5)},
    **{f"water_leak_sensor_ch{i}": f"leakbatt{i}" for i in range(1, 5)},
    **{f"temp_humidity_sensor_ch{i}": f"batt{i}" for i in range(1, 9)},
    **{f"soilmoisture_sensor_ch{i}": f"soilbatt{i}" for i in range(1, 5)},
    **{f"temperature_sensor_ch{i}": f"tf_batt{i}" for i in range(1, 5)},
}


def wire_form(data: dict[str, Any]) -> dict[str, Any]:
    """The rain, battery and channel leaves of a real_time `data` object
    re-spelled as the gateway's own upload keys, so ecowitt._rain /
    _batteries / _channels can read them. Values are already converted to
    the stored units (the per-leaf unit guard runs here)."""
    form: dict[str, Any] = {}
    for group_name, table in (("rainfall", _RAIN_TIPPING),
                              ("rainfall_piezo", _RAIN_PIEZO)):
        g = _group(data, group_name)
        for key in _RAIN_KEYS:
            v = _leaf(g, key, "rain")
            if v is not None:
                form[table[key]] = v
    batt = _group(data, "battery")
    for leaf_name, wire in _BATTERY_WIRE.items():
        v = _leaf(batt, leaf_name)
        if v is not None:
            form[wire] = v
    for i in range(1, 9):
        g = _group(data, f"temp_and_humidity_ch{i}")
        t = _leaf(g, "temperature", "temp")
        if t is not None:
            form[f"temp{i}f"] = t
        h = _leaf(g, "humidity")
        if h is not None:
            form[f"humidity{i}"] = h
        v = _leaf(_group(data, f"soil_ch{i}"), "soilmoisture")
        if v is not None:
            form[f"soilmoisture{i}"] = v
        v = _leaf(_group(data, f"temp_ch{i}"), "temperature", "temp")
        if v is not None:
            form[f"tf_ch{i}"] = v
        v = _leaf(_group(data, f"leaf_ch{i}"), "leaf_wetness")
        if v is not None:
            form[f"leafwetness_ch{i}"] = v
    leaks = _group(data, "water_leak")
    for i in range(1, 5):
        v = _leaf(leaks, f"leak_ch{i}")
        # 0 normal, 1 leaking, 2 OFFLINE — offline is silence, not a state.
        if v in (0.0, 1.0):
            form[f"leak_ch{i}"] = v
    return form


# ── the transform ────────────────────────────────────────────────────────

_TS_GROUPS = ("outdoor", "wind", "pressure", "indoor", "solar_and_uvi",
              "rainfall", "rainfall_piezo")


# A core group whose newest leaf lags the reading by more than this is the
# cloud repeating an offline sensor, not a measurement of this minute
# (R18 finding 2: the reading used to take the OUTDOOR temperature's time,
# so a stale array froze the whole station while wind, rain and pressure
# kept arriving and were thrown away as "already ingested").
STALE_GROUP_S = 15 * 60


def _group_time(data: dict[str, Any], name: str) -> int | None:
    g = _group(data, name)
    times = [t for t in (_leaf_time(g, k) for k in g) if t is not None]
    return max(times) if times else None


def reading_time(data: dict[str, Any]) -> int | None:
    """Epoch seconds of the reading: the newest leaf time across the core
    groups. Every group carries its own time; the reading is as fresh as
    the freshest sensor, and `stale_groups` names the ones that are not."""
    times = [t for t in (_group_time(data, g) for g in _TS_GROUPS)
             if t is not None]
    return max(times) if times else None


def stale_groups(data: dict[str, Any], ts: int) -> set[str]:
    """The core groups whose newest leaf lags `ts` by more than
    STALE_GROUP_S. Their values are omitted from the payload rather than
    re-stamped with a time they were not measured at."""
    out: set[str] = set()
    for g in _TS_GROUPS:
        t = _group_time(data, g)
        if t is not None and ts - t > STALE_GROUP_S:
            out.add(g)
    return out


def build_payload(mac: str, data: dict[str, Any],
                  name: str | None = None,
                  device: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Transform one real_time `data` object into an /ingest/custom payload.
    Returns None when the reading carries no usable timestamp or no data.

    Field names MUST match ingest._flatten(): mis-naming silently drops the
    value and the app shows the tile as missing while the cloud had the
    data (the lesson every poller in this directory records).

    Derives nothing the API already sends — dew point and feels-like come
    from the cloud; ingest fills feels-like only where it is absent.
    """
    ts = reading_time(data)
    if ts is None:
        return None
    ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    # Only the groups measured at (or near) this reading's time.
    stale = stale_groups(data, ts)
    if stale:
        data = {k: v for k, v in data.items() if k not in stale}

    out_g = _group(data, "outdoor")
    outdoor: dict[str, Any] = {}
    for key, dest in (("temperature", "tempf"), ("dew_point", "dew_point_f"),
                      ("feels_like", "feels_like")):
        v = _leaf(out_g, key, "temp")
        if v is not None:
            outdoor[dest] = v
    hum = _leaf(out_g, "humidity")
    if hum is not None:
        outdoor["humidity"] = round(hum)
    sol_g = _group(data, "solar_and_uvi")
    uv = _leaf(sol_g, "uvi")
    if uv is not None:
        outdoor["uv"] = uv
    solar = _leaf(sol_g, "solar", "solar")
    if solar is not None:
        outdoor["solar_wm2"] = solar

    in_g = _group(data, "indoor")
    indoor: dict[str, Any] = {}
    t_in = _leaf(in_g, "temperature", "temp")
    if t_in is not None:
        indoor["tempf"] = t_in
    h_in = _leaf(in_g, "humidity")
    if h_in is not None:
        indoor["humidity"] = round(h_in)

    wind_g = _group(data, "wind")
    wind: dict[str, Any] = {}
    for key, dest in (("wind_speed", "speed_mph"), ("wind_gust", "gust_mph")):
        v = _leaf(wind_g, key, "wind")
        if v is not None:
            wind[dest] = v
    direction = _leaf(wind_g, "wind_direction")
    if direction is not None:
        wind["direction"] = direction

    form = wire_form(data)
    # Tipping gauge beats piezo per field, rate feeds hourly_in — the one
    # rule in ecowitt._rain, not a copy of it.
    rain = {k: v for k, v in ecowitt._rain(form).items() if v is not None}

    press_g = _group(data, "pressure")
    pressure: dict[str, Any] = {}
    for key, dest in (("relative", "relative_inhg"), ("absolute", "absolute_inhg")):
        v = _leaf(press_g, key, "pressure")
        if v is not None:
            pressure[dest] = v

    # Lightning: `distance` is the nearest strike in miles and ITS leaf time
    # is when that strike happened; `count` is the running total for TODAY,
    # not an interval count, so it must not feed the accumulating
    # strike_count column (the hourlyrainin trap in another hat). Distance
    # and strike time are kept; the daily count is left out until a
    # column means the same thing.
    light_g = _group(data, "lightning")
    lightning: dict[str, Any] = {}
    dist = _leaf(light_g, "distance", "distance")
    if dist is not None:
        lightning["last_distance_mi"] = dist
        t_strike = _leaf_time(light_g, "distance")
        if t_strike is not None:
            lightning["last_strike_ms"] = t_strike * 1000

    batteries = ecowitt._batteries(form)
    extra = ecowitt._channels(form)

    device = device or {}
    dev: dict[str, Any] = {"id": mac}
    label = name or device.get("name")
    if isinstance(label, str) and label.strip():
        dev["name"] = label.strip()
    model = device.get("stationtype")
    if isinstance(model, str) and model.strip():
        dev["model"] = model.strip()
    # The outdoor array's own battery drives the battout flag the apps
    # render — same precedence as the local path (ecowitt.normalize).
    for k in ("wh90batt", "wh80batt", "wh68batt", "wh65batt", "wh24batt"):
        if k in batteries:
            dev["battery_outdoor"] = "normal" if batteries[k] else "low"
            break
    # Coordinates from the device list: without them a station gets no
    # forecast and no sunrise. ingest._payload_coords reads this shape.
    lat, lon = to_float(device.get("latitude")), to_float(device.get("longitude"))
    if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0):
        dev["coords"] = {"lat": lat, "lon": lon}

    payload: dict[str, Any] = {
        "device": dev,
        "timestamp_utc": ts_iso,
        "source": SOURCE,
    }
    for key, block in (("outdoor", outdoor), ("indoor", indoor),
                       ("wind", wind), ("rain", rain), ("pressure", pressure),
                       ("lightning", lightning), ("batteries", batteries),
                       ("extra", extra)):
        if block:
            payload[key] = block
    # Nothing but a timestamp (and battery flags) is not an observation.
    if not (outdoor or indoor or wind or rain or pressure or lightning or extra):
        return None
    return payload


def history_payloads(mac: str, data: dict[str, Any],
                     name: str | None = None,
                     device: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Pivot a /device/history `data` object (per-metric {"unit", "list":
    {epoch: value}}) into one real_time-shaped object per epoch and run
    each through build_payload. Chronological."""
    by_ts: dict[int, dict[str, dict[str, Any]]] = {}
    for group_name, group in data.items():
        if not isinstance(group, dict):
            continue
        for key, series in group.items():
            if not isinstance(series, dict):
                continue
            points = series.get("list")
            if not isinstance(points, dict):
                continue
            unit = series.get("unit")
            for epoch, value in points.items():
                t = to_float(epoch)
                if t is None or t <= 0:
                    continue
                leaf = {"time": str(int(t)), "unit": unit, "value": value}
                by_ts.setdefault(int(t), {}).setdefault(group_name, {})[key] = leaf
    out: list[dict[str, Any]] = []
    for ts in sorted(by_ts):
        p = build_payload(mac, by_ts[ts], name, device)
        if p is not None:
            out.append(p)
    return out


_MAC_RE = re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}")


def _normalize_mac(raw: Any) -> str | None:
    """AA:BB:CC:DD:EE:FF from any case, with or without colons; None for
    anything that is not a 6-byte MAC (ingest._format_mac passes junk
    through unchanged, and junk must not become a device key)."""
    mac = ingest._format_mac(str(raw or "").strip())  # type: ignore[attr-defined]
    return mac if mac and _MAC_RE.fullmatch(mac) else None


def parse_macs(csv: str | None) -> list[str]:
    """ECOWITT_MACS / the `macs` field: comma-separated, any case, with or
    without colons. Empty → every station on the account."""
    out: list[str] = []
    for part in (csv or "").split(","):
        m = _normalize_mac(part)
        if m and m not in out:
            out.append(m)
    return out


class EcowittCloudPoller:
    """Background task: poll ecowitt.net every N seconds for each device
    and ingest. `macs` restricts the account's list; `name` overrides the
    display name only when exactly one device is polled (one name cannot
    label two stations)."""

    def __init__(self, client: EcowittCloudClient, interval_s: int = 60,
                 macs: list[str] | str | None = None,
                 name: str | None = None) -> None:
        self._client = client
        self._interval_s = max(MIN_INTERVAL_S, int(interval_s))
        self._wanted = parse_macs(macs) if isinstance(macs, str) else [
            m for m in (_normalize_mac(x) for x in (macs or [])) if m]
        self._name = name
        # mac → the /device/list entry (name, coords, stationtype).
        self._devices: dict[str, dict[str, Any]] = {}
        self._last_ts: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _label(self, mac: str) -> str | None:
        if self._name and len(self._devices) == 1:
            return self._name
        return None

    async def _discover(self) -> None:
        """One /device/list read so every payload carries the station's
        name, model and coordinates, and so an unrestricted account polls
        every station on it. Best-effort: a failure here must not stop a
        poller that was handed explicit MACs."""
        try:
            listed = await self._client.list_devices()
        except Exception as e:
            log.warning("ecowitt device list failed: %s",
                        source_status.redact(str(e)))
            listed = []
        found: dict[str, dict[str, Any]] = {}
        for d in listed:
            if d.get("type") not in (None, 1, "1"):
                continue                      # cameras have no readings
            mac = _normalize_mac(d.get("mac"))
            if not mac:
                continue
            found[mac] = d
        if self._wanted:
            missing = [m for m in self._wanted if m not in found]
            if missing and listed:
                log.warning("ecowitt: configured MAC(s) not on this account: %s",
                            ", ".join(missing))
            self._devices = {m: found.get(m, {"mac": m}) for m in self._wanted}
        else:
            self._devices = found
            if listed and not found:
                log.warning("ecowitt: account has no weather station to poll")
        for mac, d in self._devices.items():
            log.info("ecowitt device %s: name=%s model=%s coords=%s,%s",
                     mac, d.get("name"), d.get("stationtype"),
                     d.get("latitude"), d.get("longitude"))

    async def bootstrap(self) -> None:
        """One-time backfill of the last day at 5-minute resolution — the
        AmbientWeather poller's precedent: rows go straight to storage,
        NOT through _do_ingest, so a day of stale readings cannot fire
        threshold alerts or be re-posted to Weather Underground. The
        (mac, dateutc) key makes a re-run (every credential change
        restarts the poller) a no-op."""
        end = datetime.now(timezone.utc)
        start = end - BOOTSTRAP_WINDOW
        for mac, meta in self._devices.items():
            try:
                # The history API reads offset-free date strings in the
                # DEVICE's zone (`date_zone_id`), and the client formats
                # what it is given, so the instants go over as that zone's
                # wall clock. Sent as UTC, a Phoenix station's "last 24 h"
                # ended seven hours in the future and skipped its earliest
                # seven (R18 finding 3).
                zone = _device_zone(meta, mac)
                data = await self._client.history(
                    mac, start.astimezone(zone), end.astimezone(zone))
                payloads = history_payloads(mac, data, self._label(mac), meta)
                rows: list[dict[str, Any]] = []
                for p in payloads:
                    flat = ingest._flatten(p)  # type: ignore[attr-defined]
                    if not flat:
                        continue
                    flat.pop("_raw_dateutc", None)
                    flat.pop("_feels_derived", None)
                    rows.append(flat)
                if rows:
                    # Same info shape _do_ingest hands upsert_device, minus
                    # lastData: a day-old row must not become the live view
                    # (upsert_device is monotonic on last_seen for exactly
                    # this reason; the first live tick fills it in).
                    last = payloads[-1]
                    explicit = last["device"].get("name")
                    auto = ingest._auto_device_name(last)  # type: ignore[attr-defined]
                    inner: dict[str, Any] = {"name": explicit or auto,
                                             "location": None,
                                             "source": SOURCE}
                    coords = ingest._payload_coords(last)  # type: ignore[attr-defined]
                    if coords is not None:
                        inner["coords"] = coords
                    await db.upsert_device(mac, {"name": explicit,
                                                 "auto_name": auto,
                                                 "info": inner})
                added = await db.insert_observations(mac, rows)
                log.info("ecowitt bootstrap %s: %d rows in the last day, %d new",
                         mac, len(rows), added)
            except Exception as e:
                log.warning("ecowitt bootstrap %s failed: %s", mac,
                            source_status.redact(str(e)))

    async def start(self) -> None:
        await self._discover()
        await self.bootstrap()
        self._task = asyncio.create_task(self._run(), name="ecowitt-cloud-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _poll_one(self, mac: str, meta: dict[str, Any]) -> int:
        """Returns rows STORED (not posted) — the history-write throttle
        legitimately rejects a reading that lands too soon, and reporting
        1 there would hide a source that has quietly stopped contributing."""
        data = await self._client.real_time(mac)
        payload = build_payload(mac, data, self._label(mac), meta) if data else None
        if payload is None:
            log.debug("no usable Ecowitt reading for %s — skipping ingest", mac)
            return 0
        ts = payload["timestamp_utc"]
        if self._last_ts.get(mac) == ts:
            # The cloud repeats the last reading until the station reports
            # again; re-posting it every cycle would be a lie about freshness.
            log.debug("ecowitt %s: reading %s already ingested", mac, ts)
            return 0
        result = await ingest._do_ingest(payload)  # type: ignore[attr-defined]
        self._last_ts[mac] = ts
        o = payload.get("outdoor", {})
        w = payload.get("wind", {})
        log.info("ingested Ecowitt %s: tempf=%s hum=%s wind=%s@%s", mac,
                 o.get("tempf"), o.get("humidity"),
                 w.get("speed_mph"), w.get("direction"))
        return int((result or {}).get("inserted", 0))

    async def _tick(self) -> None:
        if not self._devices:
            # Nothing to poll yet (list failed at start with no explicit
            # MACs) — try the account again rather than sleeping forever.
            await self._discover()
        rows = 0
        failures: list[str] = []
        for mac, meta in list(self._devices.items()):
            try:
                rows += await self._poll_one(mac, meta)
            except Exception as e:
                failures.append(f"{mac}: {e}")
                log.warning("Ecowitt poll failed for %s: %s", mac,
                            source_status.redact(str(e)))
        if failures and rows == 0:
            source_status.record_failure(SOURCE, "; ".join(failures))
        else:
            source_status.record_success(SOURCE, rows=rows)

    async def _run(self) -> None:
        log.info("ecowitt cloud poller running every %ds for %d device(s)",
                 self._interval_s, len(self._devices))
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                source_status.record_failure(SOURCE, str(e))
                log.warning("Ecowitt poll failed: %s",
                            source_status.redact(str(e)))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass
