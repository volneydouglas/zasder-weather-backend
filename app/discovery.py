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

import time
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from . import db
from .config import settings

router = APIRouter()


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
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    model = str(payload.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="payload.model required")
    # rtl_433 sometimes emits id as int, sometimes str, sometimes missing.
    sensor_id = str(payload.get("id") if payload.get("id") is not None else "?")
    now_ms = int(time.time() * 1000)
    await db.upsert_discovery(model, sensor_id, now_ms, payload)
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
