"""Update checker — tells the operator when a newer backend release exists.

Once a day the backend asks GitHub for the latest release of the public repo
and compares it to the running `__version__`. The result is cached in memory
and surfaced on the status page (a banner) and at `/api/version`, so a
self-hoster learns about updates with zero effort — the standard pattern for
self-hosted software (Pi-hole, Gitea, Home Assistant, …).

Privacy / control:
  * ONE unauthenticated GET to api.github.com per day. Nothing about the
    operator's data or instance is sent — it's a plain public-release lookup.
  * Fully opt-out: set UPDATE_CHECK=0 (or false/off) to disable.
  * Repo is configurable via UPDATE_CHECK_REPO (default the public backend).
Failures (offline, rate-limited, GitHub down) are swallowed — the check never
affects serving.
"""

import asyncio
import logging
import os
import time

import httpx

from .version import __version__

log = logging.getLogger("updates")

_DEFAULT_REPO = "volneydouglas/zasder-weather-backend"
_CHECK_INTERVAL_S = 24 * 3600
_RETRY_ON_FAIL_S = 3600


def _enabled() -> bool:
    return (os.environ.get("UPDATE_CHECK", "1").strip().lower()
            not in ("0", "false", "off", "no"))


def _repo() -> str:
    return os.environ.get("UPDATE_CHECK_REPO", "").strip() or _DEFAULT_REPO


def parse_version(v: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' → (1, 2, 3). Non-numeric / pre-release suffixes are
    dropped so a best-effort compare still works; unparseable → (0,)."""
    v = v.strip().lstrip("vV")
    v = v.split("-", 1)[0].split("+", 1)[0]  # drop -rc1 / +build
    parts: list[int] = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


class UpdateChecker:
    """Background task; stores the last result on `app.state.update_info`."""

    def __init__(self, app):
        self.app = app
        self._task: asyncio.Task | None = None
        # Seed state so /api/version + the status page always have a shape.
        app.state.update_info = {
            "version": __version__,
            "latest": None,
            "update_available": False,
            "checked_ms": None,
            "enabled": _enabled(),
        }

    def start(self) -> None:
        if not _enabled():
            log.info("update check disabled (UPDATE_CHECK=0)")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        # Small startup delay so it never competes with boot / first requests.
        await asyncio.sleep(15)
        while True:
            ok = await self._check_once()
            await asyncio.sleep(_CHECK_INTERVAL_S if ok else _RETRY_ON_FAIL_S)

    async def _check_once(self) -> bool:
        url = f"https://api.github.com/repos/{_repo()}/releases/latest"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"zasder-weather/{__version__}",
                })
        except Exception as e:  # noqa: BLE001 — never let a check break serving
            log.info("update check failed (network): %s", e)
            return False
        if resp.status_code != 200:
            log.info("update check: GitHub HTTP %s", resp.status_code)
            return False
        tag = (resp.json() or {}).get("tag_name") or ""
        latest = tag.lstrip("vV") or None
        self.app.state.update_info = {
            "version": __version__,
            "latest": latest,
            "update_available": bool(latest and is_newer(latest, __version__)),
            "checked_ms": int(time.time() * 1000),
            "enabled": True,
        }
        if self.app.state.update_info["update_available"]:
            log.info("update available: %s → %s", __version__, latest)
        return True
