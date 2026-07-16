"""Optional public weather dashboard rendered into the status page.

When PUBLIC_DASHBOARD=1, the status page (/) shows current conditions + 24h
charts for the operator's station(s) instead of the app screenshots. Fully
server-rendered — inline SVG charts baked into the HTML, no client JS, no
public data API, no external requests. See app/config.py for the env knobs
and main.py for the data-gathering + wiring.
"""

from __future__ import annotations

import html as _html
import math
from typing import Any

# Core chartable/tile fields. key = API field name (as stored + returned by
# db.history / db.latest_observation). Order here = order on the page.
FIELD_META: dict[str, dict[str, Any]] = {
    "tempf":         {"label": "Temperature", "unit": "°F",   "color": "#ff9e33"},
    "humidity":      {"label": "Humidity",    "unit": "%",    "color": "#4cb2ff"},
    "windspeedmph":  {"label": "Wind",        "unit": "mph",  "color": "#39c9d6"},
    "baromrelin":    {"label": "Pressure",    "unit": "inHg", "color": "#b39dff"},
    "hourlyrainin":  {"label": "Rain",        "unit": "in",   "color": "#5aa0ff"},
}
CORE_FIELDS = ["tempf", "humidity", "windspeedmph", "baromrelin", "hourlyrainin"]


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    if unit in ("°F", "%", "mph"):
        return f"{round(v)}{'' if unit == '°F' else ' ' + unit if unit != '%' else '%'}"
    return f"{v:.2f} {unit}"


def svg_chart(points: list[tuple[int, float]], color: str,
              width: int = 640, height: int = 120,
              overlay: list[tuple[int, float]] | None = None,
              overlay_color: str = "#ff5a5f",
              primary_label: str | None = None,
              overlay_label: str | None = None) -> str:
    """Inline SVG area+line chart for a (timestamp_ms, value) series.

    An optional `overlay` series (same units, e.g. feels-like on the temp
    chart) is drawn as a dashed line on the SAME axis so the gap between the
    two is readable; both share the value + time range. A small legend is
    emitted when labels are given.
    """
    pts = [(t, v) for t, v in points if v is not None]
    ov = [(t, v) for t, v in (overlay or []) if v is not None]
    if len(pts) < 2:
        return ('<div class="chart-empty">no data in the last 24h</div>')
    ys = [v for _, v in pts] + [v for _, v in ov]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    ts = [t for t, _ in pts] + [t for t, _ in ov]
    t0, t1 = min(ts), max(ts)
    tspan = (t1 - t0) or 1
    pad = 4.0

    def px(t: int) -> float:
        return round(width * (t - t0) / tspan, 1)

    def py(v: float) -> float:
        return round((height - pad) - ((v - lo) / span) * (height - pad * 2), 1)

    def poly(seq: list[tuple[int, float]]) -> str:
        return " ".join(f"{px(t)},{py(v)}" for t, v in seq)

    line = poly(pts)
    area = f"{px(pts[0][0])},{height} {line} {px(pts[-1][0])},{height}"
    svg = [
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="chart-svg" role="img">',
        f'<polygon points="{area}" fill="{color}" fill-opacity="0.14"/>',
    ]
    if len(ov) >= 2:
        svg.append(
            f'<polyline points="{poly(ov)}" fill="none" stroke="{overlay_color}" '
            f'stroke-width="1.6" stroke-dasharray="4 3" stroke-linejoin="round"/>'
        )
    svg.append(
        f'<polyline points="{line}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linejoin="round"/>'
    )
    svg.append('</svg>')

    legend = ""
    if len(ov) >= 2 and (primary_label or overlay_label):
        legend = (
            f'<div class="chart-legend">'
            f'<span class="lg"><i style="background:{color}"></i>{_esc(primary_label or "")}</span>'
            f'<span class="lg"><i style="background:{overlay_color}"></i>{_esc(overlay_label or "")}</span>'
            f'</div>'
        )
    return (
        "".join(svg) + legend
        + f'<div class="chart-axis"><span>{_fmt(lo, "")}</span>'
          f'<span>{_fmt(hi, "")}</span></div>'
    )


# ── Wind rose ────────────────────────────────────────────────────────────
# 16 compass sectors, petals stacked by speed bin (calm → strong shades of
# the wind color). Radius ∝ how often the wind blew from that direction.
_ROSE_SECTORS = 16
_ROSE_SPEED_BINS = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 1e9)]
_ROSE_SPEED_COLORS = ["#cdeef2", "#7fdce4", "#39c9d6", "#2b93b3", "#1f6f9e"]
_ROSE_SPEED_LABELS = ["0", "5", "10", "15", "20+"]
_COMPASS = ["N", "E", "S", "W"]


