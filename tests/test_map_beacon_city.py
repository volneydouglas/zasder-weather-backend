"""Map location (2.5, C14): a real city, and a pin the owner places.

"City" used to be a 10 km grid snap, so the pin sat within about seven
km of the house and read as "approximate". Now it is the centre of the
nearest populated place from a bundled gazetteer; and "custom" is a
point the owner sets, at most 100 miles away.
"""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

from app import map_beacon as mb  # noqa: E402

CHANDLER_HOUSE = (33.2941, -111.9202)


def _station(lat, lon, mac="AA:BB:CC:00:00:01"):
    return {"mac": mac, "name": "Yard", "lastData": {"dateutc": 1_790_000_000_000, "tempf": 90.0},
            "info": {"coords": {"coords": {"lat": lat, "lon": lon}}}}


def test_the_city_is_a_real_place_and_every_house_in_it_shares_one_point():
    a = mb.nearest_place(*CHANDLER_HOUSE)
    b = mb.nearest_place(33.3107, -111.8580)
    assert a["label"] == "Chandler, AZ" and b["label"] == "Chandler, AZ"
    assert (a["lat"], a["lon"]) == (b["lat"], b["lon"]) == (33.3062, -111.8413)


def test_a_remote_station_falls_back_to_the_grid():
    # Middle of the Pacific: no town within 80 km.
    assert mb.nearest_place(0.0, -150.0) is None
    lat, lon = mb.place(0.0, -150.0, "city")
    assert abs(lat) < 0.1 and abs(lon + 150.0) < 0.1


def test_a_city_beacon_names_the_place_and_keeps_its_precision():
    b = mb.build(server_id="srv1", station=_station(*CHANDLER_HOUSE),
                 cfg={"location_precision": "city"}, now_ms=1_790_000_000_000)
    assert b["precision"] == "city" and b["place"] == "Chandler, AZ"
    assert (b["lat"], b["lon"]) == (33.3062, -111.8413)


def test_an_owner_placed_pin_travels_as_area_and_says_who_placed_it():
    cfg = {"location_precision": "custom", "custom_lat": 33.45, "custom_lon": -112.07,
           "custom_mac": "AA:BB:CC:00:00:01"}
    b = mb.build(server_id="srv1", station=_station(*CHANDLER_HOUSE), cfg=cfg,
                 now_ms=1_790_000_000_000)
    assert b["precision"] == "area" and b["placed_by"] == "owner"
    assert (b["lat"], b["lon"]) == (33.45, -112.07)


def test_a_placed_pin_that_no_longer_fits_falls_back_to_the_city():
    far = {"location_precision": "custom", "custom_lat": 36.0, "custom_lon": -115.0,
           "custom_mac": "AA:BB:CC:00:00:01"}
    b = mb.build(server_id="srv1", station=_station(*CHANDLER_HOUSE), cfg=far,
                 now_ms=1_790_000_000_000)
    assert b["precision"] == "city" and "placed_by" not in b
    other = dict(far, custom_lat=33.45, custom_lon=-112.07, custom_mac="AA:BB:CC:00:00:99")
    b = mb.build(server_id="srv1", station=_station(*CHANDLER_HOUSE), cfg=other,
                 now_ms=1_790_000_000_000)
    assert b["precision"] == "city", "a point set for another station is not this one's"


H = {"Authorization": "Bearer test-api-token"}


def test_the_owner_can_place_a_pin_within_a_hundred_miles_and_no_farther(client):
    import time as _time
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(_time.time() - 300, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:00:00:01", "name": "Yard",
                                 "coords": {"lat": CHANDLER_HOUSE[0], "lon": CHANDLER_HOUSE[1]}},
                      "timestamp_utc": stamp, "outdoor": {"tempf": 98}})
    ok = client.put("/api/map", headers=H, json={
        "location_precision": "custom", "custom_lat": 33.45, "custom_lon": -112.07})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["location_precision"] == "custom"
    assert (body["custom_lat"], body["custom_lon"]) == (33.45, -112.07)
    assert body["city_label"] == "Chandler, AZ"
    far = client.put("/api/map", headers=H, json={"custom_lat": 36.1, "custom_lon": -115.1})
    assert far.status_code == 400 and "100 miles" in far.json()["detail"]
    half = client.put("/api/map", headers=H, json={"custom_lat": 33.4})
    assert half.status_code == 400
