"""Daily reference evapotranspiration, stored (2.7, the water balance).

One number per station per complete local day: how much water the air
took out of a reference lawn, in inches (API-native, like rain). The
Watering card's seven-day call and the Water balance page read the same
stored values, so the card and the history never disagree.

ONE method per day, chosen here:

  penman-monteith  FAO-56 hourly Penman-Monteith integrated over the day's
                   raw readings (derived.evapotranspiration_over, the same
                   integration the Science page's "today" row runs), when
                   those readings carry temperature, humidity, wind, solar
                   and pressure across at least PM_MIN_HOURS of the day.
  hargreaves       FAO-56 eq. 52 from the day's rollup high and low and the
                   station latitude (watering.hargreaves_et0_in), otherwise:
                   no solar head, a day with an outage, or an old day whose
                   raw readings were thinned coarser than an hour.
  (nothing)        neither is possible. NO row is stored (absent is not
                   zero) and the day is tried again later.

Both need the station's location (Hargreaves reads the latitude; the
Science row has always gated its ET on it), so a station without one has
no ET at all and the page says why.

When the values are made:
  - shortly after local midnight, for yesterday, for every weather
    station (store_yesterday_all, run by a background job in main.py);
  - lazily on read, for any day in the asked window with no row yet,
    newest first and bounded per request; the rest of a long window is
    filled by a per-station background task and the page says so.

When they are thrown away: when a day's readings change. Rows landing on
a complete past day (an import, a relay's backlog) drop that day inside
the insert (db.insert_observations); a rollup rebuild or a database
restore drops everything (clear); a location change drops the Hargreaves
rows (db.set_device_location); deleting a device drops its rows.

A fill in flight cannot undo one (R27-03, the 2.7 review). Every path
that drops rows opens an invalidation (`invalidating`) before its delete
and closes it after its commit; a fill takes the `epoch()` before it reads
anything and stores only if no invalidation of its station (or of every
station) is open or has closed since. The check runs inside the store's
own write transaction, so an invalidation cannot slip between the check
and the write, and the expensive computation holds no lock. A Hargreaves
value also stores only while the station's latitude is still the one it
was computed with, which covers a caller that read the latitude before a
move and started the fill after it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db

log = logging.getLogger("daily_et")

PM = "penman-monteith"
HG = "hargreaves"

# The coverage threshold, in hours of the day the Penman-Monteith integral
# actually spans: every step between two readings that carried all five
# inputs and were at most derived.ET_MAX_STEP_H (one hour) apart. 20 of 24
# tolerates a short outage, a reboot, or a few dark hours either side of
# midnight, which add almost nothing to the day's ET; a day missing more
# than a sixth could be missing the afternoon, where most of the water
# goes, so it falls back to Hargreaves, which reads the rollup's true high
# and low. The same 20 h the forecast scorecard calls a covered day
# (forecast_skill.MIN_COVER_HOURS). Five-minute thinning keeps a day
# above it; thinning coarser than an hour drops the day to Hargreaves.
PM_MIN_HOURS = 20.0

# A day ends; readings still arriving for it (a relay draining its queue)
# get this long before the day may be stored. Before it, a value is
# computed for the reader but not kept.
SETTLE_MINUTES = 15

# Lazy fill, per request: at most this many missing days and this many
# seconds inline (a default 30-day window always fits); the rest goes to
# a background task.
LAZY_INLINE_DAYS = 31
LAZY_INLINE_S = 4.0

# A day that had no ET is not tried again for this long, unless what it
# was computed from changed: new readings on the day (the insert forgets
# the attempt), a different rollup row, or a location set since.
NULL_RETRY_S = 3600.0

# The nightly job's cadence: it wakes every TICK_S and stores yesterday
# for any station that has no row for it yet.
FIRST_TICK_S = 60
TICK_S = 900

DEFAULT_SPAN_DAYS = 30
MAX_SPAN_DAYS = 400

NO_LOCATION_NOTE = "ET needs the station's location."
NO_TEMPS_NOTE = "This station has not reported temperatures, which ET needs."
FILLING_NOTE = ("Older days are still being worked out. "
                "Open this again in a minute to see them.")

# (mac, day) -> (monotonic time, inputs fingerprint) of the last attempt
# that produced nothing.
_ATTEMPTED: dict[tuple[str, str], tuple[float, Any]] = {}
_ATTEMPTED_MAX = 50_000
# mac -> the background task filling that station's older days.
_FILL_TASKS: dict[str, asyncio.Task] = {}

# Invalidation generations (R27-03). _SEQ counts closed invalidations;
# _OPEN holds the ones in progress and _CLOSED when each key's last one
# closed, by station MAC or _ALL for every station.
_ALL = "*"
_SEQ = 0
_OPEN: dict[str, int] = {}
_CLOSED: dict[str, int] = {}


def epoch() -> int:
    """Taken by a fill before it reads any input; see `current`."""
    return _SEQ


def invalidation_began(mac: str | None) -> None:
    """Before the delete: from here until `invalidation_ended`, no fill of
    this station (None: of any) may store."""
    key = _ALL if mac is None else mac
    _OPEN[key] = _OPEN.get(key, 0) + 1


def invalidation_ended(mac: str | None) -> None:
    """After the commit (or the failure): every fill that took its epoch
    before this point is stale from now on."""
    global _SEQ
    key = _ALL if mac is None else mac
    n = _OPEN.get(key, 0) - 1
    if n > 0:
        _OPEN[key] = n
    else:
        _OPEN.pop(key, None)
    _SEQ += 1
    _CLOSED[key] = _SEQ


@contextlib.contextmanager
def invalidating(mac: str | None):
    """Wrap a path that drops stored ET: began on entry, ended on exit,
    exception or not (an invalidation left open would refuse every later
    store for the station)."""
    invalidation_began(mac)
    try:
        yield
    finally:
        invalidation_ended(mac)


class Invalidation:
    """`async with connect() as db, Invalidation(mac) as inv:` for a write
    path that drops stored ET inside its transaction. `opened=True` opens
    on entry; otherwise `inv.open()` right before the delete, so a write
    that drops nothing (the common live post) opens nothing. Closes on
    exit, after the commit inside the block, exception or not."""

    def __init__(self, mac: str | None, *, opened: bool = False) -> None:
        self.mac = mac
        self._open_on_entry = opened
        self.opened = False

    def open(self) -> None:
        if not self.opened:
            invalidation_began(self.mac)
            self.opened = True

    async def __aenter__(self) -> "Invalidation":
        if self._open_on_entry:
            self.open()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        if self.opened:
            self.opened = False
            invalidation_ended(self.mac)
        return False


def current(mac: str, since: int) -> bool:
    """May a fill that took `epoch()` == since store for `mac`? Not while
    an invalidation of it is open, nor after one has closed since."""
    return (not _OPEN.get(mac) and not _OPEN.get(_ALL)
            and _CLOSED.get(mac, 0) <= since
            and _CLOSED.get(_ALL, 0) <= since)


def _tz():
    from zoneinfo import ZoneInfo
    from .config import settings
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        return timezone.utc


def tz_name() -> str:
    from .config import settings
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(settings.timezone)
        return settings.timezone
    except Exception:
        return "UTC"


def local_today(now_ms: int | None = None) -> date:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return datetime.fromtimestamp(now_ms / 1000, _tz()).date()


def day_bounds_ms(day: date) -> tuple[int, int]:
    """[local midnight, next local midnight) in ms; DST days are 23 or 25 h."""
    tz = _tz()
    lo = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    hi = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)


def settled_last_day(now_ms: int | None = None) -> date:
    """The newest day that may be stored: yesterday, once SETTLE_MINUTES
    of today have passed; the day before until then."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return local_today(now_ms - SETTLE_MINUTES * 60_000) - timedelta(days=1)


