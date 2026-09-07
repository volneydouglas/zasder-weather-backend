"""Server recommendations (2.1): the advice document, the price math and
the apply route, all against a fake Machines API. Nothing here reaches
Fly; the identity comes from monkeypatched env vars."""
import asyncio
import json

import pytest


class _Lazy:
    """`from app import machine_advice` at module top would build Settings
    before the temp_env fixture sets the credential env vars (collection
    fails on a checkout without a .env). Resolve the module on first touch;
    setattr/delattr forward too so monkeypatch works through the proxy."""

    def _mod(self):
        from app import machine_advice
        return machine_advice

    def __getattr__(self, name):
        return getattr(self._mod(), name)

    def __setattr__(self, name, value):
        setattr(self._mod(), name, value)

    def __delattr__(self, name):
        delattr(self._mod(), name)


MA = _Lazy()

H = {"Authorization": "Bearer test-api-token"}
APP, MACHINE, VOL = "zasder-weather", "e784e5f1234567", "vol_abc123"
GIB = 1024 ** 3


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeFly:
    """Scripted Machines API: records every call, answers from state."""

    def __init__(self, memory_mb=1024, size_gb=3, events=None,
                 fail_read=False):
        self.memory_mb, self.size_gb = memory_mb, size_gb
        self.events = events or []
        self.fail_read = fail_read
        self.calls: list[tuple] = []

    def _machine(self):
        return {"id": MACHINE, "config": {"image": "ghcr.io/x:2.1.0",
                                         "env": {"A": "1"},
                                         "guest": {"cpu_kind": "shared",
                                                   "cpus": 1,
                                                   "memory_mb": self.memory_mb}},
                "events": self.events}

    async def get(self, url, headers=None):
        self.calls.append(("GET", url))
        if self.fail_read:
            return _Resp(500, {"error": "boom"})
        if url.endswith("/volumes"):
            return _Resp(200, [{"id": VOL, "name": "data",
                                "size_gb": self.size_gb,
                                "attached_machine_id": MACHINE,
                                "region": "phx"},
                               {"id": "vol_other", "size_gb": 1,
                                "attached_machine_id": "someone-else"}])
        return _Resp(200, self._machine())

    async def put(self, url, headers=None, json=None):
        self.calls.append(("PUT", url, json))
        self.size_gb = json["size_gb"]
        return _Resp(200, {"id": VOL, "size_gb": self.size_gb,
                           "needs_restart": False})

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        self.memory_mb = json["config"]["guest"]["memory_mb"]
        assert json["config"]["env"] == {"A": "1"}, "config must be posted whole"
        return _Resp(200, self._machine())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fly(fake: FakeFly):
    return MA.FlyMachines(APP, MACHINE, "fm2_deploytoken", client_factory=lambda: fake)


def _patch_fly(monkeypatch, fake: FakeFly) -> None:
    """Route the module's own FlyMachines(...) construction at the fake.
    The real class is captured first: the lambda must not call the
    patched name it is replacing."""
    real = MA.FlyMachines
    monkeypatch.setattr(MA, "FlyMachines",
                        lambda *a, **k: real(APP, MACHINE, "fm2_deploytoken",
                                             client_factory=lambda: fake))


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("FLY_APP_NAME", APP)
    monkeypatch.setenv("FLY_MACHINE_ID", MACHINE)
    monkeypatch.setenv("FLY_API_TOKEN", "fm2_deploytoken")
    from app.config import settings
    monkeypatch.setattr(settings, "server_advice", True)
    MA.reset_cache()
    yield
    MA.reset_cache()


def _disk(used_pct, free_gb, total_gb):
    return {"total_bytes": int(total_gb * GIB), "free_bytes": int(free_gb * GIB),
            "used_pct": used_pct}


# ── availability ───────────────────────────────────────────────────────

