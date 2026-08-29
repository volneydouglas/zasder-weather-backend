"""Zasder Weather uploader for WeeWX — POSTs each archive record to your
Zasder Weather server's /ingest/custom.

WeeWX speaks to ~70 station families this backend never will; this ~100
lines makes every one of them a Zasder source. Configure in weewx.conf:

    [StdRESTful]
        [[Zasder]]
            server_url = https://your-server.fly.dev
            ingest_token = <your ingest token, from the app's Data tab>
            # optional:
            # station_name = Backyard Vantage
            # device_id = weewx        # storage key; keep it stable

Records are converted to US units (the Zasder storage convention) before
mapping, whatever unit system your database uses. Absent fields are
omitted, never sent as zero.

The transform below is pure and import-safe without WeeWX so the bridge's
tests run anywhere; the WeeWX service classes only exist when WeeWX is
importable (i.e. on the machine actually running it).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

VERSION = "1.0.0"


def _num(record, key):
    v = record.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _block(pairs):
    """Dict of the non-None pairs; None when nothing survived — so the
    payload simply omits blocks the station doesn't report."""
    out = {k: v for k, v in pairs.items() if v is not None}
    return out or None


def record_to_payload(record, device_id="weewx", station_name=None):
    """One WeeWX archive record (ALREADY in US units) → the /ingest/custom
    payload. Returns None when the record has no usable timestamp."""
    ts = record.get("dateTime")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    payload = {
        "device": {"id": device_id, "model": "WeeWX"},
        "source": "weewx",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(int(ts))),
    }
    if station_name:
        payload["device"]["name"] = station_name
    blocks = {
        "outdoor": _block({"tempf": _num(record, "outTemp"),
                           "humidity": _num(record, "outHumidity"),
                           "dew_point_f": _num(record, "dewpoint"),
                           "uv": _num(record, "UV"),
                           "solar_wm2": _num(record, "radiation")}),
        "indoor": _block({"tempf": _num(record, "inTemp"),
                          "humidity": _num(record, "inHumidity")}),
        "wind": _block({"speed_mph": _num(record, "windSpeed"),
                        "gust_mph": _num(record, "windGust"),
                        "direction": _num(record, "windDir")}),
        # hourly_in carries the RATE (in/hr), matching the AWN field the
        # backend stores; dayRain exists on some drivers only.
        "rain": _block({"hourly_in": _num(record, "rainRate"),
                        "daily_in": _num(record, "dayRain")}),
        "pressure": _block({"relative_inhg": _num(record, "barometer")}),
        # Soil temperature probes → the 1.9 typed columns (R11 V14).
        # soilMoist1-4 are deliberately NOT mapped: WeeWX stores them in
        # centibars (tension), and the soilhum columns store percent —
        # forwarding one as the other is a unit bug, not a feature.
        "extra": _block({f"soiltemp{i}f": _num(record, f"soilTemp{i}")
                         for i in (1, 2, 3, 4)}),
    }
    for k, v in blocks.items():
        if v is not None:
            payload[k] = v
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects (CodeRabbit): urllib's default handler re-sends the
    Authorization header to wherever a 30x points — a cross-host or
    https→http redirect would hand the ingest token to a third party.
    Returning None makes urllib raise HTTPError for the redirect status,
    which the retry machinery treats like any other failed post."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


def _cleartext_host_allowed(host):
    """True when plain http is acceptable for this host: loopback, RFC1918
    private ranges, or LAN-only name suffixes. A local-Docker backend on
    the user's own network is a documented deployment; anything routable
    must be https or the ingest token travels cleartext."""
    if host is None:
        return False
    host = host.lower()
    if host in ("localhost",) or host.endswith((".local", ".lan",
                                                ".home.arpa", ".internal")):
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def build_request(server_url, ingest_token, payload):
    """The upload request — one builder shared by the pure poster below and
    the WeeWX thread, so the URL/header shape can't drift between them.

    Refuses to attach the Authorization header to a cleartext URL
    (CodeRabbit, CWE-319) unless the host is loopback/private-LAN — a
    typo'd http:// against a real internet host would send the ingest
    token in the clear on every archive interval."""
    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and _cleartext_host_allowed(parsed.hostname)):
        # Hostname only in the error (R16): the full URL can carry
        # userinfo (http://user:pass@host) and weewx logs the reason
        # string to syslog verbatim.
        raise ValueError(
            "server_url must be https (plain http is allowed only for "
            "loopback/private-LAN hosts); got scheme %r, host %s"
            % (parsed.scheme, parsed.hostname))
    return urllib.request.Request(
        server_url.rstrip("/") + "/ingest/custom",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + ingest_token,
                 "User-Agent": "weewx-zasder/" + VERSION},
        method="POST",
    )


def post_payload(server_url, ingest_token, payload, timeout=15):
    """POST one payload; raises urllib.error.* on failure. For scripts and
    tests — the WeeWX thread goes through post_with_retries instead."""
    req = build_request(server_url, ingest_token, payload)
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as r:
        return r.status


