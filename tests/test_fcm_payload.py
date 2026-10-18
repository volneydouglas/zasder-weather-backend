"""F10 (Android review 2026-10-14): the FCM message carries the route, the
sender's server id and the delivery tier, DATA-ONLY, so the Android app's
FcmService.onMessageReceived runs in every lifecycle state and draws the
notification on the tier's channel with a tap that routes.

These capture the complete JSON body POSTed to FCM, through the real
send_to_all -> push_tokens_fcm path, with no network and no credentials."""
import json

import httpx
import pytest

SID = "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b"
_RealClient = httpx.AsyncClient


@pytest.fixture
def mods(temp_env):
    """app.apns / app.fcm imported after the test env is in place (the
    client fixture reloads app.apns, so a module-level import would go
    stale)."""
    from app import apns, fcm
    return apns, fcm


@pytest.fixture
def captured(mods, monkeypatch):
    """Every FCM POST body, decoded. FCM is 'configured' with a fake service
    account and a fake bearer; the transport answers 200 locally."""
    _, fcm = mods
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fcm.googleapis.com"
        assert request.url.path == "/v1/projects/test-project/messages:send"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"name": "projects/x/messages/1"})

    def client(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _RealClient(*a, **kw)

    monkeypatch.setattr(fcm, "_service_account",
                        lambda: {"project_id": "test-project",
                                 "client_email": "x@y", "private_key": "k"})

    async def _tok(sa):
        return "fake-bearer"
    monkeypatch.setattr(fcm, "_access_token", _tok)
    monkeypatch.setattr(fcm.httpx, "AsyncClient", client)
    return bodies


@pytest.fixture
def android_box(mods, monkeypatch):
    """One Android token, no iOS path, this box's server id stamped."""
    apns, _ = mods
    async def _tokens():
        return [{"token": "fcm-token-abcdef123456", "platform": "android"}]

    async def _no_relay():
        return None, None

    async def _sid():
        return SID
    monkeypatch.setattr(apns.db, "list_push_tokens", _tokens)
    monkeypatch.setattr(apns, "effective_relay", _no_relay)
    monkeypatch.setattr(apns, "current_server_id", _sid)


@pytest.mark.parametrize("tier", ["info", "watch", "major", "warning"])
async def test_fcm_message_is_data_only_with_route_site_and_tier(
        mods, captured, android_box, tier):
    apns, _ = mods
    res = await apns.send_to_all("Report ready", "Your morning report",
                                 route="report/12", tier=tier)
    assert res["sent"] == 1
    assert captured == [{"message": {
        "token": "fcm-token-abcdef123456",
        "data": {
            "title": "Report ready",
            "body": "Your morning report",
            "route": "report/12",
            "server_id": SID,
            "tier": tier,
        },
        "android": {"priority": "high"},
    }}]
    msg = captured[0]["message"]
    # No notification block anywhere: with one, a background push is drawn
    # by the system on the default channel and never reaches the app.
    assert "notification" not in msg
    assert "notification" not in msg["android"]
    # FCM rejects a data map with non-string values.
    assert all(isinstance(v, str) for v in msg["data"].values())


async def test_fcm_message_omits_what_it_does_not_have(mods, captured, android_box,
                                                       monkeypatch):
    """No route, no server id yet, an unknown tier: the keys are absent,
    never null or "None", and the push still goes."""
    apns, _ = mods
    async def _none():
        return None
    monkeypatch.setattr(apns, "current_server_id", _none)
    res = await apns.send_to_all("T", "B", tier="someday")
    assert res["sent"] == 1
    assert captured[0]["message"]["data"] == {"title": "T", "body": "B"}


def test_fcm_message_validates_route_and_server_id_like_apns(mods):
    """One vocabulary on both platforms: the APNs validators decide."""
    apns, fcm = mods
    m = fcm.build_message("tok", "T", "B", route="https://evil.example/x",
                          server_id="../../etc", tier="WARNING")
    assert m["message"]["data"] == {"title": "T", "body": "B"}
    m = fcm.build_message("tok", "T", "B", route="rule/3", server_id=SID,
                          tier="warning")
    ios = apns.build_payload("T", "B", route="rule/3", server_id=SID)
    assert m["message"]["data"]["route"] == ios["route"]
    assert m["message"]["data"]["server_id"] == ios["server_id"]


def test_fcm_tiers_cover_every_tone_tier(mods):
    """A tier that picks a tone on iOS must reach Android too."""
    apns, fcm = mods
    assert set(apns.SOUND_FOR_TIER) <= set(fcm.PUSH_TIERS)
