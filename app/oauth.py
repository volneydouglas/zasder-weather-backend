"""OAuth 2.1 for the MCP server (2.1): this backend is its own
authorization server, so claude.ai and ChatGPT custom connectors, which
take a URL and speak OAuth but have no field for a bearer header, can
reach `POST /mcp`.

The MCP authorization spec (2025-06-18, "Authorization") makes the MCP
server an OAuth 2.1 resource server and requires: RFC 9728 protected
resource metadata (with a `WWW-Authenticate: Bearer resource_metadata=`
challenge on 401), RFC 8414 authorization server metadata, RFC 7591
dynamic client registration (SHOULD), PKCE S256 (MUST), RFC 8707
`resource` indicators, refresh-token rotation for public clients, exact
redirect_uri matching, and HTTPS or localhost redirects. All of that is
here, and nothing more: one scope (`weather:read`), public clients only
(`token_endpoint_auth_method: none`), authorization-code and
refresh-token grants.

Who is the resource owner? Whoever can prove control of THIS server.
The consent page asks for a CONNECT CODE the owner minted in the app
(`POST /api/oauth/connect-code`, ten minutes, one use), or, for a client
the owner has already approved, the server's API token (owner) or a
read-only guest link token (guest); it is POSTed only to this same origin
and never logged. Registration stays open (RFC 7591) but a client is
inert until approved: its consent page shows no token field and its bad
requests get an error page, never a redirect (2.1 review, SEC-1). The
access token that comes back carries the role, and the MCP server is
read-only either way, so an OAuth session can never do more than the
credential typed into the page could.

Storage: three bounded tables in the main database (DDL in db.SCHEMA).
Codes and tokens are stored as SHA-256 hashes; the raw strings exist only
in the response that issued them. Clients expire when unused; codes live
five minutes and are single-use; access tokens an hour; refresh tokens
thirty days and rotate on every use. Rotating API_TOKEN does NOT
invalidate OAuth sessions — revoke a client from the app (or
`DELETE /api/oauth/clients/{id}`) to cut it off.

Rate limits are the control on the three open endpoints (register,
authorize, token): a sliding window per apparent client IP, the same
shape the hosted relay uses for its challenge nonces, written here
rather than imported because that module is private to the hosted tier.

Module state: the limiter map is process-global and this module is not on
conftest's per-test reload list, so tests call `reset_state()`; settings
are read through `config.settings` at call time for the same reason.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
from threading import Lock
from typing import Annotated, Any
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

log = logging.getLogger("api")

router = APIRouter()

SCOPE = "weather:read"
RESOURCE_PATH = "/mcp"
# RFC 9728 §3: a resource with a path component publishes its metadata at
# the path-suffixed well-known location; the bare one is served too for
# clients that only try that.
PROTECTED_RESOURCE_WELL_KNOWN = "/.well-known/oauth-protected-resource"
AS_METADATA_WELL_KNOWN = "/.well-known/oauth-authorization-server"

CODE_TTL_MS = 5 * 60_000
ACCESS_TTL_MS = 60 * 60_000
REFRESH_TTL_MS = 30 * 24 * 3_600_000
# A registered client nobody has used in this long, holding no live
# session, is swept.
CLIENT_IDLE_TTL_MS = 90 * 24 * 3_600_000
CLIENTS_MAX = 200
TOKENS_PER_CLIENT_MAX = 40             # LIVE access + refresh rows, oldest go first
# Consumed refresh rows (reuse-detection tombstones) have their own bound
# and keep their own expiry: they are evidence of a replay, and evidence
# that leaves with the next login is no evidence (round-two review, SEC-F3).
# Reaching the bound is itself the anomaly (round-three review SEC-G1: an
# attacker rotating past the cap evicted the victim's tombstone): a client
# rotating hourly for a refresh lifetime writes about 700 rows, so a
# client that reaches 5000 is a rotation storm and loses every token.
TOMBSTONES_PER_CLIENT_MAX = 5000
CLIENT_NAME_MAX = 80
REDIRECT_URIS_MAX = 8

ACCESS_PREFIX = "zwa_"
REFRESH_PREFIX = "zwr_"
# Authorization codes (opaque to clients; no client keeps one).
CODE_PREFIX = "zwx_"
# Connect codes (2.1 review, SEC-1): minted by the owner IN THE APP
# (`POST /api/oauth/connect-code`), typed on the consent page instead of
# the API token. One use, ten minutes, and the only credential the page
# accepts for a client the owner has not approved yet — typing one both
# approves the client and consents.
CONNECT_PREFIX = "zwc_"
CONNECT_TTL_MS = 10 * 60_000
# The consent page for an approved client: API token, guest token, or a
# connect code, whichever the owner has to hand.

# ───────────────────────── rate limiting ─────────────────────────

REGISTER_PER_IP = 10
AUTHORIZE_PER_IP = 10
# The consent page GET renders nothing secret, but it is the one anonymous
# OAuth endpoint that was unlimited (2.1 review, SEC-2); a wider window
# than the POST so a client's retries do not lock a real person out.
AUTHORIZE_GET_PER_IP = 30
TOKEN_PER_IP = 60
RATE_WINDOW_MS = 60 * 1000
_RATE_MAP_MAX = 4096
_hits: dict[str, list[float]] = {}
_hits_lock = Lock()


def reset_state() -> None:
    """Tests: forget the limiter."""
    _LAST_USED_STAMP.clear()
    with _hits_lock:
        _hits.clear()


def _behind_fly_edge() -> bool:
    return bool(os.environ.get("FLY_APP_NAME") or os.environ.get("RELAY_BEHIND_FLY"))


def _client_ip(request: Request) -> str:
    """Fly-Client-IP only when Fly's edge is actually in front (it is
    client-controlled anywhere else); never X-Forwarded-For."""
    fly = request.headers.get("fly-client-ip")
    if fly and _behind_fly_edge():
        return fly.strip()[:64]
    return (request.client.host if request.client else "?")[:64]


def _rate_ok(bucket: str, ip: str, limit: int, now_ms: float | None = None) -> bool:
    """Sliding window per (bucket, caller); sweeps stale keys and hard-caps
    the map so a spray of source addresses cannot make the limiter itself
    the leak."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    key = f"{bucket}:{ip}"
    cutoff = now_ms - RATE_WINDOW_MS
    with _hits_lock:
        if len(_hits) > 1024:
            for k in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
                del _hits[k]
            if len(_hits) > _RATE_MAP_MAX:
                for k, _ in sorted(_hits.items(),
                                   key=lambda kv: kv[1][-1] if kv[1] else 0
                                   )[:len(_hits) - _RATE_MAP_MAX]:
                    del _hits[k]
        hits = [t for t in _hits.get(key, []) if t >= cutoff]
        if len(hits) >= limit:
            _hits[key] = hits
            return False
        hits.append(now_ms)
        _hits[key] = hits
        return True


