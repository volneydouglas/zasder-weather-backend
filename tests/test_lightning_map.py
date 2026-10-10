"""Lightning rings (2.6): the detector knows distance, not direction, so a
strike is a ring around the station. The centre never reveals more than
the station map already publishes."""
import time
from datetime import datetime, timezone

import pytest

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:DD:EE:91"
LAT, LON = 33.45, -111.9


def _post(client, at_ms, strike_ms=None, dist=None, hour=0, mac=MAC):
    lt = {"strike_count_last_1hr": hour}
    if strike_ms is not None:
        lt["last_strike_ms"] = strike_ms
    if dist is not None:
        lt["last_distance_mi"] = dist
    stamp = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = client.post("/ingest/custom", headers=IH, json={
        "device": {"id": mac, "name": "Arts", "coords": {"lat": LAT, "lon": LON}},
        "timestamp_utc": stamp, "outdoor": {"tempf": 90.0, "humidity": 20},
        "wind": {}, "rain": {}, "pressure": {"relative_inhg": 29.9},
        "lightning": lt})
    assert r.status_code == 200, r.text


def _storm(client, now_ms):
    """Two strikes, each repeated across readings the way a Tempest does,
    plus one outside the 3h window."""
    s_old, s1, s2 = now_ms - 4 * 3_600_000, now_ms - 50 * 60_000, now_ms - 5 * 60_000
    _post(client, s_old + 1000, s_old, 20.0, 1)
    _post(client, s1 + 60_000, s1, 12.0, 1)
    _post(client, s1 + 120_000, s1, 12.0, 1)
    _post(client, s2 + 30_000, s2, 4.0, 2)
    _post(client, s2 + 90_000, s2, 4.0, 2)
    return s1, s2


# ── pure ─────────────────────────────────────────────────────────────────

def test_readings_collapse_to_strikes_newest_first():
    from app import lightning_map as lm
    now = 10_000_000_000
    rows = [{"ms": now - 600_000, "distance_mi": 8.0},
            {"ms": now - 600_000, "distance_mi": 8.0},
            {"ms": now - 60_000, "distance_mi": 3.0},
            {"ms": now - 4 * 3_600_000, "distance_mi": 9.0},  # outside
            {"ms": now - 120_000, "distance_mi": None},       # garbled
            {"ms": True, "distance_mi": 5.0}]                 # not a time
    out = lm.strikes_in_window(rows, now)
    assert [s["distance_mi"] for s in out] == [3.0, 8.0]
    assert [s["age_min"] for s in out] == [1, 10]


def test_age_colours_run_white_to_dark_red_and_count_per_bin():
    from app import lightning_map as lm
    assert lm.age_color(0) == "#ffffff"
    assert lm.age_color(9) == "#ffffff" and lm.age_color(10) == "#ffe14d"
    assert lm.age_color(59) == "#ff7a1a" and lm.age_color(60) == "#e53935"
    assert lm.age_color(179) == lm.age_color(10_000) == "#8b1a1a"
    ss = [{"age_min": a, "distance_mi": 5.0} for a in (1, 2, 15, 70, 150)]
    assert lm.bin_counts(ss) == [2, 1, 0, 0, 1, 1]


def _ss(*pairs):
    return [{"age_min": a, "distance_mi": d} for a, d in pairs]


def test_the_trend_compares_the_last_half_hour_with_the_one_before():
    from app import lightning_map as lm
    assert lm.trend(_ss((5, 4.0), (12, 6.0), (40, 12.0), (50, 14.0)))["verdict"] == "approaching"
    assert lm.trend(_ss((5, 20.0), (45, 8.0)))["verdict"] == "moving away"
    # Less than one sensor band of change is not movement.
    t = lm.trend(_ss((5, 10.0), (45, 12.0)))
    assert t == {"verdict": "holding", "recent_mi": 10.0, "earlier_mi": 12.0}
    # Exactly one band is.
    assert lm.trend(_ss((5, 9.0), (45, 12.0)))["verdict"] == "approaching"
    # Nothing to compare: no verdict, never a guess.
    assert lm.trend(_ss((5, 4.0), (20, 6.0))) is None
    assert lm.trend(_ss((45, 4.0), (100, 6.0))) is None
    assert lm.trend([]) is None


