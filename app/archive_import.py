"""Importing somebody else's archive (2.4, item 4).

Two doors into the same room.

WeeWX. A `weewx.sdb` is a SQLite file with one `archive` table, one row
per archive interval, and a `usUnits` column saying which of three unit
systems that row is in. People arrive with years of it and no way to
bring it, which is the single most common reason a self hoster stays put.

CSV. Everything else exports one: the GW3000's microSD card, Cumulus,
Weather Display, a spreadsheet somebody keeps by hand. There is no
standard, so the caller supplies a mapping of column name to field and
says which unit system the file is in.

THE RULES BOTH DOORS FOLLOW, and each of them is here because the live
ingest path learned it the hard way:

- Units are stored API native, °F, mph, inHg, inches. Conversion happens
  ONCE, here, on the way in. A threshold compared against a display unit
  is the bug this repo has shipped more than once.
- A missing reading stays missing. A station with no solar sensor must
  not import as a year of midnight, and `?? 0` is how that happens.
- The same plausibility bands the live path applies. Before the WU
  importer got them, whatever an archive held became fact, including
  dropout sentinels that then owned the all time records.
- Rows insert through `db.insert_observations`, which is INSERT OR
  IGNORE on (mac, dateutc), so re-running an import is free and a file
  that overlaps what is already stored costs nothing.
"""
from __future__ import annotations

import asyncio
import csv as _csv
import io
import logging
import math
import sqlite3
import time
from typing import Any, Iterable, Iterator

from . import db
from .config import settings
from .ingest import _apply_plausibility_bands

log = logging.getLogger("zasder.archive_import")

# How many rows go in per write, and how long the importer sleeps between
# batches. A decade of five minute archives is about a million rows, and
# the lesson from the rollup rebuild is that an unpaced bulk write locks
# the SQLite writer and takes ingest down with it.
BATCH_ROWS = 500
BATCH_PAUSE_S = 0.05

# WeeWX unit systems (weewx.units): 1 US, 16 METRIC, 17 METRICWX. The
# difference between the last two is wind, km/h against m/s, which is
# exactly the kind of thing that silently triples somebody's gusts.
US, METRIC, METRICWX = 1, 16, 17
UNIT_SYSTEMS = {"us": US, "metric": METRIC, "metricwx": METRICWX}


def _f(v: Any) -> float | None:
    """A real number, or nothing. Bools are not numbers here: isinstance
    of True against int is True, and a stray boolean reached SQLite as
    1.0 once already."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        if isinstance(v, str):
            t = v.strip()
            if not t:
                return None
            try:
                v = float(t)
            except ValueError:
                return None
        else:
            return None
    return float(v) if math.isfinite(float(v)) else None


def c_to_f(v: float | None) -> float | None:
    return None if v is None else v * 9.0 / 5.0 + 32.0


def kmh_to_mph(v: float | None) -> float | None:
    return None if v is None else v * 0.621371


def ms_to_mph(v: float | None) -> float | None:
    return None if v is None else v * 2.236936


def mbar_to_inhg(v: float | None) -> float | None:
    return None if v is None else v * 0.0295299830714


def mm_to_in(v: float | None) -> float | None:
    return None if v is None else v / 25.4


def cm_to_in(v: float | None) -> float | None:
    return None if v is None else v / 2.54


def convert(value: float | None, kind: str, system: int) -> float | None:
    """One reading into the units this database stores.

    `kind` is what the number MEANS, not what it is called, because the
    same column name is degrees in one file and something else in the
    next.
    """
    if value is None or system == US:
        return value
    if kind == "temp":
        return c_to_f(value)
    if kind == "wind":
        return ms_to_mph(value) if system == METRICWX else kmh_to_mph(value)
    if kind == "pressure":
        return mbar_to_inhg(value)
    if kind == "rain":
        return mm_to_in(value) if system == METRICWX else cm_to_in(value)
    return value


# WeeWX column → (our field, what the number means). Only the fields this
# database has a column for; everything else in an archive stays behind
# rather than being guessed at.
WEEWX_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("outTemp", "tempf", "temp"),
    ("outHumidity", "humidity", "none"),
    ("windSpeed", "windspeedmph", "wind"),
    ("windGust", "windgustmph", "wind"),
    ("windDir", "winddir", "none"),
    ("barometer", "baromrelin", "pressure"),
    ("pressure", "baromabsin", "pressure"),
    ("dewpoint", "dewPoint", "temp"),
    ("heatindex", "feelsLike", "temp"),
    ("radiation", "solarradiation", "none"),
    ("UV", "uv", "none"),
    ("rainRate", "rainratein", "rain"),
    ("inTemp", "tempinf", "temp"),
    ("inHumidity", "humidityin", "none"),
)


def weewx_row(row: dict[str, Any], *, default_system: int = US
              ) -> dict[str, Any] | None:
    """One `archive` row → one observation row, or None when it has no
    usable timestamp.

    The row's OWN `usUnits` wins: a database that changed unit system
    part way through its life, which happens when somebody edits
    weewx.conf, converts correctly on both sides of the change.
    """
    ts = _f(row.get("dateTime"))
    if ts is None or ts <= 0:
        return None
    system = row.get("usUnits")
    system = int(system) if isinstance(system, (int, float)) else default_system
    if system not in (US, METRIC, METRICWX):
        system = default_system
    out: dict[str, Any] = {"dateutc": int(ts * 1000)}
    for column, field, kind in WEEWX_FIELDS:
        value = convert(_f(row.get(column)), kind, system)
        if value is not None:
            out[field] = value
    # WeeWX's `rain` is the accumulation during THIS interval, which is
    # not what any of our rain columns mean. Kept under its own key so it
    # survives in data_json for provenance rather than being written into
    # a column it would be a lie in ([[rain counters]]).
    interval_rain = convert(_f(row.get("rain")), "rain", system)
    if interval_rain is not None:
        out["intervalRainIn"] = interval_rain
    out["source"] = "weewx-import"
    _band(out, "weewx")
    return out


def csv_rows(text: str, mapping: dict[str, str], *, system: int = US,
             time_column: str = "", time_format: str = "") -> Iterator[dict]:
    """Rows from a CSV, through a caller supplied column mapping.

    `mapping` is column name → our field name. The time column is named
    separately because it is the one field with no reading in it, and
    `time_format` is a strptime pattern, or empty for an epoch.
    """
    reader = _csv.DictReader(io.StringIO(text))
    for raw in reader:
        row = csv_row(raw, mapping, system=system, time_column=time_column,
                      time_format=time_format)
        if row is not None:
            yield row


def csv_row(raw: dict[str, Any], mapping: dict[str, str], *, system: int = US,
            time_column: str = "", time_format: str = "") -> dict | None:
    ts_ms = _parse_time(raw.get(time_column), time_format)
    if ts_ms is None:
        return None
    out: dict[str, Any] = {"dateutc": ts_ms}
    for column, field in mapping.items():
        kind = FIELD_KINDS.get(field)
        if kind is None:
            continue
        value = convert(_f(raw.get(column)), kind, system)
        if value is not None:
            out[field] = value
    out["source"] = "csv-import"
    _band(out, "csv")
    return out


# What each of our fields MEANS, for the converter. A field not in here
# cannot be imported, which is deliberate: a mapping that names a column
# we have no home for should be refused rather than quietly dropped.
FIELD_KINDS: dict[str, str] = {
    "tempf": "temp", "tempinf": "temp", "dewPoint": "temp",
    "feelsLike": "temp", "windchillf": "temp",
    "humidity": "none", "humidityin": "none",
    "windspeedmph": "wind", "windgustmph": "wind", "winddir": "none",
    "baromrelin": "pressure", "baromabsin": "pressure",
    "solarradiation": "none", "uv": "none",
    "rainratein": "rain", "dailyrainin": "rain", "eventrainin": "rain",
    "hourlyrainin": "rain", "totalrainin": "rain",
    # Rain that fell DURING the row's interval (WeeWX's `rain`, Cumulus's
    # and most spreadsheets' rain column). Summed into the day counter by
    # run_import; see day_counter_from_intervals.
    "intervalRainIn": "rain",
}


def _parse_time(value: Any, fmt: str) -> int | None:
    """Epoch milliseconds from whatever the file had in its time column."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not fmt:
        seconds = _f(text)
        if seconds is None or seconds <= 0:
            return None
        # A 13 digit number is milliseconds already. The WU importer
        # learned this one from a 2015 archive that landed in year 47000.
        return int(seconds) if seconds > 1e11 else int(seconds * 1000)
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(text, fmt)
        if dt.tzinfo is None:
            # A file without an offset is read in the SERVER's zone,
            # which is the station's zone, because that is what somebody
            # exporting their own history meant by "14:05".
            from zoneinfo import ZoneInfo
            try:
                dt = dt.replace(tzinfo=ZoneInfo(settings.timezone))
            except Exception:
                dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _band(row: dict[str, Any], label: str) -> None:
    """The live path's plausibility bands, field by field, so one bad
    reading never costs a row its good fields."""
    if not settings.ingest_plausibility_bands:
        return
    dropped = _apply_plausibility_bands(row)
    if dropped:
        log.warning("%s-import: implausible values dropped at %s: %s",
                    label, row.get("dateutc"), ", ".join(dropped))


