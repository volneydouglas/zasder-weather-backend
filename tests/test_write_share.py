"""Write-share links (1.9): the second tier's lifecycle — mint (label
mandatory, zww_ prefix), the audit trail, last-used, revocation taking
effect immediately, and the read tier staying read-only. The route-level
tier boundary itself is pinned in test_security_invariants."""
from __future__ import annotations

import os

os.environ.setdefault("API_TOKEN", "test-api-token")

H = {"Authorization": "Bearer test-api-token"}


def _mint(client, label="Volney's Mom", write=True):
    r = client.post("/api/guest-tokens", headers=H,
                    json={"label": label, "write": write})
    assert r.status_code == 200
    return r.json()


def test_mint_write_link_requires_a_label(client):
    r = client.post("/api/guest-tokens", headers=H, json={"write": True})
    assert r.status_code == 400
    assert "name" in r.json()["detail"]
    r = client.post("/api/guest-tokens", headers=H,
                    json={"label": "   ", "write": True})
    assert r.status_code == 400


def test_mint_shapes_and_listing(client):
    w = _mint(client)
    assert w["token"].startswith("zww_") and w["can_write"] is True
    g = _mint(client, label=None, write=False)
    assert g["token"].startswith("zwg_") and g["can_write"] is False

    rows = client.get("/api/guest-tokens", headers=H).json()["tokens"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[w["id"]]["can_write"] is True
    assert by_id[g["id"]]["can_write"] is False
    # The list never carries token values (established rule, re-pinned for
    # the tier where a leak would be a WRITE credential).
    assert w["token"] not in str(rows)


def test_write_share_lifecycle_with_audit(client):
    w = _mint(client, label="Backyard Crew")
    hw = {"Authorization": f"Bearer {w['token']}"}

    # Read works (the tier includes read)…
    assert client.get("/api/devices", headers=hw).status_code == 200
    # …a station-ops write works…
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/location", headers=hw,
                   json={"name": "Renamed by the crew"})
    assert r.status_code not in (401, 403)
    # …and an owner route refuses with the tailored 403.
    r = client.post("/api/guest-tokens", headers=hw,
                    json={"label": "escalation", "write": True})
    assert r.status_code == 403

    # The write landed in the audit trail with attribution.
    entries = client.get("/api/write-audit", headers=H).json()["entries"]
    assert entries, "no audit rows recorded"
    e = entries[0]
    assert e["label"] == "Backyard Crew"
    assert e["method"] == "PUT"
    assert e["path"] == "/api/devices/AA:BB:CC:DD:EE:FF/location"
    assert e["token_tail"] == w["token"][-6:]
    assert w["token"] not in str(entries)      # never the credential

    # The audit view itself is owner-only.
    assert client.get("/api/write-audit", headers=hw).status_code == 403

    # Last-used stamps on the share list.
    rows = client.get("/api/guest-tokens", headers=H).json()["tokens"]
    mine = next(r for r in rows if r["id"] == w["id"])
    assert mine["last_used_ms"] is not None

    # Revocation is immediate — the cache refresh is part of delete.
    assert client.delete(f"/api/guest-tokens/{w['id']}",
                         headers=H).status_code == 200
    assert client.put("/api/devices/AA:BB:CC:DD:EE:FF/location", headers=hw,
                      json={"name": "x"}).status_code == 401
    assert client.get("/api/devices", headers=hw).status_code == 401


def test_read_link_stays_read_only(client):
    g = _mint(client, label="Read Only Aunt", write=False)
    hg = {"Authorization": f"Bearer {g['token']}"}
    assert client.get("/api/devices", headers=hg).status_code == 200
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/location", headers=hg,
                   json={"name": "nope"})
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]


def test_write_share_is_still_a_limited_read(client):
    """The tier widens WRITES, never the operator view: /api/alerts must
    keep hiding the SMTP identity from a write-share holder exactly as it
    does from read guests."""
    w = _mint(client, label="Limited Check")
    hw = {"Authorization": f"Bearer {w['token']}"}
    body = client.get("/api/alerts", headers=hw).json()
    for k in ("smtp_host", "smtp_username", "smtp_from"):
        assert body.get(k) in (None, "", "(hidden)"), (
            f"{k} leaked to a write-share token: {body.get(k)!r}")


