"""Outbound webhooks (1.8, Pillar B): POST fired alerts to user-chosen
URLs — the glue Node-RED / n8n / Home Assistant automations expect from
a self-hosted tool.

v1 scope is ALERT events only: one JSON POST per delivered alert
(device-down, threshold, smart, storm, rain-start), with an HMAC-SHA256
signature header over the raw body so receivers can authenticate us.
Live readings are deliberately not an event — MQTT already streams
those, and a webhook per 2-second observation would be a DoS with
extra steps.

Delivery is best-effort with one retry: a dead endpoint logs its error
on the row (surfaced by GET /api/webhooks) and never blocks or fails
alert delivery itself. SSRF guard: https only, no loopback/private
hosts — the same rule the push-relay URL learned the hard way.
"""
from __future__ import annotations

import asyncio
import functools
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from . import db

log = logging.getLogger("webhooks")

_TIMEOUT_S = 10.0
# Pause before the one retry of a failed delivery. Module-level so a test
# can shrink it without replacing asyncio.sleep for the whole process.
RETRY_DELAY_S = 2.0
# How long one DNS resolution may take before the attempt is abandoned.
RESOLVE_TIMEOUT_S = 5.0
# Bound at import: tests replace `webhooks.asyncio` with a stub that has
# only `sleep`, and the delivery path must still resolve in a thread.
_wait_for = asyncio.wait_for
_get_running_loop = asyncio.get_running_loop     # bound at import, like _wait_for

# The resolver's threads are bounded by their own pool: wait_for bounds
# the WAIT, not the thread, and a dark resolver used to park two default-
# executor threads per hook per alert next to capture, the health probe
# and the backup (round-two review BE-N7). Four is plenty for a handful of
# hooks; a fifth resolution queues here instead of starving the others.
RESOLVER_THREADS = 4
_RESOLVER_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=RESOLVER_THREADS, thread_name_prefix="webhook-resolve")


def _to_thread(fn, *args):
    return _get_running_loop().run_in_executor(
        _RESOLVER_POOL, functools.partial(fn, *args))


# Carrier-grade NAT (RFC 6598). `ipaddress.is_private` is False for it,
# and on Fly it is exactly where the private 6PN/anycast neighbours live.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address a webhook must not be delivered to. An
    IPv4-mapped IPv6 address is judged by the IPv4 inside it."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                or (ip.version == 4 and ip in _CGNAT))


def resolve_public(hostname: str) -> str:
    """Resolve `hostname` and return ONE public address to deliver to.
    Raises ValueError when it does not resolve or when ANY answer is a
    private, loopback, link-local, reserved, multicast or CGNAT address
    — a host that answers both ways is treated as hostile."""
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("webhook host does not resolve")
    addrs: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _blocked(ip):
            raise ValueError("webhook host resolves to a private address")
        addrs.append(str(ip))
    if not addrs:
        raise ValueError("webhook host does not resolve")
    # Prefer IPv4 so the pinned URL matches what most self-hosters' egress
    # can actually reach; v6-only hosts still work.
    addrs.sort(key=lambda a: ":" in a)
    return addrs[0]


def validate_webhook_url(url: str) -> None:
    """Raises ValueError unless the URL is a public https endpoint."""
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError("webhook URLs must be https")
    if not p.hostname:
        raise ValueError("webhook URL has no host")
    resolve_public(p.hostname)


def pinned_request(url: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Re-resolve the stored URL's host NOW and pin the delivery to that
    address: the returned (url, headers, extensions) post to the IP with
    the original host in the Host header and as the TLS server name, so
    the certificate is still checked against the hostname. Registration
    used to be the only check (R17 / the 2.0 pre-flight): a host that
    passed then and re-pointed its DNS at 10.x afterwards was delivered
    to. Raises ValueError like validate_webhook_url."""
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("webhook URL is not https")
    addr = resolve_public(p.hostname)
    host_literal = f"[{addr}]" if ":" in addr else addr
    netloc = host_literal + (f":{p.port}" if p.port else "")
    pinned = urlunparse((p.scheme, netloc, p.path or "/", p.params,
                         p.query, ""))
    host_header = p.hostname + (f":{p.port}" if p.port else "")
    return pinned, {"Host": host_header}, {"sni_hostname": p.hostname}


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def dispatch_alert(kind: str, mac: str | None, title: str,
                         body: str, ts_ms: int,
                         severity: str | None = None) -> None:
    """Fan one delivered alert out to every enabled webhook. Fire-and-
    forget from the caller's perspective — errors land on the row."""
    hooks = await db.list_webhooks(enabled_only=True)
    if not hooks:
        return
    payload = json.dumps({
        "event": "alert",
        "kind": kind,
        "mac": mac,
        "title": title,
        "body": body,
        "ts_ms": ts_ms,
        "severity": severity,
    }, separators=(",", ":")).encode()
    await asyncio.gather(*(_send(h, payload) for h in hooks))


async def _send(hook: dict[str, Any], payload: bytes) -> None:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "zasder-weather-webhook",
        "X-Zasder-Signature": "sha256=" + sign(hook["secret"], payload),
    }
    err: str | None = None
    for attempt in (1, 2):
        try:
            # Resolve on EVERY attempt: the answer a minute ago is not the
            # answer now, which is the whole rebinding trick.
            # In a worker thread: resolve_public blocks on getaddrinfo,
            # and one slow resolver must not stall the monitor tick and
            # every request behind it (CodeRabbit, PR #36).
            # Bounded: a stalled resolver must not hold _send (and the
            # dispatch gather above it) open past the delivery timeout.
            # The thread itself cannot be interrupted; the wait can.
            url, host_hdr, ext = await _wait_for(
                _to_thread(pinned_request, hook["url"]), timeout=RESOLVE_TIMEOUT_S)
            # trust_env=False: with an HTTPS_PROXY in the environment httpx
            # would tunnel through the proxy using the pinned IP as the TLS
            # server name and ignore sni_hostname, failing verification. A
            # webhook is a direct delivery to a public address by design.
            async with httpx.AsyncClient(timeout=_TIMEOUT_S, trust_env=False) as client:
                r = await client.post(url, content=payload,
                                      headers={**headers, **host_hdr},
                                      extensions=ext)
            if 200 <= r.status_code < 300:
                await db.stamp_webhook(hook["id"], int(time.time() * 1000),
                                       None)
                return
            err = f"HTTP {r.status_code}"
        except Exception as e:
            # A timeout carries no message; the type name is the record.
            err = (str(e) or type(e).__name__)[:200]
        if attempt == 1:
            await asyncio.sleep(RETRY_DELAY_S)
    await db.stamp_webhook(hook["id"], None, err)
    log.warning("webhook %s failed: %s", hook["id"], err)
