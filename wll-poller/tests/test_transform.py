"""Unit tests for the WLL → ingest transform. Pure — no network, no env."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import poller  # noqa: E402
from poller import to_observation  # noqa: E402


def _sample():
    """A representative WLL /v1/current_conditions response (single ISS,
    barometer, and indoor sensors). Trimmed to the fields we read."""
    return {
        "error": None,
        "data": {
            "did": "001D0A700123",
            "ts": 1717200000,                       # 2024-06-01T00:00:00Z
            "conditions": [
                {
                    "data_structure_type": 1,
                    "txid": 1,
                    "temp": 73.2, "hum": 41.6, "dew_point": 47.8,
                    "wind_chill": 73.2, "heat_index": 73.2, "thsw_index": 76.4,
                    "wind_speed_last": 1.5,
                    "wind_dir_last": 230,
                    "wind_speed_hi_last_10_min": 8.2,
                    "rain_size": 1,                 # 0.01" per tip (US)
                    "rain_rate_last": 999,          # counts/hr (RATE — must be ignored)
                    "rainfall_last_60_min": 12,     # counts → 0.12" last hour
                    "rainfall_daily": 25,           # counts → 0.25"
                    "rainfall_monthly": 158,        # counts → 1.58"
                    "rainfall_year": 4720,          # counts → 47.20"
                    "solar_rad": 815,
                    "uv_index": 5.5,
                },
                {"data_structure_type": 4,
                 "temp_in": 78.0, "hum_in": 41.1},
                {"data_structure_type": 3,
                 "bar_sea_level": 30.115, "bar_absolute": 29.985},
            ],
        },
    }


class TransformTests(unittest.TestCase):

    def test_outdoor_block(self):
        obs = to_observation(_sample())
        self.assertEqual(obs["outdoor"]["tempf"], 73.2)
        self.assertEqual(obs["outdoor"]["humidity"], 41.6)
        self.assertEqual(obs["outdoor"]["dew_point_f"], 47.8)
        # 73.2°F is between the regimes, so feels-like is the air temp. THSW
        # (76.4 in the sample) is deliberately NOT used: it adds a direct-sun
        # load that reads 5-10°F hotter than every other source in the app,
        # which looks like a bug next to them.
        self.assertEqual(obs["outdoor"]["feels_like"], 73.2)

    def test_feels_like_by_temperature_regime(self):
        # WLL populates heat_index AND wind_chill on every tick regardless of
        # temperature — picking whichever is truthy reported a ~5°F "feels
        # like" on a 62°F day.
        wll = _sample()
        iss = wll["data"]["conditions"][0]
        iss.update(temp=62.7, heat_index=5.5, wind_chill=6.0)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 62.7)

        iss.update(temp=95.0, heat_index=104.0, wind_chill=95.0)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 104.0)

        iss.update(temp=30.0, heat_index=30.0, wind_chill=18.0)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 18.0)

    def test_feels_like_keeps_a_legitimate_zero(self):
        wll = _sample()
        wll["data"]["conditions"][0].update(temp=10.0, heat_index=10.0, wind_chill=0.0)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 0.0)

    def test_second_transmitter_does_not_overwrite_the_first(self):
        # Up to 8 ISS transmitters can report; merging them all last-wins mixed
        # readings from different physical stations into one observation.
        wll = _sample()
        wll["data"]["conditions"].append({
            "data_structure_type": 1, "txid": 2,
            "temp": -40.0, "hum": 3.0, "wind_speed_last": 99.0,
        })
        obs = to_observation(wll)
        self.assertEqual(obs["outdoor"]["tempf"], 73.2)     # txid 1, not txid 2
        self.assertEqual(obs["wind"]["speed_mph"], 1.5)

    def test_wll_txid_pins_a_specific_transmitter(self):
        wll = _sample()
        wll["data"]["conditions"].append({
            "data_structure_type": 1, "txid": 2,
            "temp": 61.0, "hum": 20.0, "wind_speed_last": 4.0,
        })
        prev = poller.WLL_TXID
        poller.WLL_TXID = 2
        try:
            obs = to_observation(wll)
        finally:
            poller.WLL_TXID = prev
        self.assertEqual(obs["outdoor"]["tempf"], 61.0)
        self.assertEqual(obs["wind"]["speed_mph"], 4.0)

    def test_wind_block(self):
        w = to_observation(_sample())["wind"]
        self.assertEqual(w["speed_mph"], 1.5)
        self.assertEqual(w["dir_deg"], 230)
        self.assertEqual(w["gust_mph"], 8.2)

    def test_rain_block_counts_to_inches(self):
        r = to_observation(_sample())["rain"]
        # hourly_in is the last-hour ACCUMULATION (rainfall_last_60_min),
        # matching the WeatherLink cloud poller's rainfall_last_60_min_in —
        # NOT the instantaneous rate. The sample's rain_rate_last is a
        # deliberately absurd 999 so using it by mistake fails loudly.
        self.assertAlmostEqual(r["hourly_in"], 0.12, places=4)
        self.assertAlmostEqual(r["daily_in"], 0.25, places=4)
        self.assertAlmostEqual(r["monthly_in"], 1.58, places=4)
        self.assertAlmostEqual(r["yearly_in"], 47.20, places=4)

    def test_rain_size_missing_or_unknown_defaults_to_001(self):
        # No rain_size field at all → 0.01"/count default.
        wll = _sample()
        del wll["data"]["conditions"][0]["rain_size"]
        r = to_observation(wll)["rain"]
        self.assertAlmostEqual(r["daily_in"], 0.25, places=4)
        # Unknown enum value → same default, not a KeyError.
        wll = _sample()
        wll["data"]["conditions"][0]["rain_size"] = 99
        r = to_observation(wll)["rain"]
        self.assertAlmostEqual(r["daily_in"], 0.25, places=4)

    def test_pressure_and_indoor(self):
        obs = to_observation(_sample())
        # Backend's ingest._flatten reads pressure.relative_inhg (full word).
        self.assertAlmostEqual(obs["pressure"]["relative_inhg"], 30.115)
        self.assertAlmostEqual(obs["pressure"]["absolute_inhg"], 29.985)
        self.assertEqual(obs["indoor"]["tempf"], 78.0)
        self.assertEqual(obs["indoor"]["humidity"], 41.1)

    def test_solar_block(self):
        s = to_observation(_sample())["solar"]
        self.assertEqual(s["radiation_wm2"], 815)
        # The backend flattener reads solar.uv — NOT uv_index, which it
        # silently drops. The old either-key assertion passed for both names,
        # so a key-rename regression would go green while UV disappeared
        # from the app.
        self.assertEqual(s["uv"], 5.5)
        self.assertNotIn("uv_index", s)

    def test_device_and_envelope(self):
        obs = to_observation(_sample(), mac="5D:5D:05:00:00:01", name="Davis VP2 (Local)")
        self.assertEqual(obs["device"]["id"], "5D:5D:05:00:00:01")
        self.assertEqual(obs["device"]["name"], "Davis VP2 (Local)")
        self.assertEqual(obs["source"], "davis-wll-local")
        self.assertEqual(obs["timestamp_utc"], "2024-06-01T00:00:00Z")

    def test_empty_name_omits_field(self):
        obs = to_observation(_sample(), mac="AA:BB:CC:DD:EE:FF", name="")
        # Empty name must NOT be sent — backend's _device_label would treat
        # an empty string as an explicit rename and overwrite a good name.
        self.assertNotIn("name", obs["device"])

    def test_metric_rain_size(self):
        wll = _sample()
        wll["data"]["conditions"][0]["rain_size"] = 3       # 0.1 mm
        wll["data"]["conditions"][0]["rainfall_daily"] = 50  # 5.0 mm = 0.1969"
        r = to_observation(wll)["rain"]
        self.assertAlmostEqual(r["daily_in"], 50 * (0.1 / 25.4), places=4)

    def test_returns_none_on_wll_error(self):
        wll = _sample()
        wll["error"] = {"code": 500, "message": "boom"}
        self.assertIsNone(to_observation(wll))

    def test_returns_none_when_no_conditions(self):
        self.assertIsNone(to_observation({"error": None, "data": {"ts": 0, "conditions": []}}))

    def test_null_sensor_values_become_none(self):
        wll = _sample()
        wll["data"]["conditions"][0]["temp"] = None        # WLL emits null when offline
        wll["data"]["conditions"][0]["hum"] = None
        obs = to_observation(wll)
        self.assertIsNone(obs["outdoor"]["tempf"])
        self.assertIsNone(obs["outdoor"]["humidity"])

    # ── pinned transmitter absent ────────────────────────────────────────

    def test_pinned_txid_absent_returns_no_iss(self):
        """WLL_TXID pinned to a transmitter that isn't reporting → no ISS
        record is read (a wrong pin must not silently fall back to another
        physical station's readings)."""
        wll = _sample()
        prev = poller.WLL_TXID
        poller.WLL_TXID, poller._txid_warned = 7, False
        try:
            self.assertIsNone(
                poller._pick_iss(wll["data"]["conditions"]))
            obs = to_observation(wll)
        finally:
            poller.WLL_TXID, poller._txid_warned = prev, False
        # ...but the observation still posts what the WLL itself provides:
        # barometer + indoor. Outdoor stays empty rather than wrong.
        self.assertIsNotNone(obs)
        self.assertEqual(obs["outdoor"], {})
        self.assertAlmostEqual(obs["pressure"]["relative_inhg"], 30.115)
        self.assertEqual(obs["indoor"]["tempf"], 78.0)

    # ── timestamp fallback ───────────────────────────────────────────────

    def test_ts_missing_or_zero_falls_back_to_now(self):
        for ts in (None, 0):
            wll = _sample()
            wll["data"]["ts"] = ts
            obs = to_observation(wll)
            got = obs["timestamp_utc"]
            # ~now, not 1970 — just check the year is current-ish.
            self.assertGreaterEqual(int(got[:4]), 2024, got)

    def test_ts_garbage_falls_back_to_now(self):
        """A non-numeric ts must not throw away the whole (fine) reading."""
        wll = _sample()
        wll["data"]["ts"] = "not-a-number"
        obs = to_observation(wll)
        self.assertIsNotNone(obs)
        self.assertGreaterEqual(int(obs["timestamp_utc"][:4]), 2024)

    # ── feels-like regime boundaries ─────────────────────────────────────

    def test_feels_like_boundaries_are_inclusive(self):
        wll = _sample()
        iss = wll["data"]["conditions"][0]
        # Exactly 80.0°F → heat-index regime.
        iss.update(temp=80.0, heat_index=82.0, wind_chill=80.0)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 82.0)
        # Exactly 50.0°F → wind-chill regime.
        iss.update(temp=50.0, heat_index=50.0, wind_chill=44.0)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 44.0)

    def test_feels_like_null_index_at_extremes_uses_air_temp(self):
        wll = _sample()
        iss = wll["data"]["conditions"][0]
        iss.update(temp=95.0, heat_index=None, wind_chill=None)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 95.0)
        iss.update(temp=20.0, heat_index=None, wind_chill=None)
        self.assertEqual(to_observation(wll)["outdoor"]["feels_like"], 20.0)


