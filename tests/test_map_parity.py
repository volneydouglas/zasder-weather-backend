"""The two ends of the map beacon, in one test (R23).

`app/map_beacon.py` signs what the backend sends; `map-directory/app/main.py`
decides what the directory accepts. Each has its own suite and, until this
file, the suites never met: a change to the canonical form, the envelope
shape, the rotation proof or the withdrawal tombstone on one side would
pass both suites and fail only on the wire. Here the backend's real
`build()` + `sign()` output is posted to the directory's real FastAPI app.

The directory is imported by FILE PATH under its own module name: both
packages are called `app`, and the backend's is already the one on
sys.path. Skipped cleanly when `map-directory/` is not alongside (a
checkout that dropped it) or its dependencies are not in this venv.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).resolve().parents[2] / "map-directory"
DIRECTORY_MAIN = DIRECTORY / "app" / "main.py"

pytestmark = pytest.mark.skipif(
    not DIRECTORY_MAIN.exists(),
    reason="map-directory/ is not alongside backend/ in this checkout")


def _station(mac="AA:BB:CC:DD:EE:71", lat=33.3062, lon=-111.8413, name="Backyard"):
    return {"mac": mac, "name": name,
            "info": {"coords": {"coords": {"lat": lat, "lon": lon}}, "type": "tempest"},
            "lastData": {"dateutc": 1789200000000, "tempf": 99.4, "humidity": 21,
                         "windspeedmph": 4, "winddir": 250, "baromrelin": 29.71,
                         "dailyrainin": 0, "tempinf": 78, "co2": 640, "pm25": 3}}


@pytest.fixture
def directory(tmp_path):
    """The directory's app, its database in this test's tmp_path. Loaded
    by path so it never collides with the backend's `app` package; the
    module has no relative imports, so a spec load is all it needs."""
    spec = importlib.util.spec_from_file_location("map_directory_main", DIRECTORY_MAIN)
    mod = importlib.util.module_from_spec(spec)
    # Registered under its own name BEFORE it runs: the directory's models
    # use `from __future__ import annotations`, and pydantic resolves those
    # strings through sys.modules[cls.__module__]; an unregistered module
    # leaves `Envelope` "not fully defined" at the first request.
    sys.modules[spec.name] = mod
    try:
        try:
            spec.loader.exec_module(mod)
        except ImportError as e:                   # a dependency this venv lacks
            pytest.skip(f"map-directory dependency missing from this venv: {e}")
        mod.DB_PATH = str(tmp_path / "map.db")     # read at call time, never at import
        from fastapi.testclient import TestClient
        with TestClient(mod.app) as c:
            yield mod, c
    finally:
        sys.modules.pop(spec.name, None)


def test_the_backends_signed_beacon_is_what_the_directory_accepts(client, directory, monkeypatch):
    from app import map_beacon as mb, config
    from cryptography.hazmat.primitives.asymmetric import ed25519
    mod, dc = directory
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    key = asyncio.run(mb.ensure_key())
    now = int(time.time() * 1000)

    # 1. A beacon exactly as the tick builds it, in `id` mode with a region.
    beacon = mb.build(server_id="srv-parity", station=_station(),
                      cfg={"name_visible": True, "link_mode": "id"}, now_ms=now,
                      visit_url="https://weather.example.org/", region="AZ")
    assert beacon is not None
    r = dc.post("/v1/beacons", json=mb.sign(beacon, key))
    assert r.status_code == 200, r.text
    reply = r.json()
    assert reply["station_id"] == beacon["station_id"]
    assert reply["link_mode"] == "id" and reply["public_id"].startswith("AZ")
    assert reply["visit"] == f"{mod.BASE_URL}/s/{reply['public_id']}"

    # ...and it is on the map, with the readings the backend sent and
    # nothing the backend keeps private.
    geo = dc.get("/v1/beacons").json()
    assert geo["count"] == 1
    props = geo["features"][0]["properties"]
    assert props["station_id"] == beacon["station_id"]
    assert props["name"] == "Backyard" and props["sensor"] == "tempest"
    assert props["conditions"] == {k: float(v) for k, v in beacon["conditions"].items()}
    assert "weather.example.org" not in dc.get("/v1/beacons").text   # id mode
    assert "tempinf" not in dc.get("/v1/beacons").text
    assert "AA:BB" not in dc.get("/v1/beacons").text

    # 2. The withdrawal tombstone as the switch-off sends it.
    mod._last_withdraw_ms.clear()
    tomb = mb.withdrawal(server_id="srv-parity", station_id_=beacon["station_id"],
                         now_ms=now + 1)
    r = dc.post("/v1/withdraw", json=mb.sign(tomb, key))
    assert r.status_code == 200 and r.json()["removed"] == 1, r.text
    assert dc.get("/v1/beacons").json()["count"] == 0

    # 3. A rotation proof as the backend produces it (`prev` rides along):
    # the new key is accepted with the old key's blessing, and the directory
    # answers rotated=true, which is what the backend waits for.
    mod._last_seen_ms.clear()
    new_key = ed25519.Ed25519PrivateKey.generate()
    later = mb.build(server_id="srv-parity", station=_station(), cfg={}, now_ms=now + 2)
    r = dc.post("/v1/beacons", json=mb.sign(later, new_key, prev=key))
    assert r.status_code == 200 and r.json()["rotated"] is True, r.text
    # The old key alone is refused from here on.
    mod._last_seen_ms.clear()
    again = mb.build(server_id="srv-parity", station=_station(), cfg={}, now_ms=now + 3)
    assert dc.post("/v1/beacons", json=mb.sign(again, key)).status_code == 403

    # 4. The beacon stays under the directory's body cap with room to
    # spare: a field added on the backend side that pushes it over would
    # be refused on the wire, not here.
    import json
    assert len(json.dumps(mb.sign(later, new_key))) < mod.MAX_BODY // 2
