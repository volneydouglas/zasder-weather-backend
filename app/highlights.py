"""Today's highlights (2.3): the three to five readings that make today
unusual FOR THIS STATION, ranked against its own record for the same
time of year. "Hotter than 94% of mid-Septembers here", "warmest
overnight low in 8 years", "no rain in 47 days".

The comparisons are the station's own daily rollups for a ±7 day window
around today's date across every year on record. They are rankings and
records, never "normals": Volney's call (normals.py) is that a normal
comes from NOAA or not at all, and the Today card already shows that
line. Nothing here is computed for a station with under two years of
rollups in the window: one year is not a record to rank against.

Pure scoring in `compute`; `assemble` gathers the inputs.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .day_rain import day_rain_in

log = logging.getLogger("zasder.highlights")

WINDOW_DAYS = 7          # ±7 days: 15 calendar days across each year
MIN_YEARS = 2            # under this the window is not a record
MIN_SCORE = 0.3          # below this a line is not a highlight
MAX_LINES = 5
# A day-so-far reading is only comparable with whole days once the part
# of the day that sets it has happened (local hour). At 00:40 the "high"
# is a night reading and ranked "cooler than 100% of days" (own box,
# 09-13). Gusts, rain and the streak only grow, so they compare any time.
HIGH_AFTER_HOUR = 15     # the afternoon high is in
LOW_AFTER_HOUR = 8       # the overnight low is in
HUMIDITY_AFTER_HOUR = 12 # half a day of samples

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def window_keys(today: date, days: int = WINDOW_DAYS) -> list[str]:
    """The MM-DD keys of the window, in order; Feb 29 rides with a leap
    year's neighbours."""
    return [(today + timedelta(days=i)).strftime("%m-%d") for i in range(-days, days + 1)]


def window_label(today: date) -> str:
    part = "early" if today.day <= 10 else "mid" if today.day <= 20 else "late"
    return f"{part}-{_MONTHS[today.month - 1]}"


def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _percentile_below(value: float, others: list[float]) -> float:
    """Share of `others` strictly below `value`, 0..1."""
    if not others:
        return 0.5
    return sum(1 for o in others if o < value) / len(others)


def _years(rows: list[dict[str, Any]], today_day: date) -> int:
    """PRIOR years in the window. The rows include this year's earlier
    window days (they are ranked against too), but this year is not a
    year of record: with one prior year the line used to read "Hottest
    mid-September day in 2 years" (R23)."""
    this_year = f"{today_day.year:04d}"
    return len({str(r.get("day", ""))[:4] for r in rows
                if r.get("day") and str(r.get("day", ""))[:4] < this_year})


# Every sentence the card can say, by FORM (F04). The backend writes the
# English with °F, mph and inches in it, and the app used to show that
# text as-is beside cards it had converted to the reader's units. Each
# line now also carries its `form` and typed NATIVE `args`, so the app
# formats a form it knows in the reader's units and falls back to `text`
# for one it does not. `text` is built FROM the template and the args,
# never beside them, so the two cannot drift. Units in args: tempf °F,
# mph, inches; percent 0..100; years/days whole; period is the window
# label ("mid-September"); date is the ISO day and date_label its
# English form.
FORMS: dict[str, str] = {
    "high-record-hot": "Hottest {period} day in {years} years: {tempf:.0f}°F",
    "high-record-cool": "Coolest {period} day in {years} years: {tempf:.0f}°F",
    "high-rank-hot": "Hotter than {percent:.0f}% of {period} days here",
    "high-rank-cool": "Cooler than {percent:.0f}% of {period} days here",
    "low-record-warm": "Warmest {period} overnight low in {years} years: {tempf:.0f}°F",
    "low-record-cold": "Coldest {period} overnight low in {years} years: {tempf:.0f}°F",
    "low-rank-warm": "Warmer overnight than {percent:.0f}% of {period} nights",
    "low-rank-cold": "Colder overnight than {percent:.0f}% of {period} nights",
    "gust-record": "Strongest {period} gust in {years} years: {mph:.0f} mph",
    "gust-rank": "Gustier than {percent:.0f}% of {period} days: {mph:.0f} mph",
    "rain-record": "Wettest {period} day in {years} years: {inches:.2f} in",
    "rain-rank": "Wetter than {percent:.0f}% of {period} days: {inches:.2f} in",
    "humid": "Unusually humid for {period}: {percent:.0f}% against {usual_percent:.0f}% usual",
    "dry-air": "Unusually dry air for {period}: {percent:.0f}% against {usual_percent:.0f}% usual",
    "dry-streak": "No rain in {days} days",
    "streak-ends": "First rain in {days} days",
    "last-rain": "Last recorded rain: {date_label}",
}