def test_a_box_without_a_deploy_token_is_unavailable_not_broken(client, monkeypatch):
    """Doren's guest and local Docker: no FLY_API_TOKEN, no advice, and
    the route still answers 200 so the app can hide the card. conftest
    blanks FLY_APP_NAME / FLY_MACHINE_ID / FLY_API_TOKEN for every test,
    so a box without a deploy token is the default world here."""
    MA.reset_cache()
    r = client.get("/api/server/advice", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and "deploy token" in body["reason"]


def test_the_opt_out_wins_even_with_a_token(client, identity, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "server_advice", False)   # SERVER_ADVICE=0
    body = client.get("/api/server/advice", headers=H).json()
    assert body["available"] is False and "SERVER_ADVICE" in body["reason"]


def test_a_fly_failure_is_unavailable_never_a_500(client, identity, monkeypatch):
    fake = FakeFly(fail_read=True)
    _patch_fly(monkeypatch, fake)
    r = client.get("/api/server/advice", headers=H)
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_the_route_is_token_gated(client):
    assert client.get("/api/server/advice").status_code == 401


# ── the verdict ────────────────────────────────────────────────────────

def test_a_comfortable_machine_gets_no_advice(client, identity, monkeypatch):
    from app import disk_watch
    fake = FakeFly(memory_mb=1024, size_gb=3)
    monkeypatch.setattr(disk_watch, "snapshot", lambda: _disk(40.0, 1.8, 3.0))
    monkeypatch.setattr(MA, "rss_bytes", lambda: 300 * 2**20)

    async def no_growth():
        return 1_000_000.0        # 1 MB/day: years of headroom
    monkeypatch.setattr(MA, "growth_bytes_per_day", no_growth)
    doc = asyncio.run(MA.compute(_fly(fake), now_ms=1_800_000_000_000))
    assert doc["available"] is True
    assert doc["advice"] == []
    assert doc["machine"]["memory_mb"] == 1024
    assert doc["volume"]["size_gb"] == 3 and doc["volume"]["id"] == VOL
    assert doc["volume"]["headroom_days"] > 365


def test_a_full_volume_earns_an_extend_with_the_list_price(client, identity, monkeypatch):
    """Doren's 98% moment. 3 GB, 2.7 GB used, growing 20 MB/day: the
    target holds a year of growth under 60% used, whole GB, and the
    delta is $0.15 per added GB."""
    from app import disk_watch
    fake = FakeFly(size_gb=3)
    monkeypatch.setattr(disk_watch, "snapshot", lambda: _disk(90.0, 0.3, 3.0))
    monkeypatch.setattr(MA, "rss_bytes", lambda: 300 * 2**20)

    async def growth():
        return 20 * 2**20
    monkeypatch.setattr(MA, "growth_bytes_per_day", growth)
    doc = asyncio.run(MA.compute(_fly(fake), now_ms=1_800_000_000_000))
    (row,) = doc["advice"]
    assert row["kind"] == "volume" and row["current"] == 3
    # used 2.7 GB / 0.6 = 4.5 GB; 2.7 + 365*20MB = 9.83 GB → 10 GB
    assert row["recommended"] == 10
    assert row["est_usd_per_month"] == pytest.approx(0.15 * 7)
    assert row["restarts"] is False
    assert "90% full" in row["reason"] and "days of room" in row["reason"]


def test_tight_headroom_alone_is_enough(client, identity, monkeypatch):
    from app import disk_watch
    fake = FakeFly(size_gb=10)
    # 50% used but 60 days of headroom at this growth rate.
    monkeypatch.setattr(disk_watch, "snapshot", lambda: _disk(50.0, 5.0, 10.0))
    monkeypatch.setattr(MA, "rss_bytes", lambda: 300 * 2**20)

    async def growth():
        return (5 * GIB) / 60
    monkeypatch.setattr(MA, "growth_bytes_per_day", growth)
    doc = asyncio.run(MA.compute(_fly(fake), now_ms=1_800_000_000_000))
    assert doc["volume"]["headroom_days"] == 60
    (row,) = doc["advice"]
    assert row["kind"] == "volume" and row["recommended"] > 10
    assert "days of room" in row["reason"] and "full" not in row["reason"]


def test_memory_pressure_recommends_the_next_preset_and_names_the_restart(
        client, identity, monkeypatch):
    from app import disk_watch
    fake = FakeFly(memory_mb=512, size_gb=3)
    monkeypatch.setattr(disk_watch, "snapshot", lambda: _disk(40.0, 1.8, 3.0))
    monkeypatch.setattr(MA, "rss_bytes", lambda: 420 * 2**20)   # 82%

    async def growth():
        return 1_000_000.0
    monkeypatch.setattr(MA, "growth_bytes_per_day", growth)
    doc = asyncio.run(MA.compute(_fly(fake), now_ms=1_800_000_000_000))
    (row,) = doc["advice"]
    assert row["kind"] == "memory"
    assert row["current"] == 512 and row["recommended"] == 1024
    assert row["est_usd_per_month"] == pytest.approx(5.92 - 3.32)
    assert row["restarts"] is True and "restarts" in row["reason"]
    assert doc["machine"]["rss_mb"] == 420.0


def test_a_recent_oom_kill_counts_even_when_rss_reads_fine(client, identity, monkeypatch):
    from app import disk_watch
    now = 1_800_000_000_000
    events = [{"type": "exit", "status": "stopped", "source": "flyd",
               "timestamp": now - 3_600_000,
               "request": {"exit_event": {"oom_killed": True, "exit_code": 137}}}]
    fake = FakeFly(memory_mb=1024, size_gb=3, events=events)
    monkeypatch.setattr(disk_watch, "snapshot", lambda: _disk(40.0, 1.8, 3.0))
    monkeypatch.setattr(MA, "rss_bytes", lambda: 200 * 2**20)

    async def growth():
        return 1_000_000.0
    monkeypatch.setattr(MA, "growth_bytes_per_day", growth)
    doc = asyncio.run(MA.compute(_fly(fake), now_ms=now))
    (row,) = doc["advice"]
    assert row["kind"] == "memory" and row["recommended"] == 2048
    assert "killed for running out of memory" in row["reason"]
    assert doc["machine"]["oom_recent"] is True


def test_an_old_oom_event_is_forgotten(client, identity, monkeypatch):
    from app import disk_watch
    now = 1_800_000_000_000
    events = [{"type": "exit", "timestamp": now - 30 * 86_400_000,
               "request": {"exit_event": {"oom_killed": True}}}]
    fake = FakeFly(memory_mb=1024, size_gb=3, events=events)
    monkeypatch.setattr(disk_watch, "snapshot", lambda: _disk(40.0, 1.8, 3.0))
    monkeypatch.setattr(MA, "rss_bytes", lambda: 200 * 2**20)

    async def growth():
        return 1_000_000.0
    monkeypatch.setattr(MA, "growth_bytes_per_day", growth)
    doc = asyncio.run(MA.compute(_fly(fake), now_ms=now))
    assert doc["advice"] == [] and doc["machine"]["oom_recent"] is False


def test_the_top_of_the_preset_gets_advice_without_a_target(client, identity, monkeypatch):
    from app import disk_watch
    fake = FakeFly(memory_mb=8192, size_gb=3)
    monkeypatch.setattr(disk_watch, "snapshot", lambda: _disk(40.0, 1.8, 3.0))
    monkeypatch.setattr(MA, "rss_bytes", lambda: 7000 * 2**20)

    async def growth():
        return 1_000_000.0
    monkeypatch.setattr(MA, "growth_bytes_per_day", growth)
    doc = asyncio.run(MA.compute(_fly(fake), now_ms=1_800_000_000_000))
    (row,) = doc["advice"]
    assert row["kind"] == "memory" and row["recommended"] is None
    assert row["est_usd_per_month"] is None


# ── price math ─────────────────────────────────────────────────────────

def test_the_price_table_is_the_published_one():
    assert MA.SHARED_1X_USD_PER_MONTH == {256: 2.02, 512: 3.32,
                                          1024: 5.92, 2048: 11.11}
    assert MA.VOLUME_USD_PER_GB_MONTH == 0.15
    assert "2026-09-05" in MA.PRICING_SOURCE
    assert MA.memory_delta_usd("shared", 1, 256, 512) == pytest.approx(1.30)
    # Past the named presets Fly's own rule applies: about $5 per GB.
    assert MA.memory_usd_per_month("shared", 1, 3072) == pytest.approx(11.11 + 5.0)
    # An unknown preset (performance CPUs) still prices the delta by that rule.
    assert MA.memory_delta_usd("performance", 2, 4096, 8192) == pytest.approx(20.0)
    assert MA.volume_delta_usd(7) == pytest.approx(1.05)


def test_next_memory_walks_the_presets_then_gigabytes():
    assert MA.next_memory_mb(256) == 512
    assert MA.next_memory_mb(512) == 1024
    assert MA.next_memory_mb(1024) == 2048
    assert MA.next_memory_mb(2048) == 3072
    assert MA.next_memory_mb(8192) is None


def test_recommend_volume_rounds_up_and_caps_the_step():
    # 2.7 GB used, no growth known: 2.7/0.6 = 4.5 → 5 GB.
    assert MA.recommend_volume_gb(3, int(2.7 * GIB), int(0.3 * GIB), None) == 5
    # Already roomy: no advice.
    assert MA.recommend_volume_gb(10, int(2 * GIB), int(8 * GIB), 1000.0) is None
    # Runaway growth is capped at one step.
    assert MA.recommend_volume_gb(3, int(2.7 * GIB), int(0.3 * GIB), GIB) == 53


# ── growth from the database ───────────────────────────────────────────

def test_growth_is_bytes_per_row_times_rows_per_day(client):
    """Fourteen rows in the last week from a file of known size: the
    estimate is size/rows × rows/7."""
    import os
    import time
    from app import db
    from app.config import settings
    now = int(time.time() * 1000)
    rows = [{"dateutc": now - i * 3_600_000 * 12, "tempf": 70.0} for i in range(14)]
    asyncio.run(db.insert_observations("AA:BB:CC:00:00:01", rows))
    size = os.stat(settings.database_path).st_size
    g = asyncio.run(MA.growth_bytes_per_day())
    assert g == pytest.approx((size / 14) * 2.0)


def test_growth_is_absent_on_an_empty_archive(client):
    assert asyncio.run(MA.growth_bytes_per_day()) is None


# ── the cache ──────────────────────────────────────────────────────────

def test_advice_is_cached_for_an_hour_and_refresh_bypasses(client, identity, monkeypatch):
    calls = {"n": 0}
    clock = {"t": 1000.0}

    async def fake_compute():
        calls["n"] += 1
        return {"available": True, "advice": [], "n": calls["n"]}
    monkeypatch.setattr(MA, "compute", fake_compute)
    monkeypatch.setattr(MA.time, "monotonic", lambda: clock["t"])
    a = client.get("/api/server/advice", headers=H).json()
    b = client.get("/api/server/advice", headers=H).json()
    assert a["n"] == b["n"] == 1
    c = client.get("/api/server/advice?refresh=1", headers=H).json()
    assert c["n"] == 2
    # "For an hour" means the clock, not the request count: one second
    # short of the TTL still serves the cache, one past it recomputes.
    clock["t"] += MA.ADVICE_TTL_S - 1
    assert client.get("/api/server/advice", headers=H).json()["n"] == 2
    clock["t"] += 2
    assert client.get("/api/server/advice", headers=H).json()["n"] == 3


def test_switched_off_blocks_apply_not_only_the_read(client, identity, monkeypatch):
    """T7: SERVER_ADVICE=0 must refuse the route that spends money, not
    merely hide the card."""
    fake = FakeFly(size_gb=3)
    _patch_fly(monkeypatch, fake)
    from app.config import settings
    monkeypatch.setattr(settings, "server_advice", False)   # SERVER_ADVICE=0
    r = client.post("/api/server/advice/apply", headers=H,
                    json={"kind": "volume", "target": 5})
    assert r.status_code == 409 and "SERVER_ADVICE" in r.json()["detail"]
    assert not [c for c in fake.calls if c[0] in ("PUT", "POST")], \
        "nothing may reach Fly while the feature is off"


# ── apply ──────────────────────────────────────────────────────────────

def test_apply_extends_the_volume_online(client, identity, monkeypatch):
    fake = FakeFly(size_gb=3)
    _patch_fly(monkeypatch, fake)
    r = client.post("/api/server/advice/apply", headers=H,
                    json={"kind": "volume", "target": 5})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "kind": "volume", "from": 3, "to": 5,
                        "restarts": False, "needs_restart": False}
    put = [c for c in fake.calls if c[0] == "PUT"]
    assert put == [("PUT", f"{MA.MACHINES_BASE}/apps/{APP}/volumes/{VOL}/extend",
                    {"size_gb": 5})]


