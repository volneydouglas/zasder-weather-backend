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
import os
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
    # The BOM a spreadsheet export carries is dropped here as csv_header
    # drops it: validated against a clean header, parsed against the raw
    # one, the first column became "\ufeffwhen" and every row fell out
    # (CodeRabbit and Greptile, PR #50).
    reader = _csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for raw in reader:
        row = csv_row(raw, mapping, system=system, time_column=time_column,
                      time_format=time_format)
        if row is not None:
            yield row


def csv_file_rows(path: str, mapping: dict[str, str], *, system: int = US,
                  time_column: str = "", time_format: str = "") -> Iterator[dict]:
    """csv_rows, read from a file a row at a time (2.5): the file door
    never holds the CSV in memory. A UTF-8 BOM on the header is dropped,
    as a spreadsheet export often carries one."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        for raw in _csv.DictReader(f):
            row = csv_row(raw, mapping, system=system, time_column=time_column,
                          time_format=time_format)
            if row is not None:
                yield row


def csv_file_header(path: str) -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return next(_csv.reader(f), [])


# The sort must leave this much free on the disk it spills to; a file
# that would take the disk below it is refused mid-sort instead (Greptile,
# PR #48: the upload's own room check cannot see the sort's second copy).
SORT_MIN_FREE_BYTES = 64 * 1024 * 1024
# An (ts, seq) index entry: two varints, the rowid and cell and page
# overhead. Measured about 20 bytes a row; 32 leaves room.
INDEX_BYTES_PER_ROW = 32


class SortSpaceError(OSError):
    """The spill would have taken the disk below SORT_MIN_FREE_BYTES."""


def _free_bytes(path: str) -> int:
    import shutil
    return shutil.disk_usage(path).free


def spill_sorted(rows: Iterable[dict], workdir: str, chunk: int = 5000,
                 cancelled: Any = None) -> str:
    """Spill rows into a throwaway SQLite file beside the upload, indexed by
    time, and return its path (2.5). Blocking on purpose: run_import calls
    it through asyncio.to_thread, because reading and indexing a 512 MiB
    file before the first row comes back held the event loop (and live
    ingest with it) for the whole sort (Greptile, PR #48). The free-space
    floor is checked every chunk; SortSpaceError removes the file."""
    import json as _json
    import tempfile
    fd, path = tempfile.mkstemp(prefix=".csvsort-", suffix=".db", dir=workdir)
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE r (ts INTEGER, seq INTEGER, row TEXT)")
            batch = []

            def put() -> None:
                con.executemany("INSERT INTO r VALUES (?, ?, ?)", batch)
                batch.clear()
                if _free_bytes(workdir) < SORT_MIN_FREE_BYTES:
                    raise SortSpaceError(
                        "not enough free disk to sort the file: sorting keeps a "
                        "second copy of the readings beside the upload, and "
                        f"{workdir} fell below {SORT_MIN_FREE_BYTES // 2**20} MB free")
            for seq, row in enumerate(rows):
                batch.append((int(row.get("dateutc") or 0), seq,
                              _json.dumps(row, separators=(",", ":"))))
                if len(batch) >= chunk:
                    put()
                    if cancelled is not None and cancelled():
                        break
            if batch:
                put()
            con.commit()
            # The index is the phase the per-batch floor cannot see
            # (CodeRabbit, PR #48): two integers and a rowid per row,
            # about INDEX_BYTES_PER_ROW with B-tree overhead, checked
            # against the floor before it is built.
            n = con.execute("SELECT COUNT(*) FROM r").fetchone()[0]
            if _free_bytes(workdir) - n * INDEX_BYTES_PER_ROW < SORT_MIN_FREE_BYTES:
                raise SortSpaceError(
                    "not enough free disk to sort the file: its index needs about "
                    f"{n * INDEX_BYTES_PER_ROW // 2**20} MB more and {workdir} would "
                    f"fall below {SORT_MIN_FREE_BYTES // 2**20} MB free")
            con.execute("CREATE INDEX r_ts ON r (ts, seq)")
            con.commit()
        finally:
            con.close()
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _remove_spill(fut: "asyncio.Future[str]") -> None:
    if fut.cancelled() or fut.exception() is not None:
        return
    try:
        os.unlink(fut.result())
    except OSError:
        pass


def read_spilled(path: str) -> Iterator[dict]:
    """The spilled rows in ascending time, file order within a tie; the
    file is removed when the reader finishes or is closed."""
    import json as _json
    try:
        con = sqlite3.connect(path)
        try:
            for (text,) in con.execute("SELECT row FROM r ORDER BY ts, seq"):
                yield _json.loads(text)
        finally:
            con.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def sorted_on_disk(rows: Iterable[dict], workdir: str,
                   chunk: int = 5000) -> Iterator[dict]:
    """Rows in ascending time without holding them all (2.5): spilled into
    a throwaway SQLite file beside the upload and read back ORDER BY time.
    A CSV can come in any order its author left it, and the interval-rain
    day counter only means something in order. Synchronous; run_import
    spills through a thread instead."""
    yield from read_spilled(spill_sorted(rows, workdir, chunk))


def csv_header(text: str) -> list[str]:
    """The header row of CSV text (the JSON door's), BOM dropped."""
    return next(_csv.reader(io.StringIO(text.lstrip("\ufeff"))), [])


def mapping_problem(mapping: dict[str, str], header: list[str],
                    time_column: str) -> str | None:
    """Why this mapping cannot import this file, or None (R25-02, the 2.5
    detailed review). Checked before the job starts, because rows go in
    with INSERT OR IGNORE: an import that stored timestamps with no
    readings could never be repaired by importing the file again."""
    if not mapping:
        return "map at least one column to a reading"
    if time_column in mapping:
        return f"the time column {time_column!r} cannot also be a reading"
    from collections import Counter
    twice = sorted(c for c, n in Counter(header).items() if n > 1)
    if twice:
        return "the header names these columns more than once: " + ", ".join(twice)
    missing = sorted(c for c in mapping if c not in header)
    if missing:
        return "the file has no column named " + ", ".join(repr(c) for c in missing)
    return None


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
    # A row that carried no reading (blank cells, or every value refused
    # by the bands) is not an observation: stored, it would hold the
    # timestamp against a later import that has the values (R25-02).
    if not any(v is not None for k, v in out.items() if k not in ("dateutc", "source")):
        return None
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


# Held from the moment a door accepts an import until run_import ends
# (CodeRabbit, PR #48): status() is only a copy of JOB, and the file doors
# await a whole upload between checking it and starting the task, so two
# uploads could both pass the check and share one JOB.
_RESERVED = False


def reserve() -> bool:
    """Take the one import slot, or False when it is held or running. No
    await between the test and the set, so it is atomic on the loop."""
    global _RESERVED
    if _RESERVED or JOB.get("state") == "running":
        return False
    _RESERVED = True
    return True


def release() -> None:
    global _RESERVED
    _RESERVED = False


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
        # Every local day this import touched with interval rain, for the
        # recompute from stored rows once the rows are in (R25-A01/A02).
        self.days: set[str] = set()

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
        self.days.add(day)


# Days rebuilt between pauses (BATCH_PAUSE_S) after an import.
REBUILD_DAYS_PER_PAUSE = 30


async def rebuild_interval_days(mac: str, days: Iterable[str]) -> int:
    """Recompute each day's interval-rain counter from the rows actually
    stored (R25-A01/A02, the 2.5 additional review). The importer's running
    sum starts at zero for every import, and the day's total is the
    high-water mark of dailyrainin, so a day split across two imports kept
    only the larger half; and it summed a duplicate timestamp that INSERT
    OR IGNORE then dropped. Reading back the stored rows (one per
    timestamp, in time order) and their intervalRainIn fixes both, and
    running it again over the same days repairs an earlier import. Rows
    without an interval (live readings) keep their own counter, and the
    day's rain_total is the larger of the two. Returns the days rewritten.
    """
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    done = 0
    for n, day in enumerate(sorted(set(days))):
        # One day per transaction, paced like the insert batches (Greptile,
        # PR #52): a decade of archive is thousands of days, and one
        # transaction over all of them held the writer lock against live
        # ingest. A cancel stops between days; every day done is whole.
        if JOB.get("cancel"):
            break
        if n and n % REBUILD_DAYS_PER_PAUSE == 0:
            await asyncio.sleep(BATCH_PAUSE_S)
        start = datetime.fromisoformat(day).replace(tzinfo=tz)
        lo = int(start.timestamp() * 1000)
        hi = int((start + timedelta(days=1)).timestamp() * 1000)
        async with db.connect() as conn:
            rows = await (await conn.execute(
                "SELECT dateutc_ms, dailyrainin, "
                "json_extract(data_json, '$.intervalRainIn') AS inc "
                "FROM observations WHERE mac = ? AND dateutc_ms >= ? "
                "AND dateutc_ms < ? ORDER BY dateutc_ms", (mac, lo, hi))).fetchall()
            running = 0.0
            live_max: float | None = None
            int_span: list[int] = []
            live_span: list[int] = []
            for r in rows:
                inc = r["inc"]
                if isinstance(inc, (int, float)):
                    running = round(running + max(0.0, float(inc)), 4)
                    int_span.append(int(r["dateutc_ms"]))
                    await conn.execute(
                        "UPDATE observations SET dailyrainin = ? "
                        "WHERE mac = ? AND dateutc_ms = ?", (running, mac, r["dateutc_ms"]))
                elif isinstance(r["dailyrainin"], (int, float)):
                    live_max = max(live_max or 0.0, float(r["dailyrainin"]))
                    live_span.append(int(r["dateutc_ms"]))
            if not int_span:
                continue
            # A live dailyrainin counts from local midnight, so only live
            # readings that all come BEFORE the imported intervals add to
            # them (Greptile, PR #52: 0.10 live before noon + 0.10 imported
            # after is 0.20). Live readings after the intervals already hold
            # that earlier rain, and overlapping ones describe the same rain:
            # the larger stands.
            if live_max is not None and max(live_span) < min(int_span):
                total = round(live_max + running, 4)
            else:
                total = max(running, live_max or 0.0)
            try:
                await conn.execute(
                    "UPDATE daily_rollups SET rain_total = ? WHERE mac = ? AND day = ?",
                    (total, mac, day))
            except Exception:     # no rollups table on a box without insights
                pass
            await conn.commit()
        done += 1
    for key in [k for k in db._DAILY_ROLLUP_CACHE if k[0] == mac]:
        db._DAILY_ROLLUP_CACHE.pop(key, None)
    return done


def day_counter_from_intervals(rows: Iterable[dict], *,
                               ordered: bool = True,
                               spill_dir: str | None = None,
                               counter: "_DayCounter | None" = None) -> Iterator[dict]:
    """Rows in ascending time with `dailyrainin` synthesised from
    `intervalRainIn`. A running sum only means something in order:
    WeeWX rows arrive ORDER BY dateTime from a generator and are taken
    as they come, so a decade of archive is never held in memory; a CSV
    can come any way its author left it and is already in memory, so
    its door passes `ordered=False` and the rows are sorted here."""
    counter = counter if counter is not None else _DayCounter()
    if not ordered:
        # 2.5: the file door sorts on disk; the in-memory JSON door (whose
        # rows are already in memory, 16 MiB at most) sorts in memory.
        rows = (sorted_on_disk(rows, spill_dir) if spill_dir
                else sorted(rows, key=lambda r: int(r.get("dateutc") or 0)))
    for row in rows:
        counter.apply(row)
        yield row


async def run_import(mac: str, rows: Iterable[dict], *, kind: str,
                     dry_run: bool = False,
                     ordered: bool = True,
                     spill_dir: str | None = None) -> dict[str, Any]:
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
    spilled: str | None = None
    try:
        if not ordered and spill_dir:
            # Off the event loop: parsing and indexing the whole file happens
            # before the first row can come back (Greptile, PR #48).
            # Shielded: a cancelled import (a shutdown) cannot stop the
            # worker thread, so the thread is told to stop and its file is
            # removed whenever it does finish (CodeRabbit, PR #48).
            work = asyncio.ensure_future(asyncio.to_thread(
                spill_sorted, rows, spill_dir,
                cancelled=lambda: bool(JOB.get("cancel"))))
            try:
                spilled = await asyncio.shield(work)
            except asyncio.CancelledError:
                JOB["cancel"] = True
                work.add_done_callback(_remove_spill)
                raise
            if JOB.get("cancel"):
                JOB.update(state="cancelled", finished_ms=_now_ms())
                return status()
            rows, ordered = read_spilled(spilled), True
        counter = _DayCounter()
        for row in day_counter_from_intervals(rows, ordered=ordered, counter=counter,
                                              spill_dir=spill_dir):
            if JOB.get("cancel"):
                JOB.update(state="cancelled", finished_ms=_now_ms())
                return status()
            batch.append(row)
            JOB["read"] += 1
            if len(batch) >= BATCH_ROWS:
                if not dry_run:
                    JOB["inserted"] += await db.insert_observations(mac, batch, received=False)
                batch = []
                await asyncio.sleep(BATCH_PAUSE_S)
        if batch and not dry_run:
            JOB["inserted"] += await db.insert_observations(mac, batch, received=False)
        if not dry_run and counter.days:
            await rebuild_interval_days(mac, counter.days)
        # A cancel during the rebuild leaves the remaining days as the
        # running sum wrote them: report it as cancelled, not done
        # (Greptile, PR #52). Importing the same file again finishes them.
        if JOB.get("cancel"):
            JOB.update(state="cancelled", finished_ms=_now_ms())
            return status()
        JOB.update(state="done", finished_ms=_now_ms())
    except asyncio.CancelledError:
        # Not an Exception: without this the job read "running" until a
        # restart and every later import was refused as already running.
        JOB.update(state="cancelled", finished_ms=_now_ms())
        raise
    except SortSpaceError as e:
        log.warning("%s import stopped: %s", kind, e)
        JOB.update(state="error", error=str(e), finished_ms=_now_ms())
    except Exception as e:
        log.exception("%s import failed", kind)
        JOB.update(state="error", error=str(e), finished_ms=_now_ms())
    finally:
        if spilled is not None:
            try:
                os.unlink(spilled)
            except OSError:
                pass
        release()
    return status()