# The dry streak is asserted only when the gauge COVERED the days between
# the last wet day and today (F03): this share of them must have a
# rollup row with a measured rain value. Below it the card says when it
# last recorded rain, which is what the ledger actually knows.
STREAK_COVERAGE = 0.9


def _date_label(d: date, today_day: date) -> str:
    label = f"{_MONTHS[d.month - 1]} {d.day}"
    return label if d.year == today_day.year else f"{label}, {d.year}"


def compute(*, today: dict[str, Any], window: list[dict[str, Any]],
            last_rain_day: str | None, today_day: date,
            local_hour: int = 23,
            covered_days: int | None = None) -> dict[str, Any]:
    """Rank today's readings against the window. `today` carries
    tempf_max, tempf_min, rain_in, gust_mph, humidity_avg (each None when
    unmeasured); `window` is the station's daily_rollups rows for the
    same calendar window in PRIOR years; `last_rain_day` is the latest
    rollup day with measurable rain (any date); `local_hour` gates the
    readings the day has not finished setting; `covered_days` is how many
    of the days strictly between `last_rain_day` and today have a rollup
    row with a measured rain value (None: unknown, which is not coverage)."""
    years = _years(window, today_day)
    label = window_label(today_day)
    out: list[dict[str, Any]] = []
    today = dict(today)
    if local_hour < HIGH_AFTER_HOUR:
        today["tempf_max"] = None
    if local_hour < LOW_AFTER_HOUR:
        today["tempf_min"] = None
    if local_hour < HUMIDITY_AFTER_HOUR:
        today["humidity_avg"] = None
    if years >= MIN_YEARS:
        highs = [v for v in (_finite(r.get("tempf_max")) for r in window) if v is not None]
        lows = [v for v in (_finite(r.get("tempf_min")) for r in window) if v is not None]
        gusts = [v for v in (_finite(r.get("windgustmph_max")) for r in window) if v is not None]
        hums = []
        for r in window:
            s, n = _finite(r.get("humidity_sum")), _finite(r.get("humidity_n"))
            if s is not None and n:
                hums.append(s / n)
        rains = [v for v in (day_rain_in(r) for r in window) if v is not None]

        tmax = _finite(today.get("tempf_max"))
        if tmax is not None and len(highs) >= 10:
            p = _percentile_below(tmax, highs)
            if tmax >= max(highs):
                out.append(_line("high-record", "high-record-hot",
                                 {"period": label, "years": years, "tempf": tmax},
                                 1.0, "warm", tmax))
            elif tmax <= min(highs):
                out.append(_line("cool-record", "high-record-cool",
                                 {"period": label, "years": years, "tempf": tmax},
                                 0.95, "cool", tmax))
            elif p >= 0.85:
                out.append(_line("high-rank", "high-rank-hot",
                                 {"period": label, "percent": p * 100},
                                 0.3 + (p - 0.85) * 4, "warm", tmax))
            elif p <= 0.15:
                out.append(_line("high-rank", "high-rank-cool",
                                 {"period": label, "percent": (1 - p) * 100},
                                 0.3 + (0.15 - p) * 4, "cool", tmax))

        tmin = _finite(today.get("tempf_min"))
        if tmin is not None and len(lows) >= 10:
            p = _percentile_below(tmin, lows)
            if tmin >= max(lows):
                out.append(_line("low-record", "low-record-warm",
                                 {"period": label, "years": years, "tempf": tmin},
                                 0.95, "warm", tmin))
            elif tmin <= min(lows):
                out.append(_line("low-record", "low-record-cold",
                                 {"period": label, "years": years, "tempf": tmin},
                                 0.95, "cool", tmin))
            elif p >= 0.85:
                out.append(_line("low-rank", "low-rank-warm",
                                 {"period": label, "percent": p * 100},
                                 0.25 + (p - 0.85) * 4, "warm", tmin))
            elif p <= 0.15:
                out.append(_line("low-rank", "low-rank-cold",
                                 {"period": label, "percent": (1 - p) * 100},
                                 0.25 + (0.15 - p) * 4, "cool", tmin))

        gust = _finite(today.get("gust_mph"))
        if gust is not None and gust >= 15 and len(gusts) >= 10:
            if gust >= max(gusts):
                out.append(_line("gust-record", "gust-record",
                                 {"period": label, "years": years, "mph": gust},
                                 0.9, "alert", gust))
            else:
                p = _percentile_below(gust, gusts)
                if p >= 0.9:
                    out.append(_line("gust-rank", "gust-rank",
                                     {"period": label, "percent": p * 100, "mph": gust},
                                     0.3 + (p - 0.9) * 5, "alert", gust))

        rain = _finite(today.get("rain_in"))
        if rain is not None and rain >= 0.05 and len(rains) >= 10:
            if rain >= max(rains):
                out.append(_line("rain-record", "rain-record",
                                 {"period": label, "years": years, "inches": rain},
                                 0.9, "info", rain))
            else:
                p = _percentile_below(rain, rains)
                if p >= 0.85:
                    out.append(_line("rain-rank", "rain-rank",
                                     {"period": label, "percent": p * 100, "inches": rain},
                                     0.3 + (p - 0.85) * 4, "info", rain))

        hum = _finite(today.get("humidity_avg"))
        if hum is not None and len(hums) >= 10:
            mean = sum(hums) / len(hums)
            var = sum((h - mean) ** 2 for h in hums) / len(hums)
            sd = math.sqrt(var)
            if sd >= 1:
                z = (hum - mean) / sd
                if z >= 1.5:
                    out.append(_line("humid", "humid",
                                     {"period": label, "percent": hum, "usual_percent": mean},
                                     min(1.0, 0.3 + (z - 1.5) * 0.3), "info", hum))
                elif z <= -1.5:
                    out.append(_line("dry-air", "dry-air",
                                     {"period": label, "percent": hum, "usual_percent": mean},
                                     min(1.0, 0.3 + (-z - 1.5) * 0.3), "info", hum))

    # The dry streak needs no window, only the last wet day on record —
    # and today's rain, MEASURED. A station whose gauge only reports a
    # lifetime counter (WH24, Atlas) has no dailyrainin, and `or 0.0`
    # printed "No rain in 42 days" while it rained (R23). Unknown today
    # is neither line.
    #
    # And the days BETWEEN must have been measured (F03): the last wet
    # day on record minus today is not a drought if the gauge, or the
    # server, was off for the weeks in between. Without that coverage the
    # card says when it last RECORDED rain, which is the fact it has.
    rain_today = _finite(today.get("rain_in"))
    if last_rain_day and rain_today is not None:
        try:
            last = date.fromisoformat(last_rain_day)
            streak = (today_day - last).days
        except ValueError:
            last, streak = None, 0
        between = max(0, streak - 1)
        covered = (between == 0 or (covered_days is not None
                                    and covered_days >= STREAK_COVERAGE * between))
        if streak >= 14 and rain_today < 0.01:
            if covered:
                out.append(_line("dry-streak", "dry-streak", {"days": streak},
                                 min(1.0, 0.3 + (streak - 14) / 60), "info", float(streak)))
            elif last is not None:
                out.append(_line("last-rain", "last-rain",
                                 {"date": last.isoformat(),
                                  "date_label": _date_label(last, today_day)},
                                 MIN_SCORE, "info", float(streak)))
        elif streak >= 14 and rain_today >= 0.01 and covered:
            out.append(_line("streak-ends", "streak-ends", {"days": streak},
                             min(1.0, 0.5 + (streak - 14) / 60), "info", float(streak)))

    out.sort(key=lambda h: -h["score"])
    kept = [h for h in out if h["score"] >= MIN_SCORE][:MAX_LINES]
    return {"highlights": kept, "years": years, "window": label,
            "day": today_day.isoformat(),
            # The last day with measurable rain on record, for "when did it
            # last rain" (Siri, 2.3); None when the rollups hold none.
            "last_rain_day": last_rain_day}


