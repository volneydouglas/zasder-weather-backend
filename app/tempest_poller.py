"""WeatherFlow Tempest cloud poller.

Pulls the latest derived observation from WeatherFlow's cloud every N
seconds and feeds it through the same ingest path the SDR relays and the
Davis cloud poller use, so Tempest readings land in the standard
observations table identified by a synthetic MAC.

This runs on the BACKEND rather than in the macOS app on purpose. The Mac
hosts the WeatherLink Live bridge because WLL is LAN-only — there is no
cloud to poll without a WeatherLink subscription, so the Mac was a
necessity, not a preference. Tempest's API is reachable from anywhere, and
the backend is the always-on half of this system: a sleeping laptop must
not be the reason a station goes quiet.

Synthetic MAC scheme, matching sdr-relay / davis-relay / weatherlink_poller:
  5D:5D:06:HH:HH:HH   06 = WeatherFlow type tag (01 AcuRite, 02 Fine Offset,
                      05 Davis), HH:HH:HH = the Tempest station id.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from . import ingest
from . import source_status
from .tempest_client import (TempestClient, c_to_f, km_to_mi, mb_to_inhg,
                             mm_to_in, ms_to_mph, num)


log = logging.getLogger("tempest-poller")

SOURCE = "tempest"


def synth_mac(station_id: int) -> str:
    """Compact form (no colons) — ingest._format_mac normalizes it."""
    sid = int(station_id) & 0xFFFFFF
    return f"5D5D06{(sid >> 16) & 0xFF:02X}{(sid >> 8) & 0xFF:02X}{sid & 0xFF:02X}"


def build_payload(station_id: int, obs: dict[str, Any],
                  name: str | None = None,
                  station: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Transform one Tempest station observation into an /ingest/custom
    payload. Returns None when the reading carries no usable timestamp.

    Field names MUST match ingest._flatten(): mis-naming silently drops the
    value and the app then shows the tile as missing while the cloud had the
    data (the lesson weatherlink_poller records at the same spot).
    """
    ts = num(obs, "timestamp")
    if ts is None:
        return None
    ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")

    outdoor: dict[str, Any] = {}
    for key, dest, conv in (("air_temperature", "tempf", c_to_f),
                            ("dew_point", "dew_point_f", c_to_f),
                            ("feels_like", "feels_like", c_to_f)):
        v = conv(num(obs, key))
        if v is not None:
            outdoor[dest] = v
    hum = num(obs, "relative_humidity")
    if hum is not None:
        outdoor["humidity"] = round(hum)
    uv = num(obs, "uv")
    if uv is not None:
        outdoor["uv"] = uv
    solar = num(obs, "solar_radiation")
    if solar is not None:
        outdoor["solar_wm2"] = solar

    wind: dict[str, Any] = {}
    # wind_avg is the reporting-interval average and wind_gust its peak —
    # the same sustained/gust pair the rest of the app assumes. wind_lull is
    # the interval MINIMUM and is deliberately unused: feeding it anywhere
    # would put a number below the average into a sustained-wind field.
    speed = ms_to_mph(num(obs, "wind_avg"))
    if speed is not None:
        wind["speed_mph"] = speed
    gust = ms_to_mph(num(obs, "wind_gust"))
    if gust is not None:
        wind["gust_mph"] = gust
    direction = num(obs, "wind_direction")
    if direction is not None:
        wind["direction"] = direction

    rain: dict[str, Any] = {}
    # precip_accum_last_1hr is a TRAILING 1h accumulation, which is exactly
    # what hourlyrainin means here — unlike WU's precipRate, which is an
    # instantaneous rate and is why wu_import deliberately leaves the column
    # NULL rather than lying.
    hourly = mm_to_in(num(obs, "precip_accum_last_1hr"))
    if hourly is not None:
        rain["hourly_in"] = hourly
    daily = mm_to_in(num(obs, "precip_accum_local_day"))
    if daily is not None:
        rain["daily_in"] = daily

    pressure: dict[str, Any] = {}
    # sea_level_pressure, NOT station_pressure. Tempest is one of the few
    # sources that hands us both already separated; the corrected value is
    # what baromrelin means and what the 24-33 inHg band expects. Passing the
    # raw station pressure would band-reject any station above ~2,000 ft.
    # (ingest._flatten copies relative into baromabsin too, because most
    # sources cannot split them — so the true absolute reading is lost here.
    # Storing both would need a new payload key, which is not worth it until
    # something reads it.)
    rel = mb_to_inhg(num(obs, "sea_level_pressure"))
    if rel is not None:
        pressure["relative_inhg"] = rel

    # Lightning. The Tempest is the only sensor here that detects it, and the
    # data is unrecoverable after the fact — the counters are interval-scoped
    # and reset, so a storm that passes unrecorded is simply gone.
    #
    # `lightning_strike_count` is the count for THIS reporting interval;
    # last_1hr and last_3hr are TRAILING windows over the same strikes.
    # Summing any two of them double-counts, which is the hourlyrainin trap
    # wearing a different hat. Only the interval count is safe to accumulate,
    # so the trailing windows are stored for display and explicitly named as
    # such rather than left to look additive.
    lightning: dict[str, Any] = {}
    count = num(obs, "lightning_strike_count")
    if count is not None:
        lightning["strike_count"] = int(count)
    for key, dest in (("lightning_strike_count_last_1hr", "strike_count_last_1hr"),
                      ("lightning_strike_count_last_3hr", "strike_count_last_3hr")):
        v = num(obs, key)
        if v is not None:
            lightning[dest] = int(v)
    # Distance is KILOMETRES here, like every other value in this response
    # whatever station_units claims. Stored in miles to match the API-native
    # convention the rest of the backend uses (CLAUDE.md).
    dist = km_to_mi(num(obs, "lightning_strike_last_distance"))
    if dist is not None:
        lightning["last_distance_mi"] = dist
    last_epoch = num(obs, "lightning_strike_last_epoch")
    if last_epoch is not None:
        # Seconds from the API; stored in ms to match `dateutc` everywhere else.
        lightning["last_strike_ms"] = int(last_epoch * 1000)

    station = station or {}
    device: dict[str, Any] = {"id": synth_mac(station_id)}
    # Prefer the configured name, else the station's own — a bare synthetic MAC
    # is a poor thing to meet on first launch.
    label = name or station.get("name")
    if label:
        device["name"] = label
    # No `location`: Tempest has no city field, and `public_name` is just a
    # copy of `name` — setting it would render "Chaucer Drive / Chaucer Drive".
    # Tempest hands us the station's coordinates, which almost no other source
    # here does — and without them a station gets no forecast and no sunrise,
    # so its condition glyph falls back to the neutral "we cannot tell" state.
    # ingest._payload_coords reads exactly this shape.
    lat, lon = num(station, "latitude"), num(station, "longitude")
    if lat is not None and lon is not None:
        device["coords"] = {"lat": lat, "lon": lon}

    payload: dict[str, Any] = {
        "device": device,
        "timestamp_utc": ts_iso,
        "source": SOURCE,
    }
    for key, block in (("outdoor", outdoor), ("wind", wind),
                       ("rain", rain), ("pressure", pressure),
                       ("lightning", lightning)):
        if block:
            payload[key] = block
    # Nothing but a timestamp is not an observation. Lightning counts as
    # content: a faulted air/wind sensor set during a thunderstorm can leave
    # strike data as the ONLY fields in the response, and the interval-scoped
    # counters reset server-side — dropping that reading loses the storm's
    # lightning permanently, the exact loss this module exists to prevent
    # (2026-08-20 review; the tuple predated the lightning block).
    if not (outdoor or wind or rain or pressure or lightning):
        return None
    return payload


