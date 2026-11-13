"""WeatherLink v2 cloud poller.

Pulls live Davis ISS readings from the WeatherLink cloud API every
N seconds and feeds them into the same /ingest/custom path the SDR
relays use — so Davis observations land in the standard observations
table identified by a synthetic MAC and the iOS app sees them
alongside everything else.

This exists because the Davis 6313 Console exposes NO local API
(connection refused on port 80/22222/etc) and because rtldavis on an
RTL-SDR couldn't lock onto the ISS RF link with this specific
console+ISS pairing (zero packets decoded even with -tr 255 -u after
30+ minutes). The cloud has perfect data (rx_state=0,
reception_day=100%, packets_missed_day=0), so cloud-polling is the
working path. If the SDR side ever starts working, both will UPSERT
into the same synthetic-MAC device row (last-write-wins).

Synthetic MAC scheme matches sdr-relay / davis-relay:
  5D:5D:05:HH:HH:HH where 05 = Davis type tag, HH:HH:HH = transmitter id
"""
from __future__ import annotations

from .poller_lifecycle import reap
import asyncio
import time
import logging
from typing import Any

from . import ingest
from .config import settings
from . import source_status
from .weatherlink_client import WeatherLinkClient


log = logging.getLogger("wl-poller")


# Davis WeatherLink sensor_type → handler. Davis cloud rotates between
# 43 and 46 for the VP2 ISS depending on firmware/console state — both
# carry the same field set (temp/hum/wind/rain/uv/solar). Either one
# is treated as ISS data. Drop-of-the-other was a silent data-loss
# bug pre-2026-05-22 — confirmed live when sensor_type flipped from
# 43 → 46 mid-day and 700+ observations vanished.
SENSOR_TYPES_ISS    = {43, 46}
SENSOR_TYPE_INDOOR  = 365      # 6313 built-in indoor temp+hum
SENSOR_TYPE_BAROM   = 242      # 6313 built-in barometer
# 2.8.2: the sensor_type set above is the Vantage Pro2 ISS as the 6313
# console reports it. A Vantage Vue behind a WeatherLink Live has another
# sensor_type (Davis's catalog has one per ISS model, with and without a
# fan) and its record came through as "no ISS data": success with zero
# rows, a green check in the app and no station, silently, for everyone
# who did not own the maintainer's exact hardware (Thierry, 2026-11-12).
# The DATA STRUCTURE is the stable name for "this is the ISS record"
# (weatherlink.github.io/v2-api/data-structure-types):
#   10 ISS current conditions, WeatherLink Live
#   23 ISS current conditions, WeatherLink Console
# and the console's own barometer and inside sensors are 19 and 21.
# (The cloud's 1 and 2 are the WeatherLinkIP / Vantage Connect combined
# records with their own field names, not the WLL local API's type 1.)
# A record that names none of these but carries an outdoor temperature
# and a wind or rain field is treated as the ISS too, so the next catalog
# entry Davis adds is not a repeat. The WLL's barometer and inside
# sensors are structures 3 and 4 locally, but through the cloud Davis's
# 11 and 12 are GENERIC non-ISS records (CodeRabbit, PR #153), so those
# two are told apart by the fields they carry, `bar_sea_level` and
# `temp_in`, never by a structure number. Several records can look like
# an ISS (an auxiliary transmitter reports as structure 10 as well, and
# `sensors[]` has no documented order): the one with the most of
# temperature, humidity, wind, rain, solar and UV wins, a known ISS
# sensor_type breaks a tie, and the lowest transmitter id breaks the
# rest, so the slot never goes to whichever record happened to come first.
ISS_STRUCTURE_TYPES    = {10, 23}
BAROM_STRUCTURE_TYPES  = {19}
INDOOR_STRUCTURE_TYPES = {21}
_ISS_SHAPE_KEYS = ("wind_speed_last", "wind_speed_avg_last_2_min",
                   "rain_rate_last", "rainfall_daily", "rainfall_day_in")
_ISS_SCORE_KEYS = (("temp",), ("hum",),
                   ("wind_speed_last", "wind_speed_avg_last_2_min"),
                   ("rainfall_daily", "rainfall_day_in", "rainfall_daily_in"),
                   ("solar_rad",), ("uv_index",))
# rain_size (WLL records) -> inches per bucket tip, as wll-poller/poller.py.
_RAIN_SIZE_IN: dict[int, float] = {1: 0.01, 2: 0.2 / 25.4, 3: 0.1 / 25.4, 4: 0.001}


