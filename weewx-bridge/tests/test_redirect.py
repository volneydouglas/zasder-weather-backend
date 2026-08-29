"""The redirect refusal, pinned against a REAL redirect (R14, 4th-round
carry): urllib's default handler re-sends the Authorization header to
wherever a 30x points — a cross-host redirect would hand the ingest token
to a third party. The bridge's opener must surface the 302 as HTTPError
and the redirect target must never see a request."""
from __future__ import annotations

import http.server
import os
import sys
import threading
import urllib.error

import pytest

BIN = os.path.join(os.path.dirname(__file__), "..", "bin", "user")
sys.path.insert(0, BIN)

import zasder  # noqa: E402


class _Server:
    """A localhost HTTP server on an ephemeral port, torn down per test."""

    def __init__(self, handler_cls):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_a_302_raises_and_the_token_never_follows():
    evil_hits = []

    class Evil(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            evil_hits.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    evil = _Server(Evil)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{evil.port}/ingest/custom")
            self.end_headers()

        def log_message(self, *a):
            pass

    origin = _Server(Redirector)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            zasder.post_payload(f"http://127.0.0.1:{origin.port}",
                                "zwi_secret_token",
                                {"device_id": "weewx-1", "tempf": 72.0},
                                timeout=5)
        assert exc.value.code == 302
        assert evil_hits == [], (
            "the redirect target received a request — the Authorization "
            "header followed the 302")
    finally:
        origin.close()
        evil.close()


def test_a_200_still_posts_normally():
    """The refusal must not break the happy path."""
    seen = []

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = _Server(Ok)
    try:
        status = zasder.post_payload(f"http://127.0.0.1:{srv.port}",
                                     "zwi_secret_token",
                                     {"device_id": "weewx-1"}, timeout=5)
        assert status == 200
        assert seen == ["Bearer zwi_secret_token"]
    finally:
        srv.close()


# ── the cleartext-token policy (CodeRabbit CWE-319) ─────────────────────

def test_http_to_a_routable_host_is_refused_before_the_token_attaches():
    """A typo'd http:// against a real internet host must raise from
    build_request itself — no Request object, no header, no send."""
    for url in ("http://example.com", "http://8.8.8.8:8080"):
        with pytest.raises(ValueError):
            zasder.build_request(url, "zwi_secret", {"tempf": 70.0})


def test_http_to_private_lan_hosts_still_works():
    """The documented local-Docker deployment: plain http is fine on the
    user's own network (loopback, RFC1918, mDNS/.local names)."""
    for url in ("http://127.0.0.1:8080", "http://192.168.1.40:8080",
                "http://10.0.0.5", "http://localhost:8080",
                "http://weewx-box.local:8080", "https://example.com"):
        req = zasder.build_request(url, "zwi_secret", {"tempf": 70.0})
        assert req.get_header("Authorization") == "Bearer zwi_secret"
