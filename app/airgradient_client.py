"""AirGradient public-API client (1.8).

AirGradient monitors (indoor I-series, outdoor O-series) upload to the
AirGradient cloud, whose public API exposes every location on an account:

  GET /public/api/v1/locations/measures/current?token=…  → [location, …]

Auth is the account's API token (AirGradient dashboard → Place → API), and
it rides as a QUERY PARAM — the same credential-leak shape AmbientWeather,
WeatherLink and Tempest each got bitten by, handled the same way: error
messages carry the path and status only, never the URL.

Why the cloud API and not the monitors' local endpoint: the backend runs
wherever it runs (Fly, a VPS) and generally cannot see the LAN the
monitors sit on. The local `/measures/current` endpoint is the right
answer later for a LAN relay; the cloud API works from anywhere and one
token covers every monitor on the account.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("airgradient")

API_BASE = "https://api.airgradient.com/public/api/v1"


class AirGradientError(Exception):
    """Request failure with the token stripped out (the TempestError
    pattern — the token travels as a query param and httpx messages embed
    the full request URL)."""

    @staticmethod
    def from_http(e: httpx.HTTPStatusError) -> "AirGradientError":
        r = e.response
        return AirGradientError(
            f"AirGradient HTTP {r.status_code} on {r.request.url.path}")


class AirGradientClient:
    def __init__(self, token: str, timeout: float = 15.0) -> None:
        self._token = token
        self._timeout = timeout

    async def measures_current(self) -> list[dict[str, Any]]:
        """Latest measures for every location the token can see."""
        url = f"{API_BASE}/locations/measures/current"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, params={"token": self._token})
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise AirGradientError.from_http(e) from None
        except httpx.HTTPError as e:
            # Transport errors (DNS, timeout) name the failure class, not
            # the URL — httpx puts the full query string in str(e).
            raise AirGradientError(
                f"AirGradient request failed: {type(e).__name__}") from None
        if not isinstance(data, list):
            raise AirGradientError("AirGradient response is not a list")
        return [d for d in data if isinstance(d, dict)]


class AirGradientLocalClient:
    """LAN client (1.9): every AirGradient monitor serves its current
    measures locally, unauthenticated, at

      GET http://airgradient_<serial>.local/measures/current

    For a backend that can actually see the LAN (the docker-compose
    self-host next to the router — not a Fly instance), this cuts the
    vendor cloud out entirely. Plain http by design: the monitor's ESP
    serves no TLS, and the traffic never leaves the LAN. No token exists
    on this path, so error hygiene is only about not spamming: messages
    carry the host (operator-configured, not a secret) and failure class."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def measures_current(self, host: str) -> dict[str, Any]:
        url = f"http://{host}/measures/current"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise AirGradientError(
                f"AirGradient {host}: HTTP {e.response.status_code}") from None
        except httpx.HTTPError as e:
            raise AirGradientError(
                f"AirGradient {host}: {type(e).__name__}") from None
        except ValueError:
            raise AirGradientError(
                f"AirGradient {host}: non-JSON response") from None
        if not isinstance(data, dict):
            raise AirGradientError(f"AirGradient {host}: unexpected shape")
        return data
