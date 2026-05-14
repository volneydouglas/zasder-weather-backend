"""Acurite hub relay.

Listens on 80 (HTTP) and 443 (HTTPS, self-signed cert, TLS 1.0+) for the
Wunderground-format POST that an Acurite Atlas/Access hub sends to its
configured server (after a DNS hijack of atlasapi.myacurite.com → this host).

Each request is:
  * parsed as URL-encoded query params (the body is empty; everything's in path)
  * mapped to a normalized observation dict
  * appended as JSONL to /data/observations.jsonl
  * also written verbatim as JSONL to /data/raw.jsonl for debugging
  * (later) forwarded to weather.zasder.com via /ingest/custom/<token>

Always responds 200 OK with empty body so the hub considers the upload
successful and doesn't retry-storm us."""
from __future__ import annotations

import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CERT_DIR = os.environ.get("CERT_DIR", "/certs")
RAW_LOG = os.path.join(DATA_DIR, "raw.jsonl")
OBS_LOG = os.path.join(DATA_DIR, "observations.jsonl")

# Optional forwarder: if BACKEND_URL + INGEST_TOKEN are set, every parsed
# observation is POSTed to <BACKEND_URL>/ingest/custom/<INGEST_TOKEN>.
# JSONL captures still happen unconditionally so a backend outage doesn't
# lose data.
BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
FORWARD_TIMEOUT = float(os.environ.get("FORWARD_TIMEOUT", "5"))

# Optional Wunderground rapidfire upload. If WU_STATION_ID + WU_PASSWORD are
# set, each parsed observation is also forwarded to wunderground.com using
# the user's PWS credentials. Independent of BACKEND_URL — both can be on,
# both can be off.
WU_STATION_ID = os.environ.get("WU_STATION_ID", "")
WU_PASSWORD = os.environ.get("WU_PASSWORD", "")
WU_URL = "https://rtupdate.wunderground.com/weatherstation/updateweatherstation.php"

# Optional human labels for the device row in the backend. The hub itself
# only knows its MAC + sensor IDs — these let you pin a friendly station
# name and location so the iOS app shows "Atlas — Chandler" instead of just
# "AcuRite Atlas".
STATION_NAME = os.environ.get("STATION_NAME", "")
STATION_LOCATION = os.environ.get("STATION_LOCATION", "")

# ──────────────────────────────────────────────────────────────────────────
# Wunderground PWS field → normalized observation field.
# Reference: https://support.weather.com/s/article/PWS-Upload-Protocol
# Acurite Atlas reuses this protocol verbatim, plus adds its own fields
# (sensor, sensorbattery, hubbattery, rssi, lightintensity, strike*).
# ──────────────────────────────────────────────────────────────────────────

# Numeric fields where empty-string means "no reading", not zero.
def _f(s: str | None) -> float | None:
    if s is None or s == "": return None
    try: return float(s)
    except ValueError: return None

def _i(s: str | None) -> int | None:
    v = _f(s)
    return int(v) if v is not None else None


def parse_observation(qs: dict[str, list[str]]) -> dict[str, Any]:
    """Map a Wunderground-rapidfire query dict to our normalized schema."""
    g = lambda k: qs.get(k, [""])[0]

    # Solar radiation: hub reports 'lightintensity' in lux. For sunlight,
    # ~126 lux ≈ 1 W/m² (luminous efficacy of the visible+IR mix that hits a
    # silicon pyranometer). Convert so the chart Y-axis stays comparable to
    # AmbientWeather's solarradiation field.
    light_lux = _f(g("lightintensity"))
    solar_wm2 = round(light_lux / 126.0, 1) if light_lux is not None else None

    obs: dict[str, Any] = {
        "device": {
            "id":         g("id") or None,            # hub MAC, e.g. 24C86E0A66F5
            "model":      g("mt") or None,            # "Atlas"
            "sensor_id":  g("sensor") or None,        # outdoor sensor serial
            "rssi":       _i(g("rssi")),
            "battery_outdoor": g("sensorbattery") or None,  # "normal"/"low"
            "battery_hub":     g("hubbattery") or None,
        },
        "timestamp_utc": g("dateutc") or None,        # "2026-05-14T01:05:12"
        "outdoor": {
            "tempf":       _f(g("tempf")),
            "humidity":    _f(g("humidity")),
            "feels_like":  _f(g("feelslike")),
            "heat_index":  _f(g("heatindex")),
            "wind_chill":  _f(g("windchill")),
            "dew_point_f": _f(g("dewptf")),
            "uv":          _f(g("uvindex")),
            "solar_wm2":   solar_wm2,
            "light_lux":   light_lux,
        },
        "wind": {
            "speed_mph":     _f(g("windspeedmph")),
            "gust_mph":      _f(g("windgustmph")),
            "gust_dir":      _i(g("windgustdir")),
            "avg_speed_mph": _f(g("windspeedavgmph")),
            "direction":     _i(g("winddir")),
        },
        "rain": {
            "hourly_in": _f(g("rainin")),
            "daily_in":  _f(g("dailyrainin")),
        },
        "pressure": {
            "relative_inhg": _f(g("baromin")),
        },
        "lightning": {
            "strike_count":          _i(g("strikecount")),
            "interference":          _i(g("interference")),
            "last_strike_ts":        g("last_strike_ts") or None,
            "last_strike_distance":  _f(g("last_strike_distance")),
        },
        "source": "acurite-atlas",
    }
    return obs