try:
    import weewx
    import weewx.manager
    import weewx.restx
    import weewx.units
    from weeutil.weeutil import to_int

    class Zasder(weewx.restx.StdRESTbase):
        """Binds to NEW_ARCHIVE_RECORD; a queue + thread (the standard
        WeeWX RESTful shape) keeps a slow network off the main loop."""

        def __init__(self, engine, cfg_dict):
            super().__init__(engine, cfg_dict)
            try:
                site = dict(cfg_dict["StdRESTful"]["Zasder"])
                server_url = site["server_url"]
                ingest_token = site["ingest_token"]
            except KeyError:
                return      # not configured — stay quiet, like siblings
            if "replace_me" in (server_url, ingest_token):
                return
            # Cleartext policy, split by destination (CodeRabbit, two
            # rounds): LAN docker installs legitimately post to
            # http://192.168.x.x:8080 — the same deliberate exception the
            # backend's own docs make — so private/loopback http WARNS.
            # A ROUTABLE http host would leak the ingest token on every
            # archive interval, so it refuses HERE, at init, where the
            # uploader thread never starts (build_request would raise
            # per-record, which run_loop treats as fatal — the R11 V1
            # thread-killer shape).
            # urlsplit-normalized, matching build_request (R16): a
            # startswith("http://") gate was case-sensitive, so
            # "HTTP://host" (or ftp://, or a malformed URL) slipped past
            # init only to raise per-record — the thread-killer shape this
            # gate exists to prevent. Anything that isn't exactly https or
            # allowed-http refuses HERE.
            parsed = urllib.parse.urlsplit(server_url)
            if parsed.scheme != "https":
                import logging
                if parsed.scheme != "http" or not _cleartext_host_allowed(
                        parsed.hostname):
                    logging.getLogger("user.zasder").error(
                        "zasder: refusing server_url (scheme %r, host %s) — "
                        "must be https, or http to a private-LAN host",
                        parsed.scheme, parsed.hostname)
                    return
                logging.getLogger("user.zasder").warning(
                    "zasder: server_url is plain http — the ingest token "
                    "travels unencrypted on your LAN; use https if the "
                    "server has it")
            # The archive binding lets RESTThread.get_record augment each
            # record with dayRain/hourRain sums — most drivers never emit
            # dayRain natively, and without this every default-schema
            # station stored NULL daily rain forever (R11 V7).
            try:
                manager_dict = weewx.manager.get_manager_dict_from_config(
                    cfg_dict, "wx_binding")
            except Exception as e:
                # Loudly (R12): silence here quietly reintroduces the
                # NULL-daily-rain gap this binding exists to close.
                import logging
                logging.getLogger("user.zasder").warning(
                    "zasder: no archive binding (%s) — dayRain augmentation "
                    "disabled; daily rain will not upload", e)
                manager_dict = None
            import queue
            self.archive_queue = queue.Queue()
            self.archive_thread = ZasderThread(
                self.archive_queue,
                server_url=server_url,
                ingest_token=ingest_token,
                device_id=site.get("device_id", "weewx"),
                station_name=site.get("station_name"),
                manager_dict=manager_dict,
                timeout=to_int(site.get("timeout", 15)),
                max_backlog=to_int(site.get("max_backlog", 100)),
            )
            self.archive_thread.start()
            self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

        def new_archive_record(self, event):
            self.archive_queue.put(event.record)

    class ZasderThread(weewx.restx.RESTThread):
        def __init__(self, q, server_url, ingest_token, device_id,
                     station_name, timeout, max_backlog, manager_dict=None):
            super().__init__(q, protocol_name="Zasder",
                             manager_dict=manager_dict,
                             timeout=timeout, max_backlog=max_backlog)
            self.server_url = server_url
            self.ingest_token = ingest_token
            self.device_id = device_id
            self.station_name = station_name

        def process_record(self, record, dbmanager):
            # get_record fills in the archive-derived sums (dayRain et al)
            # the driver itself doesn't report; a None dbmanager skips the
            # augmentation upstream, it never raises.
            record = self.get_record(record, dbmanager)
            payload = record_to_payload(
                weewx.units.to_US(record),
                device_id=self.device_id,
                station_name=self.station_name)
            if payload is None:
                return
            # THROUGH the base class's retry machinery, never urlopen
            # directly: post_with_retries retries transient failures and
            # raises FailedPost when exhausted, which run_loop logs and
            # SURVIVES. A raw URLError here is a generic exception to
            # run_loop, which terminates the uploader thread forever —
            # one DNS blip and nothing uploads until a manual weewxd
            # restart (R11 V1).
            self.post_with_retries(
                build_request(self.server_url, self.ingest_token, payload))

        def post_request(self, request, data=None):
            # The redirect-refusing opener (CodeRabbit): the base class's
            # urlopen would re-send the Authorization header to wherever a
            # 30x points. The redirect surfaces as HTTPError → the normal
            # retry/FailedPost path, never a silent token hand-off.
            return _NO_REDIRECT_OPENER.open(request, data=data,
                                            timeout=self.timeout)

except ImportError:
    # Not running under WeeWX (e.g. the bridge's own test suite) — the
    # pure transform above is still importable.
    pass
