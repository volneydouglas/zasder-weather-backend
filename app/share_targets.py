"""Community upload fan-out (1.8, Pillar B): PWSWeather, Windy,
WeatherCloud, and CWOP from one egress module — the mirror image of the
five-path ingest, with the per-target health nobody ships (last success
and last error surfaced in the API, not buried in a log).

Design rules:
- One primary station feeds all targets (the one-sky-per-server rule).
- Each target has its own cadence, matched to the network's ask —
  CWOP's published guidance is 5–10 minutes; WeatherCloud's free tier
  wants 10.
- Every send is best-effort: failures stamp the target's status row and
  wait for the next window. Nothing here may block the monitor tick.
- Absent readings are OMITTED from uploads, never sent as zero.
- Credentials live in server_kv (app-managed, like integrations); the
  status API never echoes them.

Protocol notes, per target:
- PWSWeather: WU-style GET to pwsupdate.pwsweather.com, imperial units.
- Windy: GET stations.windy.com/pws/update/<key>; imperial param names
  (tempf/windspeedmph/…) are accepted per their docs.
- WeatherCloud: GET api.weathercloud.net/v01/set with metric values as
  integers ×10 (their decimal convention).
- CWOP: an APRS-IS position/weather packet over TCP (cwop.aprs.net:14580),
  fixed-width fields, imperial wind/temp, hundredths-inch rain, tenths-mb
  pressure. Registration at wxqa.com issues the CWxxxx id; validation
  passcode -1 (receive-only servers accept unverified CW ids).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import math
import re
from typing import Any

import httpx

from . import db
from .version import __version__

log = logging.getLogger("share")

_URL_RE = re.compile(r"https?://\S+")


def _safe_err(e: Exception) -> str:
    """Exception text with URLs redacted. httpx errors embed the full
    request URL, and Windy/PWSWeather/WeatherCloud carry live credentials
    in the path or query — a transport error must never write a key into
    share.<target>.status, the log line, or the /api/sharing response
    (CodeRabbit, PR #32)."""
    msg = _URL_RE.sub("<url>", str(e)) or type(e).__name__
    return f"{type(e).__name__}: {msg}"[:200]

TARGETS = ("pwsweather", "windy", "weathercloud", "cwop")

_INTERVALS_MS = {
    "pwsweather": 5 * 60_000,
    "windy": 5 * 60_000,
    "weathercloud": 10 * 60_000,
    "cwop": 10 * 60_000,
}
# The operator can send more often (Doren, 2026-09-06: WeatherCat posts
# PWSWeather every 5 s and WeatherCloud every minute), down to each
# network's own floor: Windy accepts one report per 5 min, WeatherCloud's
# free plan one per 10 min, CWOP asks for 5 min or slower, PWSWeather
# has no published floor (1 min is plenty). `interval_min` in the target's
# config; absent means the defaults above.
MIN_INTERVAL_MIN = {"pwsweather": 1, "windy": 5, "weathercloud": 10, "cwop": 5}
MAX_INTERVAL_MIN = 60


def interval_min(target: str, cfg: dict | None) -> int:
    """The effective cadence in minutes: the configured value clamped to
    the network's floor and the hour ceiling, else the default."""
    default = _INTERVALS_MS[target] // 60_000
    raw = (cfg or {}).get("interval_min")
    try:
        v = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        v = default
    return max(MIN_INTERVAL_MIN[target], min(MAX_INTERVAL_MIN, v))


def interval_ms(target: str, cfg: dict | None) -> int:
    return interval_min(target, cfg) * 60_000

# Primary + fallback APRS-IS tier-2 rotation. cwop.aprs.net rotates
# through IPs that include hosts unreachable from cloud networks (live
# repro from the Fly box, 2026-08-25: 129.15.108.116 blackholes while its
# siblings answer in 0.3s) — _send_cwop resolves the addresses itself and
# gives each a short budget so one dead IP can't eat the whole connect
# window, and the second HOST covers a day the primary's rotation is bad.
_CWOP_HOSTS = (("cwop.aprs.net", 14580), ("rotate.aprs2.net", 14580))

# Seams for _cwop_connect, monkeypatchable in tests — the broken
# happy_eyeballs_delay version shipped precisely because nothing
# exercised the connect path (R8).
async def _resolve(host: str, port: int) -> list[str]:
    import socket as _socket
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=_socket.SOCK_STREAM)
    return [addr[0] for *_ignored, addr in infos]


