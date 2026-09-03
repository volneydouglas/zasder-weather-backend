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
import time
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
    """Coerce to a FINITE float, else None. Infinity matters as much as NaN:
    int(inf) raises OverflowError, which would 500 the public page just like
    int(nan) raises ValueError."""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _fmt(v: float | None, unit: str) -> str:
    """Value + unit for chips and record cards, which have no separate unit
    element — so whatever this returns is all the reader sees.

    The nested conditional this replaces emitted a BARE number for "°F", so
    every temperature on the dashboard (hottest, coldest, dew point…) rendered
    unitless next to a "30.04 inHg" that did carry one. `unit=""` still yields a
    bare number, which is what the chart axis labels want.
    """
    if v is None:
        return "—"
    if unit == "":
        return f"{v:.2f}"          # chart axis label — no unit by design
    if unit == "°F":
        return f"{round(v)}°F"     # degree symbol attaches with no space
    if unit == "%":
        return f"{round(v)}%"
    if unit == "mph":
        return f"{round(v)} mph"
    return f"{v:.2f} {unit}"


def _axis_html(lo: float, hi: float, unit: str,
               py, height: int) -> tuple[str, str, str]:
    """(svg gridlines, HTML y-labels, HTML time row) for a 24h chart.

    Volney, 2026-08-21: "a chart without x and y legend markers" — the old
    footer was the lo/hi VALUE pair laid out horizontally, which read as a
    broken time axis. Now: two horizontal gridlines with value labels at
    thirds of the range (labels are HTML overlays, NOT svg <text> — the
    charts stretch with preserveAspectRatio="none", which would distort
    glyphs), and the bottom row is an actual time axis.
    """
    span = (hi - lo) or 1.0
    grid, labels = [], []
    for frac in (1 / 3, 2 / 3):
        v = lo + span * frac
        y = py(v)
        grid.append(f'<line x1="0" y1="{y}" x2="100%" y2="{y}" '
                    f'class="chart-grid"/>')
        pct = round(y * 100.0 / height, 1)
        labels.append(f'<span class="chart-yl" style="top:{pct}%">'
                      f'{_fmt(v, "")}{_esc(unit)}</span>')
    time_row = ('<div class="chart-axis"><span>24h ago</span>'
                '<span>12h</span><span>now</span></div>')
    return "".join(grid), "".join(labels), time_row


def _extreme_markers(pts: list[tuple[int, float]],
                     px, py, width: int, height: int,
                     unit: str) -> str:
    """Dots + value labels at the series' actual max and min (Volney,
    2026-08-22: gridline thirds gave structure but "we still cannot see
    the real values"). HTML overlays, not svg shapes — circles stretch
    into ellipses under preserveAspectRatio="none". The peak's label sits
    below its dot and the trough's above, so neither leaves the plot."""
    if len(pts) < 2:
        return ""
    hi_t, hi_v = max(pts, key=lambda p: p[1])
    lo_t, lo_v = min(pts, key=lambda p: p[1])
    if hi_v == lo_v:
        return ""
    out = []
    for (t, v), is_hi in ((( hi_t, hi_v), True), ((lo_t, lo_v), False)):
        x = min(max(px(t) * 100.0 / width, 6.0), 94.0)
        y = py(v) * 100.0 / height
        out.append(f'<span class="chart-dot" '
                   f'style="left:{x:.1f}%;top:{y:.1f}%"></span>')
        vy = "chart-xl-below" if is_hi else "chart-xl-above"
        out.append(f'<span class="chart-xl {vy}" '
                   f'style="left:{x:.1f}%;top:{y:.1f}%">'
                   f'{_fmt(v, unit) if unit else _fmt(v, "")}</span>')
    return "".join(out)


