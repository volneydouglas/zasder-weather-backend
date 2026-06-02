#!/usr/bin/env python3
"""Davis WeatherLink Live (WLL) local poller.

Polls a WLL gateway's local HTTP API (`/v1/current_conditions`) every few
seconds and forwards a normalized observation to a Zasder Weather backend's
`/ingest/custom` endpoint. Designed to run on the same Raspberry Pi as
sdr-relay — pure stdlib so installs are a single file copy.

WLL local API: https://weatherlink.github.io/weatherlink-live-local-api/

Why local instead of the WeatherLink cloud poller: WLL serves fresh data on
every HTTP request (UDP broadcasts at 2.5s); the cloud only updates every
60s, needs an API key, and an internet round-trip. Same physical Davis VP2,
~6× lower latency.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

WLL_HOST          = os.environ.get("WLL_HOST", "10.0.1.56")
WLL_POLL_SECONDS  = int(os.environ.get("WLL_POLL_SECONDS", "10"))
BACKEND_URL       = os.environ.get("BACKEND_URL", "").rstrip("/")
INGEST_TOKEN      = os.environ.get("INGEST_TOKEN", "")
# Synthetic MAC the backend stores under. If you also run the WeatherLink
# CLOUD poller for the same physical Davis VP2, reuse its MAC so both feeds
# land on the same device row (cloud is then redundant + can be disabled).
DEVICE_MAC        = os.environ.get("WLL_DEVICE_MAC", "5D:5D:05:00:00:01")
DEVICE_NAME       = os.environ.get("WLL_DEVICE_NAME", "")     # empty → keep existing
DEVICE_LOCATION   = os.environ.get("WLL_DEVICE_LOCATION", "")
SOURCE            = "davis-wll-local"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s wll: %(message)s",
)
log = logging.getLogger("wll")

# rain_size enum (WLL field) → inches per single tip count.
_RAIN_SIZE_IN: dict[int, float] = {
    1: 0.01,            # 0.01"  (US Davis tipping bucket)
    2: 0.2 / 25.4,      # 0.2 mm
    3: 0.1 / 25.4,      # 0.1 mm
    4: 0.001,           # 0.001"
}


def fetch_wll(host: str = WLL_HOST, timeout: float = 5.0) -> dict:
    """GET the WLL local current-conditions snapshot. Raises on transport failure."""
    url = f"http://{host}/v1/current_conditions"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _num(d: dict, key: str):
    """Return d[key] if it's a real number — WLL emits null for sensors that
    aren't reporting (out of range, transmitter offline). Coerce to None
    rather than letting null/None/strings sneak through."""
    v = d.get(key)
    return v if isinstance(v, (int, float)) else None


def to_observation(
    wll: dict,
    *,
    mac: str = DEVICE_MAC,
    name: str = DEVICE_NAME,
    location: str = DEVICE_LOCATION,
) -> dict | None:
    """Transform a WLL JSON snapshot into the /ingest/custom payload shape.
    Returns None if there's nothing usable (WLL booted but ISS hasn't been
    heard, or WLL returned an error)."""
    err = wll.get("error")
    if err:
        log.warning("wll returned error: %s", err)
        return None
    data = wll.get("data") or {}
    conditions = data.get("conditions") or []
    ts = data.get("ts") or int(time.time())
    iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    outdoor: dict = {}
    wind: dict = {}
    rain: dict = {}
    pressure: dict = {}
    indoor: dict = {}
    solar: dict = {}

    for c in conditions:
        st = c.get("data_structure_type")
        if st == 1:                                          # ISS — outdoor + rain + solar
            outdoor["tempf"]        = _num(c, "temp")
            outdoor["humidity"]     = _num(c, "hum")
            outdoor["dew_point_f"]  = _num(c, "dew_point")
            # Davis publishes several derived temps; prefer THSW (sun-aware)
            # then heat index, then wind chill. Backend's _compute_feels_like
            # will derive one if all three are null.
            outdoor["feels_like"] = (
                _num(c, "thsw_index")
                or _num(c, "heat_index")
                or _num(c, "wind_chill"))
            wind["speed_mph"] = _num(c, "wind_speed_last")
            wind["dir_deg"]   = _num(c, "wind_dir_last")
            wind["gust_mph"]  = _num(c, "wind_speed_hi_last_10_min")

            size = _RAIN_SIZE_IN.get(c.get("rain_size"), 0.01)
            def _in(name: str):
                v = _num(c, name)
                return v * size if v is not None else None
            # rain_rate_last is in counts/hour → ×size = in/hr  (hourlyrainin equiv).
            rain["hourly_in"]  = _in("rain_rate_last")
            rain["daily_in"]   = _in("rainfall_daily")
            rain["monthly_in"] = _in("rainfall_monthly")
            rain["yearly_in"]  = _in("rainfall_year")

            solar["radiation_wm2"] = _num(c, "solar_rad")
            solar["uv"]            = _num(c, "uv_index")

        elif st == 3:                                        # WLL indoor sensor
            indoor["tempf"]    = _num(c, "temp_in")
            indoor["humidity"] = _num(c, "hum_in")

        elif st == 4:                                        # WLL barometer
            pressure["rel_inhg"] = _num(c, "bar_sea_level")
            pressure["abs_inhg"] = _num(c, "bar_absolute")
            # Surface the barometer to the indoor block too so dashboards
            # that share-barometer-from-indoor see it.
            indoor.setdefault("pressure_inhg", _num(c, "bar_sea_level"))

    if not (outdoor or wind or rain or pressure or indoor or solar):
        return None

    device: dict = {"id": mac}
    if name:     device["name"] = name
    if location: device["location"] = location

    return {
        "device": device,
        "timestamp_utc": iso,
        "outdoor": outdoor,
        "wind": wind,
        "rain": rain,
        "pressure": pressure,
        "indoor": indoor,
        "solar": solar,
        "source": SOURCE,
    }


def post_observation(obs: dict, *, backend: str = BACKEND_URL,
                     token: str = INGEST_TOKEN) -> None:
    """POST a normalized observation to /ingest/custom. Raises on HTTP error."""
    body = json.dumps(obs).encode("utf-8")
    req = urllib.request.Request(
        f"{backend}/ingest/custom",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status >= 300:
            raise RuntimeError(f"ingest returned {r.status}: {r.read()[:200]!r}")


def main() -> int:
    if not BACKEND_URL or not INGEST_TOKEN:
        log.error("BACKEND_URL and INGEST_TOKEN must be set in env")
        return 2
    log.info("polling http://%s every %ds → %s",
             WLL_HOST, WLL_POLL_SECONDS, BACKEND_URL)
    while True:
        t0 = time.time()
        try:
            obs = to_observation(fetch_wll())
            if obs:
                post_observation(obs)
                log.debug("posted observation: outdoor=%s", obs.get("outdoor"))
            else:
                log.warning("no usable WLL data this tick")
        except urllib.error.URLError as e:
            log.warning("network error: %s", e)
        except Exception:
            log.exception("unexpected error in poll loop")
        # steady cadence — sleep remainder of the window
        time.sleep(max(0.0, WLL_POLL_SECONDS - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
