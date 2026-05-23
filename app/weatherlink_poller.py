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

import asyncio
import logging
from typing import Any

from . import ingest
from .config import settings
from .weatherlink_client import WeatherLinkClient


log = logging.getLogger("wl-poller")


# Davis WeatherLink sensor_type → handler. From observation against a
# 6313 + VP2 6163 ISS in May 2026; types stable across firmwares.
SENSOR_TYPE_ISS     = 43       # Vantage ISS — outdoor temp/hum/wind/rain/UV/solar
SENSOR_TYPE_INDOOR  = 365      # 6313 built-in indoor temp+hum
SENSOR_TYPE_BAROM   = 242      # 6313 built-in barometer


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

    for s in sensors:
        stype = s.get("sensor_type")
        rows = s.get("data") or []
        if not rows:
            continue
        r = rows[0]
        if stype == SENSOR_TYPE_ISS:
            iss_data = r
            if r.get("tx_id"):
                tx_id = int(r["tx_id"])
        elif stype == SENSOR_TYPE_INDOOR:
            indoor_data = r
        elif stype == SENSOR_TYPE_BAROM:
            barom_data = r

    if not iss_data:
        log.debug("no ISS sensor in WeatherLink response")
        return None

    # Use the freshest sensor timestamp as the observation time so the
    # backend stores it correctly even if our poll cadence drifts.
    ts_ms = (iss_data.get("ts") or 0) * 1000
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
    if iss_data.get("wind_speed_last") is not None:
        wind["speed_mph"] = float(iss_data["wind_speed_last"])
    if iss_data.get("wind_speed_hi_last_2_min") is not None:
        wind["gust_mph"] = float(iss_data["wind_speed_hi_last_2_min"])
    if iss_data.get("wind_dir_last") is not None:
        wind["direction"] = round(float(iss_data["wind_dir_last"]))

    rain: dict[str, Any] = {}
    if iss_data.get("rainfall_year_in") is not None:
        # Add the operator-supplied baseline so a mid-year-installed
        # ISS reports the actual year-to-date total, not just rain
        # since pairing. (Davis cloud's rainfall_year_in resets to 0
        # at install + only counts new tips from then on.)
        rain["yearly_in"] = (float(iss_data["rainfall_year_in"])
                              + settings.weatherlink_yearly_rain_baseline_in)
    # Davis cloud reports daily + hourly directly. Setting them here
    # explicitly stops backend/app/main.py:get_current() from invoking
    # its rain-rollup enrichment (which would otherwise compute
    # daily/weekly/monthly from yearlyrainin DELTAS — broken for a
    # baselined value since the "yesterday" yearlyrainin is fictional).
    if iss_data.get("rainfall_day_in") is not None:
        rain["daily_in"] = float(iss_data["rainfall_day_in"])
    if iss_data.get("rainfall_last_60_min_in") is not None:
        rain["hourly_in"] = float(iss_data["rainfall_last_60_min_in"])
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
        self._interval_s = max(15, interval_s)
        self._station_meta: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        # Discover the station's metadata once at startup so build_payload
        # can use station_name / city as defaults.
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
        except Exception:
            log.exception("WeatherLink station discovery failed; "
                          "polling will still attempt /current/")
            self._station_meta = {}
        self._task = asyncio.create_task(self._run(), name="wl-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                current = await self._client.current(self._station_id)
                payload = build_payload(self._station_meta or {}, current)
                if payload is None:
                    log.debug("no ISS data in poll response — skipping ingest")
                else:
                    await ingest._do_ingest(payload)  # type: ignore[attr-defined]
                    o = payload.get("outdoor", {})
                    w = payload.get("wind", {})
                    log.info("ingested Davis cloud: tempf=%s hum=%s wind=%s@%s",
                             o.get("tempf"), o.get("humidity"),
                             w.get("windspeedmph"), w.get("winddir"))
            except Exception as e:
                log.warning("WeatherLink poll failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass
