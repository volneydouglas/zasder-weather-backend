import asyncio
from typing import Any

import httpx

from .config import settings

BASE_URL = "https://rt.ambientweather.net/v1"  # rt = REST + realtime endpoints
_TIMEOUT = httpx.Timeout(20.0)


class AmbientWeatherClient:
    """Thin async wrapper around the AmbientWeather REST API.

    Rate limit: the docs say 1 req/sec per applicationKey. We serialize calls
    through an asyncio.Lock + min-interval sleep to stay under it.
    """

    def __init__(self, app_key: str, api_key: str, min_interval: float = 1.1):
        self._app_key = app_key
        self._api_key = api_key
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        merged = {"applicationKey": self._app_key, "apiKey": self._api_key}
        if params:
            merged.update(params)
        async with self._lock:
            elapsed = asyncio.get_event_loop().time() - self._last_call
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            resp = await self._client.get(path, params=merged)
            self._last_call = asyncio.get_event_loop().time()
        resp.raise_for_status()
        return resp.json()

    async def list_devices(self) -> list[dict[str, Any]]:
        """Returns devices with their most recent observation embedded as `lastData`."""
        return await self._get("/devices")

    async def device_history(
        self, mac: str, end_date_ms: int | None = None, limit: int = 288
    ) -> list[dict[str, Any]]:
        """Historical observations for a device. limit max 288 (~24h at 5min)."""
        params: dict[str, Any] = {"limit": limit}
        if end_date_ms is not None:
            params["endDate"] = end_date_ms
        return await self._get(f"/devices/{mac}", params=params)
