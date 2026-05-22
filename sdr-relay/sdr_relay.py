"""SDR weather relay.

Listens on two RTL-SDR Blog V4 dongles via rtl_433 subprocesses (one on
433 MHz for AcuRite Atlas, one on 915 MHz for Fine Offset / WS-2000 family
sensors), normalizes each decoded packet
into the backend's /ingest/custom schema, and POSTs.

Replaces the network-level acurite-relay (DNS-hijack of
atlasapi.myacurite.com → captive HTTP proxy) and the AmbientWeather cloud
poller. Data flows sensor → 433/915 MHz RF → SDR → this process → backend.
Hub, AmbientWeather cloud, and AcuRite cloud are all bypassed.

Architecture:
  - One thread per dongle, each pinning rtl_433 to its SDR by serial.
  - Auto-restart if rtl_433 exits (e.g. USB glitch, SDR resets).
  - Atlas message-type coalescer: AcuRite cycles 8 message types each
    carrying a partial field set, so we keep per-id state and post a
    merged observation every ~60s once enough fields have been seen.
  - Fine Offset (WH24 / WH65 / WS80) packets are complete observations;
    posted as-is.
  - Optional indoor pairing: if WH32B_ID is set, its temp/humidity/pressure
    are merged into the outdoor station's posts under the schema's
    `indoor` block (a small backend extension supports this).

Config (env vars; .env.example documents defaults):
  BACKEND_URL          required, e.g. https://weather.zasder.com
  INGEST_TOKEN         required, same value as backend's INGEST_TOKEN
  ATLAS_ID             AcuRite Atlas sensor id (0 to disable; default 0)
  ATLAS_NAME           friendly name (default "AcuRite Atlas (SDR)")
  ATLAS_LOCATION       friendly location string
  ATLAS_SERIAL         RTL-SDR EEPROM serial (default "acurite433")
  WH24_ID              Fine Offset outdoor sensor id (0 to disable)
  WH32B_ID             Fine Offset indoor sensor id (0 to disable indoor merge)
  WS2000_NAME          friendly name for the WS-2000 family station
  WS2000_LOCATION      friendly location
  WS2000_SERIAL        RTL-SDR EEPROM serial (default "ws2000")
  ATLAS_POST_INTERVAL  seconds between coalesced Atlas posts (default 60)
  RTL433_BIN           rtl_433 binary path (default "rtl_433")
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
from collections import deque
from datetime import datetime, timezone
from typing import Any

# ───────────────────────── configuration ─────────────────────────

BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
RTL433_BIN = os.environ.get("RTL433_BIN", "rtl_433")

ATLAS_ID = int(os.environ.get("ATLAS_ID", "0") or 0)
ATLAS_NAME = os.environ.get("ATLAS_NAME", "AcuRite Atlas (SDR)")
ATLAS_LOCATION = os.environ.get("ATLAS_LOCATION", "")
ATLAS_SERIAL = os.environ.get("ATLAS_SERIAL", "acurite433")
ATLAS_POST_INTERVAL = int(os.environ.get("ATLAS_POST_INTERVAL", "60") or 60)
# AcuRite Atlas units are known to develop UV/lux photodiode failures —
# the firmware keeps broadcasting a stuck value (typically the sensor's
# max cap, e.g. uv=4 lux=79550) even at night when both should be 0.
# Set ATLAS_UV_LUX_BROKEN=1 to mask both fields as None instead of
# posting the bogus stuck values to the backend.
ATLAS_UV_LUX_BROKEN = os.environ.get("ATLAS_UV_LUX_BROKEN", "0").strip() not in ("0", "false", "no", "")

WH24_ID = int(os.environ.get("WH24_ID", "0") or 0)
WH32B_ID = int(os.environ.get("WH32B_ID", "0") or 0)
WS2000_NAME = os.environ.get("WS2000_NAME", "WS-2000 (SDR)")
WS2000_LOCATION = os.environ.get("WS2000_LOCATION", "")
WS2000_SERIAL = os.environ.get("WS2000_SERIAL", "ws2000")


# Optional LaCrosse-TH2 temp/humidity sensor (433 MHz). Single-packet
# protocol — no coalescing needed. Set LACROSSE_ID=0 to skip.
LACROSSE_ID = int(os.environ.get("LACROSSE_ID", "0") or 0)
LACROSSE_NAME = os.environ.get("LACROSSE_NAME", "LaCrosse (SDR)")
LACROSSE_LOCATION = os.environ.get("LACROSSE_LOCATION", "")

FORWARD_TIMEOUT = float(os.environ.get("FORWARD_TIMEOUT", "5") or 5)


# Rain baselining. rtl_433 emits the sensor's lifetime cumulative rain
# counter — which has no relation to "rain this year". To get a useful
# yearlyrainin, we calibrate against whatever value you've been tracking
# elsewhere (e.g., AWN's yearlyrainin at the moment you deploy). After
# baselining we just track deltas. State persists to /data/rain_state.json
# so restarts don't lose calibration. Set to 0 / empty to skip.
ATLAS_RAIN_YEARLY_BASELINE_IN = float(os.environ.get("ATLAS_RAIN_YEARLY_BASELINE_IN", "0") or 0)
WS2000_RAIN_YEARLY_BASELINE_IN = float(os.environ.get("WS2000_RAIN_YEARLY_BASELINE_IN", "0") or 0)
RAIN_STATE_PATH = os.environ.get("RAIN_STATE_PATH", "/data/rain_state.json")
# Sanity ceiling on rain delta between consecutive packets. Fine Offset
# decoders occasionally emit huge spurious values (counter jumps to 1000+
# inches) which, without a guard, get banked into yearly. Even a Hurricane-
# Harvey-class event delivers ~12"/hour ≈ ~3"/min, well under this default.
# Set to 0 to disable the check entirely.
MAX_RAIN_DELTA_IN = float(os.environ.get("MAX_RAIN_DELTA_IN", "5.0") or 5.0)
# Rolling window (seconds) for computing wind gust as the max wind_avg.
# AcuRite Atlas firmware doesn't broadcast a gust value — the hub used to
# compute it from a sliding window of wind_avg samples. 10 minutes matches
# the NWS gust-reporting convention and what the hub did internally.
WIND_GUST_WINDOW_S = int(os.environ.get("WIND_GUST_WINDOW_S", "600") or 600)

# Discovery survey: tally the long-tail of nearby RF devices (neighbors'
# weather stations, TPMS from passing cars, garage remotes, utility meters,
# etc). Default behavior is *local-only* — dedupe state lives in
# /data/discoveries.json on the Pi, never leaves the LAN. This is the
# privacy-respecting default: neighbors' RF traffic is hyperlocal data
# that shouldn't end up in a shared cloud DB.
#
# Set DISCOVERY_FORWARD_TO_BACKEND=1 to ALSO post each (rate-limited)
# sighting to the backend's /ingest/discovery — useful when you want the
# survey queryable from anywhere (e.g. your single-tenant deployment).
DISCOVERY_ENABLED = os.environ.get("DISCOVERY_ENABLED", "1").strip() not in ("0", "false", "no", "")
DISCOVERY_FORWARD_TO_BACKEND = os.environ.get("DISCOVERY_FORWARD_TO_BACKEND", "0").strip() not in ("0", "false", "no", "")
DISCOVERY_RATE_LIMIT_S = int(os.environ.get("DISCOVERY_RATE_LIMIT_S", "60") or 60)
DISCOVERY_STATE_PATH = os.environ.get("DISCOVERY_STATE_PATH", "/data/discoveries.json")
DISCOVERY_SAVE_INTERVAL_S = int(os.environ.get("DISCOVERY_SAVE_INTERVAL_S", "30") or 30)

# ───────────────────────── logging ─────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sdr-relay")


# ───────────────────────── unit conversions ─────────────────────────

def _finite(v: float | int | None) -> float | None:
    """Coerce to a finite float or None. Rejects NaN/inf — rtl_433
    decoders occasionally emit these on partial frames, and they brick
    the backend's JSON serialization downstream."""
    if v is None: return None
    try: f = float(v)
    except (TypeError, ValueError): return None
    return f if math.isfinite(f) else None