def _too_many() -> HTTPException:
    return HTTPException(status_code=429,
                         detail="too many requests; try again in a minute",
                         headers={"Retry-After": "60"})


# ───────────────────────── identity of this server ─────────────────────────

# A Host value that may appear in a header we emit: hostnames, IPv4, a
# bracketed IPv6 literal, a port. Anything else is refused before it can
# reach the WWW-Authenticate header or a metadata document (2.1 review,
# SEC-5: a quote in Host injected into the quoted header parameter).
_HOST_OK = re.compile(r"^[A-Za-z0-9.\-\[\]:]{1,253}$")


def _fly_host() -> str | None:
    """`<app>.fly.dev` when this process is provably on Fly: FLY_APP_NAME
    corroborated by FLY_MACHINE_ID, the same pair self_update and
    machine_advice require (round-three review SEC-G2; the app name alone
    is a string anyone can put in an env file)."""
    app_name = (os.environ.get("FLY_APP_NAME") or "").strip().lower()
    machine = (os.environ.get("FLY_MACHINE_ID") or "").strip()
    if app_name and machine and re.fullmatch(r"[a-z0-9-]{1,63}", app_name):
        return f"{app_name}.fly.dev"
    return None


def _allowed_hosts() -> list[str]:
    raw = (os.environ.get("ALLOWED_HOSTS") or "*").strip()
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _public_base_url_origin() -> str | None:
    from . import config
    raw = (config.settings.public_base_url or "").strip()
    if not raw:
        return None
    try:
        p = urlsplit(raw)
    except ValueError:
        log.warning("PUBLIC_BASE_URL %r is not an http(s) origin; ignoring it", raw)
        return None
    if p.scheme not in ("http", "https") or not p.netloc or not _HOST_OK.match(p.netloc):
        log.warning("PUBLIC_BASE_URL %r is not an http(s) origin; ignoring it", raw)
        return None
    return f"{p.scheme}://{p.netloc.lower()}"


def configured_origin(request: Request | None = None) -> str | None:
    """The canonical identity of this server for the issuer, the resource
    URI and the challenge, so a client-controlled Host header cannot
    become any of them (SEC-5). In order:

    1. PUBLIC_BASE_URL, normalised to an origin (the operator's word).
    2. The request's own Host when it is a host this deployment answers
       to by name: the app's fly.dev hostname, or a host in ALLOWED_HOSTS
       when that list is not `*`. A custom domain in the allow-list keeps
       working with PUBLIC_BASE_URL unset (round-three review SEC-G2: the
       fly.dev fallback alone advertised the wrong identity there).
    3. The app's fly.dev hostname, when the process is provably on Fly,
       for a request that named anything else.
    4. None: the caller reflects the Host (the pre-2.1 posture), which
       only happens off Fly with no allow-list and no PUBLIC_BASE_URL.

    Changing the origin changes the audience every token was issued for:
    assistants reconnect (documented in the changelog)."""
    fixed = _public_base_url_origin()
    if fixed:
        return fixed
    fly_host = _fly_host()
    if request is not None:
        host = (request.headers.get("host") or request.url.netloc or "").strip().lower()
        if host and _HOST_OK.match(host):
            allowed = _allowed_hosts()
            if host == fly_host:
                return f"https://{host}"
            if allowed != ["*"] and host in allowed:
                # A self-hoster on plain HTTP (local Docker) lists that
                # host too; the scheme is the request's, not assumed
                # (CodeRabbit, PR #36).
                proto = (request.headers.get("x-forwarded-proto")
                         or request.url.scheme or "https").split(",")[0].strip().lower()
                return f"{'http' if proto == 'http' else 'https'}://{host}"
    if fly_host:
        return f"https://{fly_host}"
    return None


def describe_origin() -> tuple[str | None, str]:
    """(origin, why) for the boot log and the status page: what this
    server will call itself for OAuth when no request is in hand."""
    fixed = _public_base_url_origin()
    if fixed:
        return fixed, "PUBLIC_BASE_URL"
    fly_host = _fly_host()
    allowed = _allowed_hosts()
    if fly_host and allowed != ["*"]:
        return f"https://{fly_host}", "Fly hostname; ALLOWED_HOSTS entries also answer by name"
    if fly_host:
        return f"https://{fly_host}", "Fly hostname (set PUBLIC_BASE_URL for a custom domain)"
    if allowed != ["*"]:
        return None, "the request's Host, when it is in ALLOWED_HOSTS"
    return None, "the request's Host (set PUBLIC_BASE_URL)"


def issuer(request: Request) -> str:
    """`https://host` as the outside world reaches us. PUBLIC_BASE_URL when
    the operator set it (the right answer behind any proxy); otherwise
    Fly's X-Forwarded-Proto plus the request's Host, which is refused when
    it is not a plain host[:port] (a 400, never an injected header). No
    path: the issuer is the origin, so the RFC 8414 document lives at the
    bare well-known path."""
    fixed = configured_origin(request)
    if fixed:
        return fixed
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https")
    proto = proto.split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "https"
    host = (request.headers.get("host") or request.url.netloc or "").strip().lower()
    if not _HOST_OK.match(host):
        raise HTTPException(status_code=400, detail="unacceptable Host header")
    return f"{proto}://{host}"


def resource_uri(request: Request) -> str:
    """The canonical resource identifier of the MCP server (RFC 8707 §2,
    MCP spec): origin + /mcp, no trailing slash, no fragment."""
    return issuer(request) + RESOURCE_PATH


