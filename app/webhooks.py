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
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from . import db

log = logging.getLogger("webhooks")

_TIMEOUT_S = 10.0


def validate_webhook_url(url: str) -> None:
    """Raises ValueError unless the URL is a public https endpoint."""
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError("webhook URLs must be https")
    if not p.hostname:
        raise ValueError("webhook URL has no host")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        raise ValueError("webhook host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise ValueError("webhook host resolves to a private address")


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
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                r = await client.post(hook["url"], content=payload,
                                      headers=headers)
            if 200 <= r.status_code < 300:
                await db.stamp_webhook(hook["id"], int(time.time() * 1000),
                                       None)
                return
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = str(e)[:200]
        if attempt == 1:
            await asyncio.sleep(2.0)
    await db.stamp_webhook(hook["id"], None, err)
    log.warning("webhook %s failed: %s", hook["id"], err)
