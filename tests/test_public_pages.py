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
