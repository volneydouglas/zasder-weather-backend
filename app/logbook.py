"""The station logbook (2.7, docs/2.7/LOGBOOK.md): chart markers.

A jump on a chart usually has a reason a person knows and the data does
not: the gauge was cleaned, the Tempest moved to the roof, a battery was
replaced, a correction was set. The logbook keeps those reasons per
station so the apps can draw a marker where they happened and show the
note on a tap.

Two kinds of entry share one table:

- `user`: typed by the owner (or a write-share holder), backdatable,
  editable, deletable.
- `auto`: written by the server when it changes something that moves the
  data itself: a calibration save (one entry per corrected field, with the
  before and after) and a station location change. Never written for a
  save that changed nothing. Deletable, not editable: it records what the
  server did; a person's own words go in an entry of their own.

Units stay API-native (°F, inHg, %), like everything stored. An auto entry
also carries the change as numbers in `detail`, so an app showing metric
can say "-0.7 °C" instead of quoting the stored note (an OFFSET converts
without the 32: it is a difference, not a temperature).

The rows live in the database, so a database restore brings its own
logbook and deleting a station deletes its entries (db.delete_device).
Nothing here is cached in the process.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

log = logging.getLogger("zasder.logbook")

NOTE_MAX = 280
# Per station, user and auto together. Generous (a note a day for five
# years) and finite: a write-share holder must not be able to grow the
# table without bound, and every chart request reads a window of it.
ENTRIES_MAX = 2000
# Before this, a time is a bug rather than a backdate: 2000-01-01 UTC in
# ms. It is also what catches SECONDS sent where ms belong (1.76e9 s reads
# as January 1970 in ms).
TS_MIN_MS = 946_684_800_000
# Clock skew between a phone and the server, not a way to file the future.
TS_FUTURE_SLACK_MS = 24 * 3_600_000

SOURCES = ("user", "auto")
KINDS = ("calibration", "location")


def known_fields() -> frozenset[str]:
    """API field names an entry may point at: every stored reading plus
    every correctable one (rainratein is correctable without being a
    stored column)."""
    from . import calibration, db
    return frozenset(db.QUERYABLE_FIELDS) | frozenset(calibration.CALIBRATABLE)


def clean_note(raw: Any) -> str:
    """The note as stored: one line, control characters gone, trimmed.
    Raises ValueError for a non-string, nothing left, or more than
    NOTE_MAX characters (refused, not truncated: a note cut short says
    something its author did not)."""
    if not isinstance(raw, str):
        raise ValueError("note must be text")
    text = " ".join(raw.split())
    text = "".join(c for c in text if ord(c) >= 32 and ord(c) != 127).strip()
    if not text:
        raise ValueError("note must not be empty")
    if len(text) > NOTE_MAX:
        raise ValueError(f"note must be at most {NOTE_MAX} characters")
    return text


def check_ts(ts_ms: Any, now_ms: int) -> int:
    if isinstance(ts_ms, bool) or not isinstance(ts_ms, int):
        raise ValueError("ts_ms must be an integer (milliseconds)")
    if ts_ms < TS_MIN_MS:
        raise ValueError("ts_ms is before 2000; it must be in milliseconds")
    if ts_ms > now_ms + TS_FUTURE_SLACK_MS:
        raise ValueError("ts_ms is in the future")
    return ts_ms


def check_field(field: Any) -> str | None:
    if field is None or field == "":
        return None
    if not isinstance(field, str) or field not in known_fields():
        raise ValueError(f"unknown field {field!r}")
    return field


def _now_ms() -> int:
    return int(time.time() * 1000)


def row_out(r: Any) -> dict[str, Any]:
    detail = None
    if r["detail_json"]:
        try:
            detail = json.loads(r["detail_json"])
        except ValueError:
            detail = None
    return {"id": int(r["id"]), "mac": r["mac"], "ts_ms": int(r["ts_ms"]),
            "note": r["note"], "field": r["field"], "source": r["source"],
            "kind": r["kind"], "detail": detail,
            "created_ms": int(r["created_ms"]),
            "updated_ms": int(r["updated_ms"])}


_COLS = ("id, mac, ts_ms, note, field, source, kind, detail_json, "
         "created_ms, updated_ms")


# ── storage ──────────────────────────────────────────────────────────────

async def _count(conn, mac: str) -> int:
    row = await (await conn.execute(
        "SELECT COUNT(*) FROM logbook WHERE mac = ?", (mac,))).fetchone()
    return int(row[0])


async def device_known(mac: str) -> bool:
    from . import db
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM devices WHERE mac = ?", (mac,))).fetchone()
    return row is not None


async def list_entries(mac: str, start_ms: int | None = None,
                       end_ms: int | None = None) -> list[dict[str, Any]]:
    """A station's entries, oldest first, within [start_ms, end_ms] when
    given (both ends inclusive)."""
    from . import db
    sql = f"SELECT {_COLS} FROM logbook WHERE mac = ?"
    args: list[Any] = [mac]
    if start_ms is not None:
        sql += " AND ts_ms >= ?"
        args.append(int(start_ms))
    if end_ms is not None:
        sql += " AND ts_ms <= ?"
        args.append(int(end_ms))
    sql += " ORDER BY ts_ms, id"
    async with db.connect() as conn:
        rows = await (await conn.execute(sql, args)).fetchall()
    return [row_out(r) for r in rows]


class Full(Exception):
    """The station already holds ENTRIES_MAX entries."""


async def create(mac: str, note: str, ts_ms: int, field: str | None,
                 now_ms: int | None = None) -> dict[str, Any]:
    """A user entry. The caller has validated note, time and field."""
    from . import db
    now = now_ms if now_ms is not None else _now_ms()
    async with db.connect() as conn:
        # The count and the insert under one writer (CodeRabbit, PR #97):
        # counted outside a write transaction, two creates at
        # ENTRIES_MAX - 1 both saw room and both inserted. BEGIN IMMEDIATE
        # takes the writer before the count, so the second create waits
        # (busy_timeout) and then counts the first one's row.
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if await _count(conn, mac) >= ENTRIES_MAX:
                raise Full()
            cur = await conn.execute(
                "INSERT INTO logbook (mac, ts_ms, note, field, source, kind, "
                "detail_json, created_ms, updated_ms) "
                "VALUES (?, ?, ?, ?, 'user', NULL, NULL, ?, ?)",
                (mac, ts_ms, note, field, now, now))
            new_id = cur.lastrowid
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        row = await (await conn.execute(
            f"SELECT {_COLS} FROM logbook WHERE id = ?", (new_id,))).fetchone()
    return row_out(row)


async def get(mac: str, entry_id: int) -> dict[str, Any] | None:
    from . import db
    async with db.connect() as conn:
        row = await (await conn.execute(
            f"SELECT {_COLS} FROM logbook WHERE id = ? AND mac = ?",
            (entry_id, mac))).fetchone()
    return row_out(row) if row else None


async def update(mac: str, entry_id: int, changes: dict[str, Any],
                 now_ms: int | None = None) -> dict[str, Any] | None:
    """Apply validated `changes` (any of note, ts_ms, field) to a USER
    entry. None when the entry is not this station's."""
    from . import db
    # Whitelisted column names, never interpolated from the request: the
    # keys are checked against this set and anything else is refused.
    allowed = {"note", "ts_ms", "field"}
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"cannot change {sorted(bad)}")
    now = now_ms if now_ms is not None else _now_ms()
    sets = ", ".join(f"{k} = ?" for k in sorted(changes))
    args = [changes[k] for k in sorted(changes)]
    async with db.connect() as conn:
        cur = await conn.execute(
            f"UPDATE logbook SET {sets}, updated_ms = ? "
            "WHERE id = ? AND mac = ?", (*args, now, entry_id, mac))
        await conn.commit()
        if not cur.rowcount:
            return None
        row = await (await conn.execute(
            f"SELECT {_COLS} FROM logbook WHERE id = ?", (entry_id,))).fetchone()
    return row_out(row)