async def _open(ip: str, port: int, timeout: float = 4.0):
    return await asyncio.wait_for(asyncio.open_connection(ip, port),
                                  timeout=timeout)


async def _cwop_connect():
    """Manual per-address attempts, NOT happy_eyeballs_delay: uvloop's
    create_connection doesn't accept that kwarg (live TypeError on every
    send, 2026-08-26). Each resolved address gets a short budget so one
    blackholed IP in the rotation costs 4s, not the whole window."""
    last_err: Exception | None = None
    for host, port in _CWOP_HOSTS:
        try:
            ips = await _resolve(host, port)
        except OSError as e:
            last_err = e
            continue
        for ip in ips:
            try:
                return await _open(ip, port)
            except Exception as e:
                last_err = e
    raise last_err or TimeoutError("cwop connect failed")


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ── config + status (server_kv) ─────────────────────────────────────────

async def get_config(target: str) -> dict[str, Any]:
    raw = await db.get_kv(f"share.{target}")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def set_config(target: str, cfg: dict[str, Any]) -> None:
    await db.set_kv(f"share.{target}", json.dumps(cfg) if cfg else None)


async def get_status(target: str) -> dict[str, Any]:
    raw = await db.get_kv(f"share.{target}.status")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


async def _stamp(target: str, ok: bool, error: str | None,
                 now_ms: int) -> None:
    st = await get_status(target)
    if ok:
        st["last_ok_ms"] = now_ms
        st["last_error"] = None
        st["last_error_ms"] = None
    else:
        st["last_error"] = (error or "unknown")[:200]
        st["last_error_ms"] = now_ms
    await db.set_kv(f"share.{target}.status", json.dumps(st))


# ── pure builders (unit-tested) ─────────────────────────────────────────

def pwsweather_params(cfg: dict, obs: dict, now_utc: _dt.datetime) -> dict:
    p: dict[str, Any] = {
        "ID": cfg.get("station_id", ""),
        "PASSWORD": cfg.get("api_key", ""),
        "dateutc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "softwaretype": f"ZasderWeather-{__version__}",
        "action": "updateraw",
    }
    for src, dst in (("tempf", "tempf"), ("humidity", "humidity"),
                     ("dewPoint", "dewptf"), ("winddir", "winddir"),
                     ("windspeedmph", "windspeedmph"),
                     ("windgustmph", "windgustmph"),
                     ("baromrelin", "baromin"), (RAIN_LAST_HOUR, "rainin"),
                     ("dailyrainin", "dailyrainin"),
                     ("solarradiation", "solarradiation"), ("uv", "UV")):
        v = _f(obs.get(src))
        if v is not None:
            p[dst] = v
    return p


WINDY_V2_UPDATE = "https://stations.windy.com/api/v2/observation/update"
# The read-back used by the live-marked check in test_share_targets.py:
# GET ?PASSWORD=<station password>&latestLimit=1 returns the station's
# header and its newest observation (rh, dew_point, ...). Verified by hand
# 2026-09-06: after one send with `humidity` and `dewptf` the read-back
# carried rh=[21] and dew_point=[284.15], so those names are accepted.
WINDY_V2_READ = "https://stations.windy.com/api/v2/observation"
WINDY_LEGACY_UPDATE = "https://stations.windy.com/pws/update/"

# The key the runner puts on the reading it hands the senders: rain over
# the trailing hour in inches (db.rain_last_hour_in). `hourlyrainin` in a
# reading is a RATE in in/hr by the repo's own rule and must never be sent
# as an accumulation (round-three review BE-F6: a 40 in/hr burst reached
# networks that feed NOAA as forty inches of rain).
RAIN_LAST_HOUR = "rain_last_hour_in"