def test_wu_station_credential_fields_are_owner_only(client):
    """R12 W1: /wu-station is on the write-share tier for the STATION-ID
    mapping only. upload_key/upload_enabled are credentials — a zww_ holder
    who could set them would redirect the owner's live WU feed to their own
    station, and the redirect would survive revoking the share."""
    import asyncio
    import time
    from app import db
    asyncio.run(db.upsert_device(
        "AA:BB:CC:DD:EE:FF",
        {"lastData": {"dateutc": int(time.time() * 1000), "tempf": 90.0}}))
    w = _mint(client, label="Crew")
    hw = {"Authorization": f"Bearer {w['token']}"}

    # Credential fields refuse for the write share — singly and together.
    for body in ({"upload_key": "SECRETKEY1"},
                 {"upload_enabled": True},
                 {"wu_station_id": "ATTACKER1", "upload_key": "K",
                  "upload_enabled": True}):
        r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/wu-station",
                       headers=hw, json=body)
        assert r.status_code == 403, body
        assert "owner-only" in r.json()["detail"]
    # R13 X2: the 403 must leave NO trace — a refactor moving the guard
    # after the write would pass the status assertions while persisting
    # the attacker's station ID.
    assert asyncio.run(db.get_wu_station("AA:BB:CC:DD:EE:FF")) is None

    # The station-ID mapping alone still works for the tier…
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/wu-station",
                   headers=hw, json={"wu_station_id": "KAZCHAND802"})
    assert r.status_code == 200
    assert r.json()["wu_station_id"] == "KAZCHAND802"
    assert r.json()["upload_key_set"] is False
    # …including clearing it while it is a PURE mapping (no credential).
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/wu-station",
                   headers=hw, json={"wu_station_id": ""})
    assert r.status_code == 200
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/wu-station",
                   headers=hw, json={"wu_station_id": "KAZCHAND802"})
    assert r.status_code == 200

    # …and the owner keeps full access to every field.
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/wu-station",
                   headers=H, json={"upload_key": "OWNERKEY01",
                                    "upload_enabled": True})
    assert r.status_code == 200
    assert r.json()["upload_key_set"] is True

    # R13 X1: once the owner's credential is on the row, a shared writer
    # clearing the mapping would cascade-delete the write-only key —
    # refused, row intact; the owner can still clear.
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/wu-station",
                   headers=hw, json={"wu_station_id": ""})
    assert r.status_code == 403
    assert "owner" in r.json()["detail"]
    row = asyncio.run(db.get_wu_station("AA:BB:CC:DD:EE:FF"))
    assert row and row["upload_key"] == "OWNERKEY01"
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/wu-station",
                   headers=H, json={"wu_station_id": ""})
    assert r.status_code == 200
    assert asyncio.run(db.get_wu_station("AA:BB:CC:DD:EE:FF")) is None


def test_rename_updates_audit_attribution_immediately(client):
    """R12 W3: audit labels are captured from the in-process cache at auth
    time, so a rename must refresh that cache — otherwise every later write
    audits under the OLD name until an unrelated mint/revoke or a restart."""
    w = _mint(client, label="Original Name")
    hw = {"Authorization": f"Bearer {w['token']}"}

    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/location", headers=hw,
                   json={"name": "first write"})
    assert r.status_code not in (401, 403)

    r = client.patch(f"/api/guest-tokens/{w['id']}", headers=H,
                     json={"label": "Renamed Person"})
    assert r.status_code == 200

    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/location", headers=hw,
                   json={"name": "second write"})
    assert r.status_code not in (401, 403)

    rows = client.get("/api/write-audit", headers=H).json()["entries"]
    labels = [e["label"] for e in rows[:2]]
    assert labels[0] == "Renamed Person", \
        "post-rename write still attributed to the old label"
    assert labels[1] == "Original Name"


def test_in_place_upgrade_to_write_is_refused(client):
    """Pinning the implicit rule (R12): a read link can never be widened in
    place. PATCH with write:true must leave can_write false — upgrading
    means minting a NEW zww_ link, deliberately."""
    g = _mint(client, label="Read Friend", write=False)
    r = client.patch(f"/api/guest-tokens/{g['id']}", headers=H,
                     json={"label": "Read Friend", "write": True})
    # Whether the field is rejected or ignored, the tier must not widen —
    # but a 5xx would leave can_write false for the wrong reason and pass
    # vacuously (CodeRabbit): the handler must ANSWER, not crash.
    assert r.status_code in (200, 400, 422), r.status_code
    rows = client.get("/api/guest-tokens", headers=H).json()["tokens"]
    me = next(t for t in rows if t["id"] == g["id"])
    assert me["can_write"] is False, \
        "PATCH widened a read link to the write tier in place"
    hg = {"Authorization": f"Bearer {g['token']}"}
    r = client.put("/api/devices/AA:BB:CC:DD:EE:FF/location", headers=hg,
                   json={"name": "nope"})
    assert r.status_code == 403


