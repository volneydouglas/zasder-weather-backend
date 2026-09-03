"""Govee Platform API client (2.0): the GoveeLife CO₂ monitor and its
air-quality siblings, through the vendor cloud.

Two calls: `GET /router/api/v1/user/devices` lists every Wi-Fi device on
the account with its capability declarations; `POST /router/api/v1/
device/state` returns one device's current capability values. The key
rides a header (`Govee-API-Key`), never a URL, so it cannot land in a
log line or an error message; every exception this module raises carries
status and path only (the CR-01 redaction contract).

Limits Govee publishes: 30 device-list reads a minute per account, 30
state reads a minute per device, 10,000 requests a day per account. The
poller's floor keeps a single monitor under a third of that.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

log = logging.getLogger("zasder.govee")

API_BASE = "https://openapi.api.govee.com/router/api/v1"
_KEY_HEADER = "Govee-API-Key"


class GoveeCloudError(RuntimeError):
    """Path-only upstream failure. The key is a header, so no request
    URL or body ever carried it; the message still keeps to status and
    path so a future change cannot leak one."""

    @staticmethod
    def from_http(e: httpx.HTTPStatusError) -> "GoveeCloudError":
        r = e.response
        return GoveeCloudError(f"Govee HTTP {r.status_code} on {r.request.url.path}")


class GoveeCloudClient:
    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self._key = api_key
        self._timeout = timeout

    async def _request(self, method: str, path: str,
                       body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {_KEY_HEADER: self._key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.request(method, f"{API_BASE}{path}",
                                         headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise GoveeCloudError.from_http(e) from None
        except httpx.HTTPError as e:
            # Transport errors carry the URL in their text; the class name
            # says what happened without it.
            raise GoveeCloudError(f"Govee request failed: {type(e).__name__}") from None
        except ValueError:
            raise GoveeCloudError("Govee returned non-JSON") from None
        if not isinstance(data, dict):
            raise GoveeCloudError("Govee returned an unexpected body")
        code = data.get("code")
        if isinstance(code, int) and code != 200:
            msg = data.get("message") or data.get("msg") or ""
            raise GoveeCloudError(f"Govee error {code}: {str(msg)[:80]}")
        return data

    async def list_devices(self) -> list[dict[str, Any]]:
        """Every device the key can see. Govee has answered under both
        `data` and `payload.devices` shapes; read either."""
        data = await self._request("GET", "/user/devices")
        payload = data.get("data")
        if payload is None:
            payload = data.get("payload")
        if isinstance(payload, dict):
            payload = payload.get("devices", [])
        return [d for d in (payload or []) if isinstance(d, dict)]

    async def device_state(self, sku: str, device: str) -> dict[str, Any]:
        """One device's capabilities with their current `state`."""
        body = {"requestId": str(uuid.uuid4()),
                "payload": {"sku": sku, "device": device}}
        data = await self._request("POST", "/device/state", body)
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else {}