def test_apply_sets_memory_by_posting_the_whole_config(client, identity, monkeypatch):
    fake = FakeFly(memory_mb=512)
    _patch_fly(monkeypatch, fake)
    r = client.post("/api/server/advice/apply", headers=H,
                    json={"kind": "memory", "target": 1024})
    assert r.status_code == 200, r.text
    assert r.json()["restarts"] is True and r.json()["to"] == 1024
    post = [c for c in fake.calls if c[0] == "POST"]
    assert len(post) == 1
    assert post[0][2]["config"]["guest"] == {"cpu_kind": "shared", "cpus": 1,
                                            "memory_mb": 1024}
    assert post[0][2]["config"]["image"] == "ghcr.io/x:2.1.0"


@pytest.mark.parametrize("body,needle", [
    ({"kind": "volume", "target": 3}, "only grow"),
    ({"kind": "volume", "target": 2}, "only grow"),
    ({"kind": "volume", "target": 99}, "at most"),
    ({"kind": "memory", "target": 512}, "only adds"),
    ({"kind": "memory", "target": 1300}, "multiple of 256"),
    ({"kind": "memory", "target": 16384}, "multiple of 256"),
])
def test_apply_refuses_shrinks_skips_and_odd_sizes(client, identity, monkeypatch,
                                                   body, needle):
    """Validated against the machine NOW, not the cached card: nothing
    shrinks, nothing jumps the cap, and Fly is never called."""
    fake = FakeFly(memory_mb=1024, size_gb=3)
    _patch_fly(monkeypatch, fake)
    r = client.post("/api/server/advice/apply", headers=H, json=body)
    assert r.status_code in (409, 422), r.text
    if r.status_code == 409:
        assert needle in r.json()["detail"]
    assert not [c for c in fake.calls if c[0] in ("PUT", "POST")]


