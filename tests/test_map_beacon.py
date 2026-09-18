"""The map beacon (2.3): the owner's opt-in appearance on the shared map."""
import asyncio
import base64
import json

H = {"Authorization": "Bearer test-api-token"}


def _station(mac="AA:BB:CC:DD:EE:71", lat=33.3062, lon=-111.8413, name="Backyard"):
    return {"mac": mac, "name": name,
            "info": {"coords": {"coords": {"lat": lat, "lon": lon}}, "type": "tempest"},
            "lastData": {"dateutc": 1789200000000, "tempf": 99.4, "humidity": 21,
                         "windspeedmph": 4, "winddir": 250, "baromrelin": 29.71,
                         "dailyrainin": 0, "tempinf": 78, "co2": 640, "pm25": 3,
                         "lightning_num": 2}}


def test_fuzz_snaps_to_a_half_kilometre_cell(client):
    from app import map_beacon as mb
    lat, lon = mb.fuzz(33.3062, -111.8413)
    # Never the exact point, always inside the cell, and stable.
    assert (lat, lon) != (33.3062, -111.8413)
    assert abs(lat - 33.3062) < 0.5 / 111.32 and abs(lon + 111.8413) < 0.5 / (111.32 * 0.835)
    assert mb.fuzz(33.3062, -111.8413) == (lat, lon)
    assert mb.fuzz(lat, lon) == (lat, lon)                 # the centre is a fixed point
    assert mb.fuzz(lat + 0.0004, lon + 0.0004) == (lat, lon)   # ~50 m off centre, same cell
    # A neighbour a kilometre north is another cell.
    assert mb.fuzz(33.3152, -111.8413) != (lat, lon)
    # Polar and antimeridian inputs stay in range.
    plat, plon = mb.fuzz(89.99, 179.999)
    assert -90 <= plat <= 90 and -180 <= plon <= 180


def test_the_beacon_carries_outdoor_conditions_and_nothing_private(client, monkeypatch):
    from app import map_beacon as mb, config
    monkeypatch.setattr(config.settings, "timezone", "America/Phoenix")
    b = mb.build(server_id="srv-1", station=_station(), cfg={}, now_ms=1789200060000)
    assert b["v"] == 1 and b["fuzzed"] is True and b["tz"] == "America/Phoenix"
    assert b["station_id"] == mb.station_id("srv-1", "AA:BB:CC:DD:EE:71") and len(b["station_id"]) == 16
    assert "AA:BB" not in json.dumps(b) and "name" not in b and "visit_url" not in b
    assert b["conditions"] == {"tempf": 99.4, "humidity": 21, "windspeedmph": 4,
                               "winddir": 250, "baromrelin": 29.71, "dailyrainin": 0}
    assert "tempinf" not in json.dumps(b) and "co2" not in json.dumps(b)
    assert b["observed_ms"] == 1789200000000 and b["expires_ms"] == 1789200060000 + mb.TTL_MS
    assert b["sensor"] == "tempest"
    named = mb.build(server_id="srv-1", station=_station(),
                     cfg={"name_visible": True, "location_precision": "exact"},
                     now_ms=1789200060000, visit_url="https://chaucerdrive.com")
    assert named["name"] == "Backyard" and named["visit_url"] == "https://chaucerdrive.com"
    assert named["fuzzed"] is False and named["precision"] == "exact" and named["lat"] == 33.3062
    # No http visit links, no coordinates: no beacon.
    plain = mb.build(server_id="srv-1", station=_station(), cfg={}, visit_url="http://x",
                     now_ms=1789200060000)
    assert "visit_url" not in plain
    nowhere = _station(); nowhere["info"] = {}
    assert mb.build(server_id="srv-1", station=nowhere, cfg={}, now_ms=1) is None


def test_signatures_verify_and_a_changed_byte_does_not(client):
    from app import map_beacon as mb
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.exceptions import InvalidSignature
    import pytest

    async def run():
        k1 = await mb.ensure_key()
        k2 = await mb.ensure_key()
        return k1, k2
    k1, k2 = asyncio.run(run())
    assert mb.public_key_b64(k1) == mb.public_key_b64(k2)       # minted once
    b = mb.build(server_id="srv-1", station=_station(), cfg={}, now_ms=1789200060000)
    env = mb.sign(b, k1)
    pub = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(env["pubkey"]))
    pub.verify(base64.b64decode(env["sig"]), mb.canonical(env["beacon"]))
    tampered = dict(env["beacon"], lat=env["beacon"]["lat"] + 0.01)
    with pytest.raises(InvalidSignature):
        pub.verify(base64.b64decode(env["sig"]), mb.canonical(tampered))