def _line(hid: str, form: str, args: dict[str, Any], score: float, kind: str,
          value: float) -> dict[str, Any]:
    """One highlight. `kind` is the TONE the card paints with (warm, cool,
    alert, info; the app decodes it as such), `form` the sentence, `args`
    its native-unit values; `text` is rendered from the two here."""
    args = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in args.items()}
    return {"id": hid, "text": FORMS[form].format(**args),
            "score": round(max(0.0, min(1.0, score)), 3),
            "kind": kind, "value": round(value, 2),
            "form": form, "args": args}


async def assemble(mac: str, now_ms: int | None = None) -> dict[str, Any]:
    """Gather today so far (from observations) and the window (from the
    rollups), then rank."""
    from . import db
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    now_ms = now_ms or int(time.time() * 1000)
    local = datetime.fromtimestamp(now_ms / 1000, tz)
    today_day = local.date()
    start_ms = int(datetime(today_day.year, today_day.month, today_day.day, tzinfo=tz)
                   .timestamp() * 1000)

    async def agg(field: str) -> dict[str, Any]:
        try:
            return await db.aggregate(mac, field, start_ms, now_ms)
        except Exception:
            return {}

    t = await agg("tempf")
    g = await agg("windgustmph")
    r = await agg("dailyrainin")
    h = await agg("humidity")
    today = {"tempf_max": t.get("max"), "tempf_min": t.get("min"),
             "gust_mph": g.get("max"), "rain_in": _finite(r.get("max")),
             "humidity_avg": h.get("avg") if (h.get("count") or 0) >= 12 else None}

    keys = window_keys(today_day)
    marks = ",".join("?" for _ in keys)
    async with db.connect() as conn:
        # The counter columns ride along so a counter-only station's days
        # rank too: without them day_rain_in read every prior day as
        # unmeasured and those stations never got a rain line (R23).
        rows = await (await conn.execute(
            "SELECT day, tempf_max, tempf_min, windgustmph_max, humidity_sum, humidity_n, "
            "rain_total, yearly_min, yearly_max, yearly_first, yearly_last, yearly_rise "
            f"FROM daily_rollups WHERE mac = ? AND substr(day, 6, 5) IN ({marks}) AND day < ?",
            (mac, *keys, today_day.isoformat()))).fetchall()
        # `<=`: today's own row rides at the top and is peeled off below,
        # so a counter-only station's rain today is read the one way the
        # repo reads a day's rain (day_rain.py) instead of becoming 0.0.
        wet = await (await conn.execute(
            "SELECT day, rain_total, yearly_min, yearly_max, "
            "yearly_first, yearly_last, yearly_rise FROM daily_rollups "
            "WHERE mac = ? AND day <= ? ORDER BY day DESC LIMIT 400",
            (mac, today_day.isoformat()))).fetchall()
    window = [dict(x) for x in rows]
    if wet and wet[0]["day"] == today_day.isoformat():
        today_row, wet = wet[0], wet[1:]
        if today["rain_in"] is None:
            today["rain_in"] = day_rain_in(today_row)
    last_rain_day = None
    covered_days = 0          # measured days after the last wet one (F03)
    for w in wet:
        v = day_rain_in(w)
        if v is not None and v >= 0.01:
            last_rain_day = w["day"]
            break
        if v is not None:
            covered_days += 1
    return compute(today=today, window=window, last_rain_day=last_rain_day,
                   today_day=today_day, local_hour=local.hour,
                   covered_days=covered_days if last_rain_day else None)