def svg_wind_rose(samples: list[tuple[float, float]], size: int = 200) -> str:
    """Inline SVG wind rose from (direction_deg, speed_mph) samples."""
    data = [(float(d) % 360.0, float(s)) for d, s in samples
            if d is not None and s is not None and s == s]
    if len(data) < 3:
        return '<div class="chart-empty">no wind data in the last 24h</div>'

    sec = 360.0 / _ROSE_SECTORS
    nb = len(_ROSE_SPEED_BINS)
    counts = [[0] * nb for _ in range(_ROSE_SECTORS)]
    for d, s in data:
        si = int(((d + sec / 2) % 360.0) // sec)   # sector 0 centred on N
        bi = next((k for k, (lo, hi) in enumerate(_ROSE_SPEED_BINS) if lo <= s < hi), nb - 1)
        counts[si][bi] += 1
    totals = [sum(c) for c in counts]
    maxtot = max(totals) or 1

    cx = cy = size / 2.0
    R = size / 2.0 - 20.0

    def pt(r: float, ang: float) -> tuple[float, float]:
        a = math.radians(ang)
        return (round(cx + r * math.sin(a), 1), round(cy - r * math.cos(a), 1))

    parts = [f'<svg viewBox="0 0 {size} {size}" class="rose-svg" role="img">']
    # faint grid rings
    for frac in (0.5, 1.0):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{round(R*frac,1)}" '
                     f'fill="none" stroke="rgba(255,255,255,0.10)" stroke-width="1"/>')
    # petals
    half = sec * 0.42   # leave a small gap between sectors
    for i in range(_ROSE_SECTORS):
        if totals[i] == 0:
            continue
        centre = i * sec
        a0, a1 = centre - half, centre + half
        cum = 0
        for bi in range(nb):
            c = counts[i][bi]
            if c == 0:
                continue
            r0 = R * cum / maxtot
            r1 = R * (cum + c) / maxtot
            cum += c
            x2, y2 = pt(r1, a0)
            x3, y3 = pt(r1, a1)
            if r0 <= 0.05:
                x1, y1 = cx, cy
                d = f"M{round(x1,1)},{round(y1,1)} L{x2},{y2} A{round(r1,1)},{round(r1,1)} 0 0 1 {x3},{y3} Z"
            else:
                x1, y1 = pt(r0, a0)
                x4, y4 = pt(r0, a1)
                d = (f"M{x1},{y1} L{x2},{y2} A{round(r1,1)},{round(r1,1)} 0 0 1 {x3},{y3} "
                     f"L{x4},{y4} A{round(r0,1)},{round(r0,1)} 0 0 0 {x1},{y1} Z")
            parts.append(f'<path d="{d}" fill="{_ROSE_SPEED_COLORS[bi]}" '
                         f'fill-opacity="0.9"/>')
    # cardinal labels
    for k, lbl in enumerate(_COMPASS):
        lx, ly = pt(R + 11, k * 90.0)
        parts.append(f'<text x="{lx}" y="{ly}" class="rose-lbl" '
                     f'text-anchor="middle" dominant-baseline="middle">{lbl}</text>')
    parts.append('</svg>')

    legend = ['<div class="rose-legend">']
    for bi, lbl in enumerate(_ROSE_SPEED_LABELS):
        legend.append(f'<span class="rs"><i style="background:{_ROSE_SPEED_COLORS[bi]}"></i>{lbl}</span>')
    legend.append('<span class="rs-unit">mph</span></div>')
    return "".join(parts) + "".join(legend)