def test_the_switch_publishes_on_the_tick_and_withdraws_when_turned_off(client, monkeypatch):
    import time as _time
    from datetime import datetime, timezone
    from app import map_beacon as mb, db
    mb._reset_for_tests()
    # The verify-now route reads the wall clock, so the reading is stamped
    # five minutes before real now.
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 300_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:72", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp,
                      "outdoor": {"tempf": 98}})
    posts = []

    async def fake_post(path, envelope):
        posts.append((path, envelope))
        return None, {}
    monkeypatch.setattr(mb, "_post", fake_post)
    g = client.get("/api/map", headers=H).json()
    assert g["enabled"] is False and g["fuzz_km"] == 0.5 and g["preview"]["fuzzed"] is True
    assert g["preview"]["conditions"]["tempf"] == 98
    r = client.put("/api/map", headers=H, json={"enabled": True, "name_visible": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert client.put("/api/map", headers=H, json={"location_precision": "street"}).status_code == 400

    async def run():
        devs = await db.list_devices()
        await mb.publish_if_due(devs, now)
        await mb.publish_if_due(devs, now + 60_000)          # inside the cadence
        await mb.publish_if_due(devs, now + mb.INTERVAL_MS)
        return await mb.get_status()
    status = asyncio.run(run())
    assert [p for p, _ in posts] == ["/v1/beacons", "/v1/beacons"]
    assert posts[0][1]["beacon"]["name"] == "Yard" and posts[0][1]["pubkey"]
    assert status["last_ok_ms"] == now + mb.INTERVAL_MS
    # Verify-now sends regardless of cadence and the status reads ok.
    t = client.post("/api/map/test", headers=H).json()
    assert t["ok"] is True, t
    assert len(posts) == 3
    # Off: a signed withdrawal goes out at once.
    r = client.put("/api/map", headers=H, json={"enabled": False})
    assert r.json()["enabled"] is False
    assert posts[-1][0] == "/v1/withdraw" and posts[-1][1]["beacon"]["withdraw"] is True
    assert posts[-1][1]["beacon"]["station_id"] == posts[0][1]["beacon"]["station_id"]
    # A stale reading is not published as current.
    async def stale():
        devs = await db.list_devices()
        return await mb.publish_once(devs, now + 3 * 3_600_000)
    res = asyncio.run(stale())
    assert res["ok"] is False and "old" in res["error"]
    # Read-only tokens may not see or set the switch.
    assert client.get("/api/map").status_code == 401


def test_key_rotation_carries_the_old_keys_blessing_until_acknowledged(client, monkeypatch):
    """2.3: rotate mints a new key; every beacon then carries the old
    key's signature over (server_id, new pubkey), exactly what the
    directory verifies, until the directory ACCEPTS one — any 2xx to a
    beacon signed by the new key proves the new key is on file (R23: an
    explicit rotated=true that got lost stranded the server with PREV
    set forever)."""
    import base64
    import json
    import time as _time
    from datetime import datetime, timezone
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from app import map_beacon as mb, db
    mb._reset_for_tests()
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 60_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:73", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp, "outdoor": {"tempf": 90}})
    posts = []
    directory = {"down": False}

    async def fake_post(path, envelope):
        posts.append((path, envelope))
        if directory["down"]:
            return "ConnectError", {}
        return None, {"ok": True}
    monkeypatch.setattr(mb, "_post", fake_post)
    client.put("/api/map", headers=H, json={"enabled": True})
    before = client.get("/api/map", headers=H).json()
    assert before["rotation_pending"] is False and before["pubkey"]
    old_pub = before["pubkey"]

    # The directory is down when the key rotates: the proof must wait.
    directory["down"] = True
    r = client.post("/api/map/rotate", headers=H).json()
    assert r["ok"] and r["pubkey"] != old_pub and not r["published"]["ok"]
    assert r["rotation_pending"] is True, "the directory has not acknowledged yet"
    env = posts[-1][1]
    assert env["pubkey"] == r["pubkey"]
    assert env["rotation"]["prev_pubkey"] == old_pub
    # The proof verifies the way the directory checks it.
    server_id = env["beacon"]["server_id"]
    msg = json.dumps({"rotate": server_id, "to": env["pubkey"]}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode()
    ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(old_pub)).verify(
        base64.b64decode(env["rotation"]["sig"]), msg)
    # Still down: the next beacon still carries it.
    assert not client.post("/api/map/test", headers=H).json()["ok"]
    assert "rotation" in posts[-1][1]
    assert client.get("/api/map", headers=H).json()["rotation_pending"] is True
    # Back up: the first accepted beacon is the acknowledgement, the old
    # key is dropped and the proof stops.
    directory["down"] = False
    assert client.post("/api/map/test", headers=H).json()["ok"]
    assert "rotation" in posts[-1][1], "the accepted beacon carried the proof"
    assert client.get("/api/map", headers=H).json()["rotation_pending"] is False
    assert client.post("/api/map/test", headers=H).json()["ok"]
    assert "rotation" not in posts[-1][1]
    # A second rotation before acknowledgement is REFUSED (S2, the
    # 2026-09-16 review): it used to rotate anyway and keep the oldest
    # unacknowledged key as the blesser, which stranded a server whose
    # first rotation the directory had accepted but whose reply was lost.
    # The route first tries to resolve the pending one with a beacon; with
    # the directory down that cannot land, so nothing changes.
    directory["down"] = True
    r1 = client.post("/api/map/rotate", headers=H).json()
    assert r1["rotation_pending"] is True
    n = len(posts)
    r2 = client.post("/api/map/rotate", headers=H)
    assert r2.status_code == 409 and mb.ROTATION_PENDING in r2.json()["detail"], r2.text
    assert len(posts) == n + 1, "one resolving beacon went out, and nothing else"
    assert posts[-1][1]["pubkey"] == r1["pubkey"], "no key was minted"
    assert posts[-1][1]["rotation"]["prev_pubkey"] == r["pubkey"]
    assert client.get("/api/map", headers=H).json()["pubkey"] == r1["pubkey"]
    # Back up: the resolving beacon lands, the hand-over is acknowledged,
    # and the rotation goes through in the same call.
    directory["down"] = False
    r3 = client.post("/api/map/rotate", headers=H).json()
    assert r3["ok"] and r3["pubkey"] != r1["pubkey"]
    assert posts[-2][1]["pubkey"] == r1["pubkey"] and posts[-2][1]["rotation"]["prev_pubkey"] == r["pubkey"]
    assert posts[-1][1]["pubkey"] == r3["pubkey"] and posts[-1][1]["rotation"]["prev_pubkey"] == r1["pubkey"]
    assert r3["rotation_pending"] is False


def test_a_lost_acknowledgement_does_not_strand_the_server(client, monkeypatch):
    """R23, the directory probe: PREV was cleared only on rotated=true.
    A 200 whose body never said so (or was lost) left PREV=A; the next
    rotate then blessed C with A, which the directory no longer held,
    and every beacon 403'd until the operator forgot the server. Any
    2xx to a beacon signed by the current key clears PREV."""
    import time as _time
    from datetime import datetime, timezone
    from app import map_beacon as mb, db
    mb._reset_for_tests()
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 60_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:75", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp, "outdoor": {"tempf": 90}})
    posts = []

    async def terse_200(path, envelope):
        posts.append(envelope)
        return None, {}                  # a 200 that says nothing
    monkeypatch.setattr(mb, "_post", terse_200)
    client.put("/api/map", headers=H, json={"enabled": True})
    a = client.get("/api/map", headers=H).json()["pubkey"]
    r = client.post("/api/map/rotate", headers=H).json()
    assert r["published"]["ok"] and r["rotation_pending"] is False
    assert posts[-1]["rotation"]["prev_pubkey"] == a
    # The next rotation is blessed by B (on file), never by A.
    r2 = client.post("/api/map/rotate", headers=H).json()
    assert posts[-1]["rotation"]["prev_pubkey"] == r["pubkey"]
    assert posts[-1]["pubkey"] == r2["pubkey"]
    assert asyncio.run(db.get_kv(mb.PREV_KEY_KEY)) is None


def test_a_concurrent_first_mint_keeps_one_key(client):
    """R23: two first-enable paths (the GET's pubkey and the tick's
    beacon) minted two keys; whichever the directory pinned first, the
    other signed with a key it never held."""
    from app import map_beacon as mb, db
    mb._reset_for_tests()

    async def run():
        k1, k2 = await asyncio.gather(mb.ensure_key(), mb.ensure_key())
        stored = await mb.ensure_key()
        return {mb.public_key_b64(k) for k in (k1, k2, stored)}
    assert len(asyncio.run(run())) == 1


def test_verify_and_rotate_refuse_while_sharing_is_off(client, monkeypatch):
    """R23: both published while the switch was off (API only; the app
    hides them), which put a pin on the map the owner had said no to."""
    from app import map_beacon as mb
    posts = []

    async def fake_post(path, envelope):
        posts.append(path)
        return None, {}
    monkeypatch.setattr(mb, "_post", fake_post)
    assert client.get("/api/map", headers=H).json()["enabled"] is False
    before = client.get("/api/map", headers=H).json()["pubkey"]
    assert before
    r = client.post("/api/map/test", headers=H)
    assert r.status_code == 409 and "off" in r.json()["detail"]
    r = client.post("/api/map/rotate", headers=H)
    assert r.status_code == 409 and "off" in r.json()["detail"]
    assert posts == []
    assert client.get("/api/map", headers=H).json()["pubkey"] == before, "no key was rotated"


def test_overlapping_partial_updates_keep_every_field(client):
    """R23: PUT read the config, awaited the device list, then wrote the
    whole dict back — two overlapping partial saves lost a field."""
    from app import main as m, map_beacon as mb
    mb._reset_for_tests()

    async def run():
        await asyncio.gather(
            m.put_map_share(m.MapSharePut(name_visible=True)),
            m.put_map_share(m.MapSharePut(link_mode="direct")),
            m.put_map_share(m.MapSharePut(location_precision="city")))
        return await mb.get_config()
    cfg = asyncio.run(run())
    assert cfg["name_visible"] is True
    assert cfg["link_mode"] == "direct"
    assert cfg["location_precision"] == "city"


def test_a_withdrawal_the_directory_missed_is_retried_from_the_tick(client, monkeypatch):
    """R23 (item 15): switching off (or away from a station) while the
    directory was down left the old pin public for up to three hours, and
    nothing retried because the switch was already off. The withdrawal is
    queued, the save says so in a sentence, and the tick retries it."""
    import time as _time
    from datetime import datetime, timezone
    from app import map_beacon as mb, db
    mb._reset_for_tests()
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 60_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:76", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp, "outdoor": {"tempf": 90}})
    posts = []
    directory = {"down": False}

    async def fake_post(path, envelope):
        posts.append((path, envelope["beacon"].get("station_id")))
        return ("ConnectError", {}) if directory["down"] else (None, {"ok": True})
    monkeypatch.setattr(mb, "_post", fake_post)
    assert client.put("/api/map", headers=H, json={"enabled": True}).status_code == 200
    assert client.post("/api/map/test", headers=H).json()["ok"]
    sid = posts[-1][1]

    directory["down"] = True
    r = client.put("/api/map", headers=H, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert r.json()["withdraw_notice"] == mb.WITHDRAW_PENDING
    assert posts[-1] == ("/v1/withdraw", sid)
    pending = asyncio.run(mb.pending_withdrawals())
    assert [p["mac"] for p in pending] == ["AA:BB:CC:DD:EE:76"]
    # A normal save carries no notice.
    assert "withdraw_notice" not in client.put("/api/map", headers=H, json={"name_visible": True}).json()

    # The tick, with the switch OFF, retries; still down, still pending.
    async def tick(at):
        await mb.publish_if_due(await db.list_devices(), at)
        return await mb.pending_withdrawals()
    assert len(asyncio.run(tick(now + mb.INTERVAL_MS))) == 1
    assert posts[-1] == ("/v1/withdraw", sid)
    n = len(posts)
    # Inside the cadence: no retry storm.
    assert len(asyncio.run(tick(now + mb.INTERVAL_MS + 1000))) == 1 and len(posts) == n
    # Back up: the retry lands and the ledger clears.
    directory["down"] = False
    assert asyncio.run(tick(now + 2 * mb.INTERVAL_MS)) == []
    assert posts[-1] == ("/v1/withdraw", sid)
    assert [p for p, _ in posts].count("/v1/beacons") == 1, "off means no beacon"
    # An entry older than the beacon TTL is dropped: the pin is gone anyway.
    asyncio.run(mb._set_pending_withdrawals([{"mac": "AA:BB:CC:DD:EE:77", "since_ms": now - mb.TTL_MS - 1}]))
    directory["down"] = True
    assert asyncio.run(tick(now + 3 * mb.INTERVAL_MS)) == []
    assert posts[-1] == ("/v1/withdraw", sid), "nothing was sent for the expired one"


def test_precision_is_a_three_way_choice_and_the_old_flag_still_reads(client):
    from app import map_beacon as mb
    lat, lon = 33.3062, -111.8413            # not a grid centre
    exact = mb.place(lat, lon, "exact")
    area = mb.place(lat, lon, "area")
    city = mb.place(lat, lon, "city")
    assert exact == (33.3062, -111.8413)
    assert area == mb.fuzz(lat, lon, 0.5) and city == mb.fuzz(lat, lon, 10.0)
    # City is coarser than area: the snapped point sits farther away.
    d = lambda p: abs(p[0] - lat) + abs(p[1] - lon)
    assert d(city) > d(area) > 0
    # Precision comes from the new key; a pre-09-14 exact_location row still means exact.
    assert mb.precision_of({}) == "area"
    assert mb.precision_of({"exact_location": True}) == "exact"
    assert mb.precision_of({"location_precision": "city", "exact_location": True}) == "city"
    assert mb.precision_of({"location_precision": "bogus"}) == "area"
    b = mb.build(server_id="srv-1", station=_station(), cfg={"location_precision": "city"},
                 now_ms=1789200060000)
    assert b["precision"] == "city" and b["fuzzed"] is True and (b["lat"], b["lon"]) == mb.place(33.3062, -111.8412, "city")


def test_the_only_visit_link_is_the_servers_own_public_page(client, monkeypatch):
    """Volney 09-14: nobody types a URL. The link is the public page,
    offered only while that page is on, and dropped the moment it is
    off, whatever the owner chose earlier."""
    import asyncio
    from app import map_beacon as mb, db
    monkeypatch.setattr(mb, "public_page_url", lambda: "https://weather.example.com/")
    # Page off: the switch is reported off and no link is resolved.
    g = client.get("/api/map", headers=H).json()
    assert g["public_page_enabled"] is False and g["public_page_url"] is None
    assert "visit_url" not in g and "exact_location" not in g
    r = client.put("/api/map", headers=H, json={"visit_public_page": True}).json()
    assert r["visit_public_page"] is True
    assert asyncio.run(mb.resolved_visit_url({"visit_public_page": True})) is None
    # Page on (the app-stored flag): the link is the page, nothing else.
    asyncio.run(db.set_kv("public_dashboard.enabled", "1"))
    g = client.get("/api/map", headers=H).json()
    assert g["public_page_enabled"] is True and g["public_page_url"] == "https://weather.example.com/"
    assert asyncio.run(mb.resolved_visit_url({"visit_public_page": True})) == "https://weather.example.com/"
    assert asyncio.run(mb.resolved_visit_url({"visit_public_page": False})) is None
    # A typed link on the wire is ignored, and an old stored one is dropped.
    asyncio.run(mb.set_config({"visit_url": "https://spam.example", "enabled": False}))
    client.put("/api/map", headers=H, json={"name_visible": True})
    assert "visit_url" not in asyncio.run(mb.get_config())
    # An http-only server (no PUBLIC_BASE_URL, not on Fly) cannot offer the link.
    monkeypatch.setattr(mb, "public_page_url", lambda: None)
    g = client.get("/api/map", headers=H).json()
    assert g["public_page_enabled"] is True and g["public_page_url"] is None
    asyncio.run(db.set_kv("public_dashboard.enabled", "0"))


def test_public_page_url_comes_only_from_a_trusted_origin(client, monkeypatch):
    from app import map_beacon as mb, oauth
    monkeypatch.setattr(oauth, "_public_base_url_origin", lambda: None)
    monkeypatch.setattr(oauth, "_fly_host", lambda: None)
    assert mb.public_page_url() is None
    monkeypatch.setattr(oauth, "_fly_host", lambda: "zasder-weather.fly.dev")
    assert mb.public_page_url() == "https://zasder-weather.fly.dev/"
    monkeypatch.setattr(oauth, "_public_base_url_origin", lambda: "https://weather.zasder.com")
    assert mb.public_page_url() == "https://weather.zasder.com/"
    monkeypatch.setattr(oauth, "_public_base_url_origin", lambda: "http://weather.local")
    monkeypatch.setattr(oauth, "_fly_host", lambda: None)
    assert mb.public_page_url() is None, "the directory takes https only"


def test_changing_the_station_withdraws_the_one_it_leaves_behind(client, monkeypatch):
    """2026-09-14, found live: the directory keys beacons by station, and the
    switch only ever withdrew on OFF. Moving the share from station A to
    station B therefore ADDED B and left A on the map under its own pin
    until its three-hour TTL ran out — Volney's server showed two stations,
    and the one he had just switched away from was the one he saw."""
    import time as _time
    from datetime import datetime, timezone
    from app import map_beacon as mb
    mb._reset_for_tests()
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 300_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Two MACs that differ in MANY bits: a near-twin of a known MAC lands in
    # new-device probation and is quarantined with no reading at all.
    for mac, name in (("AA:BB:CC:DD:EE:81", "Davis"), ("11:22:33:44:55:66", "SDR")):
        client.post("/ingest/custom",
                    headers={"Authorization": "Bearer test-ingest-token",
                             "Content-Type": "application/json"},
                    json={"device": {"id": mac, "name": name,
                                     "coords": {"lat": 33.3, "lon": -111.9}},
                          "timestamp_utc": stamp,
                          "outdoor": {"tempf": 98}})
    posts = []

    async def fake_post(path, envelope):
        posts.append((path, envelope))
        return None, {}
    monkeypatch.setattr(mb, "_post", fake_post)

    client.put("/api/map", headers=H,
               json={"enabled": True, "mac": "AA:BB:CC:DD:EE:81"})
    first = client.post("/api/map/test", headers=H).json()
    assert first["ok"] is True, first
    a_id = first["station_id"]

    posts.clear()
    r = client.put("/api/map", headers=H, json={"mac": "11:22:33:44:55:66"})
    assert r.status_code == 200
    tombs = [e for p, e in posts if p == "/v1/withdraw"]
    assert len(tombs) == 1, [p for p, _ in posts]
    assert tombs[0]["beacon"]["station_id"] == a_id
    assert tombs[0]["beacon"]["withdraw"] is True

    # The new station publishes under its OWN id, and saving the same mac
    # again withdraws nothing (no tombstone for the station still shared).
    second = client.post("/api/map/test", headers=H).json()
    assert second["ok"] is True, second
    assert second["station_id"] != a_id
    posts.clear()
    client.put("/api/map", headers=H, json={"mac": "11:22:33:44:55:66"})
    assert [p for p, _ in posts if p == "/v1/withdraw"] == []

    # Turning it off still withdraws the station actually on the map.
    posts.clear()
    client.put("/api/map", headers=H, json={"enabled": False})
    tombs = [e for p, e in posts if p == "/v1/withdraw"]
    assert len(tombs) == 1
    assert tombs[0]["beacon"]["station_id"] == second["station_id"]


def test_the_directory_refusals_read_as_sentences_and_a_throttle_is_not_a_fault(client, monkeypatch):
    """The directory takes one beacon a minute. Saving and then tapping Send
    now inside the same minute used to paint
    `HTTP 429: {"detail":"one beacon a minute"}` as a warning, twice — the
    beacon on file was ours and the map was current, so there was nothing to
    fix and nothing to warn about (2026-09-14)."""
    import time as _time
    from datetime import datetime, timezone
    from app import map_beacon as mb
    mb._reset_for_tests()
    assert mb._explain(429, '{"detail":"one beacon a minute"}') == mb.THROTTLED
    assert mb._explain(429, '{"detail":"a server may list 16 stations"}') == \
        "a server may list 16 stations"
    assert "blocked" in mb._explain(403, '{"detail":"server blocked"}')
    assert "Rotate" in mb._explain(403, '{"detail":"key does not match the one on file"}')
    assert "Another server" in mb._explain(403, '{"detail":"station belongs to another server"}')
    assert "newer beacon" in mb._explain(409, '{"detail":"older than the beacon on file"}')
    # Anything the directory grows later is still diagnosable, not swallowed.
    assert mb._explain(500, "boom") == "HTTP 500: boom"
    assert mb._explain(400, "not json") == "HTTP 400: not json"

    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 300_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:73", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp,
                      "outdoor": {"tempf": 98}})
    client.put("/api/map", headers=H, json={"enabled": True})

    async def throttled(path, envelope):
        return mb.THROTTLED, {}
    monkeypatch.setattr(mb, "_post", throttled)
    r = client.post("/api/map/test", headers=H).json()
    assert r["ok"] is False and r["throttled"] is True
    assert r["error"] == mb.THROTTLED
    # The warning line the app paints is driven by last_error, and a throttle
    # leaves it alone.
    assert client.get("/api/map", headers=H).json()["last_error"] is None

    # A refusal that IS the owner's to fix still stamps.
    async def blocked(path, envelope):
        return mb._explain(403, '{"detail":"server blocked"}'), {}
    monkeypatch.setattr(mb, "_post", blocked)
    r = client.post("/api/map/test", headers=H).json()
    assert r["ok"] is False and r["throttled"] is False
    assert "blocked" in client.get("/api/map", headers=H).json()["last_error"]


def test_the_link_mode_decides_whether_the_beacon_names_this_server(client, monkeypatch):
    """2.3: a pin used to carry the server's own address, so one read of the
    directory handed a scraper every listed box's hostname. `id` hands the
    address to the directory privately and the pin carries a redirect."""
    import time as _time
    from datetime import datetime, timezone
    from app import map_beacon as mb, db
    mb._reset_for_tests()
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 300_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:74", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp,
                      "outdoor": {"tempf": 98}})
    monkeypatch.setattr(mb, "public_page_url", lambda: "https://zasder-weather.fly.dev/")

    async def page_on():
        return True
    monkeypatch.setattr(mb, "public_page_enabled", page_on)

    g = client.get("/api/map", headers=H).json()
    assert g["link_mode"] == "id", "hiding the address is the default"
    assert g["link_modes"] == ["direct", "id", "none"]
    assert client.put("/api/map", headers=H,
                      json={"link_mode": "proxy"}).status_code == 400

    posts = []

    async def fake_post(path, envelope):
        posts.append((path, envelope))
        return None, {}
    monkeypatch.setattr(mb, "_post", fake_post)
    client.put("/api/map", headers=H, json={"enabled": True, "visit_public_page": True})
    assert client.post("/api/map/test", headers=H).json()["ok"] is True
    beacon = posts[-1][1]["beacon"]
    # The address still goes to the directory — /s/<id> needs somewhere to
    # send people — and the mode tells it not to publish it.
    assert beacon["link_mode"] == "id"
    assert beacon["visit_url"] == "https://zasder-weather.fly.dev/"

    # Switching to direct says so on the wire.
    client.put("/api/map", headers=H, json={"link_mode": "direct"})
    assert client.post("/api/map/test", headers=H).json()["ok"] is True
    assert posts[-1][1]["beacon"]["link_mode"] == "direct"

    # The link switch off means `none`, whatever the mode says, and no
    # address rides along at all.
    client.put("/api/map", headers=H, json={"visit_public_page": False,
                                            "link_mode": "id"})
    assert client.post("/api/map/test", headers=H).json()["ok"] is True
    assert posts[-1][1]["beacon"]["link_mode"] == "none"
    assert "visit_url" not in posts[-1][1]["beacon"]

    # The id the directory assigns is remembered for the app to show.
    async def replying(path, envelope):
        return None, {"ok": True, "public_id": "AZ4K7P2Q",
                      "visit": "https://maps.zasder.com/s/AZ4K7P2Q"}
    monkeypatch.setattr(mb, "_post", replying)
    client.put("/api/map", headers=H, json={"visit_public_page": True, "link_mode": "id"})
    assert client.post("/api/map/test", headers=H).json()["ok"] is True
    g = client.get("/api/map", headers=H).json()
    assert g["public_id"] == "AZ4K7P2Q"
    assert g["public_visit_url"] == "https://maps.zasder.com/s/AZ4K7P2Q"


