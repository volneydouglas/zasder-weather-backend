"""Server disk-space watchdog (1.9): the volume fills silently until
SQLite says 'database or disk is full' mid-ingest — the first outside
user hit 98% before anyone noticed, and the only symptom was charts
slowing down. Two jobs here:

- `snapshot()` — used/free for the volume holding the database, surfaced
  on `/api/version` so the apps can show "v1.9.0 · 2.1 GB free" next to
  the version and tint it when space runs short.
- `check()` — a NEW `disk_low` alert kind, edge-triggered through the
  smart-alert state table like every other watcher: warn at ≥85% used
  (normal push), urgent at ≥95% (warning tier — breaks quiet hours),
  recovery notice when it clears. State rides the table's `triggered`
  column as the tier number (0 / 1 / 2), keyed under a server-level
  sentinel mac — this is about the machine, not any station.

De-escalation is hysteretic: a volume hovering at exactly 85.0% must
drop a couple of points before the tier clears, or every tick near the
boundary would flap alert/recover forever.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import db
from .config import settings

_SERVER_MAC = "server"          # sentinel: no station wears this mac
_KIND = "disk_low"

WARN_PCT = 85.0
URGENT_PCT = 95.0
# Must drop this far below a tier's threshold to leave the tier.
CLEAR_MARGIN_PCT = 2.0


def snapshot() -> dict | None:
    """Used/free for the volume holding the database. The database DIR may
    not exist yet (fresh checkout before first init) — the volume still
    does, so walk up to the nearest existing ancestor rather than
    reporting null on a perfectly measurable disk. None only when nothing
    up the chain can be statted (unmounted volume) — absent is not zero,
    and the API surfaces null rather than a fake 100%-free disk."""
    usage = None
    p = Path(settings.database_path).resolve().parent
    for candidate in (p, *p.parents):
        try:
            usage = shutil.disk_usage(candidate)
            break
        except OSError:
            continue
    if usage is None or usage.total <= 0:
        return None
    # used / (used + free), not used / total: `free` is f_bavail — what
    # applications can actually write — while `total` includes the root-
    # reserved blocks. Divided by total, a volume can sit at ~95% forever
    # while writes already fail; this denominator reaches 100% exactly
    # when the app's writable space runs out (CodeRabbit, PR #33).
    writable = usage.used + usage.free
    if writable <= 0:
        return None
    return {
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_pct": round(usage.used / writable * 100, 1),
    }


def tier_for(used_pct: float, prev_tier: int) -> int:
    """Pure tier decision with hysteresis. Rising edges use the plain
    thresholds; falling out of a tier requires CLEAR_MARGIN_PCT of real
    headroom below it."""
    if used_pct >= URGENT_PCT or (prev_tier >= 2
                                  and used_pct > URGENT_PCT - CLEAR_MARGIN_PCT):
        return 2
    if used_pct >= WARN_PCT or (prev_tier >= 1
                                and used_pct > WARN_PCT - CLEAR_MARGIN_PCT):
        return 1
    return 0


def fmt_bytes(n: int) -> str:
    if n >= 10 * 1024**3:
        return f"{n / 1024**3:.0f} GB"
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    return f"{max(n, 0) / 1024**2:.0f} MB"


def build_message(tier: int, used_pct: float, free_bytes: int,
                  total_bytes: int) -> tuple[str, str]:
    free = fmt_bytes(free_bytes)
    total = fmt_bytes(total_bytes)
    if tier >= 2:
        title = f"Server disk almost full ({used_pct:.0f}%)"
        body = (f"The volume holding the weather database has {free} free "
                f"of {total}. Ingest, backups, and updates start failing "
                f"once it fills — extend the volume or prune history now.")
    elif tier == 1:
        title = f"Server disk {used_pct:.0f}% full"
        body = (f"The volume holding the weather database has {free} free "
                f"of {total}. History grows steadily and backups need "
                f"headroom — plan to extend the volume or prune history.")
    else:
        title = "Server disk space recovered"
        body = (f"The volume is back to {used_pct:.0f}% used "
                f"({free} free of {total}).")
    return title, body


async def check(cfg, now_ms: int, deliver) -> None:
    stats = snapshot()
    if stats is None:
        return
    states = await db.get_smart_alert_states()
    prev = states.get((_SERVER_MAC, _KIND), 0)
    tier = tier_for(stats["used_pct"], prev)
    if tier == prev:
        return
    if tier > prev:
        title, body = build_message(tier, stats["used_pct"],
                                    stats["free_bytes"], stats["total_bytes"])
        # Urgent tier is a warning: quiet hours don't apply to a disk
        # about to take ingest down. Persist only after delivery is
        # handled (the release invariant) so a transient channel failure
        # retries next tick instead of vanishing.
        if await deliver(cfg, f"[Zasder Weather] {title}", body, title, body,
                         email_ok=cfg.email_scope == "all",
                         kind=_KIND, mac=None,
                         severity="warning" if tier >= 2 else None):
            await db.upsert_smart_alert_state(_SERVER_MAC, _KIND, tier, now_ms)
        return
    if tier == 0:
        title, body = build_message(0, stats["used_pct"],
                                    stats["free_bytes"], stats["total_bytes"])
        if await deliver(cfg, f"[Zasder Weather] {title}", body, title, body,
                         email_ok=cfg.email_scope == "all",
                         kind="disk_recovered", mac=None):
            await db.upsert_smart_alert_state(_SERVER_MAC, _KIND, 0, now_ms)
        return
    # 2 → 1: still low, no fresh alert to send — just record the tier so
    # a later climb back to urgent fires again.
    await db.upsert_smart_alert_state(_SERVER_MAC, _KIND, tier, now_ms)
