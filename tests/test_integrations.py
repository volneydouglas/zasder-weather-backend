"""App-managed cloud-source credentials (/api/integrations).

The dangerous half — actually starting pollers — is exercised with fake
poller classes; nothing here may reach AmbientWeather/WeatherLink/
WeatherFlow. What matters: presence-only status (secrets never echo),
kv-over-env resolution with partial updates, the apply lifecycle
(restart on change, stop on clear), and the owner-only gate.
"""
from __future__ import annotations

import asyncio
import importlib

H = {"Authorization": "Bearer test-api-token"}
RH = {"Authorization": "Bearer test-reviewer-token"}


def test_status_is_presence_only_and_owner_gated(client):
    r = client.get("/api/integrations", headers=H)
    assert r.status_code == 200
    provs = r.json()["providers"]
    assert set(provs) == {"awn", "weatherlink", "tempest"}
    for p in provs.values():
        assert p["configured"] is False        # conftest blanks every cred
    # Read-only tokens must not even see WHICH providers are configured.
    assert client.get("/api/integrations", headers=RH).status_code == 403


def _stub_probe(monkeypatch, result=None):
    """The PUT endpoint probes upstream with the stored credentials — the
    suite must never make that call for real."""
    from app import integrations

    async def fake_probe(provider):
        return result
    monkeypatch.setattr(integrations, "probe", fake_probe)


def test_put_stores_applies_and_never_echoes_the_secret(client, monkeypatch):
    from app import main
    applied: list[str] = []

    async def fake_apply(provider):
        applied.append(provider)
        return True
    monkeypatch.setattr(main.app.state.integration_manager, "apply", fake_apply)
    _stub_probe(monkeypatch)

    secret = "tempest-token-abcdef123456"
    r = client.put("/api/integrations/tempest", headers=H,
                   json={"token": secret, "station_id": 229934,
                         "name": "Chandler Tempest"})
    assert r.status_code == 200, r.text
    assert applied == ["tempest"], "PUT must apply immediately"
    body = r.json()
    assert secret not in r.text, "secret echoed back"
    t = body["providers"]["tempest"]
    assert t["configured"] is True and t["source"] == "app"
    assert t["fields"]["token"] == {"set": True, "source": "app"}
    # Non-secrets are display-safe: the screen shows which station is wired.
    assert t["fields"]["station_id"]["value"] == 229934
    assert t["fields"]["name"]["value"] == "Chandler Tempest"

    # Partial update: changing only the name must keep the token (the SMTP
    # partial-update contract), and an empty string clears a field.
    r = client.put("/api/integrations/tempest", headers=H,
                   json={"name": "Backyard"})
    assert r.json()["providers"]["tempest"]["fields"]["token"]["set"] is True
    r = client.put("/api/integrations/tempest", headers=H, json={"token": ""})
    assert r.json()["providers"]["tempest"]["configured"] is False

    # Unknown fields and unparseable values are 400s, never stored.
    assert client.put("/api/integrations/tempest", headers=H,
                      json={"tokken": "x"}).status_code == 400
    assert client.put("/api/integrations/tempest", headers=H,
                      json={"station_id": "not-a-number"}).status_code == 400
    assert client.put("/api/integrations/nope", headers=H,
                      json={}).status_code == 404


def test_manager_lifecycle_with_fake_pollers(temp_env, monkeypatch):
    """apply() must stop the old poller before starting the new one, start
    nothing while unconfigured, and stop the poller when cleared."""
    for mod in ("app.config", "app.db", "app.integrations"):
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import db, integrations
    from app import tempest_client, tempest_poller
    asyncio.run(db.init_db())

    events: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def aclose(self): events.append("client-closed")

    class FakePoller:
        def __init__(self, *a, **k): pass
        async def start(self): events.append("started")
        async def stop(self): events.append("stopped")

    monkeypatch.setattr(tempest_client, "TempestClient", FakeClient)
    monkeypatch.setattr(tempest_poller, "TempestPoller", FakePoller)

    mgr = integrations.IntegrationManager()
    # Unconfigured: nothing starts.
    assert asyncio.run(mgr.apply("tempest")) is False
    assert events == []
    # Configured via the app store: starts.
    asyncio.run(integrations.store("tempest",
                                   {"token": "t" * 20, "station_id": 1}))
    assert asyncio.run(mgr.apply("tempest")) is True
    assert events == ["started"]
    # Re-apply (credential change): old stops before new starts.
    assert asyncio.run(mgr.apply("tempest")) is True
    assert events == ["started", "stopped", "client-closed", "started"]
    # Cleared: poller stops and stays stopped.
    asyncio.run(integrations.clear("tempest"))
    assert asyncio.run(mgr.apply("tempest")) is False
    assert events[-2:] == ["stopped", "client-closed"]