async def delete(mac: str, entry_id: int) -> bool:
    from . import db
    async with db.connect() as conn:
        cur = await conn.execute(
            "DELETE FROM logbook WHERE id = ? AND mac = ?", (entry_id, mac))
        await conn.commit()
        return bool(cur.rowcount)


async def insert_auto(conn, mac: str, entries: list[dict[str, Any]],
                      now_ms: int) -> int:
    """Write automatic entries on the CALLER's open connection, inside the
    caller's transaction, so the change and its record commit together.
    Each entry: note, kind, field (or None), detail (or None). Returns how
    many were written; past ENTRIES_MAX the rest are skipped and logged
    (the change itself still happens).

    The caller must already have WRITTEN in that transaction (both callers
    update their row first), so the writer is held and the count below
    cannot race another insert, the way `create` holds it with BEGIN
    IMMEDIATE. Outside a transaction the count and the inserts would be
    separate autocommits again, so that is refused (a raise, not an
    assert: asserts vanish under python -O)."""
    if not entries:
        return 0
    if not conn.in_transaction:
        raise RuntimeError("logbook.insert_auto needs the caller's open "
                           "write transaction")
    room = ENTRIES_MAX - await _count(conn, mac)
    if room < len(entries):
        log.warning("logbook for %s is full (%d entries); %d automatic "
                    "entries not written", mac, ENTRIES_MAX,
                    len(entries) - max(room, 0))
    written = 0
    for e in entries[:max(room, 0)]:
        await conn.execute(
            "INSERT INTO logbook (mac, ts_ms, note, field, source, kind, "
            "detail_json, created_ms, updated_ms) "
            "VALUES (?, ?, ?, ?, 'auto', ?, ?, ?, ?)",
            (mac, now_ms, e["note"], e.get("field"), e["kind"],
             json.dumps(e["detail"]) if e.get("detail") is not None else None,
             now_ms, now_ms))
        written += 1
    return written


