"""The public site's deeper pages (2.6, Volney 10-10: "make the public page
have more of the great data ... with links to different pages with more
data for non iOS users").

The front page (`/`, `/embed`) stays exactly as dense as it is. These pages
hang off it, each one a view the apps already have:

- History: each field over 7 days, 30 days or a year (daily highs and lows
  and daily rain for the year view, from the Insights rollups).
- Records and climate: today, this month, this year and all-time highs and
  lows, and the year's month-by-month climate table with today's normals.
- Storms: every recorded storm with its totals, and the latest one at a
  fixed address.
- Reports: the NOAA-format monthly and yearly climatological summaries at
  fixed "latest" addresses and per period, and the year's daily summary
  as CSV.

Everything here is the station's OWN data. The owner's forecast sources
(the morning report, the outlook, the sky note) are never published: those
are a provider's forecast, licensed to the owner, not open data.

Rendering only; app/main.py owns the routes, the opt-in check, which
stations are public, and the cache. Units stay API-native (°F, mph, inHg,
inches), the same as the front page.
"""
from __future__ import annotations

import csv
import html as _html
import io
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import public_dashboard as pd

# (key, path, label). The front page is "Now".
NAV = (
    ("now", "/", "Now"),
    ("history", "/weather/history", "History"),
    ("records", "/weather/records", "Records & climate"),
    ("storms", "/weather/storms", "Storms"),
    ("reports", "/weather/reports", "Reports"),
)
RANGES = {"7d": (7, "7 days"), "30d": (30, "30 days"), "1y": (365, "1 year")}
AXES = {"7d": ("7 days ago", "3.5 days", "now"),
        "30d": ("30 days ago", "15 days", "now"),
        "1y": ("1 year ago", "6 months", "today")}
HISTORY_FIELDS = ("tempf", "humidity", "windspeedmph", "baromrelin")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _esc(v: Any) -> str:
    return _html.escape(str(v), quote=True)


def _tz(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:                            # noqa: BLE001
        return timezone.utc


def _date(ms: Any, tz_name: str, with_time: bool = False) -> str:
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, tz=_tz(tz_name))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    s = f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    if with_time:
        s += " " + dt.strftime("%-I:%M %p").lower()
    return s


def _q(sid: str | None, **extra: str) -> str:
    """The query string that keeps the chosen station across pages."""
    parts = [f"station={_esc(sid)}"] if sid else []
    parts += [f"{k}={_esc(v)}" for k, v in extra.items()]
    return ("?" + "&amp;".join(parts)) if parts else ""


# ── chrome ───────────────────────────────────────────────────────────────

MORE_LINK_CLASS = 'class="pp-more-a"'
NEW_TAB = ' target="_blank" rel="noopener"'


def more_links(sid: str | None = None) -> str:
    """The row the front page carries under the stations: where the rest of
    the data is. The front page and the embed share one cached fragment;
    the embed (an iframe on someone else's site) adds NEW_TAB to each link
    at serve time (`for_embed`) so they open a tab instead of navigating
    inside the frame."""
    links = "".join(f'<a {MORE_LINK_CLASS} href="{path}{_q(sid)}">{_esc(label)}</a>'
                    for key, path, label in NAV if key != "now")
    return f'<nav class="pp-more" aria-label="More weather">{links}</nav>'


def for_embed(fragment: str) -> str:
    return fragment.replace(MORE_LINK_CLASS, MORE_LINK_CLASS + NEW_TAB)


def nav(active: str, sid: str | None) -> str:
    items = []
    for key, path, label in NAV:
        cur = ' aria-current="page" class="on"' if key == active else ""
        href = path + (_q(sid) if key != "now" else "")
        items.append(f'<a href="{href}"{cur}>{_esc(label)}</a>')
    return f'<nav class="pp-nav" aria-label="Weather pages">{"".join(items)}</nav>'


