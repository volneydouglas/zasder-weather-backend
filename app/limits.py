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

One path is exempt: the database restore upload (2.1) is a whole SQLite
file, hundreds of MB on a years-deep archive. Its route reads the body
only AFTER the write-token dependency has passed and streams it to disk
under its own free-space bound (app/restore.py), so an anonymous request
still costs no memory: FastAPI answers 401 without touching the stream.
"""

import os

_DEFAULT_MAX = 1 * 1024 * 1024  # 1 MiB
# Exact paths whose routes bound their own body, after authentication.
EXEMPT_PATHS = frozenset({"/api/backup/database/restore"})

# Paths with their OWN cap, still enforced here at the ASGI layer so an
# anonymous body is bounded before a byte is parsed (R24-02, 2.4 release
# review: both archive doors answered 413 to a 1.2 MB file, the size a
# real migration starts at). The WeeWX door streams its body to disk
# after the write-token dependency, like the restore, so its cap is a
# disk bound; the CSV door is a JSON field FastAPI reads into memory
# before authentication, so its cap stays modest and matches the model's
# max_length in main.py.
CSV_IMPORT_MAX = 16 * 1024 * 1024
WEEWX_IMPORT_MAX = 512 * 1024 * 1024
PATH_LIMITS: dict[str, int] = {
    "/api/import/csv": CSV_IMPORT_MAX,
    "/api/import/weewx": WEEWX_IMPORT_MAX,
}


def _max_bytes() -> int:
    try:
        v = int((os.environ.get("MAX_REQUEST_BYTES") or "").strip())
        return v if v > 0 else _DEFAULT_MAX
    except ValueError:
        return _DEFAULT_MAX


class _Overflow(Exception):
    """Raised inside the receive channel when a chunked body crosses its
    limit, so the request ends in a rejection rather than in a body that
    happens to parse. Truncating the stream to EOF, as this did before,
    let a valid CSV prefix import and a valid WeeWX prefix be written
    and accepted (CodeRabbit, PR #40)."""


class BodySizeLimitMiddleware:
    """Reject / bound request bodies larger than `max_bytes` at the ASGI layer."""

    def __init__(self, app, max_bytes: int | None = None):
        self.app = app
        self.max_bytes = max_bytes if max_bytes is not None else _max_bytes()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        max_bytes = PATH_LIMITS.get(scope.get("path") or "", self.max_bytes)
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                pass  # Malformed — fall through to the streaming counter.

        total = 0
        started = False

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > max_bytes:
                    # Not a terminal empty chunk: that read as EOF and the
                    # route parsed whatever prefix it had. The route sees
                    # an exception mid-read (its cleanup runs), and the
                    # client sees 413. Memory stays bounded either way.
                    raise _Overflow()
            return message

        async def watched_send(message):
            nonlocal started
            started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, watched_send)
        except _Overflow:
            # A 413 only while nothing has gone out yet; a response that
            # already started cannot be taken back, and the route has
            # already refused the short body on its own.
            if not started:
                await self._reject(send, "request body too large (crossed the limit mid-stream)")

    async def _reject(self, send, detail: str = "request body too large"):
        # The streaming branch names itself, so a test can tell it from
        # the Content-Length fast path by the answer alone.
        body = ('{"detail":"%s"}' % detail).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