# ── automatic entries: the words ─────────────────────────────────────────

# Every correctable reading, named the way the apps name it, with the unit
# its stored value (and so its offset) is in. A scale has no unit.
_CAL_LABELS: dict[str, tuple[str, str | None]] = {
    "tempf": ("Temperature", "°F"),
    "tempinf": ("Indoor temperature", "°F"),
    "humidity": ("Humidity", "%"),
    "humidityin": ("Indoor humidity", "%"),
    "baromrelin": ("Relative pressure", "inHg"),
    "baromabsin": ("Absolute pressure", "inHg"),
    "windspeedmph": ("Wind speed", None),
    "windgustmph": ("Wind gust", None),
    "solarradiation": ("Solar radiation", None),
    "uv": ("UV index", None),
    "hourlyrainin": ("Hourly rain", None),
    "rainratein": ("Rain rate", None),
    **{f"soilhum{i}": (f"Soil moisture {i}", "%") for i in range(1, 9)},
    **{f"soiltemp{i}f": (f"Soil temperature {i}", "°F") for i in range(1, 9)},
}


def _num(v: float) -> str:
    """A value as a person writes it: no trailing zeros, no float noise,
    a minus sign for negatives and no plus for positives."""
    r = round(float(v), 3)
    if r == 0:
        r = 0.0                       # -0.0 prints as "-0"
    return f"{r:g}"


def _with_unit(v: float, unit: str | None) -> str:
    if not unit:
        return _num(v)
    return f"{_num(v)}{unit}" if unit == "%" else f"{_num(v)} {unit}"


def calibration_entries(old: dict[str, float],
                        new: dict[str, float]) -> list[dict[str, Any]]:
    """One entry per field whose correction changed, in field order. A
    field absent from a table has the neutral correction (offset 0, scale
    1), which is what an absent correction does at ingest, so "from 0 to
    -1.2 °F" is a correction added and "from -1.2 °F to 0" one removed.
    Identical tables give no entries: a no-op save writes nothing."""
    from . import calibration
    out = []
    for field in sorted(set(old) | set(new)):
        kind = calibration.CALIBRATABLE.get(field)
        if kind is None:
            continue
        neutral = 1.0 if kind == "scale" else 0.0
        a = float(old.get(field, neutral))
        b = float(new.get(field, neutral))
        if not (math.isfinite(a) and math.isfinite(b)) or abs(a - b) < 1e-9:
            continue
        label, unit = _CAL_LABELS.get(field, (field, None))
        unit = unit if kind == "offset" else None
        word = "offset" if kind == "offset" else "scale"
        out.append({
            "kind": "calibration", "field": field,
            # The unit once, at the end: "from 0 to -1.2 °F".
            "note": (f"{label} {word} changed from {_num(a)} "
                     f"to {_with_unit(b, unit)}"),
            "detail": {"field": field, "correction": kind,
                       "from": a, "to": b, "unit": unit},
        })
    return out


LOCATION_CHANGED = "Station location changed"
LOCATION_SET = "Station location set"
# About 10 cm at the equator: a re-save of the same pin is not a move.
_COORD_EPS = 1e-6


def location_entry(old: tuple[float, float] | None,
                   new: tuple[float, float]) -> dict[str, Any] | None:
    """The entry for a location save, or None when the point did not move.
    Only the point counts: renaming the place is not a change to the data.
    No coordinates go in the entry; it may be shown on the public page."""
    if old is not None and (abs(old[0] - new[0]) < _COORD_EPS
                            and abs(old[1] - new[1]) < _COORD_EPS):
        return None
    return {"kind": "location", "field": None, "detail": None,
            "note": LOCATION_CHANGED if old is not None else LOCATION_SET}


# ── CSV ──────────────────────────────────────────────────────────────────

CSV_HEADER = ("timestamp_utc", "ts_ms", "source", "kind", "field", "note")


def _cell(text: str | None) -> str:
    """A spreadsheet must not run a note as a formula (=, +, -, @, tab,
    CR first): such a note is written with a leading apostrophe."""
    if not text:
        return ""
    return "'" + text if text[0] in "=+-@\t\r" else text


def csv_text(entries: list[dict[str, Any]]) -> str:
    import csv
    import io
    from datetime import datetime, timezone
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for e in entries:
        ts = datetime.fromtimestamp(e["ts_ms"] / 1000, tz=timezone.utc
                                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
        w.writerow([ts, e["ts_ms"], e["source"], e["kind"] or "",
                    e["field"] or "", _cell(e["note"])])
    return buf.getvalue()


# ── the public page (opt-in, off by default) ─────────────────────────────

PUBLIC_KV = "public_dashboard.logbook"


async def public_enabled() -> bool:
    from . import db
    return await db.get_kv(PUBLIC_KV) == "1"