def classify_sensor(s: dict[str, Any]) -> str | None:
    """'iss', 'indoor', 'barom' or None for one `sensors[]` entry of a
    /v2/current answer: by sensor_type where the type is known, by
    data_structure_type otherwise, and by the record's own fields last."""
    stype = s.get("sensor_type")
    dst = s.get("data_structure_type")
    rows = s.get("data") or []
    r = rows[0] if rows and isinstance(rows[0], dict) else {}
    if stype in SENSOR_TYPES_ISS or dst in ISS_STRUCTURE_TYPES:
        return "iss"
    if stype == SENSOR_TYPE_INDOOR or dst in INDOOR_STRUCTURE_TYPES or r.get("temp_in") is not None:
        return "indoor"
    if stype == SENSOR_TYPE_BAROM or dst in BAROM_STRUCTURE_TYPES or r.get("bar_sea_level") is not None:
        return "barom"
    if r.get("temp") is not None and any(k in r for k in _ISS_SHAPE_KEYS):
        return "iss"
    return None


def iss_score(s: dict[str, Any]) -> tuple[int, int, int]:
    """Higher sorts first: how much of an ISS the record is, whether its
    sensor_type is a known ISS, and the lowest transmitter id last."""
    rows = s.get("data") or []
    r = rows[0] if rows and isinstance(rows[0], dict) else {}
    fields = sum(1 for keys in _ISS_SCORE_KEYS if any(r.get(k) is not None for k in keys))
    known = 1 if s.get("sensor_type") in SENSOR_TYPES_ISS else 0
    try:
        tx = int(r.get("tx_id") or r.get("txid") or 0)
    except (TypeError, ValueError):
        tx = 0
    return (fields, known, -tx)


def describe_sensors(current: dict[str, Any]) -> str:
    """What the station answered with, for the log line that says no ISS
    record was recognised: `sensor_type/data_structure_type` pairs."""
    pairs = []
    for s in current.get("sensors") or []:
        pairs.append(f"{s.get('sensor_type')}/{s.get('data_structure_type')}")
    return ", ".join(pairs) if pairs else "no sensors at all"


def _rain_in(iss: dict[str, Any], *names_in: str, clicks: str) -> float | None:
    """A rain total in inches from whichever spelling the record has: the
    console's `_in` fields first (either spelling Davis has used), else a
    WLL record's bucket-tip count times its rain_size."""
    for n in names_in:
        v = iss.get(n)
        if v is not None:
            return float(v)
    v = iss.get(clicks)
    if v is None:
        return None
    try:
        return float(v) * _RAIN_SIZE_IN.get(int(iss.get("rain_size") or 1), 0.01)
    except (TypeError, ValueError):
        return None


def _synth_mac(tx_id: int) -> str:
    """Mirror the sdr-relay / davis-relay synthetic-MAC scheme so cloud
    + SDR (if it ever works) post to the same device row."""
    return f"5D5D05{(tx_id >> 16) & 0xFF:02X}{(tx_id >> 8) & 0xFF:02X}{tx_id & 0xFF:02X}"


