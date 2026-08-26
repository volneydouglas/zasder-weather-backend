"""Export / restore the operator-configured state of a backend.

Everything a self-hoster sets up by hand and would have to rebuild from
memory if the server were lost: alert recipients and thresholds, per-device
monitoring, threshold rules, and device locations. A real user's backend
crash-looped this week and would have taken all of it with it.

Deliberately NOT included:

  * observations — that's the data, not the configuration, and it's large.
  * tokens — API_TOKEN / INGEST_TOKEN are deployment secrets held by Fly, not
    database rows. A config backup should be safe to keep; one that grants
    server access is not.
  * the SMTP password — write-only by design and never returned by the API
    (`main.py` exposes only `smtp_password_set`). Restoring therefore leaves
    it unset, and the restore response says so rather than letting someone
    discover it when an alert silently fails to send.

So this file is materially less dangerous than the per-device settings
export: it carries no credential. It does carry recipient email addresses and
an SMTP username, which is why the warning still exists, just a milder one.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from . import db

log = logging.getLogger("zasder.config_backup")

FORMAT_VERSION = 1

WARNING = ("This file contains your alert recipients and server settings. "
           "It does NOT contain your API tokens or SMTP password. Keep it "
           "somewhere private anyway — it lists the email addresses your "
           "alerts go to.")

# SMTP password is never read back out; listing the keys we DO carry makes it
# obvious what a restore will and won't put back.
_ALERT_PREF_KEYS = (
    "enabled", "default_threshold_min", "repeat_hours", "recipients",
    "smtp_host", "smtp_port", "smtp_username", "smtp_from",
    "smtp_tls", "smtp_ssl", "email_scope",
)


async def export_config() -> dict[str, Any]:
    prefs = await db.get_alert_prefs() or {}
    return {
        "_WARNING": WARNING,
        "version": FORMAT_VERSION,
        "exported_ms": int(time.time() * 1000),
        "alert_prefs": {k: prefs.get(k) for k in _ALERT_PREF_KEYS},
        "device_alert_prefs": await db.get_device_alert_prefs(),
        "alert_rules": [
            {k: r.get(k) for k in ("target_mac", "field", "comparator",
                                   "threshold", "enabled")}
            for r in await db.list_alert_rules()
        ],
        "device_locations": await db.device_locations(),
        # Recorded so a restore can say what it couldn't put back.
        "smtp_password_included": False,
    }


class RestoreError(ValueError):
    pass


# Sentinel distinguishing "invalid, skip this key" from a legitimate None
# (None clears the pref → env fallback).
_INVALID = object()


def _coerce_alert_pref(key: str, v: Any) -> Any:
    """Coerce one alert_prefs value to the type set_alert_prefs stores, or
    _INVALID to skip it."""
    if v is None:
        return None
    if key in ("enabled", "smtp_tls", "smtp_ssl"):
        if isinstance(v, bool) or v in (0, 1):
            return 1 if v else 0
        return _INVALID
    if key in ("default_threshold_min", "repeat_hours"):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return _INVALID
        return f if math.isfinite(f) and f >= 0 else _INVALID
    if key == "smtp_port":
        try:
            p = int(v)
        except (TypeError, ValueError):
            return _INVALID
        return p if 1 <= p <= 65535 else _INVALID
    if key == "email_scope":
        # Same closed set PUT /api/alerts enforces; None (handled above)
        # clears back to the "all" default.
        return v if v in ("all", "device_down") else _INVALID
    # String fields: recipients (comma-joined), smtp_host/username/from.
    # Control characters are rejected — a \n here corrupts email headers at
    # send time (same rule PUT /api/alerts enforces).
    if not isinstance(v, str):
        return _INVALID
    s = v.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        return _INVALID
    return s or None


async def import_config(payload: Any, *, replace_rules: bool = True) -> dict[str, Any]:
    """Apply a previously exported config.

    Returns a summary of what changed, because a restore that silently does
    nothing (wrong file, empty payload) is worse than one that fails.
    """
    if not isinstance(payload, dict):
        raise RestoreError("that file isn't a backend configuration backup")
    version = payload.get("version")
    # bool is an int subclass, so {"version": true} would sail through an
    # isinstance check alone (TEST_GAP_AUDIT R3-137d).
    if not isinstance(version, int) or isinstance(version, bool):
        raise RestoreError("that file isn't a backend configuration backup")
    if version > FORMAT_VERSION:
        raise RestoreError(
            f"backup format {version} is newer than this server understands "
            f"({FORMAT_VERSION}) — upgrade the backend first")

    summary: dict[str, Any] = {"alert_prefs": 0, "device_alert_prefs": 0,
                               "alert_rules": 0, "device_locations": 0,
                               "smtp_password_restored": False}

    prefs = payload.get("alert_prefs")
    if isinstance(prefs, dict):
        # Coerce/validate per key — the file is hand-editable, and applying a
        # string smtp_port or a dict recipients verbatim doesn't fail HERE, it
        # breaks alert delivery later at send time. Invalid entries are
        # skipped (and simply not counted) rather than failing the restore.
        fields = {}
        for k in _ALERT_PREF_KEYS:
            if k not in prefs:
                continue
            v = _coerce_alert_pref(k, prefs[k])
            if v is not _INVALID:
                fields[k] = v
        if fields:
            await db.set_alert_prefs(**fields)
            summary["alert_prefs"] = len(fields)

    dev_prefs = payload.get("device_alert_prefs")
    if isinstance(dev_prefs, dict):
        for mac, p in dev_prefs.items():
            if not isinstance(p, dict):
                continue
            await db.upsert_device_alert_pref(
                mac, bool(p.get("monitor", True)), p.get("threshold_min"))
            summary["device_alert_prefs"] += 1

    rules = payload.get("alert_rules")
    if isinstance(rules, list):
        # Parse and validate the whole replacement set FIRST. Deleting before
        # validating meant a file whose rules were all malformed wiped every
        # existing rule and put nothing back — the worst possible outcome for
        # a restore.
        staged: list[tuple[Any, str, str, float, bool]] = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            # target_mac must be a string or None: a hand-edited dict/list
            # here survived staging and blew up INSIDE create_alert_rule
            # (sqlite3.InterfaceError → 500) AFTER the existing rules were
            # deleted — exactly the delete-before-validate failure the
            # staging pass exists to prevent. Same for a NaN/Inf threshold,
            # which additionally 500s every later GET of the rules
            # (JSONResponse serializes with allow_nan=False).
            target_mac = r.get("target_mac")
            if target_mac is not None and not isinstance(target_mac, str):
                continue
            try:
                threshold = float(r["threshold"])
                field, comparator = str(r["field"]), str(r["comparator"])
            except (KeyError, TypeError, ValueError):
                continue          # skip a malformed rule, keep the rest
            if not math.isfinite(threshold):
                continue
            staged.append((target_mac, field, comparator, threshold,
                           bool(r.get("enabled", True))))
        # An explicitly empty list is a legitimate "clear my rules"; a list
        # that had entries but none survived validation is not.
        if staged or not rules:
            if replace_rules:
                # Rules have no stable identity across servers, so restoring
                # on top of existing ones would duplicate every rule.
                for existing in await db.list_alert_rules():
                    await db.delete_alert_rule(int(existing["id"]))
            for target, field, comparator, threshold, enabled in staged:
                created = await db.create_alert_rule(target, field,
                                                     comparator, threshold)
                if created and not enabled:
                    await db.set_alert_rule_enabled(int(created["id"]), False)
                summary["alert_rules"] += 1

    locations = payload.get("device_locations")
    if isinstance(locations, dict):
        for mac, loc in locations.items():
            if not isinstance(loc, dict):
                continue
            lat, lon = loc.get("lat"), loc.get("lon")
            if lat is None or lon is None:
                continue
            try:
                await db.set_device_location(mac, float(lat), float(lon),
                                             loc.get("label"),
                                             int(time.time() * 1000))
            except (TypeError, ValueError):
                continue
            summary["device_locations"] += 1

    if sum(v for v in summary.values() if isinstance(v, int)) == 0:
        raise RestoreError("nothing in that file could be restored — "
                           "is it a backend configuration backup?")
    log.info("config restored: %s", summary)
    return summary
