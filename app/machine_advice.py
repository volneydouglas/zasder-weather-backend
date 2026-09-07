"""Server recommendations (2.1): would this Fly machine be better off with
more disk or more memory, and apply it with one tap after approval.

The first outside user's volume hit 98% before anyone noticed, and the
only symptom was charts slowing down (that is what disk_watch answers
with an alert). This goes one step further for a self-hoster who does
not want to learn flyctl: read the machine and its volume through the
Machines API the backend already holds a token for (self_update.py), put
the numbers next to the process's own memory use and the database's
growth rate, and say "extend the volume by 2 GB for about thirty cents a
month" or "bump memory to 1 GB for about $2.60 a month, the server
restarts". The apply route does exactly that call and nothing else.

Where it is inert, by design:
- No FLY_API_TOKEN / FLY_APP_NAME / FLY_MACHINE_ID (a guest instance Volney
  deploys from the mirror, local Docker): advice is `available: false`
  with the reason, the app hides the card.
- SERVER_ADVICE=0 switches it off outright on a box that does carry the
  token but is not the operator's to resize.
- Every Fly call has a timeout and any failure means "unavailable", never
  a 500: this is advice, and serving weather does not depend on it.
- One Fly read per hour at most (ADVICE_TTL_S); the apply route
  invalidates the cache so the card re-reads afterwards.

Prices are Fly's published list for the iad region, read from
https://fly.io/docs/about/pricing/ on 2026-09-05 (PRICING below, with the
page's own wording). Other regions carry a markup; the card says
"about" and names the region assumption.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any

import httpx

from . import db

log = logging.getLogger("api")


def _settings():
    """Resolved per call, not bound at import: the test suite rebuilds
    app.config per test and this module is not on its reload list."""
    from . import config
    return config.settings

# ── pricing ───────────────────────────────────────────────────────────
# fly.io/docs/about/pricing, region iad, read 2026-09-05:
#   shared-cpu-1x 256MB $2.02/mo, 512MB $3.32, 1GB $5.92, 2GB $11.11;
#   "the price of a named CPU/RAM preset, plus about $5 per 30 days per GB
#    of additional RAM";
#   Fly Volumes "$0.15/GB per month of provisioned capacity".
PRICING_SOURCE = "fly.io/docs/about/pricing, region iad, 2026-09-05"
SHARED_1X_USD_PER_MONTH = {256: 2.02, 512: 3.32, 1024: 5.92, 2048: 11.11}
EXTRA_RAM_USD_PER_GB_MONTH = 5.00
VOLUME_USD_PER_GB_MONTH = 0.15
# shared-cpu-1x tops out at 2 GB on Fly; past that is a different preset
# (shared-cpu-2x and up), which is a decision, not a one-tap bump.
SHARED_1X_MAX_MB = 2048
MEMORY_STEP_MB = 256
MEMORY_CAP_MB = 8192

# ── thresholds ────────────────────────────────────────────────────────
VOLUME_USED_PCT_ADVISE = 80.0
VOLUME_HEADROOM_DAYS_ADVISE = 90
# The recommended size leaves this much headroom at the observed growth,
# and never lands above this used share, whichever is larger.
VOLUME_TARGET_HEADROOM_DAYS = 365
VOLUME_TARGET_USED_PCT = 60.0
VOLUME_MAX_STEP_GB = 50
MEMORY_RSS_SHARE_ADVISE = 0.75
# Machine events newer than this that carry an OOM kill count as memory
# pressure even when RSS reads fine right now (it was just restarted).
OOM_LOOKBACK_MS = 7 * 86_400_000

ADVICE_TTL_S = 3600.0
FLY_TIMEOUT_S = 15.0
MACHINES_BASE = "https://api.machines.dev/v1"


def _enabled() -> bool:
    from . import config as _config
    return bool(_config.settings.server_advice)


def _identity() -> tuple[str, str, str] | None:
    """(app, machine, token) or None when any is missing. Reuses the
    self-update token: the same app-scoped deploy token can read and
    update this app's machines and volumes."""
    from .self_update import _fly_token
    app_name = os.environ.get("FLY_APP_NAME", "").strip()
    machine_id = os.environ.get("FLY_MACHINE_ID", "").strip()
    token = _fly_token()
    if not (app_name and machine_id and token):
        return None
    return app_name, machine_id, token


