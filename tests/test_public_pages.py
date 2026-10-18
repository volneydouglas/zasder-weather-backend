"""The public site's deeper pages (2.6, Volney 10-10): history, records
and climate, storms and reports for people without the apps. Opt-in with
the public dashboard, public stations only, named by public id, and never
the MAC, coordinates or the owner's settings."""
import asyncio
import re
import time
from datetime import date, datetime, timezone

import pytest

H = {"Authorization": "Bearer test-api-token"}
IH = {"Authorization": "Bearer test-ingest-token"}
MAC = "AA:BB:CC:00:00:89"
LAT, LON = 33.45, -111.9
PAGES = ["/weather/history", "/weather/history?range=30d", "/weather/history?range=1y",
         "/weather/records", "/weather/storms", "/weather/storms/latest",
         "/weather/reports"]


def _post(c, at_ms, tempf=80.0, mac=MAC, name="Arts Campus"):
    stamp = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = c.post("/ingest/custom", headers=IH, json={
        "device": {"id": mac, "name": name, "coords": {"lat": LAT, "lon": LON}},
        "timestamp_utc": stamp, "outdoor": {"tempf": tempf, "humidity": 30},
        "wind": {"speed_mph": 4, "gust_mph": 9}, "rain": {"daily_in": 0.0},
        "pressure": {"relative_inhg": 29.9}})
    assert r.status_code == 200, r.text


