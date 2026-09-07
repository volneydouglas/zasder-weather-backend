"""The daily digest as a WEATHER REPORT (1.9, Volney: "like one of the
infographics... like a weather report you see on the nightly news").

Pure builders — no I/O in this module. alerts._maybe_send_digest gathers
yesterday's rollup row per station (+ the alert log + a best-effort
Open-Meteo peek at today) and hands everything here; these functions
return the HTML body (share-card aesthetic, inline styles only, no
external resources — email clients block them) and the plain-text
alternative for clients that refuse HTML.

Format rules: imperial units (the storm-summary email convention),
every interpolated string html-escaped (station names are user/device
input), and no em-dashes anywhere (the house copy rule).
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StationDay:
    """Yesterday's numbers for one station, straight off its rollup row.
    Optionals are ABSENT sensors, never zeros."""
    name: str
    tmax_f: float | None
    tmin_f: float | None
    # Doren, 2026-09-03, after a 108°F feels-like day: "that variable
    # should also be included in the summary for the prior day." Shown
    # only when it actually ran hotter (or colder) than the air, which is
    # the only time it says anything the high and low did not.
    feels_max_f: float | None
    rain_in: float | None
    gust_mph: float | None
    humidity_lo: float | None
    humidity_hi: float | None
    uv_max: float | None


@dataclass(frozen=True)
class Outlook:
    """Today per the forecast (best-effort; the report stands without it)."""
    hi_f: float | None
    lo_f: float | None
    precip_pct: int | None


@dataclass(frozen=True)
class AlertLine:
    when: str            # "Wed 14:02", already localized by the caller
    title: str
    severity: str        # info | watch | major | warning


@dataclass(frozen=True)
class Report:
    date_label: str      # "Thursday, August 28"
    stations: list[StationDay] = field(default_factory=list)
    alerts: list[AlertLine] = field(default_factory=list)
    outlook: Outlook | None = None


# ── the disagreement (2.1) ──────────────────────────────────────────────
#
# Volney's ask was a combined, all-station report. The honest first card
# is not an average: on one day his five stations spread 6.5°F on the
# high, and siting bias is systematic, so a mean is a number no sensor
# read. What the stations DISAGREE by is real information about the yard.
# Median for temperatures and humidity, MAX for gust and rain (the
# highest gauge is closest to the truth; both under-catch), quoted as
# `consensus`, but the headline is the spread.

# The fields the spread covers, in the order the card lists them:
# (label, StationDay attribute, unit, consensus rule). Pressure is not in
# the morning rollup row the report reads, so it is not here.
SPREAD_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("High", "tmax_f", "°F", "median"),
    ("Low", "tmin_f", "°F", "median"),
    ("Humidity", "humidity_hi", "%", "median"),
    ("Peak gust", "gust_mph", "mph", "max"),
    ("Rain", "rain_in", "in", "max"),
)

_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
          7: "seven", 8: "eight", 9: "nine"}


def _median(vals: list[float]) -> float:
    xs = sorted(vals)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def compute_spread(stations: list[StationDay]) -> dict | None:
    """How far the stations disagreed on yesterday, or None with fewer
    than two stations reporting a field. Pure; the report payload and the
    email both read this one answer."""
    if len(stations) < 2:
        return None
    fields: list[dict] = []
    for label, attr, unit, rule in SPREAD_FIELDS:
        pairs = []
        for s in stations:
            v = getattr(s, attr, None)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                pairs.append((s.name, float(v)))
        if len(pairs) < 2:
            continue
        lo = min(pairs, key=lambda p: p[1])
        hi = max(pairs, key=lambda p: p[1])
        vals = [v for _, v in pairs]
        consensus = max(vals) if rule == "max" else _median(vals)
        fields.append({
            "key": attr, "label": label, "unit": unit,
            "min": lo[1], "min_station": lo[0],
            "max": hi[1], "max_station": hi[0],
            "spread": round(hi[1] - lo[1], 2),
            "consensus": round(consensus, 2), "n": len(pairs),
        })
    if not fields:
        return None
    lead = fields[0]
    word = _WORDS.get(lead["n"], str(lead["n"]))
    headline = (f"Your {word} sensors spread "
                f"{_spread_value(lead, lead['spread'], delta=True)} "
                f"on yesterday's {lead['label'].lower()}")
    return {"station_count": len(stations), "headline": headline,
            "fields": fields}


def _spread_value(f: dict, v: float, delta: bool = False) -> str:
    """A field value in the report's own (API-native) units. A DELTA of
    temperature carries the same degree sign; the app converts by scale
    only, this text is the email's."""
    key = f["key"]
    if key == "rain_in":
        return f"{v:.2f} in"
    if key == "gust_mph":
        return f"{v:.0f} mph"
    if key == "humidity_hi":
        return f"{v:.0f}%"
    return f"{v:.1f}°F" if delta else f"{v:.0f}°F"