def station_lat(device: dict[str, Any] | None) -> float | None:
    """The latitude of a device's location, parsed the way main's
    _device_coords parses it (both coordinates must be a real place)."""
    from .ingest import valid_coords
    c = ((((device or {}).get("info") or {}).get("coords") or {})
         .get("coords") or {})
    raw_lat, raw_lon = c.get("lat"), c.get("lon")
    if isinstance(raw_lat, bool) or isinstance(raw_lon, bool):
        return None
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except (TypeError, ValueError):
        return None
    return lat if valid_coords(lat, lon) else None


def _get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def rain_for(row: Any) -> float | None:
    """The day's rain the way the Watering card reads it: the rollup's
    day_rain_in, and None for a day the station did not cover (R25-A03)
    or did not measure."""
    from .day_rain import day_rain_in
    from .forecast_skill import covered
    if row is None or covered(dict(row)) is False:
        return None
    return day_rain_in(row)


async def compute_day(mac: str, day: date, lat: float | None,
                      rollup: Any) -> tuple[float, str] | None:
    """(inches, method) for one complete local day, or None. Pure of
    storage: reads the day's raw readings and the rollup row given."""
    from . import derived
    from .forecast_skill import covered
    from .watering import hargreaves_et0_in
    if lat is None:
        return None
    # A rollup that saw no sun that day says there is no solar head to
    # integrate; skip the raw read. No rollup (insights off) means ask.
    if rollup is None or _get(rollup, "solarradiation_max") is not None:
        lo, hi = day_bounds_ms(day)
        pm = derived.evapotranspiration_over(await db.et_input_rows(mac, lo, hi))
        if pm is not None and pm[1] >= PM_MIN_HOURS:
            return pm[0], PM
    if rollup is not None and covered(dict(rollup)) is not False:
        hg = hargreaves_et0_in(_get(rollup, "tempf_min"),
                               _get(rollup, "tempf_max"), lat,
                               day.timetuple().tm_yday)
        if hg is not None:
            return hg, HG
    return None