def _seed(c):
    from app import db
    now = int(time.time() * 1000)
    for h in range(0, 72, 3):
        _post(c, now - h * 3_600_000, 70 + (h % 12))
    this_year = date.today().year

    async def run():
        async with db.connect() as conn:
            for day, tmin, tmax, rain, gust in [
                    (f"{this_year - 1}-07-04", 85.0, 105.0, 0.0, 35.0),
                    (f"{this_year}-01-10", 30.0, 50.0, 0.25, 22.0),
                    (date.today().isoformat(), 70.0, 90.0, 0.40, 20.0)]:
                await conn.execute(
                    "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                    "rain_total, windgustmph_max) VALUES (?, ?, ?, ?, ?, ?)",
                    (MAC, day, tmin, tmax, rain, gust))
            await conn.execute(
                "INSERT INTO storm_history (mac, started_ms, ended_ms, total_in, "
                "peak_rate_in_hr, max_gust_mph, min_tempf, max_tempf) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (MAC, now - 5 * 3_600_000, now - 3 * 3_600_000, 0.62, 1.8, 41.0, 72.0, 96.0))
            await conn.commit()
    asyncio.run(run())


@pytest.fixture(autouse=True)
def _no_ncei(client, monkeypatch):
    """The records page asks NOAA for today's normals: never from a test."""
    from app import normals
    calls = []

    async def fake_today(lat, lon):
        calls.append((lat, lon))
        return {"date": "10-10", "station": "TEMPE ASU, AZ US",
                "normal_high": 91.0, "normal_low": 66.0}
    monkeypatch.setattr(normals, "today", fake_today)
    return calls


@pytest.fixture
def public(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    monkeypatch.setattr(settings, "insights", True)
    _seed(client)
    return client


def _sid(c):
    from app import main as M, map_beacon as mb
    return mb.station_id(asyncio.run(M.ensure_server_id()), MAC)


def test_every_page_is_404_while_the_public_dashboard_is_off(client):
    _seed(client)
    for path in PAGES + ["/weather/reports/noaa/latest-month.txt",
                         f"/weather/data/{date.today().year}.csv"]:
        assert client.get(path).status_code == 404, path


def test_every_page_renders_and_never_leaks_the_station(public):
    for path in PAGES:
        r = public.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        body = r.text
        assert MAC not in body and MAC.replace(":", "") not in body, path
        assert str(LAT) not in body and str(LON) not in body, path
        assert "test-api-token" not in body and "test-ingest-token" not in body
        assert 'aria-current="page"' in body, path
        # The page's own chrome links every other page.
        for p in ("/weather/history", "/weather/records", "/weather/storms",
                  "/weather/reports"):
            assert f'href="{p}' in body, (path, p)


def test_history_draws_each_range(public):
    h7 = public.get("/weather/history").text
    assert "last 7 days" in h7 and "7 days ago" in h7 and "chart-svg" in h7
    assert "Daily high and low" in h7
    h1y = public.get("/weather/history?range=1y").text
    assert "1 year ago" in h1y and "Daily rain" in h1y and "Peak gust each day" in h1y
    assert public.get("/weather/history?range=2y").status_code == 422


def test_the_year_view_says_so_without_daily_summaries(public, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "insights", False)
    body = public.get("/weather/history?range=1y").text
    assert "not keeping daily summaries" in body


def test_records_and_climate(public):
    body = public.get("/weather/records").text
    assert "Today against normal" in body and "NOAA 1991-2020 climate normals" in body
    # The normals station's name is a location clue: never published.
    assert "TEMPE ASU" not in body
    assert "Highs and lows" in body and "Temperature high" in body
    y = date.today().year
    assert f"Climate, {y}" in body and "Heating degree days" in body
    last = public.get(f"/weather/records?year={y - 1}").text
    assert f"Climate, {y - 1}" in last and "105.0" in last
    assert public.get("/weather/records?year=1999").status_code == 404


def test_storms_and_the_fixed_latest_link(public):
    body = public.get("/weather/storms").text
    assert "0.62" in body and "41" in body and "Recent storms" in body
    assert "/weather/storms/latest" in body
    latest = public.get("/weather/storms/latest").text
    assert "0.62 in" in latest and "1.80 in/hr" in latest and "2 h 0 min" in latest


def test_reports_index_and_fixed_addresses(public):
    y = date.today().year
    body = public.get("/weather/reports").text
    for href in ("/weather/reports/noaa/latest-month.txt",
                 "/weather/reports/noaa/latest-year.txt",
                 f"/weather/data/{y}.csv", "/weather/storms/latest",
                 f"/weather/reports/noaa/{y}.txt", f"/weather/reports/noaa/{y - 1}-07.txt"):
        assert href in body, href
    # Only months with summaries are listed (last year has only July).
    assert f"/weather/reports/noaa/{y - 1}-06.txt" not in body
    assert f"/weather/reports/noaa/{y - 1}-08.txt" not in body
    assert f"Latest year ({y})" in body
    # No forecast-provider report is ever offered.
    assert "outlook" not in body.lower() and "morning" not in body.lower()


def test_noaa_text_reports(public):
    y = date.today().year
    month = public.get("/weather/reports/noaa/latest-month.txt")
    assert month.status_code == 200 and month.headers["content-type"].startswith("text/plain")
    assert "Arts Campus" in month.text
    year = public.get(f"/weather/reports/noaa/{y - 1}.txt")
    assert year.status_code == 200 and "105" in year.text
    assert public.get(f"/weather/reports/noaa/{y - 1}-07.txt").status_code == 200
    for bad in ("1999.txt", f"{y}-13.txt", "latest.txt", "../etc.txt", f"{y}.csv"):
        assert public.get(f"/weather/reports/noaa/{bad}").status_code == 404, bad


def test_the_daily_csv(public):
    y = date.today().year
    r = public.get(f"/weather/data/{y - 1}.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("date,high_f,low_f,mean_f,rain_in")
    assert lines[1].startswith(f"{y - 1}-07-04,105.0,85.0,95.0,")
    assert public.get(f"/weather/data/{y - 1}.txt").status_code == 404
    assert public.get("/weather/data/1999.csv").status_code == 404


def test_stations_are_named_by_public_id_only(public):
    sid = _sid(public)
    assert public.get(f"/weather/storms?station={sid}").status_code == 200
    assert public.get(f"/weather/storms?station={MAC}").status_code == 404
    assert public.get("/weather/storms?station=0123456789abcdef").status_code == 404


def test_a_station_the_front_page_hides_is_not_published(public):
    from app.config import settings
    other = "11:22:33:44:55:77"
    _post(public, int(time.time() * 1000), mac=other, name="Garage")
    from app import main as M, map_beacon as mb
    other_sid = mb.station_id(asyncio.run(M.ensure_server_id()), other)
    # Default selection: the primary station only.
    assert public.get(f"/weather/storms?station={other_sid}").status_code == 404
    settings.public_dashboard_macs = "all"
    try:
        M._PUBLIC_PAGE_CACHE.clear()
        body = public.get(f"/weather/storms?station={other_sid}").text
        assert "Garage" in body and "pp-chips" in body
    finally:
        settings.public_dashboard_macs = None


def test_the_front_page_and_embed_link_the_pages(public):
    from app import main as M
    M._PUBLIC_DASH_CACHE = None
    front = public.get("/").text
    assert 'class="pp-more"' in front and 'href="/weather/history"' in front
    assert 'class="pp-more-a" target="_blank"' not in front
    embed = public.get("/embed").text
    assert 'class="pp-more-a" target="_blank" rel="noopener" href="/weather/history"' in embed


def test_a_sharing_change_reaches_the_next_page(public):
    """The pages ride the publishing-policy generation like the front page."""
    from app import main as M, publish_policy as pp
    public.get("/weather/storms")
    key = next(k for k in M._PUBLIC_PAGE_CACHE if k.startswith("storms:"))
    built = M._PUBLIC_PAGE_CACHE[key]
    pp.bump()
    public.get("/weather/storms")
    assert M._PUBLIC_PAGE_CACHE[key][2] == pp.generation() != built[2]


def test_the_cache_is_bounded(public):
    from app import main as M

    async def fill():
        for i in range(M._PUBLIC_PAGE_MAX + 10):
            await M._public_page(f"k{i}", _const(i))
    asyncio.run(fill())
    assert len(M._PUBLIC_PAGE_CACHE) <= M._PUBLIC_PAGE_MAX


def _const(v):
    async def build():
        return v
    return build


# ── the addendum review (2026-10-10) ─────────────────────────────────────

def _asgi():
    import httpx
    from app import main as M
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=M.app),
                             base_url="http://review.test")


@pytest.mark.parametrize("path,patch,marker", [
    ("/weather/storms", "list_storms", "0.62"),
    ("/weather/reports/noaa/latest-month.txt", "rollup_coverage", "Arts Campus"),
    (f"/weather/data/{date.today().year}.csv", "rollup_coverage", "date,high_f"),
])
def test_a_request_in_flight_when_sharing_is_revoked_gets_no_data(public, monkeypatch,
                                                                  path, patch, marker):
    """A26-01: a page whose build spans the owner turning the public page
    off is not handed out; the request starts over and finds it off."""
    from app import db

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        real = getattr(db, patch)

        async def held(*a, **k):
            started.set()
            await release.wait()
            return await real(*a, **k)
        monkeypatch.setattr(db, patch, held)
        async with _asgi() as c:
            pending = asyncio.create_task(c.get(path))
            await asyncio.wait_for(started.wait(), 5)
            off = await c.put("/api/public-dashboard", headers=H, json={"enabled": False})
            assert off.status_code == 200 and off.json()["enabled"] is False
            release.set()
            r = await asyncio.wait_for(pending, 5)
            assert r.status_code == 404, (r.status_code, r.text[:200])
            assert marker not in r.text
    asyncio.run(run())


def test_a_station_dropped_from_the_public_set_mid_build_is_not_served(public, monkeypatch):
    """A26-01, the station-removal shape: the owner narrows the public set
    to another station while a page for this one is being built."""
    from app import db
    other = "11:22:33:44:55:78"
    _post(public, int(time.time() * 1000), mac=other, name="Garage")
    sid = _sid(public)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        real = db.list_storms

        async def held(*a, **k):
            started.set()
            await release.wait()
            return await real(*a, **k)
        monkeypatch.setattr(db, "list_storms", held)
        async with _asgi() as c:
            pending = asyncio.create_task(c.get(f"/weather/storms?station={sid}"))
            await asyncio.wait_for(started.wait(), 5)
            r = await c.put("/api/public-dashboard", headers=H, json={"macs": other})
            assert r.status_code == 200
            release.set()
            resp = await asyncio.wait_for(pending, 5)
            assert resp.status_code == 404 and "Arts Campus" not in resp.text
    asyncio.run(run())


def test_simultaneous_cold_visitors_share_one_build(public, monkeypatch):
    """A26-03: eight visitors arriving together cost one aggregation."""
    from app import db

    async def run():
        count = 0
        release = asyncio.Event()

        async def history(*a, **k):
            nonlocal count
            count += 1
            await release.wait()
            return []
        monkeypatch.setattr(db, "history", history)
        async with _asgi() as c:
            pending = [asyncio.create_task(c.get("/weather/history?range=30d"))
                       for _ in range(8)]
            await asyncio.sleep(0.2)
            release.set()
            rs = await asyncio.gather(*pending)
            assert all(r.status_code == 200 for r in rs)
            assert count == 1
            assert (await c.get("/weather/history?range=30d")).status_code == 200
            assert count == 1
    asyncio.run(run())


def test_a_failed_build_is_not_shared_forever(public, monkeypatch):
    """A26-03: a build that fails releases its slot; the next visitor
    builds again."""
    from app import db
    real = db.list_storms
    calls = []

    async def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database busy")
        return await real(*a, **k)
    monkeypatch.setattr(db, "list_storms", flaky)
    with pytest.raises(RuntimeError):
        public.get("/weather/storms")
    assert public.get("/weather/storms").status_code == 200


def test_different_pages_build_at_a_bounded_rate(public, monkeypatch):
    """A26-03: distinct addresses do not stack without limit."""
    from app import db, main as M
    sid = _sid(public)

    async def run():
        live = peak = 0
        release = asyncio.Event()

        async def history(*a, **k):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await release.wait()
            live -= 1
            return []
        monkeypatch.setattr(db, "history", history)
        async with _asgi() as c:
            urls = [f"/weather/history?range={r}&station={sid}" for r in ("7d", "30d")] + \
                   [f"/weather/history?range={r}" for r in ("7d", "30d")]
            pending = [asyncio.create_task(c.get(u)) for u in urls]
            await asyncio.sleep(0.3)
            assert peak <= M._PUBLIC_PAGE_BUILDS
            release.set()
            await asyncio.gather(*pending)
    asyncio.run(run())


def _xs(html):
    out = []
    for pts in re.findall(r'<polyline points="([^"]+)"', html):
        out.append([float(p.split(",")[0]) for p in pts.split()])
    return out


def test_history_points_sit_where_they_happened():
    """A26-02: a day of data in a seven-day window sits at the right end,
    not stretched across the labels."""
    from app import public_pages as pp
    day = 86_400_000
    now = 1_791_000_000_000
    pts = [(now - day + h * 3_600_000, 70.0 + h) for h in range(25)]
    html = pp.render_history("7d", {"tempf": pts},
                             days=None, insights=False, domain=(now - 7 * day, now))
    xs = _xs(html)[0]
    assert xs[0] == pytest.approx(640 * 6 / 7, abs=0.2) and xs[-1] == 640.0
    # Two readings a day apart are two dots, not a line through the gap.
    two = pp.render_history("7d", {"tempf": [(now - day, 70), (now, 90)]},
                            days=None, insights=False, domain=(now - 7 * day, now))
    assert not _xs(two) and two.count("<circle") >= 2


def test_a_station_that_stopped_reporting_does_not_reach_now():
    from app import public_pages as pp
    day = 86_400_000
    now = 1_791_000_000_000
    pts = [(now - 7 * day + i * 3_600_000, 70.0) for i in range(0, 5 * 24)]
    html = pp.render_history("7d", {"tempf": pts}, days=None, insights=False,
                             domain=(now - 7 * day, now))
    assert max(max(x) for x in _xs(html)) < 640 * 5.1 / 7


def test_an_outage_breaks_the_line():
    from app import public_pages as pp
    day = 86_400_000
    now = 1_791_000_000_000
    pts = ([(now - 6 * day + i * 900_000, 70.0) for i in range(8)]
           + [(now - day + i * 900_000, 80.0) for i in range(8)])
    html = pp.render_history("7d", {"tempf": pts}, days=None, insights=False,
                             domain=(now - 7 * day, now))
    assert len(_xs(html)) == 2, "two runs, not one line across a five-day hole"


def test_partial_months_are_marked_and_full_ones_are_not():
    """A26-06."""
    from app import public_pages as pp
    from datetime import date as _d
    month = {"month": 1, "days": 1, "mean": 65, "max": 80, "min": 50,
             "rain": 0.25, "hdd": 0, "cdd": 0}
    summary = {"months": [month], "totals": {"mean": 65, "rain": 0.25, "hdd": 0, "cdd": 0}}
    one = pp.render_climate(summary, 2025, [2025], None, _d(2026, 10, 10))
    month["days"] = 31
    full = pp.render_climate(summary, 2025, [2025], None, _d(2026, 10, 10))
    assert one != full
    assert "1 of 31 days" in one and "of 31 days" not in full
    month.update({"month": 10, "days": 10})
    cur = pp.render_climate(summary, 2026, [2026], None, _d(2026, 10, 10))
    assert "in progress" in cur and "days so far" in cur


def test_report_links_follow_real_coverage():
    from app import public_pages as pp
    html = pp.render_reports(None, {"2024-03": 12, "2024-11": 30}, False)
    assert "Latest month (Nov 2024)" in html and "Latest year (2024)" in html
    assert "/weather/reports/noaa/2024-03.txt" in html
    assert "/weather/reports/noaa/2024-04.txt" not in html
    assert "/weather/reports/noaa/2025" not in html


def test_latest_follows_the_newest_data_not_the_calendar(public):
    from app import db

    async def drop_this_year():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM daily_rollups WHERE day >= ?",
                               (f"{date.today().year}-01-01",))
            await conn.commit()
    asyncio.run(drop_this_year())
    from app import main as M
    M._PUBLIC_PAGE_CACHE.clear()
    r = public.get("/weather/reports/noaa/latest-year.txt")
    assert r.status_code == 200 and str(date.today().year - 1) in r.text
    assert public.get(f"/weather/reports/noaa/{date.today().year}.txt").status_code == 404


