"""Last-mile delivery tests (1.9 test debt, TEST_GAP_AUDIT Tier 1).

The two transports every alert ultimately rides were never executed by
any test: `_send_sync` (SMTP) was always monkeypatched away, and webhook
`_send` likewise — email alerting or webhook signing could die and the
suite would stay green. These tests run the REAL functions against fakes
at the protocol boundary: a recording smtplib stand-in, and httpx's
MockTransport under the genuine client."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace

import httpx

os.environ.setdefault("API_TOKEN", "test-api-token-0123456789abcdef0123")


# ───────────────────────── SMTP (_send_sync) ─────────────────────────

class _FakeSMTP:
    """Records the exact call sequence _send_sync makes."""
    last: "_FakeSMTP | None" = None

    def __init__(self, host, port, timeout=None, context=None):
        _FakeSMTP.last = self       # always the base attr, even for SSL
        self.host, self.port = host, port
        self.context = context
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, context=None):
        self.calls.append(("starttls", context is not None))

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def send_message(self, msg):
        self.calls.append(("send", msg))


class _FakeSMTPSSL(_FakeSMTP):
    pass


def _smtp_cfg(**over):
    base = dict(smtp_host="mail.example.com", smtp_port=587,
                smtp_username="alerts@example.com", smtp_password="hunter2",
                smtp_from="", smtp_ssl=False, smtp_tls=True)
    base.update(over)
    return SimpleNamespace(**base)


def _sent_message(fake: _FakeSMTP):
    sends = [c for c in fake.calls if c[0] == "send"]
    assert len(sends) == 1
    return sends[0][1]


def _patch_smtplib(monkeypatch):
    import app.alerts as al
    monkeypatch.setattr(al.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(al.smtplib, "SMTP_SSL", _FakeSMTPSSL)


def test_send_sync_starttls_branch(monkeypatch):
    from app.alerts import _send_sync
    _patch_smtplib(monkeypatch)
    _send_sync("Subj", "Body", ["a@x.com", "b@x.com"], _smtp_cfg())
    s = _FakeSMTP.last
    assert isinstance(s, _FakeSMTP) and not isinstance(s, _FakeSMTPSSL)
    assert (s.host, s.port) == ("mail.example.com", 587)
    # ehlo → starttls(with a real ssl context) → ehlo again → login → send.
    assert [c[0] for c in s.calls] == ["ehlo", "starttls", "ehlo",
                                      "login", "send"]
    assert ("starttls", True) in s.calls
    assert ("login", "alerts@example.com", "hunter2") in s.calls
    msg = _sent_message(s)
    assert msg["Subject"] == "Subj"
    assert msg["To"] == "a@x.com, b@x.com"
    # No explicit From → the username stands in.
    assert msg["From"] == "alerts@example.com"
    assert "Body" in msg.get_content()


def test_send_sync_plain_branch_no_auth(monkeypatch):
    """No TLS, no credentials: a LAN relay setup. Nothing must attempt
    starttls or login — some plain relays drop the connection on either."""
    from app.alerts import _send_sync
    _patch_smtplib(monkeypatch)
    _send_sync("S", "B", ["a@x.com"],
               _smtp_cfg(smtp_tls=False, smtp_username="", smtp_password=""))
    s = _FakeSMTP.last
    assert [c[0] for c in s.calls] == ["ehlo", "send"]
    # Nobody configured a From anywhere → the documented localhost fallback.
    assert _sent_message(s)["From"] == "zasder-weather@localhost"


def test_send_sync_ssl_branch(monkeypatch):
    from app.alerts import _send_sync
    _patch_smtplib(monkeypatch)
    _send_sync("S", "B", ["a@x.com"],
               _smtp_cfg(smtp_ssl=True, smtp_port=465,
                         smtp_from="weather@example.com"))
    s = _FakeSMTP.last
    assert isinstance(s, _FakeSMTPSSL)
    assert s.context is not None          # implicit-SSL context passed
    assert s.port == 465
    assert [c[0] for c in s.calls] == ["login", "send"]
    assert _sent_message(s)["From"] == "weather@example.com"


def test_send_sync_none_password_logs_in_with_empty_string(monkeypatch):
    """A username with a None password must login with '' — smtplib
    raises on None and the send would die before send_message."""
    from app.alerts import _send_sync
    _patch_smtplib(monkeypatch)
    _send_sync("S", "B", ["a@x.com"], _smtp_cfg(smtp_password=None))
    assert ("login", "alerts@example.com", "") in _FakeSMTP.last.calls


# ───────────────────────── webhooks (_send) ─────────────────────────

def _mock_httpx(monkeypatch, handler):
    """Run webhook sends through the REAL httpx client over MockTransport,
    and collapse the retry backoff sleep."""
    import app.webhooks as wh
    real_client = httpx.AsyncClient

    def factory(**kw):
        kw.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(wh.httpx, "AsyncClient", factory)

    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(
        wh, "asyncio",
        SimpleNamespace(sleep=_no_sleep, gather=asyncio.gather))
    return wh


def test_webhook_send_success_signs_and_stamps(client, monkeypatch):
    from app import db
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    wh = _mock_httpx(monkeypatch, handler)

    async def run():
        hook = await db.create_webhook("https://hooks.example.com/zasder")
        ts = int(time.time() * 1000)
        await wh.dispatch_alert("frost", "AA:BB:CC:00:00:01",
                                "Frost tonight", "It is cold", ts,
                                severity="watch")
        assert len(requests) == 1
        req = requests[0]
        body = req.read()
        payload = json.loads(body)
        assert payload == {"event": "alert", "kind": "frost",
                           "mac": "AA:BB:CC:00:00:01",
                           "title": "Frost tonight", "body": "It is cold",
                           "ts_ms": ts, "severity": "watch"}
        # Receiver-side verification: recompute the HMAC over the raw
        # body with the row's secret — the contract the docs promise.
        expect = hmac.new(hook["secret"].encode(), body,
                          hashlib.sha256).hexdigest()
        assert req.headers["X-Zasder-Signature"] == "sha256=" + expect
        assert req.headers["Content-Type"] == "application/json"
        row = (await db.list_webhooks())[0]
        assert row["last_ok_ms"] is not None
        assert row["last_error"] is None

    asyncio.run(run())


def test_webhook_send_retries_once_then_succeeds(client, monkeypatch):
    from app import db
    codes = iter([500, 200])
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        code = next(codes)
        seen.append(code)
        return httpx.Response(code)

    wh = _mock_httpx(monkeypatch, handler)

    async def run():
        await db.create_webhook("https://hooks.example.com/flaky")
        await wh.dispatch_alert("heat", None, "t", "b",
                                int(time.time() * 1000))
        assert seen == [500, 200]
        row = (await db.list_webhooks())[0]
        assert row["last_ok_ms"] is not None and row["last_error"] is None

    asyncio.run(run())


def test_webhook_send_two_failures_stamp_the_error(client, monkeypatch):
    from app import db
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("connection refused")

    wh = _mock_httpx(monkeypatch, handler)

    async def run():
        await db.create_webhook("https://hooks.example.com/dead")
        await wh.dispatch_alert("nws", None, "t", "b",
                                int(time.time() * 1000))
        assert attempts["n"] == 2        # exactly one retry, then give up
        row = (await db.list_webhooks())[0]
        assert row["last_ok_ms"] is None
        assert "connection refused" in (row["last_error"] or "")

    asyncio.run(run())


def test_webhook_http_error_body_never_marks_ok(client, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    wh = _mock_httpx(monkeypatch, handler)
    from app import db

    async def run():
        await db.create_webhook("https://hooks.example.com/gone")
        await wh.dispatch_alert("frost", None, "t", "b",
                                int(time.time() * 1000))
        row = (await db.list_webhooks())[0]
        assert row["last_ok_ms"] is None
        assert row["last_error"] == "HTTP 404"

    asyncio.run(run())