async def _rollups(mac: str, first: date, last: date) -> dict[str, Any]:
    from .climate import _rollup_rows
    try:
        rows = await _rollup_rows(mac, first.isoformat(), last.isoformat())
    except Exception:
        # No rollups table on a database init_db has not seen; treat as
        # no rollups rather than failing the read.
        log.exception("daily ET: rollup read failed for %s", mac)
        return {}
    return {r["day"]: r for r in rows}


async def stored(mac: str, first: date, last: date) -> dict[str, tuple[float, str]]:
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT day, et_in, method FROM daily_et WHERE mac = ? "
            "AND day >= ? AND day <= ?",
            (mac, first.isoformat(), last.isoformat()))).fetchall()
    return {r["day"]: (float(r["et_in"]), r["method"]) for r in rows}


async def _station_lat_now(conn, mac: str) -> float | None:
    """The station's latitude as list_devices would report it (an
    operator-set location first, else the ingest coordinates), read on
    the store's own connection."""
    row = await (await conn.execute(
        "SELECT lat, lon FROM device_location WHERE mac = ?",
        (mac,))).fetchone()
    if row is not None:
        coords = {"lat": row["lat"], "lon": row["lon"]}
    else:
        dev = await (await conn.execute(
            "SELECT info_json FROM devices WHERE mac = ?", (mac,))).fetchone()
        if dev is None:
            return None
        info = db._parse_json_col(dev["info_json"]).get("info") or {}
        coords = (info.get("coords") or {}).get("coords") or {}
    return station_lat({"info": {"coords": {"coords": coords}}})


