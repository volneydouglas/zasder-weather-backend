"""2.1 hardening batch (W1): Ecowitt warm-up off the boot path (R18 #6),
webhook delivery re-resolves and pins (R17), X-Ingest-Token redacted
from captures (R17). The relay-challenge tests live in test_relay.py so
the public-mirror strip removes them with the relay."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

import pytest

H = {"Authorization": "Bearer test-api-token"}


# ───────────────────────── Ecowitt warm-up ─────────────────────────

class _HangingClient:
    """A vendor that neither answers nor refuses."""
    def __init__(self):
        self.calls = 0

    async def list_devices(self):
        self.calls += 1
        await asyncio.Event().wait()

    async def history(self, mac, start, end):
        self.calls += 1
        await asyncio.Event().wait()

    async def real_time(self, mac):
        await asyncio.Event().wait()

    async def aclose(self):
        pass


class _WorkingClient:
    def __init__(self):
        self.real_time_calls = 0

    async def list_devices(self):
        return [{"mac": "AA:BB:CC:DD:EE:01", "name": "Yard", "type": 1,
                 "stationtype": "GW2000", "latitude": 33.3, "longitude": -111.8}]

    async def history(self, mac, start, end):
        return {}                                     # nothing to backfill

    async def real_time(self, mac):
        self.real_time_calls += 1
        return {}

    async def aclose(self):
        pass


def test_a_hanging_vendor_no_longer_holds_start_or_stop(client, monkeypatch):
    """R18 #6: start() used to await discovery and a per-device history
    call, each with a 15 s timeout, so a dead ecowitt.net held the whole
    lifespan and the settings PUT. Now start() returns at once with the
    source reported as starting, and stop() cancels the stuck warm-up."""
    from app import source_status
    from app.ecowitt_cloud_poller import EcowittCloudPoller, SOURCE

    async def run():
        vendor = _HangingClient()
        poller = EcowittCloudPoller(vendor, 60, ["AA:BB:CC:DD:EE:01"])
        t0 = time.monotonic()
        await poller.start()
        started_in = time.monotonic() - t0
        await asyncio.sleep(0.05)                    # let the task reach the vendor
        state = next(s for s in source_status.snapshot() if s["name"] == SOURCE)
        # snapshot() hands out the live extra dict; read it NOW, before
        # stop() flips it.
        phase = (state["configured"], state["extra"]["state"])
        t1 = time.monotonic()
        await poller.stop()
        stopped_in = time.monotonic() - t1
        return started_in, phase, stopped_in, vendor.calls, poller.warm.is_set()
    started_in, phase, stopped_in, calls, warm = asyncio.run(run())
    assert started_in < 0.5
    assert phase == (True, "starting")
    assert calls >= 1, "the warm-up never ran in the background"
    assert stopped_in < 1.0, f"stop waited on the vendor: {stopped_in:.1f}s"
    assert warm is True


def test_the_poll_loop_waits_for_discovery_then_polls(client, monkeypatch):
    """Ordering preserved: discovery and bootstrap complete (warm set)
    BEFORE the first real-time poll, and source_status flips to polling."""
    from app import source_status
    from app.ecowitt_cloud_poller import EcowittCloudPoller, SOURCE

    async def run():
        vendor = _WorkingClient()
        poller = EcowittCloudPoller(vendor, 60, None)
        await poller.start()
        await asyncio.wait_for(poller.warm.wait(), timeout=5)
        devices = dict(poller._devices)
        await asyncio.sleep(0.05)
        state = next(s for s in source_status.snapshot() if s["name"] == SOURCE)
        polled = vendor.real_time_calls
        await poller.stop()
        return devices, state, polled
    devices, state, polled = asyncio.run(run())
    assert "AA:BB:CC:DD:EE:01" in devices
    assert state["extra"]["state"] == "polling"
    assert polled >= 1


def test_the_settings_probe_has_a_budget(client, monkeypatch):
    """The PUT answers inside PROBE_BUDGET_S even when the vendor hangs:
    the values are saved and the response says the check is pending."""
    from app import integrations

    async def hang(provider):
        await asyncio.Event().wait()
    monkeypatch.setattr(integrations, "_probe", hang)
    monkeypatch.setattr(integrations, "PROBE_BUDGET_S", 0.05)
    t0 = time.monotonic()
    note = asyncio.run(integrations.probe("ecowitt-cloud"))
    assert time.monotonic() - t0 < 1.0
    assert note and "did not answer" in note


# ───────────────────────── webhook SSRF ─────────────────────────

@pytest.mark.parametrize("addr,blocked", [
    ("100.64.1.1", True),          # CGNAT: is_private says False
    ("100.127.255.255", True),
    ("100.128.0.1", False),        # just past the /10
    ("::ffff:10.0.0.1", True),     # IPv4-mapped private
    ("::ffff:93.184.216.34", False),
    ("10.0.0.1", True), ("127.0.0.1", True), ("169.254.1.1", True),
    ("0.0.0.0", True), ("93.184.216.34", False), ("2606:2800:220:1::1", False),
])
def test_blocked_addresses(addr, blocked):
    from app import webhooks
    assert webhooks._blocked(ipaddress.ip_address(addr)) is blocked


def _resolver(*answers):
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET6 if ":" in x else socket.AF_INET,
                 socket.SOCK_STREAM, 6, "", (x, 0)) for x in answers]
    return fake_getaddrinfo


def test_pinned_request_posts_to_the_resolved_address(monkeypatch):
    from app import webhooks
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _resolver("93.184.216.34"))
    url, headers, ext = webhooks.pinned_request("https://hooks.example.com:8443/in?x=1")
    assert url == "https://93.184.216.34:8443/in?x=1"
    assert headers == {"Host": "hooks.example.com:8443"}
    assert ext == {"sni_hostname": "hooks.example.com"}


def test_pinned_request_refuses_a_rebound_host(monkeypatch):
    from app import webhooks
    monkeypatch.setattr(webhooks.socket, "getaddrinfo",
                        _resolver("93.184.216.34", "10.0.0.5"))
    with pytest.raises(ValueError, match="private"):
        webhooks.pinned_request("https://hooks.example.com/in")
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _resolver("100.64.0.9"))
    with pytest.raises(ValueError, match="private"):
        webhooks.validate_webhook_url("https://hooks.example.com/in")


class _FakeAsyncClient:
    calls: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        _FakeAsyncClient.calls.append((url, kw))

        class R:
            status_code = 200
        return R()


def test_send_resolves_at_delivery_time(client, monkeypatch):
    """Registered while public, rebound to 10.x since: delivery must
    refuse. Registered and still public: delivery goes to the pinned IP
    with the hostname in Host and as the TLS server name."""
    from app import webhooks, db
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeAsyncClient)
    stamped: list = []

    async def fake_stamp(hook_id, ok_ms, err):
        stamped.append((hook_id, ok_ms, err))
    monkeypatch.setattr(db, "stamp_webhook", fake_stamp)
    hook = {"id": 7, "url": "https://hooks.example.com/in", "secret": "s"}

    monkeypatch.setattr(webhooks, "RETRY_DELAY_S", 0.0)

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _resolver("10.0.0.5"))
    asyncio.run(webhooks._send(hook, b"{}"))
    assert _FakeAsyncClient.calls == [], "delivered to a private address"
    assert stamped and stamped[-1][2] and "private" in stamped[-1][2]

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _resolver("93.184.216.34"))
    asyncio.run(webhooks._send(hook, b"{}"))
    assert len(_FakeAsyncClient.calls) == 1
    url, kw = _FakeAsyncClient.calls[0]
    assert url == "https://93.184.216.34/in"
    assert kw["headers"]["Host"] == "hooks.example.com"
    assert kw["extensions"] == {"sni_hostname": "hooks.example.com"}
    assert stamped[-1][2] is None


# ───────────────────────── capture redaction ─────────────────────────

def test_ingest_token_header_is_redacted_from_captures():
    from app import capture
    out = capture._redact_dict({"X-Ingest-Token": "zwi_secret", "Accept": "*/*",
                                "Authorization": "Bearer x"},
                               capture._REDACT_HEADERS)
    assert out == {"X-Ingest-Token": "<redacted>", "Accept": "*/*",
                   "Authorization": "<redacted>"}


def test_a_stalled_resolver_is_abandoned_and_the_failure_recorded(client, monkeypatch):
    """CodeRabbit, PR #36: the resolver runs in a thread with no deadline,
    so a hung DNS server held _send (and the dispatch gather) open. The
    wait is bounded by RESOLVE_TIMEOUT_S; the attempt fails and is
    stamped like any other transport failure."""
    import asyncio
    import threading
    from app import db, webhooks
    stamped = []

    async def fake_stamp(hook_id, ok_ms, err):
        stamped.append((hook_id, ok_ms, err))
    monkeypatch.setattr(db, "stamp_webhook", fake_stamp)
    monkeypatch.setattr(webhooks, "RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(webhooks, "RESOLVE_TIMEOUT_S", 0.05)
    release = threading.Event()

    def hang(*a, **k):
        release.wait(2.0)
        raise OSError("resolver never answered")
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", hang)
    hook = {"id": 9, "url": "https://hooks.example.com/in", "secret": "s"}
    try:
        asyncio.run(asyncio.wait_for(webhooks._send(hook, b"{}"), timeout=2.0))
    finally:
        release.set()
    assert stamped and stamped[-1][1] is None and stamped[-1][2], \
        "the stalled attempt must be recorded as a failure"


def test_delivery_ignores_environment_proxies(client, monkeypatch):
    """With HTTPS_PROXY set, httpx would tunnel through the proxy with the
    pinned IP as the TLS server name; the webhook client is built with
    trust_env=False so the pinned direct delivery keeps its SNI."""
    import asyncio
    from app import db, webhooks
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, **kw):
            class R:
                status_code = 200
            return R()

    async def fake_stamp(*a):
        pass
    monkeypatch.setattr(db, "stamp_webhook", fake_stamp)
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _resolver("93.184.216.34"))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    asyncio.run(webhooks._send({"id": 1, "url": "https://hooks.example.com/in",
                                "secret": "s"}, b"{}"))
    assert seen.get("trust_env") is False
