"""Global request-body size limit — pure ASGI middleware.

Runs BEFORE FastAPI routing and route dependencies, so it bounds the body
before any JSON parsing or authentication happens. This closes the
unauthenticated memory-exhaustion vector an external review flagged:
FastAPI parses the JSON body before route deps authenticate, and a chunked
request (no Content-Length) can otherwise be streamed into memory up to the
VM's limit before a 401/422 is returned.

Two layers:
  * An honest `Content-Length` over the cap is rejected with 413 without
    reading a single body byte.
  * For chunked / missing-length bodies we count bytes as they stream and,
    the moment the running total crosses the cap, hand the app a terminal
    empty chunk — so no more than ~one chunk past the cap is ever buffered.
    The route's own JSON parse then fails fast on the truncated body. The
    guarantee we care about (bounded memory for anonymous requests) holds
    regardless of how the client frames the request.

Cap defaults to 1 MiB — comfortably above every legitimate request
(observations are ~500 B; ingest endpoints enforce their own 16-64 KiB
limits on top of this). Tune with MAX_REQUEST_BYTES.
"""

import os

_DEFAULT_MAX = 1 * 1024 * 1024  # 1 MiB


def _max_bytes() -> int:
    try:
        v = int((os.environ.get("MAX_REQUEST_BYTES") or "").strip())
        return v if v > 0 else _DEFAULT_MAX
    except ValueError:
        return _DEFAULT_MAX


class BodySizeLimitMiddleware:
    """Reject / bound request bodies larger than `max_bytes` at the ASGI layer."""

    def __init__(self, app, max_bytes: int | None = None):
        self.app = app
        self.max_bytes = max_bytes if max_bytes is not None else _max_bytes()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                pass  # Malformed — fall through to the streaming counter.

        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    # Truncate the stream: a terminal empty chunk means the
                    # app can't buffer any more. The route's JSON parse then
                    # rejects the short body. Memory stays bounded.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        await self.app(scope, limited_receive, send)

    async def _reject(self, send):
        body = b'{"detail":"request body too large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