async def _store(mac: str, day: date, et_in: float, method: str, *,
                 since: int, lat: float | None) -> bool:
    """Store one day's value unless it went stale while it was computed
    (R27-03). The write lock comes first, then the check, then the write:
    an invalidation either committed before the lock (and the check sees
    it) or deletes after this commit (and takes this row with it)."""
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        fresh = current(mac, since)
        if fresh and method == HG:
            # Hargreaves read this latitude; a station that has moved
            # since its caller looked keeps nothing from the old place.
            fresh = await _station_lat_now(conn, mac) == lat
        if not fresh:
            await conn.rollback()
            return False
        await conn.execute(
            "INSERT INTO daily_et (mac, day, et_in, method, computed_ms) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(mac, day) DO UPDATE SET "
            "et_in = excluded.et_in, method = excluded.method, "
            "computed_ms = excluded.computed_ms",
            (mac, day.isoformat(), float(et_in), method,
             int(time.time() * 1000)))
        await conn.commit()
    return True


def _fingerprint(lat: float | None, rollup: Any) -> Any:
    return (lat, None if rollup is None else tuple(rollup))


def _recently_attempted(mac: str, day: str, lat: float | None,
                        rollup: Any) -> bool:
    at = _ATTEMPTED.get((mac, day))
    return (at is not None and time.monotonic() - at[0] < NULL_RETRY_S
            and at[1] == _fingerprint(lat, rollup))


def _mark_attempted(mac: str, day: str, lat: float | None,
                    rollup: Any) -> None:
    if len(_ATTEMPTED) >= _ATTEMPTED_MAX:
        _ATTEMPTED.clear()
    _ATTEMPTED[(mac, day)] = (time.monotonic(), _fingerprint(lat, rollup))


def forget(mac: str, days) -> None:
    """A day's readings changed: try it again at the next read."""
    for d in days:
        _ATTEMPTED.pop((mac, d), None)


async def _fill_one(mac: str, day: date, lat: float | None, rollup: Any,
                    settled: date, since: int) -> tuple[float, str] | None:
    got = await compute_day(mac, day, lat, rollup)
    if got is None:
        # A stale "nothing" is not remembered either: the inputs that gave
        # it may already be gone.
        if current(mac, since):
            _mark_attempted(mac, day.isoformat(), lat, rollup)
        return None
    if day <= settled:
        await _store(mac, day, got[0], got[1], since=since, lat=lat)
    return got


async def ensure(mac: str, first: date, last: date, lat: float | None, *,
                 bounded: bool = True
                 ) -> tuple[dict[str, tuple[float, str]], bool]:
    """Every day's (inches, method) in [first, last] that exists, computing
    the missing ones newest first. `bounded`: at most LAZY_INLINE_DAYS
    days and LAZY_INLINE_S seconds inline, the rest handed to a background
    task; unbounded computes them all here. Returns (values by ISO day,
    pending): pending is True when a background task is still filling
    days of this window."""
    since = epoch()        # before ANY read: see `current`
    inline_days = LAZY_INLINE_DAYS if bounded else None
    inline_s = LAZY_INLINE_S if bounded else None
    have = await stored(mac, first, last)
    if lat is None or len(have) == (last - first).days + 1:
        return have, False
    rollups = await _rollups(mac, first, last)
    missing: list[date] = []
    d = last
    while d >= first:
        iso = d.isoformat()
        if iso not in have and not _recently_attempted(
                mac, iso, lat, rollups.get(iso)):
            missing.append(d)
        d -= timedelta(days=1)
    if not missing:
        return have, False
    settled = settled_last_day()
    t0 = time.monotonic()
    left: list[date] = []
    for i, day in enumerate(missing):
        if ((inline_days is not None and i >= inline_days)
                or (inline_s is not None and time.monotonic() - t0 > inline_s)):
            left = missing[i:]
            break
        got = await _fill_one(mac, day, lat, rollups.get(day.isoformat()),
                              settled, since)
        if got is not None:
            have[day.isoformat()] = got
    if not left:
        return have, False
    _spawn_fill(mac, left, lat, since)
    return have, True


