"""Real-device contract for the Ecowitt path (2026-09-02).

The Ecowitt code shipped in 1.9 without a device to test against. This
is the first upload a GW3000B (V1.2.0) with a WS90 array posted to
Volney's server, captured byte-for-byte on the LAN (PASSKEY replaced),
and what the server must make of it. If the mapping drifts, this fails
with the field named.
"""
import urllib.parse

import pytest

GW3000_WS90_FORM = {'PASSKEY': '0123456789ABCDEF0123456789ABCDEF', 'stationtype': 'GW3000B_V1.2.0', 'runtime': '1334', 'heap': '80116', 'dateutc': '2026-09-02 06:50:05', 'tempinf': '73.04', 'humidityin': '49', 'baromrelin': '28.691', 'baromabsin': '28.691', 'tempf': '77.00', 'humidity': '67', 'vpd': '0.309', 'winddir': '135', 'winddir_avg10m': '135', 'windspeedmph': '0.00', 'windgustmph': '1.12', 'maxdailygust': '2.24', 'solarradiation': '0.00', 'uv': '0', 'rrain_piezo': '0.000', 'erain_piezo': '0.000', 'hrain_piezo': '0.000', 'last24hrain_piezo': '0.000', 'drain_piezo': '0.000', 'wrain_piezo': '0.000', 'mrain_piezo': '0.000', 'yrain_piezo': '0.000', 'srain_piezo': '0', 'ws90cap_volt': '0.2', 'ws90_ver': '161', 'wh90batt': '3.28', 'freq': '915M', 'model': 'GW3000B', 'interval': '16'}

H_URL = "/ingest/ecowitt?token="
INGEST = "test-ingest-token"
API_H = {"Authorization": "Bearer test-api-token"}


def _post(client, token, form=GW3000_WS90_FORM):
    return client.post(H_URL + token,
                       data=urllib.parse.urlencode(form),
                       headers={"Content-Type": "application/x-www-form-urlencoded"})


def test_gw3000_ws90_upload_is_stored_field_for_field(client):
    r = _post(client, INGEST)
    assert r.status_code == 200, r.text
    mac = r.json()["mac"]
    assert mac.startswith("EC:EC:")                      # synthetic, from PASSKEY
    cur = client.get(f"/api/devices/{mac}/current", headers=API_H).json()
    src = cur.pop("_source")
    assert src["device"]["model"] == "GW3000B"
    assert src["device"]["battery_outdoor"] == "normal"  # wh90batt 3.28 V
    # Outdoor, wind, sun: US-native as posted.
    assert cur["tempf"] == 77.0 and cur["humidity"] == 67.0
    assert cur["windspeedmph"] == 0.0 and cur["windgustmph"] == 1.12
    assert cur["winddir"] == 135.0 and cur["maxdailygust"] == 2.24
    assert cur["uv"] == 0.0 and cur["solarradiation"] == 0.0
    # Pressure: the gateway sends abs == rel until the operator calibrates.
    assert cur["baromrelin"] == 28.691 and cur["baromabsin"] == 28.691
    # Indoor comes from the gateway's own sensor, not the outdoor array.
    assert cur["tempinf"] == 73.04 and cur["humidityin"] == 49.0
    assert cur["dewPointin"] == 52.7                     # derived
    # The WS90 is haptic: piezo windows fill the *rainin slots.
    for k in ("hourlyrainin", "eventrainin", "dailyrainin",
              "weeklyrainin", "monthlyrainin", "yearlyrainin"):
        assert cur[k] == 0.0, k
    # Battery: voltage normalized to the AWN 1/0 flag the apps render.
    assert cur["battout"] == 1 and cur["battin"] is None
    # Outdoor dew point is DERIVED (the device never sends one): 77 F / 67 %.
    assert cur["dewPoint"] == pytest.approx(65.4, abs=0.3)
    # Feels-like at 77 F is the air temperature (heat index starts at 80).
    assert cur["feelsLike"] == 77.0


def test_gw3000_ws90_upload_names_the_station_after_the_gateway(client):
    mac = _post(client, INGEST).json()["mac"]
    devs = client.get("/api/devices", headers=API_H).json()
    devs = devs if isinstance(devs, list) else devs.get("devices", devs)
    me = next(d for d in devs if d["mac"] == mac)
    assert me["name"] == "Ecowitt (GW3000B)"


def test_gw3000_low_battery_flags_the_array(client):
    form = dict(GW3000_WS90_FORM, wh90batt="2.30")      # below the 2.4 V line
    mac = _post(client, INGEST, form).json()["mac"]
    cur = client.get(f"/api/devices/{mac}/current", headers=API_H).json()
    assert cur["battout"] == 0
