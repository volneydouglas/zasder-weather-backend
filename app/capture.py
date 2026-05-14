"""Catch-all capture endpoint for reverse-engineering proprietary station
protocols. Logs every inbound request to a JSON-lines file we can grep later
without reading through fly logs.

Mounted at /ingest/capture/{slug}/{path...}. The slug lets you tell different
stations apart — e.g. /ingest/capture/acurite-atlas/... vs /ingest/capture/
ecowitt-gw1100/... — without writing the parser yet."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()

# JSON-lines log lives next to the SQLite db on the persistent volume so we
# don't lose captures across redeploys. One file per slug; appended forever.
def _log_path(slug: str) -> Path:
    base = Path(os.environ.get("DATABASE_PATH", "./data/weather.db")).parent
    base = base / "captures"
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in slug if c.isalnum() or c in "-_") or "unknown"
    return base / f"{safe}.jsonl"


async def _capture(request: Request, slug: str, full_path: str) -> dict[str, Any]:
    body_bytes = await request.body()
    # Try to decode as text so a human can grep it; fall back to base64 marker.
    body_text: str | None
    try:
        body_text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        import base64
        body_text = "<binary base64=" + base64.b64encode(body_bytes).decode() + ">"
    record: dict[str, Any] = {
        "ts":     time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "slug":   slug,
        "method": request.method,
        "path":   "/" + full_path,
        "query":  dict(request.query_params),
        "headers": dict(request.headers),
        "remote": request.client.host if request.client else None,
        "body":   body_text,
        "body_len": len(body_bytes),
    }
    line = json.dumps(record, ensure_ascii=False)
    # Append to the on-disk log AND echo to stdout so `fly logs` shows it live.
    try:
        with _log_path(slug).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(f"CAPTURE {slug} {request.method} /{full_path} body={len(body_bytes)}b", flush=True)
    return record


# Catch-all routes for every method the station might use. Path can be empty
# (root POST) or arbitrary depth.
@router.api_route("/ingest/capture/{slug}",
                  methods=["GET","POST","PUT","PATCH","DELETE","HEAD"])
@router.api_route("/ingest/capture/{slug}/{full_path:path}",
                  methods=["GET","POST","PUT","PATCH","DELETE","HEAD"])
async def capture_any(request: Request, slug: str, full_path: str = "") -> PlainTextResponse:
    await _capture(request, slug, full_path)
    # Most stations expect a 200 with an empty or terse body. Mimic that.
    return PlainTextResponse("OK", status_code=200)


# Also serve the typical Acurite Atlas vhost root path with a 200 so the hub
# considers the connection healthy even before we map specific paths.
@router.api_route("/ingest/acurite/{full_path:path}",
                  methods=["GET","POST","PUT","PATCH","DELETE","HEAD"])
async def capture_acurite(request: Request, full_path: str = "") -> PlainTextResponse:
    await _capture(request, "acurite-atlas", full_path)
    return PlainTextResponse("OK", status_code=200)