def test_the_zoom_fits_the_farthest_ring():
    from app import lightning_map as lm
    assert lm.zoom_for(33.4, 10) > lm.zoom_for(33.4, 40)
    z = lm.zoom_for(33.4, 13)
    import math
    m_per_px = 156543.03392 * math.cos(math.radians(33.4)) / 2 ** z
    assert 13 * lm.MI_TO_M / m_per_px <= lm.SIZE_PX / 2


def test_no_centre_draws_rings_without_a_basemap():
    from app import lightning_map as lm
    now = 10_000_000_000
    html = lm.render_card([{"ms": now - 60_000, "distance_mi": 4.0}], now, None)
    assert "lt-plain" in html and "<img" not in html
    assert "openstreetmap" not in html.lower()
    assert "Last strike 4 mi away" in html and "lt-near" in html
    assert "not which way" in html
    # Coloured by age, with the legend, counts and the distance-over-time strip.
    assert 'stroke="#ffffff"' in html
    assert 'class="lt-legend"' in html and "0-10 min<b>1</b>" in html
    assert 'class="lt-time"' in html and 'fill="#ffffff"' in html


def test_the_card_says_when_the_storm_is_coming():
    from app import lightning_map as lm
    now = 10_000_000_000
    rows = [{"ms": now - m * 60_000, "distance_mi": d}
            for m, d in ((2, 5.0), (12, 6.0), (40, 14.0), (55, 17.0))]
    html = lm.render_card(rows, now, None)
    assert "Approaching: about 6 mi in the last 30 min, 16 mi the 30 before" in html


def test_a_centre_draws_osm_tiles_with_attribution():
    from app import lightning_map as lm
    now = 10_000_000_000
    html = lm.render_card([{"ms": now - 60_000, "distance_mi": 12.0}], now, (LAT, LON))
    assert 'src="https://tile.openstreetmap.org/' in html
    assert "OpenStreetMap</a> contributors" in html
    assert "lt-near" not in html
    # The coordinates themselves are never written into the page.
    assert str(LAT) not in html and str(LON) not in html


def test_the_chip_is_absent_without_a_detector_and_clear_at_zero():
    from app import lightning_map as lm
    assert lm.chip({"tempf": 80}) is None
    assert lm.chip({"lightning_last_1hr": True}) is None
    assert "Clear" in lm.chip({"lightning_last_1hr": 0})
    c = lm.chip({"lightning_last_1hr": 3, "lightning_distance_mi": 4})
    assert "3/hr · 4 mi" in c and "#ff5a5f" in c


# ── the route ────────────────────────────────────────────────────────────

def test_the_owner_gets_strikes_and_the_station_position(client):
    now = int(time.time() * 1000)
    s1, s2 = _storm(client, now)
    r = client.get(f"/api/devices/{MAC}/lightning/strikes", headers=H)
    assert r.status_code == 200, r.text
    j = r.json()
    assert [s["ms"] for s in j["strikes"]] == [s2, s1]
    assert [s["distance_mi"] for s in j["strikes"]] == [4.0, 12.0]
    assert j["center"] == {"lat": LAT, "lon": LON}
    assert j["band_mi"] == 3.0 and j["reference_rings_mi"] == [3.0, 6.0, 10.0]
    # 4 mi in the last half hour against 12 mi the half hour before.
    assert j["trend"] == {"verdict": "approaching", "recent_mi": 4.0, "earlier_mi": 12.0}
    # A wider window reaches the older strike.
    j6 = client.get(f"/api/devices/{MAC}/lightning/strikes?hours=6", headers=H).json()
    assert len(j6["strikes"]) == 3


def test_a_guest_never_gets_the_exact_position(client):
    now = int(time.time() * 1000)
    _storm(client, now)
    tok = client.post("/api/guest-tokens", headers=H).json()["token"]
    gh = {"Authorization": f"Bearer {tok}"}
    # Map sharing off: strikes, no centre.
    j = client.get(f"/api/devices/{MAC}/lightning/strikes", headers=gh).json()
    assert len(j["strikes"]) == 2 and j["center"] is None
    # Shared at city precision: the published point, not the station.
    assert client.put("/api/map", headers=H, json={
        "enabled": True, "location_precision": "city"}).status_code == 200
    c = client.get(f"/api/devices/{MAC}/lightning/strikes", headers=gh).json()["center"]
    assert c is not None and (c["lat"], c["lon"]) != (LAT, LON)
    from app import map_beacon as mb
    assert (c["lat"], c["lon"]) == mb.place(LAT, LON, "city")


