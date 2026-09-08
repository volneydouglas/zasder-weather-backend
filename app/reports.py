"""Stored reports (2.1) — the morning report and every storm summary kept
as a row you can open again, instead of a notification that scrolls away.

Volney, 2026-09-04: "when you click on the morning report it would jump
you into a more detailed report page with yesterday values but also
forecast for today", listed in a Reports pane alongside every shareable
you can run.

Pure builders and shapes only — no I/O. `alerts` builds a report at the
moment it sends one (so the page and the email can never disagree about
the numbers), hands the payload to `db.insert_report`, and the API serves
it back. The payload is the WIRE: the iOS/macOS decoders read these keys,
so `bin/tests/test_report_wire_parity.py` pins them against the Swift
structs the same way the story cards are pinned.

Rules that bite here:
- Absent is not zero ([[absent is not zero]]): every measurement is
  `float | None` and a missing sensor stays None all the way to the card.
- Units are stored API-native (°F, mph, inHg, inches); the apps convert.
- No em-dashes in any copy this module produces (the house rule).
"""
from __future__ import annotations

import math
from typing import Any

# Report kinds. The list endpoint filters on these; the apps switch on
# them. New kinds append — a kind the app does not know renders as a
# plain title + summary rather than breaking the list.
KIND_MORNING = "morning"
KIND_STORM = "storm"
# 2.1: the classic NOAA-style climatological summaries (climate.py has
# rendered them since 1.9) kept as reports you run per station and
# period, so the Reports pane is where every report lives.
KIND_NOAA_MONTH = "noaa_month"
KIND_NOAA_YEAR = "noaa_year"
# 2.2 (Doren): "a day version in the NOAA style" — hour rows for one day.
KIND_NOAA_DAY = "noaa_day"
KINDS = (KIND_MORNING, KIND_STORM, KIND_NOAA_MONTH, KIND_NOAA_YEAR, KIND_NOAA_DAY)
# The kinds `POST /api/reports/run` builds on demand.
RUNNABLE_KINDS = (KIND_NOAA_MONTH, KIND_NOAA_YEAR, KIND_NOAA_DAY)

# How many report rows a server keeps, by default. They are a few hundred
# bytes each (a NOAA month is a few KB); this is two years of mornings
# plus every storm in between. A SETTING since 2.1: app-managed through
# /api/reports/retention, REPORTS_MAX_ROWS in the env as the fallback,
# clamped between the floor and ceiling below so a typo can neither wipe
# the list nor grow the table forever.
MAX_ROWS = 900
MIN_ROWS = 30
MAX_ROWS_CEILING = 5000


def clamp_retention(n: Any) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return MAX_ROWS
    return max(MIN_ROWS, min(MAX_ROWS_CEILING, v))


def _num(v: Any) -> float | None:
    """A finite float, or None. Guards the wire against NaN/inf, which is
    not JSON and decodes to garbage on the phone (the story-card lesson)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


# ── morning report ──────────────────────────────────────────────────────

def morning_payload(report: Any) -> dict[str, Any]:
    """A `digest.Report` as the stored/served payload. Same object the
    email was rendered from, so the page cannot drift from the mail."""
    return {
        "date_label": report.date_label,
        "stations": [
            {"name": s.name,
             "tmax_f": _num(s.tmax_f), "tmin_f": _num(s.tmin_f),
             "feels_max_f": _num(getattr(s, "feels_max_f", None)),
             "rain_in": _num(s.rain_in), "gust_mph": _num(s.gust_mph),
             "humidity_lo": _num(s.humidity_lo),
             "humidity_hi": _num(s.humidity_hi),
             "uv_max": _num(s.uv_max)}
            for s in report.stations],
        "alerts": [{"when": a.when, "title": a.title, "severity": a.severity}
                   for a in report.alerts],
        "outlook": None if report.outlook is None else {
            "hi_f": _num(report.outlook.hi_f),
            "lo_f": _num(report.outlook.lo_f),
            "precip_pct": _int(report.outlook.precip_pct)},
        # 2.1: how far the stations disagreed (None with one station).
        "spread": _spread_wire(report),
    }


def _spread_wire(report: Any) -> dict[str, Any] | None:
    """The disagreement block, from the same computation the email
    renders. Its own dict literal so the wire guard can pin it."""
    from .digest import compute_spread
    try:
        sp = compute_spread(list(report.stations))
    except Exception:
        return None
    if not sp:
        return None
    return {
        "station_count": _int(sp["station_count"]),
        "headline": str(sp["headline"]),
        "fields": [_spread_field(f) for f in sp["fields"]],
    }


def _spread_field(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": str(f["key"]),
        "label": str(f["label"]),
        "unit": str(f.get("unit") or ""),
        "min": _num(f["min"]),
        "min_station": str(f["min_station"]),
        "max": _num(f["max"]),
        "max_station": str(f["max_station"]),
        "spread": _num(f["spread"]),
        "consensus": _num(f["consensus"]),
        "n": _int(f["n"]),
    }


def _units(units: Any):
    """The reader's units, or the API-native rendering. Lazy import: the
    story engine imports this module's kinds."""
    if units is None:
        from .stories import UNITS_NATIVE
        return UNITS_NATIVE
    return units