def spread_lines(sp: dict) -> list[str]:
    """One line per field: 'High: 98°F at Roof to 105°F at Yard, 6.5°F
    apart'. Shared by the plain-text mail and the HTML block."""
    return [
        f"{f['label']}: {_spread_value(f, f['min'])} at {f['min_station']} "
        f"to {_spread_value(f, f['max'])} at {f['max_station']}, "
        f"{_spread_value(f, f['spread'], delta=True)} apart"
        for f in sp["fields"]]


# ── the anchor's opening line ───────────────────────────────────────────

# A feels-like worth printing: at least this far from the air
# temperature. Below it the number just repeats the high back at you.
FEELS_GAP_F = 3.0


def _feels_worth_saying(s: StationDay) -> bool:
    return (s.feels_max_f is not None and s.tmax_f is not None
            and abs(s.feels_max_f - s.tmax_f) >= FEELS_GAP_F)


def headline(r: Report) -> str:
    """One nightly-news sentence for the lead station's day. Template
    picked by what actually happened; quiet days get a quiet line."""
    lead = r.stations[0] if r.stations else None
    if lead is None:
        return "Your stations' day, in one place."
    bits: list[str] = []
    if lead.tmax_f is not None:
        deg = f"{lead.tmax_f:.0f}"
        if lead.tmax_f >= 110:
            bits.append(f"A scorcher: {deg} at the high")
        elif lead.tmax_f >= 100:
            bits.append(f"Another triple-digit day, topping out at {deg}")
        elif lead.tmax_f <= 32:
            bits.append(f"A freezing day, {deg} at best")
        else:
            bits.append(f"A high of {deg}")
    if _feels_worth_saying(lead):
        bits.append(f"feeling like {lead.feels_max_f:.0f}")
    if lead.rain_in is not None and lead.rain_in >= 0.01:
        bits.append(f'{lead.rain_in:.2f}" of rain in the gauge')
    if lead.gust_mph is not None and lead.gust_mph >= 30:
        bits.append(f"gusts to {lead.gust_mph:.0f} mph")
    if not bits:
        return f"A quiet day at {lead.name}."
    sentence = ", ".join(bits)
    n = len(r.alerts)
    tail = "" if n == 0 else (" One alert overnight."
                              if n == 1 else f" {n} alerts along the way.")
    return f"{sentence} at {lead.name}.{tail}"


# ── shared palette (the share cards' vocabulary, email-safe) ────────────

_BG = "#0b0d12"
_CARD = "#151922"
_EDGE = "#262c38"
_ACCENT = "#4fa6f2"
_TEXT = "#e8ecf2"
_DIM = "#8a93a3"
_WARM = "#ff9a4d"
_SEV = {"warning": "#ff5c47", "major": "#ff9a4d",
        "watch": "#4fa6f2", "info": "#8a93a3"}

_LABEL = ("font:800 9px -apple-system,'Segoe UI',Arial,sans-serif;"
          "letter-spacing:1.2px;color:" + _DIM + ";")
_TILE = ("background:" + _CARD + ";border:1px solid " + _EDGE + ";"
         "border-radius:10px;padding:10px 12px;")


def _fmt(v: float | None, spec: str, unit: str) -> str:
    return "" if v is None else format(v, spec) + unit


def _tile(label: str, value: str, tint: str = _TEXT) -> str:
    return (f'<td style="{_TILE}width:25%;">'
            f'<div style="{_LABEL}">{label}</div>'
            f'<div style="font:800 20px -apple-system,\'Segoe UI\',Arial,'
            f'sans-serif;color:{tint};padding-top:2px;">{value}</div></td>')