def windy_params(obs: dict) -> dict:
    """The WU-protocol names Windy's Stations API v2 documents (January
    2026: id + PASSWORD per station, `/api/v2/observation/update`, at most
    one report per 5 min). `humidity`, `dewptf` and `solarradiation` are
    the documented names (the spec lists `rh` and `dewpoint` °C as the
    metric aliases, used only when these are missing); the old
    `rh`/`dewpointf` pair was the legacy API's. Verified live 2026-09-06
    against Volney's WestChandler: the read-back after one send carried
    rh and dew_point, so humidity and dewptf are accepted (round-three
    review BE-F7)."""
    p: dict[str, Any] = {}
    for src, dst in (("tempf", "tempf"), ("humidity", "humidity"),
                     ("winddir", "winddir"),
                     ("windspeedmph", "windspeedmph"),
                     ("windgustmph", "windgustmph"),
                     ("baromrelin", "baromin"), (RAIN_LAST_HOUR, "rainin"),
                     ("uv", "uv"), ("dewPoint", "dewptf"),
                     ("solarradiation", "solarradiation")):
        v = _f(obs.get(src))
        if v is not None:
            p[dst] = v
    return p


def windy_is_v2(cfg: dict | None) -> bool:
    """A station id and a station password mean the 2026 API; an api_key
    alone is a legacy account key, honoured on the legacy path until Windy
    switches it off at the end of 2026."""
    c = cfg or {}
    return bool(str(c.get("station_id") or "").strip()
                and str(c.get("password") or "").strip())


def weathercloud_params(cfg: dict, obs: dict) -> dict:
    """Metric integers ×10, their decimal convention. Only present
    readings ride."""
    p: dict[str, Any] = {"wid": cfg.get("wid", ""), "key": cfg.get("key", "")}

    def put(name, v):
        if v is not None:
            p[name] = int(round(v))

    t = _f(obs.get("tempf"))
    put("temp", None if t is None else (t - 32) * 5 / 9 * 10)
    h = _f(obs.get("humidity"))
    put("hum", h)
    d = _f(obs.get("dewPoint"))
    put("dew", None if d is None else (d - 32) * 5 / 9 * 10)
    b = _f(obs.get("baromrelin"))
    put("bar", None if b is None else b * 33.8639 * 10)
    w = _f(obs.get("windspeedmph"))
    put("wspd", None if w is None else w * 0.44704 * 10)
    g = _f(obs.get("windgustmph"))
    put("wspdhi", None if g is None else g * 0.44704 * 10)
    wd = _f(obs.get("winddir"))
    put("wdir", wd)
    r = _f(obs.get("dailyrainin"))
    put("rain", None if r is None else r * 25.4 * 10)
    uv = _f(obs.get("uv"))
    put("uvi", None if uv is None else uv * 10)
    return p


