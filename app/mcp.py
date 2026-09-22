"""A read-only MCP server for the station's own data (2.1).

The Model Context Protocol lets an assistant the user already pays for
(Claude Desktop, Claude Code, any MCP client that sends a bearer header)
call tools on this backend and analyse the weather record itself. No
provider key is stored here and nothing is forwarded anywhere: the
client's subscription does the thinking, this server only answers
questions about the database it already holds.

Transport: MCP Streamable HTTP (spec 2025-03-26 / 2025-06-18), one
endpoint, `POST /mcp`, JSON-RPC 2.0, one message per request, JSON
responses only (no SSE streams: every tool here answers in one shot, so
there is nothing to stream). `GET /mcp` and `DELETE /mcp` answer 405 as
the spec allows for a server that offers no server-initiated stream and
no explicit session termination. Stateless: no `Mcp-Session-Id` is
issued, so clients never have to carry one.

Auth: the same `Authorization: Bearer <API_TOKEN>` gate as every other
`/api/*` route (require_token in main.py), so a guest share token reads
here exactly what it reads elsewhere. The token never travels in the
URL — the MCP authorization spec forbids query-string tokens and the
codebase's one `?token=` exception exists only because Ecowitt firmware
cannot send headers. A 401 carries `WWW-Authenticate: Bearer` as the
spec requires.

Every value is in STORAGE units (°F, mph, inHg, inches, W/m², UV
index) and every tool description says so; a sensor a station lacks is
null, never 0 (the `?? 0` family is a shipped bug, five times over).
Rows and windows are capped so one question cannot read the whole
archive. Hand-rolled JSON-RPC: the surface is five methods, which is
not worth a dependency.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Callable

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

log = logging.getLogger("api")

router = APIRouter()

SERVER_NAME = "zasder-weather"
# Versions this server speaks. The client's initialize request names the
# one it wants; an unknown one is answered with our latest and the client
# decides (spec: version negotiation). Newest first.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
# Spec: a request without MCP-Protocol-Version is assumed to be this.
_DEFAULT_PROTOCOL_VERSION = "2025-03-26"

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Caps. A question about "last month" is a few hundred rows; the whole
# archive is millions. These bound what one call may read.
HISTORY_MAX_HOURS = 24 * 31 + 1          # one calendar month, DST-safe
HISTORY_DEFAULT_ROWS = 500
HISTORY_MAX_ROWS = 2000
DAILY_MAX_DAYS = 366
STORIES_MAX = 50
REPORTS_MAX = 100
STORMS_MAX = 50

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$|^[0-9A-Fa-f]{12}$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ToolError(Exception):
    """A tool ran and has something honest to say instead of a result:
    unknown station, insights off, bad window. Reported in the tool
    result with isError=true (spec: 'tool execution errors'), never as a
    protocol error and never as a 500."""


# ───────────────────────── argument helpers ─────────────────────────

def _norm_mac(raw: Any) -> str:
    if not isinstance(raw, str) or not _MAC_RE.match(raw.strip()):
        raise ToolError("mac must be a station MAC like AA:BB:CC:DD:EE:FF "
                        "— call list_stations for the ones this server has")
    from .ingest import _format_mac
    return _format_mac(raw.strip())


async def _known_mac(raw: Any) -> str:
    from . import db
    mac = _norm_mac(raw)
    known = {d["mac"] for d in await db.list_devices()}
    if mac not in known:
        raise ToolError(f"unknown station {mac} — call list_stations")
    return mac


def _epoch_ms(value: Any, what: str) -> int:
    """ISO 8601 (with or without offset; naive = UTC) or epoch ms/s."""
    if isinstance(value, bool):
        raise ToolError(f"{what} must be an ISO 8601 time or epoch ms")
    if isinstance(value, (int, float)):
        v = float(value)
        if v < 1e11:                      # seconds, not ms
            v *= 1000.0
        return int(v)
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d{10,13}", s):
            return _epoch_ms(int(s), what)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise ToolError(f"{what} must be an ISO 8601 time like "
                            "2026-09-01T00:00:00-07:00 or epoch ms") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    raise ToolError(f"{what} must be an ISO 8601 time or epoch ms")


def _iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _day(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _DAY_RE.match(value):
        raise ToolError(f"{what} must be a local day like 2026-09-01")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ToolError(f"{what} is not a real calendar day") from None
    return value


def _int_arg(args: dict, name: str, default: int, lo: int, hi: int) -> int:
    v = args.get(name, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        raise ToolError(f"{name} must be an integer between {lo} and {hi}")
    return max(lo, min(hi, int(v)))


def _require_insights() -> None:
    # Read at call time, not import time: the settings object is rebuilt
    # when config reloads (the test suite does so per test) and a module-
    # level binding would keep answering from the stale one.
    from . import config
    if not config.settings.insights:
        raise ToolError("insights are disabled on this server (INSIGHTS=0); "
                        "records, stories, daily summaries and NOAA reports "
                        "need the rollup tables")


def _finite(v: Any) -> Any:
    """JSON has no NaN/inf; a stray one would break the client's parse."""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return _finite(obj)