class PostObservationTests(unittest.TestCase):
    """post_observation drives the module's no-redirect opener — mock its
    open(), assert the request. (It deliberately does NOT use bare
    urllib.request.urlopen: see _NoRedirect / R3-39.)"""

    def test_posts_to_ingest_custom_with_bearer_and_json(self):
        from unittest import mock
        seen = {}

        def fake_open(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            seen["ctype"] = req.get_header("Content-type")
            seen["body"] = req.data
            m = mock.MagicMock()
            m.__enter__ = lambda s: mock.Mock(status=200, read=lambda: b"{}")
            m.__exit__ = lambda s, *a: False
            return m

        obs = {"device": {"id": "AA"}, "source": "davis-wll-local"}
        with mock.patch.object(poller._INGEST_OPENER, "open", fake_open):
            poller.post_observation(obs, backend="https://b.example",
                                    token="sekrit")
        self.assertEqual(seen["url"], "https://b.example/ingest/custom")
        self.assertEqual(seen["auth"], "Bearer sekrit")
        self.assertEqual(seen["ctype"], "application/json")
        import json as _json
        self.assertEqual(_json.loads(seen["body"]), obs)

    def test_401_raises(self):
        import urllib.error
        from unittest import mock

        def fake_open(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                         hdrs=None, fp=None)

        with mock.patch.object(poller._INGEST_OPENER, "open", fake_open):
            with self.assertRaises(urllib.error.HTTPError):
                poller.post_observation({"device": {"id": "AA"}},
                                        backend="https://b.example",
                                        token="wrong")

    def test_redirect_is_refused_not_followed(self):
        """R3-39 regression, against a REAL local HTTP server: a 3xx from
        the backend must raise, not be followed — the default opener
        replays `Authorization: Bearer <INGEST_TOKEN>` verbatim to
        whatever host the redirect names."""
        import http.server
        import threading
        import urllib.error

        hits: list[str] = []

        class RedirectingHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_address[1]}/steal-token")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):  # where a followed 302 (POST→GET) would land
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectingHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                poller.post_observation({"device": {"id": "AA"}},
                                        backend=base, token="sekrit")
            self.assertEqual(ctx.exception.code, 302)
            # Exactly one request — the redirect target was never fetched.
            self.assertEqual(hits, ["/ingest/custom"])
        finally:
            srv.shutdown()
            srv.server_close()