def _aprs_latlon(lat: float, lon: float) -> tuple[str, str]:
    """APRS DDMM.mmN / DDDMM.mmW fixed-width encoding."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    alat, alon = abs(lat), abs(lon)
    lat_s = f"{int(alat):02d}{(alat - int(alat)) * 60:05.2f}{ns}"
    lon_s = f"{int(alon):03d}{(alon - int(alon)) * 60:05.2f}{ew}"
    return lat_s, lon_s


def _wx3(v: float | None) -> str:
    """3-digit fixed field; APRS spaces-out missing values as '...'."""
    if v is None:
        return "..."
    return f"{int(round(min(max(v, 0), 999))):03d}"


def cwop_packet(station_id: str, lat: float, lon: float, obs: dict,
                now_utc: _dt.datetime) -> str:
    """One APRS position/weather packet. Fixed-width, imperial, per the
    CWOP guides: t temp °F (b-padded negatives), r last-hour rain and
    P since-midnight rain in hundredths, h humidity (00 = 100%),
    b sea-level pressure in tenths of mb."""
    lat_s, lon_s = _aprs_latlon(lat, lon)
    ts = now_utc.strftime("%d%H%M")
    wind_dir = _f(obs.get("winddir"))
    wind = _f(obs.get("windspeedmph"))
    gust = _f(obs.get("windgustmph"))
    body = (f"{station_id}>APRS,TCPIP*:@{ts}z{lat_s}/{lon_s}"
            f"_{_wx3(wind_dir)}/{_wx3(wind)}g{_wx3(gust)}")
    t = _f(obs.get("tempf"))
    if t is None:
        body += "t..."
    elif t < 0:
        body += f"t-{int(round(min(-t, 99))):02d}"
    else:
        body += f"t{int(round(min(t, 999))):03d}"
    r = _f(obs.get(RAIN_LAST_HOUR))
    if r is not None:
        body += f"r{int(round(min(r, 9.99) * 100)):03d}"
    p_mid = _f(obs.get("dailyrainin"))
    if p_mid is not None:
        body += f"P{int(round(min(p_mid, 9.99) * 100)):03d}"
    h = _f(obs.get("humidity"))
    if h is not None and 0 <= h <= 100:
        # Out-of-range readings are OMITTED, not clamped: "h-3" broke the
        # fixed-width packet (R7), and clamping a sub-zero glitch published
        # it as saturated air (R8 S9). A missing group is honest.
        hh = int(round(h))
        body += f"h{0 if hh >= 100 else hh:02d}"
    b = _f(obs.get("baromrelin"))
    if b is not None:
        body += f"b{int(round(b * 33.8639 * 10)):05d}"
    body += "ZasderWeather"
    return body


# ── senders ─────────────────────────────────────────────────────────────

def _reading_time(obs: dict, now_ms: int) -> _dt.datetime:
    """The reading's own time, for the protocols that carry one. The
    monitor's clock was stamped on every upload before (2.1 pre-release
    review BE-5), so a station that died at 02:00 had that reading
    published as current every few minutes, indefinitely."""
    ts = _f(obs.get("dateutc"))
    return _dt.datetime.fromtimestamp((ts if ts is not None else now_ms) / 1000,
                                      _dt.timezone.utc)


# How old the primary station's newest reading may be before a target is
# skipped rather than fed: twice the target's own cadence, so one missed
# tick still publishes and a dead station does not.
STALE_FACTOR = 2


def reading_too_old(obs: dict, now_ms: int, target: str,
                    cfg: dict | None = None) -> int | None:
    """Minutes of staleness when the reading is too old to publish to
    `target`, else None. Measured against the DEFAULT cadence and nothing
    else: a 1-minute PWSWeather cadence must not call a 3-minute-old
    reading dead, and an hourly CWOP cadence must not let a two-hour-old
    reading through (round-three review BE-F9). `cfg` is accepted for the
    callers' sake and deliberately unused."""
    ts = _f(obs.get("dateutc"))
    if ts is None:
        return None            # legacy rows without a stamp: unchanged
    age = now_ms - ts
    if age > STALE_FACTOR * _INTERVALS_MS[target]:
        return int(age // 60_000)
    return None


async def _send_pwsweather(cfg, obs, now_ms) -> str | None:
    params = pwsweather_params(cfg, obs, _reading_time(obs, now_ms))
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            "https://pwsupdate.pwsweather.com/api/v1/submitwx", params=params)
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    return None


async def _send_windy(cfg, obs, now_ms) -> str | None:
    params = windy_params(obs)
    # Both APIs take the observation time as `dateutc`
    # ("YYYY-MM-DD HH:MM:SS", UTC); without it the receive time is used.
    params["dateutc"] = _reading_time(obs, now_ms).strftime("%Y-%m-%d %H:%M:%S")
    if windy_is_v2(cfg):
        secret = str(cfg.get("password"))
        params["id"] = str(cfg.get("station_id")).strip()
        params["PASSWORD"] = secret
        params["softwaretype"] = "Zasder Weather"
        url = WINDY_V2_UPDATE
    else:
        secret = str(cfg.get("api_key", ""))
        params["station"] = cfg.get("station") if cfg.get("station") is not None else 0
        url = WINDY_LEGACY_UPDATE + secret
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, params=params)
    # 409 is Windy saying "I already have exactly this report": the data is
    # there, which is what a send is for.
    if r.status_code in (200, 409):
        return None
    return windy_error(r.status_code, r.text, secret)


