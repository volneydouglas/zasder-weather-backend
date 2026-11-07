"""The station display (2.8): a 1280x800 room display for a Pi screen, a TV
or a tablet. /display is a static shell, /display/data its anonymous feed
(public dashboard on, public stations only), /api/display its token feed."""
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:00:00:D1"
THERMO = "3C:71:BF:0A:2E:94"
AIR = "5E:21:7C:9A:40:11"
LAT, LON = 33.30, -111.94
STATIC = Path(__file__).resolve().parent.parent / "app" / "static" / "display"


def _post(c, at_ms, mac=MAC, name="Backyard", **blocks):
    stamp = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"device": {"id": mac, "name": name, "coords": {"lat": LAT, "lon": LON}},
            "timestamp_utc": stamp}
    body.update(blocks)
    r = c.post("/ingest/custom", headers=IH, json=body)
    assert r.status_code == 200, r.text


def _seed_full(c):
    now = int(time.time() * 1000)
    for k in range(0, 24 * 6, 2):                       # every 20 min for 24 h
        t = now - k * 600_000
        _post(c, t, outdoor={"tempf": 60.0 + (k % 18), "humidity": 40, "uv": 3, "solar_wm2": 400},
              indoor={"tempf": 72.0, "humidity": 45},
              wind={"speed_mph": 4, "gust_mph": 9 + (k % 5), "direction": 200},
              rain={"daily_in": 0.0, "hourly_in": 0.0},
              pressure={"relative_inhg": 29.90 + (k % 7) * 0.01})