def resource_metadata_url(request: Request) -> str:
    return issuer(request) + PROTECTED_RESOURCE_WELL_KNOWN + RESOURCE_PATH


def www_authenticate(request: Request) -> str:
    """The 401 challenge the MCP spec requires (RFC 9728 §5.1). Built
    while a 401 is being raised, so it must not raise itself: a Host
    that is not a host (issuer() refuses it with a 400) drops the
    metadata parameter rather than turning the 401 into a 400 the
    client cannot act on (round-two review, SEC-F5)."""
    try:
        url = resource_metadata_url(request)
    except HTTPException:
        fixed = configured_origin()
        if not fixed:
            return 'Bearer realm="zasder-weather"'
        url = fixed + PROTECTED_RESOURCE_WELL_KNOWN + RESOURCE_PATH
    return f'Bearer realm="zasder-weather", resource_metadata="{url}"'


def _canon(uri: str) -> str:
    """Lowercase scheme and host, drop a trailing slash, keep the path:
    the spec asks servers to accept uppercase scheme/host for robustness."""
    try:
        p = urlsplit(uri.strip())
    except ValueError:
        return ""
    if p.fragment:
        return ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def _resource_matches(request: Request, named: str | None) -> bool:
    """RFC 8707: a resource parameter, when sent, must name THIS server.
    Missing is tolerated (older clients); wrong is refused. (A URI, not a
    credential: plain set membership is the right comparison.) With
    PUBLIC_BASE_URL set this compares against a configured value; without
    it, against the request's own host, which is only as strong as the
    Host sanity check in `issuer`."""
    if not named:
        return True
    return _canon(named) in {_canon(resource_uri(request)), _canon(issuer(request))}


# ───────────────────────── metadata documents ─────────────────────────

def _protected_resource_doc(request: Request) -> dict[str, Any]:
    return {
        "resource": resource_uri(request),
        "authorization_servers": [issuer(request)],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Zasder Weather",
    }


# Literal paths in the decorators (not the constants above): the security
# invariants inventory routes by reading the decorator text.
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata_for_mcp(request: Request) -> JSONResponse:
    """RFC 9728 §3, path-suffixed for a resource at /mcp. Public by design:
    the document says where to log in, and nothing else."""
    return JSONResponse(_protected_resource_doc(request))


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request) -> JSONResponse:
    """The bare well-known location, for clients that try only that."""
    return JSONResponse(_protected_resource_doc(request))


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414. Public by design."""
    iss = issuer(request)
    return JSONResponse({
        "issuer": iss,
        "authorization_endpoint": iss + "/oauth/authorize",
        "token_endpoint": iss + "/oauth/token",
        "registration_endpoint": iss + "/oauth/register",
        "revocation_endpoint": iss + "/oauth/revoke",
        "scopes_supported": [SCOPE],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
    })


# ───────────────────────── storage ─────────────────────────

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _redirect_uri_ok(uri: str) -> bool:
    """HTTPS, or plain http to a loopback host (OAuth 2.1 §1.5 / MCP spec:
    'localhost or HTTPS'); absolute; no fragment."""
    try:
        p = urlsplit(uri)
    except ValueError:
        return False
    if p.fragment or not p.netloc:
        return False
    host = (p.hostname or "").lower()
    if p.scheme == "https":
        return True
    return p.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}


# Columns the 2.1 review added after the tables shipped in the dev tree
# (approval, refresh-token reuse detection, the audience a token was
# issued for). db.SCHEMA creates them for a new database; db.init_db
# ALTERs them onto a table an earlier 2.1 build created, from this list,
# at boot, before the first request. Nothing else migrates.
_LATE_COLUMNS = (
    ("oauth_clients", "approved_ms", "INTEGER"),
    ("oauth_tokens", "consumed_ms", "INTEGER"),
    ("oauth_tokens", "resource", "TEXT"),
)


async def _get_client(client_id: str) -> dict[str, Any] | None:
    from . import db
    if not client_id or len(client_id) > 64:
        return None
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT client_id, client_name, redirect_uris, created_ms, last_used_ms, "
            "approved_ms FROM oauth_clients WHERE client_id = ?", (client_id,))).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["redirect_uris"] = json.loads(out["redirect_uris"] or "[]")
    except ValueError:
        out["redirect_uris"] = []
    out["approved"] = out.get("approved_ms") is not None
    return out


async def approve_client(client_id: str) -> bool:
    """The owner's say-so (SEC-1): until a registration is approved, its
    consent page renders no token field and never redirects. True if the
    client existed."""
    from . import db
    async with db.connect() as conn:
        cur = await conn.execute(
            "UPDATE oauth_clients SET approved_ms = COALESCE(approved_ms, ?) "
            "WHERE client_id = ?", (_now_ms(), client_id))
        await conn.commit()
        return bool(cur.rowcount)


async def mint_connect_code(client_id: str | None = None) -> dict[str, Any]:
    """A one-shot, ten-minute code the owner mints in the app and types
    on the consent page in place of the API token. Stored hashed in
    oauth_codes with scope 'connect' (no redirect: it is not an
    authorization code, it is proof of ownership). Minting is
    write-token gated, so whoever holds one was the owner minutes ago.

    `client_id` (2.2, SEC-G5): mint the code FOR the pending client the
    owner is looking at. A bound code approves only that registration, so
    a look-alike registration that appears at the same moment cannot
    spend it. Unbound codes keep working for any client, as before."""
    from . import db
    raw = CONNECT_PREFIX + secrets.token_urlsafe(18)
    now = _now_ms()
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, code_challenge, "
            " scope, resource, role, created_ms, expires_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (_hash(raw), client_id or "", "", "connect", "connect", "", "owner", now,
             now + CONNECT_TTL_MS))
        await conn.commit()
    return {"code": raw, "expires_in": CONNECT_TTL_MS // 1000,
            "client_id": client_id or None}


async def _consume_connect_code(raw: str, client_id: str = "") -> bool:
    """Spend a connect code: True once, then never (single use under the
    writer, like an authorization code). A code minted for a specific
    client is spent only on that client's page; an unbound one anywhere."""
    from . import db
    if not raw.startswith(CONNECT_PREFIX):
        return False
    now = _now_ms()
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute(
            "DELETE FROM oauth_codes WHERE code_hash = ? AND scope = 'connect' "
            "AND expires_ms >= ? AND (client_id = '' OR client_id = ?)",
            (_hash(raw), now, client_id))
        await conn.commit()
        return (cur.rowcount or 0) == 1


