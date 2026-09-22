"""NWS alert relay (1.8, Pillar A): server-side polling of
api.weather.gov so severe weather reaches every channel the backend
owns — push (warning tier, breaks quiet hours), the alert history, the
digest, and webhooks — instead of living only in a foregrounded app.

Etiquette per the NWS API docs: the `?point=` query resolves warning
polygons server-side (their guidance for point consumers); a mandatory
identifying User-Agent; a 10-minute cadence per station, far inside
their allowance; failures back off silently to the next window. NWS
retired the legacy feeds in Dec 2025 — this API is the only path now.

Only Severe/Extreme severities push (the app's own NWS view still shows
everything); each alert id pushes ONCE GLOBALLY, with the seen-set
bounded and persisted in server_kv. 2.3 adds the owner's switch
(`nws_push`, on unless turned off), a warnings-only filter, and reissue
dedupe: NWS mints a new id for every update of the same alert, so a
Flood Watch extended at 9 PM pushed twice (Doren, 2026-09-11) and one
Extreme Heat Warning pushed five times in three days. An Update or
Cancel whose `references` name an id already PUSHED is recorded silently
and INHERITS the pushed status, so a chain u1 -> u2 -> u3 (each Update
naming only the one before it, the common NWS shape: 75 of 95 live
Updates on 2026-09-16 referenced exactly one message) pushes once, not
every other time (R23). The one exception is an Update that ESCALATES
what was pushed: a higher severity (Moderate < Severe < Extreme) or an
event that has newly become a Warning pushes again, because the sky
changed; an unchanged extension stays silent. Global, not per-station: three
stations in one backyard share one sky, and the per-station sets pushed
the same Extreme Heat Warning three times (Volney's phone, 2026-08-26).
The alert is titled by whichever station's poll surfaced it first.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from . import db, nws_families
from .version import __version__

log = logging.getLogger("nws")

_INTERVAL_MS = 10 * 60_000
_UA = (f"zasder-weather-backend/{__version__} "
       "(github.com/volneydouglas/zasder-weather-backend)")
_PUSH_SEVERITIES = ("Severe", "Extreme")
# Global cap (the set now covers every station): NWS ids are long-lived
# URLs, and >100 simultaneously-active alerts for one household's points
# would be a national emergency, not a cache-pressure problem.
_SEEN_CAP = 120
_SEEN_KEY = "nws_watch.seen"
# 2.3: the ids that were actually PUSHED, apart from the seen-set, which
# also holds filtered and non-push ids. Reissue dedupe consults this one:
# an Update that upgrades a never-pushed alert (Moderate to Severe, or a
# Watch under warnings-only becoming a Warning) must push (CodeRabbit
# round on PR #39, then the 09-12 design docs' review).
_PUSHED_KEY = "nws_watch.pushed"
# R23: what each pushed id was pushed AS — [severity rank, is_warning] —
# so an Update that escalates a pushed alert can be told from an
# extension of it. An id in the ledger with no meta (pushed before this
# key existed) is treated as already at the top: it never re-pushes.
_PUSHED_META_KEY = "nws_watch.pushed_meta"
_SEVERITY_RANK = {"Unknown": 0, "Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}

_last_poll_ms: dict[str, int] = {}


def _reset_for_tests() -> None:
    _last_poll_ms.clear()


def is_warning(event) -> bool:
    """NWS event names end in the product's class: "Tornado Warning",
    "Flood Watch", "Wind Advisory", "Special Weather Statement".

    Takes whatever the feed held, like nws_families.event_name: this is
    reached from `pushed_as` AFTER a delivery, so a non-string here would
    raise between the push and the ledger write and re-push the alert
    next tick (CodeRabbit, PR #40)."""
    return nws_families.event_name(event).lower().endswith("warning")


def severity_rank(severity: Any) -> int:
    return _SEVERITY_RANK.get(str(severity or ""), 0)


def _referenced_pushed(alert: dict[str, Any], pushed: set[str]) -> list[str]:
    out: list[str] = []
    for r in alert.get("references") or []:
        for ident in _reference_ids(r):
            if ident in pushed and ident not in out:
                out.append(ident)
    return out


def is_reissue(alert: dict[str, Any], pushed: set[str]) -> bool:
    """An Update or Cancel that references an alert already PUSHED. A
    new id whose references are all unknown (the server missed the
    original, or filtered it) is new to us and pushes."""
    if alert.get("messageType") not in ("Update", "Cancel"):
        return False
    if _referenced_pushed(alert, pushed):
        return True
    return alert.get("messageType") == "Cancel"


def pushed_as(alert: dict[str, Any]) -> list:
    """The meta a pushed id is recorded with: [severity rank, warning]."""
    return [severity_rank(alert.get("severity")),
            is_warning(alert.get("event") or "")]


def _meta_of(ident: str, meta: dict[str, Any]) -> tuple[int, bool]:
    m = meta.get(ident)
    if (isinstance(m, list) and len(m) == 2 and isinstance(m[0], int)
            and not isinstance(m[0], bool)):
        return m[0], bool(m[1])
    return max(_SEVERITY_RANK.values()), True     # legacy: never a raise


def escalates(alert: dict[str, Any], pushed: set[str],
              meta: dict[str, Any]) -> bool:
    """R23 product decision: an Update of a pushed alert pushes again
    when it RAISES the severity or newly becomes a Warning. What it is
    compared against is the highest the chain was ever pushed at, so a
    downgrade followed by a return to the old level is not a raise."""
    if alert.get("messageType") != "Update":
        return False
    refs = _referenced_pushed(alert, pushed)
    if not refs:
        return False
    rank, warning = pushed_as(alert)
    top_rank = max(_meta_of(i, meta)[0] for i in refs)
    was_warning = any(_meta_of(i, meta)[1] for i in refs)
    return rank > top_rank or (warning and not was_warning)


def inherited_meta(alert: dict[str, Any], pushed: set[str],
                   meta: dict[str, Any]) -> list:
    """A silent reissue joins the pushed ledger carrying the HIGHEST of
    its own level and the levels it references (see `escalates`)."""
    rank, warning = pushed_as(alert)
    for i in _referenced_pushed(alert, pushed):
        r, w = _meta_of(i, meta)
        rank, warning = max(rank, r), warning or w
    return [rank, warning]


async def _load_pushed() -> list[str]:
    raw = await db.get_kv(_PUSHED_KEY)
    if raw is None:
        return []
    try:
        lst = json.loads(raw)
        return [x for x in lst if isinstance(x, str)] if isinstance(lst, list) else []
    except ValueError:
        return []


async def _load_pushed_meta(pushed: list[str]) -> dict[str, Any]:
    raw = await db.get_kv(_PUSHED_META_KEY)
    if raw is None:
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(d, dict):
        return {}
    keep = set(pushed)
    return {k: v for k, v in d.items() if k in keep}


def _reference_ids(ref: Any) -> list[str]:
    """The ids a reference may name, in the compact form the seen-set
    stores (`properties.id`): `identifier` as given, and the last path
    segment of the `@id` URL (CodeRabbit, PR #39: a reference carrying
    only the URL form missed the seen id and pushed a duplicate)."""
    if isinstance(ref, str):
        return [ref, ref.rstrip("/").rsplit("/", 1)[-1]]
    if not isinstance(ref, dict):
        return []
    out: list[str] = []
    ident = ref.get("identifier")
    if isinstance(ident, str) and ident:
        out.append(ident)
    at_id = ref.get("@id")
    if isinstance(at_id, str) and at_id:
        out.append(at_id)
        out.append(at_id.rstrip("/").rsplit("/", 1)[-1])
    return out


def _coords(device: dict[str, Any]) -> tuple[float, float] | None:
    info = device.get("info") or {}
    coords = (info.get("coords") or {}).get("coords") or {}
    lat, lon = coords.get("lat"), coords.get("lon")
    if lat is None or lon is None:
        return None
    # Stored coords are operator data, not validated at write time — one
    # non-numeric record used to raise here and kill the WHOLE pass for
    # every station, every tick (R7 R10). Skip the bad station instead.
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


async def _fetch_active(lat: float, lon: float) -> list[dict[str, Any]] | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://api.weather.gov/alerts/active",
                params={"point": f"{lat:.4f},{lon:.4f}"},
                headers={"User-Agent": _UA,
                         "Accept": "application/geo+json"})
        if r.status_code != 200:
            return None
        feats = r.json().get("features") or []
        return [f.get("properties") or {} for f in feats]
    except Exception:
        log.debug("nws fetch failed", exc_info=True)
        return None


