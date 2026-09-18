"""The map directory: signatures, pins, replay, expiry, withdrawal, listing."""
import base64
import json
import os
import time

import pytest
from app import main as m
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "map.db"))
    import importlib
    from app import main as m
    importlib.reload(m)
    m._last_seen_ms.clear()
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        yield c


def _key():
    return ed25519.Ed25519PrivateKey.generate()


def _pub(key):
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _canon(b):
    return json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _env(key, beacon):
    return {"beacon": beacon, "sig": base64.b64encode(key.sign(_canon(beacon))).decode(),
            "pubkey": _pub(key)}


def _beacon(server="srv-a", station="st-1", now=None, **over):
    now = now or int(time.time() * 1000)
    b = {"v": 1, "server_id": server, "station_id": station, "lat": 33.31, "lon": -111.84,
         "fuzzed": True, "tz": "America/Phoenix", "observed_ms": now - 60_000,
         "sent_ms": now, "expires_ms": now + 3 * 3_600_000,
         "conditions": {"tempf": 99.4, "humidity": 21}, "software": "test"}
    b.update(over)
    return b


def test_a_signed_beacon_is_stored_listed_and_withdrawn(client):
    key = _key()
    b = _beacon(name="Backyard", visit_url="https://example.com")
    r = client.post("/v1/beacons", json=_env(key, b))
    assert r.status_code == 200, r.text
    assert r.json()["station_id"] == "st-1"
    geo = client.get("/v1/beacons").json()
    assert geo["count"] == 1
    f = geo["features"][0]
    assert f["geometry"]["coordinates"] == [-111.84, 33.31]
    assert f["properties"]["name"] == "Backyard" and f["properties"]["conditions"]["tempf"] == 99.4
    assert "server_id" not in f["properties"]
    assert client.get("/v1/beacons").headers["access-control-allow-origin"] == "*"
    # bbox filtering
    assert client.get("/v1/beacons?bbox=-112,33,-111,34").json()["count"] == 1
    assert client.get("/v1/beacons?bbox=0,0,1,1").json()["count"] == 0
    assert client.get("/v1/beacons?bbox=x").status_code == 400
    # a signed withdrawal removes it (sent AFTER the beacon: the server's
    # sent_ms high-water mark refuses anything at or below the last one)
    tomb = {"v": 1, "server_id": "srv-a", "station_id": "st-1", "withdraw": True,
            "sent_ms": b["sent_ms"] + 1}
    r = client.post("/v1/withdraw", json=_env(key, tomb))
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert client.get("/v1/beacons").json()["count"] == 0
    assert client.get("/healthz").json()["ok"] is True


def test_the_first_key_is_pinned_and_another_is_refused(client):
    k1, k2 = _key(), _key()
    assert client.post("/v1/beacons", json=_env(k1, _beacon())).status_code == 200
    from app import main as m
    m._last_seen_ms.clear()
    r = client.post("/v1/beacons", json=_env(k2, _beacon(now=int(time.time() * 1000) + 5)))
    assert r.status_code == 403 and "key" in r.json()["detail"]
    # A station id cannot be taken over by another server either.
    m._last_seen_ms.clear()
    r = client.post("/v1/beacons", json=_env(k2, _beacon(server="srv-b", now=int(time.time() * 1000) + 9)))
    assert r.status_code == 403 and "another server" in r.json()["detail"]


def test_bad_signatures_replays_and_clocks_are_refused(client):
    key = _key()
    b = _beacon()
    env = _env(key, b)
    env["beacon"] = dict(b, lat=0.0)
    assert client.post("/v1/beacons", json=env).status_code == 400
    now = int(time.time() * 1000)
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now))).status_code == 200
    from app import main as m
    m._last_seen_ms.clear()
    # An older beacon than the one on file
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now - 1000))).status_code == 409
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 20 * 60_000))).status_code == 400
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 2, expires_ms=now + 9 * 3_600_000))).status_code == 400
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 3, visit_url="http://x"))).status_code == 400
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 4, v=2))).status_code == 400
    # rate: one a minute per server
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 5))).status_code == 200
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 6))).status_code == 429


def test_an_expired_beacon_is_not_served(client):
    key = _key()
    now = int(time.time() * 1000)
    b = _beacon(now=now, expires_ms=now + 500)
    assert client.post("/v1/beacons", json=_env(key, b)).status_code == 200
    time.sleep(0.6)
    assert client.get("/v1/beacons").json()["count"] == 0


def test_the_map_page_serves_under_a_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and "station map" in r.text
    assert "<script>" not in r.text and "style=" not in r.text, "nothing inline under the CSP"
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' https://unpkg.com" in csp and "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    for h in ("x-content-type-options", "x-frame-options", "referrer-policy",
              "strict-transport-security", "permissions-policy"):
        assert h in r.headers, h
    js = client.get("/static/map.js")
    assert js.status_code == 200 and "/v1/beacons" in js.text and "style=" not in js.text
    assert client.get("/static/map.css").status_code == 200
    # The API answers carry the headers too (one middleware, every response).
    assert "content-security-policy" in client.get("/healthz").headers


def _rotation(old_key, server, new_key):
    from app import main as m
    new_pub = _pub(new_key)
    sig = base64.b64encode(old_key.sign(m.rotation_message(server, new_pub))).decode()
    return {"prev_pubkey": _pub(old_key), "sig": sig}