def c_to_f(c: float | None) -> float | None:
    c = _finite(c)
    return round(c * 9 / 5 + 32, 1) if c is not None else None

def f_round(f: float | None) -> float | None:
    f = _finite(f)
    return round(f, 1) if f is not None else None

def mm_to_in(mm: float | None) -> float | None:
    mm = _finite(mm)
    return round(mm * 0.0393701, 3) if mm is not None else None

def ms_to_mph(ms: float | None) -> float | None:
    ms = _finite(ms)
    return round(ms * 2.23694, 1) if ms is not None else None

def hpa_to_inhg(hpa: float | None) -> float | None:
    hpa = _finite(hpa)
    return round(hpa * 0.02953, 2) if hpa is not None else None

def lux_to_wm2(lux: float | None) -> float | None:
    # AcuRite-community accepted conversion for outdoor light: ~126 lux/W/m².
    lux = _finite(lux)
    return round(lux / 126.0, 1) if lux is not None else None


def dew_point_f(temp_f: float | None, humidity_pct: float | None) -> float | None:
    """Magnus-Tetens dew point approximation. ±0.4°C accuracy in the
    -40°C to 50°C / 1-100% RH range, which covers any habitable climate.
    rtl_433 doesn't emit dew point — the AcuRite hub and AWN cloud were
    both computing it from temp+humidity using essentially this formula."""
    if temp_f is None or humidity_pct is None or humidity_pct <= 0:
        return None
    t_c = (temp_f - 32) * 5 / 9
    a, b = 17.625, 243.04
    gamma = math.log(humidity_pct / 100.0) + (a * t_c) / (b + t_c)
    t_dp_c = (b * gamma) / (a - gamma)
    return round(t_dp_c * 9 / 5 + 32, 1)


