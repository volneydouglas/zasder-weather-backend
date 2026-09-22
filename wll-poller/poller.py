#!/usr/bin/env python3
"""Davis WeatherLink Live (WLL) local poller.

Polls a WLL gateway's local HTTP API (`/v1/current_conditions`) every few
seconds and forwards a normalized observation to a Zasder Weather backend's
`/ingest/custom` endpoint. Designed to run on a small always-on host on the
same LAN as the WLL gateway (e.g. a Raspberry Pi) — pure stdlib so installs
are a single file copy.

WLL local API: https://weatherlink.github.io/weatherlink-live-local-api/

Why local instead of the WeatherLink cloud poller: WLL serves fresh data on
every HTTP request (UDP broadcasts at 2.5s); the cloud only updates every
60s, needs an API key, and an internet round-trip. Same physical Davis VP2,
~6× lower latency.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# No baked-in default host: the old default was one developer's LAN IP,
# which on anyone else's network either fails slowly or polls a stranger's
# device. Missing config should fail loudly at startup (see main()).
WLL_HOST          = os.environ.get("WLL_HOST", "").strip()
# Same friendly-SystemExit treatment as WLL_TXID below: under launchd/
# systemd/Docker a raw int() traceback is an opaque restart loop.
_POLL_ENV = os.environ.get("WLL_POLL_SECONDS", "").strip() or "10"
try:
    WLL_POLL_SECONDS = int(_POLL_ENV)
except ValueError:
    raise SystemExit(f"WLL_POLL_SECONDS must be a whole number of seconds, "
                     f"got {_POLL_ENV!r}")
if WLL_POLL_SECONDS <= 0:
    raise SystemExit(f"WLL_POLL_SECONDS must be positive, got {WLL_POLL_SECONDS}")
# Rediscovery after a gateway swap (2.4, item 2). Doren replaced a
# WeatherLink Live and the poller kept asking the old address forever,
# because the address is the only thing it was ever told. The gateway
# reports its OWN hardware id in every answer (`data.did`, the Davis
# serial), so the poller remembers that while the typed address works and
# can go and find it again if the address stops answering. Set
# WLL_REDISCOVER=0 to switch the whole thing off.
WLL_REDISCOVER    = os.environ.get("WLL_REDISCOVER", "1").strip() not in ("0", "false", "no")
# How long the address has to be silent before the poller goes looking. A
# gateway rebooting, or a router handing out leases after a power cut, is
# back inside a couple of minutes; a sweep before that is noise on
# somebody's network for nothing.
_QUIET_ENV        = os.environ.get("WLL_REDISCOVER_AFTER_S", "").strip() or "600"
try:
    WLL_REDISCOVER_AFTER_S = int(_QUIET_ENV)
except ValueError:
    raise SystemExit(f"WLL_REDISCOVER_AFTER_S must be a whole number of "
                     f"seconds, got {_QUIET_ENV!r}")
# Where the learned id is kept, so a restart does not forget it.
WLL_STATE_FILE    = os.environ.get("WLL_STATE_FILE", "/tmp/wll-poller-state.json")
BACKEND_URL       = os.environ.get("BACKEND_URL", "").rstrip("/")
INGEST_TOKEN      = os.environ.get("INGEST_TOKEN", "")
# Synthetic MAC the backend stores under. If you also run the WeatherLink
# CLOUD poller for the same physical Davis VP2, reuse its MAC so both feeds
# land on the same device row (cloud is then redundant + can be disabled).
DEVICE_MAC        = os.environ.get("WLL_DEVICE_MAC", "5D:5D:05:00:00:01")
DEVICE_NAME       = os.environ.get("WLL_DEVICE_NAME", "")     # empty → keep existing
DEVICE_LOCATION   = os.environ.get("WLL_DEVICE_LOCATION", "")
SOURCE            = "davis-wll-local"
# Which ISS transmitter to read. A WLL gateway supports up to 8, and merging
# them all into one observation mixes readings from different physical
# stations. Blank = pick the lowest txid present (deterministic, and correct
# for the single-ISS case); set it to pin a specific transmitter.
_TXID_ENV         = os.environ.get("WLL_TXID", "").strip()
try:
    WLL_TXID = int(_TXID_ENV) if _TXID_ENV else None
except ValueError:
    # Under systemd/Docker this would otherwise be a traceback in a restart
    # loop; say what's wrong in one line instead.
    raise SystemExit(f"WLL_TXID must be a transmitter number 1-8, got {_TXID_ENV!r}")
# Range, not just "is an int". The message above already promises 1-8, but 0,
# -1 and 9 parsed fine and then matched no ISS record — _pick_iss() returns
# nothing and EVERY outdoor field is silently dropped from every post. A typo
# in an env var should not look like a dead sensor.
if WLL_TXID is not None and not (1 <= WLL_TXID <= 8):
    raise SystemExit(
        f"WLL_TXID must be a transmitter number 1-8, got {WLL_TXID}. "
        "Leave it blank to use the lowest transmitter the WeatherLink reports.")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s wll: %(message)s",
)
log = logging.getLogger("wll")

# rain_size enum (WLL field) → inches per single tip count.
_RAIN_SIZE_IN: dict[int, float] = {
    1: 0.01,            # 0.01"  (US Davis tipping bucket)
    2: 0.2 / 25.4,      # 0.2 mm
    3: 0.1 / 25.4,      # 0.1 mm
    4: 0.001,           # 0.001"
}


def fetch_wll(host: str = WLL_HOST, timeout: float = 5.0) -> dict:
    """GET the WLL local current-conditions snapshot. Raises on transport failure."""
    url = f"http://{host}/v1/current_conditions"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _num(d: dict, key: str):
    """Return d[key] if it's a real number — WLL emits null for sensors that
    aren't reporting (out of range, transmitter offline). Coerce to None
    rather than letting null/None/strings sneak through."""
    v = d.get(key)
    # bool excluded (R6): isinstance(True, int) is True, so a malformed
    # `"temp": true` reached SQLite as 1.0 — the backend's own _finite
    # excludes bools for exactly this reason.
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


_txid_warned = False


def _iss_txid(c: dict) -> int:
    """txid of an ISS record. Single-transmitter gateways omit the field."""
    v = c.get("txid")
    return v if isinstance(v, int) else 1


def _pick_iss(conditions: list) -> dict | None:
    """The one ISS record to read this tick, or None if there isn't one.

    Reading every `data_structure_type == 1` record into the same observation
    lets a second transmitter (extra temp/hum station, standalone anemometer)
    overwrite the real station's fields, silently and with no log line."""
    global _txid_warned
    iss = [c for c in conditions
           if isinstance(c, dict) and c.get("data_structure_type") == 1]
    if not iss:
        return None
    if WLL_TXID is not None:
        for c in iss:
            if _iss_txid(c) == WLL_TXID:
                return c
        if not _txid_warned:
            _txid_warned = True
            log.warning("WLL_TXID=%d is not reporting — transmitters seen: %s. "
                        "No outdoor data will be posted until it does.",
                        WLL_TXID, sorted({_iss_txid(c) for c in iss}))
        return None
    if len(iss) > 1 and not _txid_warned:
        _txid_warned = True
        log.warning("%d ISS transmitters reporting (txids %s) — using the "
                    "lowest. Set WLL_TXID to pick a different one.",
                    len(iss), sorted({_iss_txid(c) for c in iss}))
    return min(iss, key=_iss_txid)