async def _load_seen(devices: list[dict[str, Any]]) -> list[str]:
    """The GLOBAL seen-id list. First run after the upgrade seeds it from
    the legacy per-station keys, so alerts already pushed under the old
    scheme don't re-push once as "new"."""
    raw = await db.get_kv(_SEEN_KEY)
    if raw is not None:
        try:
            seen = json.loads(raw)
            return seen if isinstance(seen, list) else []
        except ValueError:
            return []
    merged: list[str] = []
    have: set[str] = set()
    for d in devices:
        legacy = await db.get_kv(f"nws_watch.seen.{d.get('mac')}")
        if not legacy:
            continue
        try:
            ids = json.loads(legacy)
        except ValueError:
            continue
        if isinstance(ids, list):
            for aid in ids:
                if isinstance(aid, str) and aid not in have:
                    have.add(aid)
                    merged.append(aid)
    return merged


def _held_by_quiet_hours(cfg, event: str | None, now_ms: int) -> bool:
    """Whether this product's tier would be silenced by quiet hours right
    now. The same floor `deliver` applies (`_QUIET_HOURS_EXEMPT`), asked
    up front so the caller can leave the id unseen rather than spent."""
    from . import alerts                 # alerts imports this module
    from .config import settings
    if nws_families.tier(event) in alerts._QUIET_HOURS_EXEMPT:
        return False
    return alerts.in_quiet_hours(now_ms, settings.timezone,
                                 getattr(cfg, "quiet_start_min", None),
                                 getattr(cfg, "quiet_end_min", None))


