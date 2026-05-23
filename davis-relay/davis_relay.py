"""Davis VP2 ISS → Zasder Weather backend relay.

Spawns `rtldavis` (https://github.com/lheijst/rtldavis) which owns one
RTL-SDR dongle, tracks the 51-channel FHSS hop pattern, and emits one
text line per decoded 8-byte Davis ISS packet. We parse each packet
via davis_iss.parse(), accumulate fields across packets (Davis rotates
one rotating field per packet ~every 2.5s), and POST a normalized
observation to the backend's /ingest/custom endpoint.

Single-purpose service — runs in its own Docker container, claims the
configured SDR dongle, never shares it (rtldavis retunes the radio
constantly to follow FHSS, so it can't co-exist with rtl_433 on the
same dongle).

Auto-restarts rtldavis on subprocess exit (USB blips, no-signal init
failures). State is in-memory; if the container restarts the next
post is partial until rotating fields refresh.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from davis_iss import parse, parse_line


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("davis-relay")


# ────────────────────── config (env vars) ──────────────────────

BACKEND_URL  = os.environ.get("BACKEND_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
DAVIS_ID     = int(os.environ.get("DAVIS_ID", "1"))         # 1..8 (UI channel)
DAVIS_NAME   = os.environ.get("DAVIS_NAME", "Davis Vantage Pro2 (SDR)")
DAVIS_LOCATION = os.environ.get("DAVIS_LOCATION", "")
DAVIS_RAIN_BUCKET_IN = float(os.environ.get("DAVIS_RAIN_BUCKET_IN", "0.01"))
"""Inches per bucket tip. Standard US VP2 is 0.01"; metric is 0.2 mm
(≈ 0.00787"). Override per your physical sensor."""
DAVIS_RAIN_YEARLY_BASELINE_IN = float(os.environ.get("DAVIS_RAIN_YEARLY_BASELINE_IN", "0"))
DAVIS_WIND_DIR_OFFSET = float(os.environ.get("DAVIS_WIND_DIR_OFFSET", "0"))
RTLDAVIS_BIN = os.environ.get("RTLDAVIS_BIN", "rtldavis")
POST_TIMEOUT = float(os.environ.get("POST_TIMEOUT", "5"))
# How often to POST a coalesced observation. Davis cycles through ~10
# message types in ~25-50s under perfect FHSS conditions; our SDR will
# get maybe 50-80% of those. Posting every 30s gives a stable cadence
# that always has fresh wind + most rotating fields.
POST_INTERVAL_S = float(os.environ.get("POST_INTERVAL_S", "30"))

if not BACKEND_URL or not INGEST_TOKEN:
    log.error("BACKEND_URL and INGEST_TOKEN are required; exiting")
    sys.exit(1)
if not 1 <= DAVIS_ID <= 8:
    log.error("DAVIS_ID must be 1..8 (got %s); exiting", DAVIS_ID)
    sys.exit(1)


# ────────────────────── state ──────────────────────

# Per-station accumulator. Updated on every decoded packet; flushed via
# POST on the timer thread.
_state: dict[str, Any] = {}
_state_lock = threading.Lock()
_rain_baseline: int | None = None   # first-seen 7-bit counter value
_rain_total_clicks_since_baseline: int = 0
_last_rain_count: int | None = None
_stop_event = threading.Event()


def _f_to_c(f: float | None) -> float | None:
    return None if f is None else (f - 32.0) * 5.0 / 9.0


def _dew_point_f(temp_f: float | None, humidity_pct: float | None) -> float | None:
    """Magnus-Tetens dew point. Returns °F to match the backend's field."""
    if temp_f is None or humidity_pct is None or humidity_pct <= 0:
        return None
    t_c = _f_to_c(temp_f)
    a, b = 17.625, 243.04
    gamma = math.log(humidity_pct / 100.0) + (a * t_c) / (b + t_c)
    dp_c = (b * gamma) / (a - gamma)
    return dp_c * 9.0 / 5.0 + 32.0


def _heat_index_f(temp_f: float | None, humidity_pct: float | None) -> float | None:
    """NWS Rothfusz heat index. Returns °F. Below 80°F returns temp_f
    (heat index is only meaningful when it's actually hot)."""
    if temp_f is None or humidity_pct is None:
        return None
    if temp_f < 80.0:
        return temp_f
    t, h = temp_f, humidity_pct
    return (-42.379 + 2.04901523 * t + 10.14333127 * h
            - 0.22475541 * t * h - 6.83783e-3 * t * t
            - 5.481717e-2 * h * h + 1.22874e-3 * t * t * h
            + 8.5282e-4 * t * h * h - 1.99e-6 * t * t * h * h)


def update_state(parsed: dict[str, Any]) -> None:
    """Merge a single parsed Davis packet's fields into the accumulator.
    Wind is in every packet so always wins; rotating fields only update
    when present."""
    global _last_rain_count, _rain_baseline, _rain_total_clicks_since_baseline
    with _state_lock:
        # Sanity: ignore packets from other transmitters on the band.
        if parsed.get("transmitter_id") != DAVIS_ID - 1:
            return

        if "wind_speed_mph" in parsed:
            _state["wind_speed_mph"] = parsed["wind_speed_mph"]
        if "wind_dir_deg" in parsed:
            _state["wind_dir_deg"] = parsed["wind_dir_deg"]
        if "wind_gust_mph" in parsed:
            _state["wind_gust_mph"] = parsed["wind_gust_mph"]
        if "temp_f" in parsed:
            _state["temp_f"] = parsed["temp_f"]
        if "humidity_pct" in parsed:
            _state["humidity_pct"] = parsed["humidity_pct"]
        if "uv_index" in parsed:
            _state["uv_index"] = parsed["uv_index"]
        if "solar_w_m2" in parsed:
            _state["solar_w_m2"] = parsed["solar_w_m2"]
        if "battery_low" in parsed:
            _state["battery_low"] = parsed["battery_low"]

        # Rain count is a 7-bit free-running counter that wraps at 128.
        # On first sight we capture the baseline; on each later reading
        # we add the delta (handling wrap) to the running total.
        if "rain_count" in parsed:
            rc = parsed["rain_count"]
            if _rain_baseline is None:
                _rain_baseline = rc
                _last_rain_count = rc
                log.info("rain baseline captured: %d (rolling 7-bit counter)", rc)
            elif _last_rain_count is not None:
                delta = (rc - _last_rain_count) & 0x7F   # mod 128, handles wrap
                # Sanity: ignore deltas > 50 (= 0.5" in one ~30s window;
                # well over any plausible rain rate).
                if delta <= 50:
                    _rain_total_clicks_since_baseline += delta
                else:
                    log.warning("rejecting implausible rain delta %d "
                                "(last=%s now=%s)", delta, _last_rain_count, rc)
                _last_rain_count = rc


# ────────────────────── HTTP poster ──────────────────────

def _post(payload: dict[str, Any]) -> bool:
    url = BACKEND_URL + "/ingest/custom"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as r:
            if 200 <= r.status < 300:
                return True
            log.warning("backend returned %d for /ingest/custom", r.status)
            return False
    except urllib.error.HTTPError as e:
        log.warning("HTTP %d POSTing: %s", e.code, e.reason)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("network error POSTing: %s", e)
        return False


def build_payload() -> dict[str, Any] | None:
    """Snapshot current state into an /ingest/custom-shaped dict.
    Returns None if we don't have enough fields yet (wind + temp or
    humidity at minimum)."""
    with _state_lock:
        if "wind_speed_mph" not in _state:
            return None
        # Require at least one rotating field — pure wind-only posts
        # would clobber the row's tempf with NULL via UPSERT.
        if "temp_f" not in _state and "humidity_pct" not in _state:
            return None

        # Synthetic MAC matching sdr-relay's scheme: 5D:5D:TT:HH:HH:HH
        # where TT is the per-type tag byte. Davis = 0x05 (next after
        # 0x04 = LaCrosse). HH:HH:HH is the low 3 bytes of DAVIS_ID.
        mac_hex = f"5D5D05{(DAVIS_ID >> 16) & 0xFF:02X}{(DAVIS_ID >> 8) & 0xFF:02X}{DAVIS_ID & 0xFF:02X}"
        payload: dict[str, Any] = {
            "device": {
                "id":       mac_hex,
                "name":     DAVIS_NAME,
                "location": DAVIS_LOCATION or None,
            },
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "source": "davis-vp2-sdr",
        }

        outdoor: dict[str, Any] = {}
        if "temp_f" in _state:
            outdoor["tempf"] = _state["temp_f"]
        if "humidity_pct" in _state:
            outdoor["humidity"] = round(_state["humidity_pct"])
        if "temp_f" in _state and "humidity_pct" in _state:
            outdoor["dew_point_f"] = _dew_point_f(_state["temp_f"], _state["humidity_pct"])
            outdoor["feels_like"]  = _heat_index_f(_state["temp_f"], _state["humidity_pct"])
        if "uv_index" in _state:
            outdoor["uv"] = _state["uv_index"]
        if "solar_w_m2" in _state:
            outdoor["solarradiation"] = _state["solar_w_m2"]
        if outdoor:
            payload["outdoor"] = outdoor

        wind: dict[str, Any] = {}
        if "wind_speed_mph" in _state:
            wind["windspeedmph"] = _state["wind_speed_mph"]
        if "wind_dir_deg" in _state:
            wind["winddir"] = round(_state["wind_dir_deg"])
        if "wind_gust_mph" in _state:
            wind["windgustmph"] = _state["wind_gust_mph"]
        if wind:
            payload["wind"] = wind

        # Yearly rain = baseline + (running clicks × inches-per-click).
        # Only populated once we've seen at least one rain packet to
        # baseline against; otherwise we'd POST baseline-only and
        # clobber the row's yearlyrainin with the wrong value.
        if _rain_baseline is not None and DAVIS_RAIN_YEARLY_BASELINE_IN > 0:
            inches_since_baseline = (_rain_total_clicks_since_baseline
                                     * DAVIS_RAIN_BUCKET_IN)
            payload["rain"] = {
                "yearly_in": round(DAVIS_RAIN_YEARLY_BASELINE_IN
                                    + inches_since_baseline, 3),
            }

        return payload


# ────────────────────── rtldavis subprocess ──────────────────────

def _rtldavis_tr_flag(davis_id: int) -> str:
    """Davis uses ID-as-bitmask in rtldavis (-tr 1 = ID0, -tr 2 = ID1,
    -tr 4 = ID2, etc.). Our DAVIS_ID env is the human "channel" 1..8,
    so subtract 1 then shift left."""
    return str(1 << (davis_id - 1))


def stream_rtldavis() -> None:
    # DEBUG_LISTEN_ALL = listen for every transmitter (-tr 255) and log
    # any decoded but unmatched packets (-u). Lets us confirm the ISS
    # is on the air + the radio works, without depending on having the
    # right DIP-switch ID configured. Disable in production.
    listen_all = os.environ.get("DEBUG_LISTEN_ALL", "").strip() in ("1", "true", "yes")
    backoff = 1
    while not _stop_event.is_set():
        tr_flag = "255" if listen_all else _rtldavis_tr_flag(DAVIS_ID)
        cmd = [RTLDAVIS_BIN, "-tf", "US", "-tr", tr_flag]
        if listen_all:
            cmd.append("-u")
        log.info("starting: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            log.error("rtldavis binary not found at %s — sleeping 30s", RTLDAVIS_BIN)
            _stop_event.wait(30)
            continue
        start_ts = time.time()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if _stop_event.is_set():
                    break
                line = line.rstrip("\n")
                if not line:
                    continue
                # rtldavis prints debug + data lines on the same stream;
                # only the data lines have the 16-hex-char payload.
                parsed_line = parse_line(line)
                if parsed_line is None:
                    if "ChannelIdx" in line or "Hop" in line:
                        # Tuning event — useful for first-boot debugging
                        # but very noisy; log only at DEBUG.
                        log.debug("hop: %s", line)
                    else:
                        log.debug("non-data: %s", line)
                    continue
                raw, counters = parsed_line
                try:
                    parsed = parse(raw, wind_dir_offset_deg=DAVIS_WIND_DIR_OFFSET)
                except ValueError as e:
                    log.warning("parse error: %s (raw=%s)", e, raw.hex())
                    continue
                update_state(parsed)
                # One-shot info log for the first packet so it's obvious
                # in the logs that the link is up.
                if not _state.get("_first_logged"):
                    log.info("first Davis packet decoded: type=%#x fields=%s",
                             (raw[0] >> 4) & 0x0F, list(parsed.keys()))
                    with _state_lock:
                        _state["_first_logged"] = True
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                proc.kill()
        # Exponential backoff on rtldavis exit, capped at 60s.
        if time.time() - start_ts < 5:
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1
        log.warning("rtldavis exited — restarting in %ds", backoff)
        _stop_event.wait(backoff)


# ────────────────────── poster thread ──────────────────────

def poster_loop() -> None:
    log.info("posting to %s every %.0fs", BACKEND_URL, POST_INTERVAL_S)
    while not _stop_event.is_set():
        _stop_event.wait(POST_INTERVAL_S)
        if _stop_event.is_set():
            break
        payload = build_payload()
        if payload is None:
            log.debug("not enough state yet to POST")
            continue
        ok = _post(payload)
        if ok:
            outdoor = payload.get("outdoor", {})
            wind = payload.get("wind", {})
            log.info("posted: tempf=%s hum=%s wind=%s@%s",
                     outdoor.get("tempf"), outdoor.get("humidity"),
                     wind.get("windspeedmph"), wind.get("winddir"))


# ────────────────────── main ──────────────────────

def main() -> int:
    def _shutdown(signum, frame):
        log.info("signal %s → shutting down", signum)
        _stop_event.set()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("davis-relay starting: DAVIS_ID=%d (-tr %s) backend=%s",
             DAVIS_ID, _rtldavis_tr_flag(DAVIS_ID), BACKEND_URL)

    t_post = threading.Thread(target=poster_loop, daemon=True, name="poster")
    t_post.start()
    # stream_rtldavis() runs on the main thread so any unhandled
    # exception terminates the container (Docker restart policy
    # brings us back up).
    stream_rtldavis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
