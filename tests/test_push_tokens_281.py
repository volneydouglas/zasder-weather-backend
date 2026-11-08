"""2.8.1: an Android (FCM) token filed as iOS sank every relay batch.

Found at the 2.8.0 release (11-07): Mesa Arts held three, Atlas one, from
an Android build of mid-October 2026 that registered without a platform.
The hosted relay refused each batch with 422 "device tokens must be hex",
the client then retried it as an "older relay" and failed again, and an
undeliverable storm summary retried every minute for days.
"""
import asyncio

import pytest

H = {"Authorization": "Bearer test-api-token"}
APNS = "a" * 64
APNS2 = "b" * 64
# The shape of a real FCM registration token: instance id, ':', then a
# long base64url body (not a real token).
FCM = "cXyZ12AbCdE:APA91b" + "Hk-9_q" * 21


def _fcm_token(i: int) -> str:
    return f"dEvIcE{i:05d}:APA91b" + "Zz-8_w" * 21


# ── the shapes ───────────────────────────────────────────────────────

def test_the_shapes_tell_the_two_apart(client):
    from app import apns
    assert apns.is_apns_token(APNS) and not apns.looks_like_fcm(APNS)
    assert apns.looks_like_fcm(FCM) and not apns.is_apns_token(FCM)
    assert not apns.is_apns_token("short") and not apns.looks_like_fcm("short")


# ── the relay client (every server) ──────────────────────────────────

def _capture_relay(monkeypatch, respond):
    """Route the client's httpx calls to `respond(json_body) -> Response`."""
    import httpx
    import app.apns as apns
    posts = []
    real = httpx.AsyncClient

    def handler(request):
        import json
        body = json.loads(request.content)
        posts.append(body)
        return respond(body)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(apns.settings, "apns_env", "production")
    return posts


def test_the_client_never_puts_a_foreign_token_in_a_relay_batch(client, monkeypatch):
    import httpx
    from app import apns
    posts = _capture_relay(monkeypatch, lambda b: httpx.Response(
        200, json={"sent": len(b["tokens"]), "dead": [], "failed": 0}))
    res = asyncio.run(apns._push_via_relay([APNS, FCM], "t", "b",
                                           "https://relay.example/push", "rk",
                                           la_payload=None))
    assert [p["tokens"] for p in posts] == [[APNS]]
    assert res == {"sent": 1, "dead": [], "failed": 1}


def test_the_client_sends_more_than_fifty_tokens_in_slices(client, monkeypatch):
    import httpx
    from app import apns
    posts = _capture_relay(monkeypatch, lambda b: httpx.Response(
        200, json={"sent": len(b["tokens"]), "dead": [], "failed": 0}))
    toks = [f"{i:064x}" for i in range(120)]
    res = asyncio.run(apns._push_via_relay(toks, "t", "b",
                                           "https://relay.example/push", "rk",
                                           la_payload=None))
    assert [len(p["tokens"]) for p in posts] == [50, 50, 20]
    assert res["sent"] == 120 and res["failed"] == 0


def test_a_422_about_the_tokens_is_not_retried_as_an_older_relay(client, monkeypatch):
    import httpx
    from app import apns
    posts = _capture_relay(monkeypatch, lambda b: httpx.Response(422, json={
        "detail": [{"type": "value_error", "loc": ["body", "tokens"],
                    "msg": "Value error, device tokens must be hex"}]}))
    res = asyncio.run(apns._push_via_relay([APNS], "t", "b",
                                           "https://relay.example/push", "rk",
                                           la_payload=None, route="report/1",
                                           server_id="abcdef0123456789"))
    assert len(posts) == 1
    assert res["sent"] == 0 and res["failed"] == 1


def test_a_422_about_an_unknown_field_is_still_retried_plain(client, monkeypatch):
    import httpx
    from app import apns
    calls = {"n": 0}

    def respond(b):
        calls["n"] += 1
        if "route" in b:
            return httpx.Response(422, json={"detail": [
                {"type": "extra_forbidden", "loc": ["body", "route"]}]})
        return httpx.Response(200, json={"sent": 1, "dead": [], "failed": 0})
    _capture_relay(monkeypatch, respond)
    res = asyncio.run(apns._push_via_relay([APNS], "t", "b",
                                           "https://relay.example/push", "rk",
                                           la_payload=None, route="report/1"))
    assert calls["n"] == 2 and res["sent"] == 1