def build_payload(station: dict[str, Any],
                  current: dict[str, Any]) -> dict[str, Any] | None:
    """Transform a WeatherLink /v2/current response into a single
    /ingest/custom payload. Walks the sensor list, extracts outdoor,
    indoor, and barometer fields, and packs them in the AWN-style
    field names the backend's _flatten() expects.

    Returns None if the response has no usable sensor data."""
    sensors = current.get("sensors") or []
    iss_data: dict[str, Any] = {}
    indoor_data: dict[str, Any] = {}
    barom_data: dict[str, Any] = {}
    tx_id = 1   # default; overwritten if ISS data carries tx_id

    candidates: list[dict[str, Any]] = []
    for s in sensors:
        rows = s.get("data") or []
        if not rows:
            continue
        r = rows[0]
        kind = classify_sensor(s)
        if kind == "iss":
            candidates.append(s)
        elif kind == "indoor" and (not indoor_data or r.get("temp_in") is not None):
            indoor_data = r
        elif kind == "barom" and (not barom_data or r.get("bar_sea_level") is not None):
            barom_data = r
    if candidates:
        # Eligibility first, then the score: a record without a temperature
        # is not the ISS whatever else it carries, so it must not win the
        # slot and then be rejected with the rest unconsidered (CodeRabbit,
        # PR #153). With no temperature anywhere, the best of what there
        # is still goes through, as before.
        with_temp = [s for s in candidates if s["data"][0].get("temp") is not None]
        best = max(with_temp or candidates, key=iss_score)
        r = best["data"][0]
        iss_data = r
        # The console spells the transmitter `tx_id`; a WeatherLink Live
        # record spells it `txid` (read live from one on 2026-11-12).
        # R6: a truthy non-numeric id raised out of build_payload and
        # dropped the ENTIRE cycle's reading — fall back to the default
        # instead, matching this module's own "mis-naming silently
        # drops data" lesson.
        if r.get("tx_id") or r.get("txid"):
            try:
                tx_id = int(r.get("tx_id") or r.get("txid"))
            except (TypeError, ValueError):
                pass

    if not iss_data:
        log.debug("no ISS sensor in WeatherLink response")
        return None

    # Use the freshest sensor timestamp as the observation time so the
    # backend stores it correctly even if our poll cadence drifts.
    # The console stamps each record (`ts`); a WLL-shaped record may carry
    # none, in which case the answer's own stamp (`generated_at`) is the
    # observation time rather than dropping the whole reading.
    ts_ms = (iss_data.get("ts") or current.get("generated_at") or 0) * 1000
    if not ts_ms:
        return None
    from datetime import datetime, timezone
    ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) \
        .isoformat(timespec="seconds")

    # Field names MUST match the backend ingest._flatten() schema:
    #   outdoor: tempf, feels_like, dew_point_f, humidity, uv, solar_wm2
    #   wind:    speed_mph, gust_mph, direction
    #   rain:    hourly_in, daily_in, monthly_in, yearly_in
    #   indoor:  tempf, humidity, pressure_inhg
    #   pressure: relative_inhg
    # Mis-naming silently drops the field — the iOS app then shows the
    # tile as missing even though the cloud had the data.
    outdoor: dict[str, Any] = {}
    if iss_data.get("temp") is not None:
        outdoor["tempf"] = float(iss_data["temp"])
    if iss_data.get("hum") is not None:
        outdoor["humidity"] = round(float(iss_data["hum"]))
    if iss_data.get("dew_point") is not None:
        outdoor["dew_point_f"] = float(iss_data["dew_point"])
    # Davis's "heat_index" matches the NWS "feels like" we expose.
    if iss_data.get("heat_index") is not None:
        outdoor["feels_like"] = float(iss_data["heat_index"])
    if iss_data.get("uv_index") is not None:
        outdoor["uv"] = float(iss_data["uv_index"])
    if iss_data.get("solar_rad") is not None:
        outdoor["solar_wm2"] = float(iss_data["solar_rad"])

    wind: dict[str, Any] = {}
    # Prefer the 2-min rolling average over wind_speed_last (single 2.5s
    # sample, often 0 between gusts) — gives the iOS tile a steadier
    # value that actually represents conditions. wind_speed_hi_last_2_min
    # is the 2-min peak (= the gust tile).
    speed = (iss_data.get("wind_speed_avg_last_2_min")
             if iss_data.get("wind_speed_avg_last_2_min") is not None
             else iss_data.get("wind_speed_last"))
    if speed is not None:
        wind["speed_mph"] = float(speed)
    if iss_data.get("wind_speed_hi_last_2_min") is not None:
        wind["gust_mph"] = float(iss_data["wind_speed_hi_last_2_min"])
    # Use the scalar-averaged direction from the same 2-min window when
    # available — single-sample wind_dir_last reports the vane's resting
    # direction during calms (gives the wind rose spurious dominant
    # directions). The scalar avg respects speed weighting.
    direction = (iss_data.get("wind_dir_scalar_avg_last_2_min")
                 if iss_data.get("wind_dir_scalar_avg_last_2_min") is not None
                 else iss_data.get("wind_dir_last"))
    if direction is not None:
        wind["direction"] = round(float(direction))

    rain: dict[str, Any] = {}
    yearly = _rain_in(iss_data, "rainfall_year_in", clicks="rainfall_year")
    if yearly is not None:
        # Add the operator-supplied baseline so a mid-year-installed
        # ISS reports the actual year-to-date total, not just rain
        # since pairing. (Davis cloud's rainfall_year_in resets to 0
        # at install + only counts new tips from then on.)
        rain["yearly_in"] = yearly + settings.weatherlink_yearly_rain_baseline_in
    # Davis cloud reports daily + hourly directly. Setting them here
    # explicitly stops backend/app/main.py:get_current() from invoking
    # its rain-rollup enrichment (which would otherwise compute
    # daily/weekly/monthly from yearlyrainin DELTAS — broken for a
    # baselined value since the "yesterday" yearlyrainin is fictional).
    daily = _rain_in(iss_data, "rainfall_day_in", "rainfall_daily_in", clicks="rainfall_daily")
    if daily is not None:
        rain["daily_in"] = daily
    hourly = _rain_in(iss_data, "rainfall_last_60_min_in", clicks="rainfall_last_60_min")
    if hourly is not None:
        rain["hourly_in"] = hourly
    # A WLL record carries the month too; the console's does not, and the
    # backend's rollup fills it in from the yearly history either way.
    monthly = _rain_in(iss_data, "rainfall_monthly_in", clicks="rainfall_monthly")
    if monthly is not None:
        rain["monthly_in"] = monthly
    # Davis cloud doesn't expose weekly_in / monthly_in inches; the
    # backend's rollup will still compute them from now-baselined
    # yearly history (which is correct as soon as all stored rows
    # carry the baseline value).

    indoor: dict[str, Any] = {}
    if indoor_data.get("temp_in") is not None:
        indoor["tempf"] = float(indoor_data["temp_in"])
    if indoor_data.get("hum_in") is not None:
        indoor["humidity"] = round(float(indoor_data["hum_in"]))
    if barom_data.get("bar_sea_level") is not None:
        indoor["pressure_inhg"] = float(barom_data["bar_sea_level"])

    pressure: dict[str, Any] = {}
    if barom_data.get("bar_sea_level") is not None:
        pressure["relative_inhg"] = float(barom_data["bar_sea_level"])

    payload: dict[str, Any] = {
        "device": {
            "id":       _synth_mac(tx_id),
            "name":     settings.weatherlink_name or station.get("station_name"),
            "location": settings.weatherlink_location
                        or (f"{station.get('city')}" if station.get("city") else None),
        },
        "timestamp_utc": ts_iso,
        "source":        "davis-vp2-cloud",
    }
    if outdoor:  payload["outdoor"]  = outdoor
    if indoor:   payload["indoor"]   = indoor
    if wind:     payload["wind"]     = wind
    if rain:     payload["rain"]     = rain
    if pressure: payload["pressure"] = pressure
    return payload


