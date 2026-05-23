"""WeatherLink v2 cloud API client.

Davis WeatherLink Console (6313) uploads ISS readings to weatherlink.com
every ~15-30s. The v2 API exposes:
  GET /v2/stations                  → list user's stations
  GET /v2/current/{station_id}      → live readings (all sensors)
  GET /v2/historic/{station_id}     → time range (Pro tier only)

Auth: api-key as a query param + X-Api-Secret as a header. No HMAC
signing required in the modern flow — that's the legacy v2 scheme
people sometimes mis-document.

Used by WeatherlinkPoller (see weatherlink_poller.py) to pull live
data and feed it into the same /ingest/custom path the SDR relays use,
so Davis observations land in the standard observations table
identified by a synthetic MAC.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx


log = logging.getLogger("weatherlink")

API_BASE = "https://api.weatherlink.com/v2"


class WeatherLinkClient:
    """Thin async wrapper around the WeatherLink v2 REST API."""

    def __init__(self, api_key: str, api_secret: str, timeout: float = 10.0):
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self._api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"X-Api-Secret": api_secret},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_stations(self) -> list[dict[str, Any]]:
        r = await self._http.get("/stations", params={"api-key": self._api_key})
        r.raise_for_status()
        return r.json().get("stations", []) or []

    async def current(self, station_id: int) -> dict[str, Any]:
        r = await self._http.get(f"/current/{station_id}",
                                 params={"api-key": self._api_key})
        r.raise_for_status()
        return r.json()