# ───────────────────────── the tools ─────────────────────────
#
# Each entry: the MCP tool definition the client sees, plus the coroutine
# that answers it. Descriptions name the units because the model reads
# them and nothing else about the schema.

_UNITS = ("Units are the station's storage units: temperatures °F, wind "
          "mph, pressure inHg, rain inches, solar W/m², UV index. A field "
          "the station does not measure is null.")


async def _list_stations(args: dict, role: str) -> dict:
    """Stations with their place. A guest session sees exactly what a
    guest sees on /api/devices (2.1 review, T2): the location label
    dropped and the coordinates rounded to town scale, through the same
    `_strip_device_pii` the REST route uses, so the two surfaces cannot
    drift apart."""
    from . import db, main
    devices = await db.list_devices()
    if role != "owner":
        devices = main._strip_device_pii(devices)
    out = []
    for d in devices:
        last = d.get("lastData") or {}
        # `lastData` is the flat reading; the poster's own payload lives on
        # the newest observation row. The owner gets it (round-three
        # review BE-F12: the field was always null), a guest never does.
        source = None
        if role == "owner" and d.get("mac"):
            try:
                source = (await db.latest_observation(str(d["mac"])) or {}).get("_source")
            except Exception:                        # noqa: BLE001
                source = None
        sensors = sorted(k for k, v in last.items()
                         if v is not None and not k.startswith("_")
                         and k not in ("dateutc", "name"))
        coords = ((d.get("info") or {}).get("coords") or {}).get("coords") or {}
        try:
            place = {"lat": float(coords["lat"]), "lon": float(coords["lon"])}
        except (KeyError, TypeError, ValueError):
            place = None
        out.append({
            "mac": d.get("mac"),
            "name": d.get("name") or d.get("mac"),
            "location": d.get("location"),
            "coords": place,
            "last_seen_ms": d.get("lastSeen"),
            "last_seen_iso": _iso(d.get("lastSeen")) if d.get("lastSeen") else None,
            # The poster's own payload (its name and exact coordinates for a
            # cloud-polled station): the owner's, like /current's strip.
            "source": source,
            "sensors": sensors,
        })
    return {"stations": out, "count": len(out)}


async def _current_conditions(args: dict, role: str) -> dict:
    from . import db, main
    mac = await _known_mac(args.get("mac"))
    obs = await db.latest_observation(mac)
    if not obs:
        raise ToolError(f"no readings stored for {mac}")
    await main._fill_rain_periods(mac, obs)
    # Same strip as /api/devices/{mac}/current for a guest: the source
    # blob names the station and its exact coordinates.
    # Anything but the owner is stripped: a third role fails closed, as in
    # _list_stations (round-three review BE-F12).
    obs = dict(obs) if role == "owner" else main._strip_observation_pii(obs)
    ts = obs.get("dateutc")
    return {"mac": mac, "observed_ms": ts, "observed_iso": _iso(ts) if ts else None,
            "reading": obs}


async def _history(args: dict, role: str) -> dict:
    from . import db
    mac = await _known_mac(args.get("mac"))
    start = _epoch_ms(args.get("start"), "start")
    end = _epoch_ms(args.get("end"), "end") if args.get("end") is not None \
        else int(time.time() * 1000)
    if end <= start:
        raise ToolError("end must be after start")
    if end - start > HISTORY_MAX_HOURS * 3600 * 1000:
        raise ToolError(f"the window may span at most {HISTORY_MAX_HOURS // 24} "
                        "days — ask in monthly pieces, or use daily_summary")
    limit = _int_arg(args, "limit", HISTORY_DEFAULT_ROWS, 1, HISTORY_MAX_ROWS)
    fields = args.get("fields")
    if fields is not None:
        if (not isinstance(fields, list)
                or not all(isinstance(f, str) for f in fields)):
            raise ToolError("fields must be a list of field names")
        keep = set(fields) | {"dateutc"}
    else:
        keep = None
    rows = await db.history(mac, start, end, limit=limit)
    out = []
    for r in rows:
        row = {k: v for k, v in r.items() if not k.startswith("_")
               and (keep is None or k in keep)}
        if "dateutc" in row:
            row["iso"] = _iso(row["dateutc"])
        out.append(row)
    return {"mac": mac, "start_ms": start, "end_ms": end,
            "bucketed": (end - start) > 6 * 3600 * 1000,
            "count": len(out), "rows": out}