def summary_line(kind: str, payload: dict[str, Any], units: Any = None) -> str:
    """The stored summary re-rendered for a reader whose app is not on the
    API-native units (2.1 pre-release review BE-3): the list endpoint
    omits payloads, so the app could not convert "Yesterday 93/70 · 0.25 in
    rain" for a Celsius reader and showed it beside a 34/21 °C detail
    page. The default reproduces the stored text byte for byte."""
    if kind == KIND_MORNING:
        return morning_summary(payload, units)
    if kind == KIND_STORM:
        return storm_summary_line(payload, units)
    if kind in (KIND_NOAA_MONTH, KIND_NOAA_YEAR, KIND_NOAA_DAY):
        return noaa_summary_line(payload, units)
    return ""


def morning_summary(payload: dict[str, Any], units: Any = None) -> str:
    """The one line the Reports list shows under the title. Yesterday's
    lead station, then today's outlook when the forecast answered."""
    u = _units(units)
    bits: list[str] = []
    stations = payload.get("stations") or []
    if stations:
        lead = stations[0]
        hi, lo = lead.get("tmax_f"), lead.get("tmin_f")
        if hi is not None and lo is not None:
            bits.append(f"Yesterday {round(u.temp(hi))}/{round(u.temp(lo))}")
        rain = lead.get("rain_in")
        if rain is not None and rain >= 0.01:
            bits.append(f"{u.rain_amount(rain)} rain")
    out = payload.get("outlook") or {}
    if out.get("hi_f") is not None:
        today = f"today near {round(u.temp(out['hi_f']))}"
        if out.get("precip_pct"):
            today += f", {out['precip_pct']}% rain"
        bits.append(today)
    n = len(payload.get("alerts") or [])
    if n:
        bits.append(f"{n} alert{'s' if n != 1 else ''}")
    return " · ".join(bits) or "No numbers to report"


# ── storm report ────────────────────────────────────────────────────────

def storm_payload(station_name: str, summary: Any,
                  capture: dict[str, Any] | None = None) -> dict[str, Any]:
    """A `storm.StormSummary` (plus the close-capture columns when the
    storm carried them) as the stored payload."""
    cap = capture or {}
    return {
        "station": station_name,
        "started_ms": _int(getattr(summary, "started_ms", None)),
        "ended_ms": _int(getattr(summary, "ended_ms", None)),
        "total_in": _num(getattr(summary, "total_in", None)),
        "peak_rate_in_hr": _num(getattr(summary, "peak_rate_in_hr", None)),
        "max_gust_mph": _num(getattr(summary, "max_gust_mph", None)),
        "min_tempf": _num(getattr(summary, "min_tempf", None)),
        "max_tempf": _num(getattr(summary, "max_tempf", None)),
        "pre_tempf": _num(cap.get("pre_tempf")),
        "post_tempf": _num(cap.get("post_tempf")),
        "temp_drop_f": _num(cap.get("temp_drop_f")),
        "pressure_change_inhg": _num(cap.get("pressure_change_inhg")),
        "dew_change_f": _num(cap.get("dew_change_f")),
    }


