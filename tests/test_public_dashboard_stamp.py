"""The public page's "updated Xm ago" is recomputed at serve time (2.1).
The section is cached and served stale for up to a day; a page built when
the newest reading was a minute old used to say "updated 1m ago" for as
long as that cache lived (seen on Volney's box 2026-09-05: a dawn reading
served at 09:26 as "updated 1m ago")."""
import re
import time

import pytest

IH = {"Authorization": "Bearer test-ingest-token"}


def _post(client, ts_iso: str, dev: str = "AABBCCDDEE77"):
    return client.post("/ingest/custom", headers=IH, json={
        "device": {"id": dev, "name": "Stamp " + dev[-2:]},
        "timestamp_utc": ts_iso,
        "outdoor": {"tempf": 80.0, "humidity": 30},
        "wind": {}, "rain": {}, "pressure": {"relative_inhg": 29.9}})


@pytest.fixture
def public(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    # The "RIGHT NOW · updated" strip renders for two or more stations.
    monkeypatch.setattr(settings, "public_dashboard_macs", "all")
    return client


def test_a_stale_cache_restamps_its_age_on_every_serve(public, monkeypatch):
    from app import main as M, public_dashboard as pd
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    assert _post(public, stamp).status_code == 200
    assert _post(public, stamp, dev="AABBCCDDEE78").status_code == 200
    M._PUBLIC_DASH_CACHE = None
    first = public.get("/embed").text
    m = re.search(r'data-as-of-ms="(\d+)">updated (\w+) ago', first)
    assert m, "the marker is in the page"
    # Age the READING two hours, keep the cache 'fresh': the phrase must
    # follow the reading's age, not the build's.
    aged = first.replace(m.group(0), f'data-as-of-ms="{int((now - 7200) * 1000)}">updated 0s ago')
    built_at, html = M._PUBLIC_DASH_CACHE
    monkeypatch.setattr(M, "_PUBLIC_DASH_CACHE", (built_at, aged))
    served = public.get("/embed").text
    assert "updated 2h ago" in served, served[:400]
    assert "updated 0s ago" not in served


def test_restamp_leaves_other_markup_alone():
    from app import public_dashboard as pd
    html = '<b>x</b>' + pd.updated_marker(time.time() * 1000 - 90_000) + '<i>y</i>'
    out = pd.restamp_ages(html)
    assert out.startswith('<b>x</b>') and out.endswith('<i>y</i>')
    assert 'updated 1m ago' in out