def test_the_route_needs_a_token_and_a_known_station(client):
    assert client.get(f"/api/devices/{MAC}/lightning/strikes").status_code == 401
    assert client.get("/api/devices/AA:BB:CC:DD:EE:00/lightning/strikes",
                      headers=H).status_code == 404


# ── the public page ──────────────────────────────────────────────────────

@pytest.fixture
def public(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    return client


def test_the_public_page_shows_the_rings_on_the_published_point(public):
    from app import main as M
    now = int(time.time() * 1000)
    _storm(public, now)
    M._PUBLIC_DASH_CACHE = None
    r = public.get("/embed")
    html = r.text
    assert 'class="chart chart-lightning"' in html and "Lightning</span>" in html
    assert "lt-plain" in html, "map sharing off: no basemap"
    assert "tile.openstreetmap.org" in r.headers["content-security-policy"]
    public.put("/api/map", headers=H, json={"enabled": True, "location_precision": "exact"})
    M._PUBLIC_DASH_CACHE = None
    html = public.get("/embed").text
    assert 'src="https://tile.openstreetmap.org/' in html


def test_a_quiet_detector_gets_the_chip_but_no_card(public):
    from app import main as M
    now = int(time.time() * 1000)
    _post(public, now - 60_000, hour=0)
    M._PUBLIC_DASH_CACHE = None
    html = public.get("/embed").text
    assert "Clear" in html and 'class="chart chart-lightning"' not in html


# ── the station map link (2.6, Volney 10-10) ─────────────────────────────

def test_a_station_on_the_map_links_to_its_pin_and_one_off_it_does_not(public):
    from app import main as M, map_beacon as mb
    import asyncio
    now = int(time.time() * 1000)
    _post(public, now - 60_000, hour=0)
    M._PUBLIC_DASH_CACHE = None
    assert "On the station map" not in public.get("/embed").text
    public.put("/api/map", headers=H, json={"enabled": True, "location_precision": "city"})
    M._PUBLIC_DASH_CACHE = None
    html = public.get("/embed").text
    sid = mb.station_id(asyncio.run(M.ensure_server_id()), MAC)
    assert f'href="https://maps.zasder.com/?station={sid}"' in html
    assert "On the station map" in html
    # The link names the pin, never the station's MAC or coordinates.
    assert MAC not in html and str(LAT) not in html
    public.put("/api/map", headers=H, json={"enabled": False})
    M._PUBLIC_DASH_CACHE = None
    assert "On the station map" not in public.get("/embed").text


# ── follow-up review (2026-10-10): sampling, the cap, the poles ──────────

def test_the_count_is_the_detectors_not_the_number_of_rings(client):
    """F26-04: one sampled last-strike report while the detector counted 15."""
    from app import lightning_map as lm
    now = int(time.time() * 1000)
    _post(client, now - 1000, strike_ms=now - 2000, dist=4.0, hour=15)
    body = client.get(f"/api/devices/{MAC}/lightning/strikes", headers=H).json()
    assert len(body["strikes"]) == 1
    assert body["count_1h"] == 15 and body["now_ms"] >= now and body["truncated"] is False
    html = lm.render_card(body["strikes"], now, None, (body["count_1h"], body["count_3h"]))
    assert "The detector counted 15 strikes in the last hour" in html
    assert "1 strikes" not in html and "strikes in the last 3 hours" not in html
    assert "distance report" in html


def test_a_long_window_is_thinned_flagged_and_still_trends(client):
    """F26-04: past MAX_DRAWN reports the drawing is a sample, said so, and
    the trend still comes from every report."""
    import asyncio
    from app import db, lightning_map as lm
    now = int(time.time() * 1000)
    _post(client, now - 1000, hour=221)
    rows = []
    for i in range(201):
        at = now - 10_000 - i * 5_000
        rows.append({"dateutc": at, "lightning_last_strike_ms": at, "lightning_distance_mi": 4.0})
    for i in range(20):
        at = now - 40 * 60_000 - i * 1_000
        rows.append({"dateutc": at, "lightning_last_strike_ms": at, "lightning_distance_mi": 12.0})
    asyncio.run(db.insert_observations(MAC, rows))
    body = client.get(f"/api/devices/{MAC}/lightning/strikes", headers=H).json()
    assert len(body["strikes"]) == lm.MAX_DRAWN and body["truncated"] is True
    assert body["trend"]["verdict"] == "approaching"
    # The sample spans the window: the oldest reports are still represented.
    assert body["strikes"][-1]["distance_mi"] == 12.0
    assert body["strikes"][0]["ms"] == max(r["dateutc"] for r in rows)
    html = lm.render_card([{"ms": r["dateutc"], "distance_mi": r["lightning_distance_mi"]}
                           for r in rows], now, None)
    assert "Showing a sample of the reports." in html and "Approaching" in html


def test_thin_keeps_the_newest_and_the_oldest():
    from app import lightning_map as lm
    xs = [{"ms": i} for i in range(1000, 0, -1)]
    out = lm.thin(xs, 10)
    assert len(out) == 10 and out[0] is xs[0] and out[-1] is xs[-1]
    assert lm.thin(xs[:5], 10) == xs[:5]


@pytest.mark.parametrize("center", [(90.0, 0.0), (-90.0, 0.0), (85.06, 10.0), (10**400, 0),
                                    (float("nan"), 0.0), (0.0, float("inf")), (12.0, 181.0)])
def test_unmappable_centres_draw_plain_rings(center):
    """F26-05: the poles (and garbage) fall back to rings without a basemap."""
    from app import lightning_map as lm
    now = 1_791_000_000_000
    html = lm.render_card([{"ms": now - 1000, "distance_mi": 4.0}], now, center)
    assert "lt-plain" in html and "<img" not in html


def test_the_mercator_limit_itself_still_maps():
    from app import lightning_map as lm
    now = 1_791_000_000_000
    html = lm.render_card([{"ms": now - 1000, "distance_mi": 4.0}], now, (85.0, 10.0))
    assert "<img" in html


def test_a_failing_card_never_takes_the_page_down(monkeypatch):
    from app import lightning_map as lm, public_dashboard as pd

    def boom(*a, **k):
        raise ZeroDivisionError
    monkeypatch.setattr(lm, "render_card", boom)
    html = pd.render_station("Arts", {"tempf": 80.0}, {}, ["tempf"],
                             lightning={"strikes": [], "now_ms": 0, "center": (1.0, 1.0)})
    assert "Arts" in html and "chart-lightning" not in html


@pytest.mark.parametrize("coords", [{"lat": "north", "lon": 1}, {"lat": 1, "lon": [2]},
                                    {"lat": float("nan"), "lon": 1}, {"lat": 1, "lon": "inf"},
                                    {"lat": 10**400, "lon": 1}])
def test_malformed_stored_coordinates_are_no_position(coords):
    """CodeRabbit on PR #87: a stored coordinate that is not a finite number
    must read as no position, not 500 the strikes route or the public page."""
    from app.share_targets import _coords
    assert _coords({"info": {"coords": {"coords": coords}}}) is None
    assert _coords({"info": {"coords": {"coords": {"lat": "33.4", "lon": -111.9}}}}) == (33.4, -111.9)


def test_the_strikes_route_survives_malformed_coordinates(client, monkeypatch):
    from app import db
    real = db.list_devices

    async def broken():
        out = await real()
        for d in out:
            d.setdefault("info", {})["coords"] = {"coords": {"lat": "north", "lon": "west"}}
        return out
    now = int(time.time() * 1000)
    _post(client, now - 1000, strike_ms=now - 2000, dist=4.0, hour=1)
    monkeypatch.setattr(db, "list_devices", broken)
    r = client.get(f"/api/devices/{MAC}/lightning/strikes", headers=H)
    assert r.status_code == 200 and r.json()["center"] is None