_DAILY_COLS = (
    "tempf_min", "tempf_max", "humidity_min", "humidity_max",
    "windspeedmph_max", "windgustmph_max", "baromrelin_min", "baromrelin_max",
    "dew_point_min", "dew_point_max", "feels_like_min", "feels_like_max",
    "uv_max", "solarradiation_max", "rain_total", "lightning_max",
    # 2.2: the air-monitor pair and indoor temperature.
    "pm25_min", "pm25_max", "co2_min", "co2_max", "tempinf_min", "tempinf_max",
)
# Means from the day's sum/n pairs (2.2); reported as <stem>_mean.
_DAILY_MEANS = ("humidity", "windspeedmph", "baromrelin", "pm25", "co2")


async def _daily_summary(args: dict, role: str) -> dict:
    from . import climate, insights
    _require_insights()
    mac = await _known_mac(args.get("mac"))
    first = _day(args.get("start_day"), "start_day")
    last = _day(args.get("end_day"), "end_day") if args.get("end_day") else first
    if last < first:
        raise ToolError("end_day must not be before start_day")
    d0 = datetime.strptime(first, "%Y-%m-%d")
    d1 = datetime.strptime(last, "%Y-%m-%d")
    if (d1 - d0).days + 1 > DAILY_MAX_DAYS:
        raise ToolError(f"at most {DAILY_MAX_DAYS} days per call")
    rows = await climate._rollup_rows(mac, first, last)
    days = []
    for r in rows:
        keys = r.keys()
        d: dict[str, Any] = {"day": r["day"]}
        for c in _DAILY_COLS:
            d[c] = r[c] if c in keys else None
        n = r["tempf_n"] if "tempf_n" in keys else None
        total = r["tempf_sum"] if "tempf_sum" in keys else None
        d["tempf_mean"] = (round(total / n, 2)
                           if n and total is not None else None)
        for stem in _DAILY_MEANS:
            d[f"{stem}_mean"] = insights.rollup_mean(r, stem)
        days.append(d)
    return {"mac": mac, "start_day": first, "end_day": last,
            "count": len(days), "days": days,
            "note": "rain_total is the day's gauge total; every *_mean is the "
                    "mean of that day's readings, not (min+max)/2; pm25 in "
                    "µg/m³ and co2 in ppm are null on a weather station"}


async def _records(args: dict, role: str) -> dict:
    from . import main
    mac = await _known_mac(args.get("mac"))
    return {"mac": mac, "records": await main._cached_records(mac)}


async def _insights(args: dict, role: str) -> dict:
    from . import insights
    _require_insights()
    mac = await _known_mac(args.get("mac"))
    return {"mac": mac, "insights": await insights.assemble(mac)}


async def _stories(args: dict, role: str) -> dict:
    from . import stories
    _require_insights()
    mac = await _known_mac(args.get("mac"))
    limit = _int_arg(args, "limit", 12, 1, STORIES_MAX)
    out = await stories.top_stories(mac, limit=limit, min_score=0.0)
    return {"mac": mac, **out}


def _changes_max() -> int:
    from . import changes as ch
    return ch.WINDOW_HOURS_MAX


async def _changes(args: dict, role: str) -> dict:
    """The weather-change timeline (2.4). Not an insights tool: it reads
    the station's raw observations, so it answers on a server with
    insights switched off and on a station with no history to rank
    against."""
    from . import changes as ch
    mac = await _known_mac(args.get("mac"))
    hours = _int_arg(args, "hours", ch.WINDOW_HOURS_DEFAULT, 1,
                     ch.WINDOW_HOURS_MAX)
    out = await ch.assemble(mac, hours)
    for c in out["changes"]:
        c["at_iso"] = _iso(c.get("at_ms"))
    return out