def read_weewx(path: str, *, limit: int | None = None) -> Iterator[dict]:
    """Rows out of a weewx.sdb, oldest first, as observation dicts.

    Opened read only. A file somebody uploaded is not a database this
    server is allowed to write to, and opening it read write would
    create a journal beside it.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM archive ORDER BY dateTime"
                           + (f" LIMIT {int(limit)}" if limit else ""))
        for raw in cur:
            row = weewx_row(dict(raw))
            if row is not None:
                yield row
    finally:
        conn.close()


def weewx_summary(path: str) -> dict[str, Any]:
    """What is in the file, without importing it. The number people want
    before they agree to anything is how much and how far back."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(dateTime) AS lo, MAX(dateTime) AS hi "
            "FROM archive").fetchone()
        return {"rows": row[0] or 0,
                "first_ms": int(row[1] * 1000) if row[1] else None,
                "last_ms": int(row[2] * 1000) if row[2] else None}
    finally:
        conn.close()


# ── the job ───────────────────────────────────────────────────────────

JOB: dict[str, Any] = {"state": "idle"}
_TASK: asyncio.Task | None = None


def status() -> dict[str, Any]:
    return dict(JOB)


def cancel() -> bool:
    if JOB.get("state") == "running":
        JOB["cancel"] = True
        return True
    return False


def _now_ms() -> int:
    return int(time.time() * 1000)


class _DayCounter:
    """The day counter a station would have posted, from interval rain.

    WeeWX's `rain` is what fell during the row's interval, which is not
    what any of our counter columns mean, and 2.4 kept it in data_json
    for provenance only. R24-03 (2.4 release review): four imported
    intervals of 0.05 in reported four insertions and a day ledger rain
    of None. So the importer sums the intervals per LOCAL day, in the
    server's zone (the station's), into `dailyrainin`, which is exactly
    the number a tier-three station posts and the fold already reads.
    Reset at local midnight, rows ascending. Nothing else is touched:
    the yearly and total counters stay absent, because an archive that
    starts mid-year cannot say what they were.
    """

    def __init__(self) -> None:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        try:
            self._tz = ZoneInfo(settings.timezone)
        except Exception:
            self._tz = timezone.utc
        self._day: str | None = None
        self._sum = 0.0
        self._dt = datetime
        self._utc = timezone.utc

    def apply(self, row: dict[str, Any]) -> None:
        inc = row.get("intervalRainIn")
        at = row.get("dateutc")
        if inc is None or at is None:
            return
        day = (self._dt.fromtimestamp(int(at) / 1000, self._utc)
               .astimezone(self._tz).strftime("%Y-%m-%d"))
        if day != self._day:
            self._day, self._sum = day, 0.0
        self._sum = round(self._sum + max(0.0, float(inc)), 4)
        row["dailyrainin"] = self._sum


def day_counter_from_intervals(rows: Iterable[dict], *,
                               ordered: bool = True) -> Iterator[dict]:
    """Rows in ascending time with `dailyrainin` synthesised from
    `intervalRainIn`. A running sum only means something in order:
    WeeWX rows arrive ORDER BY dateTime from a generator and are taken
    as they come, so a decade of archive is never held in memory; a CSV
    can come any way its author left it and is already in memory, so
    its door passes `ordered=False` and the rows are sorted here."""
    counter = _DayCounter()
    if not ordered:
        rows = sorted(rows, key=lambda r: int(r.get("dateutc") or 0))
    for row in rows:
        counter.apply(row)
        yield row


async def run_import(mac: str, rows: Iterable[dict], *, kind: str,
                     dry_run: bool = False,
                     ordered: bool = True) -> dict[str, Any]:
    """Insert an archive, paced, with the job dict carrying progress.

    Paced on purpose. A decade of five minute archives is about a million
    rows, and an unpaced bulk write holds the SQLite writer long enough
    to take live ingest down with it, which this project has done to
    itself once already and does not intend to repeat.
    """
    JOB.clear()
    JOB.update(state="running", kind=kind, mac=mac, read=0, inserted=0,
               started_ms=_now_ms(), dry_run=bool(dry_run))
    batch: list[dict] = []
    try:
        for row in day_counter_from_intervals(rows, ordered=ordered):
            if JOB.get("cancel"):
                JOB.update(state="cancelled", finished_ms=_now_ms())
                return status()
            batch.append(row)
            JOB["read"] += 1
            if len(batch) >= BATCH_ROWS:
                if not dry_run:
                    JOB["inserted"] += await db.insert_observations(mac, batch)
                batch = []
                await asyncio.sleep(BATCH_PAUSE_S)
        if batch and not dry_run:
            JOB["inserted"] += await db.insert_observations(mac, batch)
        JOB.update(state="done", finished_ms=_now_ms())
    except Exception as e:
        log.exception("%s import failed", kind)
        JOB.update(state="error", error=str(e), finished_ms=_now_ms())
    return status()
