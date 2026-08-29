"""R8 review batch: classifier alignment, relay widget quota, CWOP connect
path, webhook PATCH, Windy clear, humidity omission, poller isolation, and
the MCP tool contract (SDK-free via a stubbed server class)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")

H = {"Authorization": "Bearer test-api-token"}


def _ingest(client, mac, name, tempf=90.0):
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": mac, "name": name},
              "timestamp_utc": "2026-06-01T12:00:00Z",
              "outdoor": {"tempf": tempf}})


# ─────────────────────────── classifier ───────────────────────────

def test_air_monitor_device_classifier():
    from app.db import is_air_monitor_device

    assert is_air_monitor_device({"mac": "5D:5D:07:02:EC:4A"})
    assert is_air_monitor_device(
        {"mac": "AA:00", "lastData": {"pm25": 2.0, "co2": 400}})
    # Pressure guard (S4): a station whose wind array died is NOT a monitor.
    assert not is_air_monitor_device(
        {"mac": "AA:01", "lastData": {"pm25": 2.0, "baromrelin": 29.9}})
    # A full weather station with a PM add-on stays a station.
    assert not is_air_monitor_device(
        {"mac": "AA:02", "lastData": {"pm25": 2.0, "windspeedmph": 4.0,
                                      "dailyrainin": 0.0}})
    assert not is_air_monitor_device({"mac": "AA:03", "lastData": {"tempf": 90}})
    assert not is_air_monitor_device({"mac": "AA:04"})


# ─────────────────────────── CWOP connect path ───────────────────────────

def test_cwop_connect_falls_through_dead_addresses(monkeypatch):
    from app import share_targets as st

    attempts = []

    async def fake_resolve(host, port):
        return {"cwop.aprs.net": ["10.0.0.1", "10.0.0.2"],
                "rotate.aprs2.net": ["10.0.1.1"]}[host]

    async def fake_open(ip, port, timeout=4.0):
        attempts.append(ip)
        if ip == "10.0.0.2":
            return ("reader", "writer")
        raise TimeoutError(f"dead {ip}")
    monkeypatch.setattr(st, "_resolve", fake_resolve)
    monkeypatch.setattr(st, "_open", fake_open)

    r, w = asyncio.run(st._cwop_connect())
    assert (r, w) == ("reader", "writer")
    assert attempts == ["10.0.0.1", "10.0.0.2"], "first dead IP must not end it"


def test_cwop_connect_all_dead_raises_last_error(monkeypatch):
    from app import share_targets as st
    import pytest

    async def fake_resolve(host, port):
        return ["10.9.9.9"]

    async def fake_open(ip, port, timeout=4.0):
        raise TimeoutError("nope")
    monkeypatch.setattr(st, "_resolve", fake_resolve)
    monkeypatch.setattr(st, "_open", fake_open)
    with pytest.raises(TimeoutError):
        asyncio.run(st._cwop_connect())


def test_cwop_humidity_out_of_range_is_omitted():
    from app.share_targets import cwop_packet
    from datetime import datetime, timezone
    now = datetime(2026, 8, 25, 21, 30, tzinfo=timezone.utc)
    import re
    low = cwop_packet("GW7475", 33.3, -111.9, {"tempf": 70.0,
                                               "humidity": -3.0}, now)
    high = cwop_packet("GW7475", 33.3, -111.9, {"tempf": 70.0,
                                                "humidity": 104.0}, now)
    sat = cwop_packet("GW7475", 33.3, -111.9, {"tempf": 70.0,
                                               "humidity": 100.0}, now)
    assert re.search(r"h\d\d", low.partition("_")[2]) is None
    assert re.search(r"h\d\d", high.partition("_")[2]) is None
    assert "h00" in sat, "true saturation still encodes"


# ─────────────────────────── webhook PATCH ───────────────────────────

def test_webhook_pause_and_resume(client, monkeypatch):
    from app import webhooks as wh
    monkeypatch.setattr(wh, "validate_webhook_url", lambda url: None)
    made = client.post("/api/webhooks", headers=H,
                       json={"url": "https://hooks.example.com/z"}).json()
    hid = made["id"]

    r = client.patch(f"/api/webhooks/{hid}", headers=H,
                     json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    listed = client.get("/api/webhooks", headers=H).json()["webhooks"]
    assert listed[0]["enabled"] in (False, 0)

    assert client.patch(f"/api/webhooks/{hid}", headers=H,
                        json={"enabled": True}).status_code == 200
    assert client.patch("/api/webhooks/99999", headers=H,
                        json={"enabled": False}).status_code == 404

    # Paused hooks receive nothing.
    client.patch(f"/api/webhooks/{hid}", headers=H, json={"enabled": False})
    sent = []

    async def fake_send(hook, payload):
        sent.append(1)
    monkeypatch.setattr(wh, "_send", fake_send)
    asyncio.run(wh.dispatch_alert("rule", None, "t", "b", 1))
    assert sent == []


# ─────────────────────────── Windy clear ───────────────────────────

def test_windy_station_clears_with_minus_one(client):
    r = client.put("/api/sharing/windy", headers=H,
                   json={"api_key": "k" * 12, "station": 2})
    assert r.status_code == 200
    from app import share_targets as st
    cfg = asyncio.run(st.get_config("windy"))
    assert cfg.get("station") == 2
    r = client.put("/api/sharing/windy", headers=H, json={"station": -1})
    assert r.status_code == 200
    cfg = asyncio.run(st.get_config("windy"))
    assert "station" not in cfg or cfg.get("station") in (None, "")


# ─────────────────────── AirGradient poller hardening ───────────────────────

def test_airgradient_rh_overshoot_clamps_to_100():
    from app.airgradient_poller import build_payload
    from datetime import datetime, timezone
    loc = {"locationId": 5, "locationType": "outdoor",
           "timestamp": datetime.now(timezone.utc).isoformat(),
           "atmp_corrected": 20.0, "rhum_corrected": 103.4, "rco2": 400}
    p = build_payload(loc)
    assert p["outdoor"]["humidity"] == 100


def test_airgradient_one_bad_location_does_not_kill_siblings(client, monkeypatch):
    from app import airgradient_poller as agp
    from app import source_status
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    good = {"locationId": 7, "locationType": "outdoor",
            "timestamp": now_iso, "atmp": 20.0, "rco2": 400}
    bad = dict(good, locationId=8)

    stored = []

    async def fake_ingest(payload):
        if payload["device"]["id"].endswith("08"):
            raise RuntimeError("boom")
        stored.append(payload["device"]["id"])
        return {"inserted": 1}
    monkeypatch.setattr(agp.ingest, "_do_ingest", fake_ingest)

    class OneShotClient:
        def __init__(self):
            self.calls = 0
        async def measures_current(self):
            self.calls += 1
            return [bad, good]

    successes = []
    monkeypatch.setattr(source_status, "record_success",
                        lambda name, rows=None: successes.append(rows))

    async def run():
        p = agp.AirGradientPoller(OneShotClient(), interval_s=30)
        await p.start()
        await asyncio.sleep(0.05)
        await p.stop()
    asyncio.run(run())
    assert stored == ["5D5D070007"[:6] + "000007"] or stored, stored
    assert stored and stored[0].endswith("07")
    assert successes and successes[0] == 1, \
        "fetch succeeded — source records success with the stored count"


# ─────────────────────────── MCP tool contract ───────────────────────────

def _load_mcp_module(monkeypatch):
    """Import mcp/zasder_mcp.py without the MCP SDK: stub the server class
    (both generations) so @tool() is a passthrough, then check every tool's
    HTTP path against the real FastAPI route table."""
    class StubServer:
        def __init__(self, *a, **kw): ...
        def tool(self, *a, **kw):
            def deco(fn):
                return fn
            return deco
        def run(self): ...

    pkg = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    mcpserver = types.ModuleType("mcp.server.mcpserver")
    mcpserver.MCPServer = StubServer
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = StubServer
    monkeypatch.setitem(sys.modules, "mcp", pkg)
    monkeypatch.setitem(sys.modules, "mcp.server", server)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", mcpserver)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
    monkeypatch.setenv("ZASDER_URL", "https://example.invalid")
    monkeypatch.setenv("ZASDER_TOKEN", "t")

    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "mcp" / "zasder_mcp.py"
    spec = importlib.util.spec_from_file_location("zasder_mcp_under_test",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mcp_tools_hit_real_routes_with_valid_params(client, monkeypatch):
    mod = _load_mcp_module(monkeypatch)
    calls = []

    def fake_get(path, params=None, timeout=20.0):
        calls.append((path, params))
        # Shape-faithful minimal payloads per endpoint.
        if path.endswith("/records"):
            return {"mac": "AA", "periods": {"today": {}, "month": {},
                                             "year": {}, "all": {}}}
        if path == "/api/devices":
            return []
        return {}
    monkeypatch.setattr(mod, "_get", fake_get)

    mod.list_stations()
    mod.current_conditions("AA:BB")
    mod.derived_metrics("AA:BB")
    mod.history_summary("AA:BB", field="tempf", hours=744)  # clamps to 720
    out = mod.records("AA:BB", period="today")
    mod.recent_alerts(limit=500)                             # clamps to 100

    # Every called path must exist in the real route table.
    from app.main import app as fastapi_app
    routes = [getattr(r, "path", "") for r in fastapi_app.routes]

    def matches(path):
        parts = path.split("/")
        for rp in routes:
            rps = rp.split("/")
            if len(rps) == len(parts) and all(
                    b.startswith("{") or a == b
                    for a, b in zip(parts, rps)):
                return True
        return False
    for path, _ in calls:
        assert matches(path), f"MCP calls a nonexistent route: {path}"

    by_path = dict(calls)
    assert by_path["/api/devices/AA%3ABB/summary"]["hours"] == 720
    assert by_path["/api/alerts/recent"]["limit"] == 100
    # records: server payload projected client-side to the asked period.
    assert out["period"] == "today"

    import pytest
    with pytest.raises(ValueError):
        mod.records("AA", period="bogus")


# ───────────────────────── R9 batch ─────────────────────────

def test_push_unregister_removes_token_and_la_tokens(client):
    tok = "cd" * 32
    client.post("/api/push/register", headers=H,
                json={"token": tok, "platform": "ios"})
    la = "ef" * 16
    client.post("/api/push/live-activity-token", headers=H,
                json={"token": la, "kind": "start", "activity": "storm"})
    r = client.post("/api/push/unregister", headers=H,
                    json={"token": tok, "live_activity_tokens": [la]})
    body = r.json()
    assert r.status_code == 200 and body["removed"] is True
    assert body["live_activity_removed"] == 1
    # Idempotent: unknown tokens still answer ok.
    r = client.post("/api/push/unregister", headers=H,
                    json={"token": tok, "live_activity_tokens": [la]})
    body = r.json()
    assert body["removed"] is False and body["live_activity_removed"] == 0

    from app import db
    toks = asyncio.run(db.list_push_tokens())
    assert all(t["token"] != tok for t in toks)
    rows = asyncio.run(db.list_live_activity_tokens("start", activity="storm"))
    assert all(t["token"] != la for t in rows)


def test_nws_legacy_keys_retired_after_seed(client, monkeypatch):
    """R9 T6: the first global-scheme run persists the merged seed and
    DELETES the per-station keys — a quiet server must not re-read them
    every tick forever."""
    from app import nws_watch as nw, db

    async def fake_fetch(lat, lon):
        return []
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    monkeypatch.setattr(nw, "_last_poll_ms", {})

    dev = [{"mac": "AA:20", "name": "D",
            "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]

    async def run():
        await db.set_kv("nws_watch.seen.AA:20", json.dumps(["urn:z:1"]))
        from app.alerts import effective_config
        cfg = await effective_config()
        await nw.check(cfg, dev, int(time.time() * 1000),
                       lambda *a, **kw: None)
        return (await db.get_kv(nw._SEEN_KEY),
                await db.get_kv("nws_watch.seen.AA:20"))
    global_raw, legacy = asyncio.run(run())
    assert global_raw is not None and "urn:z:1" in global_raw
    assert legacy is None, "legacy key must be deleted after seeding"


def test_nws_legacy_sweep_covers_deleted_devices(client, monkeypatch):
    """R10 U4: keys for since-DELETED devices are swept too, and the
    GLOBAL key survives the prefix sweep."""
    from app import nws_watch as nw, db

    async def fake_fetch(lat, lon):
        return []
    monkeypatch.setattr(nw, "_fetch_active", fake_fetch)
    monkeypatch.setattr(nw, "_last_poll_ms", {})
    dev = [{"mac": "AA:21", "name": "D",
            "info": {"coords": {"coords": {"lat": 33.3, "lon": -111.9}}}}]

    async def run():
        await db.set_kv("nws_watch.seen.GH:OS:TS", json.dumps(["urn:g:1"]))
        from app.alerts import effective_config
        cfg = await effective_config()
        await nw.check(cfg, dev, int(time.time() * 1000),
                       lambda *a, **kw: None)
        return (await db.get_kv("nws_watch.seen.GH:OS:TS"),
                await db.get_kv(nw._SEEN_KEY))
    ghost, global_raw = asyncio.run(run())
    assert ghost is None, "deleted-device key must be swept"
    assert global_raw is not None, "the global key must survive the sweep"


def test_public_dashboard_excludes_monitors_even_when_kv_names_them(client):
    """R8 S5 + the unconditional filter: a kv list naming ONLY monitor
    macs must fall back to a weather station, and 'all' must not render
    monitor blocks."""
    _ingest(client, "AA:BB:CC:00:00:90", "Realstation", tempf=90.0)
    client.post("/ingest/custom",
        headers={"Authorization": "Bearer test-ingest-token",
                 "Content-Type": "application/json"},
        json={"device": {"id": "5D5D07000042", "name": "Airbox"},
              "timestamp_utc": "2026-06-01T12:00:00Z",
              "air": {"pm25": 2.0, "co2": 400}})
    client.put("/api/public-dashboard", headers=H,
               json={"enabled": True, "macs": "5D5D07000042"})
    page = client.get("/embed").text
    assert "Airbox" not in page, "monitor block must not render"
    assert "Realstation" in page, "fallback must pick the weather station"
    client.put("/api/public-dashboard", headers=H,
               json={"enabled": True, "macs": "all"})
    # bust the cache via the config PUT; fetch again
    page = client.get("/embed").text
    assert "Airbox" not in page and "Realstation" in page


def test_webhook_task_reaper_consumes_exceptions(client):
    """R8 S7: the done-callback must consume the task exception (no
    'exception was never retrieved') and drop the strong ref."""
    import app.alerts as al

    async def run():
        async def boom():
            raise RuntimeError("dispatch failed")
        task = asyncio.get_running_loop().create_task(boom())
        al._WEBHOOK_TASKS.add(task)
        task.add_done_callback(al._reap_webhook_task)
        await asyncio.sleep(0.02)
        return task
    task = asyncio.run(run())
    assert task not in al._WEBHOOK_TASKS
    # exception() after the reaper ran: already consumed, not raising into
    # the "never retrieved" warning path.
    assert isinstance(task.exception(), RuntimeError)


def test_chart_index_rebuild_defers_on_big_archives(client, monkeypatch):
    """v1.8.1: the idx_obs_chart rebuild must not run inline at boot on a
    large archive — the 1.8.0 upgrade crash-looped a 1.15M-row box when
    the CREATE INDEX outlived the platform health-check window. Above the
    threshold init_db only FLAGS the rebuild; rebuild_chart_index() then
    restores the covering index with the full column set."""
    import asyncio
    from app import db

    async def run():
        # Sabotage: drop a column from the index so the probe sees stale.
        async with db.connect() as conn:
            await conn.execute("DROP INDEX idx_obs_chart")
            await conn.execute(
                "CREATE INDEX idx_obs_chart ON observations (mac, dateutc_ms)")
            await conn.commit()
        monkeypatch.setattr(db, "_CHART_INDEX_INLINE_MAX_ROWS", -1)
        db._CHART_INDEX_REBUILD_NEEDED = False
        await db.init_db()
        deferred = db.chart_index_rebuild_needed()
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_obs_chart'"
            )).fetchone()
        still_stale = not set(db._CHART_INDEX_COLS) <= db._index_columns(row[0])
        await db.rebuild_chart_index()
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_obs_chart'"
            )).fetchone()
        rebuilt = set(db._CHART_INDEX_COLS) <= db._index_columns(row[0])
        return deferred, still_stale, rebuilt, db.chart_index_rebuild_needed()
    deferred, still_stale, rebuilt, flag_after = asyncio.run(run())
    assert deferred, "big archive must defer, not rebuild inline"
    assert still_stale, "boot must NOT have rebuilt inline"
    assert rebuilt, "the background rebuild restores the covering index"
    assert flag_after is False


def test_chart_index_missing_entirely_is_rebuild_worthy(client, monkeypatch):
    """R11 V3: a kill between the deferred rebuild's DROP and CREATE leaves
    NO index at all. The probe must treat missing as rebuild-worthy — the
    old `row and row[0]` guard skipped it, and the SCHEMA script's
    unconditional CREATE then rebuilt it INLINE during executescript,
    resurrecting the 1.8.0 boot crash-loop on big archives. Small archives
    rebuild inline; big ones defer with the index still absent."""
    import asyncio
    from app import db

    async def run():
        # Simulate the interrupted deferred rebuild: index gone entirely.
        async with db.connect() as conn:
            await conn.execute("DROP INDEX idx_obs_chart")
            await conn.commit()
        # Small archive: init_db recreates it inline, full column set.
        db._CHART_INDEX_REBUILD_NEEDED = False
        await db.init_db()
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_obs_chart'"
            )).fetchone()
        inline_ok = (row is not None
                     and set(db._CHART_INDEX_COLS) <= db._index_columns(row[0]))
        inline_flag = db.chart_index_rebuild_needed()

        # Big archive: missing index defers; boot must NOT build it inline.
        async with db.connect() as conn:
            await conn.execute("DROP INDEX idx_obs_chart")
            await conn.commit()
        monkeypatch.setattr(db, "_CHART_INDEX_INLINE_MAX_ROWS", -1)
        db._CHART_INDEX_REBUILD_NEEDED = False
        await db.init_db()
        deferred = db.chart_index_rebuild_needed()
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_obs_chart'"
            )).fetchone()
        still_missing = row is None
        await db.rebuild_chart_index()
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_obs_chart'"
            )).fetchone()
        rebuilt = (row is not None
                   and set(db._CHART_INDEX_COLS) <= db._index_columns(row[0]))
        return inline_ok, inline_flag, deferred, still_missing, rebuilt

    inline_ok, inline_flag, deferred, still_missing, rebuilt = asyncio.run(run())
    assert inline_ok, "small archive: missing index must be recreated inline"
    assert inline_flag is False
    assert deferred, "big archive: missing index must defer, not build inline"
    assert still_missing, "boot must not have built the index inline"
    assert rebuilt