async def check(cfg, devices: list[dict[str, Any]], now_ms: int,
                deliver) -> None:
    """One monitor-tick entry point; per-station poll cadence, ONE global
    dedup set across stations."""
    if not getattr(cfg, "nws_push", True):
        # Off: no poll at all. The seen-set is left as it is, so turning
        # the switch back on during a long-lived alert pushes it once.
        return
    warnings_only = bool(getattr(cfg, "nws_warnings_only", False))
    # 2.4 item 1: the muted families, and the same `allows` rule the
    # app's banner and the widget's triangle ask — one set of toggles,
    # honoured everywhere, or the owner has to mute the same family
    # twice and still hears it from the third place.
    muted = list(getattr(cfg, "nws_muted_families", ()) or ())
    seen: list[str] | None = None      # loaded lazily on the first due poll
    seen_set: set[str] = set()
    pushed: list[str] = []
    pushed_set: set[str] = set()
    pushed_meta: dict[str, Any] = {}
    changed = False
    pushed_changed = False
    for d in devices:
        # Air monitors carry coords too — polling them would double every
        # NWS push for the same sky.
        if db.is_air_monitor_device(d):
            continue
        target = _coords(d)
        if target is None:
            continue
        mac = d["mac"]
        if now_ms - _last_poll_ms.get(mac, 0) < _INTERVAL_MS:
            continue
        _last_poll_ms[mac] = now_ms
        alerts = await _fetch_active(*target)
        if alerts is None:
            continue
        if seen is None:
            raw_global = await db.get_kv(_SEEN_KEY)
            seen = await _load_seen(devices)
            pushed = await _load_pushed()
            pushed_set = set(pushed)
            pushed_meta = await _load_pushed_meta(pushed)
            # The pushed ledger is written FIRST below, so after a crash
            # between the two writes a pushed id may be missing from seen;
            # folding pushed into seen on load keeps it from pushing twice
            # (CodeRabbit, PR #39).
            for aid in pushed:
                if aid not in seen:
                    seen.append(aid)
                    changed = True          # persist the repaired ledger
            seen_set = set(seen)
            if raw_global is None:
                # First run under the global scheme: persist the merged
                # seed NOW and retire the legacy per-station keys — the
                # global key was only written on change, so a quiet server
                # re-read N legacy rows every tick forever (R9 T6).
                await db.set_kv(_SEEN_KEY, json.dumps(seen[-_SEEN_CAP:]))
                # Prefix sweep, not per-current-device: keys for since-
                # DELETED devices would otherwise linger forever (R10 U4).
                await db.delete_kv_prefix("nws_watch.seen.")
        name = d.get("name") or mac
        for a in alerts:
            aid = a.get("id")
            if not aid or aid in seen_set:
                continue
            # The normalised name is what gets CLASSIFIED; the fallback
            # is only ever a title (CodeRabbit, PR #40). They were the
            # same string for one commit, and "Weather alert" ends in
            # "alert" — so a malformed event read as an ADVISORY and
            # warnings-only silently dropped it, which is the opposite of
            # the loud-side rule an unidentifiable product is meant to
            # get.
            event = nws_families.event_name(a.get("event"))
            title_event = event or "Weather alert"
            reissue = (is_reissue(a, pushed_set)
                       and not escalates(a, pushed_set, pushed_meta))
            if (a.get("severity") not in _PUSH_SEVERITIES
                    or not nws_families.allows(event, muted, warnings_only)
                    or reissue):
                # Non-push severities, filtered events and reissues are
                # recorded immediately — there is nothing to retry.
                seen_set.add(aid)
                seen.append(aid)
                changed = True
                if reissue:
                    # The reissue stands in for what it references: the
                    # next Update names only THIS id (R23).
                    pushed_meta[aid] = inherited_meta(a, pushed_set, pushed_meta)
                    pushed_set.add(aid)
                    pushed.append(aid)
                    pushed_changed = True
                continue
            # 2.4 review: held is not handled. An Advisory (tier watch)
            # or a Statement (tier info) sits below the quiet-hours floor,
            # and `deliver` at night attempts nothing, reports the alert
            # handled, and this loop then recorded it pushed for good — a
            # Frost Advisory issued at 22:30 never reached the phone. So
            # the floor is asked HERE, before delivery: a held id stays
            # unseen and goes on the first tick after quiet hours end, if
            # the product is still active then. A warning or a watch
            # rides through the night exactly as before.
            if _held_by_quiet_hours(cfg, event, now_ms):
                log.debug("NWS %s held through quiet hours", title_event)
                continue
            headline = (a.get("headline") or a.get("description")
                        or "")[:180]
            title = f"{name}: {title_event}"
            body = headline or "See the app for details."
            # Persist-after-deliver: a failed push leaves the id unseen so
            # the next tick retries; a handled one is done for good.
            # 2.4 item 1: the product class sets the tier. Until now every
            # relayed alert rode `warning` — time-sensitive, through quiet
            # hours — so a Frost Advisory punched through Focus exactly as
            # a Tornado Warning did. A Watch now wakes without punching,
            # an Advisory is an ordinary push.
            if await deliver(cfg, f"[Zasder Weather] {title}", body,
                             title, body,
                             email_ok=cfg.email_scope == "all",
                             kind="nws", mac=mac,
                             severity=nws_families.tier(event)):
                seen_set.add(aid)
                seen.append(aid)
                pushed_set.add(aid)
                pushed.append(aid)
                pushed_meta[aid] = pushed_as(a)
                changed = pushed_changed = True
                log.info("NWS %s pushed (surfaced by %s)", title_event, name)
    # Pushed first: see the load-time merge above for why the order matters.
    if pushed_changed:
        kept = pushed[-_SEEN_CAP:]
        await db.set_kv(_PUSHED_KEY, json.dumps(kept))
        await db.set_kv(_PUSHED_META_KEY, json.dumps(
            {k: pushed_meta[k] for k in kept if k in pushed_meta}))
    if changed and seen is not None:
        await db.set_kv(_SEEN_KEY, json.dumps(seen[-_SEEN_CAP:]))