def test_send_to_all_routes_an_fcm_token_filed_as_ios_to_fcm(client, monkeypatch):
    import app.apns as apns
    import app.fcm as fcm
    from app import db

    async def seed():
        await db.init_db()
        await db.register_push_token(APNS, "ios", "production")
        async with db.connect() as conn:            # the old, mislabeled row
            await conn.execute(
                "INSERT INTO push_tokens (token, platform, env, created_ms, "
                "last_seen_ms) VALUES (?, 'ios', NULL, 1, 1)", (FCM,))
            await conn.commit()
    asyncio.run(seed())
    monkeypatch.setattr(apns.settings, "apns_relay_url", "https://relay.example/push")
    monkeypatch.setattr(apns.settings, "apns_relay_token", "rtok")
    monkeypatch.setattr(apns.settings, "apns_env", "production")
    relayed, fcmd = [], []

    async def fake_relay(tokens, *a, **kw):
        relayed.extend(tokens)
        return {"sent": len(tokens), "dead": [], "failed": 0}

    async def fake_fcm(tokens, *a, **kw):
        fcmd.extend(tokens)
        return {"sent": len(tokens), "dead": [], "failed": 0}
    monkeypatch.setattr(apns, "_push_via_relay", fake_relay)
    monkeypatch.setattr(fcm, "fcm_configured", lambda: True)
    monkeypatch.setattr(fcm, "push_tokens_fcm", fake_fcm)
    res = asyncio.run(apns.send_to_all("T", "B"))
    assert relayed == [APNS] and fcmd == [FCM]
    assert res["sent"] == 2


# ── the rows ─────────────────────────────────────────────────────────

def test_a_platformless_fcm_registration_is_filed_as_android(client):
    from app import db
    r = client.post("/api/push/register", headers=H, json={"token": FCM})
    assert r.status_code == 200
    rows = asyncio.run(db.list_push_tokens())
    assert [(t["token"], t["platform"]) for t in rows] == [(FCM, "android")]


def test_boot_refiles_the_old_rows_and_leaves_real_ios_alone(client):
    """From the OLD data: rows written before 2.8.1, then a boot."""
    from app import db

    async def run():
        await db.init_db()
        async with db.connect() as conn:
            for tok in (APNS, APNS2, _fcm_token(1), _fcm_token(2)):
                await conn.execute(
                    "INSERT INTO push_tokens (token, platform, env, created_ms, "
                    "last_seen_ms) VALUES (?, 'ios', NULL, 1, 1)", (tok,))
            await conn.commit()
        await db.init_db()                       # the 2.8.1 boot
        await db.init_db()                       # and again: idempotent
        return {t["token"]: t["platform"] for t in await db.list_push_tokens()}
    got = asyncio.run(run())
    assert got[APNS] == "ios" and got[APNS2] == "ios"
    assert got[_fcm_token(1)] == "android" and got[_fcm_token(2)] == "android"


# ── the storm summary that could never go out ────────────────────────

def test_a_long_quiet_window_still_gets_its_delivery_attempts(client, monkeypatch):
    """With a 6 h quiet window the storm only becomes reportable 6 h after
    the rain; the give-up counts from there, not from the rain."""
    import app.alerts as A
    from app import db
    mac = "AA:BB:CC:DD:EE:83"
    tried = []

    async def deliver(*a, **kw):
        tried.append(1)
        return True
    monkeypatch.setattr(A, "_deliver", deliver)

    async def fake_stats(mac, start_ms, end_ms, counter_col):
        return {"total_in": 0.5, "peak_rate_in_hr": 0.3, "min_tempf": 66.0,
                "max_tempf": 73.0, "max_gust_mph": 12.0}
    monkeypatch.setattr(A.db, "storm_window_stats", fake_stats)

    class _Cfg:
        storm_summary = True
        storm_quiet_minutes = 360
        storm_min_total_in = 0.05
        email_scope = "device_down"
        storm_channels = "push"

    now = 1_787_000_000_000
    last_rain = now - 7 * 3_600_000
    device = {"mac": mac, "name": "Yard",
              "lastData": {"dailyrainin": 0.0, "dateutc": now}}

    async def run():
        await db.init_db()
        await db.upsert_storm_state(mac, last_rain - 3_600_000, last_rain,
                                    "dailyrainin", 0.5, now)
        await A.AlertMonitor()._check_storm_summaries(_Cfg(), [device], now, {})
    asyncio.run(run())
    assert tried == [1]