# ── R14: the audit buffer itself ────────────────────────────────────────

def test_audit_buffer_cap_drops_oldest_loudly(client, caplog):
    """A scripted write flood must bound memory at _AUDIT_BUFFER_MAX by
    dropping the OLDEST rows, and say so in the log."""
    from app import db as d
    for i in range(d._AUDIT_BUFFER_MAX + 50):
        d.record_write_audit(f"zww_{i:040d}", "PUT", f"/api/x/{i}")
    with d._WRITE_AUDIT_LOCK:
        assert len(d._WRITE_AUDIT_PENDING) == d._AUDIT_BUFFER_MAX
        # The survivors are the NEWEST 200.
        assert d._WRITE_AUDIT_PENDING[0][4] == "/api/x/50"
        assert d._WRITE_AUDIT_PENDING[-1][4] == "/api/x/249"
        d._WRITE_AUDIT_PENDING.clear()
    assert any("overflow" in r.message for r in caplog.records)


def test_audit_flush_failure_requeues_instead_of_losing_rows(client,
                                                             monkeypatch):
    """R14: a transient DB error mid-flush must put the drained rows back
    (front, original order) — losing them silently defeats an attribution
    log. The next flush persists them."""
    import asyncio

    import pytest

    from app import db as d

    d.record_write_audit("zww_" + "a" * 40, "PUT", "/api/first")
    d.record_write_audit("zww_" + "b" * 40, "PUT", "/api/second")

    def broken_connect():
        raise RuntimeError("disk went away")
    real_connect = d.connect
    monkeypatch.setattr(d, "connect", broken_connect)
    with pytest.raises(RuntimeError):
        asyncio.run(d.flush_write_audit())
    with d._WRITE_AUDIT_LOCK:
        paths = [row[4] for row in d._WRITE_AUDIT_PENDING]
    assert paths == ["/api/first", "/api/second"], \
        "drained rows lost or reordered on flush failure"

    monkeypatch.setattr(d, "connect", real_connect)
    rows = asyncio.run(d.list_write_audit())
    assert [r["path"] for r in rows[:2]] == ["/api/second", "/api/first"]


def test_audit_list_orders_by_timestamp_not_rowid(client):
    """R14: two concurrent flushes can persist rows with ids inverted
    against their timestamps — newest-first must follow ts_ms."""
    import asyncio
    from app import db as d

    async def seed():
        async with d.connect() as con:
            # Lower id carries the NEWER timestamp.
            await con.execute(
                "INSERT INTO write_audit (ts_ms, token_tail, label, method,"
                " path) VALUES (2000, 'aaaaaa', 'A', 'PUT', '/newer')")
            await con.execute(
                "INSERT INTO write_audit (ts_ms, token_tail, label, method,"
                " path) VALUES (1000, 'bbbbbb', 'B', 'PUT', '/older')")
            await con.commit()
        return await d.list_write_audit()

    rows = asyncio.run(seed())
    assert [r["path"] for r in rows] == ["/newer", "/older"]


def test_clear_refusal_covers_the_enabled_only_row(client):
    """R14 X1 variant: the credential guard must also fire when the row
    carries ONLY upload_enabled (owner toggled upload on before pasting a
    key) — the guard reads either credential field, not just the key."""
    import asyncio
    import time

    from app import db
    asyncio.run(db.upsert_device(
        "AA:BB:CC:DD:EE:F1",
        {"lastData": {"dateutc": int(time.time() * 1000), "tempf": 90.0}}))
    w = _mint(client, label="Crew2")
    hw = {"Authorization": f"Bearer {w['token']}"}

    r = client.put("/api/devices/AA:BB:CC:DD:EE:F1/wu-station",
                   headers=H, json={"wu_station_id": "KAZOWNER01",
                                    "upload_enabled": True})
    assert r.status_code == 200 and r.json()["upload_key_set"] is False

    r = client.put("/api/devices/AA:BB:CC:DD:EE:F1/wu-station",
                   headers=hw, json={"wu_station_id": ""})
    assert r.status_code == 403
    row = asyncio.run(db.get_wu_station("AA:BB:CC:DD:EE:F1"))
    assert row and row["station_id"] == "KAZOWNER01"


