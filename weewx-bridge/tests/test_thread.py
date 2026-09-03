"""The WeeWX-side thread classes, exercised against a fake weewx package.

The real bug class here (R11 V1): process_record calling urlopen directly
means one URLError is a *generic* exception to RESTThread.run_loop, which
terminates the uploader thread forever. These tests pin that uploads go
through post_with_retries (whose FailedPost run_loop survives) and that
get_record's archive augmentation actually reaches the payload (R11 V7).
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import types

BIN = os.path.join(os.path.dirname(__file__), "..", "bin", "user")
sys.path.insert(0, BIN)


def _fake_weewx():
    """Install a minimal fake weewx into sys.modules; returns the recorder
    list post_with_retries appends to."""
    posted = []

    weewx = types.ModuleType("weewx")
    weewx.NEW_ARCHIVE_RECORD = object()

    restx = types.ModuleType("weewx.restx")

    class RESTThread:
        def __init__(self, q, protocol_name=None, manager_dict=None,
                     timeout=None, max_backlog=None):
            self.manager_dict = manager_dict
            self.timeout = timeout

        def get_record(self, record, dbmanager):
            # Upstream augments archive-derived sums when a dbmanager is
            # around; mimic the dayRain augmentation.
            out = dict(record)
            if dbmanager is not None:
                out.setdefault("dayRain", 0.55)
            return out

        def post_with_retries(self, request, data=None):
            posted.append(request)

        def start(self):
            pass

    class StdRESTbase:
        def __init__(self, engine, cfg_dict):
            pass

        def bind(self, *a):
            pass

    restx.RESTThread = RESTThread
    restx.StdRESTbase = StdRESTbase

    units = types.ModuleType("weewx.units")
    units.to_US = lambda r: r
    manager = types.ModuleType("weewx.manager")
    manager.get_manager_dict_from_config = lambda cfg, binding: {"fake": 1}

    weeutil = types.ModuleType("weeutil")
    weeutil_weeutil = types.ModuleType("weeutil.weeutil")
    weeutil_weeutil.to_int = int
    weeutil.weeutil = weeutil_weeutil

    sys.modules.update({
        "weewx": weewx,
        "weewx.restx": restx,
        "weewx.units": units,
        "weewx.manager": manager,
        "weeutil": weeutil,
        "weeutil.weeutil": weeutil_weeutil,
    })
    weewx.restx = restx
    weewx.units = units
    weewx.manager = manager
    return posted


def _reload_zasder():
    import zasder
    return importlib.reload(zasder)


def teardown_module(module):
    # Leave zasder in its no-weewx shape for the transform tests, whatever
    # order pytest ran the files in.
    for name in ("weewx", "weewx.restx", "weewx.units", "weewx.manager",
                 "weeutil", "weeutil.weeutil"):
        sys.modules.pop(name, None)
    _reload_zasder()


def test_process_record_posts_through_retry_machinery():
    posted = _fake_weewx()
    zasder = _reload_zasder()
    t = zasder.ZasderThread(
        None, server_url="https://wx.example/", ingest_token="tok-1",
        device_id="patio", station_name="Patio",
        timeout=15, max_backlog=100)
    t.process_record({"dateTime": 1_787_155_200, "outTemp": 100.5},
                     dbmanager=object())
    assert len(posted) == 1, "upload must go through post_with_retries"
    req = posted[0]
    assert req.full_url == "https://wx.example/ingest/custom"
    assert req.get_header("Authorization") == "Bearer tok-1"
    body = json.loads(req.data.decode())
    assert body["outdoor"]["tempf"] == 100.5
    # get_record's archive augmentation reached the payload: the driver
    # never emitted dayRain, the fake dbmanager supplied it (R11 V7).
    assert body["rain"]["daily_in"] == 0.55


def test_timestampless_record_posts_nothing():
    posted = _fake_weewx()
    zasder = _reload_zasder()
    t = zasder.ZasderThread(
        None, server_url="https://wx.example", ingest_token="tok-1",
        device_id="patio", station_name=None,
        timeout=15, max_backlog=100)
    t.process_record({"outTemp": 100.5}, dbmanager=None)
    assert posted == []


def test_service_passes_manager_dict_to_thread():
    _fake_weewx()
    zasder = _reload_zasder()
    svc = zasder.Zasder(engine=None, cfg_dict={
        "StdRESTful": {"Zasder": {
            "server_url": "https://wx.example",
            "ingest_token": "tok-1",
        }},
    })
    assert svc.archive_thread.manager_dict == {"fake": 1}


def test_init_refuses_cleartext_routable_urls_before_thread_start():
    """R16 finding 1: the init gate must be urlsplit-normalized like
    build_request — "HTTP://" (case), ftp://, and malformed URLs all used
    to slip past a startswith check and then kill the uploader thread on
    the first record (the R11 V1 shape). Refused = no archive_thread."""
    _fake_weewx()
    zasder = _reload_zasder()
    for bad in ("http://example.com", "HTTP://example.com",
                "HTTP://EXAMPLE.COM", "ftp://192.168.1.5",
                "not a url", ""):
        svc = zasder.Zasder(engine=None, cfg_dict={
            "StdRESTful": {"Zasder": {
                "server_url": bad, "ingest_token": "tok-1"}}})
        assert not hasattr(svc, "archive_thread"), \
            f"{bad!r} must refuse at init, not die per-record"


def test_init_accepts_https_and_private_http_case_insensitively():
    _fake_weewx()
    zasder = _reload_zasder()
    for ok in ("https://wx.example", "HTTPS://wx.example",
               "http://192.168.1.40:8080", "HTTP://192.168.1.40:8080"):
        svc = zasder.Zasder(engine=None, cfg_dict={
            "StdRESTful": {"Zasder": {
                "server_url": ok, "ingest_token": "tok-1"}}})
        assert hasattr(svc, "archive_thread"), f"{ok!r} must start"


def test_cleartext_allowlist_covers_every_private_family():
    """R16: only .local and two IPv4 ranges were exercised. Pin the whole
    allowlist — RFC1918 x3, loopback, link-local, IPv6 loopback/ULA/
    link-local, and every LAN name suffix — plus the refusals beside
    them."""
    _fake_weewx()
    zasder = _reload_zasder()
    for host in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.254",
                 "192.168.0.1", "169.254.10.10", "::1", "fd12::1",
                 "fe80::1", "localhost", "pi.local", "nas.lan",
                 "box.home.arpa", "srv.internal"):
        assert zasder._cleartext_host_allowed(host), host
    for host in ("172.32.0.1", "8.8.8.8", "example.com", "2001:4860::1",
                 None, ""):
        assert not zasder._cleartext_host_allowed(host), host
    # R17 #4: CGNAT (Tailscale's 100.64.0.0/10) is NOT private to Python's
    # ipaddress on any supported version — the README once claimed 3.13+
    # accepted it, which sent users chasing a fix that does not exist. Pin
    # the refusal so the doc and the code cannot drift apart again.
    for host in ("100.64.0.1", "100.100.100.100", "100.127.255.254"):
        assert not zasder._cleartext_host_allowed(host), host