def test_apply_without_a_token_is_a_clear_refusal(client):
    # conftest blanks the Fly trio: no deploy token is the default world.
    r = client.post("/api/server/advice/apply", headers=H,
                    json={"kind": "volume", "target": 5})
    assert r.status_code == 409 and "deploy token" in r.json()["detail"]


def test_apply_reports_a_fly_refusal_as_502_not_500(client, identity, monkeypatch):
    fake = FakeFly(size_gb=3)

    async def refuse(url, headers=None, json=None):
        return _Resp(422, {"error": "volume is being snapshotted"})
    fake.put = refuse
    _patch_fly(monkeypatch, fake)
    r = client.post("/api/server/advice/apply", headers=H,
                    json={"kind": "volume", "target": 5})
    assert r.status_code == 502 and "snapshotted" in r.json()["detail"]


def test_apply_needs_the_write_token(client, identity):
    r = client.post("/api/server/advice/apply",
                    json={"kind": "volume", "target": 5})
    assert r.status_code == 401


def test_apply_invalidates_the_cached_advice(client, identity, monkeypatch):
    fake = FakeFly(size_gb=3)
    _patch_fly(monkeypatch, fake)
    MA._CACHE["at"], MA._CACHE["value"] = 10**12, {"available": True, "stale": True}
    client.post("/api/server/advice/apply", headers=H,
                json={"kind": "volume", "target": 5})
    assert MA._CACHE["value"] is None