def _guest(c):
    r = c.post("/api/guest-tokens", headers=H, json={"label": "Kitchen display"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _finite_or_null(obj, path="$"):
    """Every number in the payload is finite: NaN and infinity never reach
    a page that does arithmetic on them."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _finite_or_null(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _finite_or_null(v, f"{path}[{i}]")
    elif isinstance(obj, float):
        assert math.isfinite(obj), path


@pytest.fixture
def public(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    return client


# ── the shell ─────────────────────────────────────────────────────────

def test_the_shell_is_open_bare_and_strict(client):
    r = client.get("/display")
    assert r.status_code == 200
    html = r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    # One script, same-origin; nothing inline to run, no third-party host.
    assert re.findall(r"<script[^>]*>", html) == ['<script src="/static/display/display.js">']
    assert not re.search(r"https?://", html)
    js = (STATIC / "display.js").read_text()
    assert not re.search(r"https?://", js), "the display talks only to its own server"
    assert "/api/display" in js and "/display/data" in js
    # No readings, names or settings in the shell itself.
    assert "AA:BB" not in html and "Backyard" not in html


def test_an_idle_pointer_hides_and_comes_back():
    # Volney's Pi kiosk, 11-05: the mouse sat over OUTSIDE NOW for good.
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "display.js").read_text()
    assert re.search(r"html\.idle[^{]*\{[^}]*cursor:\s*none", html)
    assert 'classList.add("idle")' in js and 'classList.remove("idle")' in js
    for ev in ("mousemove", "mousedown", "touchstart"):
        assert f'"{ev}"' in js, ev


def test_the_fonts_are_served_from_this_server(client):
    for name in ("barlow-500.woff2", "barlow-semi-condensed-300.woff2", "jetbrains-mono.woff2"):
        assert client.get(f"/static/display/fonts/{name}").status_code == 200, name
    html = (STATIC / "index.html").read_text()
    assert "fonts.googleapis" not in html and "/static/display/fonts/" in html


# ── the anonymous feed ────────────────────────────────────────────────

def test_the_anonymous_feed_is_off_with_the_public_pages(client):
    _seed_full(client)
    assert client.get("/display/data").status_code == 404


def test_the_anonymous_feed_shows_public_stations_without_the_private_parts(public):
    _seed_full(public)
    _post(public, int(time.time() * 1000), mac=AIR, name="Kitchen Air", air={"pm25": 4.0, "co2": 600})
    r = public.get("/display/data")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["public"] is True and d["tz"] == "UTC"
    st = d["stations"][0]
    assert re.fullmatch(r"[0-9a-f]{16}", st["id"])          # public id, not the MAC
    raw = r.text
    assert MAC not in raw and MAC.replace(":", "") not in raw
    assert str(LAT) not in raw and str(LON) not in raw       # no coordinates
    assert "tempinf" not in st["current"] and "humidityin" not in st["current"]
    assert "can_write" not in d
    # The primary-station default: the air monitor is not on the page
    # unless the owner put it there.
    assert d["air"] is None
    _finite_or_null(d)


# ── the token feed ────────────────────────────────────────────────────

def test_the_token_feed_needs_a_token_and_says_who_it_is_for(client):
    _seed_full(client)
    assert client.get("/api/display").status_code == 401
    owner = client.get("/api/display", headers=H).json()
    assert owner["can_write"] is True
    guest = client.get("/api/display", headers=_guest(client)).json()
    assert guest["can_write"] is False
    assert guest["stations"][0]["id"] == MAC
    assert guest["stations"][0]["current"]["tempinf"] == 72.0   # private mode keeps indoor


def test_the_payload_contract(client):
    _seed_full(client)
    _post(client, int(time.time() * 1000), mac=AIR, name="Kitchen Air", air={"pm25": 4.0, "co2": 600})
    d = client.get("/api/display", headers=H).json()
    _finite_or_null(d)
    assert {s["id"] for s in d["stations"]} == {MAC}            # the monitor is not a station
    assert d["air"]["pm25"] == 4.0 and d["air"]["co2"] == 600.0
    st = d["stations"][0]
    n = len(st["t"])
    assert 140 <= n <= 145                                       # 24 h of 10-minute points
    assert all(len(v) == n for v in st["series"].values())
    assert st["t"] == sorted(st["t"])
    assert st["hi"][1] >= st["lo"][1]
    assert st["gust24"] >= 9
    assert len(st["rain_hours"]) in (24, 25)
    assert all(v == 0 for v in st["rain_hours"] if v is not None)
    assert d["forecast"] == [] and d["alerts"] == []             # upstreams stubbed
    assert d["sun"] and d["sun"][0] < d["sun"][1]


def test_absent_is_not_zero(client):
    now = int(time.time() * 1000)
    for k in range(0, 30, 3):
        _post(client, now - k * 600_000, mac=THERMO, name="Shed", outdoor={"tempf": 70.0})
    d = client.get("/api/display", headers=H).json()
    c = d["stations"][0]["current"]
    assert c["tempf"] == 70.0
    for f in ("humidity", "windspeedmph", "baromrelin", "uv", "solarradiation", "dailyrainin"):
        assert c[f] is None, f
    st = d["stations"][0]
    assert all(v is None for v in st["series"]["windspeedmph"])
    assert all(v is None for v in st["rain_hours"])
    assert st["gust24"] is None and st["press3h"] is None


def test_the_payload_is_built_once_per_window(client, monkeypatch):
    from app import station_display as sd
    _seed_full(client)
    calls = []
    real = sd.build

    async def counting(*a, **k):
        calls.append(1)
        return await real(*a, **k)
    monkeypatch.setattr(sd, "build", counting)
    for _ in range(3):
        assert client.get("/api/display", headers=H).status_code == 200
    assert len(calls) == 1


# ── alerts ────────────────────────────────────────────────────────────

def test_alerts_are_real_current_and_trimmed_for_the_public(public, monkeypatch):
    from app import station_display as sd
    _seed_full(public)
    soon = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
    gone = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()

    async def fake(lat, lon):  # noqa: ANN001
        return [
            {"event": "Heat Advisory", "status": "Actual", "ends": soon, "severity": "Moderate",
             "headline": "Heat Advisory for Central Maricopa County"},
            {"event": "Flash Flood Warning", "status": "Actual", "ends": soon, "severity": "Severe",
             "headline": "Flash Flood Warning for Chandler"},
            {"event": "Wind Advisory", "status": "Actual", "ends": gone, "severity": "Moderate"},
            {"event": "Test Message", "status": "Test", "ends": soon},
        ]
    monkeypatch.setattr(sd, "_fetch_alerts", fake)
    pub = public.get("/display/data").json()["alerts"]
    assert [a["event"] for a in pub] == ["Flash Flood Warning", "Heat Advisory"]   # warnings first
    assert pub[0]["warning"] is True
    assert all("headline" not in a for a in pub)              # county names stay off the public feed
    sd._reset_for_tests()
    monkeypatch.setattr(sd, "_fetch_alerts", fake)
    priv = public.get("/api/display", headers=H).json()["alerts"]
    assert priv[0]["headline"] == "Flash Flood Warning for Chandler"


# ── the arithmetic ────────────────────────────────────────────────────

def test_downsample_keeps_peaks_and_gaps():
    from app import station_display as sd
    start = 1_800_000_000_000
    rows = [{"dateutc": start + 60_000, "tempf": 60.0, "windgustmph": 5.0, "windgustmph_max": 12.0},
            {"dateutc": start + 120_000, "tempf": 62.0, "windgustmph": 6.0, "windgustmph_max": 18.0},
            {"dateutc": start + 1_260_000, "tempf": float("nan")}]
    t, s = sd.downsample(rows, start, start + 1_800_000)
    assert len(t) == 3
    assert s["tempf"] == [61.0, None, None]                   # NaN is no reading
    assert s["windgustmph"] == [18.0, None, None]              # the bucket's peak, not its mean


def test_rain_by_hour_reads_the_day_total_and_the_counter():
    from app import station_display as sd
    tz = timezone.utc
    start = int(datetime(2026, 7, 1, 22, 0, tzinfo=tz).timestamp() * 1000)
    end = start + 3 * 3_600_000
    day = [{"dateutc": start + 10 * 60_000, "dailyrainin": 0.10},
           {"dateutc": start + 40 * 60_000, "dailyrainin": 0.30},      # +0.20 in hour 0
           {"dateutc": start + 3_600_000 + 50 * 60_000, "dailyrainin": 0.35},  # +0.05 in hour 1
           {"dateutc": start + 2 * 3_600_000 + 5 * 60_000, "dailyrainin": 0.0},   # midnight reset
           {"dateutc": start + 2 * 3_600_000 + 30 * 60_000, "dailyrainin": 0.04}]
    bars = sd.rain_by_hour(day, start, end, tz)
    assert bars[:3] == [0.2, 0.05, 0.04]
    counter = [{"dateutc": start + 5 * 60_000, "yearlyrainin": 20.0},
               {"dateutc": start + 35 * 60_000, "yearlyrainin": 20.12}]
    assert sd.rain_by_hour(counter, start, end, tz)[0] == 0.12


def test_pressure_trend_needs_three_hours():
    from app import station_display as sd
    t0 = 1_800_000_000_000
    times = [t0 + i * 600_000 for i in range(20)]
    press = [29.90 + i * 0.005 for i in range(20)]
    assert sd.pressure_trend(times, press) == pytest.approx(0.09, abs=1e-6)
    assert sd.pressure_trend(times[:10], press[:10]) is None   # under 3 h of history


def test_day_extremes_use_the_bucket_range():
    from app import station_display as sd
    rows = [{"dateutc": 1000, "tempf": 70.0, "tempf_max": 74.0, "tempf_min": 68.0},
            {"dateutc": 2000, "tempf": 65.0, "tempf_max": 66.0, "tempf_min": 61.5},
            {"dateutc": 500, "tempf": 99.0}]                          # before midnight
    hi, lo = sd.day_extremes(rows, 900)
    assert hi == [1000, 74.0] and lo == [2000, 61.5]


# ── review fixes (CodeRabbit, PR #129) ────────────────────────────────

def test_a_station_made_private_leaves_the_public_feed_at_once(public):
    _seed_full(public)
    now = int(time.time() * 1000)
    for k in range(0, 12, 2):
        _post(public, now - k * 600_000, mac=THERMO, name="Shed", outdoor={"tempf": 70.0})
    r = public.put("/api/public-dashboard", headers=H, json={"macs": "all"})
    assert r.status_code == 200, r.text
    assert len(public.get("/display/data").json()["stations"]) == 2
    r = public.put("/api/public-dashboard", headers=H, json={"macs": MAC})
    assert r.status_code == 200, r.text
    names = [s["name"] for s in public.get("/display/data").json()["stations"]]
    assert names == ["Backyard"], names                      # no 20 s window for "Shed"


def test_a_failed_forecast_is_not_retried_on_every_build(client, monkeypatch):
    from app import station_display as sd
    _seed_full(client)
    calls = []

    async def down(lat, lon):  # noqa: ANN001
        calls.append(1)
        return None
    monkeypatch.setattr(sd, "_fetch_forecast", down)
    assert client.get("/api/display", headers=H).json()["forecast"] == []
    sd._CACHE.clear()                                         # force a second build
    assert client.get("/api/display", headers=H).json()["forecast"] == []
    assert len(calls) == 1                                    # the failure was remembered


def test_a_remembered_forecast_failure_retries_after_the_short_delay(client, monkeypatch):
    import asyncio
    from app import station_display as sd
    calls = []

    async def down(lat, lon):  # noqa: ANN001
        calls.append(1)
        return None
    monkeypatch.setattr(sd, "_fetch_forecast", down)
    assert asyncio.run(sd.forecast((33.3, -111.9))) == []
    assert asyncio.run(sd.forecast((33.3, -111.9))) == []
    assert len(calls) == 1                                    # inside the retry delay
    at, val = sd._FORECAST[(33.3, -111.9)]
    sd._FORECAST[(33.3, -111.9)] = (at - sd.FORECAST_RETRY_S - 1, val)
    asyncio.run(sd.forecast((33.3, -111.9)))
    assert len(calls) == 2                                    # retried, not held for the full TTL


def test_a_counter_only_gauge_gets_its_day_and_hour(client):
    """An SDR or LilyGO station posts only its running counter. The display
    used to show no Rain today for it (seen on the own box's AWN SDR row,
    2026-11-05); the day and hour now come from the rain ledger, like /current."""
    now = int(time.time() * 1000)
    for k, total in ((20, 10.00), (10, 10.05), (0, 10.12)):
        _post(client, now - k * 60_000, mac=THERMO, name="SDR",
              outdoor={"tempf": 70.0}, rain={"yearly_in": total})
    cur = client.get(f"/api/devices/{THERMO}/current", headers=H).json()
    d = client.get("/api/display", headers=H).json()
    c = d["stations"][0]["current"]
    assert c["dailyrainin"] is not None
    assert c["dailyrainin"] == pytest.approx(cur["dailyrainin"])
    assert c["hourlyrainin"] == pytest.approx(cur["hourlyrainin"])
    _finite_or_null(d)


def test_the_feels_like_line_fits_its_tile():
    """Short enough for the tile at 1280x800, and named by the air
    temperature, not the sign of the difference: dry heat reads a heat
    index BELOW the air (99.3°F at 15% is 95.1), which is not wind chill."""
    js = (STATIC / "display.js").read_text()
    assert "Heat index or wind chill" not in js
    for line in ("Heat index", "Wind chill", "Same as the air"):
        assert line in js
    assert "c.tempf >= 80" in js and "c.tempf <= 50" in js
    assert "fd >= 1 ?" not in js


def test_a_stale_strip_during_an_outage_is_kept_and_retried_once_per_delay(monkeypatch):
    """An expired strip plus a failing upstream: the strip is served and
    the fetch is not retried on the very next build (CodeRabbit, PR #146)."""
    import asyncio as _a
    from app import station_display as sd
    key = (33.3, -111.94)
    strip = [{"date": "2026-11-07", "hi": 80.0, "lo": 55.0}]
    sd._FORECAST.clear()
    sd._FORECAST[key] = (time.time() - sd.FORECAST_TTL_S - 60, strip)
    calls = []

    async def failing(lat, lon):
        calls.append((lat, lon))
        return None

    monkeypatch.setattr(sd, "_fetch_forecast", failing)
    assert _a.run(sd.forecast(key)) == strip
    assert _a.run(sd.forecast(key)) == strip
    assert len(calls) == 1
    sd._FORECAST.clear()