async def sweep_expired(now_ms: int | None = None) -> dict[str, int]:
    """Drop expired codes and tokens, then clients idle past
    CLIENT_IDLE_TTL_MS that hold no live token. Runs at boot and hourly."""
    from . import db
    now = _now_ms() if now_ms is None else now_ms
    out = {"codes": 0, "tokens": 0, "clients": 0}
    async with db.connect() as conn:
        cur = await conn.execute("DELETE FROM oauth_codes WHERE expires_ms < ?", (now,))
        out["codes"] = cur.rowcount or 0
        cur = await conn.execute("DELETE FROM oauth_tokens WHERE expires_ms < ?", (now,))
        out["tokens"] = cur.rowcount or 0
        cur = await conn.execute(
            "SELECT client_id FROM oauth_clients "
            "WHERE COALESCE(last_used_ms, created_ms) < ? "
            "AND client_id NOT IN (SELECT DISTINCT client_id FROM oauth_tokens "
            "                      WHERE consumed_ms IS NULL)",
            (now - CLIENT_IDLE_TTL_MS,))
        idle = [r[0] for r in await cur.fetchall()]
        for cid in idle:
            await conn.execute("DELETE FROM oauth_codes WHERE client_id = ?", (cid,))
            await conn.execute("DELETE FROM oauth_clients WHERE client_id = ?", (cid,))
        out["clients"] = len(idle)
        await conn.commit()
    return out


async def revoke_client(client_id: str) -> bool:
    """Everything the client holds, then the client. True if it existed."""
    from . import db
    async with db.connect() as conn:
        cur = await conn.execute("DELETE FROM oauth_clients WHERE client_id = ?", (client_id,))
        existed = bool(cur.rowcount)
        await conn.execute("DELETE FROM oauth_codes WHERE client_id = ?", (client_id,))
        await conn.execute("DELETE FROM oauth_tokens WHERE client_id = ?", (client_id,))
        await conn.commit()
    return existed


async def list_clients() -> list[dict[str, Any]]:
    """For the owner's connected-apps screen: never the tokens, only what
    each client is, what it holds, and when it was last seen."""
    from . import db
    now = _now_ms()
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT c.client_id, c.client_name, c.created_ms, c.last_used_ms, "
            "  c.approved_ms, c.redirect_uris, "
            "  (SELECT COUNT(*) FROM oauth_tokens t WHERE t.client_id = c.client_id "
            "   AND t.kind = 'refresh' AND t.expires_ms >= ? AND t.consumed_ms IS NULL) "
            "   AS sessions, "
            "  (SELECT MAX(role) FROM oauth_tokens t WHERE t.client_id = c.client_id) AS role "
            "FROM oauth_clients c ORDER BY COALESCE(c.last_used_ms, c.created_ms) DESC",
            (now,))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["approved"] = d.get("approved_ms") is not None
        # The hosts the app returns to, for the owner's screen: the one
        # thing that tells "Claude" registered by Anthropic from "Claude"
        # registered by anyone. Never the full URIs (they carry paths the
        # owner has no use for).
        try:
            uris = json.loads(d.pop("redirect_uris", None) or "[]")
        except (TypeError, ValueError):
            uris = []
        hosts: list[str] = []
        for u in uris:
            h = urlparse(str(u)).hostname
            if h and h not in hosts:
                hosts.append(h)
        d["redirect_hosts"] = hosts
        out.append(d)
    return out


# token hash -> ms of the last last-used stamp written; process-local,
# reset with the module's other state.
_LAST_USED_STAMP: dict[str, int] = {}
LAST_USED_STAMP_EVERY_MS = 60_000
# Bounded: entries older than the interval are useless (the next call
# stamps anyway) and every distinct token would otherwise stay for the
# process lifetime (CodeRabbit, PR #36). The hard cap is a backstop for a
# burst of fresh tokens inside one interval.
LAST_USED_STAMP_MAX = 1000


def _prune_last_used_stamps(now: int) -> None:
    if len(_LAST_USED_STAMP) < LAST_USED_STAMP_MAX:
        return
    for k, at in list(_LAST_USED_STAMP.items()):
        if now - at >= LAST_USED_STAMP_EVERY_MS:
            del _LAST_USED_STAMP[k]
    if len(_LAST_USED_STAMP) >= LAST_USED_STAMP_MAX:
        _LAST_USED_STAMP.clear()


async def authenticate_access_token(bearer: str | None,
                                    request: Request | None = None) -> str | None:
    """The role ('owner' | 'guest') an OAuth access token carries, or None
    when it is not one of ours, unknown, expired, or issued for another
    audience (the `resource` persisted at issue must be THIS server's /mcp
    as reached now; SEC-5). The prefix check comes first so the API-token
    path pays nothing for this."""
    if not bearer or not bearer.startswith(ACCESS_PREFIX):
        return None
    from . import db
    now = _now_ms()
    h = _hash(bearer)
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT client_id, role, expires_ms, resource FROM oauth_tokens "
            "WHERE token_hash = ? AND kind = 'access'", (h,))).fetchone()
        if not row or int(row["expires_ms"]) < now:
            return None
    if request is not None and row["resource"]:
        try:
            ours = resource_uri(request)
        except HTTPException:
            # A Host that is not a host: the token cannot be for it. The
            # caller answers 401 with the challenge, not a 400 with none
            # (round-three review SEC-G3).
            return None
        if _canon(str(row["resource"])) != _canon(ours):
            return None
    # The last-used stamp is a display value (the owner's connected-apps
    # list), not a security one: refresh it at most once a minute per
    # token so a chatty assistant does not take the writer on every call
    # (CodeRabbit, PR #36; ingest already contends for that lock).
    stamped = _LAST_USED_STAMP.get(h, 0)
    if now - stamped >= LAST_USED_STAMP_EVERY_MS:
        _prune_last_used_stamps(now)
        _LAST_USED_STAMP[h] = now
        async with db.connect() as conn:
            await conn.execute("UPDATE oauth_tokens SET last_used_ms = ? WHERE token_hash = ?",
                               (now, h))
            await conn.execute("UPDATE oauth_clients SET last_used_ms = ? WHERE client_id = ?",
                               (now, row["client_id"]))
            await conn.commit()
    return str(row["role"])


