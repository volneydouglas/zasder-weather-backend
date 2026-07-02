"""Catch-all capture endpoint for reverse-engineering proprietary station
protocols. Logs every inbound request to a JSON-lines file we can grep later
without reading through fly logs.

Mounted at /ingest/capture/{slug}/{path...}. The slug lets you tell different
stations apart — e.g. /ingest/capture/acurite-atlas/... vs /ingest/capture/
ecowitt-gw1100/... — without writing the parser yet.

Security: gated behind a token (CAPTURE_TOKEN env var) and bounds bodies at
MAX_BODY_BYTES so a random POST flood can't exhaust disk or worker memory.
When CAPTURE_TOKEN isn't set, the endpoint is disabled entirely (returns
404), since reverse-engineering is normally a one-off task."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .config import tokens_match

router = APIRouter()

# Hard limit on a captured payload. Real station POSTs are < 4 KB; 64 KB is
# generous and keeps a single request from spiking memory.
MAX_BODY_BYTES = 64 * 1024

# JSON-lines log lives next to the SQLite db on the persistent volume so we
# don't lose captures across redeploys. One file per slug; appended forever.
def _log_path(slug: str) -> Path:
    base = Path(os.environ.get("DATABASE_PATH", "./data/weather.db")).parent
    base = base / "captures"
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in slug if c.isalnum() or c in "-_") or "unknown"
    return base / f"{safe}.jsonl"


def _require_capture_token(authorization: str | None, query_token: str | None) -> None:
    expected = os.environ.get("CAPTURE_TOKEN", "").strip()
    if not expected:
        # Endpoint disabled entirely when no token is configured. We return
        # 404 (not 401) so a port scan can't tell the route exists.
        raise HTTPException(status_code=404, detail="not found")
    presented: str | None = None
    if authorization and authorization.startswith("Bearer "):
        presented = authorization.removeprefix("Bearer ").strip()
    elif query_token:
        presented = query_token.strip()
    if not presented or not tokens_match(presented, expected):
        raise HTTPException(status_code=404, detail="not found")


# Headers + query-params we redact before writing to the JSONL log so the
# capture token doesn't end up in plaintext where anyone with the API token
# could read it back via /api/captures/{slug}.
_REDACT_HEADERS = {"authorization", "cookie", "x-capture-token", "proxy-authorization"}
_REDACT_QUERY   = {"t", "token", "api_key", "apikey", "auth"}


def _redact_dict(d: dict[str, Any], drop_keys: set[str]) -> dict[str, Any]:
    """Return a copy of `d` with any keys in drop_keys (case-insensitive)
    replaced by the literal string "<redacted>"."""
    return {k: ("<redacted>" if k.lower() in drop_keys else v) for k, v in d.items()}


async def _capture(request: Request, slug: str, full_path: str) -> dict[str, Any]:
    # Refuse oversized payloads early — both via Content-Length and by
    # enforcing the max during read. A misbehaving station can't fill the
    # volume with one giant POST.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body_bytes = b""
    async for chunk in request.stream():
        body_bytes += chunk
        if len(body_bytes) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
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
        # Redact bearer tokens / capture-token query params so the JSONL log
        # doesn't store secrets that would be re-emitted by /api/captures.
        "query":  _redact_dict(dict(request.query_params), _REDACT_QUERY),
        "headers": _redact_dict(dict(request.headers), _REDACT_HEADERS),
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
# (root POST) or arbitrary depth. ALL of them are token-gated — the operator
# must explicitly opt into capture by setting CAPTURE_TOKEN. Token can be
# supplied via Authorization: Bearer (preferred) or ?t= query param so a
# script-less station can still hit the endpoint.
@router.api_route("/ingest/capture/{slug}",
                  methods=["GET","POST","PUT","PATCH","DELETE","HEAD"])
@router.api_route("/ingest/capture/{slug}/{full_path:path}",
                  methods=["GET","POST","PUT","PATCH","DELETE","HEAD"])
async def capture_any(
    request: Request,
    slug: str,
    full_path: str = "",
    authorization: Annotated[str | None, Header()] = None,
    t: str | None = None,
) -> PlainTextResponse:
    _require_capture_token(authorization, t)
    await _capture(request, slug, full_path)
    return PlainTextResponse("OK", status_code=200)


@router.api_route("/ingest/acurite/{full_path:path}",
                  methods=["GET","POST","PUT","PATCH","DELETE","HEAD"])
async def capture_acurite(
    request: Request,
    full_path: str = "",
    authorization: Annotated[str | None, Header()] = None,
    t: str | None = None,
) -> PlainTextResponse:
    _require_capture_token(authorization, t)
    await _capture(request, "acurite-atlas", full_path)
    return PlainTextResponse("OK", status_code=200)