async def _reports(args: dict, role: str) -> dict:
    from . import db, reports as rp
    kind = args.get("kind")
    if kind is not None and kind not in rp.KINDS:
        raise ToolError(f"kind must be one of {', '.join(rp.KINDS)}")
    limit = _int_arg(args, "limit", 30, 1, REPORTS_MAX)
    rows = await db.list_reports(kind=kind, limit=limit)
    for r in rows:
        r["ts_iso"] = _iso(r.get("ts_ms"))
    return {"reports": rows, "count": len(rows), "kinds": list(rp.KINDS)}


async def _report(args: dict, role: str) -> dict:
    from . import db
    rid = args.get("id")
    if isinstance(rid, bool) or not isinstance(rid, (int, float)) or int(rid) != rid:
        raise ToolError("id must be a report id from the reports tool")
    row = await db.get_report(int(rid))
    if row is None:
        raise ToolError(f"no report with id {int(rid)}")
    return {"report": row}


async def _storm_history(args: dict, role: str) -> dict:
    from . import db
    mac = await _known_mac(args.get("mac"))
    limit = _int_arg(args, "limit", 10, 1, STORMS_MAX)
    storms = await db.list_storms(mac, limit)
    for s in storms:
        s["started_iso"] = _iso(s.get("started_ms"))
        s["ended_iso"] = _iso(s.get("ended_ms"))
    return {"mac": mac, "count": len(storms), "storms": storms}


async def _noaa_report(args: dict, role: str) -> dict:
    from . import climate, db
    _require_insights()
    mac = await _known_mac(args.get("mac"))
    year = _int_arg(args, "year", 0, 1970, 2100)
    if year == 0 or "year" not in args:
        raise ToolError("year is required")
    month = args.get("month")
    if month is not None:
        month = _int_arg(args, "month", 1, 1, 12)
    dev = next((d for d in await db.list_devices() if d["mac"] == mac), None)
    name = (dev or {}).get("name") or mac
    if month is not None:
        text = await climate.noaa_month_report(mac, name, year, month)
    else:
        text = await climate.noaa_year_report(mac, name, year)
    return {"mac": mac, "year": year, "month": month, "text": text}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


_MAC_PROP = {"type": "string",
             "description": "Station MAC, e.g. AA:BB:CC:DD:EE:FF (from list_stations)"}

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
              "idempotentHint": True, "openWorldHint": False}

Handler = Callable[[dict], Awaitable[dict]]