# ───────────────────────── dynamic client registration ─────────────────────────

def _reg_error(code: str, desc: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code, "error_description": desc}, status_code=status)


@router.post("/oauth/register", status_code=201)
async def register_client(request: Request) -> JSONResponse:
    """RFC 7591. Open by design: a registration grants nothing by itself
    (every token still needs the consent page and a real server token);
    its control is the per-IP rate limit (_rate_ok) and the caps on what
    is stored."""
    if not _rate_ok("register", _client_ip(request), REGISTER_PER_IP):
        raise _too_many()
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return _reg_error("invalid_client_metadata", "body must be JSON")
    if not isinstance(body, dict):
        return _reg_error("invalid_client_metadata", "body must be a JSON object")
    uris = body.get("redirect_uris")
    if (not isinstance(uris, list) or not uris or len(uris) > REDIRECT_URIS_MAX
            or not all(isinstance(u, str) and len(u) <= 2048 for u in uris)):
        return _reg_error("invalid_redirect_uri",
                          f"redirect_uris must be 1..{REDIRECT_URIS_MAX} absolute URIs")
    if any(not _redirect_uri_ok(u) for u in uris):
        return _reg_error("invalid_redirect_uri",
                          "redirect URIs must be https, or http to localhost")
    if body.get("token_endpoint_auth_method", "none") != "none":
        return _reg_error("invalid_client_metadata",
                          "only public clients (token_endpoint_auth_method 'none') are supported")
    grants = body.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        return _reg_error("invalid_client_metadata",
                          "grant_types may only be authorization_code and refresh_token")
    rtypes = body.get("response_types") or ["code"]
    if not isinstance(rtypes, list) or set(rtypes) != {"code"}:
        return _reg_error("invalid_client_metadata", 'response_types must be ["code"]')
    name = body.get("client_name")
    name = " ".join(str(name).split())[:CLIENT_NAME_MAX] if isinstance(name, str) else ""
    name = name or "an MCP client"

    from . import db
    client_id = "zwo_" + secrets.token_hex(12)
    now = _now_ms()
    async with db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM oauth_clients")
        if (await cur.fetchone())[0] >= CLIENTS_MAX:
            # Evict the longest-idle client that holds no live session.
            await conn.execute(
                "DELETE FROM oauth_clients WHERE client_id = ("
                " SELECT client_id FROM oauth_clients WHERE client_id NOT IN "
                "  (SELECT DISTINCT client_id FROM oauth_tokens "
                "   WHERE expires_ms >= ? AND consumed_ms IS NULL)"
                " ORDER BY COALESCE(last_used_ms, created_ms) ASC LIMIT 1)", (now,))
            cur = await conn.execute("SELECT COUNT(*) FROM oauth_clients")
            if (await cur.fetchone())[0] >= CLIENTS_MAX:
                await conn.commit()
                return _reg_error("invalid_client_metadata",
                                  "this server holds as many connected clients as it "
                                  "will; revoke one from the app first", status=503)
        await conn.execute(
            "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_ms) "
            "VALUES (?, ?, ?, ?)", (client_id, name, json.dumps(uris), now))
        await conn.commit()
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": now // 1000,
        "client_name": name,
        "redirect_uris": uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": SCOPE,
    }, status_code=201)


# ───────────────────────── authorization endpoint ─────────────────────────

_PAGE_CSS = """
:root{color-scheme:dark}body{margin:0;background:#0d0f12;color:#fff;font:15px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:420px;margin:12vh auto;padding:0 20px}h1{font-size:20px;margin:0 0 6px}
p{color:rgba(255,255,255,.7);margin:0 0 14px}.who{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:12px 14px;margin:0 0 18px}
label{display:block;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.55);margin:0 0 6px}
input[type=password]{width:100%;box-sizing:border-box;font:15px ui-monospace,SF Mono,monospace;padding:11px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.04);color:#fff}
button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:10px;background:#f0a020;color:#111;font-weight:700;font-size:15px}
.err{color:#ff8a65;margin:10px 0 0}.fine{font-size:12px;color:rgba(255,255,255,.45);margin-top:16px}
"""

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _redirect_host(uri: str) -> str:
    try:
        return urlsplit(uri).netloc or uri
    except ValueError:
        return uri


def _consent_page(client: dict[str, Any], fields: dict[str, str], error: str | None,
                  status: int = 200) -> HTMLResponse:
    """The consent page. Two shapes (SEC-1): a client the owner has
    APPROVED gets the token field (API token, guest link token, or a
    connect code); one that has not gets only the connect-code field and a
    sentence saying it is waiting for the owner. Either way the page names
    the app AND the address it will send the code to, so a registration
    that borrowed a familiar name cannot hide where the grant goes."""
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in fields.items())
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    name = html.escape(client.get("client_name") or "an MCP client")
    where = html.escape(_redirect_host(fields.get("redirect_uri", "")))
    who = (f'<div class="who"><strong>{name}</strong><br>'
           f'<span style="color:rgba(255,255,255,.55)">wants to read this server and '
           f'will be sent back to <code>{where}</code></span></div>')
    # The pending shape's input is a different field with the one-time-
    # code hint, so a password manager that saved the API token on the
    # approved page never offers it on the page that says not to type it.
    if client.get("approved"):
        field = ('<input id="token" name="token" type="password" autocapitalize="off" '
                 'autocorrect="off" spellcheck="false" required>')
        ask = ('<label for="token">Connect code from the app, this server\'s API token, '
               'or a guest link token</label>')
        note = ('Your token is sent only to this server, over this page, and is not '
                'stored. Revoke the app any time from Settings in Zasder Weather.')
    else:
        ask = ('<p>This app is waiting for the server owner\'s approval. In Zasder '
               'Weather open Settings, Server &amp; Backups, Connected apps, and mint a '
               'connect code; typing it here approves the app and connects it. '
               'Do not type your API token here.</p>'
               '<label for="connect_code">Connect code from the app</label>')
        field = ('<input id="connect_code" name="connect_code" type="text" '
                 'autocomplete="one-time-code" autocapitalize="off" autocorrect="off" '
                 'spellcheck="false" required>')
        note = ('A connect code works once and expires in ten minutes. This page '
                'does not accept the API token for an app the owner has not approved.')
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Connect to Zasder Weather</title><style>{_PAGE_CSS}</style></head><body><main>
<h1>Connect to Zasder Weather</h1>
<p>Read-only access to this server's weather record for the app below. It will be able to read stations, history, records, reports and stories, and nothing else.</p>
{who}
<form method="post" action="/oauth/authorize" autocomplete="off">{hidden}
{ask}
{field}
{err}
<button type="submit">{"Allow read access" if client.get("approved") else "Approve and connect"}</button>
<p class="fine">{note}</p>
</form></main></body></html>"""
    return HTMLResponse(body, status_code=status, headers=_NO_STORE)


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>Zasder Weather</title><style>{_PAGE_CSS}</style></head><body><main>
<h1>That request cannot be completed</h1><p>{html.escape(message)}</p></main></body></html>"""
    return HTMLResponse(body, status_code=status, headers=_NO_STORE)


