"""A rolling 24 hours of each source's health (2.4, item 3).

`source_status` answers "is this poller working RIGHT NOW", in memory,
and forgets everything at a restart. That is the wrong shape for the
question people actually ask after a gap in their charts, which is "what
happened overnight, and whose fault was it".

So: one run-length encoded record per source, in `server_kv`, holding at
most 24 hours. A sample is written only when the VERDICT CHANGES, plus a
keep-alive every quarter hour so a long quiet stretch still has an
endpoint to draw. That is a handful of writes a day instead of one per
poll tick per source, and it survives the restart that loses the
in-memory view.

THE THREE VERDICTS, and why the distinction is the whole feature. A
station that stops updating looks identical in every app whatever the
cause, and the fix is completely different in each case:

  vendor   the service did not answer, or refused our credentials. Not
           your fault and not ours. Waiting is usually right.
  device   the service answered and had nothing new for this station.
           The hardware or its own uplink is the thing to go and look at.
  ours     the call worked and the rows were there, and the failure was
           on this side of the wire.
  ok       readings arrived.

`unknown` is its own answer and not a fourth failure: it is what a
stretch reads as when the server was not running, and drawing that as
health would be a lie about a period nobody was watching.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import db

log = logging.getLogger("health")

WINDOW_MS = 24 * 3_600_000
# A run this old is dropped entirely; one that straddles the edge is
# clipped when it is read, not when it is written.
_KEY_PREFIX = "source_health.v1."
# Even with no change, stamp the current verdict this often so a quiet
# day is a drawable run rather than a single point.
KEEPALIVE_MS = 15 * 60_000
# A run may not bridge more than this: the watcher stamps a steady source
# every KEEPALIVE_MS, so silence twice that long is the server not
# running, and extending the last run across it would paint six hours
# nobody watched as healthy (R24-05, 2.4 release review). The old run
# closes where its last stamp was and the gap draws as unknown.
MAX_BRIDGE_MS = 2 * KEEPALIVE_MS
# How long the service may answer with nothing new before the station,
# not the service, is the verdict. Pollers tick every minute against
# stations that upload every one to five minutes, so a single empty
# tick is the normal case and not a quiet station; read as one, it
# painted the own box's Tempest strip `doddddddddddo` on a day with
# 98 percent of its readings in hand.
DEVICE_QUIET_MS = 15 * 60_000

OK = "ok"
VENDOR = "vendor"
DEVICE = "device"
OURS = "ours"
UNKNOWN = "unknown"
VERDICTS = (OK, VENDOR, DEVICE, OURS, UNKNOWN)

# How an error kind from source_status.classify maps onto who has to act.
# credentials and rate_limit are the vendor's side of the wire even though
# the owner may be the one to fix the key: the point of the verdict is
# WHERE the chain broke, and the app already says what to do about it.
_ERROR_VERDICT = {
    "upstream": VENDOR,
    "credentials": VENDOR,
    "rate_limit": VENDOR,
    "ours": OURS,
}


def verdict_for(now_ms: int, row: dict[str, Any]) -> str:
    """The verdict for one source_status row, this instant.

    A poller that is configured and has never once succeeded reads as
    `unknown` rather than as a failure: a server that booted two minutes
    ago has not yet earned an opinion about anybody.
    """
    if not row.get("configured"):
        return UNKNOWN
    if row.get("consecutive_failures"):
        return _ERROR_VERDICT.get(row.get("last_error_kind") or "ours", OURS)
    if row.get("last_success_ms") is None:
        return UNKNOWN
    # The call worked. Whether anything came back is what separates a
    # quiet station from a working one.
    rows = row.get("last_rows")
    if rows is None or rows > 0:
        return OK
    since = row.get("last_rows_ms")
    if since is None or now_ms - int(since) > DEVICE_QUIET_MS:
        return DEVICE
    return OK


async def record(name: str, verdict: str, now_ms: int) -> None:
    """Append this instant's verdict to the source's record."""
    if verdict not in VERDICTS:
        return
    runs = await _load(name)
    silent = (now_ms - int(runs[-1].get("until_ms") or 0)) if runs else 0
    if runs and silent > MAX_BRIDGE_MS:
        # Unobserved time is not this verdict and not the last one either.
        runs.append({"from_ms": now_ms, "until_ms": now_ms, "verdict": verdict})
    elif runs and runs[-1]["verdict"] == verdict:
        # Same as before: only move the run's end, and only every so
        # often, so a steady source costs a write a quarter hour.
        if silent < KEEPALIVE_MS:
            return
        runs[-1]["until_ms"] = now_ms
    else:
        if runs:
            runs[-1]["until_ms"] = now_ms
        runs.append({"from_ms": now_ms, "until_ms": now_ms, "verdict": verdict})
    runs = _trim(runs, now_ms)
    await db.set_kv(_KEY_PREFIX + name, json.dumps(runs))


async def history(name: str, now_ms: int) -> list[dict[str, Any]]:
    """The last 24 hours for one source, oldest first, clipped to the
    window. Gaps between runs are the server not running and are left as
    gaps rather than invented."""
    return _trim(await _load(name), now_ms)


def summarise(runs: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    """What a strip needs in words as well as bars.

    `covered_ms` is how much of the day this record can speak for at all,
    which is the honest denominator: a server that was off for twelve
    hours does not get to claim 100 percent of a day it did not see.
    """
    totals = {v: 0 for v in VERDICTS}
    for r in runs:
        totals[r["verdict"]] = totals.get(r["verdict"], 0) + max(
            0, int(r["until_ms"]) - int(r["from_ms"]))
    covered = sum(totals.values())
    worst = None
    for v in (OURS, VENDOR, DEVICE):
        if totals.get(v):
            worst = v
            break
    current = runs[-1]["verdict"] if runs else UNKNOWN
    since = None
    if runs:
        since = int(runs[-1]["from_ms"])
        for r in reversed(runs):
            if r["verdict"] != current:
                break
            since = int(r["from_ms"])
    return {
        "hours": buckets(runs, now_ms),
        "window_ms": WINDOW_MS,
        "covered_ms": covered,
        "totals_ms": totals,
        "ok_fraction": (totals[OK] / covered) if covered else None,
        "worst": worst,
        "current": current,
        "current_since_ms": since,
    }


# One character per verdict, for the hourly strip the apps draw. A string
# of 24 of these is the whole strip, which is small enough to ride on
# every device row without anyone thinking about it.
BUCKET_CHARS = {OK: "o", VENDOR: "v", DEVICE: "d", OURS: "x", UNKNOWN: "-"}
# Which verdict wins when an hour held more than one. The order is who
# has to act, most urgent first, because an hour that was mostly fine
# with ten minutes of outage in it is an hour with an outage in it.
_BUCKET_PRIORITY = (OURS, VENDOR, DEVICE, OK, UNKNOWN)


def buckets(runs: list[dict[str, Any]], now_ms: int, count: int = 24) -> str:
    """The last `count` hours as one character each, oldest first.

    An hour nothing is known about is "-", which draws as absent rather
    than as health. That distinction is the point: a server that was off
    has no opinion about those hours and must not appear to have one.
    """
    span = WINDOW_MS // count
    start = now_ms - span * count
    seen: list[set] = [set() for _ in range(count)]
    for r in runs:
        a, b = int(r["from_ms"]), int(r["until_ms"])
        if b <= start:
            continue
        first = max(0, (a - start) // span)
        # Half open, [from, until): a run that ENDS exactly on the hour
        # belongs to the hour before it, not to the one starting there.
        # A zero length run still colours the bucket it falls in, which
        # is what a single sample means.
        end = max(a, min(b, now_ms))
        last = min(count - 1, (max(end - 1, a) - start) // span)
        for i in range(int(first), int(last) + 1):
            seen[i].add(r["verdict"])
    out = []
    for got in seen:
        verdict = next((v for v in _BUCKET_PRIORITY if v in got), UNKNOWN)
        out.append(BUCKET_CHARS[verdict])
    return "".join(out)


async def _load(name: str) -> list[dict[str, Any]]:
    raw = await db.get_kv(_KEY_PREFIX + name)
    if not raw:
        return []
    try:
        runs = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(runs, list):
        return []
    out = []
    for r in runs:
        if (isinstance(r, dict) and isinstance(r.get("from_ms"), int)
                and isinstance(r.get("until_ms"), int)
                and r.get("verdict") in VERDICTS):
            out.append(dict(r))
    return out


def _trim(runs: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    edge = now_ms - WINDOW_MS
    out = []
    for r in runs:
        if int(r["until_ms"]) <= edge:
            continue
        r = dict(r)
        if int(r["from_ms"]) < edge:
            r["from_ms"] = edge
        out.append(r)
    return out


async def clear(name: str) -> None:
    """Forget one source's record. `set_kv(None)` is the delete here —
    db has a prefix sweep and no single-key delete."""
    await db.set_kv(_KEY_PREFIX + name, None)