def heat_index_f(temp_f: float | None, humidity_pct: float | None) -> float | None:
    """NWS Rothfusz heat index. Only meaningful above ~80°F + ~40% RH;
    returns the air temp itself below those thresholds (per NWS convention)
    so callers always get a usable 'feels like' number."""
    if temp_f is None or humidity_pct is None:
        return None
    if temp_f < 80:
        return temp_f
    t, r = temp_f, humidity_pct
    # Rothfusz regression coefficients
    hi = (-42.379 + 2.04901523*t + 10.14333127*r
          - 0.22475541*t*r - 6.83783e-3*t*t - 5.481717e-2*r*r
          + 1.22874e-3*t*t*r + 8.5282e-4*t*r*r - 1.99e-6*t*t*r*r)
    # At low humidity the Rothfusz polynomial undershoots — it can return
    # values BELOW the air temp, which is physically impossible. NWS
    # convention is to clamp to the air temp in that case (heat index can
    # never be cooler than the actual temperature).
    return round(max(hi, temp_f), 1)


# ───────────────────────── rain accumulator ─────────────────────────
# rtl_433 emits a lifetime cumulative rain counter that has no relationship
# to "rain this year" — it only resets on sensor power-cycle / battery
# change. To produce a useful yearlyrainin, we calibrate once against a
# known-good value (typically the AWN's yearlyrainin at deploy time) and
# track deltas from there. Resets are detected (counter goes down ⇒ sensor
# rebooted) and the prior accumulated value is banked into extra_offset
# so the running total doesn't snap back to zero.
#
# Persisted state schema (one entry per sensor_key):
#   {"baseline_in":      <calibration target — yearly rain at baseline>,
#    "base_offset_in":   baseline_in - first_counter_we_saw,
#    "extra_offset_in":  rolling sum of "lost" counters across resets,
#    "last_counter_in":  most recent raw counter we saw,
#    "baselined_at":     ISO timestamp we first computed base_offset}

_rain_state: dict[str, dict] = {}
_rain_state_lock = threading.Lock()

# Per-sensor sliding window of (epoch_seconds, wind_mph) tuples for gust
# computation. In-memory only — gust readings are inherently real-time
# (a 10-minute window can't be reconstructed from disk after a restart;
# we simply re-fill it as packets arrive over the next 10 minutes).
_wind_window: dict[str, deque] = {}
_wind_window_lock = threading.Lock()


def update_wind_sample(sensor_key: str, wind_mph: float | None) -> None:
    """Append a wind reading to the per-sensor rolling window and prune
    entries older than the configured window."""
    if wind_mph is None:
        return
    now = time.time()
    cutoff = now - WIND_GUST_WINDOW_S
    with _wind_window_lock:
        dq = _wind_window.setdefault(sensor_key, deque())
        dq.append((now, float(wind_mph)))
        while dq and dq[0][0] < cutoff:
            dq.popleft()


def computed_gust_mph(sensor_key: str) -> float | None:
    """Max wind_avg seen in the rolling window — meteorological 'gust'.
    Returns None until the window has at least one sample (typical first
    ~20s after restart)."""
    cutoff = time.time() - WIND_GUST_WINDOW_S
    with _wind_window_lock:
        dq = _wind_window.get(sensor_key)
        if not dq:
            return None
        # Prune at read time too — a quiet sensor could leave stale data
        # sitting in the deque otherwise.
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        if not dq:
            return None
        return round(max(v for _, v in dq), 1)
# Per-sensor counter of consecutive near-zero packets. Required to debounce
# real sensor resets vs single-packet decoder glitches. Tracked in-memory
# only — on restart we start fresh, which is correct (a real reset would
# still produce near-zero packets continuously).
_rain_low_runs: dict[str, int] = {}
_RAIN_RESET_THRESHOLD_IN = 1.0  # below this is "near zero" — possibly reset
_RAIN_RESET_RUN_LENGTH = 5      # this many consecutive near-zero packets ⇒ real


def _load_rain_state() -> None:
    p = os.path.dirname(RAIN_STATE_PATH)
    try:
        with open(RAIN_STATE_PATH, "r", encoding="utf-8") as f:
            _rain_state.update(json.load(f))
        log.info("loaded rain state from %s: %s",
                 RAIN_STATE_PATH, list(_rain_state.keys()))
    except FileNotFoundError:
        log.info("no prior rain state at %s — fresh baseline on first packet",
                 RAIN_STATE_PATH)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read rain state (%s); starting fresh", e)