TOOLS: list[tuple[dict[str, Any], Handler]] = [
    ({
        "name": "list_stations",
        "title": "List stations",
        "description": "Every weather station this server stores, with its "
                       "MAC (the key every other tool takes), display name, "
                       "when it last reported, and which sensors it has.",
        "inputSchema": _schema({}, []),
        "annotations": _READ_ONLY,
    }, _list_stations),
    ({
        "name": "current_conditions",
        "title": "Current conditions",
        "description": "The latest reading for one station, every field the "
                       "station posts plus rain period totals. yearlyrainin "
                       "is always year to date; when totalrainin is present "
                       "it is the sensor's lifetime counter, not this year. "
                       + _UNITS,
        "inputSchema": _schema({"mac": _MAC_PROP}, ["mac"]),
        "annotations": _READ_ONLY,
    }, _current_conditions),
    ({
        "name": "history",
        "title": "Reading history",
        "description": "Readings for one station over a window of at most "
                       "31 days. Windows over six hours come back bucketed "
                       "(averaged) so long spans stay small; use "
                       "daily_summary for per-day extremes over longer "
                       "spans. Times are epoch ms plus ISO 8601. " + _UNITS,
        "inputSchema": _schema({
            "mac": _MAC_PROP,
            "start": {"type": ["string", "number"],
                      "description": "Window start: ISO 8601 or epoch ms"},
            "end": {"type": ["string", "number"],
                    "description": "Window end: ISO 8601 or epoch ms; default now"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "Only these fields (dateutc always kept), "
                                      "e.g. [\"tempf\", \"windgustmph\"]"},
            "limit": {"type": "integer", "minimum": 1, "maximum": HISTORY_MAX_ROWS,
                      "description": f"Row cap, default {HISTORY_DEFAULT_ROWS}"},
        }, ["mac", "start"]),
        "annotations": _READ_ONLY,
    }, _history),
    ({
        "name": "daily_summary",
        "title": "Daily summary",
        "description": "Per-day extremes for one station from the rollup "
                       "ledger: temperature min/max/mean, humidity, gust, "
                       "pressure, dew point, feels-like, UV, solar, rain "
                       "total, peak lightning strikes per hour. Up to 366 "
                       "days per call. Days are the station's local calendar "
                       "days. " + _UNITS,
        "inputSchema": _schema({
            "mac": _MAC_PROP,
            "start_day": {"type": "string", "description": "First day, YYYY-MM-DD"},
            "end_day": {"type": "string",
                        "description": "Last day, YYYY-MM-DD; default = start_day"},
        }, ["mac", "start_day"]),
        "annotations": _READ_ONLY,
    }, _daily_summary),
    ({
        "name": "records",
        "title": "Records",
        "description": "All-time, yearly, monthly, weekly and today's highs "
                       "and lows per measurement for one station, with the "
                       "local time each was set. " + _UNITS,
        "inputSchema": _schema({"mac": _MAC_PROP}, ["mac"]),
        "annotations": _READ_ONLY,
    }, _records),
    ({
        "name": "insights",
        "title": "Insights",
        "description": "The station statistics the app's Insights page shows: "
                       "heat and cold ledgers, rain seasons and dry streaks, "
                       "normals and anomalies, the month-by-hour diurnal "
                       "grid, coverage per year. " + _UNITS,
        "inputSchema": _schema({"mac": _MAC_PROP}, ["mac"]),
        "annotations": _READ_ONLY,
    }, _insights),
    ({
        "name": "stories",
        "title": "Story cards",
        "description": "Server-written, ranked weather stories about one "
                       "station (records broken, streaks, comparisons with "
                       "past years), each with its rendered text and the "
                       "numbers behind it. " + _UNITS,
        "inputSchema": _schema({
            "mac": _MAC_PROP,
            "limit": {"type": "integer", "minimum": 1, "maximum": STORIES_MAX,
                      "description": "How many, default 12"},
        }, ["mac"]),
        "annotations": _READ_ONLY,
    }, _stories),
    ({
        "name": "weather_changes",
        "title": "What changed",
        "description": "When the weather TURNED at one station over the "
                       "last day or three, from its own readings: rain "
                       "starting and stopping, wind shifts, the barometer "
                       "changing direction, the window's high and low, "
                       "clearing and clouding over. Each entry carries the "
                       "instant it happened and, where there is a number, "
                       "the value and the unit it is in. " + _UNITS,
        "inputSchema": _schema({
            "mac": _MAC_PROP,
            "hours": {"type": "integer", "minimum": 1,
                      "maximum": _changes_max(),
                      "description": "Window, default 24"},
        }, ["mac"]),
        "annotations": _READ_ONLY,
    }, _changes),
    ({
        "name": "reports",
        "title": "Stored reports",
        "description": "Stored morning reports and storm summaries, newest "
                       "first, without their payloads; read one with the "
                       "report tool.",
        "inputSchema": _schema({
            "kind": {"type": "string", "enum": ["morning", "storm"],
                     "description": "Only this kind; omit for all"},
            "limit": {"type": "integer", "minimum": 1, "maximum": REPORTS_MAX,
                      "description": "How many, default 30"},
        }, []),
        "annotations": _READ_ONLY,
    }, _reports),
    ({
        "name": "report",
        "title": "One report",
        "description": "One stored report with its full payload: yesterday's "
                       "numbers per station, overnight alerts and today's "
                       "forecast for a morning report; the storm's totals "
                       "and peaks for a storm summary. " + _UNITS,
        "inputSchema": _schema({
            "id": {"type": "integer", "description": "Report id from the reports tool"},
        }, ["id"]),
        "annotations": _READ_ONLY,
    }, _report),
    ({
        "name": "storm_history",
        "title": "Storm history",
        "description": "Closed storm episodes for one station, newest first: "
                       "start and end, rain total, peak rate, peak gust, "
                       "temperature range. " + _UNITS,
        "inputSchema": _schema({
            "mac": _MAC_PROP,
            "limit": {"type": "integer", "minimum": 1, "maximum": STORMS_MAX,
                      "description": "How many, default 10"},
        }, ["mac"]),
        "annotations": _READ_ONLY,
    }, _storm_history),
    ({
        "name": "noaa_report",
        "title": "NOAA-style climate report",
        "description": "The classic fixed-width climatological summary as "
                       "plain text: with a month, one row per day; without, "
                       "one row per month of the year. " + _UNITS,
        "inputSchema": _schema({
            "mac": _MAC_PROP,
            "year": {"type": "integer", "minimum": 1970, "maximum": 2100},
            "month": {"type": "integer", "minimum": 1, "maximum": 12,
                      "description": "Omit for the year report"},
        }, ["mac", "year"]),
        "annotations": _READ_ONLY,
    }, _noaa_report),
]