def svg_chart(points: list[tuple[int, float]], color: str,
              width: int = 640, height: int = 120,
              overlay: list[tuple[int, float]] | None = None,
              overlay_color: str = "#ff5a5f",
              primary_label: str | None = None,
              overlay_label: str | None = None,
              unit: str = "") -> str:
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
    grid, ylabels, time_row = _axis_html(lo, hi, unit, py, height)
    svg = [
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="chart-svg" role="img">',
        grid,
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
    markers = _extreme_markers(pts + ov, px, py, width, height, unit)
    return (f'<div class="chart-plot">{"".join(svg)}{ylabels}{markers}</div>'
            + legend + time_row)


# ── Multi-station overlay charts (1.7, Volney's ask) ─────────────────────
# When the page shows 2+ stations, a compare block leads: the "coolest"
# overlays first (temp, feels like, humidity, pressure), every station on
# one axis per field, then the per-station blocks as before.

# Distinct per-station hues, tuned to read on BOTH themes (mid-brightness,
# well-separated). Cycles if someone shares more than six stations.
STATION_COLORS = ["#ff9e33", "#4cb2ff", "#3ddc97", "#b39dff",
                  "#ff5a8f", "#ffd24c"]

OVERLAY_FIELDS: list[tuple[str, str, str]] = [
    ("tempf",      "Temperature", "°F"),
    ("feelsLike",  "Feels like",  "°F"),
    ("humidity",   "Humidity",    "%"),
    ("baromrelin", "Pressure",    "inHg"),
]


def svg_multi_chart(series_list: list[tuple[str, str, list[tuple[int, float]]]],
                    width: int = 640, height: int = 130,
                    unit: str = "") -> str:
    """N same-unit series on one axis: (label, color, points) each. Lines
    only — stacked area fills over each other read as mud. Series with
    fewer than 2 points are dropped rather than drawn as specks."""
    drawn = [(label, color, [(t, v) for t, v in pts if v is not None])
             for label, color, pts in series_list]
    drawn = [(label, color, pts) for label, color, pts in drawn if len(pts) >= 2]
    if len(drawn) < 2:
        return ""
    ys = [v for _, _, pts in drawn for _, v in pts]
    ts = [t for _, _, pts in drawn for t, _ in pts]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    t0, t1 = min(ts), max(ts)
    tspan = (t1 - t0) or 1
    pad = 4.0

    def px(t: int) -> float:
        return round(width * (t - t0) / tspan, 1)

    def py(v: float) -> float:
        return round((height - pad) - ((v - lo) / span) * (height - pad * 2), 1)

    grid, ylabels, time_row = _axis_html(lo, hi, unit, py, height)
    all_pts = [p for _, _, pts in drawn for p in pts]
    svg = [f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
           f'class="chart-svg" role="img">', grid]
    for _, color, pts in drawn:
        line = " ".join(f"{px(t)},{py(v)}" for t, v in pts)
        svg.append(f'<polyline points="{line}" fill="none" stroke="{color}" '
                   f'stroke-width="1.8" stroke-linejoin="round"/>')
    svg.append("</svg>")
    legend = ('<div class="chart-legend">'
              + "".join(f'<span class="lg"><i style="background:{color}"></i>'
                        f'{_esc(label)}</span>'
                        for label, color, _ in drawn)
              + "</div>")
    markers = _extreme_markers(all_pts, px, py, width, height, unit)
    return (f'<div class="chart-plot">{"".join(svg)}{ylabels}{markers}</div>'
            + legend + time_row)


def _age_text(ms: float) -> str:
    s_ = max(0, int(time.time() - ms / 1000))
    if s_ < 60:
        return f"{s_}s ago"
    if s_ < 3600:
        return f"{s_ // 60}m ago"
    return f"{s_ // 3600}h ago"


def render_now_strip(stations: list[dict[str, Any]],
                     location: str | None, tz_name: str = "UTC") -> str:
    """The "Right now" composite strip (Volney picked option A,
    2026-08-22): one compact band opening multi-station pages so a small
    embed frame — Doren's 520x390 iframe — shows a temperature, not the
    compare block's chart headers. Big temp + feels come from the PRIMARY
    station; each chip comes from the FRESHEST station reporting that
    field (the app's composite-tile philosophy: real readings, never
    cross-backyard averages), with the source station named in the chip's
    tooltip. Single-station pages skip it — their station header already
    opens the page.
    """
    if len(stations) < 2:
        return ""
    primary = stations[0]
    po = primary.get("obs") or {}
    temp = _num(po.get("tempf"))
    feels = _num(po.get("feelsLike"))
    temp_html = f"{round(temp)}°" if temp is not None else "—"
    feels_html = (f'<div class="cc-feels">feels {round(feels)}°</div>'
                  if feels is not None and (temp is None or abs(feels - temp) >= 1)
                  else "")

    def freshest(field: str):
        """(value, station_name) — newest observation carrying the field."""
        best = None
        for st in stations:
            o = st.get("obs") or {}
            v = _num(o.get(field))
            ts = _num(o.get("dateutc"))
            if v is None or ts is None:
                continue
            if best is None or ts > best[2]:
                best = (v, st.get("name") or "", ts)
        return (best[0], best[1]) if best else (None, "")

    chips = []
    for key in ("humidity", "windspeedmph", "baromrelin", "dailyrainin"):
        label, unit, color = {
            "humidity":     ("Humidity", "%",    "#4cb2ff"),
            "windspeedmph": ("Wind",     "mph",  "#39c9d6"),
            "baromrelin":   ("Pressure", "inHg", "#b39dff"),
            "dailyrainin":  ("Rain today", "in", "#5aa0ff"),
        }[key]
        v, src = freshest(key)
        title = f' title="from {_esc(src)}"' if src else ""
        chips.append(
            f'<div class="cc-chip"{title}>'
            f'<span class="cc-k">{_esc(label)}</span>'
            f'<span class="cc-v" style="color:{color}">{_fmt(v, unit)}</span></div>')

    # Second row (Volney, 2026-08-22, looking at Doren's single-station
    # embed): a compact version of the Today board — the PRIMARY station's
    # 24h numbers, matching the hero the way the single-station page's
    # board matches its header. Not cross-station: "hottest reading in the
    # yard at 4:47 PM" and "this station's 24h high" are different claims,
    # and the second one is the board's.
    today = []
    stats = primary.get("summary") or {}
    def tcell(label: str, value: str, sub: str = "") -> None:
        sub_html = f'<span class="cc-k">{_esc(sub)}</span>' if sub else ""
        today.append(f'<div class="cc-chip"><span class="cc-k">{_esc(label)}</span>'
                     f'<span class="cc-v now-today-v">{_esc(value)}</span>{sub_html}</div>')
    if stats.get("hi"):
        ts, v = stats["hi"]
        tcell("24h High", f"{round(v)}°", _local_time(ts, tz_name))
    if stats.get("lo"):
        ts, v = stats["lo"]
        tcell("24h Low", f"{round(v)}°", _local_time(ts, tz_name))
    if stats.get("gust_max") is not None:
        tcell("Max Gust", f'{stats["gust_max"]:.0f} mph')
    if stats.get("press_delta") is not None:
        tcell("Press Δ 24h", f'{stats["press_delta"]:+.2f} inHg')
    today_html = (f'<div class="cc-chips now-today">{"".join(today)}</div>'
                  if today else "")

    ages = [_num((st.get("obs") or {}).get("dateutc")) for st in stations]
    ages = [a for a in ages if a is not None]
    meta_bits = [f"{len(stations)} stations"]
    if location:
        meta_bits.append(_esc(location))
    if ages:
        meta_bits.append("updated " + _age_text(max(ages)))
    return (
        f'<section class="station now-strip">'
        f'  <div class="cc">'
        f'    <div class="cc-main">'
        f'      <div class="cc-name">RIGHT NOW'
        f'<span class="cc-loc"> · {" · ".join(meta_bits)}</span></div>'
        f'      <div class="cc-temp now-temp">{temp_html}</div>{feels_html}'
        f'      <div class="cc-chips">{"".join(chips)}</div>'
        f'      {today_html}'
        f'    </div>'
        f'  </div>'
        f'</section>')


def render_compare(stations: list[dict[str, Any]]) -> str:
    """The multi-station lead block. Empty string when it has nothing to
    say — fewer than two stations, or fewer than two with data for every
    candidate field (each chart decides for itself)."""
    if len(stations) < 2:
        return ""
    charts = []
    for field, label, unit in OVERLAY_FIELDS:
        series_list = [
            (s["name"], STATION_COLORS[i % len(STATION_COLORS)],
             s.get("series", {}).get(field) or [])
            for i, s in enumerate(stations)
        ]
        body = svg_multi_chart(series_list, unit=unit)
        if not body:
            continue
        charts.append(f'<div class="chart"><div class="chart-title">'
                      f'{_esc(label)} <span class="chart-unit">{unit} · '
                      f'all stations · 24h</span></div>{body}</div>')
    if not charts:
        return ""
    return (f'<div class="station station-compare">'
            f'<div class="cc-name">Side by side</div>'
            f'<div class="charts">{"".join(charts)}</div></div>')


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
    # Defence in depth: filter through _num so a non-finite direction or speed
    # can't reach int() below and 500 the page (the caller filters too).
    data = [(dv % 360.0, sv)
            for dv, sv in ((_num(d), _num(s)) for d, s in samples)
            if dv is not None and sv is not None]
    if len(data) < 3:
        return '<div class="chart-empty">no wind data in the last 24h</div>'

    sec = 360.0 / _ROSE_SECTORS
    nb = len(_ROSE_SPEED_BINS)
    counts = [[0] * nb for _ in range(_ROSE_SECTORS)]
    for d, s in data:
        if s < 0:
            # Ingest guarantees finiteness, not non-negativity: a negative
            # speed matches no (lo <= s < hi) bin and the next(..., nb-1)
            # default filed it into the STRONGEST petal — a glitch reading
            # plotted as extreme wind on the public page.
            continue
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
                     f'fill="none" style="stroke:var(--grid)" stroke-width="1"/>')
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
            parts.append(f'<path d="{d}" class="rose-b{bi}" '
                         f'fill-opacity="0.9"/>')
    # cardinal labels
    for k, lbl in enumerate(_COMPASS):
        lx, ly = pt(R + 11, k * 90.0)
        parts.append(f'<text x="{lx}" y="{ly}" class="rose-lbl" '
                     f'text-anchor="middle" dominant-baseline="middle">{lbl}</text>')
    parts.append('</svg>')

    legend = ['<div class="rose-legend">']
    for bi, lbl in enumerate(_ROSE_SPEED_LABELS):
        legend.append(f'<span class="rs"><i class="rose-b{bi}"></i>{lbl}</span>')
    legend.append('<span class="rs-unit">mph</span></div>')
    return "".join(parts) + "".join(legend)


def resolve_fields(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return CORE_FIELDS
    out = [f.strip() for f in raw.split(",") if f.strip() in FIELD_META]
    return out or CORE_FIELDS


def _esc(s: Any) -> str:
    return _html.escape(str(s), quote=True)


def summary_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The numbers behind the app main page's "Today · 24h" board. The 24h
    window the dashboard fetches is NEVER raw: db._auto_bucket_ms buckets
    anything past 6h into 1-minute AVG rows, so taking max() of the plain
    columns understates every extreme — a 21 mph gust averaged against the
    calm seconds around it rendered as "Max Gust 12" (CODE_REVIEW_R5
    R5-08). Prefer the per-bucket extreme columns the bucketed query
    carries for exactly this, falling back to the plain column for genuine
    raw rows. Every entry is None-when-absent — a station with no UV sensor
    gets no Max UV cell, not a confident 0 (the absent-is-not-zero rule)."""
    def col(key: str, extreme: str | None = None) -> list[tuple[int, float]]:
        out = []
        for r in rows:
            t = r.get("dateutc")
            v = _num(r.get(extreme)) if extreme else None
            if v is None:
                v = _num(r.get(key))
            if t is not None and v is not None:
                out.append((int(t), v))
        return out

    temps_hi = col("tempf", "tempf_max")
    temps_lo = col("tempf", "tempf_min")
    hums = col("humidity", "humidity_min")
    gusts = col("windgustmph", "windgustmph_max")
    uvs = col("uv", "uv_max")
    press = col("baromrelin")
    hi = max(temps_hi, key=lambda p: p[1]) if temps_hi else None
    lo = min(temps_lo, key=lambda p: p[1]) if temps_lo else None
    return {
        "hi": hi, "lo": lo,                       # (ts_ms, value) or None
        "gust_max": max(v for _, v in gusts) if gusts else None,
        "humidity_lo": min(v for _, v in hums) if hums else None,
        "uv_max": max(v for _, v in uvs) if uvs else None,
        "press_delta": (press[-1][1] - press[0][1]) if len(press) >= 2 else None,
        # Bucketed windows make this a POINT count, not a reading count —
        # the label must not claim "samples" (R5-08).
        "samples": len(rows),
    }


def _local_time(ts_ms: int, tz_name: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone
        tz = timezone.utc
    return datetime.fromtimestamp(ts_ms / 1000, tz).strftime("%-I:%M %p")


def render_summary_strip(stats: dict[str, Any] | None, tz_name: str) -> str:
    """The "Today · 24h" board: high/low with their local times, max gust,
    24h pressure change, humidity low, max UV. Cells render only for
    readings the station actually produced."""
    if not stats:
        return ""
    cells: list[str] = []

    def cell(label: str, value: str, sub: str = "") -> None:
        sub_html = f'<span class="cc-k">{_esc(sub)}</span>' if sub else ""
        cells.append(f'<div class="cc-chip"><span class="cc-k">{_esc(label)}</span>'
                     f'<span class="cc-v">{_esc(value)}</span>{sub_html}</div>')

    if stats.get("hi"):
        ts, v = stats["hi"]
        cell("24h High", f"{round(v)}°", _local_time(ts, tz_name))
    if stats.get("lo"):
        ts, v = stats["lo"]
        cell("24h Low", f"{round(v)}°", _local_time(ts, tz_name))
    if stats.get("gust_max") is not None:
        cell("Max Gust", f'{stats["gust_max"]:.0f} mph')
    if stats.get("press_delta") is not None:
        cell("Press Δ 24h", f'{stats["press_delta"]:+.2f} inHg')
    if stats.get("humidity_lo") is not None:
        cell("Humidity Low", f'{stats["humidity_lo"]:.0f}%')
    if stats.get("uv_max") is not None:
        cell("Max UV", f'{stats["uv_max"]:.0f}')
    if not cells:
        return ""
    n = stats.get("samples") or 0
    return (f'<div class="chart"><div class="chart-title">Today '
            f'<span class="chart-unit">· last 24h · {n:,} points</span></div>'
            f'<div class="cc-chips">{"".join(cells)}</div></div>')


def render_rain_periods(o: dict[str, Any]) -> str:
    """Today / Week / Month / Year rain, from the same enriched values the
    app's dashboard shows. Hidden entirely for a station with no rain
    counters — absent is not zero."""
    pairs = [("Today", _num(o.get("dailyrainin"))),
             ("Week", _num(o.get("weeklyrainin"))),
             ("Month", _num(o.get("monthlyrainin"))),
             ("Year", _num(o.get("yearlyrainin")))]
    if all(v is None for _, v in pairs):
        return ""
    cells = "".join(
        f'<div class="cc-chip"><span class="cc-k">{label}</span>'
        f'<span class="cc-v">{f"{v:.2f} in" if v is not None else "—"}</span></div>'
        for label, v in pairs)
    return (f'<div class="chart"><div class="chart-title">Rain '
            f'<span class="chart-unit">· by period</span></div>'
            f'<div class="cc-chips">{cells}</div></div>')


# ── Air-quality monitors (2.0) ───────────────────────────────────────────
# An AirGradient (or any device the backend classifies as an air monitor,
# db.is_air_monitor_device) gets its OWN card: a PM2.5 hero with a
# plain-language band, fixed tiles for the rest of its sensors, and a 24h
# PM2.5 chart. No weather hero, no wind/rain/pressure tiles, no records
# footer, and PUBLIC_DASHBOARD_FIELDS does not apply — "just their tiles"
# (Volney, 2026-09-01). Until now the page skipped monitors entirely, and
# the app's picker hid them so their macs could not land in the selection.

# US EPA PM2.5 breakpoints, 24-hour, as revised 2024-02-07 (the NAAQS
# update that lowered the Good/Moderate line from 12.0 to 9.0 µg/m³):
#
#   AQI category                     PM2.5 µg/m³ (24h)
#   Good                               0.0 -   9.0
#   Moderate                           9.1 -  35.4
#   Unhealthy for Sensitive Groups    35.5 -  55.4
#   Unhealthy                         55.5 - 125.4
#   Very Unhealthy                   125.5 - 225.4
#   Hazardous                        225.5 +
#
# EPA truncates the concentration to one decimal before looking it up,
# so 9.04 is Good and 9.10 is Moderate. Colors are existing page hues
# (STATION_COLORS / FIELD_META / the feels-like overlay red), painted
# only on a swatch dot: the band's TEXT stays in --ink-70 so the label
# reads on both themes whatever hue the band carries (yellow on white
# would not).
PM25_BANDS: list[tuple[float, str, str]] = [
    (9.0,   "Good",                           "#3ddc97"),
    (35.4,  "Moderate",                       "#ffd24c"),
    (55.4,  "Unhealthy for sensitive groups", "#ff9e33"),
    (125.4, "Unhealthy",                      "#ff5a5f"),
    (225.4, "Very unhealthy",                 "#b39dff"),
    (math.inf, "Hazardous",                   "#ff5a8f"),
]


def pm25_band(v: float | None) -> tuple[str, str] | None:
    """(label, color) for a PM2.5 reading, None when there is no reading.
    Truncated to one decimal first, the way EPA reads its own table."""
    if v is None:
        return None
    c = max(0.0, math.floor(v * 10.0 + 1e-9) / 10.0)
    for upper, label, color in PM25_BANDS:
        if c <= upper:
            return label, color
    return PM25_BANDS[-1][1], PM25_BANDS[-1][2]


# CO2 thresholds (ppm): indoor-air guidance treats 1000 as the point where
# a room reads "stuffy" and 2000 as where complaints start.
CO2_ELEVATED_PPM = 1000.0
CO2_HIGH_PPM = 2000.0


def co2_state(v: float | None) -> str | None:
    if v is None:
        return None
    if v >= CO2_HIGH_PPM:
        return "high"
    if v >= CO2_ELEVATED_PPM:
        return "elevated"
    return None


# PM2.5 chart hue. Text-bearing tiles use hues the page already paints
# chip values in (FIELD_META), so the air card keeps the contrast the
# weather chips keep.
PM25_COLOR = "#b39dff"
AIR_TILE_COLORS = {"pm10": "#5aa0ff", "co2": "#39c9d6",
                   "tvoc_index": "#b39dff", "nox_index": "#ff9e33"}


def _air_tile(label: str, value: str, sub: str, color: str) -> str:
    sub_html = f'<span class="cc-k">{_esc(sub)}</span>' if sub else ""
    return (f'<div class="cc-chip"><span class="cc-k">{_esc(label)}</span>'
            f'<span class="cc-v" style="color:{color}">{_esc(value)}</span>'
            f'{sub_html}</div>')


def render_air_station(name: str, obs: dict[str, Any] | None,
                       series: dict[str, list[tuple[int, float]]],
                       app_url: str = "", location: str | None = None) -> str:
    """The air card. Fixed tiles (PM10, CO2, TVOC index, NOx index) render
    whether or not the latest reading carries them, so a sensor still
    warming up shows "no reading" rather than a hole; temperature and
    humidity tiles appear ONLY when the monitor reports them (an outdoor
    AirGradient does, absent is not zero)."""
    o = obs or {}
    pm = _num(o.get("pm25"))
    band = pm25_band(pm)
    if pm is not None and band is not None:
        hero_html = (f'<div class="cc-temp aq-hero">{pm:.1f}'
                     f'<span class="aq-unit">µg/m³ PM2.5</span></div>'
                     f'<div class="aq-band"><i style="background:{band[1]}"></i>'
                     f'{_esc(band[0])}</div>')
    elif (co2_hero := _num(o.get("co2"))) is not None:
        # A CO₂ monitor (Govee H5140) has no particle sensor: its hero is
        # the CO₂ reading with the same 1000/2000 ppm states the tile uses.
        st = co2_state(co2_hero)
        colour = {"high": AIR_TILE_COLORS["co2"], "elevated": "#f6a13a"}.get(st or "", "#4ed98c")
        hero_html = (f'<div class="cc-temp aq-hero">{round(co2_hero)}'
                     f'<span class="aq-unit">ppm CO2</span></div>'
                     f'<div class="aq-band"><i style="background:{colour}"></i>'
                     f'{_esc((st or "fresh air").capitalize())}</div>')
    else:
        hero_html = '<div class="cc-feels aq-nodata">PM2.5 not reported yet</div>'

    tiles = []
    pm10 = _num(o.get("pm10"))
    tiles.append(_air_tile(
        "PM10", f"{pm10:.1f} µg/m³" if pm10 is not None else "no reading",
        "", AIR_TILE_COLORS["pm10"]))
    co2 = _num(o.get("co2"))
    state = co2_state(co2)
    tiles.append(_air_tile(
        "CO2" + (f" · {state}" if state else ""),
        f"{round(co2)} ppm" if co2 is not None else "no reading",
        f"{CO2_ELEVATED_PPM:.0f} elevated · {CO2_HIGH_PPM:.0f} high",
        AIR_TILE_COLORS["co2"]))
    # Sensirion VOC/NOx index: 1 to 500, 100 = the sensor's learned typical
    # air, higher = more than usual.
    for key, label in (("tvoc_index", "TVOC index"), ("nox_index", "NOx index")):
        v = _num(o.get(key))
        tiles.append(_air_tile(
            label, f"{round(v)}" if v is not None else "no reading",
            "1 to 500 · 100 typical", AIR_TILE_COLORS[key]))
    for key in ("tempf", "humidity"):
        v = _num(o.get(key))
        if v is None:
            continue
        meta = FIELD_META[key]
        tiles.append(_air_tile(meta["label"], _fmt(v, meta["unit"]), "",
                               meta["color"]))

    if series.get("pm25") or not series.get("co2"):
        chart = (f'<div class="chart"><div class="chart-title">PM2.5 '
                 f'<span class="chart-unit">· last 24h · µg/m³</span></div>'
                 f'{svg_chart(series.get("pm25", []), PM25_COLOR, unit="µg/m³")}'
                 f'</div>')
    else:
        chart = (f'<div class="chart"><div class="chart-title">CO2 '
                 f'<span class="chart-unit">· last 24h · ppm</span></div>'
                 f'{svg_chart(series.get("co2", []), AIR_TILE_COLORS["co2"], unit="ppm")}'
                 f'</div>')

    loc_html = (f'<span class="cc-loc"> · {_esc(location)}</span>'
                if location else "")
    side_html = ""
    if app_url:
        side_html = (f'<div class="cc-side">'
                     f'<a class="cc-app" href="{_esc(app_url)}" '
                     f'target="_blank" rel="noopener">Get the iOS app ↗</a>'
                     f'</div>')
    return (
        f'<section class="station station-air">'
        f'  <div class="cc">'
        f'    <div class="cc-main">'
        f'      <div class="cc-name">{_esc(name)}'
        f'<span class="cc-loc"> · Air quality</span>{loc_html}</div>'
        f'      {hero_html}'
        f'      <div class="cc-chips">{"".join(tiles)}</div>'
        f'    </div>'
        f'    {side_html}'
        f'  </div>'
        f'  <div class="charts">{chart}</div>'
        f'</section>'
    )


def render_station(name: str, obs: dict[str, Any] | None,
                   series: dict[str, list[tuple[int, float]]],
                   fields: list[str],
                   wind_samples: list[tuple[float, float]] | None = None,
                   records: dict[str, Any] | None = None,
                   tz_name: str = "UTC",
                   summary: dict[str, Any] | None = None,
                   app_url: str = "", location: str | None = None,
                   air: bool = False) -> str:
    """One station block: current-conditions header + a chart per field.

    The temperature chart overlays the feels-like line (from
    series["feelsLike"]); a wind rose tile is appended after the wind chart
    when direction+speed samples are available; an all-time records strip is
    appended below the charts when records are supplied.

    `air=True` (the caller classified the device as an air monitor) draws
    the air card instead: `fields`, wind, records and summary are ignored.
    """
    if air:
        return render_air_station(name, obs, series, app_url=app_url,
                                  location=location)
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
            body = svg_chart(series.get(key, []), meta["color"],
                             unit=meta["unit"])
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

    # The app main page's summary boards (Volney, 2026-08-20: the public
    # page should replace screenshots, so it carries the same boards).
    boards = render_summary_strip(summary, tz_name) + render_rain_periods(o)
    # The App Store link rides BESIDE the temperature (Volney: the full-width
    # top banner "takes up too much room and looks strange"), and the
    # operator-typed place label sits on the name line.
    loc_html = (f'<span class="cc-loc"> · {_esc(location)}</span>'
                if location else "")
    side_html = ""
    if app_url:
        import html as _h
        side_html = (f'<div class="cc-side">'
                     f'<a class="cc-app" href="{_h.escape(app_url, quote=True)}" '
                     f'target="_blank" rel="noopener">Get the iOS app ↗</a>'
                     f'</div>')
    return (
        f'<section class="station">'
        f'  <div class="cc">'
        f'    <div class="cc-main">'
        f'      <div class="cc-name">{_esc(name)}{loc_html}</div>'
        f'      <div class="cc-temp">{temp_html}</div>{feels_html}'
        f'      <div class="cc-chips">{"".join(chips)}</div>'
        f'    </div>'
        f'    {side_html}'
        f'  </div>'
        f'  <div class="charts">{boards}{"".join(charts)}</div>'
        f'  {render_records(records, tz_name)}'
        f'</section>'
    )


# All-time record cards shown under the charts. (field, hi|lo, label, unit).
_RECORD_CARDS = [
    ("tempf",        "max", "Hottest",        "°F"),
    ("tempf",        "min", "Coldest",        "°F"),
    ("feelsLike",    "max", "Hottest feels",  "°F"),
    ("feelsLike",    "min", "Coldest feels",  "°F"),
    ("dewPoint",     "max", "Highest dew pt", "°F"),
    ("dewPoint",     "min", "Lowest dew pt",  "°F"),
    ("windgustmph",  "max", "Peak gust",      "mph"),
    ("dailyrainin",  "max", "Wettest day",    "in"),
    ("baromrelin",   "max", "High pressure",  "inHg"),
    ("baromrelin",   "min", "Low pressure",   "inHg"),
]


def _record_date(ms: Any, tz_name: str) -> str:
    """Short local date for a record's timestamp, e.g. 'Jul 3, 2026'."""
    if not ms:
        return ""
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=tz)
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    except Exception:
        return ""


def render_records(records: dict[str, Any] | None, tz_name: str) -> str:
    """Compact all-time records strip (hottest/coldest/peak gust/etc.)."""
    if not records:
        return ""
    allf = (records.get("periods", {}).get("all", {}) or {}).get("fields", {}) or {}
    cards = []
    for field, which, label, unit in _RECORD_CARDS:
        rec = allf.get(field)
        if not rec:
            continue
        val = rec.get(which)
        if val is None:
            continue
        at = rec.get("maxAt" if which == "max" else "minAt")
        when = _record_date(at, tz_name)
        cards.append(
            f'<div class="rec"><div class="rec-k">{_esc(label)}</div>'
            f'<div class="rec-v">{_fmt(_num(val), unit)}</div>'
            f'<div class="rec-d">{_esc(when)}</div></div>'
        )
    if not cards:
        return ""
    return (
        f'<div class="records"><div class="records-h">Records '
        f'<span class="chart-unit">· all-time</span></div>'
        f'<div class="records-grid">{"".join(cards)}</div></div>'
    )


def render_dashboard(stations: list[dict[str, Any]], fields: list[str],
                     tz_name: str = "UTC",
                     app_url: str = "", location: str | None = None) -> str:
    """Full dashboard section for all selected stations. With 2+ stations a
    side-by-side compare block leads (temp/feels/humidity/pressure, every
    station on one axis per field), then the per-station blocks."""
    if not stations:
        return '<div class="chart-empty">No station data yet.</div>'
    # The composite strip and the side-by-side charts are weather-station
    # affairs: a monitor's humidity chip would otherwise win "freshest"
    # and its temperature would join the compare block as a fifth line.
    # Air stations (`"air": True` from the caller) keep their place in the
    # page order and render as their own card.
    weather = [s for s in stations if not s.get("air")]
    return (render_now_strip(weather, location, tz_name)
            + render_compare(weather) + "".join(
        render_station(s["name"], s.get("obs"), s.get("series", {}), fields,
                       wind_samples=s.get("wind_samples"),
                       records=s.get("records"), tz_name=tz_name,
                       summary=s.get("summary"),
                       # First station only: one link and one place label per
                       # page, however many stations it shows.
                       app_url=app_url if i == 0 else "",
                       location=location if i == 0 else None,
                       air=bool(s.get("air")))
        for i, s in enumerate(stations)
    ))


# Shared theme tokens for every page that renders the dashboard fragment
# (the "/" status page and /embed). Dark is the default; light applies via
# the visitor's system preference, and /embed can FORCE either with
# ?theme= (data-theme on <html> wins over the media query). Light values
# are tuned for contrast, not naively inverted — the light-mode lesson
# from the app applies to the web too.
_TOKENS_DARK = (
    "--page-bg:#0d0f12; --page-fg:#fff; "
    "--ink-70:rgba(255,255,255,0.7); --ink-55:rgba(255,255,255,0.55); "
    "--ink-50:rgba(255,255,255,0.5); --ink-40:rgba(255,255,255,0.4); "
    "--ink-38:rgba(255,255,255,0.38); --ink-35:rgba(255,255,255,0.35); "
    "--card-bg:rgba(255,255,255,0.03); --card-edge:rgba(255,255,255,0.06); "
    "--grid:rgba(255,255,255,0.10); "
    "--rose-0:#cdeef2; --rose-1:#7fdce4;"
)
_TOKENS_LIGHT = (
    "--page-bg:#f2f4f7; --page-fg:#14171c; "
    "--ink-70:rgba(15,20,28,0.78); --ink-55:rgba(15,20,28,0.66); "
    "--ink-50:rgba(15,20,28,0.62); --ink-40:rgba(15,20,28,0.55); "
    "--ink-38:rgba(15,20,28,0.53); --ink-35:rgba(15,20,28,0.5); "
    "--card-bg:#ffffff; --card-edge:rgba(15,20,28,0.12); "
    "--grid:rgba(15,20,28,0.12); "
    "--rose-0:#8fcdd8; --rose-1:#57b9c6;"
)
THEME_CSS = f"""
    :root {{ color-scheme: dark; {_TOKENS_DARK} }}
    @media (prefers-color-scheme: light) {{
      :root:not([data-theme="dark"]) {{ color-scheme: light; {_TOKENS_LIGHT} }}
    }}
    :root[data-theme="light"] {{ color-scheme: light; {_TOKENS_LIGHT} }}
    body {{ background: var(--page-bg); color: var(--page-fg); }}
"""


