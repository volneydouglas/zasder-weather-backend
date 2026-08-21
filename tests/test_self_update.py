"""Opt-in self-update (app/self_update.py).

The dangerous half — pointing the machine at a new image — is exercised
against fakes only; nothing here reaches Fly or a registry. What matters:
the eligibility rules (maturity, same-major, never-downgrade), the
first-seen clock surviving restarts, the missing-image hold, and the
attempt-before-apply ordering that prevents restart loops.
"""
from __future__ import annotations

import asyncio
import importlib
import json

import pytest


@pytest.fixture
def wired(temp_env: str, monkeypatch):
    for mod in ("app.config", "app.db", "app.updates", "app.self_update"):
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import db, self_update
    asyncio.run(db.init_db())
    return db, self_update


HOUR = 3_600_000


def test_eligibility_rules(wired):
    _, su = wired
    e = su.eligible
    now = 1_000 * HOUR
    # Maturity: first sighting starts the clock; 48h must elapse.
    assert e("1.7.0", "1.6.0", None, now, 48 * HOUR)[0] is False
    assert e("1.7.0", "1.6.0", now - 47 * HOUR, now, 48 * HOUR)[0] is False
    assert e("1.7.0", "1.6.0", now - 49 * HOUR, now, 48 * HOUR)[0] is True
    # Never downgrade, never sideways.
    assert e("1.6.0", "1.6.0", now - 99 * HOUR, now, 48 * HOUR)[0] is False
    assert e("1.5.9", "1.6.0", now - 99 * HOUR, now, 48 * HOUR)[0] is False
    # A major bump waits for a human however mature it is.
    ok, reason = e("2.0.0", "1.6.0", now - 999 * HOUR, now, 48 * HOUR)
    assert ok is False and "human" in reason
    # No release visible.
    assert e(None, "1.6.0", None, now, 48 * HOUR)[0] is False


class _FakeApp:
    class state:
        update_info: dict = {}


def _tick(su, latest):
    app = _FakeApp()
    app.state.update_info = {"latest": latest}
    updater = su.SelfUpdater(app)
    asyncio.run(updater._tick())


def test_first_seen_clock_persists_and_resets_per_tag(wired, monkeypatch):
    """Restarts must not reset the maturity clock (it lives in server_kv),
    and a NEW tag must restart it — v1.6.1 seen for two days says nothing
    about v1.6.2's maturity."""
    db, su = wired
    applied: list[str] = []
    monkeypatch.setattr(su, "image_exists", _async_true)
    monkeypatch.setattr(su, "apply_update", _async_capture(applied))

    _tick(su, "1.999.0")
    seen = json.loads(asyncio.run(db.get_kv("auto_update_first_seen")))
    assert seen["tag"] == "1.999.0"
    assert applied == [], "must not apply on first sighting"

    # Simulate a restart two days later: rewind the stored clock instead of
    # waiting — the point is that the value comes from the DB, not memory.
    seen["ms"] -= 49 * HOUR
    asyncio.run(db.set_kv("auto_update_first_seen", json.dumps(seen)))
    _tick(su, "1.999.0")
    assert applied == ["1.999.0"], "mature release must apply"

    # A newer tag restarts the clock: nothing applies on ITS first sighting.
    applied.clear()
    _tick(su, "1.999.1")
    assert applied == []
    seen2 = json.loads(asyncio.run(db.get_kv("auto_update_first_seen")))
    assert seen2["tag"] == "1.999.1"


def test_missing_image_holds_and_attempt_cooldown(wired, monkeypatch):
    """A release with no published image must hold (not brick the machine),
    and once an apply is attempted it is not retried for a day — recorded
    BEFORE the apply so a restart mid-update cannot loop."""
    db, su = wired
    applied: list[str] = []
    monkeypatch.setattr(su, "apply_update", _async_capture(applied))

    asyncio.run(db.set_kv("auto_update_first_seen", json.dumps(
        {"tag": "1.999.0", "ms": 0})))
    monkeypatch.setattr(su, "image_exists", _async_false)
    _tick(su, "1.999.0")
    assert applied == [], "applied onto an unverifiable image"
    assert asyncio.run(db.get_kv("auto_update_last_attempt")) is None, \
        "a held update must not consume the attempt budget"

    monkeypatch.setattr(su, "image_exists", _async_true)
    _tick(su, "1.999.0")
    assert applied == ["1.999.0"]
    attempt = json.loads(asyncio.run(db.get_kv("auto_update_last_attempt")))
    assert attempt["tag"] == "1.999.0"

    _tick(su, "1.999.0")
    assert applied == ["1.999.0"], "retried within the cooldown"


async def _async_true(*a, **k):
    return True


async def _async_false(*a, **k):
    return False


def _async_capture(sink):
    async def _c(tag):
        sink.append(tag)
        return True
    return _c


def test_update_apply_endpoint_guards(client, monkeypatch):
    """POST /api/update/apply is the push-button sibling of AUTO_UPDATE.
    Guards, in order: no update -> 409; major bump -> 409 (manual steps);
    no deploy token -> 409 with the recipe; unpublished image -> 409; and
    only then does the machine update fire. Write-gated like every other
    mutating route (covered by the invariants suite)."""
    from app import main, self_update
    H = {"Authorization": "Bearer test-api-token"}

    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "up to date" in r.json()["detail"]

    main.app.state.update_info = {"latest": "999.0.0", "update_available": True}
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "major" in r.json()["detail"]

    main.app.state.update_info = {"latest": "1.999.0", "update_available": True}
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "deploy token" in r.json()["detail"]

    monkeypatch.setenv("FLY_API_TOKEN", "x" * 20)
    monkeypatch.setattr(self_update, "image_exists", _async_false)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "no published image" in r.json()["detail"]

    applied: list[str] = []
    monkeypatch.setattr(self_update, "image_exists", _async_true)
    monkeypatch.setattr(self_update, "apply_update", _async_capture(applied))
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 200 and applied == ["1.999.0"]


