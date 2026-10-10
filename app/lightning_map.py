"""Lightning on a map, as rings (2.6, Volney 10-09).

A Tempest's lightning sensor (an AS3935) estimates how FAR a strike was,
in coarse bands, and nothing about which way: its own maker says strikes
"cannot be mapped" with it. So a strike is drawn as what the station
knows, a ring around the station at the reported distance, as wide as the
sensor's uncertainty, coloured by its age the way lightning maps colour
strikes. Reference rings at 3, 6 and 10 miles make "is it close?"
readable at a glance, and a distance-over-time strip under the rings
shows whether the storm is coming or going (the Tempest app's own graph).

The map's centre never reveals more than the owner already chose to
publish: it is the point the station map publishes for this station
(map_beacon: exact, area, city or custom), and with map sharing off the
rings are drawn without a basemap or coordinates at all.

Tiles are OpenStreetMap's own raster tiles, the ones maps.zasder.com
uses, with the attribution their policy asks for. Units stay API-native
(miles) to the drawing.
"""
from __future__ import annotations

import html as _html
import math
from typing import Any

# The sensor reports in bands; about three miles either side is the best
# case its datasheet and Tempest's own notes give.
BAND_MI = 3.0
REFERENCE_RINGS_MI = (3.0, 6.0, 10.0)
WINDOW_MS = 3 * 3_600_000
MI_TO_M = 1609.344
TILE_PX = 256
OSM_TILE_HOST = "https://tile.openstreetmap.org"
# The card's square, in CSS pixels before it scales to the card width.
SIZE_PX = 360
# The most distance reports a card draws. More than that are thinned
# evenly across the window (the newest always kept) and flagged; the trend
# is computed from all of them first (follow-up review F26-04).
MAX_DRAWN = 200
# The most reports one query reads. A Tempest reports at most once a
# minute (1,440 a day); this leaves room for faster feeds, and hitting it
# is flagged as truncation like the drawing cap.
QUERY_CAP = 5000
# Web Mercator's limit: beyond it the projection divides by zero at the
# poles (F26-05). A station past it gets rings without a basemap.
MAX_MERCATOR_LAT = 85.05112878


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def strikes_in_window(strikes: list[dict[str, Any]], now_ms: int,
                      window_ms: int = WINDOW_MS) -> list[dict[str, Any]]:
    """Distinct strikes inside the window, newest first, each with its age.
    A strike is one last-strike time; the station repeats it on every
    reading until the next one, so readings collapse to strikes here."""
    seen: dict[int, float] = {}
    for s in strikes:
        ms, d = s.get("ms"), _num(s.get("distance_mi"))
        if not isinstance(ms, (int, float)) or isinstance(ms, bool) or d is None or d < 0:
            continue
        ms = int(ms)
        if now_ms - window_ms <= ms <= now_ms + 60_000:
            seen[ms] = d
    out = [{"ms": ms, "distance_mi": d, "age_min": max(0, (now_ms - ms) // 60_000)}
           for ms, d in seen.items()]
    out.sort(key=lambda s: -s["ms"])
    return out


# Strike age as colour, the lightning-map convention (Blitzortung and the
# maps built on it): white for the newest, through yellow and orange, to
# dark red for the oldest. Their bins are 20 minutes over two hours; the
# rings span three, so the last bin is wider. (upper bound in minutes,
# colour, legend label). Twin of LightningRingsCard.ageBins in the app.
AGE_BINS = (
    (10, "#ffffff", "0-10 min"),
    (20, "#ffe14d", "10-20"),
    (40, "#ffb020", "20-40"),
    (60, "#ff7a1a", "40-60"),
    (120, "#e53935", "1-2 h"),
    (10**9, "#8b1a1a", "2-3 h"),
)
# A storm has moved when its distance changed by at least one sensor band.
TREND_MIN_MI = BAND_MI
TREND_SPLIT_MIN = 30
TREND_WINDOW_MS = 2 * TREND_SPLIT_MIN * 60_000


def thin(strikes: list[dict[str, Any]], n: int = MAX_DRAWN) -> list[dict[str, Any]]:
    """At most `n` of `strikes` (newest first), spread evenly across them so
    a long window keeps its shape; the newest is always kept."""
    if len(strikes) <= n:
        return strikes
    step = (len(strikes) - 1) / (n - 1)
    return [strikes[round(i * step)] for i in range(n)]


def age_bin(age_min: float) -> int:
    for i, (upper, _c, _l) in enumerate(AGE_BINS):
        if age_min < upper:
            return i
    return len(AGE_BINS) - 1


def age_color(age_min: float) -> str:
    return AGE_BINS[age_bin(age_min)][1]


def bin_counts(strikes: list[dict[str, Any]]) -> list[int]:
    out = [0] * len(AGE_BINS)
    for s in strikes:
        out[age_bin(s["age_min"])] += 1
    return out


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def trend(strikes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Is the storm coming or going? The sensor estimates the distance to
    the storm's near edge, so the median distance of the last 30 minutes
    against the 30 before it is the storm's movement. One band (3 mi) is
    the least change that means anything. None without strikes in both."""
    recent = [s["distance_mi"] for s in strikes if s["age_min"] < TREND_SPLIT_MIN]
    earlier = [s["distance_mi"] for s in strikes
               if TREND_SPLIT_MIN <= s["age_min"] < 2 * TREND_SPLIT_MIN]
    if not recent or not earlier:
        return None
    r, e = _median(recent), _median(earlier)
    if r <= e - TREND_MIN_MI:
        verdict = "approaching"
    elif r >= e + TREND_MIN_MI:
        verdict = "moving away"
    else:
        verdict = "holding"
    return {"verdict": verdict, "recent_mi": round(r, 1), "earlier_mi": round(e, 1)}


def zoom_for(lat: float, radius_mi: float, size_px: int = SIZE_PX) -> int:
    """The highest Web Mercator zoom at which a circle of radius_mi still
    fits in the square with a little margin."""
    want_px = size_px / 2 * 0.9
    for z in range(13, 2, -1):
        m_per_px = 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)
        if radius_mi * MI_TO_M / m_per_px <= want_px:
            return z
    return 3


def _world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = TILE_PX * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _tiles_html(lat: float, lon: float, z: int, size: int, scale: float = 1.0) -> str:
    """Tiles at zoom z, each drawn `scale` times its size (1 to 2), so the
    rings fill the square exactly instead of jumping by whole zooms."""
    tile = TILE_PX * scale
    cx, cy = _world_px(lat, lon, z)
    cx, cy = cx * scale, cy * scale
    left, top = cx - size / 2, cy - size / 2
    n = 2 ** z
    imgs = []
    for tx in range(int(left // tile), int((left + size) // tile) + 1):
        for ty in range(int(top // tile), int((top + size) // tile) + 1):
            if not 0 <= ty < n:
                continue
            x = tx * tile - left
            y = ty * tile - top
            pct_x, pct_y = 100 * x / size, 100 * y / size
            pct_w = 100 * tile / size
            src = f"{OSM_TILE_HOST}/{z}/{tx % n}/{ty}.png"
            imgs.append(
                f'<img src="{src}" alt="" loading="lazy" decoding="async" '
                f'style="left:{pct_x:.3f}%;top:{pct_y:.3f}%;width:{pct_w:.3f}%">')
    return "".join(imgs)


def _rings_svg(strikes: list[dict[str, Any]], radius_mi: float, size: int,
               px_per_mi: float) -> str:
    c = size / 2
    parts = []
    for r in REFERENCE_RINGS_MI:
        if r <= radius_mi:
            pr = r * px_per_mi
            parts.append(f'<circle class="lt-ref" cx="{c}" cy="{c}" r="{pr:.1f}"/>'
                         f'<text class="lt-ref-l" x="{c + 3:.1f}" y="{c - pr - 3:.1f}">'
                         f'{r:g} mi</text>')
    # Oldest first, so the newest ring is drawn on top.
    # The band is drawn faint and the strike's own line strong, in its age
    # colour with a dark edge, so the newest (white) still reads on a light
    # map and overlapping rings stay separable.
    w = max(2.0, BAND_MI * px_per_mi)
    for s in reversed(strikes):
        pr = s["distance_mi"] * px_per_mi
        col = age_color(s["age_min"])
        parts.append(f'<circle class="lt-band" cx="{c}" cy="{c}" r="{pr:.1f}" '
                     f'stroke="{col}" stroke-width="{w:.1f}"/>'
                     f'<circle class="lt-edge" cx="{c}" cy="{c}" r="{pr:.1f}"/>'
                     f'<circle class="lt-strike" cx="{c}" cy="{c}" r="{pr:.1f}" '
                     f'stroke="{col}"/>')
    parts.append(f'<circle class="lt-home" cx="{c}" cy="{c}" r="5"/>')
    return (f'<svg class="lt-svg" viewBox="0 0 {size} {size}" '
            f'role="img" aria-label="Lightning distance rings">{"".join(parts)}</svg>')


TREND_WORDS = {
    "approaching": ("Approaching", "lt-t-in"),
    "moving away": ("Moving away", "lt-t-out"),
    "holding": ("Holding distance", "lt-t-hold"),
}


def timeline_svg(strikes: list[dict[str, Any]], radius_mi: float,
                 window_min: int = WINDOW_MS // 60_000) -> str:
    """Distance over time, the Tempest app's own lightning graph: three
    hours left to right, near at the bottom, each strike a dot in its age
    colour. A storm coming in is a line of dots falling toward the 6 mi
    line."""
    w, h, pad_l, pad_b = 360, 110, 30, 16
    pw, ph = w - pad_l - 6, h - pad_b - 6

    def x(age):
        return pad_l + pw * (1 - min(age, window_min) / window_min)

    def y(mi):
        return 6 + ph * (1 - min(mi, radius_mi) / radius_mi)
    parts = [f'<line class="lt-axis" x1="{pad_l}" y1="{6 + ph}" x2="{w - 6}" y2="{6 + ph}"/>']
    for mi in (6.0, radius_mi):
        cls = "lt-six" if mi == 6.0 else "lt-grid"
        if mi <= radius_mi:
            parts.append(f'<line class="{cls}" x1="{pad_l}" y1="{y(mi):.1f}" '
                         f'x2="{w - 6}" y2="{y(mi):.1f}"/>'
                         f'<text class="lt-ax-l" x="{pad_l - 3}" y="{y(mi) + 3:.1f}" '
                         f'text-anchor="end">{mi:.0f} mi</text>')
    for label, age in (("3h ago", window_min), ("2h", 120), ("1h", 60), ("now", 0)):
        anchor = "start" if age == window_min else ("end" if age == 0 else "middle")
        parts.append(f'<text class="lt-ax-l" x="{x(age):.1f}" y="{h - 3}" '
                     f'text-anchor="{anchor}">{label}</text>')
    for s in reversed(strikes):
        parts.append(f'<circle class="lt-dot" cx="{x(s["age_min"]):.1f}" '
                     f'cy="{y(s["distance_mi"]):.1f}" r="4" fill="{age_color(s["age_min"])}"/>')
    return (f'<svg class="lt-time" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="Lightning distance over the last 3 hours">{"".join(parts)}</svg>')


def _legend(strikes: list[dict[str, Any]]) -> str:
    counts = bin_counts(strikes)
    items = "".join(
        f'<span class="lt-key"><i style="background:{col}"></i>{label}'
        f'<b>{n}</b></span>'
        for (_u, col, label), n in zip(AGE_BINS, counts))
    return (f'<div class="lt-legend" aria-label="Distance reports by age">'
            f'<span class="lt-key lt-key-h">Distance reports by age</span>{items}</div>')


def _count(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None and f >= 0 and not isinstance(v, bool) else None


def count_line(count_1h: Any, count_3h: Any) -> str | None:
    """The detector's own trailing counts, which are the real number of
    strikes. The rings are sampled LAST-strike reports, one per reading at
    most, so their number is never a strike count (F26-04)."""
    c1, c3 = _count(count_1h), _count(count_3h)
    if c1 is None and c3 is None:
        return None
    def n(c):
        return f"{c} strike" + ("" if c == 1 else "s")
    if c1 is not None and c3 is not None:
        return f"The detector counted {n(c1)} in the last hour and {c3} in 3 hours."
    if c1 is not None:
        return f"The detector counted {n(c1)} in the last hour."
    return f"The detector counted {n(c3)} in the last 3 hours."


def _lines(strikes: list[dict[str, Any]], counts: tuple[Any, Any] = (None, None),
           truncated: bool = False, all_strikes: list[dict[str, Any]] | None = None) -> str:
    if not strikes:
        return '<div class="lt-note">No strikes in the last 3 hours.</div>'
    s = strikes[0]
    near = s["distance_mi"] < 6
    age = "just now" if s["age_min"] < 1 else f'{s["age_min"]} min ago'
    head = (f'<div class="lt-last{" lt-near" if near else ""}">Last strike '
            f'{s["distance_mi"]:.0f} mi away · {age}</div>')
    t = trend(all_strikes if all_strikes is not None else strikes)
    if t:
        word, cls = TREND_WORDS[t["verdict"]]
        head += (f'<div class="lt-trend {cls}">{word}: about {t["recent_mi"]:.0f} mi '
                 f'in the last 30 min, {t["earlier_mi"]:.0f} mi the 30 before</div>')
    counted = count_line(*counts)
    note = ((counted + " ") if counted else "") + (
        "Each ring is a distance report from the station's latest strike; "
        "the sensor knows how far, not which way.")
    if truncated:
        note += " Showing a sample of the reports."
    return head + f'<div class="lt-note">{_html.escape(note)}</div>'


def _mappable(center: Any) -> tuple[float, float] | None:
    """The centre if Web Mercator can draw it, else None (plain rings)."""
    try:
        lat, lon = float(center[0]), float(center[1])
    except (OverflowError, TypeError, ValueError, IndexError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > MAX_MERCATOR_LAT \
            or abs(lon) > 180:
        return None
    return lat, lon


def render_card(strikes: list[dict[str, Any]], now_ms: int,
                center: tuple[float, float] | None,
                counts: tuple[Any, Any] = (None, None)) -> str:
    """The lightning card. `center` is the PUBLISHED point (or None, which
    draws the rings with no basemap and no coordinates). `counts` are the
    detector's trailing 1 h and 3 h strike counts, the real totals."""
    everything = strikes_in_window(strikes, now_ms)
    recent = thin(everything)
    truncated = len(recent) < len(everything)
    farthest = max([s["distance_mi"] for s in recent] or [0.0])
    radius_mi = max(REFERENCE_RINGS_MI[-1], farthest + BAND_MI)
    size = SIZE_PX
    center = _mappable(center) if center is not None else None
    if center is not None:
        lat, lon = center
        z = zoom_for(lat, radius_mi, size)
        m_per_px = 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)
        # The farthest ring fills the square's margin exactly: the tiles
        # stretch by up to 2x to meet it, not the rings shrink to the zoom.
        px_per_mi = (size / 2 * 0.9) / radius_mi
        scale = px_per_mi / (MI_TO_M / m_per_px)
        base = (f'<div class="lt-tiles">{_tiles_html(lat, lon, z, size, scale)}</div>'
                f'<div class="lt-attr">© <a href="https://www.openstreetmap.org/copyright" '
                f'target="_blank" rel="noopener">OpenStreetMap</a> contributors</div>')
        cls = "lt-map"
    else:
        px_per_mi = (size / 2 * 0.9) / radius_mi
        base = ""
        cls = "lt-map lt-plain"
    return (f'<div class="chart chart-lightning"><div class="chart-title">Lightning '
            f'<span class="chart-unit">· last 3h · distance rings</span></div>'
            f'<div class="{cls}">{base}{_rings_svg(recent, radius_mi, size, px_per_mi)}</div>'
            f'{_legend(recent) if recent else ""}'
            f'{timeline_svg(recent, radius_mi) if recent else ""}'
            f'{_lines(recent, counts, truncated, everything)}</div>')


def chip(obs: dict[str, Any]) -> str | None:
    """The Lightning chip beside the others, or None for a station without
    a detector (no lightning_last_1hr at all: absent is not zero)."""
    n = obs.get("lightning_last_1hr")
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return None
    n = int(n)
    if n <= 0:
        value, color = "Clear", "var(--ink-70)"
    else:
        d = _num(obs.get("lightning_distance_mi"))
        value = f"{n}/hr" + (f" · {d:.0f} mi" if d is not None else "")
        color = "#ffb020" if d is None or d >= 6 else "#ff5a5f"
    return (f'<div class="cc-chip"><span class="cc-k">Lightning</span>'
            f'<span class="cc-v" style="color:{color}">{_html.escape(value)}</span></div>')


CSS = """
    .chart-lightning { display:flex; flex-direction:column; }
    .lt-map { position:relative; width:100%; max-width:360px; aspect-ratio:1/1;
        margin:2px auto 0; overflow:hidden; border-radius:10px;
        background:var(--card-bg); }
    .lt-tiles img { position:absolute; display:block; height:auto; aspect-ratio:1/1; max-width:none;
        filter: invert(1) hue-rotate(180deg) brightness(0.85) contrast(0.9); }
    @media (prefers-color-scheme: light) {
      :root:not([data-theme="dark"]) .lt-tiles img { filter:none; }
    }
    :root[data-theme="light"] .lt-tiles img { filter:none; }
    .lt-svg { position:absolute; inset:0; width:100%; height:100%; }
    .lt-ref { fill:none; stroke:var(--ink-50); stroke-width:1; stroke-dasharray:4 4; }
    .lt-ref-l { font-size:10px; fill:var(--ink-70); font-weight:700; }
    .lt-band { fill:none; stroke-opacity:0.18; }
    .lt-edge { fill:none; stroke:#000; stroke-opacity:0.45; stroke-width:4.5; }
    .lt-strike { fill:none; stroke-width:2.5; }
    .lt-legend { display:flex; flex-wrap:wrap; gap:4px 10px; margin-top:8px;
        font-size:10px; color:var(--ink-70); }
    .lt-key { display:inline-flex; align-items:center; gap:4px; }
    .lt-key i { width:10px; height:10px; border-radius:50%;
        box-shadow:0 0 0 1px rgba(0,0,0,0.45); }
    .lt-key-h { flex-basis:100%; font-weight:700; }
    .lt-key b { color:var(--ink-90, inherit); font-weight:700; }
    .lt-time { width:100%; max-width:360px; margin:6px auto 0; display:block; }
    .lt-axis { stroke:var(--ink-50); stroke-width:1; }
    .lt-grid { stroke:var(--ink-50); stroke-width:0.5; stroke-dasharray:2 4; }
    .lt-six { stroke:#ff5a5f; stroke-width:1; stroke-dasharray:4 3; }
    .lt-ax-l { font-size:9px; fill:var(--ink-70); }
    .lt-dot { stroke:rgba(0,0,0,0.5); stroke-width:1; }
    .lt-trend { font-size:12px; font-weight:700; margin-top:2px; }
    .lt-t-in { color:#ff5a5f; }
    .lt-t-out { color:#3fb950; }
    .lt-t-hold { color:var(--ink-70); }
    .lt-home { fill:#39c9d6; stroke:var(--page-bg); stroke-width:2; }
    .lt-attr { position:absolute; right:4px; bottom:3px; font-size:9px;
        color:var(--ink-55); background:var(--page-bg); border-radius:3px; padding:0 3px; }
    .lt-attr a { color:inherit; }
    .lt-last { font-size:12px; font-weight:700; color:#ffb020; margin-top:8px; }
    .lt-last.lt-near { color:#ff5a5f; }
    .lt-note { font-size:11px; color:var(--ink-55); margin-top:4px; }
"""