# CSS injected into the status page's <style> when the dashboard is on. This is
# a plain string inserted into the page f-string via a {placeholder}, so its
# value is copied verbatim — use single (normal CSS) braces here.
DASHBOARD_CSS = """
    .station { margin-bottom: 28px; }
    .cc { margin-bottom: 14px; display:flex; align-items:flex-start;
        justify-content:space-between; gap:16px; flex-wrap:wrap; }
    .cc-main { min-width:0; }
    .cc-side { flex-shrink:0; padding-top:22px; }
    .cc-app { display:inline-flex; align-items:center; gap:6px; font-size:12px;
        font-weight:600; color:#0b0d13; background:#fff; border:1px solid var(--card-edge); border-radius:9px;
        padding:8px 14px; text-decoration:none; white-space:nowrap; }
    .cc-name { font-size:11px; font-weight:800; letter-spacing:1.2px;
        text-transform:uppercase; color:var(--ink-50); }
    .cc-loc { font-weight:600; letter-spacing:0.4px; text-transform:none;
        color:var(--ink-38); }
    .cc-temp { font-size:56px; font-weight:200; line-height:1; margin-top:2px; }
    .now-strip { padding-bottom:6px; }
    .now-temp { font-size:48px; }
    .now-today { margin-top:8px; padding-top:8px;
        border-top:1px solid var(--card-edge); }
    .now-today .now-today-v { color:var(--ink-70); }
    .cc-feels { font-size:13px; color:var(--ink-55); margin-top:2px; }
    .cc-chips { display:flex; flex-wrap:wrap; gap:16px; margin-top:10px; }
    .cc-k { font-size:9px; font-weight:700; letter-spacing:0.8px; text-transform:uppercase;
        color:var(--ink-40); display:block; }
    .cc-v { font-size:15px; font-weight:600; }
    .charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; }
    .chart { background:var(--card-bg); border:1px solid var(--card-edge);
        border-radius:12px; padding:12px 14px; }
    .chart-title { font-size:11px; font-weight:700; color:var(--ink-70); margin-bottom:6px; }
    .chart-unit { font-weight:400; color:var(--ink-35); }
    .chart-svg { width:100%; height:110px; display:block; }
    .chart-axis { display:flex; justify-content:space-between; font-size:9px;
        color:var(--ink-35); margin-top:2px; }
    .chart-plot { position:relative; }
    .chart-plot .chart-grid { stroke:var(--grid); stroke-width:1;
        vector-effect:non-scaling-stroke; }
    .chart-yl { position:absolute; left:4px; transform:translateY(-110%);
        font-size:8.5px; color:var(--ink-35); pointer-events:none; }
    .chart-dot { position:absolute; width:7px; height:7px; border-radius:50%;
        background:var(--page-fg); border:1.5px solid var(--page-bg);
        transform:translate(-50%,-50%); pointer-events:none; }
    .chart-xl { position:absolute; font-size:9px; font-weight:700;
        color:var(--ink-70); background:var(--page-bg); border-radius:4px;
        padding:0 3px; pointer-events:none; white-space:nowrap; }
    .chart-xl-below { transform:translate(-50%,7px); }
    .chart-xl-above { transform:translate(-50%,calc(-100% - 7px)); }
    .chart-empty { font-size:12px; color:var(--ink-40); padding:20px 0; }
    .chart-legend { display:flex; gap:14px; margin-top:6px; }
    .chart-legend .lg { display:inline-flex; align-items:center; gap:5px;
        font-size:10px; color:var(--ink-55); }
    .chart-legend .lg i { width:14px; height:0; border-top:2px solid; display:inline-block; }
    .chart-rose { display:flex; flex-direction:column; }
    .rose-svg { width:100%; max-width:210px; height:auto; margin:2px auto 0; display:block; }
    .rose-svg path { stroke:var(--grid); stroke-width:0.5; }
    .rose-b0 { fill:var(--rose-0); background:var(--rose-0); }
    .rose-b1 { fill:var(--rose-1); background:var(--rose-1); }
    .rose-b2 { fill:#39c9d6; background:#39c9d6; }
    .rose-b3 { fill:#2b93b3; background:#2b93b3; }
    .rose-b4 { fill:#1f6f9e; background:#1f6f9e; }
    .rose-lbl { fill:var(--ink-50); font-size:11px; font-weight:700; }
    .rose-legend { display:flex; flex-wrap:wrap; justify-content:center; gap:10px; margin-top:8px; }
    .rose-legend .rs { display:inline-flex; align-items:center; gap:4px;
        font-size:9px; color:var(--ink-50); }
    .rose-legend .rs i { width:9px; height:9px; border-radius:2px; display:inline-block; }
    .rose-legend .rs-unit { font-size:9px; color:var(--ink-35); }
    .records { margin-top:18px; }
    .records-h { font-size:11px; font-weight:700; color:var(--ink-70); margin-bottom:8px; }
    .records-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; }
    .rec { background:var(--card-bg); border:1px solid var(--card-edge);
        border-radius:10px; padding:10px 12px; }
    .rec-k { font-size:9px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;
        color:var(--ink-40); }
    .rec-v { font-size:19px; font-weight:600; margin-top:3px; }
    .rec-d { font-size:10px; color:var(--ink-40); margin-top:2px; }
    .aq-hero { font-size:44px; }
    .aq-unit { font-size:13px; font-weight:500; color:var(--ink-55); margin-left:8px; }
    .aq-band { display:inline-flex; align-items:center; gap:6px; margin-top:6px;
        font-size:13px; font-weight:600; color:var(--ink-70); }
    .aq-band i { width:10px; height:10px; border-radius:50%; display:inline-block;
        border:1px solid var(--card-edge); }
    .aq-nodata { margin-top:8px; }
"""