class TempestPoller:
    """Background task: poll the Tempest cloud every N seconds and ingest."""

    def __init__(self, client: TempestClient, station_id: int,
                 interval_s: int = 60, name: str | None = None) -> None:
        self._client = client
        self._station_id = int(station_id)
        self._interval_s = max(15, int(interval_s))
        self._name = name
        self._station_meta: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        # One metadata read so every payload can carry the station's name and
        # coordinates. Best-effort: a failure here must not stop the poller,
        # which still has everything it needs to record observations.
        try:
            for st in await self._client.stations():
                if int(st.get("station_id") or 0) == self._station_id:
                    self._station_meta = st
                    log.info("tempest station %s: name=%s coords=%s,%s",
                             self._station_id, st.get("name"),
                             st.get("latitude"), st.get("longitude"))
                    break
            else:
                log.warning("tempest station %s not visible to this token",
                            self._station_id)
        except Exception as e:
            log.warning("tempest station lookup failed: %s",
                        source_status.redact(str(e)))
        self._task = asyncio.create_task(self._run(), name="tempest-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        log.info("tempest poller running every %ds for station %s",
                 self._interval_s, self._station_id)
        while not self._stop.is_set():
            try:
                obs = await self._client.station_observation(self._station_id)
                payload = build_payload(self._station_id, obs or {}, self._name,
                                        self._station_meta) if obs else None
                if payload is None:
                    # Credentials are fine, the station just has nothing to
                    # say. Distinct from a failure and worth showing as such.
                    source_status.record_success(SOURCE, rows=0)
                    log.debug("no usable Tempest observation — skipping ingest")
                else:
                    result = await ingest._do_ingest(payload)  # type: ignore[attr-defined]
                    # Rows STORED, not posted: the history-write throttle
                    # legitimately rejects a reading that lands too soon, and
                    # reporting 1 there would hide a source that has quietly
                    # stopped contributing.
                    source_status.record_success(
                        SOURCE, rows=int((result or {}).get("inserted", 0)))
                    o = payload.get("outdoor", {})
                    w = payload.get("wind", {})
                    log.info("ingested Tempest: tempf=%s hum=%s wind=%s@%s",
                             o.get("tempf"), o.get("humidity"),
                             w.get("speed_mph"), w.get("direction"))
            except Exception as e:
                source_status.record_failure(SOURCE, str(e))
                log.warning("Tempest poll failed: %s",
                            source_status.redact(str(e)))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass
