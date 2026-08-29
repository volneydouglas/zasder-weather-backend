"""Ecowitt local ingest (1.9): the pure transform in app/ecowitt.py and
the /ingest/ecowitt route. Fixture fields mirror a GW3000 + WS90 + WH40
"Customized" upload — the exact bundle the live hardware will send."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app.ecowitt import (_batteries, _rain, _synth_mac,  # noqa: E402
                         _timestamp_utc, normalize)

# A real PASSKEY is 32 hex chars (MD5 of the gateway MAC). This fixture
# deliberately is NOT: `PASSKEY = "<32 alnum>"` is exactly the credential
# shape the publish secret-sweep hunts (a *KEY* assignment), and it
# aborts the mirror on any match — the sweep is right, the fixture
# yields. The dashes break the alnum run; normalize() only requires the
# FIRST 8 chars to be hex (the synth-mac source), which stays pinned.
PASSKEY = "34271334-TEST-PASSKEY-NOT-REAL-0"


def _now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _gw3000_form(**over):
    """A GW3000A posting a WS90 array + WH40 tipping gauge."""
    d = {
        "PASSKEY": PASSKEY, "stationtype": "GW3000A_V1.0.6",
        "runtime": "49030", "dateutc": _now_utc(),
        "tempinf": "82.4", "humidityin": "31",
        "baromrelin": "29.864", "baromabsin": "28.989",
        "tempf": "104.5", "humidity": "15",
        "winddir": "193", "windspeedmph": "3.80", "windgustmph": "5.14",
        "maxdailygust": "12.30",
        "solarradiation": "847.95", "uv": "8",
        "rainratein": "0.000", "eventrainin": "0.000",
        "hourlyrainin": "0.000", "dailyrainin": "0.120",
        "weeklyrainin": "0.120", "monthlyrainin": "0.331",
        "yearlyrainin": "6.909", "totalrainin": "6.909",
        "wh90batt": "3.28", "wh40batt": "1.45",
        "model": "GW3000A", "freq": "915M",
    }
    d.update(over)
    return d


# ── identity ────────────────────────────────────────────────────────────

def test_synth_mac_layout_is_pinned():
    """"ECEC" + first 8 PASSKEY chars, uppercased. Changing this orphans
    every Ecowitt device row's history — same class of pin as the LilyGO
    firmware's 5D5D scheme."""
    assert _synth_mac(PASSKEY) == "ECEC34271334"
    assert _synth_mac("abcdef12rest-ignored") == "ECECABCDEF12"


def test_synth_mac_rejects_garbage():
    assert _synth_mac("") is None
    assert _synth_mac("short") is None
    assert _synth_mac("XYZXYZXYZXYZ") is None      # not hex


def test_timestamp_shape():
    assert _timestamp_utc("2026-09-02 17:12:07") == "2026-09-02T17:12:07Z"
    assert _timestamp_utc("2026-09-02T17:12:07") is None   # not their shape
    assert _timestamp_utc("now") is None
    assert _timestamp_utc(None) is None


# ── batteries ───────────────────────────────────────────────────────────

def test_binary_flags_are_inverted_to_stored_convention():
    """Ecowitt wire: 1 = LOW. Stored (AWN) convention: 0 = low. A missed
    inversion would report every healthy sensor as dying and vice versa."""
    out = _batteries({"wh65batt": "1", "batt1": "0", "batt3": "1"})
    assert out == {"wh65batt": 0.0, "batt1": 1.0, "batt3": 0.0}


def test_level_batteries_low_at_one_and_awn_renames():
    """Level sensors flag at ≤1 — and land under AWN-native names (the
    1.9 field survey): wh57 IS the lightning detector, WH41 ch1 IS
    AWN's batt_25. Unmapped channels keep their vendor names."""
    out = _batteries({"wh57batt": "5", "pm25batt1": "1", "pm25batt2": "4"})
    assert out == {"batt_lightning": 1.0, "batt_25": 0.0, "pm25batt2": 1.0}


def test_voltage_batteries_use_low_water_marks():
    out = _batteries({"wh90batt": "3.28", "wh80batt": "2.1",
                      "wh40batt": "1.45"})
    assert out == {"wh90batt": 1.0, "wh80batt": 0.0, "wh40batt": 1.0}
    # WH40 AA cell sagging under 1.2 V is low.
    assert _batteries({"wh40batt": "1.1"}) == {"wh40batt": 0.0}


def test_binary_shaped_value_on_voltage_key_reads_as_flag():
    """Older firmware sends the flag form on voltage keys — no real cell
    reads a flat 0 or 1 V, so treat those as the wire flag (1 = low)."""
    assert _batteries({"wh90batt": "0"}) == {"wh90batt": 1.0}
    assert _batteries({"wh90batt": "1"}) == {"wh90batt": 0.0}


def test_unreadable_battery_is_silence_not_a_claim():
    assert _batteries({"wh65batt": "7", "wh57batt": "9",
                       "somebatt": "0"}) == {}


# ── rain ────────────────────────────────────────────────────────────────

def test_tipping_gauge_beats_piezo_per_field():
    """Dual-rain station (WS90 haptic + WH40 tipping): the tipping gauge
    wins wherever it reports — haptic rain is phantom-prone (the Tempest
    lesson) — and piezo only fills holes."""
    r = _rain({"dailyrainin": "0.12", "drain_piezo": "0.31",
               "yrain_piezo": "7.0"})
    assert r["daily_in"] == 0.12          # tipping wins
    assert r["yearly_in"] == 7.0          # piezo fills the hole


def test_rate_key_feeds_hourly():
    assert _rain({"rainratein": "0.24", "hourlyrainin": "0.08"})["hourly_in"] == 0.24
    assert _rain({"hourlyrainin": "0.08"})["hourly_in"] == 0.08
    assert _rain({"rrain_piezo": "0.16"})["hourly_in"] == 0.16


def test_absent_rain_is_none_not_zero():
    assert _rain({})["daily_in"] is None


# ── normalize ───────────────────────────────────────────────────────────

def test_normalize_full_station():
    n = normalize(_gw3000_form())
    assert n["device"]["id"] == "ECEC34271334"
    assert n["device"]["model"] == "GW3000A"
    assert n["device"]["battery_outdoor"] == "normal"   # WS90 3.28 V
    assert n["source"] == "ecowitt"
    assert n["outdoor"]["tempf"] == 104.5
    assert n["outdoor"]["solar_wm2"] == 847.95
    assert n["indoor"]["tempf"] == 82.4
    assert n["wind"]["direction"] == 193.0
    assert n["pressure"]["relative_inhg"] == 29.864
    assert n["rain"]["daily_in"] == 0.12
    assert n["batteries"] == {"wh90batt": 1.0, "wh40batt": 1.0}


def test_normalize_low_ws90_battery():
    n = normalize(_gw3000_form(wh90batt="2.1"))
    assert n["device"]["battery_outdoor"] == "low"


def test_normalize_rejects_non_ecowitt_posts():
    assert normalize({}) is None
    assert normalize({"PASSKEY": PASSKEY}) is None            # no dateutc
    assert normalize({"dateutc": _now_utc()}) is None         # no PASSKEY


# ── the route ───────────────────────────────────────────────────────────

def test_route_query_token_end_to_end(client):
    """The hardware's only credential channel is the upload path's query
    string. Full path: form post → normalize → _do_ingest → stored row
    with the battery flags in data_json for health_watch."""
    r = client.post("/ingest/ecowitt?token=test-ingest-token",
                    data=_gw3000_form())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mac"] == "EC:EC:34:27:13:34"
    assert body["inserted"] == 1

    from app import db

    async def fetch():
        obs = await db.latest_observation("EC:EC:34:27:13:34")
        devs = await db.list_devices()
        return obs, devs

    obs, devs = asyncio.run(fetch())
    assert obs["tempf"] == 104.5
    assert obs["dailyrainin"] == 0.12
    assert obs["battout"] == 1                     # WS90 voltage → flag
    assert obs["wh90batt"] == 1.0                  # per-sensor passthrough
    assert obs["wh40batt"] == 1.0
    d = next(d for d in devs if d["mac"] == "EC:EC:34:27:13:34")
    assert "Ecowitt" in (d.get("name") or "")      # auto-name on first insert


def test_route_header_token_also_accepted(client):
    r = client.post("/ingest/ecowitt",
                    headers={"Authorization": "Bearer test-ingest-token"},
                    data=_gw3000_form())
    assert r.status_code == 200


def test_route_rejects_bad_or_missing_token(client):
    assert client.post("/ingest/ecowitt?token=wrong",
                       data=_gw3000_form()).status_code == 401
    assert client.post("/ingest/ecowitt",
                       data=_gw3000_form()).status_code == 401


def test_route_400_on_non_ecowitt_body(client):
    r = client.post("/ingest/ecowitt?token=test-ingest-token",
                    data={"hello": "world"})
    assert r.status_code == 400

def test_channel_sensors_map_to_awn_names():
    from app.ecowitt import _channels
    out = _channels({"temp1f": "68.2", "humidity2": "41",
                     "soilmoisture1": "23", "soilmoisture4": "55",
                     "leak_ch2": "1", "leafwetness_ch1": "12"})
    assert out == {"temp1f": 68.2, "humidity2": 41.0,
                   "soilhum1": 23.0, "soilhum4": 55.0,
                   "leak2": 1.0, "leafwetness1": 12.0}
    assert _channels({}) == {}


def test_route_stores_channel_columns(client):
    """End to end: WH31 channel + soil + leak survive into TYPED columns,
    so their history outlives data_json trimming."""
    r = client.post("/ingest/ecowitt?token=test-ingest-token",
                    data=_gw3000_form(temp1f="68.2", humidity1="41",
                                      soilmoisture1="23", leak_ch1="0",
                                      wh57batt="4"))
    assert r.status_code == 200
    from app import db

    async def fetch():
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT temp1f, humidity1, soilhum1, leak1, batt_lightning "
                "FROM observations WHERE mac = ?", ("EC:EC:34:27:13:34",))
            return await cur.fetchone()

    row = asyncio.run(fetch())
    assert row["temp1f"] == 68.2
    assert row["humidity1"] == 41.0
    assert row["soilhum1"] == 23.0
    assert row["leak1"] == 0
    assert row["batt_lightning"] == 1


def test_route_preserves_absolute_pressure(client):
    """Ecowitt sends BOTH pressures — baromabsin must store the real
    absolute reading, not a copy of relative (CodeRabbit, PR #33)."""
    r = client.post("/ingest/ecowitt?token=test-ingest-token",
                    data=_gw3000_form())
    assert r.status_code == 200
    from app import db

    async def fetch():
        async with db.connect() as conn:
            cur = await conn.execute(
                "SELECT baromrelin, baromabsin FROM observations "
                "WHERE mac = ?", ("EC:EC:34:27:13:34",))
            return await cur.fetchone()

    row = asyncio.run(fetch())
    assert row["baromrelin"] == 29.864
    assert row["baromabsin"] == 28.989


def test_low_rain_gauge_battery_is_monitored():
    """A low WH40 beside a healthy WS90 array: battery_outdoor stays
    normal (the array IS healthy), so the per-sensor flag must be in
    health_watch's watch list or nobody hears about the dying gauge."""
    from app.health_watch import _BATT_LOW_FIELDS, battery_low_fields
    n = normalize(_gw3000_form(wh40batt="1.1"))     # sagging AA cell
    assert n["device"]["battery_outdoor"] == "normal"
    assert n["batteries"]["wh40batt"] == 0.0
    for k in n["batteries"]:
        assert k in _BATT_LOW_FIELDS or k.startswith("pm25batt"), k
    # And the watcher's pure classifier flags it from a stored row shape.
    assert "wh40batt" in battery_low_fields({"wh40batt": 0})


# ── metric firmware (R11 V4) ────────────────────────────────────────────

def _metric_form(**over):
    """A GW2000-style upload with the console set to metric: the same
    station as _gw3000_form, but the metric key family. Before the
    _with_imperial pre-pass this 200'd and stored all-NULL rows forever."""
    d = {
        "PASSKEY": PASSKEY, "stationtype": "GW2000A_V3.1.5",
        "dateutc": _now_utc(),
        "tempc": "40.28", "humidity": "15",
        "tempinc": "28.0", "humidityin": "31",
        "baromrelhpa": "1011.25", "baromabshpa": "981.62",
        "winddir": "193", "windspeedkmh": "6.12", "windgustkmh": "8.27",
        "solarradiation": "847.95", "uv": "8",
        "rainratemm": "0.0", "dailyrainmm": "3.05",
        "yearlyrainmm": "175.49",
        "temp1c": "25.0",
        "model": "GW2000A",
    }
    d.update(over)
    return d


def test_metric_gateway_converts_to_stored_units():
    n = normalize(_metric_form())
    assert n["outdoor"]["tempf"] == pytest.approx(104.5, abs=0.01)
    assert n["indoor"]["tempf"] == pytest.approx(82.4, abs=0.01)
    assert n["pressure"]["relative_inhg"] == pytest.approx(29.86, abs=0.01)
    assert n["pressure"]["absolute_inhg"] == pytest.approx(28.99, abs=0.01)
    assert n["wind"]["speed_mph"] == pytest.approx(3.80, abs=0.01)
    assert n["wind"]["gust_mph"] == pytest.approx(5.14, abs=0.01)
    assert n["rain"]["daily_in"] == pytest.approx(0.12, abs=0.001)
    assert n["rain"]["yearly_in"] == pytest.approx(6.909, abs=0.001)
    # rate 0 mm/h is a real zero, not an absence
    assert n["rain"]["hourly_in"] == 0.0
    # channel sensors convert too
    assert n["extra"]["temp1f"] == pytest.approx(77.0, abs=0.01)


def test_imperial_keys_always_win_over_metric():
    """A payload carrying both families (or a hostile duplicate) must never
    let the metric value overwrite the imperial one."""
    n = normalize(_metric_form(tempf="70.0"))
    assert n["outdoor"]["tempf"] == 70.0


def test_metric_conversion_never_invents_readings():
    """Absent is not zero: a metric station without a rain gauge stores no
    rain, same as the imperial path."""
    form = _metric_form()
    for k in ("rainratemm", "dailyrainmm", "yearlyrainmm"):
        form.pop(k)
    n = normalize(form)
    assert n["rain"]["hourly_in"] is None
    assert n["rain"]["daily_in"] is None