def rss_bytes() -> int | None:
    """Current resident set of THIS process. /proc on Linux (Fly);
    ru_maxrss elsewhere, which is a peak — good enough for a developer's
    Mac, and it is labelled as an estimate anyway."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except Exception:  # noqa: BLE001 — advice, not serving
        return None


# ── Fly Machines API ──────────────────────────────────────────────────

class FlyMachines:
    """The four calls this module makes. `client_factory` is the test
    seam: production builds an httpx.AsyncClient, tests hand back a fake
    with get/post/put."""

    def __init__(self, app_name: str, machine_id: str, token: str,
                 client_factory=None) -> None:
        from .self_update import _auth_header
        self.app_name = app_name
        self.machine_id = machine_id
        self.headers = {"Authorization": _auth_header(token)}
        self._factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=FLY_TIMEOUT_S))

    @property
    def _machine_url(self) -> str:
        return f"{MACHINES_BASE}/apps/{self.app_name}/machines/{self.machine_id}"

    async def machine(self) -> dict[str, Any]:
        """GET /v1/apps/{app}/machines/{id}."""
        async with self._factory() as client:
            r = await client.get(self._machine_url, headers=self.headers)
            if r.status_code != 200:
                raise RuntimeError(f"machine read HTTP {r.status_code}")
            body = r.json()
            if not isinstance(body, dict):
                raise RuntimeError("machine read: not an object")
            return body

    async def volumes(self) -> list[dict[str, Any]]:
        """GET /v1/apps/{app}/volumes."""
        async with self._factory() as client:
            r = await client.get(f"{MACHINES_BASE}/apps/{self.app_name}/volumes",
                                 headers=self.headers)
            if r.status_code != 200:
                raise RuntimeError(f"volume list HTTP {r.status_code}")
            body = r.json()
            return [v for v in body if isinstance(v, dict)] \
                if isinstance(body, list) else []

    async def extend_volume(self, volume_id: str, size_gb: int) -> dict[str, Any]:
        """PUT /v1/apps/{app}/volumes/{id}/extend {"size_gb": N}. The
        response carries `needs_restart`."""
        async with self._factory() as client:
            r = await client.put(
                f"{MACHINES_BASE}/apps/{self.app_name}/volumes/{volume_id}/extend",
                headers=self.headers, json={"size_gb": int(size_gb)})
            if r.status_code not in (200, 201):
                raise RuntimeError(
                    f"volume extend HTTP {r.status_code}: {r.text[:200]}")
            body = r.json()
            return body if isinstance(body, dict) else {}

    async def set_memory(self, memory_mb: int) -> dict[str, Any]:
        """POST /v1/apps/{app}/machines/{id} with the FULL current config
        and only guest.memory_mb changed. Posting a partial config would
        replace the whole machine config (the self_update lesson). Fly
        reboots a running machine on a successful update."""
        async with self._factory() as client:
            cur = await client.get(self._machine_url, headers=self.headers)
            if cur.status_code != 200:
                raise RuntimeError(f"machine read HTTP {cur.status_code}")
            config = (cur.json() or {}).get("config") or {}
            if not config or not isinstance(config.get("guest"), dict):
                raise RuntimeError("machine returned an empty config; "
                                   "holding rather than replacing it")
            config["guest"]["memory_mb"] = int(memory_mb)
            r = await client.post(self._machine_url, headers=self.headers,
                                  json={"config": config})
            if r.status_code not in (200, 201):
                raise RuntimeError(
                    f"machine update HTTP {r.status_code}: {r.text[:200]}")
            body = r.json()
            return body if isinstance(body, dict) else {}


def _oom_recent(machine: dict[str, Any], now_ms: int) -> bool:
    """Any machine event inside the lookback that mentions an OOM kill.
    Fly's exit events carry `request.exit_event.oom_killed`; searched
    structurally so a reshuffled event shape still counts."""
    def has_oom(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("oom_killed") is True:
                return True
            return any(has_oom(v) for v in node.values())
        if isinstance(node, list):
            return any(has_oom(v) for v in node)
        return False
    for ev in machine.get("events") or []:
        if not isinstance(ev, dict):
            continue
        ts = ev.get("timestamp")
        # Fail closed: an event whose time cannot be read is not "recent"
        # (2.1 pre-release review BE-10).
        if not isinstance(ts, (int, float)) or now_ms - ts > OOM_LOOKBACK_MS:
            continue
        if has_oom(ev):
            return True
    return False


# ── growth ────────────────────────────────────────────────────────────

async def growth_bytes_per_day() -> float | None:
    """How fast the database grows: bytes per row (file size over row
    count) times rows per day (the last seven days). Two COUNTs and one
    stat; cached with the rest for an hour. None when there is nothing to
    measure yet — absent is not zero, and zero growth would read as
    infinite headroom."""
    try:
        size = os.stat(_settings().database_path).st_size
    except OSError:
        return None
    week_ago = int(time.time() * 1000) - 7 * 86_400_000
    async with db.connect() as conn:
        total = (await (await conn.execute(
            "SELECT COUNT(*) FROM observations")).fetchone())[0]
        recent = (await (await conn.execute(
            "SELECT COUNT(*) FROM observations WHERE dateutc_ms > ?",
            (week_ago,))).fetchone())[0]
    if not total or not recent:
        return None
    return (size / total) * (recent / 7.0)


# ── pricing math ──────────────────────────────────────────────────────

def memory_usd_per_month(cpu_kind: str, cpus: int, memory_mb: int) -> float | None:
    """Monthly price of a preset, or None when this module does not know
    the preset (the card then shows the delta as unknown rather than a
    made-up number)."""
    if cpu_kind == "shared" and cpus == 1:
        if memory_mb in SHARED_1X_USD_PER_MONTH:
            return SHARED_1X_USD_PER_MONTH[memory_mb]
        if memory_mb > SHARED_1X_MAX_MB:
            extra_gb = (memory_mb - SHARED_1X_MAX_MB) / 1024.0
            return round(SHARED_1X_USD_PER_MONTH[SHARED_1X_MAX_MB]
                         + EXTRA_RAM_USD_PER_GB_MONTH * extra_gb, 2)
    return None


def memory_delta_usd(cpu_kind: str, cpus: int, cur_mb: int, new_mb: int) -> float | None:
    a = memory_usd_per_month(cpu_kind, cpus, cur_mb)
    b = memory_usd_per_month(cpu_kind, cpus, new_mb)
    if a is None or b is None:
        # The named presets are unknown, but Fly's own rule for additional
        # RAM is not: the delta is close enough to say "about".
        return round(EXTRA_RAM_USD_PER_GB_MONTH * (new_mb - cur_mb) / 1024.0, 2)
    return round(b - a, 2)


def volume_delta_usd(added_gb: int) -> float:
    return round(VOLUME_USD_PER_GB_MONTH * added_gb, 2)


def next_memory_mb(cur_mb: int) -> int | None:
    """The next step up: the next named shared-cpu-1x preset, then 1 GB at
    a time to the cap. None at the cap."""
    presets = sorted(SHARED_1X_USD_PER_MONTH)
    for p in presets:
        if p > cur_mb:
            return p
    nxt = ((cur_mb // 1024) + 1) * 1024
    return nxt if nxt <= MEMORY_CAP_MB else None


def recommend_volume_gb(size_gb: int, used_bytes: int, free_bytes: int,
                        growth_per_day: float | None) -> int | None:
    """Target size: enough that the volume sits under VOLUME_TARGET_USED_PCT
    today AND holds VOLUME_TARGET_HEADROOM_DAYS of growth, rounded up to a
    whole GB, at most VOLUME_MAX_STEP_GB more than today. None when today's
    size already satisfies both (no advice)."""
    gib = 1024 ** 3
    want = used_bytes / (VOLUME_TARGET_USED_PCT / 100.0)
    if growth_per_day:
        want = max(want, used_bytes + growth_per_day * VOLUME_TARGET_HEADROOM_DAYS)
    target = int(-(-want // gib))          # ceil in GB
    target = min(target, size_gb + VOLUME_MAX_STEP_GB)
    return target if target > size_gb else None


# ── the verdict ───────────────────────────────────────────────────────

_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
# The refresh lock is rebuilt whenever the running loop changes (the test
# suite runs asyncio.run() per test; a Lock bound to a finished loop
# raises on its next use), the same reason main._PUBLIC_DASH_LOCK is lazy.
_LOCK: asyncio.Lock | None = None
_LOCK_LOOP: Any = None


def reset_cache() -> None:
    _CACHE["at"], _CACHE["value"] = 0.0, None


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


async def compute(fly: FlyMachines | None = None,
                  now_ms: int | None = None) -> dict[str, Any]:
    """The advice document. Never raises: any failure becomes
    `available: false` with the reason."""
    from . import disk_watch
    if not _enabled():
        return _unavailable("switched off (SERVER_ADVICE=0)")
    if fly is None:
        ident = _identity()
        if ident is None:
            return _unavailable("this server has no Fly deploy token, so it "
                                "cannot read or resize its own machine")
        fly = FlyMachines(*ident)
    now_ms = now_ms or int(time.time() * 1000)
    try:
        machine = await fly.machine()
        vols = await fly.volumes()
    except Exception as e:  # noqa: BLE001 — advice, never a 500
        log.info("server advice: Fly read failed: %s", e)
        return _unavailable(f"could not read the machine from Fly ({e})")

    guest = ((machine.get("config") or {}).get("guest") or {})
    cpu_kind = str(guest.get("cpu_kind") or "shared")
    cpus = int(guest.get("cpus") or 1)
    memory_mb = guest.get("memory_mb")
    memory_mb = int(memory_mb) if isinstance(memory_mb, (int, float)) else None
    rss = rss_bytes()
    oom = _oom_recent(machine, now_ms)

    vol = next((v for v in vols
                if v.get("attached_machine_id") == fly.machine_id), None)
    disk = disk_watch.snapshot() or {}
    try:
        growth = await growth_bytes_per_day()
    except Exception as e:  # noqa: BLE001
        log.info("server advice: growth estimate failed: %s", e)
        growth = None

    advice: list[dict[str, Any]] = []

    # ── volume ──
    size_gb = vol.get("size_gb") if vol else None
    size_gb = int(size_gb) if isinstance(size_gb, (int, float)) else None
    free_b = disk.get("free_bytes")
    total_b = disk.get("total_bytes")
    used_pct = disk.get("used_pct")
    headroom_days = None
    if isinstance(free_b, (int, float)) and growth:
        headroom_days = int(free_b / growth)
    if size_gb and isinstance(free_b, (int, float)) and isinstance(total_b, (int, float)):
        used_b = max(0, int(total_b) - int(free_b))
        low = isinstance(used_pct, (int, float)) and used_pct >= VOLUME_USED_PCT_ADVISE
        tight = headroom_days is not None and headroom_days < VOLUME_HEADROOM_DAYS_ADVISE
        if low or tight:
            target = recommend_volume_gb(size_gb, used_b, int(free_b), growth)
            if target:
                if low and tight:
                    reason = (f"The volume is {used_pct:.0f}% full and, at the "
                              f"rate the archive grows, has about {headroom_days} "
                              f"days of room left.")
                elif low:
                    reason = f"The volume is {used_pct:.0f}% full."
                else:
                    reason = (f"At the rate the archive grows, the volume has "
                              f"about {headroom_days} days of room left.")
                advice.append({
                    "kind": "volume", "unit": "GB",
                    "current": size_gb, "recommended": target,
                    "est_usd_per_month": volume_delta_usd(target - size_gb),
                    "restarts": False,
                    "reason": reason + " Extending is online; nothing restarts.",
                })

    # ── memory ──
    if memory_mb:
        share = (rss / (memory_mb * 1024 * 1024)) if rss else None
        pressure = (share is not None and share > MEMORY_RSS_SHARE_ADVISE) or oom
        if pressure:
            target = next_memory_mb(memory_mb)
            if oom and share is not None and share > MEMORY_RSS_SHARE_ADVISE:
                reason = (f"The server was killed for running out of memory "
                          f"in the last week and is using {share * 100:.0f}% "
                          f"of its {memory_mb} MB right now.")
            elif oom:
                reason = ("The server was killed for running out of memory "
                          "in the last week.")
            else:
                reason = (f"The server is using {share * 100:.0f}% of its "
                          f"{memory_mb} MB.")
            if target:
                advice.append({
                    "kind": "memory", "unit": "MB",
                    "current": memory_mb, "recommended": target,
                    "est_usd_per_month": memory_delta_usd(cpu_kind, cpus,
                                                          memory_mb, target),
                    "restarts": True,
                    "reason": reason + " Applying restarts the server "
                              "(about ten seconds of downtime; readings "
                              "relays send meanwhile are retried).",
                })
            else:
                advice.append({
                    "kind": "memory", "unit": "MB",
                    "current": memory_mb, "recommended": None,
                    "est_usd_per_month": None, "restarts": True,
                    "reason": reason + " This machine is at the top of its "
                              "size; a bigger preset is a flyctl decision.",
                })

    return {
        "available": True,
        "checked_ms": now_ms,
        "pricing": {"source": PRICING_SOURCE,
                    "volume_usd_per_gb_month": VOLUME_USD_PER_GB_MONTH},
        "machine": {"cpu_kind": cpu_kind, "cpus": cpus,
                    "memory_mb": memory_mb,
                    "rss_mb": round(rss / 2**20, 1) if rss else None,
                    "oom_recent": oom},
        "volume": {"id": vol.get("id") if vol else None,
                   "size_gb": size_gb,
                   "used_pct": used_pct,
                   "free_bytes": free_b,
                   "growth_bytes_per_day": round(growth) if growth else None,
                   "headroom_days": headroom_days},
        "advice": advice,
    }


async def advice(refresh: bool = False) -> dict[str, Any]:
    """Cached for ADVICE_TTL_S. `refresh` bypasses the cache (the app's
    pull-to-refresh); a lock keeps two refreshes from both calling Fly."""
    global _LOCK, _LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _LOCK is None or _LOCK_LOOP is not loop:
        _LOCK, _LOCK_LOOP = asyncio.Lock(), loop
    now = time.monotonic()
    cached = _CACHE["value"]
    if cached is not None and not refresh and now - _CACHE["at"] < ADVICE_TTL_S:
        return cached
    async with _LOCK:
        cached = _CACHE["value"]
        if cached is not None and not refresh and \
                time.monotonic() - _CACHE["at"] < ADVICE_TTL_S:
            return cached
        value = await compute()
        _CACHE["at"], _CACHE["value"] = time.monotonic(), value
        return value


class ApplyError(ValueError):
    """A refusal the route turns into a 4xx with this text."""


async def apply(kind: str, target: int, fly: FlyMachines | None = None) -> dict[str, Any]:
    """Apply one recommendation. Validates against the CURRENT machine, not
    the cached advice, so a stale card cannot shrink anything or skip a
    step. Raises ApplyError for refusals; other exceptions are Fly
    failures the route reports as 502."""
    if not _enabled():
        raise ApplyError("server advice is switched off (SERVER_ADVICE=0)")
    if fly is None:
        ident = _identity()
        if ident is None:
            raise ApplyError("this server has no Fly deploy token, so it "
                             "cannot resize its own machine")
        fly = FlyMachines(*ident)
    try:
        target = int(target)
    except (TypeError, ValueError):
        raise ApplyError("target must be a whole number") from None
    if kind == "volume":
        vols = await fly.volumes()
        vol = next((v for v in vols
                    if v.get("attached_machine_id") == fly.machine_id), None)
        if not vol or not isinstance(vol.get("size_gb"), (int, float)):
            raise ApplyError("no volume is attached to this machine")
        cur = int(vol["size_gb"])
        if target <= cur:
            raise ApplyError(f"the volume is already {cur} GB; volumes only grow")
        if target > cur + VOLUME_MAX_STEP_GB:
            raise ApplyError(f"one step is at most {VOLUME_MAX_STEP_GB} GB")
        log.warning("server advice: extending volume %s %d GB -> %d GB",
                    vol.get("id"), cur, target)
        out = await fly.extend_volume(str(vol["id"]), target)
        reset_cache()
        return {"ok": True, "kind": "volume", "from": cur, "to": target,
                "restarts": False,
                "needs_restart": bool(out.get("needs_restart", False))}
    if kind == "memory":
        machine = await fly.machine()
        guest = ((machine.get("config") or {}).get("guest") or {})
        cur = guest.get("memory_mb")
        if not isinstance(cur, (int, float)):
            raise ApplyError("could not read the machine's current memory")
        cur = int(cur)
        if target <= cur:
            raise ApplyError(f"the machine already has {cur} MB; this only "
                             "adds memory")
        if target % MEMORY_STEP_MB or target > MEMORY_CAP_MB:
            raise ApplyError(f"memory must be a multiple of {MEMORY_STEP_MB} MB "
                             f"up to {MEMORY_CAP_MB} MB")
        log.warning("server advice: memory %d MB -> %d MB (machine restarts)",
                    cur, target)
        await fly.set_memory(target)
        reset_cache()
        return {"ok": True, "kind": "memory", "from": cur, "to": target,
                "restarts": True, "needs_restart": False}
    raise ApplyError("kind must be 'volume' or 'memory'")