def _spawn_fill(mac: str, days: list[date], lat: float,
                since: int | None = None) -> None:
    running = _FILL_TASKS.get(mac)
    if running is not None and not running.done():
        return
    task = asyncio.create_task(_fill_in_background(mac, days, lat, since))
    _FILL_TASKS[mac] = task
    task.add_done_callback(
        lambda t, m=mac: _FILL_TASKS.pop(m, None)
        if _FILL_TASKS.get(m) is t else None)


async def _fill_in_background(mac: str, days: list[date], lat: float,
                              since: int | None = None) -> None:
    """`since`: the epoch of the read that handed these days over (its
    latitude came with it). Once an invalidation makes it stale, the rest
    of the work is for inputs that are gone: stop, and the next read
    starts a fresh fill."""
    since = epoch() if since is None else since
    try:
        settled = settled_last_day()
        for day in days:
            if not current(mac, since):
                return
            iso = day.isoformat()
            if await stored(mac, day, day):
                continue
            rollups = await _rollups(mac, day, day)
            if _recently_attempted(mac, iso, lat, rollups.get(iso)):
                continue
            await _fill_one(mac, day, lat, rollups.get(iso), settled, since)
            await asyncio.sleep(0.02)       # ingest and readers go first
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("daily ET background fill failed for %s", mac)


async def store_yesterday_all(now_ms: int | None = None) -> int:
    """The nightly job's tick: store the newest settled day for every
    weather station that has no row for it. Returns how many it stored."""
    day = settled_last_day(now_ms)
    n = 0
    since = epoch()        # before the device list hands out latitudes
    for device in await db.list_devices():
        if db.is_air_monitor_device(device):
            continue
        mac = device.get("mac")
        lat = station_lat(device)
        if not mac or lat is None:
            continue
        iso = day.isoformat()
        if await stored(mac, day, day):
            continue
        rollups = await _rollups(mac, day, day)
        if _recently_attempted(mac, iso, lat, rollups.get(iso)):
            continue
        got = await compute_day(mac, day, lat, rollups.get(iso))
        if got is None:
            if current(mac, since):
                _mark_attempted(mac, iso, lat, rollups.get(iso))
            continue
        if await _store(mac, day, got[0], got[1], since=since, lat=lat):
            n += 1
    return n


async def clear(mac: str | None = None) -> None:
    """Drop stored values (one station, or all) so the next read
    recomputes them: after a rollup rebuild or a database restore."""
    with invalidating(mac):
        for m, t in list(_FILL_TASKS.items()):
            if mac is None or m == mac:
                t.cancel()
        if mac is None:
            _ATTEMPTED.clear()
        else:
            for k in [k for k in _ATTEMPTED if k[0] == mac]:
                _ATTEMPTED.pop(k, None)
        async with db.connect() as conn:
            if mac is None:
                await conn.execute("DELETE FROM daily_et")
            else:
                await conn.execute("DELETE FROM daily_et WHERE mac = ?",
                                   (mac,))
            await conn.commit()


# ── the water balance (GET /api/devices/{mac}/water-balance) ─────────────

class WindowError(ValueError):
    """The asked window cannot be served (start after end): a 422."""


def window(start: date | None, end: date | None,
           now_ms: int | None = None) -> tuple[date, date]:
    """Defaults, clamps and the one refusal of docs/2.7/WATER_BALANCE.md:
    end defaults to and is clamped to yesterday; start defaults to 29 days
    before end; a span over MAX_SPAN_DAYS moves start forward; a start
    after end raises WindowError."""
    yesterday = local_today(now_ms) - timedelta(days=1)
    end = yesterday if end is None or end > yesterday else end
    if start is None:
        start = end - timedelta(days=DEFAULT_SPAN_DAYS - 1)
    if start > end:
        raise WindowError("start must not be after end")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        start = end - timedelta(days=MAX_SPAN_DAYS - 1)
    return start, end


def _r(v: float | None, places: int) -> float | None:
    return None if v is None else round(v, places)