def _station_block(s: StationDay) -> str:
    # upper() BEFORE escape (R14): named entities are case-sensitive, so
    # escape-then-upper turned "Bed & Breakfast" into literal "&AMP;"
    # garbage in the header. Display corruption only — brackets were
    # still escaped — but garbage nonetheless.
    name = html.escape(s.name.upper())
    hi = _fmt(s.tmax_f, ".0f", "&deg;")
    lo = _fmt(s.tmin_f, ".0f", "&deg;")
    tiles: list[str] = []
    if _feels_worth_saying(s):
        tiles.append(_tile("FELT LIKE", f"{s.feels_max_f:.0f}&deg;",
                           _WARM if s.feels_max_f > s.tmax_f else _ACCENT))
    if s.rain_in is not None:
        tiles.append(_tile("RAIN", f"{s.rain_in:.2f}&quot;",
                           _ACCENT if s.rain_in >= 0.01 else _TEXT))
    if s.gust_mph is not None:
        tiles.append(_tile("PEAK GUST", f"{s.gust_mph:.0f} mph",
                           _WARM if s.gust_mph >= 30 else _TEXT))
    if s.humidity_lo is not None and s.humidity_hi is not None:
        tiles.append(_tile("HUMIDITY",
                           f"{s.humidity_lo:.0f}&#8211;{s.humidity_hi:.0f}%"))
    if s.uv_max is not None and s.uv_max > 0:
        tiles.append(_tile("PEAK UV", f"{s.uv_max:.0f}"))
    spacer = '<td style="width:6px;"></td>'
    tile_row = spacer.join(tiles)
    hero = ""
    if hi or lo:
        hero = (
            '<table role="presentation" width="100%" cellpadding="0" '
            'cellspacing="0"><tr>'
            f'<td style="font:200 44px -apple-system,\'Segoe UI\',Arial,'
            f'sans-serif;color:{_WARM};">{hi}'
            f'<span style="font:800 10px -apple-system,Arial,sans-serif;'
            f'color:{_DIM};letter-spacing:1px;"> HIGH</span></td>'
            f'<td align="right" style="font:200 44px -apple-system,'
            f'\'Segoe UI\',Arial,sans-serif;color:{_ACCENT};">{lo}'
            f'<span style="font:800 10px -apple-system,Arial,sans-serif;'
            f'color:{_DIM};letter-spacing:1px;"> LOW</span></td>'
            '</tr></table>')
    return (
        f'<div style="{_LABEL}padding:14px 0 6px;">&#9679; {name}</div>'
        + hero
        + ('<table role="presentation" width="100%" cellpadding="0" '
           f'cellspacing="0" style="margin-top:8px;"><tr>{tile_row}</tr>'
           '</table>' if tiles else ''))


def _outlook_block(o: Outlook) -> str:
    parts = []
    if o.hi_f is not None:
        parts.append(f'high near <b style="color:{_WARM};">{o.hi_f:.0f}&deg;</b>')
    if o.lo_f is not None:
        parts.append(f'low around <b style="color:{_ACCENT};">{o.lo_f:.0f}&deg;'
                     '</b>')
    if o.precip_pct is not None:
        parts.append(f'a <b>{o.precip_pct}%</b> chance of rain')
    if not parts:
        return ""
    return (f'<div style="{_LABEL}padding:18px 0 6px;">TODAY&#8217;S '
            'OUTLOOK</div>'
            f'<div style="{_TILE}font:400 14px -apple-system,\'Segoe UI\','
            f'Arial,sans-serif;color:{_TEXT};">Today looks like a '
            + ", ".join(parts) + ".</div>")


def _alerts_block(alerts: list[AlertLine]) -> str:
    if not alerts:
        return (f'<div style="{_LABEL}padding:18px 0 6px;">ALERT LOG</div>'
                f'<div style="font:400 12px -apple-system,Arial,sans-serif;'
                f'color:{_DIM};">Nothing fired. A quiet day is a good '
                'report too.</div>')
    rows = []
    for a in alerts:
        dot = _SEV.get(a.severity, _DIM)
        rows.append(
            '<tr>'
            f'<td style="padding:4px 8px 4px 0;white-space:nowrap;'
            f'font:600 11px ui-monospace,Menlo,monospace;color:{_DIM};">'
            f'{html.escape(a.when)}</td>'
            f'<td style="padding:4px 0;font:400 13px -apple-system,'
            f'\'Segoe UI\',Arial,sans-serif;color:{_TEXT};">'
            f'<span style="color:{dot};">&#9679;</span> '
            f'{html.escape(a.title)}</td></tr>')
    return (f'<div style="{_LABEL}padding:18px 0 6px;">ALERT LOG</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0">'
            + "".join(rows) + '</table>')


def _spread_block(sp: dict | None) -> str:
    if not sp:
        return ""
    rows = "".join(
        f'<div style="font:400 12px -apple-system,Arial,sans-serif;'
        f'color:{_TEXT};padding:3px 0;">{html.escape(line)}</div>'
        for line in spread_lines(sp))
    return (f'<div style="{_TILE}margin-top:12px;">'
            f'<div style="{_LABEL}">YOUR SENSORS DISAGREE</div>'
            f'<div style="font:700 14px -apple-system,Arial,sans-serif;'
            f'color:{_TEXT};padding:4px 0 6px;">'
            f'{html.escape(sp["headline"])}</div>{rows}'
            f'<div style="font:400 10px -apple-system,Arial,sans-serif;'
            f'color:{_DIM};padding-top:6px;">Siting, not error. No average '
            f'is printed because no sensor read one.</div></div>')