_BY_NAME: dict[str, tuple[dict[str, Any], Handler]] = {t[0]["name"]: t for t in TOOLS}


def tool_definitions() -> list[dict[str, Any]]:
    return [t[0] for t in TOOLS]


# ───────────────────────── schema check ─────────────────────────
#
# The spec puts input validation on the server. A small checker for the
# subset of JSON Schema the definitions above use: object with typed
# properties, required, enum, min/max, arrays of strings. Anything it
# rejects is a protocol error (-32602); a value that passes shape but
# fails meaning (unknown station) is a tool error.

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_arguments(schema: dict[str, Any], args: Any) -> list[str]:
    """Problems with `args` against `schema`; empty when it conforms."""
    problems: list[str] = []
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args or args[name] is None:
            problems.append(f"{name} is required")
    for name, value in args.items():
        if name not in props:
            problems.append(f"unknown argument {name}")
            continue
        if value is None:
            continue
        spec = props[name]
        types = spec.get("type")
        types = [types] if isinstance(types, str) else list(types or [])
        if types and not any(_TYPE_CHECKS.get(t, lambda v: True)(value)
                             for t in types):
            problems.append(f"{name} must be {' or '.join(types)}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{name} must be one of {', '.join(map(str, spec['enum']))}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                problems.append(f"{name} must be at least {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                problems.append(f"{name} must be at most {spec['maximum']}")
        if isinstance(value, list) and spec.get("items", {}).get("type") == "string":
            if not all(isinstance(v, str) for v in value):
                problems.append(f"{name} must be a list of strings")
    return problems


# ───────────────────────── JSON-RPC ─────────────────────────

def _error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _result(rid: Any, result: dict[str, Any]) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


async def call_tool(name: str, arguments: Any, role: str) -> dict[str, Any]:
    """Run one tool as `role` ('owner' | 'guest', from the gate; no
    default on purpose: a privilege parameter fails closed, round-two
    review BE-N8). Returns
    the MCP CallToolResult (content + structuredContent, or isError).
    Raises ValueError for an unknown tool or bad argument shape — the
    caller turns that into -32602."""
    entry = _BY_NAME.get(name)
    if entry is None:
        raise ValueError(f"Unknown tool: {name}")
    definition, handler = entry
    args = {} if arguments is None else arguments
    problems = validate_arguments(definition["inputSchema"], args)
    if problems:
        raise ValueError("Invalid arguments: " + "; ".join(problems))
    try:
        payload = _clean(await handler(args, role))
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}],
                "isError": True}
    except HTTPException as exc:
        return {"content": [{"type": "text", "text": str(exc.detail)}],
                "isError": True}
    except Exception:
        log.exception("mcp tool %s failed", name)
        return {"content": [{"type": "text",
                             "text": f"{name} failed on the server; see its log"}],
                "isError": True}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload, "isError": False}