# A tiny corner anemometer whose cup wheel spins while the page is
# working (Volney's pick from four options, 2026-08-25) — visible during
# the initial load, faded out once the page settles, and woken back up for
# the last moment before the auto-refresh navigates. Purely decorative:
# fixed-position, pointer-events none, aria-hidden, so it can never block
# or shift content. Born 2026-08-25, the day 1.7.1's instant embed made
# Doren retire his hand-made site preloader ("I loved seeing the icon I
# made though!") — the page keeps a little something to look at.
# The color var carries a fallback because the status page can render
# without the dashboard token set.
# The settle script, kept as its own constant so the CSP middleware can
# allow exactly these bytes by sha256 hash (script-src carries no
# 'unsafe-inline'; a hash pins the one script we mean to run). The body
# must stay byte-identical to what lands between the <script> tags —
# SPINNER_HTML composes it below, and a test hashes the served page to
# keep the two from drifting.
SPINNER_SCRIPT = """
(function () {
  var s = document.getElementById("zw-spin");
  if (!s) return;
  function settle() { setTimeout(function () { s.classList.add("zw-done"); }, 700); }
  if (document.readyState === "complete") settle();
  else window.addEventListener("load", settle);
  // Back-forward cache restores skip 'load' — fade again on return.
  window.addEventListener("pageshow", function (e) { if (e.persisted) settle(); });
  // Wake just before the page's own auto-refresh navigates, so the spin
  // covers the reload gap instead of appearing after it.
  var meta = document.querySelector('meta[http-equiv="refresh"]');
  var secs = meta ? parseInt(meta.getAttribute("content"), 10) : 0;
  if (secs > 3) {
    setTimeout(function () { s.classList.remove("zw-done"); }, (secs - 2) * 1000);
  }
  window.addEventListener("pagehide", function () { s.classList.remove("zw-done"); });
})();
"""