def _redirect_error(redirect_uri: str, error: str, desc: str, state: str | None) -> Response:
    q = {"error": error, "error_description": desc}
    if state:
        q["state"] = state
    sep = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(q), status_code=302,
                            headers=_NO_STORE)


_B64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_VERIFIER_CHARS = _B64URL | frozenset(".~")


def _challenge_ok(challenge: str) -> bool:
    """RFC 7636 §4.2: 43..128 base64url characters. One definition, used by
    the pre-approval gate AND the redirecting check, so an unapproved
    client cannot reach the redirect through a malformed challenge."""
    return 43 <= len(challenge) <= 128 and all(c in _B64URL for c in challenge)


async def _validate_authorize(request: Request, p: dict[str, str]
                              ) -> tuple[dict[str, Any], dict[str, str]] | Response:
    """Shared by GET and POST: the client and redirect_uri gates that must
    never redirect on failure (the open-redirect rule), then the parameters
    the spec lets us report back to the client's own redirect_uri."""
    client = await _get_client(p.get("client_id") or "")
    if client is None:
        return _error_page("Unknown client. The app that sent you here is not "
                           "registered with this server.")
    redirect_uri = p.get("redirect_uri") or ""
    if redirect_uri not in client["redirect_uris"]:
        return _error_page("The return address the app supplied is not one it registered.")
    state = (p.get("state") or "")[:512] or None
    if not client.get("approved"):
        # SEC-1: a client nobody has approved gets an error PAGE for a bad
        # request, never a redirect to its own address — anonymous
        # registration must not turn this origin into an open redirect.
        # (A well-formed request renders the pending consent page.)
        problems = (p.get("response_type") != "code"
                    or p.get("code_challenge_method") != "S256"
                    or not _challenge_ok(p.get("code_challenge") or "")
                    or (p.get("scope") and set(p["scope"].split()) - {SCOPE})
                    or not _resource_matches(request, p.get("resource")))
        if problems:
            return _error_page("The app sent a request this server cannot honour, and "
                               "it has not been approved by the owner, so nothing is "
                               "sent back to it.", status=403)
    if p.get("response_type") != "code":
        return _redirect_error(redirect_uri, "unsupported_response_type",
                               "only response_type=code is supported", state)
    challenge = p.get("code_challenge") or ""
    if p.get("code_challenge_method") != "S256":
        return _redirect_error(redirect_uri, "invalid_request",
                               "PKCE with code_challenge_method=S256 is required", state)
    if not _challenge_ok(challenge):
        return _redirect_error(redirect_uri, "invalid_request",
                               "code_challenge must be base64url, 43..128 characters", state)
    scope = p.get("scope") or SCOPE
    if set(scope.split()) - {SCOPE}:
        return _redirect_error(redirect_uri, "invalid_scope",
                               f"this server offers only {SCOPE}", state)
    if not _resource_matches(request, p.get("resource")):
        return _redirect_error(redirect_uri, "invalid_target",
                               "resource must be this server's /mcp URL", state)
    fields = {"client_id": client["client_id"], "redirect_uri": redirect_uri,
              "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "scope": SCOPE,
              "resource": resource_uri(request)}
    if state:
        fields["state"] = state
    return client, fields


@router.get("/oauth/authorize")
async def authorize_page(request: Request) -> Response:
    """Render the consent page. Public by design: it names the app that is
    asking and takes a token; nothing is granted until the POST. Rate-
    limited like the other anonymous endpoints (SEC-2)."""
    if not _rate_ok("authorize_get", _client_ip(request), AUTHORIZE_GET_PER_IP):
        raise _too_many()
    got = await _validate_authorize(request, dict(request.query_params))
    if isinstance(got, Response):
        return got
    client, fields = got
    return _consent_page(client, fields, None)