class WeatherlinkPoller:
    """Background task: poll WeatherLink every N seconds and ingest."""

    def __init__(self, client: WeatherLinkClient, station_id: int,
                 interval_s: int = 60):
        self._client = client
        self._station_id = station_id
        self._no_iss_warned_at = -3600.0
        self._interval_s = max(15, interval_s)
        self._station_meta: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Register the task and return; discovery runs inside it
        (app/poller_lifecycle.py)."""
        self._task = asyncio.create_task(self._run(), name="wl-poller")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        await reap(task, "wl-poller")

    async def _warm_up(self) -> None:
        # Discover the station's metadata once so build_payload can use
        # station_name / city as defaults.
        try:
            stations = await self._client.list_stations()
            self._station_meta = next(
                (s for s in stations if s.get("station_id") == self._station_id),
                {})
            if self._station_meta:
                log.info("station %s '%s' (%s, %s) — polling every %ds",
                         self._station_id, self._station_meta.get("station_name"),
                         self._station_meta.get("city"),
                         self._station_meta.get("region"), self._interval_s)
            else:
                log.warning("station_id %s not found in account — "
                            "did you put the wrong ID?", self._station_id)
                self._station_meta = {}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Message only, never log.exception: a traceback renders the raising
            # exception's args, so anything that escapes WeatherLinkError (an
            # httpx error built from the full request URL) would put the
            # api-key query param straight into the logs. redact() is the same
            # scrub /api/sources already applies to these strings.
            log.warning("WeatherLink station discovery failed (%s); "
                        "polling will still attempt /current/",
                        source_status.redact(str(e)))
            self._station_meta = {}

    async def _run(self) -> None:
        await self._warm_up()
        while not self._stop.is_set():
            try:
                current = await self._client.current(self._station_id)
                payload = build_payload(self._station_meta or {}, current)
                if payload is None:
                    # The API answered, so credentials are fine — there's just
                    # no ISS data. Distinct from a failure, and worth showing
                    # as such rather than silent success.
                    source_status.record_success("davis-cloud", rows=0)
                    # A WARNING that names what the station DID send, once an
                    # hour: this is the line that would have said "Vantage Vue"
                    # on day one (2.8.2). DEBUG hid it for every self-hoster.
                    now = time.monotonic()
                    if now - self._no_iss_warned_at >= 3600:
                        self._no_iss_warned_at = now
                        log.warning("WeatherLink answered but no ISS record was "
                                    "recognised (sensor_type/data_structure_type: %s); "
                                    "nothing ingested", describe_sensors(current))
                else:
                    result = await ingest._do_ingest(payload)  # type: ignore[attr-defined]
                    # rows STORED, not rows posted — the history-write throttle
                    # legitimately rejects a reading that lands too soon, and
                    # reporting 1 there would hide a source that has silently
                    # stopped contributing.
                    source_status.record_success(
                        "davis-cloud", rows=int((result or {}).get("inserted", 0)))
                    o = payload.get("outdoor", {})
                    w = payload.get("wind", {})
                    # build_payload emits wind as {speed_mph, gust_mph,
                    # direction} — the old AWN-style key names here logged
                    # "wind=None@None" on every healthy poll, masking the
                    # "is my source working?" signal.
                    log.info("ingested Davis cloud: tempf=%s hum=%s wind=%s@%s",
                             o.get("tempf"), o.get("humidity"),
                             w.get("speed_mph"), w.get("direction"))
            except Exception as e:
                source_status.record_failure("davis-cloud", str(e))
                log.warning("WeatherLink poll failed: %s",
                            source_status.redact(str(e)))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass
