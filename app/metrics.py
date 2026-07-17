"""Prometheus exposition for Zasder Weather.

Opt-in (`PROMETHEUS_METRICS=1`) `/metrics` endpoint that renders each device's
latest reading as Prometheus gauges — point Grafana/Prometheus at it and get
dashboards + alerting for free. Pure text rendering here; the endpoint wiring
lives in main.py. No external dependency (the exposition format is trivial).
"""

from __future__ import annotations

from typing import Any

# (metric_name, HELP text, lastData field key). Names follow Prometheus
# base-unit conventions; every series carries mac + name labels.
_METRICS: list[tuple[str, str, str]] = [
    ("zasder_temperature_fahrenheit", "Outdoor temperature (°F)", "tempf"),
    ("zasder_feels_like_fahrenheit", "Feels-like temperature (°F)", "feelsLike"),
    ("zasder_dew_point_fahrenheit", "Dew point (°F)", "dewPoint"),
    ("zasder_humidity_percent", "Relative humidity (%)", "humidity"),
    ("zasder_pressure_inhg", "Relative barometric pressure (inHg)", "baromrelin"),
    ("zasder_wind_speed_mph", "Wind speed (mph)", "windspeedmph"),
    ("zasder_wind_gust_mph", "Wind gust (mph)", "windgustmph"),
    ("zasder_wind_direction_degrees", "Wind direction (degrees)", "winddir"),
    ("zasder_rain_daily_inches", "Rain so far today (in)", "dailyrainin"),
    ("zasder_rain_rate_inches", "Rain in the last hour (in)", "hourlyrainin"),
    ("zasder_uv_index", "UV index", "uv"),
    ("zasder_solar_radiation_wm2", "Solar radiation (W/m²)", "solarradiation"),
]


def _esc_label(s: Any) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return (str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " "))


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def render_prometheus(devices: list[dict[str, Any]], now_ms: int) -> str:
    """Prometheus text-format exposition for all devices' latest readings."""
    lines: list[str] = []
    for name, help_text, key in _METRICS:
        block: list[str] = []
        for d in devices:
            v = _num((d.get("lastData") or {}).get(key))
            if v is None:
                continue
            labels = f'mac="{_esc_label(d.get("mac", ""))}",name="{_esc_label(d.get("name") or d.get("mac", ""))}"'
            block.append(f"{name}{{{labels}}} {v:g}")
        if block:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.extend(block)

    # Freshness: seconds since each device last reported.
    age_block: list[str] = []
    for d in devices:
        ls = d.get("lastSeen")
        if ls is None:
            continue
        age = max(0.0, (now_ms - int(ls)) / 1000.0)
        labels = f'mac="{_esc_label(d.get("mac", ""))}",name="{_esc_label(d.get("name") or d.get("mac", ""))}"'
        age_block.append(f"zasder_device_last_seen_seconds{{{labels}}} {age:g}")
    if age_block:
        lines.append("# HELP zasder_device_last_seen_seconds Seconds since the device last reported")
        lines.append("# TYPE zasder_device_last_seen_seconds gauge")
        lines.extend(age_block)

    return "\n".join(lines) + "\n"