def windy_error(status: int, body: str, key: str = "") -> str:
    """"HTTP 400" told an operator nothing (Doren, 2026-09-06: a fresh
    key, a 400, no idea why). Windy answers JSON with a `message`; carry
    it, bounded, with the key scrubbed should Windy ever echo it."""
    msg = ""
    try:
        parsed = json.loads(body or "")
        if isinstance(parsed, dict):
            msg = str(parsed.get("message") or "")
    except ValueError:
        msg = ""
    msg = " ".join(msg.split())[:120]
    if key and key in msg:
        msg = msg.replace(key, "***")
    return f"HTTP {status}" + (f" — {msg}" if msg else "")


async def _send_weathercloud(cfg, obs, now_ms) -> str | None:
    params = weathercloud_params(cfg, obs)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get("https://api.weathercloud.net/v01/set",
                             params=params)
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    return None


async def _send_cwop(cfg, obs, now_ms, coords) -> str | None:
    sid = (cfg.get("station_id") or "").strip().upper()
    if not sid:
        return "no station id"
    if coords is None:
        return "station has no coordinates"
    packet = cwop_packet(sid, coords[0], coords[1], obs,
                         _reading_time(obs, now_ms))
    try:
        reader, writer = await _cwop_connect()
    except Exception as e:
        return _safe_err(e)
    try:
        try:
            await asyncio.wait_for(reader.readline(), timeout=10)  # banner
            writer.write(
                f"user {sid} pass -1 vers ZasderWeather {__version__}\r\n"
                .encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=10)  # logresp
            writer.write((packet + "\r\n").encode())
            await writer.drain()
            await asyncio.sleep(3)     # the guides' post-send settle
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    except Exception as e:
        return _safe_err(e)
    return None


# ── runner ──────────────────────────────────────────────────────────────

_last_send_ms: dict[str, int] = {}


def _reset_for_tests() -> None:
    _last_send_ms.clear()