def storm_summary_line(payload: dict[str, Any], units: Any = None) -> str:
    u = _units(units)
    bits: list[str] = []
    total = payload.get("total_in")
    if total is not None:
        bits.append(u.rain_amount(total))
    rate = payload.get("peak_rate_in_hr")
    if rate is not None:
        bits.append(f"peak {u.rain_text(rate)} {u.rate_token}")
    gust = payload.get("max_gust_mph")
    if gust is not None:
        bits.append(f"gust {round(u.wind_value(gust))} {u.wind_token}")
    drop = payload.get("temp_drop_f")
    if drop is not None and drop >= 1:
        # A difference, so the scale-only conversion (a 9°F drop is a 5°C
        # drop, not −12.8°C); rounding as before.
        bits.append(f"cooled {round(u.temp_delta(drop))}{u.temp_suffix}")
    return " · ".join(bits) or "A storm passed through"


def storm_duration_minutes(payload: dict[str, Any]) -> int | None:
    a, b = payload.get("started_ms"), payload.get("ended_ms")
    if a is None or b is None or b < a:
        return None
    return int((b - a) / 60_000)


# ── NOAA-style climate reports (2.1) ────────────────────────────────────

def noaa_payload(kind: str, mac: str, station: str, year: int,
                 month: int | None, text: str,
                 numbers: dict[str, Any], day: str | None = None) -> dict[str, Any]:
    """The rendered fixed-width table (the thing the page shows, verbatim,
    in a monospaced face) plus the headline numbers the list row, the
    share card and a future comparison read without parsing text. `day`
    (YYYY-MM-DD) only on the daily kind; a 2.1 app ignores the key."""
    return {
        "station": station,
        "mac": mac,
        "year": int(year),
        "month": _int(month),
        "day": day,
        "text": text,
        "days": _int(numbers.get("days")),
        "mean_f": _num(numbers.get("mean_f")),
        "high_f": _num(numbers.get("high_f")),
        "high_day": numbers.get("high_day"),
        "low_f": _num(numbers.get("low_f")),
        "low_day": numbers.get("low_day"),
        "rain_in": _num(numbers.get("rain_in")),
        "hdd": _num(numbers.get("hdd")),
        "cdd": _num(numbers.get("cdd")),
        "gust_mph": _num(numbers.get("gust_mph")),
    }


def noaa_title(kind: str, station: str, year: int,
               month: int | None, day: str | None = None) -> str:
    if kind == KIND_NOAA_DAY and day:
        from datetime import date as _date
        d = _date.fromisoformat(day)
        return f"{station} · {d:%B} {d.day}, {d.year} climate report"
    if kind == KIND_NOAA_MONTH and month:
        import calendar
        return f"{station} · {calendar.month_name[month]} {year} climate report"
    return f"{station} · {year} climate report"


def noaa_summary_line(payload: dict[str, Any], units: Any = None) -> str:
    u = _units(units)
    bits: list[str] = []
    hi, lo = payload.get("high_f"), payload.get("low_f")
    if hi is not None and lo is not None:
        bits.append(f"High {round(u.temp(hi))}, low {round(u.temp(lo))}")
    mean = payload.get("mean_f")
    if mean is not None:
        bits.append(f"mean {u.temp(mean):.1f}")
    rain = payload.get("rain_in")
    if rain is not None:
        bits.append(f"{u.rain_amount(rain)} rain")
    days = payload.get("days")
    if days and not payload.get("day"):
        bits.append(f"{days} day{'s' if days != 1 else ''}")
    gust = payload.get("gust_mph")
    if payload.get("day") and gust is not None:
        bits.append(f"gust {u.wind_speed(gust)}" if hasattr(u, "wind_speed")
                    else f"gust {round(gust)} mph")
    return " · ".join(bits) or "No rollup data for this period"


# ── dedupe keys ─────────────────────────────────────────────────────────
#
# One row per report, whatever the retry does. The morning report retries
# its phone half on its own stamp (R15), and a storm summary that fails to
# send is re-attempted next tick — either could otherwise write a second
# row for the same report.

def morning_key(for_date: str) -> str:
    return f"{KIND_MORNING}:{for_date}"


def storm_key(mac: str, started_ms: int) -> str:
    return f"{KIND_STORM}:{mac}:{int(started_ms)}"


def noaa_key(kind: str, mac: str, year: int, month: int | None,
             day: str | None = None) -> str:
    """One row per station and period: re-running a month that is still
    in progress updates its row rather than stacking a copy per run."""
    if kind == KIND_NOAA_DAY and day:
        return f"{kind}:{mac}:{day}"
    if kind == KIND_NOAA_MONTH:
        return f"{kind}:{mac}:{int(year)}-{int(month or 0):02d}"
    return f"{kind}:{mac}:{int(year)}"