SPINNER_HTML = """
<style>
  #zw-spin { position:fixed; right:10px; bottom:10px; width:24px; height:24px;
             pointer-events:none; z-index:9; opacity:.55;
             transition:opacity .6s ease;
             color:var(--ink-40, rgba(128,138,148,.8)); }
  #zw-spin.zw-done { opacity:0; }
  #zw-spin svg { width:100%; height:100%; display:block; }
  #zw-spin .zw-wheel { transform-box: view-box; transform-origin: 50% 50%;
                       animation: zw-rot 1.6s linear infinite; }
  @media (prefers-reduced-motion: reduce) { #zw-spin .zw-wheel { animation:none; } }
  @keyframes zw-rot { to { transform: rotate(360deg); } }
</style>
<div id="zw-spin" aria-hidden="true">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round">
    <path d="M12 12v9M8.5 21h7"/>
    <g class="zw-wheel">
      <path d="M12 12L12 7M12 12L7.7 14.5M12 12L16.3 14.5"/>
      <circle cx="12" cy="6.2" r="1.8" fill="currentColor" stroke="none"/>
      <circle cx="7" cy="14.9" r="1.8" fill="currentColor" stroke="none"/>
      <circle cx="17" cy="14.9" r="1.8" fill="currentColor" stroke="none"/>
    </g>
  </svg>
</div>
<script>""" + SPINNER_SCRIPT + """</script>
"""