def test_counts_carry_their_reading_time(public):
    """The addendum's notes: detector counts are as old as the reading
    they came from, and a positive count with no distance report is a
    valid answer the app must not read as clear weather."""
    from tests.test_lightning_map import _post as lt_post, H as LH, MAC as LMAC
    now = int(time.time() * 1000)
    lt_post(public, now - 1_000, hour=15)
    body = public.get(f"/api/devices/{LMAC}/lightning/strikes", headers=LH).json()
    assert body["count_1h"] == 15 and body["strikes"] == []
    assert abs(body["counts_ms"] - (now - 1_000)) < 2_000


# ── fields a station has never reported (2.7, mirror #8 on every page) ──
#
# The dashboard hides a field the station has never sent
# (public_dashboard.shown_fields). The deeper pages drew every card anyway:
# a thermometer-only station's History had empty Humidity, Wind and
# Pressure charts, a rain bar of nothing and a gust line of nothing, which
# reads as broken. Same rule, same helper, through the real routes.

BARE = "AA:BB:CC:00:00:8A"


def _post_raw(c, at_ms, body, mac=BARE, name="Shed"):
    stamp = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = c.post("/ingest/custom", headers=IH, json={
        "device": {"id": mac, "name": name}, "timestamp_utc": stamp, **body})
    assert r.status_code == 200, r.text