async def handle_message(msg: Any, role: str) -> dict[str, Any] | None:
    """One JSON-RPC message → a response dict, or None for a notification
    or a client response (the transport answers those with 202). `role`
    is what the gate decided the caller is; every tool receives it."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST,
                      "Invalid Request: expected one JSON-RPC 2.0 message")
    method = msg.get("method")
    rid = msg.get("id")
    if method is None:
        # A response to a server request: this server never sends any,
        # so there is nothing to match it to. Accept and drop.
        return None
    if not isinstance(method, str):
        return _error(rid, INVALID_REQUEST, "Invalid Request: method must be a string")
    params = msg.get("params")
    if params is not None and not isinstance(params, dict):
        return _error(rid, INVALID_PARAMS, "params must be an object")
    params = params or {}
    if "id" not in msg:                     # notification
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME,
                           "title": "Zasder Weather",
                           "version": _version()},
            "instructions": (
                "Read-only access to this weather station's own record. "
                "Start with list_stations; every other tool takes a station "
                "MAC from it. " + _UNITS),
        })
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": tool_definitions()})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(rid, INVALID_PARAMS, "params.name must be a string")
        try:
            return _result(rid, await call_tool(name, params.get("arguments"), role))
        except ValueError as exc:
            return _error(rid, INVALID_PARAMS, str(exc))
    # resources/*, prompts/*, completion/*, logging/*: not offered, and the
    # capabilities we announce say so.
    return _error(rid, METHOD_NOT_FOUND, f"Method not found: {method}")


def _version() -> str:
    from .version import __version__
    return __version__


# ───────────────────────── transport ─────────────────────────

def _check_origin(request: Request) -> None:
    """DNS-rebinding guard the transport spec requires: a browser-borne
    request names its Origin, and one that does not match this host is
    refused. MCP clients (desktop apps, CLIs) send no Origin at all."""
    origin = request.headers.get("origin")
    if not origin:
        return
    host = request.headers.get("host", "")
    from urllib.parse import urlsplit
    try:
        o_host = urlsplit(origin).netloc.lower()
    except ValueError:
        o_host = ""
    if not o_host or o_host != host.lower():
        raise HTTPException(status_code=403, detail="origin not allowed")


def _check_protocol_version(request: Request) -> None:
    v = request.headers.get("mcp-protocol-version")
    if v is None:
        return                              # spec: assume 2025-03-26
    if v not in PROTOCOL_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported MCP-Protocol-Version {v!r}; "
                   f"this server speaks {', '.join(PROTOCOL_VERSIONS)}")


async def _require_read_token(request: Request, authorization: str | None) -> str:
    """The read gate. Two credentials open it: an OAuth access token this
    server issued (app/oauth.py — how claude.ai and ChatGPT connectors
    arrive, since 2.1) or the same API / guest bearer every /api/* route
    takes. Returns the role, 'owner' or 'guest'. A 401 carries the
    WWW-Authenticate challenge the MCP authorization spec requires, naming
    the protected-resource metadata so a client can discover how to log
    in (RFC 9728 §5.1)."""
    from . import main, oauth
    bearer = authorization.removeprefix("Bearer ") if authorization else None
    role = await oauth.authenticate_access_token(bearer, request)
    if role is not None:
        return role
    try:
        main.require_token(request, authorization)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(
                status_code=401, detail=exc.detail,
                headers={"WWW-Authenticate": oauth.www_authenticate(request)})
        raise
    return "guest" if main._is_limited_read(authorization) else "owner"


@router.post("/mcp")
async def mcp_post(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    role = await _require_read_token(request, authorization)
    _check_origin(request)
    _check_protocol_version(request)
    raw = await request.body()
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _rejected(_error(None, PARSE_ERROR, "Parse error"), method=None,
                         size=len(raw))
    if isinstance(msg, list):
        # Batches went away in 2025-06-18 and were optional before; one
        # message per POST keeps the transport a function call.
        return _rejected(
            _error(None, INVALID_REQUEST,
                   "Invalid Request: send one JSON-RPC message per POST"),
            method="<batch>", size=len(raw))
    reply = await handle_message(msg, role)
    if reply is None:
        return Response(status_code=202)
    if "error" in reply and reply.get("id") is None:
        return _rejected(reply, method=msg.get("method") if isinstance(msg, dict) else None,
                         size=len(raw))
    return JSONResponse(reply)


def _rejected(reply: dict[str, Any], *, method: str | None, size: int) -> JSONResponse:
    """A 400 with its JSON-RPC error code and the offending method in the
    log (2.2, item 9 of the 2.2 plan): claude.ai's connector 400s once per
    handshake and the log said only 'POST /mcp 400', which is not a clue.
    Never the body: a malformed message may carry a token."""
    err = reply.get("error") or {}
    log.info("mcp 400: code=%s message=%r method=%s bytes=%d",
             err.get("code"), str(err.get("message", ""))[:120], method, size)
    return JSONResponse(reply, status_code=400)


@router.get("/mcp")
async def mcp_get(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """No server-initiated stream here: every tool answers in one POST.
    405 is the spec's answer for that."""
    await _require_read_token(request, authorization)
    return JSONResponse({"detail": "this MCP server does not open SSE "
                                   "streams; POST JSON-RPC messages"},
                        status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
async def mcp_delete(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Stateless server: there is no session to terminate."""
    await _require_read_token(request, authorization)
    return JSONResponse({"detail": "stateless server; no session to end"},
                        status_code=405, headers={"Allow": "POST"})
