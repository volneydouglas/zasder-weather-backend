"""SDR-discovery ingest + read endpoints.

The SDR captures hundreds of decoded packets per day from RF devices
nearby (neighbors' weather stations, TPMS, utility meters, garage
remotes, etc). The "discoveries" table dedupes those into one row per
(model, sensor_id) so the user can see the long tail of what's on the
airwaves without polluting the observations table or the iOS app's
device list.

POST /ingest/discovery   — relay calls this for every decoded packet,
                            rate-limited to once per minute per device
GET  /api/discoveries    — survey of nearby RF traffic, latest first
"""
from __future__ import annotations

import json as _json
import time
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from . import db
from .config import settings

router = APIRouter()


# Same caps as /ingest/custom — discovery payloads are even smaller
# (single rtl_433 packet ~300 bytes), so 64 KiB is far past anything
# legitimate.
DISCOVERY_BODY_MAX_BYTES   = 64 * 1024
DISCOVERY_SAMPLE_MAX_BYTES = 16 * 1024


async def _parse_json_body(request: Request) -> Any:
    """Parse a JSON body and 400 (not 500) on malformed input. Also
    rejects Python's NaN/Infinity literals via parse_constant. Enforces
    a size cap to bound worker memory."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > DISCOVERY_BODY_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"body too large; max {DISCOVERY_BODY_MAX_BYTES} bytes")
        except ValueError:
            pass
    body = await request.body()
    if len(body) > DISCOVERY_BODY_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"body too large; max {DISCOVERY_BODY_MAX_BYTES} bytes")
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        return _json.loads(body, parse_constant=lambda _: None)
    except _json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e.msg}")


def _truncate_sample(payload: dict[str, Any]) -> dict[str, Any]:
    """Cap the stored sample so a pathological decoder spew can't bloat
    the discoveries table. Falls back to model/id/time-only marker when
    the original is over the limit."""
    raw = _json.dumps(payload, separators=(",", ":"))
    if len(raw) <= DISCOVERY_SAMPLE_MAX_BYTES:
        return payload
    return {
        "_truncated": True,
        "_original_bytes": len(raw),
        "model": payload.get("model"),
        "id": payload.get("id"),
        "time": payload.get("time"),
    }


def _require_ingest_token(authorization: str | None,
                          x_ingest_token: str | None) -> None:
    expected = settings.ingest_token
    presented = ""
    if x_ingest_token: presented = x_ingest_token.strip()
    elif authorization and authorization.startswith("Bearer "):
        presented = authorization.removeprefix("Bearer ").strip()
    if not expected or presented != expected:
        raise HTTPException(status_code=401, detail="invalid ingest token")


def _require_api_token(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid token")
    presented = authorization.removeprefix("Bearer ")
    if presented not in settings.valid_api_tokens:
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/ingest/discovery")
async def ingest_discovery(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ingest_token: Annotated[str | None, Header(alias="X-Ingest-Token")] = None,
) -> dict[str, Any]:
    """Upsert a (model, id) sighting. Payload is the raw rtl_433 decoded
    record — we store the first one verbatim as a sample for inspection
    and bump counters on subsequent sightings."""
    _require_ingest_token(authorization, x_ingest_token)
    payload = await _parse_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    model = str(payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="payload.model required")
    # rtl_433 sometimes emits id as int, sometimes str, sometimes missing.
    sensor_id = str(payload.get("id") if payload.get("id") is not None else "?")
    now_ms = int(time.time() * 1000)
    await db.upsert_discovery(model, sensor_id, now_ms, _truncate_sample(payload))
    return {"ok": True, "model": model, "id": sensor_id}


@router.get("/api/discoveries")
async def list_discoveries(
    since_hours: int = Query(0, ge=0, le=24 * 365),
    limit: int = Query(500, ge=1, le=5000),
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Survey of distinct RF devices the SDR has decoded. since_hours=0
    means 'all-time'; otherwise restrict to devices last seen in that
    window."""
    _require_api_token(authorization)
    since_ms = None
    if since_hours > 0:
        since_ms = int(time.time() * 1000) - since_hours * 3600 * 1000
    rows = await db.list_discoveries(since_ms=since_ms, limit=limit)
    return {"count": len(rows), "since_hours": since_hours, "rows": rows}