def test_the_region_prefix_comes_from_the_place_the_owner_typed(client):
    """The id is permanent once minted, so its two-letter prefix is read
    from the owner's own words rather than reverse-geocoded off coordinates
    that are deliberately fuzzed and would be wrong along every border."""
    import asyncio as _a
    from app import map_beacon as mb, db as _db
    mb._reset_for_tests()

    async def region_for(loc):
        await _db.set_kv("public_dashboard.location", loc)
        return await mb.region_hint()
    assert _a.run(region_for("Chandler, AZ")) == "AZ"
    assert _a.run(region_for("Irwin, PA")) == "PA"
    assert _a.run(region_for("  Mesa , az ")) == "AZ"
    assert _a.run(region_for("Chandler AZ")) == "AZ"
    # Nothing that reads like a region, no prefix — the id is all random.
    assert _a.run(region_for("Chandler")) is None
    assert _a.run(region_for("")) is None
    assert _a.run(region_for("Stoke-on-Trent")) is None


def test_a_redirect_is_not_an_acceptance(client, monkeypatch):
    """`_post` treated anything under 400 as success. httpx does not follow
    redirects, so a 301 from a directory URL that moved (or a captive
    portal) was stamped as a successful publish AND cleared the retiring
    key as if the directory had acknowledged the rotation. Only a 2xx is
    an acceptance; a 3xx is an error like any other non-2xx."""
    import time as _time
    from datetime import datetime, timezone
    import httpx
    from app import map_beacon as mb, db
    mb._reset_for_tests()
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 60_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:79", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp, "outdoor": {"tempf": 90}})
    real_post = mb._post

    async def down(path, envelope):
        return "ConnectError", {}
    monkeypatch.setattr(mb, "_post", down)
    client.put("/api/map", headers=H, json={"enabled": True})
    r = client.post("/api/map/rotate", headers=H).json()
    assert r["rotation_pending"] is True
    assert asyncio.run(db.get_kv(mb.PREV_KEY_KEY)), "the retiring key is on file"

    # The real _post, against a directory that answers 301.
    seen = []

    def bounce(request):
        seen.append(request.url.path)
        return httpx.Response(301, headers={"Location": "https://elsewhere.example/v1/beacons"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(mb.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(bounce), **kw))
    monkeypatch.setattr(mb, "_post", real_post)
    r = client.post("/api/map/test", headers=H).json()
    assert seen == ["/v1/beacons"]
    assert r["ok"] is False and r["throttled"] is False
    assert r["error"].startswith("HTTP 301")
    st = client.get("/api/map", headers=H).json()
    assert st["last_error"].startswith("HTTP 301")
    assert st["rotation_pending"] is True, "a bounce is not an acknowledgement"
    assert asyncio.run(db.get_kv(mb.PREV_KEY_KEY)), "the retiring key stays until a 2xx"

    # And a 2xx through the same path is what clears it.
    def accept(request):
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(mb.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(accept), **kw))
    mb._last_publish_ms.clear()
    r = client.post("/api/map/test", headers=H).json()
    assert r["ok"] is True
    assert client.get("/api/map", headers=H).json()["rotation_pending"] is False