def _coords(device: dict[str, Any]) -> tuple[float, float] | None:
    info = device.get("info") or {}
    coords = (info.get("coords") or {}).get("coords") or {}
    lat, lon = coords.get("lat"), coords.get("lon")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def station_for(cfg: dict | None, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The device a target publishes: the configured `mac`, or when none
    is configured the first weather station with a reading (the rule
    before 2.1; Volney, 2026-09-06: people with several stations pick
    which one goes out, the app defaults it to their top station).

    An explicit choice never substitutes (round-three review BE-F8):
    a configured station that is absent, deleted or silent returns None
    and the caller says so, rather than station B's readings going out
    under station A's network id. An air monitor is never a station."""
    want = str((cfg or {}).get("mac") or "").upper()
    if want:
        for d in devices:
            if str(d.get("mac") or "").upper() == want:
                if d.get("lastData") and not db.is_air_monitor_device(d):
                    return d
                return None
        return None
    return next((d for d in devices
                 if d.get("lastData") and not db.is_air_monitor_device(d)), None)


async def _with_hour_rain(station: dict[str, Any], now_ms: int) -> dict[str, Any]:
    """The reading the senders get: the station's newest, plus the
    trailing hour's accumulation under RAIN_LAST_HOUR (BE-F6)."""
    obs = dict(station.get("lastData") or {})
    try:
        obs[RAIN_LAST_HOUR] = await db.rain_last_hour_in(str(station.get("mac") or ""),
                                                         now_ms)
    except Exception as e:                            # noqa: BLE001
        log.warning("rain_last_hour_in failed for %s: %s", station.get("mac"), e)
        obs[RAIN_LAST_HOUR] = None
    return obs


SENDERS = {"pwsweather": "_send_pwsweather", "windy": "_send_windy",
           "weathercloud": "_send_weathercloud", "cwop": "_send_cwop"}


async def send_once(target: str, devices: list[dict[str, Any]],
                    now_ms: int) -> dict[str, Any]:
    """One send right now, cadence ignored, for the app's Save and verify
    button (Volney, 2026-09-06: "see if it works before exiting"). Returns
    {ok, error, station} and stamps the status like the tick does, so the
    row agrees with the sheet. Network floors are the NETWORK's business:
    a WeatherCloud test inside its ten minutes comes back as its error."""
    cfg = await get_config(target)
    station = station_for(cfg, devices)
    if station is None:
        return {"ok": False, "error": "the chosen station has no reading yet",
                "station": None}
    obs = await _with_hour_rain(station, now_ms)
    # The same gate the tick applies (round-three review SEC-G6): a
    # station that died three days ago is not verified by publishing its
    # last reading as current to four networks.
    stale = reading_too_old(obs, now_ms, target, cfg)
    if stale is not None:
        err = f"newest reading is {stale} min old; not published"
        await _stamp(target, False, err, now_ms)
        return {"ok": False, "error": err, "station": station.get("mac")}
    try:
        sender = globals()[SENDERS[target]]
        if target == "cwop":
            err = await sender(cfg, obs, now_ms, _coords(station))
        else:
            err = await sender(cfg, obs, now_ms)
    except Exception as e:                            # noqa: BLE001
        err = _safe_err(e)
    await _stamp(target, err is None, err, now_ms)
    _last_send_ms[target] = now_ms
    return {"ok": err is None, "error": err, "station": station.get("mac")}


async def check(devices: list[dict[str, Any]], now_ms: int) -> None:
    """One monitor-tick entry point. Per-target station and cadence."""
    if not any(d.get("lastData") for d in devices):
        return

    async def _one(target: str, cfg: dict) -> None:
        station = station_for(cfg, devices)
        if station is None:
            return
        obs = await _with_hour_rain(station, now_ms)
        try:
            if target == "pwsweather":
                err = await _send_pwsweather(cfg, obs, now_ms)
            elif target == "windy":
                err = await _send_windy(cfg, obs, now_ms)
            elif target == "weathercloud":
                err = await _send_weathercloud(cfg, obs, now_ms)
            else:
                err = await _send_cwop(cfg, obs, now_ms, _coords(station))
        except Exception as e:
            err = _safe_err(e)
        await _stamp(target, err is None, err, now_ms)
        if err:
            log.warning("share %s failed: %s", target, err)

    # Concurrent, not sequential (R7): four dead-network targets used to
    # stall the 60s monitor tick ~68s back-to-back — delaying device-down
    # detection, the monitor's actual job.
    due: list[tuple[str, dict]] = []
    for target in TARGETS:
        # Cheapest gate first: nothing can be due inside the network's own
        # floor, so the config read happens at most once per floor.
        if now_ms - _last_send_ms.get(target, 0) < MIN_INTERVAL_MIN[target] * 60_000:
            continue
        cfg = await get_config(target)
        if not cfg.get("enabled"):
            continue
        if now_ms - _last_send_ms.get(target, 0) < interval_ms(target, cfg):
            continue
        _last_send_ms[target] = now_ms
        station = station_for(cfg, devices)
        if station is None:
            await _stamp(target, False, "the chosen station has no reading yet", now_ms)
            continue
        obs = station["lastData"]
        # A reading older than twice the cadence is a dead station, not
        # weather: skip the send and say so in the status, rather than
        # publish it to PWSWeather, Windy, WeatherCloud and CWOP (which
        # feeds NOAA MADIS) as current (2.1 pre-release review BE-5).
        stale = reading_too_old(obs, now_ms, target, cfg)
        if stale is not None:
            await _stamp(target, False,
                         f"newest reading is {stale} min old; not published",
                         now_ms)
            continue
        due.append((target, cfg))
    if due:
        await asyncio.gather(*(_one(t, c) for t, c in due))
