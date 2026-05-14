import asyncio
import logging
from typing import Any

from . import db
from .ambient_client import AmbientWeatherClient
from .config import settings

log = logging.getLogger("poller")


class Poller:
    def __init__(self, client: AmbientWeatherClient):
        self.client = client
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        await self.bootstrap()
        self._task = asyncio.create_task(self._run(), name="aw-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def bootstrap(self) -> None:
        """One-time backfill: pull device list + 288 most recent observations each."""
        try:
            devices = await self.client.list_devices()
        except Exception as e:
            log.exception("bootstrap: list_devices failed: %s", e)
            return
        for d in devices:
            mac = d.get("macAddress") or d.get("mac")
            if not mac:
                continue
            await db.upsert_device(mac, d)
            try:
                rows = await self.client.device_history(mac, limit=288)
                added = await db.insert_observations(mac, rows)
                log.info("bootstrap %s: added %d historical rows", mac, added)
            except Exception as e:
                log.exception("bootstrap: device_history(%s) failed: %s", mac, e)

    async def _run(self) -> None:
        interval = max(15, settings.poll_interval_seconds)
        log.info("poller running every %ds", interval)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:
                log.exception("poll tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        devices = await self.client.list_devices()
        for d in devices:
            mac = d.get("macAddress") or d.get("mac")
            if not mac:
                continue
            await db.upsert_device(mac, d)
            last: dict[str, Any] | None = d.get("lastData")
            if last:
                added = await db.insert_observations(mac, [last])
                if added:
                    log.debug("stored new obs for %s @ %s", mac, last.get("dateutc"))