def _feels_like(c: dict):
    """WLL reports heat_index AND wind_chill on every tick — its own sample
    response shows heat_index 5.5 alongside temp 62.7 — so taking whichever
    is truthy reports a nonsense 'feels like' most of the year. Select by
    temperature regime, matching backend _compute_feels_like (heat index at
    >=80F, wind chill at <=50F, air temp between)."""
    t  = _num(c, "temp")
    hi = _num(c, "heat_index")
    wc = _num(c, "wind_chill")
    if t is None:
        return None
    if t >= 80.0 and hi is not None:
        return hi
    if t <= 50.0 and wc is not None:
        return wc
    return t


def to_observation(
    wll: dict,
    *,
    mac: str = DEVICE_MAC,
    name: str = DEVICE_NAME,
    location: str = DEVICE_LOCATION,
) -> dict | None:
    """Transform a WLL JSON snapshot into the /ingest/custom payload shape.
    Returns None if there's nothing usable (WLL booted but ISS hasn't been
    heard, or WLL returned an error)."""
    if not isinstance(wll, dict):
        # R6: syntactically-valid non-object JSON (a captive portal's list,
        # say) raised AttributeError on .get and cost the tick a traceback
        # instead of the documented "returns None if nothing usable".
        log.warning("wll returned non-object JSON (%s)", type(wll).__name__)
        return None
    err = wll.get("error")
    if err:
        log.warning("wll returned error: %s", err)
        return None
    data = wll.get("data") or {}
    conditions = data.get("conditions") or []
    # Fall back to "now" when ts is missing, 0, or garbage. A non-numeric ts
    # used to raise out of int() and drop the whole observation; the reading
    # itself is fine, only the gateway's clock claim is broken.
    try:
        ts = int(data.get("ts") or 0) or int(time.time())
    except (TypeError, ValueError):
        ts = int(time.time())
    # Sanity-bound NUMERIC garbage too (R6): a huge ts raised "year must be
    # in 1..9999" OUT of the fromtimestamp below (killing every tick while
    # the gateway clock stayed broken), and a tiny/negative one posted
    # 1969/1970 stamps the backend 400s — logged here as "check
    # INGEST_TOKEN", sending the operator to debug credentials while every
    # reading was silently lost. Bounds mirror the backend's own policy:
    # >15 min future or before ~2001 = broken clock, not data — use now.
    now_s = int(time.time())
    if ts > now_s + 15 * 60 or ts < 1_000_000_000:
        log.warning("wll ts %s is implausible — using server time", ts)
        ts = now_s
    try:
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        iso = datetime.fromtimestamp(now_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    outdoor: dict = {}
    wind: dict = {}
    rain: dict = {}
    pressure: dict = {}
    indoor: dict = {}
    solar: dict = {}

    iss = _pick_iss(conditions)                              # ISS — outdoor + rain + solar
    if iss is not None:
        outdoor["tempf"]        = _num(iss, "temp")
        outdoor["humidity"]     = _num(iss, "hum")
        outdoor["dew_point_f"]  = _num(iss, "dew_point")
        # "Feels like" = NWS heat index / wind chill, matching what the
        # other stations (AmbientWeather, SDR via backend _compute_feels_like)
        # report. Deliberately NOT thsw_index: THSW adds a direct-sun load
        # that runs ~5-10°F hotter (e.g. 99°F/30%/471 W/m² → THSW 110 vs
        # heat-index 104), which reads as wrong next to every other source.
        outdoor["feels_like"] = _feels_like(iss)
        wind["speed_mph"] = _num(iss, "wind_speed_last")
        wind["dir_deg"]   = _num(iss, "wind_dir_last")
        wind["gust_mph"]  = _num(iss, "wind_speed_hi_last_10_min")

        size = _RAIN_SIZE_IN.get(iss.get("rain_size"), 0.01)
        def _in(name: str):
            v = _num(iss, name)
            return v * size if v is not None else None
        # hourly_in must be the LAST-HOUR ACCUMULATION (rainfall_last_60_min,
        # counts), matching what the WeatherLink cloud poller writes to the
        # same backend field (rainfall_last_60_min_in) — possibly the same
        # device row. The instantaneous rate (rain_rate_last, counts/hour)
        # spikes on burst intensity and reads ~0 during steady light rain;
        # posting it here mixed two different quantities into one column.
        rain["hourly_in"]  = _in("rainfall_last_60_min")
        rain["daily_in"]   = _in("rainfall_daily")
        rain["monthly_in"] = _in("rainfall_monthly")
        rain["yearly_in"]  = _in("rainfall_year")

        solar["radiation_wm2"] = _num(iss, "solar_rad")
        solar["uv"]            = _num(iss, "uv_index")

    for c in conditions:
        st = c.get("data_structure_type") if isinstance(c, dict) else None
        if st == 3:                                          # LSS BAR — barometer
            # Backend's _flatten reads pressure.relative_inhg (full word) and
            # treats it as both relative and absolute for ingest sources.
            pressure["relative_inhg"] = _num(c, "bar_sea_level")
            pressure["absolute_inhg"] = _num(c, "bar_absolute")

        elif st == 4:                                        # LSS Temp/Hum — WLL indoor
            indoor["tempf"]    = _num(c, "temp_in")
            indoor["humidity"] = _num(c, "hum_in")

    if not (outdoor or wind or rain or pressure or indoor or solar):
        return None

    device: dict = {"id": mac}
    if name:     device["name"] = name
    if location: device["location"] = location
    # ISS transmitter battery (trans_battery_flag: 0 = ok, 1 = low), mapped
    # onto the same device.battery_outdoor convention the other relays post
    # so the backend's ingest turns it into battout and the app's battery
    # icon + low-battery notable work for Davis stations too (1.7).
    if iss is not None:
        flag = _num(iss, "trans_battery_flag")
        if flag is not None:
            device["battery_outdoor"] = "low" if flag else "normal"

    return {
        "device": device,
        "timestamp_utc": iso,
        "outdoor": outdoor,
        "wind": wind,
        "rain": rain,
        "pressure": pressure,
        "indoor": indoor,
        "solar": solar,
        "source": SOURCE,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects on the ingest POST. The default opener
    replays the request headers — including `Authorization: Bearer
    <INGEST_TOKEN>` — verbatim to whatever host a 3xx names, so a
    misconfigured BACKEND_URL (or an interposed proxy) would hand the
    write token to an arbitrary third party. Returning None makes urlopen
    raise HTTPError instead, which main() logs like any other bad status."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_INGEST_OPENER = urllib.request.build_opener(_NoRedirect)


def post_observation(obs: dict, *, backend: str = BACKEND_URL,
                     token: str = INGEST_TOKEN) -> None:
    """POST a normalized observation to /ingest/custom. Raises on HTTP error."""
    body = json.dumps(obs).encode("utf-8")
    req = urllib.request.Request(
        f"{backend}/ingest/custom",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    # The opener raises HTTPError for 4xx/5xx AND for 3xx (see _NoRedirect),
    # so a status check inside the `with` was dead code. Drain the body so
    # the connection can be reused/closed cleanly.
    with _INGEST_OPENER.open(req, timeout=10) as r:
        r.read()


# ── finding a gateway that moved (2.4, item 2) ────────────────────────
#
# The rule, and the reason for each half of it.
#
# The gateway is identified by `data.did`, which it reports itself, so
# there is no ARP table to read and nothing to run as root. A poller that
# moved on an address alone would happily start reading the NEIGHBOUR's
# WeatherLink Live, which is worse than not reading anything.
#
# The sweep is the /24 around the address that was working, private
# ranges only, and it runs at most once per cooldown after a long
# silence. A subnet scan is a small rudeness on somebody's network; this
# one is bounded, rare, and only ever a plain GET of a documented local
# API.

_PRIVATE_PREFIXES = ("10.", "192.168.", "169.254.")


def _is_private_v4(ip: str) -> bool:
    """RFC 1918 and link local. A public address is never swept: those are
    not ours to knock on, and a WLL is not on one anyway."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octets):
        return False
    if ip.startswith(_PRIVATE_PREFIXES):
        return True
    return octets[0] == 172 and 16 <= octets[1] <= 31


def subnet_candidates(host: str) -> list[str]:
    """Every other address on `host`'s /24, nearest first.

    Nearest first because a gateway that got a new lease usually got a
    neighbouring one, so the common case finishes in a few probes instead
    of two hundred. .0 and .255 are skipped, and so is the address we
    already know does not answer."""
    if not _is_private_v4(host):
        return []
    a, b, c, d = host.split(".")
    try:
        base = int(d)
    except ValueError:
        return []
    order = sorted(range(1, 255), key=lambda n: (abs(n - base), n))
    return [f"{a}.{b}.{c}.{n}" for n in order if n != base]


def device_id(snapshot: Any) -> str:
    """The gateway's own hardware id, upper cased. Empty when absent.

    Anything on the LAN can answer the probe URL with anything (a list,
    a string, a number): that is "not a gateway", never a crash."""
    if not isinstance(snapshot, dict):
        return ""
    did = (snapshot.get("data") or {}).get("did")
    return did.strip().upper() if isinstance(did, str) else ""


def _read_state(path: str = "") -> dict:
    try:
        with open(path or WLL_STATE_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict, path: str = "") -> None:
    try:
        with open(path or WLL_STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def starting_host(env_host: str, state: dict) -> str:
    """Where to poll first after a (re)start.

    The state file remembers where the gateway was LAST FOUND. Starting
    from the env address instead meant a restarted container polled the
    dead address for another WLL_REDISCOVER_AFTER_S before sweeping
    again. Only a stored host that belongs to a learned id is trusted;
    an empty or half-written file falls back to the env."""
    host = state.get("host") if isinstance(state, dict) else None
    did = state.get("did") if isinstance(state, dict) else None
    if isinstance(host, str) and host.strip() and isinstance(did, str) and did:
        return host.strip()
    return env_host


def remember_device(host: str, snapshot: dict, path: str = "") -> str:
    """Learn the id of whatever answered, while the address works."""
    did = device_id(snapshot)
    if not did:
        return ""
    state = _read_state(path)
    if state.get("did") != did or state.get("host") != host:
        _write_state({"did": did, "host": host}, path)
    return did


def find_gateway(did: str, from_host: str, *, fetch=None,
                 timeout: float = 0.6, log_every: int = 64) -> str | None:
    """Sweep `from_host`'s /24 for the gateway with this id.

    Returns the address it answered on, or None. `fetch` is injectable so
    the test suite can sweep a pretend network."""
    if not did:
        return None
    probe = fetch or (lambda h: fetch_wll(h, timeout=timeout))
    candidates = subnet_candidates(from_host)
    if not candidates:
        log.info("not sweeping %s — only private addresses are swept", from_host)
        return None
    log.warning("looking for gateway %s on %d addresses near %s",
                did, len(candidates), from_host)
    for i, host in enumerate(candidates, 1):
        # Before the probe, not after it: a silent address `continue`s
        # past anything below, and a whole /24 of silence is exactly the
        # sweep that takes longest.
        if log_every and i % log_every == 0:
            log.info("swept %d of %d", i, len(candidates))
            # A full /24 at 0.6 s a probe is over two minutes of not
            # looping, which is longer than the Docker healthcheck waits.
            # Sweeping is alive.
            _touch_heartbeat()
        try:
            snap = probe(host)
        except Exception:
            continue
        found = device_id(snap)
        if found and found == did:
            log.warning("gateway %s answered at %s after %d probes",
                        did, host, i)
            return host
        if found:
            # Somebody else's WeatherLink Live. This is exactly why the id
            # is checked and the address alone is never enough.
            log.info("%s is a different gateway (%s), leaving it alone",
                     host, found)
    log.warning("swept %d addresses near %s and found no gateway %s",
                len(candidates), from_host, did)
    return None


# Liveness heartbeat for the Docker HEALTHCHECK: touched once per loop
# iteration (even on error ticks — "alive but failing" is still alive and
# still logging; a WEDGED process is what the healthcheck must catch).
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/wll-heartbeat")


def _touch_heartbeat(path: str = "") -> None:
    """Best-effort mtime bump; never let liveness plumbing kill the poller."""
    try:
        with open(path or HEARTBEAT_FILE, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def main() -> int:
    if not WLL_HOST:
        log.error("WLL_HOST must be set in env (the WeatherLink Live's "
                  "LAN IP address, e.g. 192.168.1.42)")
        return 2
    if not BACKEND_URL or not INGEST_TOKEN:
        log.error("BACKEND_URL and INGEST_TOKEN must be set in env")
        return 2
    log.info("polling http://%s every %ds → %s",
             WLL_HOST, WLL_POLL_SECONDS, BACKEND_URL)
    # The id of the gateway this address last answered for, learned on the
    # first good poll and kept across restarts (2.4, item 2), and the
    # address it was last found at, so a restart after a move does not
    # begin by polling the dead one.
    state = _read_state()
    known_did = state.get("did", "")
    host = starting_host(WLL_HOST, state)
    if host != WLL_HOST:
        log.warning("starting at %s, where gateway %s was last found "
                    "(WLL_HOST is %s)", host, known_did, WLL_HOST)
    last_good = time.monotonic()
    last_sweep = 0.0
    while True:
        # Monotonic clock for cadence: an NTP step backward would stall the
        # wall-clock version (huge sleep), a step forward would compress it.
        t0 = time.monotonic()
        try:
            snapshot = fetch_wll(host)
            last_good = time.monotonic()
            known_did = remember_device(host, snapshot) or known_did
            obs = to_observation(snapshot)
            if obs:
                try:
                    post_observation(obs)
                except urllib.error.HTTPError as e:
                    # Scoped to the ingest POST, and caught BEFORE URLError
                    # (its parent class): a persistent 401/403 is a credential
                    # problem, not connectivity, and labeling it "network
                    # error" every 10s sent operators debugging the wrong
                    # thing. An HTTPError from the WLL fetch above still
                    # falls through to the generic handlers.
                    log.warning("ingest rejected (HTTP %d %s) — check "
                                "INGEST_TOKEN / BACKEND_URL", e.code, e.reason)
                else:
                    log.debug("posted observation: outdoor=%s",
                              obs.get("outdoor"))
            else:
                log.warning("no usable WLL data this tick")
        except urllib.error.URLError as e:
            log.warning("network error: %s", e)
        except Exception:
            log.exception("unexpected error in poll loop")
        # The address has been silent long enough to be worth looking for
        # the gateway elsewhere (2.4, item 2). Only with an id to match,
        # only after the quiet period, and at most once per quiet period
        # after that, so a gateway that is simply off does not turn into a
        # sweep every ten minutes forever.
        quiet = time.monotonic() - last_good
        if (WLL_REDISCOVER and known_did and quiet >= WLL_REDISCOVER_AFTER_S
                and time.monotonic() - last_sweep >= WLL_REDISCOVER_AFTER_S):
            last_sweep = time.monotonic()
            log.warning("no answer from %s for %d minutes", host, quiet // 60)
            # Inside its own guard: the sweep talks to 253 strangers, and
            # one of them answering with something surprising must not
            # end the process (the R6 lesson, for the sweep).
            try:
                moved = find_gateway(known_did, host)
            except Exception:
                log.exception("unexpected error while sweeping for the gateway")
                moved = None
            if moved:
                log.warning("gateway moved from %s to %s, polling there now",
                            host, moved)
                host = moved
                _write_state({"did": known_did, "host": host})
                last_good = time.monotonic()
        _touch_heartbeat()
        # steady cadence — sleep remainder of the window
        time.sleep(max(0.0, WLL_POLL_SECONDS - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