def test_a_pending_withdrawal_is_visible_on_get(client, monkeypatch):
    """F06: PUT said "the old pin may still stand" once, in a sentence the
    app did not decode, and GET said nothing; reopening Settings lost the
    fact. GET carries the durable queue, and it empties when the
    directory finally takes the pin down."""
    import time as _time
    from datetime import datetime, timezone
    from app import map_beacon as mb
    mb._reset_for_tests()
    now = int(_time.time() * 1000)
    stamp = datetime.fromtimestamp((now - 60_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/ingest/custom",
                headers={"Authorization": "Bearer test-ingest-token",
                         "Content-Type": "application/json"},
                json={"device": {"id": "AA:BB:CC:DD:EE:81", "name": "Yard",
                                 "coords": {"lat": 33.3, "lon": -111.9}},
                      "timestamp_utc": stamp, "outdoor": {"tempf": 90}})
    directory = {"down": True}

    async def fake_post(path, envelope):
        return ("ConnectTimeout", {}) if directory["down"] else (None, {})
    monkeypatch.setattr(mb, "_post", fake_post)
    assert client.get("/api/map", headers=H).json()["pending_withdraw"] == []
    client.put("/api/map", headers=H, json={"enabled": True})
    r = client.put("/api/map", headers=H, json={"enabled": False}).json()
    assert r["withdraw_notice"] == mb.WITHDRAW_PENDING
    assert r["pending_withdraw"] == ["AA:BB:CC:DD:EE:81"]
    assert client.get("/api/map", headers=H).json()["pending_withdraw"] == ["AA:BB:CC:DD:EE:81"]
    directory["down"] = False
    asyncio.run(mb.retry_pending_withdrawals(now + 700_000))
    assert client.get("/api/map", headers=H).json()["pending_withdraw"] == []