def _mock_httpx(monkeypatch, handler):
    """Route the module's httpx.AsyncClient through a MockTransport — these
    tests must never touch the live Machines API or a registry. Idempotent
    within a test: re-patching must wrap the REAL class, not the previous
    factory (stacked factories silently reuse the first handler)."""
    import httpx
    if not hasattr(httpx, "_zw_real_async_client"):
        httpx._zw_real_async_client = httpx.AsyncClient
    real = httpx._zw_real_async_client

    def factory(*a, **kw):
        return real(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _fly_env(monkeypatch):
    monkeypatch.setenv("FLY_APP_NAME", "zw-test")
    monkeypatch.setenv("FLY_MACHINE_ID", "d891234")
    monkeypatch.setenv("FLY_API_TOKEN", "FlyV1 fm2_testtoken")


def test_apply_update_round_trips_the_full_machine_config(wired, monkeypatch):
    """CODE_REVIEW_R5 R5-17: apply_update was monkeypatched away in every
    test, leaving the machine-config rewrite — the one call that can brick
    an instance — unexercised. The POST must carry the machine's WHOLE
    config (env, services, mounts…) with only the image swapped; POSTing
    {"config": {"image": …}} alone would replace the config wholesale."""
    import httpx
    db, self_update = wired
    _fly_env(monkeypatch)
    machine_config = {
        "image": "ghcr.io/x/y:1.5.0",
        "env": {"REQUIRE_RELAY": "1"},
        "services": [{"internal_port": 8000}],
        "mounts": [{"volume": "vol_data", "path": "/data"}],
    }
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"config": dict(machine_config)})
        posted["json"] = json.loads(request.content)
        posted["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})
    _mock_httpx(monkeypatch, handler)

    assert asyncio.run(self_update.apply_update("1.6.0")) is True
    cfg = posted["json"]["config"]
    assert cfg["image"].endswith(":1.6.0")
    assert cfg["env"] == {"REQUIRE_RELAY": "1"}, "machine env dropped"
    assert cfg["services"] == [{"internal_port": 8000}], "services dropped"
    assert cfg["mounts"] == [{"volume": "vol_data", "path": "/data"}], \
        "volume mount dropped — the database would detach"
    # The deploy token goes through verbatim, interior space included.
    assert posted["auth"] == "Bearer FlyV1 fm2_testtoken"


def test_apply_update_holds_on_empty_or_unreadable_config(wired, monkeypatch):
    """The load-bearing guard: an empty machine config must HOLD (no POST),
    and a failed read must hold too — worse than not updating is updating
    onto a wholesale-replaced config."""
    import httpx
    db, self_update = wired
    _fly_env(monkeypatch)
    calls = {"posts": 0}

    def empty_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"config": {}})
        calls["posts"] += 1
        return httpx.Response(200, json={})
    _mock_httpx(monkeypatch, empty_handler)
    assert asyncio.run(self_update.apply_update("1.6.0")) is False
    assert calls["posts"] == 0, "posted a wholesale config replacement"

    def failed_read(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(500)
        calls["posts"] += 1
        return httpx.Response(200, json={})
    _mock_httpx(monkeypatch, failed_read)
    assert asyncio.run(self_update.apply_update("1.6.0")) is False
    assert calls["posts"] == 0


def test_image_exists_registry_challenge_flow(wired, monkeypatch):
    """image_exists must follow the WWW-Authenticate challenge to the REALM
    host (auth.docker.io-style, not the registry host), send the scoped
    token, and fail CLOSED on a challenge without a realm."""
    import httpx
    db, self_update = wired
    seen = {"token_host": None}

    def challenge_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.io":
            seen["token_host"] = request.url.host
            assert request.url.params["scope"] == "repository:x/y:pull"
            assert request.url.params["service"] == "registry.example.io"
            return httpx.Response(200, json={"token": "tok123"})
        if request.headers.get("authorization") == "Bearer tok123":
            return httpx.Response(200)
        return httpx.Response(401, headers={
            "www-authenticate": 'Bearer realm="https://auth.example.io/token",'
                                'service="registry.example.io"'})
    _mock_httpx(monkeypatch, challenge_handler)
    assert asyncio.run(
        self_update.image_exists("registry.example.io/x/y", "1.6.0")) is True
    assert seen["token_host"] == "auth.example.io"

    # Anonymous-read registry: first HEAD 200s, done.
    _mock_httpx(monkeypatch, lambda r: httpx.Response(200))
    assert asyncio.run(
        self_update.image_exists("registry.example.io/x/y", "1.6.0")) is True

    # 401 with no realm: fail closed.
    _mock_httpx(monkeypatch, lambda r: httpx.Response(
        401, headers={"www-authenticate": "Bearer nonsense"}))
    assert asyncio.run(
        self_update.image_exists("registry.example.io/x/y", "1.6.0")) is False