# ──────────────────────────────────────────────────────────────────────────
# HTTP handler
# ──────────────────────────────────────────────────────────────────────────

class RelayHandler(BaseHTTPRequestHandler):
    def _handle(self, scheme: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = urlsplit(self.path)
        qs = parse_qs(url.query, keep_blank_values=True)

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        raw = {
            "ts_iso": ts,
            "scheme": scheme,
            "method": self.command,
            "path":   url.path,
            "query":  {k: v[0] if len(v) == 1 else v for k, v in qs.items()},
            "headers": {k: v for k, v in self.headers.items()},
            "remote": self.client_address[0],
            "body_len": len(body),
        }
        # Keep raw payload too (for debugging / future schema drift).
        try:
            raw["body"] = body.decode("utf-8") if length else ""
        except UnicodeDecodeError:
            import base64
            raw["body"] = "<binary base64=" + base64.b64encode(body).decode() + ">"
        _append_jsonl(RAW_LOG, raw)

        # Only the Wunderground-style updateweatherstation path carries
        # observations. Hub may also probe other paths during boot — ignore
        # those for the observations log.
        if "updateweatherstation" in url.path and qs:
            obs = parse_observation(qs)
            obs["received_iso"] = ts
            # Hub clock can drift hours behind reality (we've seen 15+ minute
            # lag between Atlas sensor RF time and the hub's reported dateutc).
            # Use the relay's wall clock as the canonical observation time so
            # every POST creates a fresh row downstream and charts stay smooth.
            # Preserve the hub's claim too in case anyone wants it.
            obs["hub_timestamp_utc"] = obs.get("timestamp_utc")
            obs["timestamp_utc"] = ts
            # Station-level overrides: friendly name + location aren't in the
            # hub's payload but the operator knows them.
            if STATION_NAME:     obs["device"]["name"] = STATION_NAME
            if STATION_LOCATION: obs["device"]["location"] = STATION_LOCATION
            _append_jsonl(OBS_LOG, obs)
            _forward(obs)
            _forward_wunderground(obs)
            tempf = obs["outdoor"]["tempf"]
            wind = obs["wind"]["speed_mph"]
            rain = obs["rain"]["daily_in"]
            fwds = []
            if BACKEND_URL and INGEST_TOKEN: fwds.append("backend")
            if WU_STATION_ID and WU_PASSWORD: fwds.append("wu")
            tail = " → " + "+".join(fwds) if fwds else ""
            print(f"[{ts}] {scheme.upper()} obs from {raw['remote']}: "
                  f"{tempf}°F, wind {wind} mph, rain {rain}\" today{tail}",
                  flush=True)
        else:
            print(f"[{ts}] {scheme.upper()} {self.command} {url.path} from {raw['remote']} (non-obs)",
                  flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):    self._handle(self.server.scheme)  # type: ignore[attr-defined]
    def do_POST(self):   self._handle(self.server.scheme)  # type: ignore[attr-defined]
    def do_PUT(self):    self._handle(self.server.scheme)  # type: ignore[attr-defined]
    def do_DELETE(self): self._handle(self.server.scheme)  # type: ignore[attr-defined]
    def do_PATCH(self):  self._handle(self.server.scheme)  # type: ignore[attr-defined]
    def do_HEAD(self):   self._handle(self.server.scheme)  # type: ignore[attr-defined]
    def log_message(self, *args, **kwargs): pass


def _append_jsonl(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"WARN: failed to write {path}: {e}", flush=True)


def _forward(observation: dict) -> None:
    """Best-effort POST to backend's /ingest/custom/<token>. Runs in a
    daemon thread so a slow/down backend never blocks the next hub POST."""
    if not BACKEND_URL or not INGEST_TOKEN:
        return
    def _send():
        try:
            import urllib.request
            # Token via Authorization header (not URL path) so it doesn't
            # land in proxy / access logs.
            req = urllib.request.Request(
                f"{BACKEND_URL}/ingest/custom",
                data=json.dumps(observation).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {INGEST_TOKEN}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT) as resp:
                if resp.status >= 400:
                    print(f"WARN: backend {resp.status}", flush=True)
        except Exception as e:
            print(f"WARN: forward failed: {e}", flush=True)
    threading.Thread(target=_send, daemon=True).start()


def _forward_wunderground(observation: dict) -> None:
    """Best-effort upload to Wunderground's rapidfire PWS endpoint using
    the user's WU station credentials. Runs in a daemon thread.

    Spec: https://support.weather.com/s/article/PWS-Upload-Protocol
    """
    if not WU_STATION_ID or not WU_PASSWORD:
        return
    out = observation.get("outdoor") or {}
    wind = observation.get("wind") or {}
    rain = observation.get("rain") or {}
    press = observation.get("pressure") or {}
    ts = observation.get("timestamp_utc") or ""
    # WU expects "YYYY-MM-DD HH:MM:SS" with a space, not the ISO 'T'.
    dateutc = ts.replace("T", " ").rstrip("Z") if ts else "now"

    params: dict[str, str] = {
        "ID":       WU_STATION_ID,
        "PASSWORD": WU_PASSWORD,
        "dateutc":  dateutc,
        "action":   "updateraw",
        "realtime": "1",
        "rtfreq":   "18",   # hub posts every ~18s
        "softwaretype": "ZasderRelay/0.1",
    }
    # Only include fields we actually have. Wunderground silently ignores
    # missing ones, but null values upset some receivers.
    for src, dst in [
        ("tempf",       out.get("tempf")),
        ("humidity",    out.get("humidity")),
        ("dewptf",      out.get("dew_point_f")),
        ("baromin",     press.get("relative_inhg")),
        ("windspeedmph", wind.get("speed_mph")),
        ("windgustmph", wind.get("gust_mph")),
        ("winddir",     wind.get("direction")),
        ("windgustdir", wind.get("gust_dir")),
        ("rainin",      rain.get("hourly_in")),
        ("dailyrainin", rain.get("daily_in")),
        ("solarradiation", out.get("solar_wm2")),
        ("UV",          out.get("uv")),
    ]:
        if dst is not None:
            params[src] = str(dst)

    def _send():
        try:
            import urllib.parse, urllib.request
            url = WU_URL + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=FORWARD_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "ignore").strip()
                if resp.status >= 400 or "success" not in body.lower():
                    print(f"WARN: WU {resp.status}: {body[:80]}", flush=True)
        except Exception as e:
            print(f"WARN: WU forward failed: {e}", flush=True)
    threading.Thread(target=_send, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────────
# TLS — generate a self-signed cert on first boot if one isn't mounted in.
# Hub doesn't validate the chain (per Acuparse community notes) but it does
# need *something* to complete the handshake.
# ──────────────────────────────────────────────────────────────────────────

def ensure_self_signed_cert() -> tuple[str, str]:
    cert_path = os.path.join(CERT_DIR, "cert.pem")
    key_path = os.path.join(CERT_DIR, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    os.makedirs(CERT_DIR, exist_ok=True)
    print("Generating self-signed cert for atlasapi.myacurite.com...", flush=True)
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key_path, "-out", cert_path, "-days", "3650", "-nodes",
        "-subj", "/CN=atlasapi.myacurite.com",
        "-addext", "subjectAltName=DNS:atlasapi.myacurite.com,DNS:*.myacurite.com",
    ], check=True, capture_output=True)
    return cert_path, key_path


def serve(scheme: str, port: int, ssl_ctx: ssl.SSLContext | None = None) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), RelayHandler)
    server.scheme = scheme  # type: ignore[attr-defined]
    if ssl_ctx:
        server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True)
    print(f"  {scheme.upper()} listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    cert_path, key_path = ensure_self_signed_cert()
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(cert_path, key_path)
    # Acurite Atlas firmware speaks TLS 1.1 — be permissive about versions
    # and ciphers so the handshake completes.
    ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1
    ssl_ctx.set_ciphers("ALL:@SECLEVEL=0")

    print(f"acurite-relay starting; data dir {DATA_DIR}", flush=True)
    threading.Thread(target=serve, args=("http", 80), daemon=True).start()
    threading.Thread(target=serve, args=("https", 443, ssl_ctx), daemon=True).start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("shutting down", flush=True)
        sys.exit(0)
