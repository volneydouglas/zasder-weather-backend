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
# A "newer patch of the SAME major", whatever the current version is: the
# literals were 1.999.x and went stale the day version.py said 2.0.0.
from app.version import __version__ as _CURRENT  # noqa: E402
_MAJOR = _CURRENT.split(".")[0]
NEWER = f"{_MAJOR}.999.0"
NEWER1 = f"{_MAJOR}.999.1"
NEXT_MAJOR = f"{int(_MAJOR) + 1}.0.0"


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

    _tick(su, NEWER)
    seen = json.loads(asyncio.run(db.get_kv("auto_update_first_seen")))
    assert seen["tag"] == NEWER
    assert applied == [], "must not apply on first sighting"

    # Simulate a restart two days later: rewind the stored clock instead of
    # waiting — the point is that the value comes from the DB, not memory.
    seen["ms"] -= 49 * HOUR
    asyncio.run(db.set_kv("auto_update_first_seen", json.dumps(seen)))
    _tick(su, NEWER)
    assert applied == [NEWER], "mature release must apply"

    # A newer tag restarts the clock: nothing applies on ITS first sighting.
    applied.clear()
    _tick(su, NEWER1)
    assert applied == []
    seen2 = json.loads(asyncio.run(db.get_kv("auto_update_first_seen")))
    assert seen2["tag"] == NEWER1


def test_missing_image_holds_and_attempt_cooldown(wired, monkeypatch):
    """A release with no published image must hold (not brick the machine),
    and once an apply is attempted it is not retried for a day — recorded
    BEFORE the apply so a restart mid-update cannot loop."""
    db, su = wired
    applied: list[str] = []
    monkeypatch.setattr(su, "apply_update", _async_capture(applied))

    asyncio.run(db.set_kv("auto_update_first_seen", json.dumps(
        {"tag": NEWER, "ms": 0})))
    monkeypatch.setattr(su, "image_exists", _async_false)
    _tick(su, NEWER)
    assert applied == [], "applied onto an unverifiable image"
    assert asyncio.run(db.get_kv("auto_update_last_attempt")) is None, \
        "a held update must not consume the attempt budget"

    monkeypatch.setattr(su, "image_exists", _async_true)
    _tick(su, NEWER)
    assert applied == [NEWER]
    attempt = json.loads(asyncio.run(db.get_kv("auto_update_last_attempt")))
    assert attempt["tag"] == NEWER

    _tick(su, NEWER)
    assert applied == [NEWER], "retried within the cooldown"


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

    # Deploy-token check now precedes the major gate (R12): the vouch
    # fetch is a network round-trip, and a token-less instance shouldn't
    # pay it for a foregone 409 — so a major bump WITHOUT a token reads
    # "deploy token", not "major".
    main.app.state.update_info = {"latest": NEWER, "update_available": True}
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "deploy token" in r.json()["detail"]

    monkeypatch.setenv("FLY_API_TOKEN", "x" * 20)
    # The graceful-majors vouch fetch (8fe6556) reaches for the real tag on
    # GitHub — mock it CLOSED like the neighboring vouch test, or this arm
    # makes a live HTTPS call on every run and can't tell "gate worked"
    # from "fetch failed" (R12 W5).
    monkeypatch.setattr(self_update, "upgrade_manifest_allows", _async_false)
    main.app.state.update_info = {"latest": "999.0.0", "update_available": True}
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "major" in r.json()["detail"]

    main.app.state.update_info = {"latest": NEWER, "update_available": True}
    monkeypatch.setattr(self_update, "image_exists", _async_false)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "no published image" in r.json()["detail"]

    applied: list[str] = []
    monkeypatch.setattr(self_update, "image_exists", _async_true)
    monkeypatch.setattr(self_update, "apply_update", _async_capture(applied))
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 200 and applied == [NEWER]


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
    # Macaroon deploy tokens go out under the FlyV1 scheme (the Bearer wrap
    # broke one-tap updates on every setup-script server — 2026-08-26).
    assert posted["auth"] == "FlyV1 fm2_testtoken"


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


def test_auth_header_scheme_split(wired):
    """Deploy tokens are macaroons and go out as FlyV1 — the Bearer wrap
    broke one-tap updates on every setup-script server (found live on the
    2026-08-26 field test). OAuth tokens stay Bearer."""
    _, self_update = wired
    h = self_update._auth_header
    # Raw macaroon → FlyV1.
    assert h("fm2_abc") == "FlyV1 fm2_abc"
    # flyctl output stored verbatim (setup-fly.sh does this): no double scheme.
    assert h("FlyV1 fm2_abc") == "FlyV1 fm2_abc"
    # Comma-joined discharge set rides the first token's type.
    assert h("fm2_abc,fo1_xyz") == "FlyV1 fm2_abc,fo1_xyz"
    # Older restricted-token prefixes count as macaroons too.
    assert h("fm1r_abc") == "FlyV1 fm1r_abc"
    # OAuth/account tokens keep Bearer, prefix stripped if present.
    assert h("fo1_xyz") == "Bearer fo1_xyz"
    assert h("Bearer fo1_xyz") == "Bearer fo1_xyz"
    # Whitespace never leaks into the header.
    assert h("  FlyV1  fm2_abc  ") == "FlyV1 fm2_abc"


