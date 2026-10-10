"""A change to what may be published reaches the very next public page
(2.6 follow-up review F26-01). The page carries location-bearing output
(the lightning rings' tiles, the station-map link) and is cached, served
stale for up to a day, and rebuilt in the background; none of those may
hand out a page built under an older sharing setting."""
import asyncio
import time

import pytest

from tests.test_lightning_map import H, LAT, LON, MAC, _storm

TILE = 'src="https://tile.openstreetmap.org/'


@pytest.fixture
def public(client, monkeypatch):
    from app import map_beacon as mb
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)

    # Persist the real config but never contact the directory.
    async def off_without_network(cfg, prev_mac, now_ms):
        await mb.set_config(cfg)
        return {"ok": True}
    monkeypatch.setattr(mb, "turn_off", off_without_network)
    _storm(client, int(time.time() * 1000))
    return client


def _share(c, **kw):
    r = c.put("/api/map", headers=H, json=kw)
    assert r.status_code == 200, r.text


def test_turning_sharing_off_reaches_the_next_visitor(public):
    _share(public, enabled=True, location_precision="exact")
    warm = public.get("/embed").text
    assert TILE in warm and "On the station map" in warm
    _share(public, enabled=False)
    after = public.get("/embed").text
    assert TILE not in after and "On the station map" not in after
    assert "lt-plain" in after


def _tiles(html):
    """The basemap layer as placed: which tiles AND where. Nearby points
    can share tiles at a zoom; their offsets are what locate them."""
    import re
    m = re.search(r'<div class="lt-tiles">(.*?)</div>', html)
    return m.group(1) if m else None


def test_coarsening_precision_moves_the_tiles_at_once(public):
    from app import lightning_map as lm, map_beacon as mb
    _share(public, enabled=True, location_precision="exact")
    exact = _tiles(public.get("/embed").text)
    _share(public, location_precision="city")
    city = _tiles(public.get("/embed").text)
    strikes = public.get(f"/api/devices/{MAC}/lightning/strikes", headers=H).json()["strikes"]
    now = int(time.time() * 1000)
    want_city = _tiles(lm.render_card(strikes, now, mb.place(LAT, LON, "city")))
    want_exact = _tiles(lm.render_card(strikes, now, (LAT, LON)))
    assert exact == want_exact
    assert city == want_city and city != exact


def test_a_stale_page_from_an_older_setting_is_never_served(public, monkeypatch):
    from app import main as M
    _share(public, enabled=True, location_precision="exact")
    public.get("/embed")
    built_at, html, gen = M._PUBLIC_DASH_CACHE
    # Age it into the serve-stale band, then change the setting.
    monkeypatch.setattr(M, "_PUBLIC_DASH_CACHE",
                        (built_at - M._PUBLIC_DASH_TTL_S - 5, html, gen))
    _share(public, enabled=False)
    assert TILE not in public.get("/embed").text


def test_a_build_in_flight_across_a_change_is_not_stored(public, monkeypatch):
    """The build starts under exact sharing, the owner turns sharing off
    while it runs: its page must not be cached or served."""
    from app import main as M, publish_policy as pp
    _share(public, enabled=True, location_precision="exact")
    M._PUBLIC_DASH_CACHE = None
    real = M._build_public_dashboard
    calls = []

    async def slow_build(devices, now_ms):
        html = await real(devices, now_ms)
        calls.append(TILE in html)
        if len(calls) == 1:
            pp.bump()          # the change lands mid-build
        return html
    monkeypatch.setattr(M, "_build_public_dashboard", slow_build)
    out = asyncio.run(M._cached_public_dashboard_raw([], int(time.time() * 1000)))
    assert len(calls) == 2, "the page built under the old setting was rebuilt"
    assert M._PUBLIC_DASH_CACHE[2] == pp.generation()
    assert out is not None


def test_a_background_refresh_across_a_change_is_dropped(public, monkeypatch):
    from app import main as M, publish_policy as pp
    _share(public, enabled=True, location_precision="exact")
    M._PUBLIC_DASH_CACHE = None

    async def build(devices, now_ms):
        pp.bump()
        return "<old policy page>"
    monkeypatch.setattr(M, "_build_public_dashboard", build)

    async def go():
        M._refresh_public_dashboard_soon([], 0)
        await M._PUBLIC_DASH_REFRESH_TASK
    asyncio.run(go())
    assert M._PUBLIC_DASH_CACHE is None


def test_switching_the_mapped_station_drops_the_old_stations_link(public):
    from tests.test_lightning_map import _post
    other = "11:22:33:44:55:66"
    _post(public, int(time.time() * 1000) - 60_000, hour=0, mac=other)
    from app.config import settings
    settings.public_dashboard_macs = "all"
    try:
        _share(public, enabled=True, location_precision="exact", mac=MAC)
        a = public.get("/embed").text
        assert a.count("On the station map") == 1
        _share(public, mac=other)
        b = public.get("/embed").text
        assert b.count("On the station map") == 1 and a != b
    finally:
        settings.public_dashboard_macs = None


def test_a_restore_bumps_the_policy(client):
    from app import publish_policy as pp
    g = pp.generation()
    pp.bump()
    assert pp.generation() == g + 1