def test_owner_only_gets_refuse_the_write_share(client):
    """R12 sub-ledger: the write tier reads station data, never the
    owner's administrative surfaces — every owner-only GET answers the
    tailored 403, not 200 and not 401."""
    w = _mint(client, label="Nosy Crew")
    hw = {"Authorization": f"Bearer {w['token']}"}
    for path in ("/api/write-audit", "/api/guest-tokens",
                 "/api/ingest-tokens", "/api/webhooks",
                 "/api/integrations", "/api/sharing",
                 "/api/history-retention"):
        r = client.get(path, headers=hw)
        assert r.status_code == 403, (path, r.status_code)


def test_audit_flush_midloop_failure_rolls_back_and_requeues(client,
                                                             monkeypatch):
    """R15: a failure AFTER some INSERTs must leave no partial rows (the
    commit is the last statement) and re-queue everything — otherwise the
    re-queued rows duplicate on the next flush."""
    import asyncio

    import pytest

    from app import db as d

    d.record_write_audit("zww_" + "c" * 40, "PUT", "/api/one")
    d.record_write_audit("zww_" + "d" * 40, "PUT", "/api/two")

    real_connect = d.connect

    class _BombOnPrune:
        def __init__(self):
            self._cm = real_connect()

        async def __aenter__(self):
            self._con = await self._cm.__aenter__()
            return _Proxy(self._con)

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    class _Proxy:
        def __init__(self, con):
            self._con = con

        async def execute(self, sql, *a):
            if sql.strip().startswith("DELETE FROM write_audit"):
                raise RuntimeError("disk died mid-flush")
            return await self._con.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._con, name)

    monkeypatch.setattr(d, "connect", _BombOnPrune)
    with pytest.raises(RuntimeError):
        asyncio.run(d.flush_write_audit())
    with d._WRITE_AUDIT_LOCK:
        assert [r[4] for r in d._WRITE_AUDIT_PENDING] == ["/api/one",
                                                          "/api/two"]

    monkeypatch.setattr(d, "connect", real_connect)

    async def persisted():
        rows = await d.list_write_audit()
        return [r["path"] for r in rows]

    paths = asyncio.run(persisted())
    # Exactly once each: the aborted flush's INSERTs rolled back, so the
    # successful retry did not duplicate them.
    assert paths.count("/api/one") == 1
    assert paths.count("/api/two") == 1


def test_audit_list_serves_persisted_rows_while_flush_still_fails(
        client, monkeypatch):
    """R16 test 5: the owner endpoint must serve what is on disk even
    while the flush keeps failing — the earlier test restored the real
    connect before listing, which never exercised the catch."""
    import asyncio

    from app import db as d

    # One row safely persisted first.
    d.record_write_audit("zww_" + "e" * 40, "PUT", "/api/persisted")
    asyncio.run(d.flush_write_audit())
    # A pending row, and a connection that bombs INSERTs but serves reads.
    d.record_write_audit("zww_" + "f" * 40, "PUT", "/api/stuck")

    real_connect = d.connect

    class _ReadOnly:
        def __init__(self):
            self._cm = real_connect()

        async def __aenter__(self):
            self._con = await self._cm.__aenter__()
            proxy = self

            class _P:
                async def execute(self, sql, *a):
                    if sql.strip().startswith("INSERT INTO write_audit"):
                        raise RuntimeError("disk still down")
                    return await proxy._con.execute(sql, *a)

                def __getattr__(self, name):
                    return getattr(proxy._con, name)
            return _P()

        async def __aexit__(self, *exc):
            return await self._cm.__aexit__(*exc)

    monkeypatch.setattr(d, "connect", _ReadOnly)
    rows = asyncio.run(d.list_write_audit())
    paths = [r["path"] for r in rows]
    assert "/api/persisted" in paths, "disk rows must still serve"
    assert "/api/stuck" not in paths
    with d._WRITE_AUDIT_LOCK:
        pending = [r[4] for r in d._WRITE_AUDIT_PENDING]
    assert pending == ["/api/stuck"], "the pending row stays queued"