def _rollup(day, tmin, tmax, rain=None, gust=None, mac=BARE):
    from app import db

    async def run():
        async with db.connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO daily_rollups (mac, day, tempf_min, tempf_max, "
                "rain_total, windgustmph_max) VALUES (?, ?, ?, ?, ?, ?)",
                (mac, day, tmin, tmax, rain, gust))
            await conn.commit()
    asyncio.run(run())


def _storm(total, gust, mac=BARE):
    from app import db
    now = int(time.time() * 1000)

    async def run():
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO storm_history (mac, started_ms, ended_ms, total_in, "
                "peak_rate_in_hr, max_gust_mph, min_tempf, max_tempf) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mac, now - 5 * 3_600_000, now - 3 * 3_600_000, total, 0.5, gust, 70.0, 80.0))
            await conn.commit()
    asyncio.run(run())


@pytest.fixture
def bare(client, monkeypatch):
    """The public pages on, insights on, and no station seeded yet."""
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    monkeypatch.setattr(settings, "insights", True)
    return client


def _cards(html):
    return re.findall(r'<div class="chart-title">([^<]+?) <span', html)


def _warm(c):
    """The open pages read the all-time records without waiting for them
    (CodeRabbit, PR #113), so a cold cache hides nothing. The records page
    computes them (it needs them to draw), which is what a test asserting
    a hidden field visits first."""
    assert c.get("/weather/records").status_code == 200


def _seed_thermometer(c, extra=None):
    now = int(time.time() * 1000)
    for h in range(0, 48, 3):
        _post_raw(c, now - h * 3_600_000, {"outdoor": {"tempf": 70.0 + h % 7}, **(extra or {})})
    _rollup(date.today().isoformat(), 68.0, 76.0,
            rain=(extra or {}).get("rain", {}).get("daily_in"))


def test_history_of_a_thermometer_only_station_has_only_temperature(bare):
    _seed_thermometer(bare)
    _warm(bare)
    for rng in ("7d", "30d", "1y"):
        body = bare.get(f"/weather/history?range={rng}").text
        cards = _cards(body)
        assert "Daily high and low" in cards, (rng, cards)
        for never in ("Humidity", "Wind", "Pressure", "Daily rain", "Peak gust each day"):
            assert never not in cards, (rng, never, cards)
    assert "Temperature" in _cards(bare.get("/weather/history").text)


def test_history_keeps_rain_judged_by_the_day_total(bare):
    """A station that sends only a day total (its hourly figure is derived
    from it): the dashboard keeps Rain by `dailyrainin`, and so does
    History's daily rain card."""
    _seed_thermometer(bare, {"rain": {"daily_in": 0.12}})
    _warm(bare)
    for rng in ("7d", "1y"):
        cards = _cards(bare.get(f"/weather/history?range={rng}").text)
        assert "Daily rain" in cards, (rng, cards)
        assert "Pressure" not in cards and "Peak gust each day" not in cards, (rng, cards)


def test_a_field_that_reported_once_is_not_never(bare):
    """Quiet now is not never: a barometer that reported weeks ago keeps
    its card on the 7-day page even with nothing in the window (the
    all-time records remember it, exactly the dashboard's rule)."""
    old = int(time.time() * 1000) - 40 * 86_400_000
    _post_raw(bare, old, {"outdoor": {"tempf": 60.0}, "pressure": {"relative_inhg": 29.9},
                          "wind": {"speed_mph": 3, "gust_mph": 8}})
    _seed_thermometer(bare)
    _warm(bare)
    cards = _cards(bare.get("/weather/history").text)
    assert "Pressure" in cards and "Wind" in cards, cards
    assert "Humidity" not in cards, cards


def test_records_climate_has_no_rain_column_without_a_gauge(bare):
    _seed_thermometer(bare)
    body = bare.get("/weather/records").text
    assert "Temperature high" in body and f"Climate, {date.today().year}" in body
    assert "Rain in</th>" not in body
    assert "Pressure high" not in body and "Humidity high" not in body


def test_records_climate_keeps_rain_judged_by_the_day_total(bare):
    _seed_thermometer(bare, {"rain": {"daily_in": 0.12}})
    assert "Rain in</th>" in bare.get("/weather/records").text


def test_storms_of_a_gauge_with_no_anemometer_show_no_gust(bare):
    _seed_thermometer(bare, {"rain": {"daily_in": 0.12}})
    _storm(0.62, None)
    _warm(bare)
    for path in ("/weather/storms", "/weather/storms/latest"):
        body = bare.get(path).text
        assert "0.62" in body, path
        assert "Peak gust" not in body, path


def test_the_pages_stay_off_while_the_public_dashboard_is_off(client):
    _seed_thermometer(client)
    for path in ("/weather/history", "/weather/history?range=1y", "/weather/records",
                 "/weather/storms", "/weather/storms/latest"):
        assert client.get(path).status_code == 404, path


# ── rain a station measures only through a counter (2.7 follow-up F27-02) ──
#
# The gate above judged rain by `dailyrainin` in the all-time records and
# by the raw hourly key. A gauge that posts only its lifetime counter
# (SDR, LilyGO: `yearlyrainin` and nothing else) has neither, yet its
# daily ledger holds real derived rain: History lost the Daily rain card,
# the climate table its Rain column and the dashboard its Rain chart.
# Rain is now judged by every rain input the station sends and by the
# derived daily ledger. Dry is rain (a 0.00 day is a working gauge);
# a station with no gauge still loses the card.

COUNTER = "AA:BB:CC:00:00:8B"


def _seed_counter(c, steps, key="yearly_in", mac=COUNTER):
    """Readings `hours_ago` → counter value, oldest first, each with a
    temperature so the station is an ordinary weather station."""
    now = int(time.time() * 1000)
    for hours_ago, value in steps:
        _post_raw(c, now - hours_ago * 3_600_000,
                  {"outdoor": {"tempf": 70.0}, "rain": {key: value}}, mac=mac)


def _derived_rain(mac=COUNTER):
    from app import climate, db
    from app.day_rain import day_rain_in

    async def run():
        async with db.connect() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM daily_rollups WHERE mac = ?", (mac,))).fetchall()
        return [day_rain_in(r) for r in rows], [climate._day_stats(r)["rain"] for r in rows]
    return asyncio.run(run())


def _rain_shown(c):
    # Records first: it computes the all-time records History's gate reads
    # (a cold cache would hide nothing and prove nothing).
    records = c.get("/weather/records")
    history = c.get("/weather/history?range=7d")
    assert history.status_code == records.status_code == 200
    return {"history": "Daily rain" in _cards(history.text),
            "records": "Rain in</th>" in records.text}


def test_a_counter_only_gauge_keeps_its_derived_rain(bare):
    _seed_counter(bare, [(30, 10.0), (18, 10.25), (6, 10.25), (1, 10.25)])
    ledger, _ = _derived_rain()
    assert ledger and abs(sum(v for v in ledger if v is not None) - 0.25) < 1e-9, ledger
    assert _rain_shown(bare) == {"history": True, "records": True}
    # The dashboard shares the rule (records warm from the page above).
    assert 'Rain <span class="chart-unit">· last 24h' in bare.get("/").text


def test_a_dry_counter_is_a_gauge_not_an_absence(bare):
    """Zero is a real answer: a counter that never moved is a working
    gauge on dry days, never "never measured"."""
    _seed_counter(bare, [(30, 4.0), (18, 4.0), (6, 4.0), (1, 4.0)])
    ledger, _ = _derived_rain()
    assert ledger and all(v == 0.0 for v in ledger), ledger
    assert _rain_shown(bare) == {"history": True, "records": True}


def test_the_ledger_keeps_rain_after_the_raw_readings_are_gone(bare):
    """The daily ledger outlives the raw rows: with every observation
    gone, the derived days still prove a gauge."""
    from app import db
    _seed_counter(bare, [(30, 10.0), (18, 10.25), (6, 10.25), (1, 10.25)])
    # A thermometer elsewhere keeps the records non-empty, so the gate is
    # live (no records at all hides nothing).
    _seed_thermometer(bare)

    async def age_out():
        async with db.connect() as conn:
            await conn.execute("DELETE FROM observations WHERE mac = ?", (COUNTER,))
            await conn.commit()
    asyncio.run(age_out())
    from app import main
    asyncio.run(main._warm_records(COUNTER))     # a live gate, not a cold cache
    assert main._records_cached_or_warm(COUNTER) is not None
    assert asyncio.run(main._public_shown(COUNTER, ["hourlyrainin"])) == {"hourlyrainin"}


_LEDGER_ARMS = {
    "rain_total": {"rain_total": 0.0},
    "yearly_rise": {"yearly_rise": 0.0},
    "yearly_first_and_last": {"yearly_first": 4.0, "yearly_last": 4.0},
    "yearly_min_and_max": {"yearly_min": 4.0, "yearly_max": 4.0},
}


def _ledger_row(cols):
    """One daily_rollups row with a temperature and only `cols` of the
    rain inputs set; db.has_rain_ledger's answer for its station."""
    from app import db
    mac = "AA:BB:CC:00:00:8A"
    names = "".join(f", {c}" for c in cols)
    marks = "".join(", ?" for _ in cols)

    async def run():
        async with db.connect() as conn:
            await conn.execute(
                f"INSERT INTO daily_rollups (mac, day, tempf_max{names}) "
                f"VALUES (?, ?, ?{marks})",
                (mac, "2026-07-04", 90.0, *cols.values()))
            await conn.commit()
        return await db.has_rain_ledger(mac)
    return asyncio.run(run())


@pytest.mark.parametrize("arm", sorted(_LEDGER_ARMS))
def test_each_ledger_arm_alone_is_a_rain_ledger(bare, arm):
    """Every input day_rain_in reads proves a gauge on its own, each with
    a dry 0.0 or an unmoved counter: one row with only that arm set."""
    assert _ledger_row(_LEDGER_ARMS[arm]) is True


def test_a_rollup_row_with_no_rain_input_is_no_ledger(bare):
    """A thermometer's day: every rain column NULL."""
    assert _ledger_row({}) is False


def test_half_of_each_counter_pair_is_no_ledger(bare):
    assert _ledger_row({"yearly_first": 4.0, "yearly_min": 4.0}) is False


def test_a_monthly_counter_only_gauge_keeps_rain(bare):
    _seed_counter(bare, [(30, 1.0), (18, 1.25), (6, 1.25), (1, 1.25)], key="monthly_in")
    assert _rain_shown(bare) == {"history": True, "records": True}


def test_rain_gate_evidence_is_the_whole_rain_family_not_a_zero_shortcut():
    """The pure rule: any rain input in the current reading, the derived
    ledger, or a wettest-day record keeps Rain; a thermometer keeps
    nothing; a 0.0 counts."""
    from app import public_dashboard as pd
    recs = {"periods": {"all": {"fields": {"tempf": {"max": 90}}}}}
    fields = ["tempf", "hourlyrainin"]
    assert pd.shown_fields(fields, {"tempf": 70}, {}, recs) == ["tempf"]
    for key in ("yearlyrainin", "monthlyrainin", "dailyrainin", "eventrainin",
                "weeklyrainin", "totalrainin"):
        assert pd.shown_fields(fields, {"tempf": 70, key: 0.0}, {}, recs) == fields, key
    assert pd.shown_fields(fields, {"tempf": 70}, {}, recs,
                           measured={"hourlyrainin"}) == fields


def test_today_against_normal_differences_the_numbers_it_shows():
    """2.7 RC smoke test: "High 72° vs 88° -17°". 71.6 and 88.4 print as
    72 and 88, so the difference beside them is -16, not round(-16.8)."""
    from app import public_pages as pp
    html = pp.render_normals({"normal_high": 88.4, "normal_low": 60.0}, None,
                             {"fields": {"tempf": {"max": 71.6, "min": 59.6}}})
    assert 'High 72° vs 88° <span class="pp-diff">-16°</span>' in html, html
    assert 'Low 60° vs 60° <span class="pp-diff">+0°</span>' in html, html


# ── Storms is offered only to a station with rain (2.7 RC smoke test) ──
#
# A thermometer-only station was offered the Storms tab and the front
# page's Storms link, whose page then explained when rain starts a storm.
# Same never-reported rule as the Rain cards, counter-derived rain
# included.

def _offers_storms(c):
    link = re.compile(r'href="/weather/storms(\?[^"]*)?">Storms</a>')
    return {path: bool(link.search(c.get(path).text))
            for path in ("/weather/history", "/weather/records", "/weather/reports", "/")}


def test_a_thermometer_is_not_offered_storms(bare):
    _seed_thermometer(bare)
    bare.get("/weather/records")                 # warms the records cache
    assert not any(_offers_storms(bare).values()), _offers_storms(bare)
    # A bookmarked link still answers, and says why there is nothing.
    for path in ("/weather/storms", "/weather/storms/latest"):
        r = bare.get(path)
        assert r.status_code == 200 and "does not report rain" in r.text, path
        assert "rain starts" not in r.text, path


def test_a_counter_only_gauge_is_offered_storms(bare):
    _seed_counter(bare, [(30, 10.0), (18, 10.25), (6, 10.25), (1, 10.25)])
    bare.get("/weather/records")
    assert all(_offers_storms(bare).values()), _offers_storms(bare)
    assert "rain starts" in bare.get("/weather/storms").text


def test_a_day_total_gauge_is_offered_storms(bare):
    _seed_thermometer(bare, {"rain": {"daily_in": 0.12}})
    bare.get("/weather/records")
    assert all(_offers_storms(bare).values()), _offers_storms(bare)


# ── the open pages never wait on a cold records compute (CodeRabbit, PR #113) ──
#
# _public_shown awaited the all-time records on a cache miss. Every open
# page reaches it (History, Storms, Reports through the Storms nav), and a
# cold compute on a big archive with the rollups unable to answer takes
# minutes, with the visitor waiting and a _PUBLIC_PAGE_SEM slot held. It
# now reads them the dashboard's way: cached, else hide nothing and warm in
# the background.

def test_a_cold_records_cache_does_not_hold_up_the_page(bare, monkeypatch):
    from app import db, main
    _seed_thermometer(bare)
    calls = []

    async def run():
        release = asyncio.Event()

        async def slow_records(mac, tz):
            calls.append(mac)
            await release.wait()             # a compute that has not finished
            return {"periods": {"all": {"fields": {"tempf": {"max": 76.0}}}}}
        monkeypatch.setattr(db, "records", slow_records)
        fields = ["tempf", "humidity", "baromrelin", "hourlyrainin"]
        # The page answers at once with every field (unknown is not never)...
        shown = await asyncio.wait_for(main._public_shown(BARE, fields), 2.0)
        assert shown == set(fields)
        # ...and the compute it needs is under way in the background.
        await asyncio.sleep(0)
        assert calls == [BARE]
        assert main._records_cached_or_warm(BARE) is None   # one warm, not two
        await asyncio.sleep(0)
        assert calls == [BARE]
        release.set()
        await asyncio.gather(*list(main._WARM_TASKS))
        # Warm now: the next build filters.
        assert await main._public_shown(BARE, fields) == {"tempf"}
    asyncio.run(run())


def test_a_cold_records_cache_answers_the_history_page(bare, monkeypatch):
    """Through the real route: a records compute that never finishes in
    time does not keep the visitor waiting."""
    from app import db
    _seed_thermometer(bare)

    async def stuck(mac, tz):
        await asyncio.sleep(30)
        return {}
    monkeypatch.setattr(db, "records", stuck)
    t0 = time.monotonic()
    r = bare.get("/weather/history")
    assert r.status_code == 200
    assert time.monotonic() - t0 < 10, "the page waited on the records compute"
