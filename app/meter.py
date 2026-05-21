"""Utility-meter ingest endpoint.

Captures broadcast readings from neighborhood utility meters that the SDR
happens to hear on 915 MHz (Neptune R900 water, Itron ERT electric/gas,
etc.). The data shape varies by meter family so we don't try to normalize
into the weather schema — we just append-log per-meter to JSONL and expose
a read endpoint scoped by meter ID.

Useful primarily for "is THIS one mine?" identification: turn on a hose,
see which meter's consumption counter jumps. Long-term, this could feed a
separate iOS tab — but for now it's read-only telemetry the SDR captures
incidentally.

Endpoints:
  POST /ingest/meter         (auth via INGEST_TOKEN, same as /ingest/custom)
  GET  /api/meters           (auth via API_TOKEN; lists distinct meter IDs)
  GET  /api/meters/{id}/recent?tail=50  (auth; latest readings for one meter)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam, Query, Request

from .config import settings

router = APIRouter()

# One JSONL file per meter — keeps lookups fast and lets us cap individual
# meters' history without affecting others. Lives on the same Fly volume as
# the SQLite DB so it survives deploys. Resolved per-call so test fixtures
# that swap DATABASE_PATH per-test get their own isolated meters dir.
def _meter_dir() -> Path:
    return Path(settings.database_path).parent / "meters"


def _meter_path(meter_id: str) -> Path:
    safe = "".join(c for c in meter_id if c.isalnum() or c in "_-")[:64]
    if not safe:
        raise HTTPException(status_code=400, detail="invalid meter id")
    return _meter_dir() / f"{safe}.jsonl"


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


@router.post("/ingest/meter")
async def ingest_meter(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ingest_token: Annotated[str | None, Header(alias="X-Ingest-Token")] = None,
) -> dict[str, Any]:
    """Append a meter reading to its per-meter JSONL log. Payload is the
    rtl_433-style record (whatever fields the decoder produced) plus an
    `id` we use for filename routing."""
    _require_ingest_token(authorization, x_ingest_token)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        payload = json.loads(body, parse_constant=lambda _: None)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e.msg}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    meter_id = str(payload.get("id") or "")
    if not meter_id:
        raise HTTPException(status_code=400, detail="payload.id required")
    _meter_dir().mkdir(parents=True, exist_ok=True)
    record = {"received_ms": int(time.time() * 1000), **payload}
    with _meter_path(meter_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "id": meter_id}


@router.get("/api/meters")
async def list_meters(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_api_token(authorization)
    mdir = _meter_dir()
    if not mdir.exists():
        return {"meters": []}
    meters = []
    for p in sorted(mdir.glob("*.jsonl")):
        try:
            stat = p.stat()
            # Quick peek at the last line for the latest reading
            with p.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                # Read up to last 2KB and split — handles arbitrarily large last line up to that cap
                f.seek(max(0, size - 2048))
                tail = f.read().decode("utf-8", "ignore").splitlines()
                last = json.loads(tail[-1]) if tail else {}
        except (OSError, json.JSONDecodeError, IndexError):
            last = {}
        meters.append({
            "id": p.stem,
            "size_bytes": stat.st_size if "stat" in dir() else 0,
            "model": last.get("model"),
            "last_received_ms": last.get("received_ms"),
            "last": last,
        })
    return {"meters": meters}


@router.get("/api/meters/{meter_id}/recent")
async def meter_recent(
    meter_id: Annotated[str, PathParam()],
    tail: int = Query(50, ge=1, le=5000),
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_api_token(authorization)
    path = _meter_path(meter_id)
    if not path.exists():
        return {"id": meter_id, "rows": []}
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()[-tail:]
    rows: list[dict] = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"id": meter_id, "count": len(rows), "rows": rows}