def resolve_fields(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return CORE_FIELDS
    out = [f.strip() for f in raw.split(",") if f.strip() in FIELD_META]
    return out or CORE_FIELDS


def _esc(s: Any) -> str:
    return _html.escape(str(s), quote=True)


def render_station(name: str, obs: dict[str, Any] | None,
                   series: dict[str, list[tuple[int, float]]],
                   fields: list[str],
                   wind_samples: list[tuple[float, float]] | None = None) -> str:
    """One station block: current-conditions header + a chart per field.

    The temperature chart overlays the feels-like line (from
    series["feelsLike"]); a wind rose tile is appended after the wind chart
    when direction+speed samples are available.
    """
    o = obs or {}
    temp = _num(o.get("tempf"))
    feels = _num(o.get("feelsLike"))
    temp_html = f"{round(temp)}°" if temp is not None else "—"
    feels_html = (f'<div class="cc-feels">feels {round(feels)}°</div>'
                  if feels is not None and (temp is None or abs(feels - temp) >= 1)
                  else "")

    # Small current-conditions stats row for the selected fields (minus temp,
    # which is the hero number).
    chips = []
    for key in fields:
        if key == "tempf":
            continue
        meta = FIELD_META[key]
        v = _num(o.get(key))
        chips.append(
            f'<div class="cc-chip"><span class="cc-k">{_esc(meta["label"])}</span>'
            f'<span class="cc-v" style="color:{meta["color"]}">{_fmt(v, meta["unit"])}</span></div>'
        )

    charts = []
    for key in fields:
        meta = FIELD_META[key]
        # Temperature tile overlays the feels-like line on the same °F axis.
        if key == "tempf" and len(series.get("feelsLike", [])) >= 2:
            body = svg_chart(series.get(key, []), meta["color"],
                             overlay=series.get("feelsLike"),
                             overlay_color="#ff5a5f",
                             primary_label="Temp", overlay_label="Feels like")
        else:
            body = svg_chart(series.get(key, []), meta["color"])
        charts.append(
            f'<div class="chart"><div class="chart-title">{_esc(meta["label"])} '
            f'<span class="chart-unit">· last 24h · {_esc(meta["unit"])}</span></div>'
            f'{body}</div>'
        )
        # Wind rose rides alongside the wind chart.
        if key == "windspeedmph" and wind_samples and len(wind_samples) >= 3:
            charts.append(
                f'<div class="chart chart-rose"><div class="chart-title">Wind rose '
                f'<span class="chart-unit">· last 24h · by direction</span></div>'
                f'{svg_wind_rose(wind_samples)}</div>'
            )

    return (
        f'<section class="station">'
        f'  <div class="cc">'
        f'    <div class="cc-name">{_esc(name)}</div>'
        f'    <div class="cc-temp">{temp_html}</div>{feels_html}'
        f'    <div class="cc-chips">{"".join(chips)}</div>'
        f'  </div>'
        f'  <div class="charts">{"".join(charts)}</div>'
        f'</section>'
    )


def render_dashboard(stations: list[dict[str, Any]], fields: list[str]) -> str:
    """Full dashboard section for all selected stations."""
    if not stations:
        return '<div class="chart-empty">No station data yet.</div>'
    return "".join(
        render_station(s["name"], s.get("obs"), s.get("series", {}), fields,
                       wind_samples=s.get("wind_samples"))
        for s in stations
    )


# CSS injected into the status page's <style> when the dashboard is on. This is
# a plain string inserted into the page f-string via a {placeholder}, so its
# value is copied verbatim — use single (normal CSS) braces here.
DASHBOARD_CSS = """
    .app-cta { text-align:center; margin: 4px 0 24px; }
    .app-cta a { display:inline-flex; align-items:center; gap:8px; font-size:13px;
        font-weight:600; color:#0b0d13; background:#fff; border-radius:10px;
        padding:10px 18px; text-decoration:none; }
    .app-cta .sub { display:block; font-size:11px; color:rgba(255,255,255,0.45); margin-top:8px; }
    .station { margin-bottom: 28px; }
    .cc { margin-bottom: 14px; }
    .cc-name { font-size:11px; font-weight:800; letter-spacing:1.2px;
        text-transform:uppercase; color:rgba(255,255,255,0.5); }
    .cc-temp { font-size:56px; font-weight:200; line-height:1; margin-top:2px; }
    .cc-feels { font-size:13px; color:rgba(255,255,255,0.55); margin-top:2px; }
    .cc-chips { display:flex; flex-wrap:wrap; gap:16px; margin-top:10px; }
    .cc-k { font-size:9px; font-weight:700; letter-spacing:0.8px; text-transform:uppercase;
        color:rgba(255,255,255,0.4); display:block; }
    .cc-v { font-size:15px; font-weight:600; }
    .charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; }
    .chart { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06);
        border-radius:12px; padding:12px 14px; }
    .chart-title { font-size:11px; font-weight:700; color:rgba(255,255,255,0.7); margin-bottom:6px; }
    .chart-unit { font-weight:400; color:rgba(255,255,255,0.35); }
    .chart-svg { width:100%; height:110px; display:block; }
    .chart-axis { display:flex; justify-content:space-between; font-size:9px;
        color:rgba(255,255,255,0.35); margin-top:2px; }
    .chart-empty { font-size:12px; color:rgba(255,255,255,0.4); padding:20px 0; }
    .chart-legend { display:flex; gap:14px; margin-top:6px; }
    .chart-legend .lg { display:inline-flex; align-items:center; gap:5px;
        font-size:10px; color:rgba(255,255,255,0.55); }
    .chart-legend .lg i { width:14px; height:0; border-top:2px solid; display:inline-block; }
    .chart-rose { display:flex; flex-direction:column; }
    .rose-svg { width:100%; max-width:210px; height:auto; margin:2px auto 0; display:block; }
    .rose-lbl { fill:rgba(255,255,255,0.5); font-size:11px; font-weight:700; }
    .rose-legend { display:flex; flex-wrap:wrap; justify-content:center; gap:10px; margin-top:8px; }
    .rose-legend .rs { display:inline-flex; align-items:center; gap:4px;
        font-size:9px; color:rgba(255,255,255,0.5); }
    .rose-legend .rs i { width:9px; height:9px; border-radius:2px; display:inline-block; }
    .rose-legend .rs-unit { font-size:9px; color:rgba(255,255,255,0.35); }
"""