def test_update_check_endpoint(client, monkeypatch):
    """POST /api/update/check (1.9) runs the daily lookup ON DEMAND — a
    release published an hour ago otherwise reads "up to date" for a day.
    Success returns the same shape /api/version serves (disk included);
    a GitHub miss is a 502 the caller can retry; write-gated."""
    from app import main
    H = {"Authorization": "Bearer test-api-token"}

    checker = main.app.state.update_checker

    async def fake_check():
        main.app.state.update_info = {
            "version": "1.8.2", "latest": "1.9.0",
            "update_available": True, "checked_ms": 123, "enabled": True}
        return True

    monkeypatch.setattr(checker, "_check_once", fake_check)
    r = client.post("/api/update/check", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["update_available"] is True and body["latest"] == "1.9.0"
    assert "disk" in body

    async def fake_fail():
        return False

    monkeypatch.setattr(checker, "_check_once", fake_fail)
    assert client.post("/api/update/check", headers=H).status_code == 502

    monkeypatch.setenv("UPDATE_CHECK", "0")
    r = client.post("/api/update/check", headers=H)
    assert r.status_code == 409 and "disabled" in r.json()["detail"]

    assert client.post("/api/update/check").status_code == 401


# ── graceful major upgrades (1.9): the vouching manifest ────────────────

def _manifest_handler(status=200, body=None):
    import httpx as _hx

    def handler(request):
        assert "raw.githubusercontent.com" in str(request.url)
        assert str(request.url).endswith("/upgrade.json")
        # R12 sub-ledger: the manifest must come from the TARGET release's
        # tag — fetching main's copy would let a future release vouch for
        # a past one.
        assert "/v2.0.0/" in str(request.url)
        if body is None:
            return _hx.Response(status)
        return _hx.Response(status, json=body)
    return handler


def test_manifest_vouching_matrix(wired, monkeypatch):
    """seamless_from at or below the running version lifts the gate; above
    it, absent, unreachable, or malformed all fail CLOSED."""
    _, su = wired

    def allows(status=200, body=None, current="1.9.2"):
        _mock_httpx(monkeypatch, _manifest_handler(status, body))
        return asyncio.run(su.upgrade_manifest_allows("2.0.0", current))

    assert allows(body={"seamless_from": "1.9.0"}) is True
    assert allows(body={"seamless_from": "1.0.0"}) is True
    # This server is too old for the vouch — release notes it is.
    assert allows(body={"seamless_from": "1.9.0"}, current="1.7.1") is False
    assert allows(status=404) is False                    # no manifest
    assert allows(body={"wrong": "shape"}) is False       # malformed
    assert allows(body={"seamless_from": 190}) is False   # non-string
    # Unparseable floors fail CLOSED (R12): parse_version returns (0,) for
    # garbage, and current >= (0,) is always true — before the regex guard
    # (96abd2d) every one of these silently lifted the major gate.
    assert allows(body={"seamless_from": "soon"}) is False
    assert allows(body={"seamless_from": "2.x"}) is False
    assert allows(body={"seamless_from": ""}) is False
    # The v-prefixed spelling of a valid floor still counts.
    assert allows(body={"seamless_from": "v1.9.0"}) is True

    def boom(request):
        raise __import__("httpx").ConnectError("offline")
    _mock_httpx(monkeypatch, boom)
    assert asyncio.run(su.upgrade_manifest_allows("2.0.0", "1.9.2")) is False


def test_update_apply_major_gate_honors_the_vouch(client, monkeypatch):
    """End to end through POST /api/update/apply: an unvouched major keeps
    the classic 409; a vouched one clears the MAJOR gate and proceeds to
    the next guard (published image — proving the gate is what lifted).
    A deploy token is present throughout: the token check runs BEFORE the
    vouch fetch now (R12), so a token-less instance never pays the network
    round-trip for a foregone 409."""
    from app import main, self_update
    H = {"Authorization": "Bearer test-api-token"}
    monkeypatch.setenv("FLY_API_TOKEN", "x" * 20)
    main.app.state.update_info = {"latest": NEXT_MAJOR, "update_available": True}

    async def no_vouch(latest, current):
        return False
    monkeypatch.setattr(self_update, "upgrade_manifest_allows", no_vouch)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "major" in r.json()["detail"]

    async def vouched(latest, current):
        assert latest == NEXT_MAJOR
        return True

    async def no_image(repo, tag):
        return False
    monkeypatch.setattr(self_update, "upgrade_manifest_allows", vouched)
    monkeypatch.setattr(self_update, "image_exists", no_image)
    r = client.post("/api/update/apply", headers=H)
    assert r.status_code == 409 and "no published image" in r.json()["detail"]


def test_auto_update_still_never_crosses_majors(wired):
    """AUTO_UPDATE deliberately ignores the manifest: unattended upgrades
    stay same-major; era changes get a human pressing the button once."""
    _, su = wired
    now = 1_000 * 3_600_000
    ok, _why = su.eligible("2.0.0", "1.9.2", now - 99 * 3_600_000, now,
                           48 * 3_600_000)
    assert ok is False



def test_a_pre_upgrade_snapshot_is_written_beside_the_database(wired, monkeypatch):
    """R17 / 2.0: the one-tap path used to take no snapshot. One is written
    with VACUUM INTO before the machine is pointed at the new image, only
    the newest is kept, and no room means no snapshot rather than no
    upgrade."""
    from pathlib import Path
    from app import self_update as su
    from app.config import settings
    db_path = Path(settings.database_path)
    stale = db_path.with_name(f"{db_path.name}.pre-upgrade-1.8.0.db")
    stale.write_bytes(b"old")
    dest = asyncio.run(su.snapshot_before_upgrade("2.0.0"))
    assert dest is not None and dest.exists() and dest.stat().st_size > 0
    assert dest.name == f"{db_path.name}.pre-upgrade-2.0.0.db"
    assert not stale.exists(), "only the newest snapshot is kept"
    # No room: logged, skipped, and the upgrade is not blocked.
    class Usage:
        free = 0
    monkeypatch.setattr(su.shutil, "disk_usage", lambda p: Usage)
    assert asyncio.run(su.snapshot_before_upgrade("2.0.1")) is None
    assert dest.exists()