@router.post("/oauth/authorize")
async def authorize_submit(
    request: Request,
    token: Annotated[str, Form()] = "",
    connect_code: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    response_type: Annotated[str, Form()] = "",
    code_challenge: Annotated[str, Form()] = "",
    code_challenge_method: Annotated[str, Form()] = "",
    scope: Annotated[str, Form()] = "",
    resource: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
) -> Response:
    """The consent. The typed token is checked against this server's own
    tokens in constant time (tokens_match), never logged, never echoed; a
    wrong one re-renders the page with a plain error and issues no code.
    Rate-limited per IP so the page cannot be used to guess."""
    if not _rate_ok("authorize", _client_ip(request), AUTHORIZE_PER_IP):
        raise _too_many()
    p = {"client_id": client_id, "redirect_uri": redirect_uri,
         "response_type": response_type, "code_challenge": code_challenge,
         "code_challenge_method": code_challenge_method, "scope": scope,
         "resource": resource, "state": state}
    got = await _validate_authorize(request, p)
    if isinstance(got, Response):
        return got
    client, fields = got
    typed = (token or connect_code or "").strip()
    if await _consume_connect_code(typed, client["client_id"]):
        # The owner minted this minutes ago in the app: it proves ownership
        # and, for a client not yet approved, IS the approval. Logged
        # (SEC-G5): an approval is the one event in this flow worth a
        # line, and the name is the registration's own claim.
        role = "owner"
        if not client.get("approved"):
            await approve_client(client["client_id"])
            log.info("oauth: client %s (%r) approved with a connect code",
                     client["client_id"], str(client.get("client_name") or "")[:60])
    elif not client.get("approved"):
        # SEC-1: an unapproved client's page never takes the API token, so
        # a registration with a borrowed name cannot phish it.
        return _consent_page(client, fields,
                             "That is not a connect code. This app needs the "
                             "owner's approval first.", status=401)
    else:
        role = _role_for(typed)
    if role is None:
        return _consent_page(client, fields,
                             "That token is not one this server knows.", status=401)
    from . import db
    code = CODE_PREFIX + secrets.token_urlsafe(32)
    now = _now_ms()
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, code_challenge, "
            " scope, resource, role, created_ms, expires_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (_hash(code), client["client_id"], fields["redirect_uri"],
             fields["code_challenge"], SCOPE, fields["resource"], role, now,
             now + CODE_TTL_MS))
        await conn.commit()
    q = {"code": code}
    if fields.get("state"):
        q["state"] = fields["state"]
    sep = "&" if urlsplit(fields["redirect_uri"]).query else "?"
    return RedirectResponse(fields["redirect_uri"] + sep + urlencode(q),
                            status_code=302, headers=_NO_STORE)


def _role_for(token: str) -> str | None:
    """'owner' for the server's API token, 'guest' for any read-only token
    it honours (env guests, app-minted share links, the reviewer token),
    None otherwise. Settings are read at call time."""
    from . import config, db
    from .config import tokens_match
    settings = config.settings
    t = (token or "").strip()
    if not t:
        return None
    if tokens_match(t, settings.write_tokens):
        return "owner"
    if tokens_match(t, settings.valid_api_tokens | db.guest_token_cache()):
        return "guest"
    return None


# ───────────────────────── token endpoint ─────────────────────────

def _token_error(code: str, desc: str, status: int = 400) -> JSONResponse:
    headers = dict(_NO_STORE)
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="zasder-weather"'
    return JSONResponse({"error": code, "error_description": desc},
                        status_code=status, headers=headers)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _issue(conn, client_id: str, role: str, resource: str,
                 family: str | None = None) -> dict[str, Any]:
    """A fresh access + refresh pair, capped per client. A login starts a
    family; a rotation keeps it (SEC-3), so a replay of a rotated refresh
    token can be recognised as that family's and revoke all of it. The
    `resource` the grant named is stored with the tokens and checked at
    use (SEC-5)."""
    now = _now_ms()
    family = family or secrets.token_hex(8)
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    for raw, kind, ttl in ((access, "access", ACCESS_TTL_MS),
                           (refresh, "refresh", REFRESH_TTL_MS)):
        await conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, scope, role, family, "
            " created_ms, expires_ms, resource) VALUES (?,?,?,?,?,?,?,?,?)",
            (_hash(raw), kind, client_id, SCOPE, role, family, now, now + ttl, resource))
    # Per-client cap on LIVE rows: the oldest go, so a client that keeps
    # logging in cannot grow the table without bound. Consumed refresh
    # rows are breach evidence with their own bound below, never counted
    # here, so a burst of rotations cannot walk the evidence out of the
    # table (round-two review, SEC-F3). rowid breaks a same-millisecond
    # tie so the pair just minted is never the pair evicted.
    await conn.execute(
        "DELETE FROM oauth_tokens WHERE client_id = ? AND consumed_ms IS NULL "
        "AND token_hash NOT IN ("
        " SELECT token_hash FROM oauth_tokens WHERE client_id = ? AND consumed_ms IS NULL "
        " ORDER BY created_ms DESC, rowid DESC LIMIT ?)",
        (client_id, client_id, TOKENS_PER_CLIENT_MAX))
    await conn.execute(
        "DELETE FROM oauth_tokens WHERE client_id = ? AND consumed_ms IS NOT NULL "
        "AND token_hash NOT IN ("
        " SELECT token_hash FROM oauth_tokens WHERE client_id = ? AND consumed_ms IS NOT NULL "
        " ORDER BY consumed_ms DESC, rowid DESC LIMIT ?)",
        (client_id, client_id, TOMBSTONES_PER_CLIENT_MAX))
    await conn.execute("UPDATE oauth_clients SET last_used_ms = ? WHERE client_id = ?",
                       (now, client_id))
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": ACCESS_TTL_MS // 1000, "refresh_token": refresh,
            "scope": SCOPE}


