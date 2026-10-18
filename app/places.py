"""Which stations stand in the same place (2.7, "one smart alert per place").

Doren, 2026-10-13: a Davis and a Tempest in one yard, both with a
barometer, both saw the same 0.06 inHg fall, and "Pressure falling fast"
arrived twice a minute apart. A weather watch is about the weather at a
place, not about a box on a pole, so stations at the same place send one
alert when they agree.

"Same place" is the station's own coordinates (info.coords.coords, which
list_devices fills from the operator-set device_location first, else what
the source reported, else the server's FORECAST_LAT/LON home that ingest
stamps on sources with no coordinates of their own) within PLACE_RADIUS_KM
of the place's first station. A station with no usable coordinates is a
place of its own and never groups: absent is not "here".

Pure: no database, no clock. The callers (alerts._check_smart_alerts,
alerts._check_seasonal_events, lightning_watch.check) keep their per-station
state and use this only to decide who speaks for whom.
"""
from __future__ import annotations

import math
from typing import Any

# One kilometre. Wide enough for one property and for the slop between
# two sources' idea of where it is (WeatherFlow's station metadata, an
# operator-typed address, the server's FORECAST_LAT/LON stamped on a WLL
# or SDR station, the map beacon's own fuzz is 0.5 km); narrow enough that
# two stations a few kilometres apart, which can honestly see different
# frost, heat or wind, keep their own alerts. The forecast's 25 km
# neighbour radius (neighbors.MAX_DISTANCE_KM) is "same sky", far too wide
# for "same yard".
PLACE_RADIUS_KM = 1.0

# How long one station's firing speaks for its place. A second station at
# the same place that crosses the same line inside this window joins the
# alert already sent instead of sending its own. Thirty minutes: Doren's
# pair was a minute apart, but two thermometers half a degree apart on a
# night cooling 2 °F an hour cross a frost line fifteen minutes apart,
# and two barometers on different upload cadences (a Tempest every
# minute, an AWN poll every five) cross the 3 h fall line minutes apart
# too. It is also the lightning all-clear's window. These alerts are
# edge-triggered with a deadband re-arm, not timed cooldowns, so the
# window only decides who speaks for an edge; a sibling still IN the
# condition (fresh data, still over the line) covers it for as long as
# it lasts.
PLACE_WINDOW_MS = 30 * 60_000

# The kinds that are about the WEATHER at a place, so stations at one
# place send one. Everything else stays per station on purpose, because
# it is about the hardware: battery / battery_recovered, sensor_quiet /
# sensor_recovered, flatline (health_watch), leak / leak_cleared, the
# device_stale / device_recovered pair, source outages, and the user's
# own threshold rules ("rule"), which name a station and field. Storm
# summaries, the rain-start nowcast and NWS warnings are not smart
# alerts (NWS dedupes by alert id across stations already).
GROUPED_KINDS = frozenset({
    # alerts._check_smart_alerts (SMART_KINDS, and "front", their
    # several-at-once group)
    "frost", "heat", "pressure_drop", "temp_drop", "wind_ramp",
    "pipe_freeze", "outflow", "front",
    # alerts._check_seasonal_events
    "first_frost",
    # lightning_watch: the opening alert, "closing", and the all clear
    "lightning", "lightning_clear",
})


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def device_coords(device: dict[str, Any]) -> tuple[float, float] | None:
    """The station's own coordinates, or None. Stored coords are operator
    and source data: a bool, a string, NaN or a point off the globe is no
    location (the forecast_snapshots.coords_device rule)."""
    info = device.get("info") or {}
    if not isinstance(info, dict):
        return None
    outer = info.get("coords") or {}
    inner = outer.get("coords") if isinstance(outer, dict) else None
    if not isinstance(inner, dict):
        return None
    lat, lon = inner.get("lat"), inner.get("lon")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(flat) and math.isfinite(flon)
            and -90.0 <= flat <= 90.0 and -180.0 <= flon <= 180.0):
        return None
    return flat, flon


def group(devices: list[dict[str, Any]],
          radius_km: float = PLACE_RADIUS_KM) -> list[list[dict[str, Any]]]:
    """The devices as places, each a list in the order given (so the
    server's first station leads its place). A station joins the first
    earlier place whose FIRST station is within `radius_km`: anchored, not
    chained, so three stations 0.9 km apart in a line do not make one
    1.8 km place. A station with no coordinates is always its own place."""
    places: list[tuple[tuple[float, float] | None, list[dict[str, Any]]]] = []
    for d in devices:
        here = device_coords(d)
        home = None
        if here is not None:
            for anchor, members in places:
                if (anchor is not None
                        and distance_km(anchor[0], anchor[1],
                                        here[0], here[1]) <= radius_km):
                    home = members
                    break
        if home is None:
            places.append((here, [d]))
        else:
            home.append(d)
    return [members for _, members in places]


def place_of(devices: list[dict[str, Any]],
             radius_km: float = PLACE_RADIUS_KM) -> dict[str, list[str]]:
    """MAC -> every MAC in its place (itself included, in order)."""
    out: dict[str, list[str]] = {}
    for members in group(devices, radius_km):
        macs = [m["mac"] for m in members]
        for mac in macs:
            out[mac] = macs
    return out


def is_fresh(device: dict[str, Any], now_ms: int,
             max_age_ms: int = PLACE_WINDOW_MS) -> bool:
    """The station's latest reading is recent. A station that died while
    over a line keeps a lastData that reads as over it forever; it must
    not keep speaking for its place. No numeric timestamp: not fresh."""
    last = device.get("lastData") or {}
    ts = last.get("dateutc") if isinstance(last, dict) else None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    return abs(now_ms - ts) <= max_age_ms


def also_line(verb: str, names: list[str]) -> str:
    """" Also seen at Tempest." appended to a place's alert body, or ""
    when the station spoke alone."""
    if not names:
        return ""
    return f" Also {verb} at {join_names(names)}."


def join_names(names: list[str]) -> str:
    """"A", "A and B", "A, B and C". User-facing: no serial comma, the
    app's voice everywhere else."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