def _save_rain_state() -> None:
    try:
        os.makedirs(os.path.dirname(RAIN_STATE_PATH), exist_ok=True)
        tmp = RAIN_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_rain_state, f, indent=2)
        os.replace(tmp, RAIN_STATE_PATH)
    except OSError as e:
        log.warning("could not persist rain state: %s", e)


def compute_yearly_rain(sensor_key: str, baseline_in: float | None,
                        current_counter_in: float | None) -> float | None:
    """Convert a raw rtl_433 cumulative rain counter into yearly rain
    calibrated against `baseline_in`. Returns None when no baseline is
    configured or the sensor isn't reporting rain. Persists state on every
    call so resets and offsets survive container restarts."""
    if not baseline_in or current_counter_in is None:
        return None
    with _rain_state_lock:
        state = _rain_state.get(sensor_key) or {}
        base_offset = state.get("base_offset_in")
        extra_offset = state.get("extra_offset_in", 0.0)
        last_counter = state.get("last_counter_in")

        if base_offset is None:
            # First packet for this sensor — calibrate.
            base_offset = baseline_in - current_counter_in
            state = {
                "baseline_in": baseline_in,
                "base_offset_in": base_offset,
                "extra_offset_in": 0.0,
                "last_counter_in": current_counter_in,
                "baselined_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            }
            log.info("rain[%s] baselined to %.3f in (counter=%.3f, offset=%.3f)",
                     sensor_key, baseline_in, current_counter_in, base_offset)
        elif last_counter is not None and current_counter_in < last_counter - 0.01:
            # Counter went down. Two possibilities:
            #   (a) Real sensor reset (battery change / power loss): counter
            #       drops near-zero and stays there. We need to bank
            #       last_counter into extra_offset to preserve the yearly
            #       total across the reset.
            #   (b) Spurious decoder glitch: Fine Offset sensors can emit
            #       a single low rain_mm value during RF retries or low
            #       battery. Banking on a single such packet causes yearly
            #       to inflate by ~lifetime_counter every time it happens.
            # Debounce: require both "near zero" (< _RAIN_RESET_THRESHOLD_IN)
            # AND "sustained" (_RAIN_RESET_RUN_LENGTH consecutive packets)
            # before treating it as case (a). Otherwise skip the packet
            # entirely and continue reporting the pre-glitch yearly value.
            if current_counter_in < _RAIN_RESET_THRESHOLD_IN:
                runs = _rain_low_runs.get(sensor_key, 0) + 1
                _rain_low_runs[sensor_key] = runs
                if runs >= _RAIN_RESET_RUN_LENGTH:
                    extra_offset += last_counter
                    state["extra_offset_in"] = extra_offset
                    state["last_counter_in"] = current_counter_in
                    _rain_low_runs[sensor_key] = 0
                    log.warning("rain[%s] confirmed sensor reset after %d "
                                "consecutive near-zero packets; banked %.3f in",
                                sensor_key, runs, last_counter)
                    _rain_state[sensor_key] = state
                    _save_rain_state()
                    return round(current_counter_in + base_offset + extra_offset, 3)
                log.info("rain[%s] possible reset (run %d/%d), skipping packet",
                         sensor_key, runs, _RAIN_RESET_RUN_LENGTH)
            else:
                log.warning("rain[%s] spurious decrease (last=%.3f, now=%.3f); "
                            "skipping packet", sensor_key, last_counter,
                            current_counter_in)
            return round(last_counter + base_offset + extra_offset, 3)
        else:
            # Counter went up (or stayed put). Sanity-check the magnitude
            # before accepting — a spurious huge jump (decoder glitch where
            # rain_mm reads as some giant number) would otherwise bake
            # forever into yearly. The threshold is configurable so the
            # operator can tune for actual local extremes.
            if last_counter is not None and MAX_RAIN_DELTA_IN > 0:
                delta = current_counter_in - last_counter
                if delta > MAX_RAIN_DELTA_IN:
                    log.warning("rain[%s] implausible jump (last=%.3f, "
                                "now=%.3f, +%.3f in exceeds MAX_RAIN_DELTA_IN="
                                "%.1f); skipping packet", sensor_key,
                                last_counter, current_counter_in, delta,
                                MAX_RAIN_DELTA_IN)
                    return round(last_counter + base_offset + extra_offset, 3)
            # Clear in-flight "possible reset" run since the sensor's fine.
            _rain_low_runs[sensor_key] = 0

        state["last_counter_in"] = current_counter_in
        _rain_state[sensor_key] = state
        _save_rain_state()
    return round(current_counter_in + base_offset + extra_offset, 3)


# ───────────────────────── MAC synthesis ─────────────────────────

def sensor_mac(sensor_type_byte: int, sensor_id: int) -> str:
    """Build a deterministic 12-hex MAC from a sensor type tag + RF id.
    SDR-sourced devices use the 5D:5D prefix (locally-administered MAC
    range, mnemonic for 'SDR') so they don't collide with real hardware
    MACs in the devices table."""
    low3 = sensor_id & 0xFFFFFF
    return (f"5D:5D:{sensor_type_byte:02X}:"
            f"{(low3 >> 16) & 0xFF:02X}:"
            f"{(low3 >> 8) & 0xFF:02X}:"
            f"{low3 & 0xFF:02X}")


# ───────────────────────── HTTP forwarders ─────────────────────────

def _post(url: str, payload: dict[str, Any]) -> bool:
    """Best-effort JSON POST with bearer auth. Returns True on success."""
    if not BACKEND_URL or not INGEST_TOKEN:
        log.warning("backend not configured; dropping payload")
        return False
    req = urllib.request.Request(
        f"{BACKEND_URL}{url}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {INGEST_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT) as resp:
            if resp.status >= 400:
                log.warning("backend %s returned %s", url, resp.status)
                return False
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("post %s failed: %s", url, e)
        return False


def post_observation(mac: str, name: str, location: str, source: str,
                     ts_iso: str, fields: dict[str, dict]) -> None:
    """POST a normalized observation to the backend's /ingest/custom.
    `fields` is the standard {outdoor, indoor, wind, rain, pressure} dict;
    None values inside each subdict are stripped to keep the payload small."""
    payload = {
        "device": {
            "id": mac.replace(":", ""),
            "name": name,
            "location": location or None,
        },
        "timestamp_utc": ts_iso,
        "source": source,
    }
    for key in ("outdoor", "indoor", "wind", "rain", "pressure"):
        block = {k: v for k, v in (fields.get(key) or {}).items() if v is not None}
        if block:
            payload[key] = block
    if _post("/ingest/custom", payload):
        log.info("posted %s (%s)", name, source)



# Discovery state — keyed by "<model>:<id>" so the JSON file is easy to
# eyeball with `jq`. Each entry tracks first_seen / last_seen / count and
# stashes the very first packet payload as a sample (so you can inspect
# what e.g. a TPMS frame actually looks like). State persists to a volume
# so survey continuity survives container restarts.
_discovery_state: dict[str, dict] = {}
# Cloud-forwarding rate-limit (only used when DISCOVERY_FORWARD_TO_BACKEND=1).
_discovery_last_posted: dict[tuple[str, str], float] = {}
_discovery_lock = threading.Lock()
_discovery_last_saved = 0.0


def _load_discovery_state() -> None:
    try:
        with open(DISCOVERY_STATE_PATH, "r", encoding="utf-8") as f:
            _discovery_state.update(json.load(f))
        log.info("loaded %d discovery records from %s",
                 len(_discovery_state), DISCOVERY_STATE_PATH)
    except FileNotFoundError:
        log.info("no prior discovery state at %s — fresh survey",
                 DISCOVERY_STATE_PATH)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read discovery state (%s); starting fresh", e)


def _save_discovery_state() -> None:
    try:
        os.makedirs(os.path.dirname(DISCOVERY_STATE_PATH), exist_ok=True)
        tmp = DISCOVERY_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_discovery_state, f, indent=2)
        os.replace(tmp, DISCOVERY_STATE_PATH)
    except OSError as e:
        log.warning("could not persist discovery state: %s", e)


def record_discovery(packet: dict[str, Any]) -> None:
    """Update local discovery state for a decoded packet, and (if opted in)
    forward to the backend's /ingest/discovery as well. Always called for
    every packet route() sees, regardless of whether we route the packet
    for normal observation handling."""
    if not DISCOVERY_ENABLED:
        return
    model = str(packet.get("model") or "").strip()
    if not model:
        return
    sensor_id = str(packet.get("id") if packet.get("id") is not None else "?")
    key = f"{model}:{sensor_id}"
    now = time.time()
    now_ms = int(now * 1000)

    global _discovery_last_saved
    with _discovery_lock:
        existing = _discovery_state.get(key)
        if existing:
            existing["last_seen_ms"] = now_ms
            existing["seen_count"] = existing.get("seen_count", 0) + 1
        else:
            _discovery_state[key] = {
                "model": model,
                "id": sensor_id,
                "first_seen_ms": now_ms,
                "last_seen_ms": now_ms,
                "seen_count": 1,
                "sample": packet,
            }
        # Batched save — don't write the JSON every single packet (would
        # cause excessive disk I/O on a busy 915 MHz band).
        if now - _discovery_last_saved > DISCOVERY_SAVE_INTERVAL_S:
            _save_discovery_state()
            _discovery_last_saved = now

    # Optional cloud forwarding, rate-limited per (model, id).
    if DISCOVERY_FORWARD_TO_BACKEND:
        rl_key = (model, sensor_id)
        with _discovery_lock:
            last = _discovery_last_posted.get(rl_key, 0.0)
            if now - last < DISCOVERY_RATE_LIMIT_S:
                return
            _discovery_last_posted[rl_key] = now
        _post("/ingest/discovery", packet)


# ───────────────────────── Atlas coalescer ─────────────────────────
# AcuRite Atlas rotates through 8 message types (message_type 1..8). Each
# type carries a partial set of fields:
#   types 1, 5  → wind_avg_mph
#   type 2      → wind_dir, rainfall
#   types 3, 4  → temperature_F, humidity
#   type 7      → wind_avg_mph, uv, lux
#   type 8      → wind_max_mph, wind_max_dir
# We accumulate latest-value per field and post a merged observation
# every ATLAS_POST_INTERVAL seconds. Stale fields are kept around — we
# only refresh them when new packets arrive.

_atlas_state: dict[str, Any] = {}
_atlas_lock = threading.Lock()
_atlas_last_post = 0.0


def update_atlas(pkt: dict[str, Any]) -> None:
    """Merge fresh fields from one Atlas packet into the per-sensor state."""
    if pkt.get("id") != ATLAS_ID:
        return
    with _atlas_lock:
        # rtl_433 emits fahrenheit directly for Atlas; humidity is %.
        for src, dst in (("temperature_F", "tempf"),
                         ("humidity", "humidity"),
                         ("wind_avg_mi_h", "wind_avg_mph"),
                         ("wind_max_mi_h", "wind_gust_mph"),
                         ("wind_dir_deg", "wind_dir"),
                         ("rain_in", "rain_daily_in"),
                         ("uv", "uv"),
                         ("lux", "lux")):
            if src in pkt and pkt[src] is not None:
                _atlas_state[dst] = pkt[src]
        if "battery_ok" in pkt:
            _atlas_state["battery_ok"] = bool(pkt["battery_ok"])
        _atlas_state["_last_pkt_ts"] = time.time()
    # Feed the gust-computation window — this sensor's firmware doesn't
    # broadcast wind_max so we synthesize gust from rolling max(wind_avg).
    if pkt.get("wind_avg_mi_h") is not None:
        update_wind_sample("atlas", pkt["wind_avg_mi_h"])


def maybe_flush_atlas() -> None:
    """If enough time has passed and we have data, post a merged observation."""
    global _atlas_last_post
    now = time.time()
    if now - _atlas_last_post < ATLAS_POST_INTERVAL:
        return
    with _atlas_lock:
        if not _atlas_state.get("_last_pkt_ts"):
            return
        # Need at least one of these to be worth posting
        if all(_atlas_state.get(k) is None for k in ("tempf", "wind_avg_mph", "uv")):
            return
        state = dict(_atlas_state)
    # state["rain_daily_in"] is misnamed for legacy reasons — rtl_433's
    # rain_in for Acurite-Atlas is actually the sensor's lifetime cumulative
    # counter. Feed it through compute_yearly_rain to get a calibrated
    # yearlyrainin; daily/hourly buckets are TODO (need midnight accumulator).
    yearly_in = compute_yearly_rain("atlas", ATLAS_RAIN_YEARLY_BASELINE_IN,
                                    state.get("rain_daily_in"))
    tempf = f_round(state.get("tempf"))
    humidity = state.get("humidity")
    # If the Atlas's UV/lux sensor is known-broken, mask those fields
    # rather than reporting bogus stuck values to the backend.
    uv_val = None if ATLAS_UV_LUX_BROKEN else state.get("uv")
    solar_val = None if ATLAS_UV_LUX_BROKEN else lux_to_wm2(state.get("lux"))
    fields = {
        "outdoor": {
            "tempf": tempf,
            "humidity": humidity,
            "dew_point_f": dew_point_f(tempf, humidity),
            "feels_like": heat_index_f(tempf, humidity),
            "uv": uv_val,
            "solar_wm2": solar_val,
        },
        "wind": {
            "speed_mph": state.get("wind_avg_mph"),
            # Prefer the sensor-reported gust (type 8 packets) when present;
            # fall back to rolling-window max for firmwares (like this one)
            # that don't broadcast type 8 at all.
            "gust_mph": state.get("wind_gust_mph") or computed_gust_mph("atlas"),
            "direction": state.get("wind_dir"),
        },
        "rain": {"yearly_in": yearly_in},
    }
    # Atlas outdoor sensor has no indoor or pressure sensor of its own —
    # the Atlas hub had its own internal baro but the SDR can't reach it.
    # If a WH32B is configured we borrow its values, since indoor temp/
    # humidity/pressure are property-wide constants (one indoor sensor's
    # readings apply equally to any outdoor station at the same house).
    if WH32B_ID:
        with _wh32b_lock:
            ind = dict(_wh32b_state)
        if ind.get("_last_pkt_ts"):
            fields["indoor"] = {
                "tempf": ind.get("tempf"),
                "humidity": ind.get("humidity"),
                "pressure_inhg": ind.get("pressure_inhg"),
            }
    ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    mac = sensor_mac(0x01, ATLAS_ID)
    post_observation(mac, ATLAS_NAME, ATLAS_LOCATION,
                     "acurite-atlas-sdr", ts, fields)
    _atlas_last_post = now


# ───────────────────────── Fine Offset / WS-2000 ─────────────────────────

_wh32b_state: dict[str, Any] = {}
_wh32b_lock = threading.Lock()


def update_wh32b(pkt: dict[str, Any]) -> None:
    """Track latest indoor values so we can merge them into WH24 posts."""
    if pkt.get("id") != WH32B_ID:
        return
    with _wh32b_lock:
        _wh32b_state["tempf"] = c_to_f(pkt.get("temperature_C"))
        _wh32b_state["humidity"] = pkt.get("humidity")
        _wh32b_state["pressure_inhg"] = hpa_to_inhg(pkt.get("pressure_hPa"))
        _wh32b_state["_last_pkt_ts"] = time.time()


def handle_wh24(pkt: dict[str, Any]) -> None:
    """Each WH24/WH65/WS80 packet is a complete observation — post it
    immediately. Merges in the latest WH32B indoor values if configured."""
    if pkt.get("id") != WH24_ID:
        return
    # WH24's rain_mm is the lifetime cumulative counter — calibrate it
    # against the WS-2000 yearly baseline (typically taken from AWN at
    # deploy time).
    cumulative_in = mm_to_in(pkt.get("rain_mm"))
    yearly_in = compute_yearly_rain("ws2000", WS2000_RAIN_YEARLY_BASELINE_IN,
                                    cumulative_in)
    tempf = c_to_f(pkt.get("temperature_C"))
    humidity = pkt.get("humidity")
    fields: dict[str, dict] = {
        "outdoor": {
            "tempf": tempf,
            "humidity": humidity,
            "dew_point_f": dew_point_f(tempf, humidity),
            "feels_like": heat_index_f(tempf, humidity),
            "uv": pkt.get("uvi"),
            "solar_wm2": lux_to_wm2(pkt.get("light_lux")),
        },
        "wind": {
            "speed_mph": ms_to_mph(pkt.get("wind_avg_m_s")),
            "gust_mph": ms_to_mph(pkt.get("wind_max_m_s")),
            "direction": pkt.get("wind_dir_deg"),
        },
        "rain": {"yearly_in": yearly_in},
    }
    if WH32B_ID:
        with _wh32b_lock:
            ind = dict(_wh32b_state)
        if ind.get("_last_pkt_ts"):
            fields["indoor"] = {
                "tempf": ind.get("tempf"),
                "humidity": ind.get("humidity"),
                "pressure_inhg": ind.get("pressure_inhg"),
            }
    ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    mac = sensor_mac(0x02, WH24_ID)
    post_observation(mac, WS2000_NAME, WS2000_LOCATION,
                     "fineoffset-wh24-sdr", ts, fields)


# ───────────────────────── LaCrosse-TH2 (temp/humidity only) ─────────────────────────

def handle_lacrosse(pkt: dict[str, Any]) -> None:
    """LaCrosse TX29/TX35 family (rtl_433 model 'LaCrosse-TH2'). Carries
    only temperature + humidity; we compute dew point + feels-like from
    those and post as a partial observation."""
    if pkt.get("id") != LACROSSE_ID:
        return
    tempf = c_to_f(pkt.get("temperature_C"))
    humidity = pkt.get("humidity")
    fields: dict[str, dict] = {
        "outdoor": {
            "tempf": tempf,
            "humidity": humidity,
            "dew_point_f": dew_point_f(tempf, humidity),
            "feels_like": heat_index_f(tempf, humidity),
        },
    }
    ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    mac = sensor_mac(0x04, LACROSSE_ID)
    post_observation(mac, LACROSSE_NAME, LACROSSE_LOCATION,
                     "lacrosse-th2-sdr", ts, fields)


# ───────────────────────── packet router ─────────────────────────

def route(pkt: dict[str, Any]) -> None:
    """Dispatch one decoded rtl_433 packet to the right handler. Every
    packet is also forwarded to /ingest/discovery (rate-limited) so the
    backend can tally what's on the airwaves regardless of whether we
    recognize/route it."""
    record_discovery(pkt)  # always — captures the long-tail survey locally
    model = pkt.get("model", "")
    if model == "Acurite-Atlas" and ATLAS_ID:
        update_atlas(pkt)
        maybe_flush_atlas()
    elif model in ("Fineoffset-WH24", "Fineoffset-WH65B", "Fineoffset-WS80") and WH24_ID:
        handle_wh24(pkt)
    elif model == "Fineoffset-WH32B" and WH32B_ID:
        update_wh32b(pkt)
    elif model == "LaCrosse-TH2" and LACROSSE_ID:
        handle_lacrosse(pkt)
    # Other models are passed only through post_discovery above.


# ───────────────────────── rtl_433 subprocess wrappers ─────────────────────────

def stream_rtl433(serial: str, freq_hz: str, decoders: list[str] | None,
                  label: str, stop_event: threading.Event) -> None:
    """Run one rtl_433 subprocess, parse JSON lines, route to handlers.
    Auto-restarts on exit with backoff so a USB blip doesn't kill us."""
    backoff = 1
    while not stop_event.is_set():
        cmd = [RTL433_BIN, "-d", f"serial={serial}", "-f", freq_hz, "-F", "json"]
        for d in (decoders or []):
            cmd.extend(["-R", d])
        log.info("[%s] starting: %s", label, " ".join(cmd))
        try:
            # Capture stderr so failed starts are visible in our logs
            # instead of silently restart-looping forever.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            log.error("[%s] rtl_433 binary not found (RTL433_BIN=%s)",
                      label, RTL433_BIN)
            stop_event.wait(30)
            continue
        start_ts = time.time()
        first_packet_seen = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if stop_event.is_set(): break
                line = line.strip()
                if not line: continue
                try:
                    pkt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                first_packet_seen = True
                try:
                    route(pkt)
                except Exception as e:  # never let one bad packet kill the thread
                    log.exception("[%s] handler raised: %s", label, e)
        finally:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill()
            err = ""
            try: err = proc.stderr.read() if proc.stderr else ""
            except Exception: pass
        if stop_event.is_set():
            break
        # If the process exited very quickly without ever producing a
        # packet, surface stderr so the user can see *why*, and back off
        # harder to avoid a CPU-eating restart-loop. If it ran for at
        # least 30s it was probably a transient SDR hiccup — reset backoff.
        ran_for = time.time() - start_ts
        if not first_packet_seen and ran_for < 2:
            log.error("[%s] rtl_433 exited after %.2fs with no packets. "
                      "stderr (last 800 chars):\n%s",
                      label, ran_for, (err or "").strip()[-800:])
            backoff = min(max(backoff * 2, 5), 60)
        else:
            backoff = 5 if ran_for < 30 else 1
        log.warning("[%s] rtl_433 exited; restarting in %ss", label, backoff)
        stop_event.wait(backoff)