def station_picker(stations: list[tuple[str, str]], sid: str, path: str,
                   **extra: str) -> str:
    """Chips for each public station, when there is more than one."""
    if len(stations) < 2:
        return ""
    chips = "".join(
        f'<a class="pp-chip{" on" if s == sid else ""}" href="{path}{_q(s, **extra)}">'
        f'{_esc(name)}</a>' for s, name in stations)
    return f'<div class="pp-chips" aria-label="Station">{chips}</div>'


def page(title: str, active: str, sid: str | None, body: str,
         station_name: str = "") -> str:
    head = _esc(station_name or "Zasder Weather")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)} · {head}</title>
  <style>
{pd.THEME_CSS}
    body {{ font-family: system-ui, -apple-system, sans-serif;
            margin: 0; padding: 16px; line-height: 1.4; }}
    .wrap {{ max-width: 960px; margin: 0 auto; }}
{pd.DASHBOARD_CSS}
{CSS}
  </style>
</head>
<body><div class="wrap">
<header class="pp-head"><div class="pp-station">{head}</div>{nav(active, sid)}</header>
<main>
<h1 class="pp-title">{_esc(title)}</h1>
{body}
</main>
<footer class="pp-foot">Live from this station's own server ·
<a href="https://zasder.com/weather" target="_blank" rel="noopener">Zasder Weather</a></footer>
</div></body>
</html>"""


def unavailable(text: str) -> str:
    return f'<div class="chart-empty pp-empty">{_esc(text)}</div>'


# ── history ──────────────────────────────────────────────────────────────

def svg_bars(points: list[tuple[int, float]], color: str, unit: str,
             time_labels: tuple[str, str, str], width: int = 640,
             height: int = 110, domain: tuple[int, int] | None = None,
             slots: int | None = None) -> str:
    """Daily bars (rain) on a fixed calendar `domain` of `slots` days, so a
    station with a few days of data shows them where they fell. Zero days
    draw nothing; an all-dry window says so; days with no rain data are
    simply absent, never drawn as zero."""
    pts = [(t, v) for t, v in points if v is not None
           and (domain is None or domain[0] <= t <= domain[1])]
    if not pts:
        return '<div class="chart-empty">no rain data in this range</div>'
    hi = max(v for _, v in pts)
    if hi <= 0:
        return '<div class="chart-empty">no rain in this range</div>'
    t0, t1 = domain if domain is not None else (min(t for t, _ in pts), max(t for t, _ in pts))
    span = (t1 - t0) or 1
    n = slots or max(len(pts), 1)
    bw = max(1.0, width / n * 0.8)
    bars = []
    for t, v in pts:
        if v <= 0:
            continue
        h = max(1.0, (height - 6) * v / hi)
        x = (width - bw) * (t - t0) / span
        bars.append(f'<rect x="{x:.1f}" y="{height - h:.1f}" width="{bw:.1f}" '
                    f'height="{h:.1f}" fill="{color}" rx="1"/>')
    a, b, c = time_labels
    total = sum(v for _, v in pts)
    return (f'<div class="chart-plot"><svg viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" class="chart-svg" role="img" '
            f'aria-label="Daily rain">{"".join(bars)}</svg>'
            f'<span class="chart-yl" style="top:4%">{hi:.2f}{_esc(unit)}</span></div>'
            f'<div class="chart-legend"><span class="lg">Total {total:.2f} {_esc(unit)} · '
            f'wettest day {hi:.2f} {_esc(unit)}</span></div>'
            f'<div class="chart-axis"><span>{_esc(a)}</span><span>{_esc(b)}</span>'
            f'<span>{_esc(c)}</span></div>')


def _card(title: str, unit: str, body: str, sub: str) -> str:
    return (f'<div class="chart"><div class="chart-title">{_esc(title)} '
            f'<span class="chart-unit">· {_esc(sub)} · {_esc(unit)}</span></div>{body}</div>')


# Readings further apart than this break the line (A26-02): the history
# buckets are 15 minutes (7 d) and 1 hour (30 d); daily values are a day.
GAP_MS = {"7d": 2 * 3_600_000, "30d": 6 * 3_600_000}
DAY_GAP_MS = int(2.5 * 86_400_000)


def render_history(range_key: str, series: dict[str, list[tuple[int, float]]],
                   days: list[dict[str, Any]] | None, insights: bool,
                   sid: str | None = None,
                   domain: tuple[int, int] | None = None,
                   day_domain: tuple[int, int] | None = None) -> str:
    """`series`: field -> (ms, value) points for the 7 d / 30 d views (the
    server's bucketed history), drawn on `domain`, the window the labels
    name. `days`: the rollup day rows (ms at local noon, tmin, tmax, rain,
    gust) for the daily cards and the 1 y view, drawn on `day_domain`."""
    days_n, label = RANGES[range_key]
    axis = AXES[range_key]
    tabs = "".join(
        f'<a class="pp-chip{" on" if k == range_key else ""}" data-range="{k}" '
        f'href="/weather/history{_q(sid, range=k)}">{_esc(v[1])}</a>'
        for k, v in RANGES.items())
    cards = []
    if range_key == "1y":
        if not insights or not days:
            return (f'<div class="pp-chips pp-ranges">{tabs}</div>'
                    + unavailable("This server is not keeping daily summaries, "
                                  "so there is no year view yet."))
    else:
        for key in HISTORY_FIELDS:
            meta = pd.FIELD_META[key]
            body = pd.svg_chart(series.get(key, []), meta["color"], unit=meta["unit"],
                                time_labels=axis, domain=domain,
                                gap_ms=GAP_MS.get(range_key),
                                empty=f"no data in the last {label}")
            cards.append(_card(meta["label"], meta["unit"], body, f"last {label}"))
    if days:
        hi = [(d["ms"], d["tmax"]) for d in days]
        lo = [(d["ms"], d["tmin"]) for d in days]
        temp = pd.svg_multi_chart([("High", "#ff9e33", hi), ("Low", "#4cb2ff", lo)],
                                  unit="°F", time_labels=axis, domain=day_domain,
                                  gap_ms=DAY_GAP_MS)
        if temp:
            cards.insert(0, _card("Daily high and low", "°F", temp, f"last {label}"))
        rain = svg_bars([(d["ms"], d["rain"]) for d in days], "#5aa0ff", "in", axis,
                        domain=day_domain, slots=days_n)
        cards.append(_card("Daily rain", "in", rain, f"last {label}"))
        gust = pd.svg_chart([(d["ms"], d["gust"]) for d in days], "#39c9d6",
                            unit="mph", time_labels=axis, domain=day_domain,
                            gap_ms=DAY_GAP_MS,
                            empty=f"no gust data in the last {label}")
        cards.append(_card("Peak gust each day", "mph", gust, f"last {label}"))
    return (f'<div class="pp-chips pp-ranges">{tabs}</div>'
            f'<div class="charts pp-charts">{"".join(cards)}</div>')


# ── records and climate ──────────────────────────────────────────────────

RECORD_ROWS = (
    ("tempf", "Temperature", "°F"),
    ("feelsLike", "Feels like", "°F"),
    ("dewPoint", "Dew point", "°F"),
    ("humidity", "Humidity", "%"),
    ("windgustmph", "Wind gust", "mph"),
    ("dailyrainin", "Rain in a day", "in"),
    ("baromrelin", "Pressure", "inHg"),
    ("solarradiation", "Solar", "W/m²"),
    ("uv", "UV index", ""),
)
PERIODS = (("today", "Today"), ("month", "This month"), ("year", "This year"),
           ("all", "All time"))
# Fields whose low is not a record anyone reads.
_NO_LOW = {"windgustmph", "dailyrainin", "solarradiation", "uv"}


def _rec_cell(rec: dict[str, Any] | None, which: str, unit: str, tz_name: str) -> str:
    if not rec or rec.get(which) is None:
        return "<td>—</td>"
    v = pd._num(rec.get(which))
    if v is None:
        return "<td>—</td>"
    at = rec.get("maxAt" if which == "max" else "minAt")
    return (f'<td><b>{_esc(pd._fmt(v, unit))}</b>'
            f'<small>{_esc(_date(at, tz_name))}</small></td>')


def render_records(records: dict[str, Any] | None, tz_name: str) -> str:
    periods = (records or {}).get("periods") or {}
    if not periods:
        return unavailable("No records yet.")
    head = "".join(f"<th>{_esc(lbl)}</th>" for _, lbl in PERIODS)
    rows = []
    for field, label, unit in RECORD_ROWS:
        recs = [((periods.get(p) or {}).get("fields") or {}).get(field) for p, _ in PERIODS]
        if not any(recs):
            continue
        rows.append(f'<tr><th scope="row">{_esc(label)} high</th>'
                    + "".join(_rec_cell(r, "max", unit, tz_name) for r in recs) + "</tr>")
        if field not in _NO_LOW:
            rows.append(f'<tr><th scope="row">{_esc(label)} low</th>'
                        + "".join(_rec_cell(r, "min", unit, tz_name) for r in recs) + "</tr>")
    if not rows:
        return unavailable("No records yet.")
    return (f'<div class="pp-table-wrap"><table class="pp-table pp-records">'
            f'<thead><tr><th></th>{head}</tr></thead><tbody>{"".join(rows)}</tbody>'
            f'</table></div>')


def _n(v: Any, digits: int = 1, unit: str = "") -> str:
    f = pd._num(v)
    return "—" if f is None else f"{f:.{digits}f}{unit}"


def _month_days(year: int, month: int) -> int:
    import calendar
    return calendar.monthrange(year, month)[1]


def render_climate(summary: dict[str, Any] | None, year: int,
                   years: list[int], sid: str | None, today=None) -> str:
    """The year's month-by-month table (the climate summary the apps show),
    with each month's coverage: how many days it has summaries for, and
    whether it is still in progress (A26-06). A month of one day must not
    read like a month of thirty-one."""
    if not summary or not any(m.get("days") for m in summary.get("months") or []):
        return unavailable(f"No daily summaries for {year}.")
    pick = "".join(
        f'<a class="pp-chip{" on" if y == year else ""}" href="/weather/records{_q(sid, year=str(y))}">{y}</a>'
        for y in years)
    rows = []
    covered = possible = 0
    for m in summary["months"]:
        if not m.get("days"):
            continue
        n = int(m["days"])
        full = _month_days(year, m["month"])
        current = bool(today and today.year == year and today.month == m["month"])
        if current:
            full = today.day
        covered, possible = covered + n, possible + full
        if current:
            note = f"{n} of {full} days so far" if n < full else "in progress"
        elif n < full:
            note = f"{n} of {full} days"
        else:
            note = ""
        cls = ' class="pp-partial"' if note else ""
        rows.append(
            f'<tr{cls}><th scope="row">{MONTHS[m["month"] - 1]}'
            + (f'<small>{_esc(note)}</small>' if note else "") + '</th>'
            f'<td>{_n(m.get("mean"))}</td><td>{_n(m.get("max"))}</td>'
            f'<td>{_n(m.get("min"))}</td><td>{_n(m.get("rain"), 2)}</td>'
            f'<td>{_n(m.get("hdd"), 0)}</td><td>{_n(m.get("cdd"), 0)}</td></tr>')
    t = summary.get("totals") or {}
    highs = [pd._num(m.get("max")) for m in summary["months"]]
    lows = [pd._num(m.get("min")) for m in summary["months"]]
    y_hi = max((v for v in highs if v is not None), default=None)
    y_lo = min((v for v in lows if v is not None), default=None)
    so_far = bool(today and today.year == year)
    y_note = (f"{covered} days so far" if so_far
              else (f"{covered} of {possible} days" if covered < possible else ""))
    rows.append('<tr class="pp-total"><th scope="row">Year'
                + (f'<small>{_esc(y_note)}</small>' if y_note else "") + '</th>'
                f'<td>{_n(t.get("mean"))}</td>'
                f'<td>{_n(y_hi)}</td><td>{_n(y_lo)}</td>'
                f'<td>{_n(t.get("rain"), 2)}</td><td>{_n(t.get("hdd"), 0)}</td>'
                f'<td>{_n(t.get("cdd"), 0)}</td></tr>')
    return (f'<div class="pp-chips">{pick}</div>'
            '<div class="pp-table-wrap"><table class="pp-table pp-climate"><thead><tr>'
            '<th></th><th>Mean °F</th><th>High °F</th><th>Low °F</th><th>Rain in</th>'
            '<th>Heating degree days</th><th>Cooling degree days</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="pp-note">Means are (high + low) / 2, the NOAA convention; '
            'degree days are base 65 °F. A month marked with a day count is '
            'missing days, and its totals cover only those days.</p>')


def render_normals(normal: dict[str, Any] | None, obs: dict[str, Any] | None,
                   today_rec: dict[str, Any] | None) -> str:
    """Today against the 1991-2020 NOAA normals, when the location has them."""
    if not normal:
        return ""
    hi_n, lo_n = pd._num(normal.get("normal_high")), pd._num(normal.get("normal_low"))
    if hi_n is None and lo_n is None:
        return ""
    t = ((today_rec or {}).get("fields") or {}).get("tempf") or {}
    hi, lo = pd._num(t.get("max")), pd._num(t.get("min"))

    def diff(a, b):
        if a is None or b is None:
            return ""
        d = a - b
        return f' <span class="pp-diff">{d:+.0f}°</span>'
    # The NOAA station's name is a location clue the owner never chose to
    # publish (the addendum review's note), so the page names the source,
    # not the place.
    return (f'<div class="pp-normals"><b>Today against normal</b> '
            f'High {_n(hi, 0, "°")} vs {_n(hi_n, 0, "°")}{diff(hi, hi_n)} · '
            f'Low {_n(lo, 0, "°")} vs {_n(lo_n, 0, "°")}{diff(lo, lo_n)}'
            + '<small>NOAA 1991-2020 climate normals</small>'
            + "</div>")


# ── storms ───────────────────────────────────────────────────────────────

def _duration(start: Any, end: Any) -> str:
    try:
        mins = max(0, int((int(end) - int(start)) / 60_000))
    except (TypeError, ValueError):
        return "—"
    h, m = divmod(mins, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def render_storm_detail(s: dict[str, Any], tz_name: str) -> str:
    rng = ""
    lo, hi = pd._num(s.get("min_tempf")), pd._num(s.get("max_tempf"))
    if lo is not None and hi is not None:
        rng = f"{lo:.0f}° to {hi:.0f}°"
    tiles = [("Rain", _n(s.get("total_in"), 2, " in")),
             ("Peak rate", _n(s.get("peak_rate_in_hr"), 2, " in/hr")),
             ("Peak gust", _n(s.get("max_gust_mph"), 0, " mph")),
             ("Temperature", rng or "—"),
             ("Lasted", _duration(s.get("started_ms"), s.get("ended_ms")))]
    t = "".join(f'<div class="rec"><div class="rec-k">{_esc(k)}</div>'
                f'<div class="rec-v">{_esc(v)}</div></div>' for k, v in tiles)
    return (f'<div class="pp-storm"><div class="pp-storm-when">'
            f'{_esc(_date(s.get("started_ms"), tz_name, True))} to '
            f'{_esc(_date(s.get("ended_ms"), tz_name, True))}</div>'
            f'<div class="records pp-tiles">{t}</div></div>')


def render_storms(storms: list[dict[str, Any]], tz_name: str, sid: str | None) -> str:
    if not storms:
        return unavailable("No storms recorded yet. A storm is recorded when "
                           "rain starts and closes after a quiet spell.")
    latest = (f'<h2 class="pp-h2">Latest storm <a class="pp-perma" '
              f'href="/weather/storms/latest{_q(sid)}">fixed link</a></h2>'
              + render_storm_detail(storms[0], tz_name))
    rows = "".join(
        f'<tr><th scope="row">{_esc(_date(s.get("started_ms"), tz_name))}</th>'
        f'<td>{_n(s.get("total_in"), 2)}</td><td>{_n(s.get("peak_rate_in_hr"), 2)}</td>'
        f'<td>{_n(s.get("max_gust_mph"), 0)}</td>'
        f'<td>{_esc(_duration(s.get("started_ms"), s.get("ended_ms")))}</td></tr>'
        for s in storms)
    return (latest + '<h2 class="pp-h2">Recent storms</h2>'
            '<div class="pp-table-wrap"><table class="pp-table"><thead><tr><th>Began</th>'
            '<th>Rain in</th><th>Peak in/hr</th><th>Peak gust mph</th><th>Lasted</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>')


# ── reports ──────────────────────────────────────────────────────────────

def render_reports(sid: str | None, coverage: dict[str, int], has_storm: bool) -> str:
    """`coverage`: 'YYYY-MM' -> days with summaries. Only periods with
    summaries are offered, and "latest" names the newest month and year
    there actually are, not the calendar's (the addendum review's notes)."""
    q = _q(sid)
    fixed = []
    if coverage:
        newest = max(coverage)
        ny, nm = int(newest[:4]), int(newest[5:7])
        fixed += [
            (f"Latest month ({MONTHS[nm - 1]} {ny}), NOAA climatological summary",
             f"/weather/reports/noaa/latest-month.txt{q}"),
            (f"Latest year ({ny}), NOAA climatological summary",
             f"/weather/reports/noaa/latest-year.txt{q}"),
            (f"{ny} daily summary, CSV", f"/weather/data/{ny}.csv{q}"),
        ]
    if has_storm:
        fixed.append(("The latest storm", f"/weather/storms/latest{q}"))
    if not fixed:
        return unavailable("This server is not keeping daily summaries, so there "
                           "are no reports to publish yet.")
    fixed_html = "".join(f'<li><a href="{href}">{_esc(t)}</a>'
                         f'<code>{_esc(href.split("?")[0])}</code></li>' for t, href in fixed)
    out = (f'<h2 class="pp-h2">Always the latest</h2>'
           f'<p class="pp-note">These addresses never change, so they can be '
           f'bookmarked or linked from another site.</p>'
           f'<ul class="pp-list">{fixed_html}</ul>')
    if coverage:
        by_year: dict[int, list[tuple[int, int]]] = {}
        for ym, n in coverage.items():
            by_year.setdefault(int(ym[:4]), []).append((int(ym[5:7]), n))
        blocks = []
        for y in sorted(by_year, reverse=True):
            months = " ".join(
                f'<a href="/weather/reports/noaa/{y}-{m:02d}.txt{q}" '
                f'title="{n} days">{MONTHS[m - 1]}</a>'
                for m, n in sorted(by_year[y]))
            blocks.append(
                f'<div class="pp-year"><b>{y}</b> '
                f'<a href="/weather/reports/noaa/{y}.txt{q}">Year</a> · '
                f'<a href="/weather/data/{y}.csv{q}">CSV</a>'
                f'<div class="pp-months">{months}</div></div>')
        out += (f'<h2 class="pp-h2">Every period</h2>'
                f'<p class="pp-note">The NOAA climatological summary is the fixed-width '
                f'report weather observers have exchanged for decades: a row per day '
                f'(or per month for a year) with temperatures, degree days, rain and '
                f'wind.</p>{"".join(blocks)}')
    return out


def daily_csv(days: list[dict[str, Any]]) -> str:
    """One row per local day. Empty cells are empty, never zero."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "high_f", "low_f", "mean_f", "rain_in", "peak_gust_mph",
                "heating_degree_days", "cooling_degree_days"])

    def c(v, digits):
        f = pd._num(v)
        return "" if f is None else f"{f:.{digits}f}"
    for d in days:
        w.writerow([d["day"], c(d.get("tmax"), 1), c(d.get("tmin"), 1),
                    c(d.get("mean"), 1), c(d.get("rain"), 2), c(d.get("gust"), 1),
                    c(d.get("hdd"), 1), c(d.get("cdd"), 1)])
    return buf.getvalue()