def test_store_persists_the_coerced_form(client, monkeypatch):
    """CodeRabbit 2026-08-20: a JSON 1.9 or true survives int() at
    validation, but storing str(raw) persisted "1.9"/"True" — which the
    read path's int() rejects, silently degrading the field to None behind
    a 200. The coerced form must be what lands in the store."""
    from app import main
    async def fake_apply(provider):
        return True
    monkeypatch.setattr(main.app.state.integration_manager, "apply", fake_apply)
    _stub_probe(monkeypatch)
    r = client.put("/api/integrations/tempest", headers=H,
                   json={"token": "t" * 20, "station_id": 1.9})
    assert r.status_code == 200
    t = r.json()["providers"]["tempest"]
    assert t["configured"] is True, "stored form failed the read path"
    assert t["fields"]["station_id"]["value"] == 1


def test_put_reports_failing_credentials_without_refusing_them(client, monkeypatch):
    """R5-07 (the R3-21 WU-key pattern reborn): wrong keys used to save as
    a silent success — "On" in the UI, the failure visible only at
    /api/sources, which no client reads. The PUT response must carry the
    probe result; the values persist anyway, because an upstream outage
    must never block saving."""
    from app import main

    async def fake_apply(provider):
        return True
    monkeypatch.setattr(main.app.state.integration_manager, "apply", fake_apply)
    _stub_probe(monkeypatch, "Tempest HTTP 401 on /stations")

    r = client.put("/api/integrations/tempest", headers=H,
                   json={"token": "w" * 20, "station_id": 1})
    assert r.status_code == 200, r.text
    assert r.json()["check"] == "Tempest HTTP 401 on /stations"
    assert r.json()["providers"]["tempest"]["configured"] is True, \
        "a failing probe must not refuse the save"

    _stub_probe(monkeypatch, None)
    r = client.put("/api/integrations/tempest", headers=H,
                   json={"station_id": 2})
    assert r.json()["check"] is None


def test_probe_reports_auth_failure_and_closes_the_client(temp_env, monkeypatch):
    """probe() must return the client's (path-only, credential-free) error
    message rather than raising, and must close the throwaway client either
    way."""
    for mod in ("app.config", "app.db", "app.integrations"):
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import db, integrations, tempest_client
    asyncio.run(db.init_db())

    events: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k):
            events.append("built")

        async def stations(self):
            raise tempest_client.TempestError("Tempest HTTP 401 on /stations")

        async def aclose(self):
            events.append("closed")

    monkeypatch.setattr(tempest_client, "TempestClient", FakeClient)
    # Unconfigured: no client is even built.
    assert asyncio.run(integrations.probe("tempest")) is None
    assert events == []
    asyncio.run(integrations.store("tempest",
                                   {"token": "t" * 20, "station_id": 1}))
    msg = asyncio.run(integrations.probe("tempest"))
    assert msg == "Tempest HTTP 401 on /stations"
    assert events == ["built", "closed"], "throwaway client leaked"


def test_store_is_all_or_nothing(temp_env):
    """R5-23: store() wrote fields one at a time, so a coercion failure on
    a LATER field left the earlier new values in server_kv behind the 400.
    A failed store must change nothing."""
    for mod in ("app.config", "app.db", "app.integrations"):
        if mod in importlib.sys.modules:
            importlib.reload(importlib.sys.modules[mod])
    from app import db, integrations
    asyncio.run(db.init_db())

    asyncio.run(integrations.store("tempest",
                                   {"token": "old" * 7, "station_id": 1}))
    try:
        asyncio.run(integrations.store(
            "tempest", {"token": "new" * 7, "station_id": "junk"}))
        raise AssertionError("bad station_id must raise")
    except ValueError:
        pass
    eff = asyncio.run(integrations.effective("tempest"))
    assert eff["token"] == "old" * 7, "partial write leaked the new token"
    assert eff["station_id"] == 1