@pytest.mark.parametrize("quiet_hours,closes", [(3, False), (7, True)])
def test_an_undeliverable_storm_summary_stops_retrying(client, monkeypatch,
                                                       quiet_hours, closes):
    """Before 2.8.1 a failed delivery retried every minute forever. It
    still retries while the summary is fresh, and past six hours after
    the rain stopped the storm closes with its record kept."""
    import app.alerts as A
    from app import db
    mac = "AA:BB:CC:DD:EE:81"

    async def fail_deliver(*a, **kw):
        return False
    monkeypatch.setattr(A, "_deliver", fail_deliver)

    async def fake_stats(mac, start_ms, end_ms, counter_col):
        return {"total_in": 0.5, "peak_rate_in_hr": 0.3, "min_tempf": 66.0,
                "max_tempf": 73.0, "max_gust_mph": 12.0}
    monkeypatch.setattr(A.db, "storm_window_stats", fake_stats)

    class _Cfg:
        storm_summary = True
        storm_quiet_minutes = 30
        storm_min_total_in = 0.05
        email_scope = "device_down"
        storm_channels = "push"

    now = 1_787_000_000_000
    last_rain = now - quiet_hours * 3_600_000
    device = {"mac": mac, "name": "Yard",
              "lastData": {"dailyrainin": 0.0, "dateutc": now}}

    async def run():
        await db.init_db()
        await db.upsert_storm_state(mac, last_rain - 3_600_000, last_rain,
                                    "dailyrainin", 0.5, now)
        await A.AlertMonitor()._check_storm_summaries(_Cfg(), [device], now, {})
        state = await db.get_storm_state(mac)
        return state, await db.list_storms(mac)
    state, storms = asyncio.run(run())
    still_open = bool(state and state.get("started_ms"))
    assert still_open is (not closes)
    assert (len(storms) == 1) is closes


def test_a_given_up_storm_is_recorded_once_and_never_pushed(client, monkeypatch):
    """The give-up close is decided before any delivery attempt, so a
    retry never sends the stale push; a close that failed after its record
    was written does not record the storm twice (CodeRabbit, PR #148)."""
    import app.alerts as A
    from app import db
    mac = "AA:BB:CC:DD:EE:82"
    attempts = []

    async def deliver(*a, **kw):
        attempts.append(1)
        return True
    monkeypatch.setattr(A, "_deliver", deliver)

    async def fake_stats(mac, start_ms, end_ms, counter_col):
        return {"total_in": 0.5, "peak_rate_in_hr": 0.3, "min_tempf": 66.0,
                "max_tempf": 73.0, "max_gust_mph": 12.0}
    monkeypatch.setattr(A.db, "storm_window_stats", fake_stats)
    real_upsert = db.upsert_storm_state
    fail_once = {"n": 1}

    async def flaky_upsert(*a, **kw):
        if a[1] is None and fail_once["n"]:
            fail_once["n"] -= 1
            raise RuntimeError("disk hiccup")
        return await real_upsert(*a, **kw)

    class _Cfg:
        storm_summary = True
        storm_quiet_minutes = 30
        storm_min_total_in = 0.05
        email_scope = "device_down"
        storm_channels = "push"

    now = 1_787_000_000_000
    last_rain = now - 7 * 3_600_000
    device = {"mac": mac, "name": "Yard",
              "lastData": {"dailyrainin": 0.0, "dateutc": now}}

    async def run():
        await db.init_db()
        await db.upsert_storm_state(mac, last_rain - 3_600_000, last_rain,
                                    "dailyrainin", 0.5, now)
        monkeypatch.setattr(A.db, "upsert_storm_state", flaky_upsert)
        mon = A.AlertMonitor()
        try:
            await mon._check_storm_summaries(_Cfg(), [device], now, {})
        except RuntimeError:
            pass
        # The record is written BEFORE the close: it is there although the
        # close failed (CodeRabbit, PR #148).
        after_first = await db.list_storms(mac)
        await mon._check_storm_summaries(_Cfg(), [device], now + 60_000, {})
        return after_first, await db.get_storm_state(mac), await db.list_storms(mac)
    after_first, state, storms = asyncio.run(run())
    assert len(after_first) == 1
    assert attempts == []
    assert len(storms) == 1
    assert not (state and state.get("started_ms"))