def test_a_key_rotates_only_with_the_old_keys_blessing(client):
    old, new, stranger = _key(), _key(), _key()
    now = int(time.time() * 1000)
    assert client.post("/v1/beacons", json=_env(old, _beacon(now=now))).status_code == 200
    from app import main as m
    m._last_seen_ms.clear()
    # A new key with no proof: refused, as before.
    r = client.post("/v1/beacons", json=_env(new, _beacon(now=now + 1000)))
    assert r.status_code == 403
    # A proof signed by a stranger: refused.
    env = _env(new, _beacon(now=now + 2000)); env["rotation"] = _rotation(stranger, "srv-a", new)
    assert client.post("/v1/beacons", json=env).status_code == 403
    # A proof for a different server id: refused (the message binds the id).
    env = _env(new, _beacon(now=now + 3000)); env["rotation"] = _rotation(old, "srv-b", new)
    assert client.post("/v1/beacons", json=env).status_code == 403
    # The old key's blessing: accepted, answered rotated=true, pin moved.
    env = _env(new, _beacon(now=now + 4000)); env["rotation"] = _rotation(old, "srv-a", new)
    r = client.post("/v1/beacons", json=env)
    assert r.status_code == 200 and r.json()["rotated"] is True
    m._last_seen_ms.clear()
    # The new key alone works now; the old key is refused.
    r = client.post("/v1/beacons", json=_env(new, _beacon(now=now + 5000)))
    assert r.status_code == 200 and r.json()["rotated"] is False
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(old, _beacon(now=now + 6000))).status_code == 403
    # A withdrawal may carry the proof too.
    m._last_seen_ms.clear()
    tomb = {"v": 1, "server_id": "srv-a", "station_id": "st-1", "withdraw": True,
            "sent_ms": now + 7000}
    assert client.post("/v1/withdraw", json=_env(new, tomb)).status_code == 200


def test_a_server_may_list_only_so_many_stations(client):
    from app import main as m
    key = _key()
    now = int(time.time() * 1000)
    for i in range(m.MAX_STATIONS_PER_SERVER):
        m._last_seen_ms.clear()
        r = client.post("/v1/beacons", json=_env(key, _beacon(station=f"st-{i}", now=now + i)))
        assert r.status_code == 200, r.text
    m._last_seen_ms.clear()
    r = client.post("/v1/beacons", json=_env(key, _beacon(station="st-extra", now=now + 99)))
    assert r.status_code == 429
    # Re-sending an existing station is fine (a refresh is not a new listing).
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(station="st-0", now=now + 100))).status_code == 200


def test_precision_rides_along_and_a_bad_one_is_refused(client):
    key = _key()
    assert client.post("/v1/beacons", json=_env(key, _beacon(precision="city"))).status_code == 200
    assert client.get("/v1/beacons").json()["features"][0]["properties"]["precision"] == "city"
    from app import main as m
    m._last_seen_ms.clear()
    r = client.post("/v1/beacons", json=_env(key, _beacon(precision="street", now=int(time.time() * 1000) + 5)))
    assert r.status_code == 400


def test_stats_and_a_single_station_lookup(client):
    key = _key()
    b = _beacon(name="Backyard")
    assert client.post("/v1/beacons", json=_env(key, b)).status_code == 200
    st = client.get("/v1/stats").json()
    assert st["live"] == 1 and st["servers"] == 1
    f = client.get("/v1/stations/st-1")
    assert f.status_code == 200 and f.json()["properties"]["name"] == "Backyard"
    assert f.headers["access-control-allow-origin"] == "*"
    assert client.get("/v1/stations/nope").status_code == 404


def test_admin_routes_do_not_exist_without_a_token(client):
    assert client.get("/v1/admin/servers").status_code == 404
    assert client.get("/admin").status_code == 404


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "map.db"))
    monkeypatch.setenv("MAP_ADMIN_TOKEN", "operator-secret-for-tests")
    import importlib
    from app import main as m
    importlib.reload(m)
    m._last_seen_ms.clear()
    m._admin_failures.clear()
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        yield c
    monkeypatch.delenv("MAP_ADMIN_TOKEN")
    importlib.reload(m)