@router.post("/oauth/token")
async def token_endpoint(
    request: Request,
    grant_type: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    code_verifier: Annotated[str, Form()] = "",
    refresh_token: Annotated[str, Form()] = "",
    resource: Annotated[str, Form()] = "",
) -> Response:
    """authorization_code (PKCE S256, single use, redirect_uri bound) and
    refresh_token (rotating). Public clients only: the client_id is the
    only client credential, and an unknown one is 401 invalid_client (RFC
    6749 §5.2). Tokens are opaque random strings, hashed at rest."""
    if not _rate_ok("token", _client_ip(request), TOKEN_PER_IP):
        raise _too_many()
    from . import db
    client = await _get_client(client_id)
    if client is None:
        return _token_error("invalid_client", "unknown client_id", status=401)
    try:
        if not _resource_matches(request, resource):
            return _token_error("invalid_target", "resource must be this server's /mcp URL")
    except HTTPException:
        # An OAuth error object, not FastAPI's 400 (round-three review SEC-G3).
        return _token_error("invalid_request", "unacceptable Host header")
    now = _now_ms()
    if grant_type == "authorization_code":
        if not code or not code_verifier:
            return _token_error("invalid_request", "code and code_verifier are required")
        if not (43 <= len(code_verifier) <= 128) or any(c not in _VERIFIER_CHARS
                                                       for c in code_verifier):
            return _token_error("invalid_request",
                                "code_verifier must be 43..128 unreserved characters")
        h = _hash(code)
        async with db.connect() as conn:
            # BEGIN IMMEDIATE takes the writer before the read, so two
            # exchanges racing on one code serialize here; the loser's
            # DELETE then hits nothing and the rowcount below refuses it
            # (CodeRabbit, PR #36: a plain SELECT-then-DELETE let both
            # requests read the row and both issue tokens).
            await conn.execute("BEGIN IMMEDIATE")
            row = await (await conn.execute(
                "SELECT client_id, redirect_uri, code_challenge, role, expires_ms, "
                "resource FROM oauth_codes WHERE code_hash = ? AND scope = ?",
                (h, SCOPE))).fetchone()
            # Single use whatever happens next: a code that was presented is
            # spent, so a stolen copy cannot be tried after ours.
            spent = await conn.execute(
                "DELETE FROM oauth_codes WHERE code_hash = ? AND scope = ?", (h, SCOPE))
            problem = None
            if not row or int(row["expires_ms"]) < now or (spent.rowcount or 0) != 1:
                problem = "authorization code is unknown or expired"
            elif row["client_id"] != client_id:
                problem = "authorization code was issued to another client"
            elif row["redirect_uri"] != redirect_uri:
                problem = "redirect_uri does not match the authorization request"
            elif not secrets.compare_digest(_s256(code_verifier), str(row["code_challenge"])):
                problem = "code_verifier does not match"
            if problem:
                await conn.commit()
                return _token_error("invalid_grant", problem)
            out = await _issue(conn, client_id, str(row["role"]), str(row["resource"]))
            await conn.commit()
        return JSONResponse(out, headers=_NO_STORE)
    if grant_type == "refresh_token":
        if not refresh_token.startswith(REFRESH_PREFIX):
            return _token_error("invalid_grant", "refresh token is unknown")
        rh = _hash(refresh_token)
        async with db.connect() as conn:
            # Under the writer from the first read (SEC-4): two rotations
            # racing on one refresh token serialize here, and the UPDATE's
            # rowcount decides which one actually consumed it.
            await conn.execute("BEGIN IMMEDIATE")
            row = await (await conn.execute(
                "SELECT client_id, role, family, expires_ms, consumed_ms, resource "
                "FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                (rh,))).fetchone()
            if not row or int(row["expires_ms"]) < now or row["client_id"] != client_id:
                await conn.commit()
                return _token_error("invalid_grant",
                                    "refresh token is unknown, expired, or not this client's")
            if row["consumed_ms"] is not None:
                # Reuse of a rotated token (SEC-3, OAuth 2.1 §4.3.1): either
                # a copy was stolen and one party is now replaying, or the
                # client retried across a rotation. Both parties lose the
                # whole family; the client logs in again.
                await conn.execute("DELETE FROM oauth_tokens WHERE family = ?",
                                   (row["family"],))
                await conn.commit()
                log.warning("oauth: rotated refresh token reused by client %s; "
                            "family revoked", client_id)
                return _token_error("invalid_grant",
                                    "refresh token was already used; the session "
                                    "has been revoked, log in again")
            # The consumed row stays until the token would have expired
            # anyway (a replay after that is a plain "expired"), so a
            # client that was offline for a day still trips reuse
            # detection. It is not a session: the idle sweep, the
            # registration eviction and the live cap all skip consumed
            # rows (round-two review, SEC-F3).
            cur = await conn.execute(
                "UPDATE oauth_tokens SET consumed_ms = ? "
                "WHERE token_hash = ? AND consumed_ms IS NULL",
                (now, rh))
            if (cur.rowcount or 0) != 1:
                await conn.commit()
                return _token_error("invalid_grant", "refresh token was already used")
            # A client at the tombstone cap is rotating faster than any
            # honest client can: the evidence must not be evicted, so the
            # CLIENT is (round-three review SEC-G1). Every token it holds
            # dies; it logs in again through the owner's consent.
            tomb = await (await conn.execute(
                "SELECT COUNT(*) FROM oauth_tokens WHERE client_id = ? "
                "AND consumed_ms IS NOT NULL", (client_id,))).fetchone()
            if int(tomb[0]) >= TOMBSTONES_PER_CLIENT_MAX:
                await conn.execute("DELETE FROM oauth_tokens WHERE client_id = ?",
                                   (client_id,))
                await conn.commit()
                log.warning("oauth: client %s rotated its refresh token %d times; "
                            "every token revoked", client_id, int(tomb[0]))
                return _token_error("invalid_grant",
                                    "too many refresh rotations; every session for "
                                    "this app has been revoked, log in again")
            # Rotation: the family's access tokens die with the presented
            # refresh token; the consumed refresh row stays (until it
            # expires) so a replay is recognisable as reuse.
            await conn.execute(
                "DELETE FROM oauth_tokens WHERE family = ? AND kind = 'access'",
                (row["family"],))
            out = await _issue(conn, client_id, str(row["role"]),
                               str(row["resource"] or resource_uri(request)),
                               family=str(row["family"]))
            await conn.commit()
        return JSONResponse(out, headers=_NO_STORE)
    return _token_error("unsupported_grant_type",
                        "grant_type must be authorization_code or refresh_token")


@router.post("/oauth/revoke")
async def revoke_endpoint(
    request: Request,
    token: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
) -> Response:
    """RFC 7009: a client gives back a token and the whole family goes with
    it. 200 whether or not the token was known, as the RFC says; an unknown
    client is 401 like the token endpoint."""
    if not _rate_ok("token", _client_ip(request), TOKEN_PER_IP):
        raise _too_many()
    from . import db
    if await _get_client(client_id) is None:
        return _token_error("invalid_client", "unknown client_id", status=401)
    if token.startswith((ACCESS_PREFIX, REFRESH_PREFIX)):
        async with db.connect() as conn:
            row = await (await conn.execute(
                "SELECT family, client_id FROM oauth_tokens WHERE token_hash = ?",
                (_hash(token),))).fetchone()
            if row and row["client_id"] == client_id:
                await conn.execute("DELETE FROM oauth_tokens WHERE family = ?",
                                   (row["family"],))
                await conn.commit()
    return Response(status_code=200, headers=_NO_STORE)
