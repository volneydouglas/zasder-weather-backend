"""Unit tests for the WLL → ingest transform. Pure — no network, no env."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
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
                    "rain_rate_last": 12,           # counts/hr → 0.12 in/hr
                    "rainfall_daily": 25,           # counts → 0.25"
                    "rainfall_monthly": 158,        # counts → 1.58"
                    "rainfall_year": 4720,          # counts → 47.20"
                    "solar_rad": 815,
                    "uv_index": 5.5,
                },
                {"data_structure_type": 3,
                 "temp_in": 78.0, "hum_in": 41.1},
                {"data_structure_type": 4,
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
        # THSW wins over heat_index / wind_chill
        self.assertEqual(obs["outdoor"]["feels_like"], 76.4)

    def test_wind_block(self):
        w = to_observation(_sample())["wind"]
        self.assertEqual(w["speed_mph"], 1.5)
        self.assertEqual(w["dir_deg"], 230)
        self.assertEqual(w["gust_mph"], 8.2)

    def test_rain_block_counts_to_inches(self):
        r = to_observation(_sample())["rain"]
        self.assertAlmostEqual(r["hourly_in"], 0.12, places=4)
        self.assertAlmostEqual(r["daily_in"], 0.25, places=4)
        self.assertAlmostEqual(r["monthly_in"], 1.58, places=4)
        self.assertAlmostEqual(r["yearly_in"], 47.20, places=4)

    def test_pressure_and_indoor(self):
        obs = to_observation(_sample())
        self.assertAlmostEqual(obs["pressure"]["rel_inhg"], 30.115)
        self.assertAlmostEqual(obs["pressure"]["abs_inhg"], 29.985)
        self.assertEqual(obs["indoor"]["tempf"], 78.0)
        self.assertEqual(obs["indoor"]["humidity"], 41.1)
        # Barometer mirrored into indoor.pressure_inhg
        self.assertAlmostEqual(obs["indoor"]["pressure_inhg"], 30.115)

    def test_solar_block(self):
        s = to_observation(_sample())["solar"]
        self.assertEqual(s["radiation_wm2"], 815)
        self.assertEqual(s["uv_index"] if "uv_index" in s else s["uv"], 5.5)

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


if __name__ == "__main__":
    unittest.main()