def atlas_flusher(stop_event: threading.Event) -> None:
    """Backup flusher in case the 433 stream goes quiet — keeps the Atlas
    flush cadence honest even if no new packets arrive."""
    while not stop_event.is_set():
        stop_event.wait(5)
        if stop_event.is_set(): break
        try: maybe_flush_atlas()
        except Exception:
            log.exception("atlas flusher error")


# ───────────────────────── main ─────────────────────────


def main() -> int:
    if not BACKEND_URL or not INGEST_TOKEN:
        log.error("BACKEND_URL and INGEST_TOKEN must be set")
        return 2

    _load_rain_state()
    _load_discovery_state()
    stop = threading.Event()

    def _shutdown(signum, frame):
        log.info("signal %s → shutting down", signum)
        stop.set()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    threads: list[threading.Thread] = []

    if ATLAS_ID or LACROSSE_ID:
        # No decoder filter — rtl_433 picks up ~150 protocols at 433 MHz
        # including TPMS, garage remotes, security sensors, neighbors'
        # weather stations, etc. The discovery survey captures them all
        # for inspection at http://<pi>:8080/. Atlas/LaCrosse handlers
        # in route() guard by model + sensor id, so unrelated packets
        # are passed straight through to discovery without being routed.
        # Symmetrical with the 915 MHz dongle which has always been open.
        threads.append(threading.Thread(
            target=stream_rtl433,
            args=(ATLAS_SERIAL, "433.92M", None, "atlas-433", stop),
            daemon=True, name="atlas-433"))
        threads.append(threading.Thread(
            target=atlas_flusher, args=(stop,),
            daemon=True, name="atlas-flusher"))
        log.info("Atlas enabled: id=%d serial=%s", ATLAS_ID, ATLAS_SERIAL)
    else:
        log.info("Atlas disabled (ATLAS_ID unset)")

    if WH24_ID or WH32B_ID:
        # 915 MHz stream — WH24 outdoor and/or WH32B indoor.
        threads.append(threading.Thread(
            target=stream_rtl433,
            args=(WS2000_SERIAL, "915M", None, "ws2000-915", stop),
            daemon=True, name="ws2000-915"))
        log.info("915 MHz enabled: serial=%s wh24=%d wh32b=%d",
                 WS2000_SERIAL, WH24_ID, WH32B_ID)

    for t in threads:
        t.start()

    # Block until signalled
    try:
        while not stop.is_set():
            stop.wait(60)
    except KeyboardInterrupt:
        stop.set()

    for t in threads:
        t.join(timeout=5)
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