class ConfigValidationTests(unittest.TestCase):
    """Import-time env validation — must SystemExit with a one-line message,
    not a traceback in a launchd/systemd restart loop. Run in a subprocess
    because the validation happens at import."""

    def _import_with_env(self, **env):
        import subprocess
        full = {**os.environ, **env}
        return subprocess.run(
            [sys.executable, "-c", "import poller"],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            env=full, capture_output=True, text=True)

    def test_wll_txid_rejects_out_of_range_and_garbage(self):
        for bad in ("0", "9", "abc", "-1"):
            r = self._import_with_env(WLL_TXID=bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("WLL_TXID", r.stderr)
            self.assertNotIn("Traceback", r.stderr, bad)

    def test_wll_txid_accepts_valid_and_blank(self):
        for good in ("1", "8", ""):
            r = self._import_with_env(WLL_TXID=good)
            self.assertEqual(r.returncode, 0, (good, r.stderr))

    def test_wll_poll_seconds_rejects_garbage_and_nonpositive(self):
        for bad in ("abc", "0", "-5"):
            r = self._import_with_env(WLL_POLL_SECONDS=bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("WLL_POLL_SECONDS", r.stderr)
            self.assertNotIn("Traceback", r.stderr, bad)

    def test_wll_poll_seconds_accepts_valid(self):
        r = self._import_with_env(WLL_POLL_SECONDS="30")
        self.assertEqual(r.returncode, 0, r.stderr)


class SetupUrlValidatorTests(unittest.TestCase):
    """Shell-level tests for bin/setup-macos.sh's backend-URL validator
    (via its --check-url seam). This allowlist is the only thing standing
    between the ingest token and cleartext HTTP to a public host."""

    SCRIPT = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "bin", "setup-macos.sh"))

    def _check(self, url):
        import subprocess
        r = subprocess.run(["bash", self.SCRIPT, "--check-url", url],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def test_rejected_hosts(self):
        for url, reason in [
            ("http://10.attacker.example", "cleartext"),   # DNS label, not octet
            ("http://10.0.0.1@attacker.example", "userinfo"),
            ("http://172.15.0.1", "cleartext"),            # just below 172.16/12
            ("http://172.32.0.1", "cleartext"),            # just above
            ("http://8.8.8.8", "cleartext"),               # public IP
            ("http://weather.example.com", "cleartext"),   # public hostname
            ("ftp://x", "scheme"),
            ("", "empty"),
        ]:
            rc, out = self._check(url)
            self.assertEqual((rc, out), (1, reason), url)

    def test_accepted_hosts(self):
        for url, expect in [
            ("http://172.16.0.1:8080", "http://172.16.0.1:8080"),
            ("http://172.31.9.9", "http://172.31.9.9"),
            ("http://192.168.1.50", "http://192.168.1.50"),
            ("http://10.0.0.5", "http://10.0.0.5"),
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
            ("http://[::1]:8080", "http://[::1]:8080"),
            ("http://[fd00::1]:8080", "http://[fd00::1]:8080"),  # ULA
            ("http://[fe80::1%en0]", "http://[fe80::1%en0]"),    # link-local
            ("http://backend.local", "http://backend.local"),
            ("HTTP://192.168.1.50", "http://192.168.1.50"),      # scheme case
            ("example.com", "https://example.com"),              # https assumed
            ("https://anything.example", "https://anything.example"),
        ]:
            rc, out = self._check(url)
            self.assertEqual((rc, out), (0, expect), url)


if __name__ == "__main__":
    unittest.main()


class BatteryTests(unittest.TestCase):
    """trans_battery_flag (0 = ok, 1 = low) → device.battery_outdoor, the
    same relay convention the SDR/Davis relays post. Absent flag = absent
    key — a WLL that doesn't report battery must not claim one."""

    def test_flag_maps_to_battery_outdoor(self):
        s = _sample()
        s["data"]["conditions"][0]["trans_battery_flag"] = 0
        self.assertEqual(to_observation(s)["device"]["battery_outdoor"], "normal")
        s["data"]["conditions"][0]["trans_battery_flag"] = 1
        self.assertEqual(to_observation(s)["device"]["battery_outdoor"], "low")

    def test_absent_flag_stays_absent(self):
        self.assertNotIn("battery_outdoor", to_observation(_sample())["device"])


# ────────────────────── R6: timestamp + type guards ──────────────────────

class R6TimestampAndTypeGuards(unittest.TestCase):
    def test_huge_numeric_ts_falls_back_to_now(self):
        # R6 finding 1: 99999999999999 raised "year must be in 1..9999"
        # out of to_observation and killed every tick for as long as the
        # gateway clock stayed broken.
        obs = poller.to_observation({"data": {
            "ts": 99999999999999,
            "conditions": [{"data_structure_type": 1, "txid": 1,
                            "temp": 70.0}]}})
        self.assertIsNotNone(obs)
        year = int(obs["timestamp_utc"][:4])
        self.assertGreaterEqual(year, 2020)

    def test_tiny_and_negative_ts_fall_back_to_now(self):
        # R6 finding 2: ts=1 posted "1970-01-01…", the backend 400'd it,
        # and the poller's error message blamed the INGEST_TOKEN.
        for bad in (1, -5):
            obs = poller.to_observation({"data": {
                "ts": bad,
                "conditions": [{"data_structure_type": 1, "txid": 1,
                                "temp": 70.0}]}})
            self.assertIsNotNone(obs, bad)
            self.assertGreaterEqual(int(obs["timestamp_utc"][:4]), 2020, bad)

    def test_num_rejects_json_booleans(self):
        # R6 finding 3: isinstance(True, int) is True.
        self.assertIsNone(poller._num({"x": True}, "x"))
        self.assertIsNone(poller._num({"x": False}, "x"))
        self.assertEqual(poller._num({"x": 1}, "x"), 1)

    def test_non_dict_payload_returns_none(self):
        # R6 finding 4: a JSON list crashed on .get instead of the
        # documented None.
        self.assertIsNone(poller.to_observation([1, 2, 3]))