def build_html(r: Report) -> str:
    """The whole email body. Single dark column, 480px, every style
    inline, zero external requests."""
    stations = "".join(_station_block(s) for s in r.stations)
    stations += _spread_block(compute_spread(r.stations))
    outlook = _outlook_block(r.outlook) if r.outlook else ""
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:{_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:{_BG};"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="480" cellpadding="0" cellspacing="0"
       style="max-width:480px;width:100%;">
<tr><td>
  <div style="font:300 13px -apple-system,'Segoe UI',Arial,sans-serif;color:{_DIM};">
    <i>zasder</i><b style="color:{_TEXT};letter-spacing:1px;">WEATHER</b>
    &nbsp;&#183;&nbsp; {html.escape(r.date_label)}
  </div>
  <div style="font:800 22px -apple-system,'Segoe UI',Arial,sans-serif;
              color:{_TEXT};padding:10px 0 2px;">{html.escape(headline(r))}</div>
  {stations}
  {outlook}
  {_alerts_block(r.alerts)}
  <div style="border-top:1px solid {_EDGE};margin-top:20px;padding-top:10px;
              font:400 11px -apple-system,Arial,sans-serif;color:{_DIM};">
    Measured in your backyard, reported every morning.
    Full history lives in the app&#8217;s Alerts tab.
  </div>
</td></tr></table></td></tr></table>
</body></html>"""


def build_text(r: Report) -> str:
    """Plain alternative, upgraded from the old bare alert list so
    text-only clients still get the report."""
    out = [f"Zasder Weather report for {r.date_label}", "", headline(r), ""]
    for s in r.stations:
        line = [s.name + ":"]
        if s.tmax_f is not None:
            line.append(f"high {s.tmax_f:.0f}F")
        if s.tmin_f is not None:
            line.append(f"low {s.tmin_f:.0f}F")
        if _feels_worth_saying(s):
            line.append(f"felt like {s.feels_max_f:.0f}F")
        if s.rain_in is not None:
            line.append(f'rain {s.rain_in:.2f}"')
        if s.gust_mph is not None:
            line.append(f"gust {s.gust_mph:.0f} mph")
        # Parity with the HTML tiles (R14): text-only clients get the
        # same facts, not a subset.
        if s.humidity_lo is not None and s.humidity_hi is not None:
            line.append(f"humidity {s.humidity_lo:.0f}-{s.humidity_hi:.0f}%")
        if s.uv_max is not None and s.uv_max > 0:
            line.append(f"UV {s.uv_max:.0f}")
        out.append("  " + " | ".join(line))
    if (sp := compute_spread(r.stations)):
        out += ["", sp["headline"] + "."]
        out += ["  " + line for line in spread_lines(sp)]
    if r.outlook and (r.outlook.hi_f is not None
                      or r.outlook.lo_f is not None
                      or r.outlook.precip_pct is not None):
        o = r.outlook
        bits = []
        if o.hi_f is not None:
            bits.append(f"high near {o.hi_f:.0f}F")
        if o.lo_f is not None:
            bits.append(f"low around {o.lo_f:.0f}F")
        if o.precip_pct is not None:
            bits.append(f"{o.precip_pct}% chance of rain")
        out += ["", "Today: " + ", ".join(bits)]
    out.append("")
    if r.alerts:
        out.append("Alert log:")
        out += [f"  {a.when}  {a.title}" for a in r.alerts]
    else:
        out.append("Alert log: nothing fired.")
    out += ["", "Full history lives in the app's Alerts tab.", ""]
    return "\n".join(out)


def push_text(r: Report) -> tuple[str, str]:
    """(title, body) for the compact morning push — the report's numbers
    in two short lines, sized for a lock-screen banner."""
    lead = r.stations[0] if r.stations else None
    title = ("Morning report · " + lead.name) if lead else "Morning report"
    lines: list[str] = []
    if lead is not None:
        bits: list[str] = []
        if lead.tmax_f is not None:
            bits.append(f"Hi {lead.tmax_f:.0f}°")
        if lead.tmin_f is not None:
            bits.append(f"Lo {lead.tmin_f:.0f}°")
        if _feels_worth_saying(lead):
            bits.append(f"felt {lead.feels_max_f:.0f}°")
        if lead.rain_in is not None and lead.rain_in >= 0.01:
            bits.append(f'{lead.rain_in:.2f}" rain')
        if lead.gust_mph is not None:
            bits.append(f"gust {lead.gust_mph:.0f} mph")
        if bits:
            lines.append("Yesterday: " + " · ".join(bits))
    if r.outlook is not None:
        bits = []
        if r.outlook.hi_f is not None:
            bits.append(f"near {r.outlook.hi_f:.0f}°")
        if r.outlook.precip_pct is not None:
            bits.append(f"{r.outlook.precip_pct}% rain chance")
        if bits:
            lines.append("Today: " + " · ".join(bits))
    n = len(r.alerts)
    if n:
        lines.append(f"{n} alert{'s' if n != 1 else ''} in the log.")
    if not lines:
        lines.append("Your stations' day, in one place.")
    return title, "\n".join(lines)
