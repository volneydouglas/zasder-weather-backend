"""The station logbook (2.7, docs/2.7/LOGBOOK.md): chart markers.

Per-station notes with a time and an optional field, typed by a person or
written by the server beside a calibration or location change. Pinned
here: the CRUD and its refusals, the auth tiers (a read-only token reads
and never writes), the time window charts ask for, the automatic entries
and their wording (and NOT on a save that changed nothing), the CSV, the
database restore and device deletion, the migration of an older
database, and the public page's opt-in.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

import pytest

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

H = {"Authorization": "Bearer test-api-token"}
RO = {"Authorization": "Bearer test-reviewer-token"}
MAC = "AA:10:6B:00:00:01"
MAC2 = "AA:10:6B:00:00:02"
LAT, LON = 33.3, -111.8
T0 = 1_760_000_000_000          # 2025-10-09, a fixed past instant


def _device(mac: str = MAC, located: bool = True) -> None:
    from app import db
    info = {"coords": {"coords": {"lat": LAT, "lon": LON}}} if located else {}
    asyncio.run(db.upsert_device(mac, {"name": "Yard", "info": info}))


def _url(mac: str = MAC, entry_id: int | None = None) -> str:
    base = f"/api/devices/{mac}/logbook"
    return base if entry_id is None else f"{base}/{entry_id}"


def _add(client, note="Gauge cleaned", ts_ms=None, field=None, mac=MAC,
         headers=H):
    body: dict = {"note": note}
    if ts_ms is not None:
        body["ts_ms"] = ts_ms
    if field is not None:
        body["field"] = field
    return client.post(_url(mac), headers=headers, json=body)


def _entries(client, mac=MAC, **params):
    r = client.get(_url(mac), headers=H, params=params)
    assert r.status_code == 200, r.text
    return r.json()["entries"]


def _mint(client, write: bool) -> dict:
    r = client.post("/api/guest-tokens", headers=H,
                    json={"label": "Logbook Suite", "write": write})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ── CRUD ─────────────────────────────────────────────────────────────────

def test_create_list_edit_delete(client):
    _device()
    before = int(time.time() * 1000)
    r = _add(client, "  New   battery\tin the  sensor ")
    assert r.status_code == 200, r.text
    e = r.json()
    assert set(e) == {"id", "mac", "ts_ms", "note", "field", "source", "kind",
                      "detail", "created_ms", "updated_ms"}
    assert e["note"] == "New battery in the sensor"     # one line, trimmed
    assert e["source"] == "user" and e["kind"] is None and e["detail"] is None
    assert e["field"] is None and e["mac"] == MAC
    assert before <= e["ts_ms"] <= int(time.time() * 1000)   # default: now
    assert e["created_ms"] == e["updated_ms"]

    back = _add(client, "Moved the Tempest to the roof", ts_ms=T0,
                field="tempf").json()
    assert back["ts_ms"] == T0 and back["field"] == "tempf"

    listed = client.get(_url(), headers=H).json()
    assert listed["mac"] == MAC
    assert listed["note_max"] == 280 and listed["entries_max"] == 2000
    # Oldest first: the backdated one leads.
    assert [x["id"] for x in listed["entries"]] == [back["id"], e["id"]]

    # A lowercase, compact MAC reaches the same station.
    assert len(_entries(client, mac="aa106b000001")) == 2

    r = client.patch(_url(entry_id=back["id"]), headers=H,
                     json={"note": "Moved it to the roof", "ts_ms": T0 + 60_000})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["note"] == "Moved it to the roof" and p["ts_ms"] == T0 + 60_000
    assert p["field"] == "tempf", "a key not sent must not change"
    assert p["updated_ms"] >= p["created_ms"]
    # null clears the field; so does "".
    assert client.patch(_url(entry_id=back["id"]), headers=H,
                        json={"field": None}).json()["field"] is None
    assert client.patch(_url(entry_id=back["id"]), headers=H,
                        json={"field": "dailyrainin"}).json()["field"] == "dailyrainin"
    assert client.patch(_url(entry_id=back["id"]), headers=H,
                        json={"field": ""}).json()["field"] is None

    r = client.delete(_url(entry_id=e["id"]), headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True, "deleted": e["id"]}
    assert client.delete(_url(entry_id=e["id"]), headers=H).status_code == 404
    assert [x["id"] for x in _entries(client)] == [back["id"]]


def test_validation(client):
    _device()
    _device(MAC2)
    assert _add(client, "x" * 280).status_code == 200
    r = _add(client, "x" * 281)
    assert r.status_code == 400 and "280" in r.json()["detail"]
    assert _add(client, "   \n\t ").status_code == 400
    assert _add(client, "\x00\x01").status_code == 400
    assert client.post(_url(), headers=H, json={}).status_code == 422
    assert client.post(_url(), headers=H, json={"note": 5}).status_code == 422
    # Fields: the stored readings and the correctable ones; nothing else.
    assert _add(client, field="dailyrainin").status_code == 200
    assert _add(client, field="rainratein").status_code == 200
    r = _add(client, field="nonsense")
    assert r.status_code == 400 and "nonsense" in r.json()["detail"]
    assert _add(client, field="tempF").status_code == 400
    # Time: SECONDS sent where ms belong are refused, and so is the future.
    assert _add(client, ts_ms=1_760_000_000).status_code == 400
    assert _add(client, ts_ms=int(time.time() * 1000) + 3 * 86_400_000
                ).status_code == 400
    assert _add(client, ts_ms=int(time.time() * 1000) + 3_600_000
                ).status_code == 200        # a phone's clock running ahead
    # Unknown station: 404 on every route.
    other = "AA:10:6B:00:00:99"
    assert _add(client, mac=other).status_code == 404
    assert client.get(_url(other), headers=H).status_code == 404
    assert client.get(f"/api/devices/{other}/logbook.csv", headers=H).status_code == 404
    assert client.patch(_url(other, 1), headers=H, json={"note": "x"}).status_code == 404
    assert client.delete(_url(other, 1), headers=H).status_code == 404
    # An entry is reached only through its own station.
    e = _add(client).json()
    assert client.patch(_url(MAC2, e["id"]), headers=H,
                        json={"note": "x"}).status_code == 404
    assert client.delete(_url(MAC2, e["id"]), headers=H).status_code == 404
    assert client.patch(_url(MAC, 999_999), headers=H,
                        json={"note": "x"}).status_code == 404
    # Edits: nothing sent, a cleared note or time, bad values.
    assert client.patch(_url(entry_id=e["id"]), headers=H, json={}).status_code == 400
    assert client.patch(_url(entry_id=e["id"]), headers=H,
                        json={"note": None}).status_code == 400
    assert client.patch(_url(entry_id=e["id"]), headers=H,
                        json={"ts_ms": None}).status_code == 400
    assert client.patch(_url(entry_id=e["id"]), headers=H,
                        json={"note": "y" * 281}).status_code == 400
    assert client.patch(_url(entry_id=e["id"]), headers=H,
                        json={"field": "bogus"}).status_code == 400
    assert client.patch(_url(entry_id=e["id"]), headers=H,
                        json={"ts_ms": 5}).status_code == 400
    # A window that ends before it starts.
    assert client.get(_url(), headers=H, params={"start_ms": 10, "end_ms": 5}
                      ).status_code == 400


def test_a_full_logbook_refuses_more(client, monkeypatch):
    from app import logbook
    monkeypatch.setattr(logbook, "ENTRIES_MAX", 2)
    _device()
    assert _add(client).status_code == 200
    assert _add(client).status_code == 200
    r = _add(client)
    assert r.status_code == 409 and "full" in r.json()["detail"]
    # An automatic entry past the cap is skipped; the change still lands.
    r = client.put(f"/api/devices/{MAC}/calibration", headers=H,
                   json={"calibration": {"tempf": -1.0}})
    assert r.status_code == 200 and r.json()["calibration"] == {"tempf": -1.0}
    assert len(_entries(client)) == 2


def test_two_creates_at_the_cap_cannot_both_land(client, monkeypatch):
    """The count and the insert are one write transaction (CodeRabbit,
    PR #97). Two creates racing for the last slot: one 200, one 409. The
    widened gap after the count is what made both see room before."""
    import httpx
    from app import logbook
    monkeypatch.setattr(logbook, "ENTRIES_MAX", 2)
    _device()
    assert _add(client).status_code == 200
    real_count = logbook._count

    async def slow_count(conn, mac):
        n = await real_count(conn, mac)
        await asyncio.sleep(0.2)          # the other create runs here
        return n
    monkeypatch.setattr(logbook, "_count", slow_count)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://test") as c:
            return await asyncio.gather(*(
                c.post(_url(), headers=H, json={"note": f"racer {i}"})
                for i in range(2)))
    codes = sorted(r.status_code for r in asyncio.run(go()))
    assert codes == [200, 409], codes
    monkeypatch.setattr(logbook, "_count", real_count)
    assert len(_entries(client)) == 2


def test_an_automatic_entry_needs_the_callers_write_transaction(client):
    """insert_auto counts against the cap on the caller's connection; that
    is only race-free while the caller's write holds the writer, so a call
    outside a transaction is refused rather than racing."""
    from app import db, logbook
    _device()

    async def go():
        async with db.connect() as conn:
            await logbook.insert_auto(conn, MAC, [
                {"note": "n", "kind": "location", "field": None,
                 "detail": None}], T0)
    with pytest.raises(RuntimeError, match="write transaction"):
        asyncio.run(go())
    assert _entries(client) == []


# ── auth tiers ───────────────────────────────────────────────────────────

def test_read_only_tokens_read_and_never_write(client):
    _device()
    e = _add(client).json()
    guest = _mint(client, write=False)
    for ro in (RO, guest):
        assert client.get(_url(), headers=ro).status_code == 200
        assert client.get(f"/api/devices/{MAC}/logbook.csv",
                          headers=ro).status_code == 200
        r = _add(client, headers=ro)
        assert r.status_code == 403 and "read-only" in r.json()["detail"]
        assert client.patch(_url(entry_id=e["id"]), headers=ro,
                            json={"note": "changed"}).status_code == 403
        assert client.delete(_url(entry_id=e["id"]), headers=ro).status_code == 403
    # Nothing a read-only token sent landed.
    [only] = _entries(client)
    assert only["note"] == "Gauge cleaned"
    # No token, or a wrong one: 401 on reads and writes alike.
    bad = {"Authorization": "Bearer nope"}
    for h in ({}, bad):
        assert client.get(_url(), headers=h).status_code == 401
        assert client.post(_url(), headers=h, json={"note": "x"}).status_code == 401


def test_a_write_share_link_writes_and_is_audited(client):
    _device()
    w = _mint(client, write=True)
    r = _add(client, "Cleaned the funnel", headers=w)
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    assert client.patch(_url(entry_id=eid), headers=w,
                        json={"note": "Cleaned the funnel again"}).status_code == 200
    assert client.delete(_url(entry_id=eid), headers=w).status_code == 200
    audit = client.get("/api/write-audit", headers=H).json()["entries"]
    seen = {(a["method"], a["path"]) for a in audit}
    assert ("POST", f"/api/devices/{MAC}/logbook") in seen
    assert ("PATCH", f"/api/devices/{MAC}/logbook/{eid}") in seen
    assert ("DELETE", f"/api/devices/{MAC}/logbook/{eid}") in seen


# ── the chart window ─────────────────────────────────────────────────────

def test_the_window_is_inclusive_at_both_ends(client):
    _device()
    for k in range(5):
        assert _add(client, f"n{k}", ts_ms=T0 + k * 1000).status_code == 200
    notes = lambda **p: [e["note"] for e in _entries(client, **p)]  # noqa: E731
    assert notes() == ["n0", "n1", "n2", "n3", "n4"]
    assert notes(start_ms=T0 + 1000, end_ms=T0 + 3000) == ["n1", "n2", "n3"]
    assert notes(start_ms=T0 + 3000) == ["n3", "n4"]
    assert notes(end_ms=T0 + 1000) == ["n0", "n1"]
    assert notes(start_ms=T0 + 2000, end_ms=T0 + 2000) == ["n2"]
    assert notes(start_ms=T0 + 10_000) == []
    # Another station's entries never ride along.
    _device(MAC2)
    _add(client, "elsewhere", ts_ms=T0 + 2000, mac=MAC2)
    assert notes(start_ms=T0 + 2000, end_ms=T0 + 2000) == ["n2"]


# ── automatic entries ────────────────────────────────────────────────────

def _cal(client, table: dict, mac=MAC):
    r = client.put(f"/api/devices/{mac}/calibration", headers=H,
                   json={"calibration": table})
    assert r.status_code == 200, r.text


def _auto(client, mac=MAC):
    return [e for e in _entries(client, mac=mac) if e["source"] == "auto"]


def test_a_calibration_change_is_logged_in_plain_words(client):
    _device()
    before = int(time.time() * 1000)
    _cal(client, {"tempf": -1.2})
    [e] = _auto(client)
    assert e["note"] == "Temperature offset changed from 0 to -1.2 °F"
    assert e["field"] == "tempf" and e["kind"] == "calibration"
    assert e["detail"] == {"field": "tempf", "correction": "offset",
                           "from": 0.0, "to": -1.2, "unit": "°F"}
    assert e["ts_ms"] >= before

    # Two fields in one save: one entry each, the unchanged one silent.
    _cal(client, {"tempf": -1.2, "humidity": 3, "rainratein": 1.1})
    new = _auto(client)[1:]
    assert sorted(x["note"] for x in new) == [
        "Humidity offset changed from 0 to 3%",
        "Rain rate scale changed from 1 to 1.1"]
    rate = next(x for x in new if x["field"] == "rainratein")
    assert rate["detail"] == {"field": "rainratein", "correction": "scale",
                              "from": 1.0, "to": 1.1, "unit": None}

    # Changed, then removed: the neutral value is the other end.
    _cal(client, {"tempf": -1.0, "humidity": 3, "rainratein": 1.1})
    assert _auto(client)[-1]["note"] == \
        "Temperature offset changed from -1.2 to -1 °F"
    _cal(client, {"humidity": 3, "rainratein": 1.1})
    assert _auto(client)[-1]["note"] == \
        "Temperature offset changed from -1 to 0 °F"
    assert len(_auto(client)) == 5


def test_a_save_that_changes_nothing_logs_nothing(client):
    _device()
    _cal(client, {"tempf": -1.2, "baromrelin": 0.05})
    assert len(_auto(client)) == 2
    for _ in range(3):
        _cal(client, {"baromrelin": 0.05, "tempf": -1.2})
    # A neutral correction is no correction: storing nothing for it, and
    # saying nothing about it.
    _cal(client, {"tempf": -1.2, "baromrelin": 0.05, "uv": 1.0, "tempinf": 0})
    assert len(_auto(client)) == 2
    # Clearing an already-empty table on another station: nothing.
    _device(MAC2)
    _cal(client, {}, mac=MAC2)
    assert _auto(client, mac=MAC2) == []
    # A refused save logs nothing either.
    r = client.put(f"/api/devices/{MAC}/calibration", headers=H,
                   json={"calibration": {"tempf": 400}})
    assert r.status_code == 400
    assert len(_auto(client)) == 2


def test_a_config_restore_that_changes_a_correction_is_logged(client):
    from app import config_backup
    _device()
    asyncio.run(config_backup.import_config(
        {"version": 1, "device_calibrations": {MAC: {"tempf": 0.5}}}))
    [e] = _auto(client)
    assert e["note"] == "Temperature offset changed from 0 to 0.5 °F"


def test_a_config_restore_reaches_a_cached_public_history_page(client, monkeypatch):
    """POST /api/config/restore can log a correction; with the public
    logbook on, a warm /weather/history cache must not hide that entry for
    ten minutes (CodeRabbit, PR #97: the route never bumped the
    publish_policy generation)."""
    from datetime import datetime, timezone
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    ih = {"Authorization": "Bearer test-ingest-token"}
    now = int(time.time() * 1000)
    for h in range(0, 48, 3):
        stamp = datetime.fromtimestamp((now - h * 3_600_000) / 1000, timezone.utc
                                       ).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert client.post("/ingest/custom", headers=ih, json={
            "device": {"id": MAC, "name": "Yard"}, "timestamp_utc": stamp,
            "outdoor": {"tempf": 70 + h % 5, "humidity": 30},
            "pressure": {"relative_inhg": 29.9}}).status_code == 200
    assert client.put("/api/public-dashboard", headers=H,
                      json={"logbook": True}).status_code == 200
    page = client.get("/weather/history").text            # warm the cache
    assert "Temperature offset" not in page
    r = client.post("/api/config/restore", headers=H, json={
        "version": 1, "device_calibrations": {MAC: {"tempf": 0.5}}})
    assert r.status_code == 200, r.text
    assert r.json()["restored"]["device_calibrations"] == 1
    page = client.get("/weather/history").text
    assert "Temperature offset changed from 0 to 0.5" in page


def test_a_location_change_is_logged_and_a_resave_is_not(client):
    _device()                         # posted with coordinates
    loc = f"/api/devices/{MAC}/location"
    # The same point the station already had: not a move.
    assert client.put(loc, headers=H, json={"lat": LAT, "lon": LON}
                      ).status_code == 200
    assert _auto(client) == []
    assert client.put(loc, headers=H, json={"lat": 33.31, "lon": LON}
                      ).status_code == 200
    [e] = _auto(client)
    assert e["note"] == "Station location changed"
    assert e["kind"] == "location" and e["field"] is None and e["detail"] is None
    # No coordinates in the entry: it may be shown on the public page.
    assert "33.31" not in str(e) and "111.8" not in str(e)
    # Re-saving the point, or renaming the place, is not a change.
    client.put(loc, headers=H, json={"lat": 33.31, "lon": LON})
    client.put(loc, headers=H, json={"lat": 33.31, "lon": LON, "label": "Roof"})
    assert len(_auto(client)) == 1
    client.put(loc, headers=H, json={"lat": 33.31, "lon": -111.81})
    assert len(_auto(client)) == 2

    # A station that never had a point: "set", once.
    _device(MAC2, located=False)
    loc2 = f"/api/devices/{MAC2}/location"
    client.put(loc2, headers=H, json={"lat": 40.0, "lon": -100.0})
    client.put(loc2, headers=H, json={"lat": 40.0, "lon": -100.0})
    assert [e["note"] for e in _auto(client, MAC2)] == ["Station location set"]

    # A MAC that never posted gets no logbook row (nothing to attach to).
    client.put("/api/devices/AA:10:6B:00:00:77/location", headers=H,
               json={"lat": 1.0, "lon": 1.0})
    from app import db

    async def orphans():
        async with db.connect() as conn:
            return (await (await conn.execute(
                "SELECT COUNT(*) FROM logbook WHERE mac = ?",
                ("AA:10:6B:00:00:77",))).fetchone())[0]
    assert asyncio.run(orphans()) == 0


def test_an_automatic_entry_can_be_deleted_but_not_edited(client):
    _device()
    _cal(client, {"tempf": 1.0})
    [e] = _auto(client)
    r = client.patch(_url(entry_id=e["id"]), headers=H, json={"note": "mine now"})
    assert r.status_code == 409
    assert _auto(client)[0]["note"] == e["note"]
    assert client.delete(_url(entry_id=e["id"]), headers=H).status_code == 200
    assert _auto(client) == []


def test_the_wording_unit_by_unit():
    from app import logbook as lb
    notes = {e["field"]: e["note"] for e in lb.calibration_entries(
        {"baromrelin": 0.02, "soiltemp3f": 1.5},
        {"baromrelin": -0.04, "soilhum2": -4, "windgustmph": 0.95,
         "humidityin": 2.25})}
    assert notes == {
        "baromrelin": "Relative pressure offset changed from 0.02 to -0.04 inHg",
        "soiltemp3f": "Soil temperature 3 offset changed from 1.5 to 0 °F",
        "soilhum2": "Soil moisture 2 offset changed from 0 to -4%",
        "windgustmph": "Wind gust scale changed from 1 to 0.95",
        "humidityin": "Indoor humidity offset changed from 0 to 2.25%",
    }
    # Float noise never reaches a person, and a no-op is no entry.
    [e] = lb.calibration_entries({}, {"tempf": 0.1 + 0.2})
    assert e["note"] == "Temperature offset changed from 0 to 0.3 °F"
    assert lb.calibration_entries({"tempf": -1.2}, {"tempf": -1.2}) == []
    assert lb.location_entry((1.0, 2.0), (1.0 + 1e-8, 2.0)) is None
    assert lb.location_entry(None, (1.0, 2.0))["note"] == "Station location set"
    # No em-dash anywhere a person reads (the public voice rule).
    for e in lb.calibration_entries({}, {f: 1.1 for f in lb._CAL_LABELS}):
        assert "—" not in e["note"]


# ── CSV ──────────────────────────────────────────────────────────────────

def test_the_csv(client):
    _device()
    _add(client, "Gauge cleaned", ts_ms=T0, field="dailyrainin")
    _add(client, "=HYPERLINK(\"x\")", ts_ms=T0 + 60_000)
    _add(client, 'Said "hello", then left', ts_ms=T0 + 120_000)
    _add(client, "later", ts_ms=T0 + 86_400_000)
    _cal(client, {"tempf": -1.2})
    r = client.get(f"/api/devices/{MAC}/logbook.csv", headers=H,
                   params={"start_ms": T0, "end_ms": T0 + 120_000})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == \
        'attachment; filename="logbook-AA106B000001.csv"'
    lines = r.text.splitlines()
    assert lines == [
        "timestamp_utc,ts_ms,source,kind,field,note",
        f"2025-10-09T08:53:20Z,{T0},user,,dailyrainin,Gauge cleaned",
        f"2025-10-09T08:54:20Z,{T0 + 60_000},user,,,\"'=HYPERLINK(\"\"x\"\")\"",
        f"2025-10-09T08:55:20Z,{T0 + 120_000},user,,,\"Said \"\"hello\"\", then left\"",
    ]
    whole = client.get(f"/api/devices/{MAC}/logbook.csv", headers=H).text
    assert whole.splitlines()[-1].endswith(
        ",auto,calibration,tempf,Temperature offset changed from 0 to -1.2 °F")
    assert len(whole.splitlines()) == 6


# ── restore, deletion, migration ─────────────────────────────────────────

def test_deleting_a_device_deletes_its_logbook(client):
    _device()
    _device(MAC2)
    _add(client)
    _cal(client, {"tempf": 1.0})
    _add(client, mac=MAC2)
    r = client.delete(f"/api/devices/{MAC}", headers=H)
    assert r.status_code == 200 and r.json()["logbook"] == 2
    _device()                         # the MAC comes back: a clean logbook
    assert _entries(client) == []
    assert len(_entries(client, mac=MAC2)) == 1


def _restore(client, snap: Path) -> dict:
    import hashlib
    body = snap.read_bytes()
    c = client.post("/api/backup/database/restore/challenge", headers=H).json()
    r = client.post("/api/backup/database/restore", content=body, headers={
        **H, "X-Restore-Challenge": c["challenge"],
        "X-Restore-SHA256": hashlib.sha256(body).hexdigest(),
        "Content-Type": "application/vnd.sqlite3"})
    assert r.status_code == 200, r.text
    deadline = time.time() + 20
    while time.time() < deadline:
        st = client.get("/api/backup/database/restore/status", headers=H).json()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError("restore never finished")


def _snapshot(db_path: str, dest: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()


def _reset_restore_state():
    from app import restore
    restore.JOB.clear()
    restore.JOB["state"] = "idle"
    restore._CHALLENGE = None
    restore._TASK = None


def test_a_restore_brings_its_own_logbook(client, temp_env):
    _reset_restore_state()
    from app import db
    _device()
    asyncio.run(db.insert_observations(MAC, [{"dateutc": T0, "tempf": 70.0}]))
    kept = _add(client, "In the backup", ts_ms=T0).json()
    snap = Path(temp_env).parent / "saved.db"
    _snapshot(temp_env, snap)
    _add(client, "After the backup", ts_ms=T0 + 1000)
    st = _restore(client, snap)
    assert st["state"] == "done", st
    assert [e["note"] for e in _entries(client)] == ["In the backup"]
    # The restored rows are live: editable by their old ids.
    assert client.patch(_url(entry_id=kept["id"]), headers=H,
                        json={"note": "edited"}).status_code == 200
    _reset_restore_state()


def test_a_restore_of_a_26_backup_has_an_empty_logbook(client, temp_env):
    """A backup taken before 2.7 has no logbook table. It restores, the
    migration creates the table, and the live entries do not survive
    (they were not in the database that was put back)."""
    _reset_restore_state()
    from app import db
    _device()
    asyncio.run(db.insert_observations(MAC, [{"dateutc": T0, "tempf": 70.0}]))
    snap = Path(temp_env).parent / "old.db"
    _snapshot(temp_env, snap)
    con = sqlite3.connect(snap)
    con.execute("DROP TABLE logbook")
    con.commit()
    con.close()
    _add(client, "Only in the live database")
    st = _restore(client, snap)
    assert st["state"] == "done", st
    assert _entries(client) == []
    assert _add(client, "Works after the restore").status_code == 200
    _reset_restore_state()


def test_an_older_database_gains_the_table_and_keeps_its_data(temp_env):
    """The 2.6 schema has no logbook. A database at that schema (built by
    init_db then losing the table and its index, the shape every upgraded
    box has) migrates on boot with its readings intact and takes entries."""
    import importlib
    import app.config
    import app.db
    importlib.reload(app.config)
    importlib.reload(app.db)
    from app import db as fresh_db, logbook
    path = app.config.settings.database_path

    asyncio.run(fresh_db.init_db())
    asyncio.run(fresh_db.upsert_device(MAC, {"name": "Old", "info": {}}))
    asyncio.run(fresh_db.insert_observations(MAC, [{"dateutc": T0, "tempf": 70.0}]))
    con = sqlite3.connect(path)
    con.execute("DROP TABLE logbook")
    con.commit()
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name IN "
                           "('logbook', 'idx_logbook_mac_ts')").fetchone()
    con.close()

    asyncio.run(fresh_db.init_db())
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(logbook)")]
    idx = con.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                      "AND name='idx_logbook_mac_ts'").fetchone()
    n_obs = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    con.close()
    assert cols == ["id", "mac", "ts_ms", "note", "field", "source", "kind",
                    "detail_json", "created_ms", "updated_ms"]
    assert idx is not None and n_obs == 1
    e = asyncio.run(logbook.create(MAC, "after the upgrade", T0, None))
    assert asyncio.run(logbook.list_entries(MAC)) == [e]
    # A calibration save on the upgraded database logs as well.
    assert asyncio.run(fresh_db.set_calibration(MAC, {"tempf": 2.0}))
    assert asyncio.run(logbook.list_entries(MAC))[-1]["source"] == "auto"


# ── the public page (opt-in) ─────────────────────────────────────────────

def test_the_public_logbook_is_off_by_default_and_owner_switched(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_dashboard", True)
    ih = {"Authorization": "Bearer test-ingest-token"}
    now = int(time.time() * 1000)
    from datetime import datetime, timezone
    for h in range(0, 48, 3):
        stamp = datetime.fromtimestamp((now - h * 3_600_000) / 1000, timezone.utc
                                       ).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert client.post("/ingest/custom", headers=ih, json={
            "device": {"id": MAC, "name": "Yard"}, "timestamp_utc": stamp,
            "outdoor": {"tempf": 70 + h % 5, "humidity": 30},
            "pressure": {"relative_inhg": 29.9}}).status_code == 200
    e = _add(client, "Gauge <cleaned>", ts_ms=now - 5 * 3_600_000).json()

    cfg = client.get("/api/public-dashboard", headers=H).json()
    assert cfg["logbook"] is False
    page = client.get("/weather/history").text
    assert "Gauge" not in page and "chart-mark" not in page
    assert "Station log" not in page

    # Owner-only, like every public-page switch.
    assert client.put("/api/public-dashboard", headers=RO,
                      json={"logbook": True}).status_code == 403
    r = client.put("/api/public-dashboard", headers=H, json={"logbook": True})
    assert r.status_code == 200 and r.json()["logbook"] is True
    page = client.get("/weather/history").text
    assert "Station log" in page and "chart-mark" in page
    assert "Gauge &lt;cleaned&gt;" in page and "<cleaned>" not in page

    # A deleted note leaves the cached page at once.
    assert client.delete(_url(entry_id=e["id"]), headers=H).status_code == 200
    page = client.get("/weather/history").text
    assert "Gauge" not in page and "Station log" not in page

    assert client.put("/api/public-dashboard", headers=H,
                      json={"logbook": False}).json()["logbook"] is False
    _add(client, "Back to private", ts_ms=now - 3_600_000)
    assert "Back to private" not in client.get("/weather/history").text


@pytest.mark.parametrize("path", ["/api/devices/{m}/logbook",
                                  "/api/devices/{m}/logbook.csv"])
def test_reads_are_token_gated(client, path):
    _device()
    assert client.get(path.format(m=MAC)).status_code == 401


def test_the_response_has_exactly_the_contracts_keys(client):
    """The wire shape the apps decode is pinned in one file,
    tests/fixtures/logbook_example.json (the example in
    docs/2.7/LOGBOOK.md). A renamed or added key fails here before it is
    a silent nil on a phone."""
    import json
    want = json.loads((Path(__file__).parent / "fixtures"
                       / "logbook_example.json").read_text())
    _device()
    _add(client, "Cleaned the rain gauge funnel", ts_ms=T0, field="dailyrainin")
    _cal(client, {"tempf": -1.2})
    client.put(f"/api/devices/{MAC}/location", headers=H,
               json={"lat": 33.31, "lon": LON})
    body = client.get(_url(), headers=H).json()
    assert set(body) == set(want)
    got = {e["kind"]: e for e in body["entries"]}
    for w in want["entries"]:
        g = got[w["kind"]]
        assert set(g) == set(w), w["kind"]
        assert type(g["detail"]) is type(w["detail"]), w["kind"]
        if w["detail"] is not None:
            assert set(g["detail"]) == set(w["detail"])
            assert g["note"] == w["note"] and g["detail"] == w["detail"]
    # Every created entry, and an edit, answer with the same entry shape.
    e = _add(client, "x").json()
    assert set(e) == set(want["entries"][0])
    p = client.patch(_url(entry_id=e["id"]), headers=H, json={"note": "y"}).json()
    assert set(p) == set(want["entries"][0])