CSS = """
    .pp-head { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between;
        gap:8px 16px; margin-bottom:12px; }
    .pp-station { font-size:11px; font-weight:800; letter-spacing:1.2px;
        text-transform:uppercase; color:var(--ink-50); }
    .pp-nav { display:flex; flex-wrap:wrap; gap:6px; }
    .pp-nav a { font-size:12px; font-weight:600; text-decoration:none;
        color:var(--ink-70); border:1px solid var(--card-edge); border-radius:999px;
        padding:5px 12px; background:var(--card-bg); }
    .pp-nav a.on { color:var(--page-bg); background:var(--ink-90, #e8eaf0);
        border-color:transparent; }
    .pp-title { font-size:22px; font-weight:700; margin:4px 0 12px; }
    .pp-h2 { font-size:15px; font-weight:700; margin:18px 0 8px; }
    .pp-chips { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 12px; }
    .pp-chip { font-size:12px; font-weight:600; text-decoration:none; color:var(--ink-70);
        border:1px solid var(--card-edge); border-radius:8px; padding:4px 10px; }
    .pp-chip.on { color:var(--ink-90, inherit); border-color:var(--ink-50); }
    .pp-charts { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
    .pp-table-wrap { overflow-x:auto; margin:0 0 8px; }
    .pp-table { border-collapse:collapse; width:100%; font-size:13px;
        font-variant-numeric:tabular-nums; }
    .pp-table th, .pp-table td { text-align:right; padding:6px 8px;
        border-bottom:1px solid var(--card-edge); white-space:nowrap; }
    .pp-table th:first-child { text-align:left; }
    .pp-table thead th { font-size:11px; color:var(--ink-55); font-weight:700; }
    .pp-table td b { display:block; font-weight:700; }
    .pp-table td small { display:block; font-size:10px; color:var(--ink-55); }
    .pp-total th, .pp-total td { font-weight:700; }
    .pp-table th small { display:block; font-size:10px; font-weight:600; color:#ffb020; }
    .pp-note { font-size:12px; color:var(--ink-55); margin:4px 0 10px; }
    .pp-normals { font-size:13px; margin:0 0 12px; padding:10px 12px;
        border:1px solid var(--card-edge); border-radius:10px; background:var(--card-bg); }
    .pp-normals small { display:block; font-size:10px; color:var(--ink-55); margin-top:2px; }
    .pp-diff { font-weight:700; color:var(--ink-70); }
    .pp-storm { margin-bottom:8px; }
    .pp-tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));
        gap:8px; }
    .pp-tiles .rec { margin:0; }
    .pp-storm-when { font-size:13px; color:var(--ink-70); margin-bottom:6px; }
    .pp-perma { font-size:11px; font-weight:600; margin-left:8px; }
    .pp-list { padding-left:18px; }
    .pp-list li { margin:6px 0; }
    .pp-list code { display:block; font-size:11px; color:var(--ink-55); }
    .pp-year { margin:10px 0; font-size:13px; }
    .pp-months { display:flex; flex-wrap:wrap; gap:4px 10px; margin-top:4px; font-size:12px; }
    .pp-empty { padding:24px 12px; }
    .pp-foot { margin-top:24px; font-size:11px; color:var(--ink-55); }
    .pp-foot a, .pp-list a, .pp-year a, .pp-months a, .pp-perma { color:var(--link, #4cb2ff); }
"""