def test_the_operator_can_block_unblock_and_forget(admin_client):
    c = admin_client
    from app import main as m
    H = {"Authorization": "Bearer operator-secret-for-tests"}
    key = _key()
    now = int(time.time() * 1000)
    assert c.post("/v1/beacons", json=_env(key, _beacon(name="Backyard", now=now))).status_code == 200
    # Wrong token: 401 and counted; no token: 401.
    assert c.get("/v1/admin/servers", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/admin/servers").status_code == 401
    rows = c.get("/v1/admin/servers", headers=H).json()["servers"]
    assert rows[0]["server_id"] == "srv-a" and rows[0]["live"] == 1 and "Backyard" in rows[0]["names"]
    assert rows[0]["blocked"] is False
    # Block: off the map now, refused from now on.
    r = c.post("/v1/admin/servers/srv-a/block", headers=H)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert c.get("/v1/beacons").json()["count"] == 0
    m._last_seen_ms.clear()
    assert c.post("/v1/beacons", json=_env(key, _beacon(now=now + 1000))).status_code == 403
    assert c.get("/v1/admin/servers", headers=H).json()["servers"][0]["blocked"] is True
    # Unblock: the same key is recognised again.
    assert c.post("/v1/admin/servers/srv-a/unblock", headers=H).status_code == 200
    m._last_seen_ms.clear()
    assert c.post("/v1/beacons", json=_env(key, _beacon(now=now + 2000))).status_code == 200
    # Forget: pin and beacons gone; a brand-new key may claim the id.
    assert c.delete("/v1/admin/servers/srv-a", headers=H).status_code == 200
    assert c.get("/v1/beacons").json()["count"] == 0
    m._last_seen_ms.clear()
    assert c.post("/v1/beacons", json=_env(_key(), _beacon(now=now + 3000))).status_code == 200
    assert c.post("/v1/admin/servers/unknown/block", headers=H).status_code == 404
    # The operator page exists only with a token, and carries the CSP.
    r = c.get("/admin")
    assert r.status_code == 200 and "operator" in r.text and "<script>" not in r.text
    assert c.get("/static/admin.js").status_code == 200


def test_bad_admin_tokens_are_rate_limited(admin_client):
    c = admin_client
    from app import main as m
    for _ in range(m.ADMIN_FAIL_LIMIT):
        assert c.get("/v1/admin/servers", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/admin/servers", headers={"Authorization": "Bearer nope"}).status_code == 429
    # Even the right token waits out the minute: brute force gains nothing.
    assert c.get("/v1/admin/servers",
                 headers={"Authorization": "Bearer operator-secret-for-tests"}).status_code == 429


def test_id_mode_keeps_the_servers_address_out_of_the_map(client):
    """The point of the mode. Before 2.3 every pin carried the server's own
    address, so one GET /v1/beacons handed a scraper the hostname of every
    box listed — Volney's own read "https://zasder-weather.fly.dev/"
    (2026-09-14). In `id` mode the address reaches the directory and stops
    there; the map gets a /s/<id> redirect instead."""
    key = _key()
    secret = "https://zasder-weather.fly.dev/"
    r = client.post("/v1/beacons", json=_env(key, _beacon(
        name="Backyard", visit_url=secret, link_mode="id", region="AZ")))
    assert r.status_code == 200, r.text
    pid = r.json()["public_id"]
    assert pid.startswith("AZ") and len(pid) == 8
    assert set(pid[2:]) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert r.json()["visit"] == f"https://maps.zasder.com/s/{pid}"

    # Nothing anywhere in the public listing names the real host.
    raw = client.get("/v1/beacons").text
    assert "zasder-weather.fly.dev" not in raw
    assert "fly.dev" not in raw
    props = client.get("/v1/beacons").json()["features"][0]["properties"]
    assert props["visit_url"] == f"https://maps.zasder.com/s/{pid}"
    assert props["public_id"] == pid and props["link_mode"] == "id"
    # The single-station lookup is the same surface and must not leak either.
    one = client.get(f"/v1/stations/{props['station_id']}").text
    assert "fly.dev" not in one

    # The /s/ page is the ONLY place the address comes back, and it is a
    # page that names the host, not a redirect.
    hop = client.get(f"/s/{pid}", follow_redirects=False)
    assert hop.status_code == 200 and "location" not in hop.headers
    assert "zasder-weather.fly.dev" in hop.text and secret in hop.text
    assert hop.headers["cache-control"] == "no-store"
    assert client.get(f"/s/{pid.lower()}", follow_redirects=False).status_code == 200
    assert client.get("/s/AZNOSUCH", follow_redirects=False).status_code == 404


def test_the_id_is_assigned_once_and_survives_a_change_of_address(client):
    key = _key()
    first = client.post("/v1/beacons", json=_env(key, _beacon(
        visit_url="https://old.example.com/", link_mode="id", region="AZ"))).json()
    pid = first["public_id"]
    m._last_seen_ms.clear()                      # past the one-a-minute limit
    now = int(time.time() * 1000) + 1000
    second = client.post("/v1/beacons", json=_env(key, _beacon(
        now=now, visit_url="https://weather.zasder.com/",
        link_mode="id", region="TX"))).json()
    # Same id even though the region changed: it is the server's public
    # name and links already handed out must keep working.
    assert second["public_id"] == pid
    # ...but it now resolves to the new address.
    hop = client.get(f"/s/{pid}", follow_redirects=False)
    assert hop.status_code == 200 and "weather.zasder.com" in hop.text
    assert "old.example.com" not in hop.text


def test_direct_mode_still_publishes_the_address_and_none_publishes_nothing(client):
    key = _key()
    r = client.post("/v1/beacons", json=_env(key, _beacon(
        visit_url="https://example.com/", link_mode="direct"))).json()
    props = client.get("/v1/beacons").json()["features"][0]["properties"]
    assert props["visit_url"] == "https://example.com/" and props["link_mode"] == "direct"
    assert "public_id" not in props
    # No id at all: a server asking for `direct` has no use for one, and
    # minting from a beacon that carries no region would fix a region-less
    # id on a server that has simply not upgraded yet.
    assert r["public_id"] is None

    m._last_seen_ms.clear()
    now = int(time.time() * 1000) + 1000
    client.post("/v1/beacons", json=_env(key, _beacon(
        now=now, visit_url="https://example.com/", link_mode="none")))
    props = client.get("/v1/beacons").json()["features"][0]["properties"]
    assert props["link_mode"] == "none" and "visit_url" not in props
    assert "example.com" not in client.get("/v1/beacons").text


def test_a_beacon_that_names_no_mode_is_direct(client):
    """Every beacon minted before 2.3 carried a visit_url meaning "show
    this". Reading those as `id` would swap a working link for a redirect
    their owner never asked for."""
    key = _key()
    client.post("/v1/beacons", json=_env(key, _beacon(visit_url="https://example.com/")))
    props = client.get("/v1/beacons").json()["features"][0]["properties"]
    assert props["link_mode"] == "direct" and props["visit_url"] == "https://example.com/"


def test_a_bad_link_mode_or_region_is_refused(client):
    key = _key()
    for over in ({"link_mode": "proxy"}, {"region": "arizona"}, {"region": "az"},
                 {"region": "A"}, {"region": 4}):
        r = client.post("/v1/beacons", json=_env(key, _beacon(**over)))
        assert r.status_code == 400, (over, r.text)
        m._last_seen_ms.clear()


def test_a_blocked_or_withdrawn_server_stops_resolving(client):
    key = _key()
    now = int(time.time() * 1000)
    r = client.post("/v1/beacons", json=_env(key, _beacon(
        now=now, visit_url="https://example.com/", link_mode="id", region="AZ"))).json()
    pid = r["public_id"]
    assert client.get(f"/s/{pid}", follow_redirects=False).status_code == 200
    tomb = {"v": 1, "server_id": "srv-a", "station_id": "st-1", "withdraw": True,
            "sent_ms": now + 1}
    assert client.post("/v1/withdraw", json=_env(key, tomb)).status_code == 200
    # No live beacon left, so the id resolves to nothing.
    assert client.get(f"/s/{pid}", follow_redirects=False).status_code == 404


def test_id_mode_with_nothing_behind_it_offers_no_link(client):
    """A pin in `id` mode whose server sent no address would carry a Visit
    link straight to a 404 — the app turns the link off by clearing the
    address, not by changing the mode."""
    key = _key()
    r = client.post("/v1/beacons", json=_env(key, _beacon(link_mode="id", region="AZ")))
    assert r.status_code == 200
    assert r.json()["visit"] is None
    props = client.get("/v1/beacons").json()["features"][0]["properties"]
    assert "visit_url" not in props and "public_id" not in props
    assert client.get(f"/s/{r.json()['public_id']}", follow_redirects=False).status_code == 404


PRE_2_3_SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    server_id  TEXT PRIMARY KEY,
    pubkey     TEXT NOT NULL,
    first_ms   INTEGER NOT NULL,
    last_ms    INTEGER NOT NULL,
    blocked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS beacons (
    station_id  TEXT PRIMARY KEY, server_id TEXT NOT NULL, sent_ms INTEGER NOT NULL,
    expires_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beacons_expires ON beacons(expires_ms);
"""


def test_a_directory_that_predates_the_ids_upgrades_in_place(tmp_path, monkeypatch):
    """Every other test starts from an empty file, where CREATE TABLE builds
    `servers` WITH the id columns. A LIVE directory has the table already,
    without them — and the unique index used to sit in SCHEMA, which
    `executescript` runs before the migration can add the column. It died
    with "no such column: public_id" at boot and took the deploy down
    (2026-09-14, zasder-map). Start from the real old schema."""
    import sqlite3
    import importlib
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(db_path)
    old.executescript(PRE_2_3_SCHEMA)
    key = _key()
    old.execute("INSERT INTO servers VALUES (?, ?, 1, 2, 0)", ("srv-old", _pub(key)))
    old.commit()
    old.close()

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    from app import main as mod
    importlib.reload(mod)
    mod._last_seen_ms.clear()
    from fastapi.testclient import TestClient
    with TestClient(mod.app) as c:                 # boots = runs init_db
        after = sqlite3.connect(db_path)
        cols = {r[1] for r in after.execute("PRAGMA table_info(servers)")}
        assert {"public_id", "visit_url", "last_sent_ms"} <= cols
        # The pinned key survived: an upgrade must not make every server
        # re-pin, which would silently reopen trust on first use.
        assert after.execute("SELECT COUNT(*) FROM servers").fetchone()[0] == 1
        assert after.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_servers_public_id'").fetchone() is not None
        after.close()
        # The server that was already pinned gets an id on its next beacon,
        # signed by the key it pinned before the upgrade.
        r = c.post("/v1/beacons", json=_env(key, _beacon(
            server="srv-old", visit_url="https://example.com/",
            link_mode="id", region="AZ")))
        assert r.status_code == 200, r.text
        assert r.json()["public_id"].startswith("AZ")

    # Idempotent: booting again over the migrated file is a no-op.
    importlib.reload(mod)
    with TestClient(mod.app):
        pass


def test_an_id_is_minted_only_when_a_server_asks_for_id_mode(client):
    """Ordering bite, 2026-09-14: the directory went out before the servers
    posting to it, so the first beacon it saw from each came from an older
    build with no region — and an id minted there would have been stuck
    without its region prefix for good, because ids are never reissued.
    Volney's own server drew `FYKP449G` where `AZ...` was the whole point.
    Wait for the beacon that actually asks."""
    key = _key()
    # An older build: no link_mode, no region.
    r = client.post("/v1/beacons", json=_env(key, _beacon(visit_url="https://example.com/")))
    assert r.json()["public_id"] is None
    m._last_seen_ms.clear()

    # It upgrades and asks for `id`, now reporting its region.
    now = int(time.time() * 1000) + 1000
    r = client.post("/v1/beacons", json=_env(key, _beacon(
        now=now, visit_url="https://example.com/", link_mode="id", region="AZ")))
    pid = r.json()["public_id"]
    assert pid.startswith("AZ") and len(pid) == 8
    assert client.get(f"/s/{pid}", follow_redirects=False).status_code == 200


def test_only_the_operator_may_reissue_a_public_id(admin_client):
    """Ids are never reissued, because links already handed out have to keep
    working — and only the operator can know whether any have been. So the
    override is theirs alone. It exists because the first badly minted id
    was the directory's own fault: deployed ahead of the servers posting to
    it, ids came out with no region prefix (2026-09-14)."""
    H = {"Authorization": "Bearer operator-secret-for-tests"}
    key = _key()
    r = admin_client.post("/v1/beacons", json=_env(key, _beacon(
        visit_url="https://example.com/", link_mode="id"))).json()
    minted = r["public_id"]
    assert len(minted) == 8 and not minted.startswith("AZ")   # no region reported

    # Not without the token, and not with a bad one.
    assert admin_client.post("/v1/admin/servers/srv-a/public-id",
                             json={"public_id": "AZZASDER"}).status_code == 401
    # The alphabet is enforced: I, L, O and U are exactly the characters a
    # person misreads, so an id containing them is refused rather than
    # quietly rewritten.
    # 422 for the over-long one: pydantic's max_length refuses it before the
    # route sees it. Both are refusals.
    for bad in ("AZZASDEI", "AZZASDEL", "AZZASDEO", "AZZASDEU", "AZ", "A" * 17, "az-tag!"):
        assert admin_client.post("/v1/admin/servers/srv-a/public-id",
                                 headers=H, json={"public_id": bad}
                                 ).status_code in (400, 422), bad

    r = admin_client.post("/v1/admin/servers/srv-a/public-id",
                          headers=H, json={"public_id": "azzasder"}).json()
    assert r["public_id"] == "AZZASDER" and r["previous"] == minted

    # The new id resolves and the old one stops.
    assert admin_client.get("/s/AZZASDER", follow_redirects=False).status_code == 200
    assert admin_client.get(f"/s/{minted}", follow_redirects=False).status_code == 404
    assert admin_client.get("/v1/beacons").json(
        )["features"][0]["properties"]["public_id"] == "AZZASDER"

    # An unknown server is a 404, and a second server cannot take the id.
    assert admin_client.post("/v1/admin/servers/nope/public-id",
                             headers=H, json={"public_id": "AZOTHER1"}).status_code in (400, 404)
    other = _key()
    m._last_seen_ms.clear()
    admin_client.post("/v1/beacons", json=_env(other, _beacon(
        server="srv-b", station="st-b", visit_url="https://b.example.com/",
        link_mode="id")))
    assert admin_client.post("/v1/admin/servers/srv-b/public-id",
                             headers=H, json={"public_id": "AZZASDER"}).status_code == 409


# ── R23: what one poster can and cannot do to everyone else ──────────


def test_readings_are_stored_as_numbers_and_a_bool_is_refused(client):
    """A numeric STRING passed `_num` and was stored as sent; the map page
    then called `.toFixed` on it, the render threw, and every visitor read
    "map unavailable" for three hours (R23). Coerce on the way in, and a
    bool (True is 1.0 to float()) is not a reading at all."""
    key = _key()
    now = int(time.time() * 1000)
    r = client.post("/v1/beacons", json=_env(key, _beacon(
        now=now, conditions={"tempf": "99.4", "humidity": 21, "baromrelin": "29.71"})))
    assert r.status_code == 200, r.text
    cond = client.get("/v1/beacons").json()["features"][0]["properties"]["conditions"]
    assert cond == {"tempf": 99.4, "humidity": 21.0, "baromrelin": 29.71}
    assert all(isinstance(v, float) for v in cond.values())
    # The stored body itself carries numbers, so a raw read of the table
    # (the single-station lookup, an export) can never hand a string out.
    one = client.get("/v1/stations/st-1").json()["properties"]["conditions"]
    assert one["baromrelin"] == 29.71 and isinstance(one["baromrelin"], float)
    m._last_seen_ms.clear()
    for bad in ({"tempf": True}, {"tempf": "warm"}, {"tempf": None}, {"tempf": [1]}):
        r = client.post("/v1/beacons", json=_env(key, _beacon(now=now + 1000, conditions=bad)))
        assert r.status_code == 400, (bad, r.text)
        m._last_seen_ms.clear()
    # Coordinates get the same treatment: a string lat is stored as a number.
    r = client.post("/v1/beacons", json=_env(key, _beacon(now=now + 2000, lat="33.31")))
    assert r.status_code == 200
    assert client.get("/v1/beacons").json()["features"][0]["geometry"]["coordinates"] == [-111.84, 33.31]


def test_admin_lockout_is_per_address_and_a_non_ascii_token_is_a_401(admin_client):
    """One global bucket let ten bad bearers a minute from ANYONE lock the
    operator out of the blocklist (R23). And `compare_digest` on str raises
    on a non-ASCII token: a 500 the counter never saw."""
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer operator-secret-for-tests"}
    with TestClient(m.app, client=("10.0.0.1", 40000)) as stranger, \
            TestClient(m.app, client=("10.0.0.2", 40001)) as operator:
        for _ in range(m.ADMIN_FAIL_LIMIT):
            assert stranger.get("/v1/admin/servers",
                                headers={"Authorization": "Bearer nope"}).status_code == 401
        assert stranger.get("/v1/admin/servers", headers=H).status_code == 429
        # The operator, from another address, is not locked out.
        assert operator.get("/v1/admin/servers", headers=H).status_code == 200
        # A non-ASCII bearer: refused and counted, never a 500. Bytes, because
        # httpx refuses to encode a non-ASCII str header itself.
        r = operator.get("/v1/admin/servers",
                         headers={"Authorization": "Bearer tökén-ñ".encode("utf-8")})
        assert r.status_code == 401, r.text
        assert len(m._admin_failures["10.0.0.2"]) == 1
        assert operator.get("/v1/admin/servers", headers=H).status_code == 200


def test_the_body_cap_holds_for_a_chunked_body_and_long_strings(client):
    """Content-Length was checked after parsing, so a chunked 300 KB beacon
    declared no length, was parsed, stored and served (R23). The body is
    now read in chunks and refused past the cap before anything parses it;
    the free-text fields are bounded too."""
    key = _key()
    now = int(time.time() * 1000)

    def chunks():
        for _ in range(300):
            yield b"x" * 1024
    r = client.post("/v1/beacons", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text
    assert "content-length" not in r.request.headers, "the test must send a chunked body"
    r = client.post("/v1/withdraw", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    # A declared length over the cap is refused too, and a malformed one
    # is a 400, not a 500.
    r = client.post("/v1/beacons", content=b"{}",
                    headers={"Content-Type": "application/json", "Content-Length": "900000"})
    assert r.status_code == 413
    # Under the cap but not an envelope: a 400 with a sentence, not a trace.
    assert client.post("/v1/beacons", content=b"not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
    assert client.get("/v1/beacons").json()["count"] == 0
    # Text fields: 64 is fine, 65 is not; and the typed fields are typed.
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now, software="s" * 64))).status_code == 200
    m._last_seen_ms.clear()
    for over in ({"software": "s" * 65}, {"tz": "t" * 65}, {"sensor": "k" * 65},
                 {"software": 7}, {"fuzzed": "yes"}, {"fuzzed": 1},
                 {"observed_ms": "1"}, {"observed_ms": True}, {"sent_ms": True}):
        r = client.post("/v1/beacons", json=_env(key, _beacon(now=now + 1000, **over)))
        assert r.status_code == 400, (over, r.text)
        m._last_seen_ms.clear()


def test_a_replayed_tombstone_and_an_older_beacon_are_refused(client):
    """Withdrawals had no monotonic `sent_ms`: a tombstone captured once
    could be posted again whenever the station came back and knock it off
    the map (R23). Every accepted message moves the server's high-water
    mark; nothing at or below it is accepted again. In order still works."""
    key = _key()
    now = int(time.time() * 1000)
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now))).status_code == 200
    tomb = _env(key, {"v": 1, "server_id": "srv-a", "station_id": "st-1", "withdraw": True,
                      "sent_ms": now + 1})
    r = client.post("/v1/withdraw", json=tomb)
    assert r.status_code == 200 and r.json()["removed"] == 1
    # In order: the station comes back with a newer beacon.
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 2))).status_code == 200
    assert client.get("/v1/beacons").json()["count"] == 1
    # The captured tombstone, replayed: refused, and the station stays.
    m._last_withdraw_ms.clear()
    r = client.post("/v1/withdraw", json=tomb)
    assert r.status_code == 409, r.text
    assert client.get("/v1/beacons").json()["count"] == 1
    # A beacon older than the last accepted message: refused, even for a
    # station the directory has never seen (the per-station check alone
    # would have let it in).
    m._last_seen_ms.clear()
    r = client.post("/v1/beacons", json=_env(key, _beacon(station="st-9", now=now + 1)))
    assert r.status_code == 409, r.text
    assert client.get("/v1/beacons").json()["count"] == 1
    # Equal is a replay too.
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 2))).status_code == 409
    # A refused message did not move the mark: now + 3 is still in order.
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 3))).status_code == 200
    # A withdrawal's sent_ms is checked like a beacon's (a bool, the future).
    m._last_withdraw_ms.clear()
    bad = _env(key, {"v": 1, "server_id": "srv-a", "station_id": "st-1", "withdraw": True,
                     "sent_ms": now + 20 * 60_000})
    assert client.post("/v1/withdraw", json=bad).status_code == 400


def test_withdrawals_are_rate_limited_like_beacons(client):
    key = _key()
    now = int(time.time() * 1000)
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now))).status_code == 200
    # A withdrawal right after a beacon is fine (the switch going off).
    t1 = {"v": 1, "server_id": "srv-a", "station_id": "st-1", "withdraw": True, "sent_ms": now + 1}
    assert client.post("/v1/withdraw", json=_env(key, t1)).status_code == 200
    t2 = dict(t1, sent_ms=now + 2)
    r = client.post("/v1/withdraw", json=_env(key, t2))
    assert r.status_code == 429 and "a minute" in r.json()["detail"]


SCHEMA_2_3_0 = """
CREATE TABLE IF NOT EXISTS servers (
    server_id  TEXT PRIMARY KEY,
    pubkey     TEXT NOT NULL,
    first_ms   INTEGER NOT NULL,
    last_ms    INTEGER NOT NULL,
    blocked    INTEGER NOT NULL DEFAULT 0,
    public_id  TEXT,
    visit_url  TEXT
);
CREATE TABLE IF NOT EXISTS beacons (
    station_id  TEXT PRIMARY KEY, server_id TEXT NOT NULL, sent_ms INTEGER NOT NULL,
    expires_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beacons_expires ON beacons(expires_ms);
CREATE UNIQUE INDEX IF NOT EXISTS idx_servers_public_id
    ON servers(public_id) WHERE public_id IS NOT NULL;
"""


def test_a_directory_at_2_3_0_gains_the_replay_column_in_place(tmp_path, monkeypatch):
    """The schema the live directory has TODAY (ids and visit_url, no
    `last_sent_ms`), with a pinned server and a live beacon on it. Boot
    over it: the column arrives, the pin survives, the beacon is still
    served, and the mark starts working from the next message."""
    import sqlite3
    import importlib
    db_path = tmp_path / "v230.db"
    old = sqlite3.connect(db_path)
    old.executescript(SCHEMA_2_3_0)
    key = _key()
    now = int(time.time() * 1000)
    live = _beacon(server="srv-old", now=now - 60_000)
    old.execute("INSERT INTO servers VALUES (?, ?, 1, 2, 0, 'AZOLDONE', 'https://example.com/')",
                ("srv-old", _pub(key)))
    old.execute("INSERT INTO beacons VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("st-1", "srv-old", live["sent_ms"], live["expires_ms"], now - 60_000,
                 live["lat"], live["lon"], json.dumps(live)))
    old.commit()
    old.close()

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    from app import main as mod
    importlib.reload(mod)
    mod._last_seen_ms.clear()
    from fastapi.testclient import TestClient
    with TestClient(mod.app) as c:
        after = sqlite3.connect(db_path)
        cols = {r[1] for r in after.execute("PRAGMA table_info(servers)")}
        assert "last_sent_ms" in cols
        assert after.execute("SELECT last_sent_ms FROM servers").fetchone() == (None,)
        after.close()
        assert c.get("/v1/beacons").json()["count"] == 1
        # The next beacon, signed by the key pinned before the upgrade.
        r = c.post("/v1/beacons", json=_env(key, _beacon(server="srv-old", now=now)))
        assert r.status_code == 200, r.text
        mod._last_seen_ms.clear()
        assert c.post("/v1/beacons", json=_env(key, _beacon(server="srv-old", now=now))).status_code == 409
        after = sqlite3.connect(db_path)
        assert after.execute("SELECT last_sent_ms FROM servers").fetchone() == (now,)
        after.close()
    importlib.reload(mod)
    with TestClient(mod.app):
        pass


def test_the_visit_page_names_the_host_and_never_redirects(client):
    """A bare 302 to any https address a poster signed was an open
    redirect: a reader who clicked a pin was on a stranger's server before
    seeing where the link went (R23). Now a page names the host, with a
    Continue link, under the same CSP and with nothing inline."""
    key = _key()
    r = client.post("/v1/beacons", json=_env(key, _beacon(
        visit_url="https://weather.example.org/dash?x=<img>", link_mode="id", region="AZ"))).json()
    pid = r["public_id"]
    page = client.get(f"/s/{pid}", follow_redirects=False)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "location" not in page.headers
    assert "weather.example.org" in page.text and "Continue" in page.text
    assert pid in page.text
    # Escaped, not inlined: the address is whatever the server signed.
    assert "<img" not in page.text and "&lt;img&gt;" in page.text
    assert 'href="https://weather.example.org/dash?x=&lt;img&gt;"' in page.text
    assert "<script" not in page.text and "style=" not in page.text
    assert "rel=\"noopener noreferrer nofollow\"" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    csp = page.headers["content-security-policy"]
    assert "style-src 'self'" in csp and "'unsafe-inline'" not in csp
    assert client.get("/static/visit.css").status_code == 200


def test_expired_rows_do_not_count_against_the_station_cap(client):
    """The cap counted every row for the server, expired ones included, so
    a server whose sixteen old stations had all lapsed could not list a
    new one until the listing route happened to sweep (R23). The post
    sweeps first."""
    import sqlite3
    key = _key()
    now = int(time.time() * 1000)
    db = sqlite3.connect(m.DB_PATH)
    for i in range(m.MAX_STATIONS_PER_SERVER):
        stale = _beacon(station=f"old-{i}", now=now - 4 * 3_600_000)
        db.execute("INSERT INTO beacons VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (stale["station_id"], "srv-a", stale["sent_ms"], stale["expires_ms"],
                    now - 4 * 3_600_000, stale["lat"], stale["lon"], json.dumps(stale)))
    db.commit()
    db.close()
    r = client.post("/v1/beacons", json=_env(key, _beacon(station="st-new", now=now)))
    assert r.status_code == 200, r.text
    db = sqlite3.connect(m.DB_PATH)
    assert db.execute("SELECT COUNT(*) FROM beacons").fetchone()[0] == 1
    db.close()


def test_the_listing_is_ordered_by_receipt_not_by_the_senders_clock(client):
    """`sent_ms` is whatever the sender's clock said; ordering the public
    listing by it let one server with a fast clock sort itself to the top
    and the LIMIT is applied on that order (R23). Receipt time is ours."""
    import sqlite3
    now = int(time.time() * 1000)
    ka, kb = _key(), _key()
    # srv-a claims a sent time well AHEAD of srv-b's...
    assert client.post("/v1/beacons", json=_env(ka, _beacon(server="srv-a", station="st-a",
                                                             now=now + 5 * 60_000))).status_code == 200
    assert client.post("/v1/beacons", json=_env(kb, _beacon(server="srv-b", station="st-b",
                                                             now=now))).status_code == 200
    # ...but was received earlier (pinned so the two posts cannot tie).
    db = sqlite3.connect(m.DB_PATH)
    db.execute("UPDATE beacons SET received_ms = ? WHERE station_id = 'st-a'", (now - 10_000,))
    db.commit()
    db.close()
    ids = [f["properties"]["station_id"] for f in client.get("/v1/beacons").json()["features"]]
    assert ids == ["st-b", "st-a"]


def test_posts_from_one_address_are_capped_a_minute(client):
    """A fresh key mints a fresh server id, so the per-server minute never
    saw a sender that changed keys per request: one servers row and one
    beacons row per request, forever (R23). The address is the one thing
    such a sender cannot mint."""
    now = int(time.time() * 1000)
    for i in range(m.POST_IP_LIMIT):
        r = client.post("/v1/beacons", json=_env(_key(), _beacon(
            server=f"srv-{i:03d}", station=f"st-{i:03d}", now=now + i)))
        assert r.status_code == 200, (i, r.text)
    r = client.post("/v1/beacons", json=_env(_key(), _beacon(server="srv-more", station="st-more",
                                                             now=now + 999)))
    assert r.status_code == 429 and "address" in r.json()["detail"]
    # Withdrawals share the window: the same sender cannot switch routes.
    tomb = {"v": 1, "server_id": "srv-000", "station_id": "st-000", "withdraw": True,
            "sent_ms": now + 1000}
    assert client.post("/v1/withdraw", json=_env(_key(), tomb)).status_code == 429
    # Another address is unaffected.
    from fastapi.testclient import TestClient
    with TestClient(m.app, client=("10.9.9.9", 1)) as other:
        r = other.post("/v1/beacons", json=_env(_key(), _beacon(server="srv-other", station="st-o",
                                                                 now=now + 1001)))
        assert r.status_code == 200, r.text


def test_a_new_server_past_the_cap_is_refused_while_known_ones_still_post(client, monkeypatch):
    monkeypatch.setattr(m, "MAX_SERVERS", 2)
    now = int(time.time() * 1000)
    ka, kb, kc = _key(), _key(), _key()
    assert client.post("/v1/beacons", json=_env(ka, _beacon(server="srv-a", station="st-a", now=now))).status_code == 200
    assert client.post("/v1/beacons", json=_env(kb, _beacon(server="srv-b", station="st-b", now=now))).status_code == 200
    r = client.post("/v1/beacons", json=_env(kc, _beacon(server="srv-c", station="st-c", now=now)))
    assert r.status_code == 503 and "not taking new ones" in r.json()["detail"]
    assert client.get("/v1/stats").json()["servers"] == 2
    # A known server keeps posting; a withdrawal from an unknown one is
    # refused the same way (nothing to withdraw, and no pin minted for it).
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(ka, _beacon(server="srv-a", station="st-a", now=now + 1))).status_code == 200
    tomb = {"v": 1, "server_id": "srv-c", "station_id": "st-c", "withdraw": True, "sent_ms": now + 2}
    assert client.post("/v1/withdraw", json=_env(kc, tomb)).status_code == 503


def test_the_recent_maps_are_bounded():
    recent = m._BoundedRecent(3)
    for i in range(5):
        recent[f"srv-{i}"] = i
    assert len(recent) == 3 and list(recent) == ["srv-2", "srv-3", "srv-4"]
    # Touching an entry keeps it; the untouched oldest goes next.
    recent["srv-2"] = 9
    recent["srv-5"] = 5
    assert list(recent) == ["srv-4", "srv-2", "srv-5"]
    assert m._last_seen_ms.bound == m.RECENT_BOUND == 10_000
    assert m._last_withdraw_ms.bound == m.RECENT_BOUND
    # The per-address windows forget an address after its minute.
    store = {"1.1.1.1": [0], "2.2.2.2": [100_000]}
    assert m._window(store, "3.3.3.3", 120_000) == []
    assert store == {"2.2.2.2": [100_000]}


def test_a_fast_clock_does_not_lock_a_server_out_once_it_is_fixed(client):
    """The high-water mark used to be the sender's own `sent_ms`. A server
    whose clock ran fast set a mark in the future and was then refused
    with 409 until wall time caught up with its mistake, even after it
    fixed the clock. The mark is clamped to the wall clock at receipt."""
    from app import main as m
    key = _key()
    now = int(time.time() * 1000)
    fast = now + m.MAX_FUTURE_MS - 30_000          # as fast as is accepted
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=fast))).status_code == 200
    m._last_seen_ms.clear()
    # Clock corrected: a beacon stamped a second from now is accepted...
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 1000))).status_code == 200
    m._last_seen_ms.clear()
    # ...and so is a withdrawal from the corrected clock.
    tomb = {"v": 1, "server_id": "srv-a", "station_id": "st-1", "withdraw": True,
            "sent_ms": now + 2000}
    assert client.post("/v1/withdraw", json=_env(key, tomb)).status_code == 200
    # A true replay is still a replay: the tombstone is stamped a moment
    # AHEAD of the wall clock (the clamped mark sits below it), and it is
    # refused all the same.
    m._last_withdraw_ms.clear()
    r = client.post("/v1/withdraw", json=_env(key, tomb))
    assert r.status_code == 409 and "already" in r.json()["detail"]
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now + 1000))).status_code == 409
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=now - 1000))).status_code == 409
    # And the fast beacon itself, replayed while its stamp is still in
    # the future, is refused too.
    m._last_seen_ms.clear()
    assert client.post("/v1/beacons", json=_env(key, _beacon(now=fast))).status_code == 409


def test_a_key_rotation_the_directory_applied_resets_the_mark(client):
    """The new key's first message is the owner starting over; the proof
    it carries was signed by the key on file, which no replay can forge.
    Without the reset a rotation posted from a corrected clock after a
    fast-clock beacon was refused as a replay of its predecessor."""
    from app import main as m
    old, new = _key(), _key()
    now = int(time.time() * 1000)
    assert client.post("/v1/beacons", json=_env(old, _beacon(now=now + 60_000))).status_code == 200
    m._last_seen_ms.clear()
    # Stamped before the server's mark (the clamp put it at receipt time),
    # on a station with no beacon of its own to compare against: only the
    # per-server mark stands in the way, and the rotation resets it.
    env = _env(new, _beacon(now=now - 5_000, station="st-2"))
    env["rotation"] = _rotation(old, "srv-a", new)
    r = client.post("/v1/beacons", json=env)
    assert r.status_code == 200 and r.json()["rotated"] is True
    m._last_seen_ms.clear()
    # The old key's beacon, replayed, is refused: the key is no longer on file.
    assert client.post("/v1/beacons", json=_env(old, _beacon(now=now + 60_000))).status_code == 403


def test_epoch_fields_are_bounded_to_what_a_reader_can_hold(client):
    """F08: a signed beacon with observed_ms = 10**40 was accepted and
    served; Python holds it, an Int64 does not, and native readers dropped
    the pin. Every epoch field must sit after 2000 and inside an Int64,
    and observed_ms may not be in the future either."""
    from app import main as m
    key = _key()
    now = int(time.time() * 1000)

    def post(**over):
        m._last_seen_ms.clear()
        return client.post("/v1/beacons", json=_env(key, _beacon(now=now, **over)))
    for bad in (10 ** 40, -1, 0, 946_684_800_000, 2 ** 63, now + m.MAX_FUTURE_MS + 60_000):
        r = post(observed_ms=bad)
        assert r.status_code == 400, bad
        assert "observed_ms" in r.json()["detail"]
    for bad in (10 ** 40, -1, 0):
        assert post(sent_ms=bad).status_code == 400, bad
    assert post(expires_ms=10 ** 40).status_code == 400
    r = post(observed_ms=now - 60_000)
    assert r.status_code == 200
    geo = client.get("/v1/beacons").json()
    assert geo["features"][0]["properties"]["observed_ms"] == now - 60_000
