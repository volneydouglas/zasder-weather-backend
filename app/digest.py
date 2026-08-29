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


# ── the anchor's opening line ───────────────────────────────────────────

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


def build_html(r: Report) -> str:
    """The whole email body. Single dark column, 480px, every style
    inline, zero external requests."""
    stations = "".join(_station_block(s) for s in r.stations)
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