def _period(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Sums over a period's days: each field over the days that have it
    (null when none do); the balance over ONLY the days that have both,
    the running balance's rule, so a day of rain with no ET is not set
    against another day's ET (contract amendment, 10-15)."""
    ets = [d["et_in"] for d in days if d["et_in"] is not None]
    rains = [d["rain_in"] for d in days if d["rain_in"] is not None]
    both = [d for d in days
            if d["et_in"] is not None and d["rain_in"] is not None]
    return {"et_in": _r(sum(ets), 3) if ets else None,
            "rain_in": _r(sum(rains), 2) if rains else None,
            "balance_in": (_r(sum(d["rain_in"] - d["et_in"] for d in both), 3)
                           if both else None),
            "days_used": len(both)}


async def _ever_reported_temperature(mac: str, first: date, last: date) -> bool:
    lo, _ = day_bounds_ms(first)
    _, hi = day_bounds_ms(last)
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM observations WHERE mac = ? AND dateutc_ms >= ? "
            "AND dateutc_ms < ? AND tempf IS NOT NULL LIMIT 1",
            (mac, lo, hi))).fetchone()
    return row is not None


async def water_balance(mac: str, start: date, end: date,
                        lat: float | None) -> dict[str, Any]:
    """The contract's body for a window already through `window()`."""
    values, pending = await ensure(mac, start, end, lat)
    rollups = await _rollups(mac, start, end)
    days: list[dict[str, Any]] = []
    running: float | None = None
    methods: dict[str, int] = {}
    d = start
    while d <= end:
        iso = d.isoformat()
        v = values.get(iso)
        et = None if v is None else round(v[0], 3)
        method = None if v is None else v[1]
        rain = _r(rain_for(rollups.get(iso)), 2)
        if et is not None and rain is not None:
            running = (running or 0.0) + rain - et
        if method is not None:
            methods[method] = methods.get(method, 0) + 1
        days.append({"day": iso, "et_in": et, "method": method,
                     "rain_in": rain, "balance_in": _r(running, 3)})
        d += timedelta(days=1)

    weeks: list[dict[str, Any]] = []
    months: list[dict[str, Any]] = []
    by_week: dict[str, list[dict[str, Any]]] = {}
    by_month: dict[str, list[dict[str, Any]]] = {}
    for row in days:
        day = date.fromisoformat(row["day"])
        monday = (day - timedelta(days=day.weekday())).isoformat()
        by_week.setdefault(monday, []).append(row)
        by_month.setdefault(row["day"][:7], []).append(row)
    for monday, rows in by_week.items():
        weeks.append({"start": monday, **_period(rows)})
    for month, rows in by_month.items():
        months.append({"month": month, **_period(rows)})
    # The window's balance is the running balance's last value, which is
    # the period rule over the whole window.
    totals = {**_period(days), "days": len(days)}

    out: dict[str, Any] = {
        "mac": mac, "start": start.isoformat(), "end": end.isoformat(),
        "timezone": tz_name(), "days": days, "weeks": weeks,
        "months": months, "totals": totals, "methods": methods}
    if lat is None:
        out["note"] = NO_LOCATION_NOTE
    elif not values and not pending \
            and not await _ever_reported_temperature(mac, start, end):
        out["note"] = NO_TEMPS_NOTE
    elif pending:
        out["note"] = FILLING_NOTE
    return out


def csv_text(body: dict[str, Any]) -> str:
    """`day,et_in,method,rain_in,balance_in`, inches, a null an empty cell."""
    import csv
    import io

    def cell(v: Any, places: int | None = None) -> str:
        if v is None:
            return ""
        return f"{v:.{places}f}" if places is not None else str(v)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["day", "et_in", "method", "rain_in", "balance_in"])
    for d in body["days"]:
        w.writerow([d["day"], cell(d["et_in"], 3), cell(d["method"]),
                    cell(d["rain_in"], 2), cell(d["balance_in"], 3)])
    return buf.getvalue()
