import asyncio
import html as _html
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .limits import BodySizeLimitMiddleware
from .updates import UpdateChecker
from .version import __version__

from . import db
from . import build_guard
from .alerts import AlertMonitor
from .capture import router as capture_router
from .config import settings, tokens_match
from . import source_status
from . import config_backup
from . import public_dashboard as _pd
from .discovery import router as discovery_router
from .ingest import router as ingest_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs every request at INFO with the FULL URL — and the WU import,
# TWC forecast, AWN and WeatherLink clients all carry their API keys as
# query parameters (those APIs accept them nowhere else). Left at INFO,
# every forecast refresh and each of the ~1400 calls of a WU import writes
# the plaintext key into the server logs / Fly log drain. The modules scrub
# their own exception messages; this closes httpx's channel too.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if the stripped public build was deployed onto a host that is
    # supposed to serve the relay. Deliberately BEFORE anything else, and
    # deliberately not marked PRIVATE — this line must survive into the mirror,
    # where it is inert unless REQUIRE_RELAY is set. See app/build_guard.py.
    build_guard.assert_build_variant()
    await db.init_db()
    app.state.started_at = time.time()
    # Declare every source up front, configured or not. "Not set up" and "set
    # up but broken" are the two answers a self-hoster needs to tell apart,
    # and they look identical from outside.
    source_status.reset()
    source_status.declare("custom-ingest", True,
                          note="LilyGO boards, SDR relays and the WeatherLink "
                               "Live poller POST here; health is per-device "
                               "last-seen, see /api/devices")
    # Cloud pollers (AmbientWeather, Davis WeatherLink, Tempest) — owned by
    # the IntegrationManager so credentials configured FROM THE APP
    # (/api/integrations → server_kv, kv-over-env like the WU key) apply at
    # boot and on change without a redeploy. Each provider's source_status
    # is declared inside apply(), configured or not: "not set up" and "set
    # up but broken" are the two answers a self-hoster must be able to tell
    # apart. AcuRite-only deploys configure none of them and rely entirely
    # on /ingest/custom.
    from .integrations import IntegrationManager
    integration_manager = IntegrationManager()
    await integration_manager.start_all()
    app.state.integration_manager = integration_manager

    # Device-staleness email alerts — independent of any poller; watches ALL
    # devices (cloud + SDR) for going quiet. ALWAYS started: it re-reads the
    # effective config each tick and no-ops unless alerts are enabled with a
    # transport + recipients. Gating on env SMTP at boot would miss transport
    # configured later from the app (PUT /api/alerts → DB), so the monitor
    # must already be running to pick that up without a redeploy.
    alert_monitor = AlertMonitor()
    await alert_monitor.start()
    app.state.alert_monitor = alert_monitor
    log.info("staleness alert monitor started (active once alerts are configured)")

    # Self-healing rollups: a repair or column backfill marks the ledgers
    # dirty (records() serves raw — correct but slow — while the flag is
    # set), and this background rebuild clears it without an operator having
    # to know POST /api/insights/rebuild exists (CODE_REVIEW_R5 R5-14).
    # BACKGROUND on purpose: a full rebuild of a 1M-row archive takes
    # minutes, and running it inline here failed a deploy's health checks
    # once already (2026-08-20).
    if settings.insights and await db.get_kv("rollups_dirty"):
        from . import insights as _insights

        async def _heal_rollups() -> None:
            try:
                stats = await _insights.rebuild()   # clears the flag itself
                log.info("background rollup rebuild done: %s", stats)
            except Exception:
                log.exception("background rollup rebuild failed — records "
                              "stay on the raw path until one succeeds")
        app.state.rollup_heal_task = asyncio.create_task(_heal_rollups())

    # Daily "is there a newer release?" check → status-page banner + /api/version.
    update_checker = UpdateChecker(app)
    update_checker.start()
    app.state.update_checker = update_checker

    # Opt-in self-update rides on the checker's result (AUTO_UPDATE=1 +
    # FLY_API_TOKEN — inert without both; see app/self_update.py).
    from .self_update import SelfUpdater
    self_updater = SelfUpdater(app)
    self_updater.start()
    app.state.self_updater = self_updater

    # MQTT publisher (Home Assistant discovery) — only if a broker is configured.
    mqtt_pub = None
    if settings.mqtt_host:
        from .mqtt_publish import MqttPublisher
        mqtt_pub = MqttPublisher()
        await mqtt_pub.start()
        app.state.mqtt_pub = mqtt_pub
        log.info("MQTT publisher started (broker %s:%s)",
                 settings.mqtt_host, settings.mqtt_port)

    try:
        yield
    finally:
        heal = getattr(app.state, "rollup_heal_task", None)
        if heal is not None:
            # Cancellation mid-scan clears the partial tables (the
            # _rebuild_locked safeguard) and LEAVES the dirty flag set, so
            # the next boot retries — never a silently truncated ledger.
            heal.cancel()
        await integration_manager.stop_all()
        await alert_monitor.stop()
        await update_checker.stop()
        await self_updater.stop()
        if mqtt_pub is not None: await mqtt_pub.stop()


# /docs, /redoc, /openapi.json are exposed by default in FastAPI and
# advertise the shapes of every route — including /ingest/* and
# /ingest/capture/* — to anyone who can hit the URL. They also load
# CDN scripts (Swagger UI), which exacerbates the missing CSP. Disable
# in production; set DEBUG=1 (or any truthy value) to re-enable for
# local development.
_DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")
app = FastAPI(
    title="zasder weather",
    lifespan=lifespan,
    docs_url="/docs" if _DEBUG else None,
    redoc_url="/redoc" if _DEBUG else None,
    openapi_url="/openapi.json" if _DEBUG else None,
)

@app.exception_handler(RequestValidationError)
async def _validation_error_no_echo(request: Request,
                                    exc: RequestValidationError) -> JSONResponse:
    """422 bodies minus the "input" echo. Pydantic's default includes the
    rejected value verbatim — for credential-carrying routes (wu-station
    upload_key, the WU api key) the app renders that body in a persistent
    label, redisplaying a just-typed secret the SecureField hid."""
    errors = [{k: v for k, v in e.items() if k != "input"}
              for e in exc.errors()]
    return JSONResponse(status_code=422,
                        content={"detail": jsonable_encoder(errors)})


app.include_router(capture_router)
app.include_router(discovery_router)
app.include_router(ingest_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ───────────────────────── security middleware ─────────────────────────
# Two layers of hardening recommended by an external code review:
#   1. TrustedHostMiddleware — reject requests whose Host header doesn't
#      match an allow-list. Defends against Host-header poisoning if we
#      ever generate absolute URLs from request.url (we don't today; this
#      is belt-and-suspenders). Allow list is configurable via
#      ALLOWED_HOSTS env var (comma-separated). Defaults to "*" (accept
#      anything) so the public template works out-of-box; set this in
#      Fly secrets for production deploys (e.g.
#      ALLOWED_HOSTS="weather.example.com,*.fly.dev").
#   2. Browser security headers — CSP, HSTS, X-Content-Type-Options,
#      X-Frame-Options, Referrer-Policy. Especially important on the
#      public HTML status page; documents loading CDN scripts (Swagger UI
#      in DEBUG mode) need a CSP that allows them.

_allowed_raw = os.environ.get("ALLOWED_HOSTS", "*").strip()
_ALLOWED_HOSTS = [h.strip() for h in _allowed_raw.split(",") if h.strip()] or ["*"]
if _ALLOWED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

# 3. Global body-size cap (added last → outermost middleware → runs FIRST):
#    bounds every request body before FastAPI parses JSON or checks auth, so
#    an anonymous malformed/chunked request can't stream unbounded data into
#    memory. See app/limits.py. Covers the /static mount too.
app.add_middleware(BodySizeLimitMiddleware)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Add a baseline set of browser security headers to every response.
    These mostly matter for HTML responses (the /status page and FastAPI's
    /docs when DEBUG=1) but cost nothing to set on JSON responses too."""
    response = await call_next(request)
    # Conservative CSP — page renders inline styles + same-origin images.
    # When DEBUG=1 and /docs is enabled, Swagger UI also needs cdn.jsdelivr.net
    # for its script and style assets; we allow that selectively.
    if _DEBUG:
        csp = ("default-src 'self'; "
               "img-src 'self' data:; "
               "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
               "script-src 'self' https://cdn.jsdelivr.net; "
               "connect-src 'self'; frame-ancestors 'none'")
    else:
        csp = ("default-src 'self'; "
               "img-src 'self' data:; "
               "style-src 'self' 'unsafe-inline'; "
               "script-src 'self'; "
               "connect-src 'self'; frame-ancestors 'none'")
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("Strict-Transport-Security",
                                 "max-age=63072000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy",
                                 "geolocation=(), microphone=(), camera=()")
    return response


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid token")
    return authorization.removeprefix("Bearer ")


# Throttled 401 logging: one line per source IP per minute, so failed bearer
# auth is at least VISIBLE (probes used to be completely silent) without a
# flood turning the log itself into the problem. Process-global like the other
# caches here; plain dict is fine (mutated only on the event loop thread).
_AUTH_FAIL_LOG_TS: dict[str, float] = {}
_AUTH_FAIL_LOG_INTERVAL_S = 60.0
_AUTH_FAIL_LOG_MAX_IPS = 1024


def _log_auth_failure(request: Request | None) -> None:
    host = (request.client.host if request and request.client else "?")[:64]
    now = time.monotonic()
    last = _AUTH_FAIL_LOG_TS.get(host)
    if last is not None and now - last < _AUTH_FAIL_LOG_INTERVAL_S:
        return
    if len(_AUTH_FAIL_LOG_TS) >= _AUTH_FAIL_LOG_MAX_IPS:
        _AUTH_FAIL_LOG_TS.clear()      # cheap bound; worst case one extra line per IP
    _AUTH_FAIL_LOG_TS[host] = now
    log.warning("rejected bearer auth from %s (throttled: 1 line/min/IP)", host)


def require_token(request: Request,
                  authorization: Annotated[str | None, Header()] = None) -> None:
    """READ-allowing dep: accepts api_token, reviewer_api_token, an env
    guest token, or an app-minted share token (db.guest_token_cache). Use
    on GETs."""
    try:
        ok = tokens_match(_extract_bearer(authorization),
                          settings.valid_api_tokens | db.guest_token_cache())
    except HTTPException:
        _log_auth_failure(request)
        raise
    if not ok:
        _log_auth_failure(request)
        raise HTTPException(status_code=401, detail="invalid token")


def require_write_token(request: Request,
                        authorization: Annotated[str | None, Header()] = None) -> None:
    """MUTATING dep: only api_token. The reviewer/demo token is read-only,
    so it can't alter user state if the reviewer hits a write route. Use on
    every POST/PUT/PATCH/DELETE under /api/*."""
    try:
        ok = tokens_match(_extract_bearer(authorization), settings.write_tokens)
    except HTTPException:
        _log_auth_failure(request)
        raise
    if not ok:
        # Distinguish "wrong token" from "valid but read-only". The reviewer
        # token IS valid — telling its holder "invalid token" is factually
        # wrong, reads like broken demo credentials during App Review, and
        # gives a real read-only user no clue that a fuller token exists.
        # 403 (authenticated, not permitted) vs 401 (not authenticated).
        token = _extract_bearer(authorization)
        if tokens_match(token,
                        settings.valid_api_tokens | db.guest_token_cache()):
            raise HTTPException(
                status_code=403,
                detail="this access token is read-only — backups, restores and "
                       "other changes need the server's full-access API token")
        _log_auth_failure(request)
        raise HTTPException(status_code=401, detail="invalid token")


def _is_reviewer(authorization: str | None) -> bool:
    """True when the presented bearer is the read-only reviewer/demo token."""
    return tokens_match(_extract_bearer(authorization), settings.reviewer_api_token)


def _is_limited_read(authorization: str | None) -> bool:
    """True for any read-only token that is NOT the operator's own.

    Covers the reviewer/demo token and every guest share token. Both can read
    /api/* but neither should see the operator's infrastructure: the alerts
    response carries SMTP host, username and sender, which is the maintainer's
    mail setup. Sharing a station with family must not also share that.

    Guest tokens were admitted to the read surface without being added here,
    so this generalises the reviewer check rather than adding a second one.
    App-minted share tokens (db.guest_token_cache) are the same read-only
    contract as the env guests and MUST be limited identically — they were
    admitted to require_token's union without being added here, which handed
    every share-link recipient the operator view: SMTP identity from
    /api/alerts and the un-stripped home coordinates from /api/devices
    (found by the 2026-08-20 review, same day the minting shipped).
    """
    if _is_reviewer(authorization):
        return True
    presented = _extract_bearer(authorization)
    return tokens_match(presented,
                        settings.guest_tokens | db.guest_token_cache())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/metrics")
async def prometheus_metrics() -> PlainTextResponse:
    """Prometheus exposition of every device's latest reading. Opt-in via
    PROMETHEUS_METRICS=1 (404 otherwise); open when enabled — same data class
    as the public dashboard. Point Prometheus/Grafana here for dashboards +
    alerting. See app/metrics.py."""
    if not settings.prometheus_metrics:
        raise HTTPException(status_code=404, detail="metrics not enabled")
    from . import metrics as _metrics
    devices = await db.list_devices()
    text = _metrics.render_prometheus(devices, int(time.time() * 1000))
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/version")
async def api_version() -> JSONResponse:
    """Running version + (if the daily check has run) the latest published
    release and whether an update is available. Open — version info is not a
    secret in an open-source project, and the app / monitoring read it to
    surface an update hint. See app/updates.py (opt-out with UPDATE_CHECK=0)."""
    info = getattr(app.state, "update_info", {"version": __version__,
                                              "latest": None,
                                              "update_available": False,
                                              "checked_ms": None, "enabled": False})
    return JSONResponse(info)


@app.get("/", response_class=HTMLResponse)
@app.get("/status", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    """Public read-only status page. No secrets exposed — just enough to
    verify the deploy is alive and ingesting data. Anyone can hit this; we
    only show device names + counts + last-poll timestamp."""
    devices = await db.list_devices()
    now_ms = int(time.time() * 1000)
    rows = []
    total_obs = 0
    # Find the freshest non-null tempf across all devices for the sanity-check
    # tile. "Freshest" = highest dateutc_ms in the observations table, scoped
    # to rows that actually have a tempf value (a few SDR-coalesced posts can
    # land without it if the message-type cycle hasn't seen temp yet).
    latest_temp: dict | None = None
    for d in devices:
        n = await db.observation_count(d["mac"])
        total_obs += n
        last_seen_ms = d.get("lastSeen")
        last_seen_label = "—"
        last_seen_class = "stale"
        if last_seen_ms:
            age = (now_ms - last_seen_ms) / 1000
            last_seen_label = _humanize_age(age)
            last_seen_class = "fresh" if age < 600 else ("warm" if age < 3600 else "stale")
        # Latest observation may or may not include tempf — pick best.
        # Coerce through pd._num, never bare float(): stored values are not
        # guaranteed numeric (rows written before the ingest-boundary scrub
        # can hold strings), and a ValueError here 500s the ANONYMOUS `/`
        # page — same hardening the wind-rose loop already has.
        obs = await db.latest_observation(d["mac"])
        tval = _pd._num(obs.get("tempf")) if obs else None
        if tval is not None:
            obs_ms = obs.get("dateutc")
            if obs_ms and (latest_temp is None or obs_ms > latest_temp["ts_ms"]):
                latest_temp = {
                    "tempf": tval,
                    "ts_ms": obs_ms,
                    "device": d.get("name") or d["mac"],
                }
        # Public page: mask the MAC to its last 2 bytes and DON'T publish the
        # operator's free-text location label (it can name a home). Device
        # name + counts + freshness stay — enough to eyeball "the deploy is
        # alive and ingesting" without disclosing who/where.
        raw_mac = d["mac"]
        masked_mac = ("··:" * 4 + raw_mac[-5:]) if len(raw_mac) >= 5 else "··"
        rows.append({
            "name": d.get("name") or masked_mac,
            "mac": masked_mac,
            "count": n,
            "last_seen": last_seen_label,
            "last_seen_class": last_seen_class,
        })

    uptime = time.time() - getattr(app.state, "started_at", time.time())
    update_info = getattr(app.state, "update_info", None)

    # Optional public dashboard: current conditions + 24h charts for the
    # operator's station(s), rendered in place of the app screenshots.
    dashboard_html = ""
    if settings.public_dashboard and devices:
        dashboard_html = await _cached_public_dashboard(devices, now_ms)

    return HTMLResponse(_render_status_html(
        rows, total_obs, uptime, latest_temp, now_ms, update_info,
        dashboard_html=dashboard_html))


# The public dashboard is rendered for ANONYMOUS requests on `/`, and the page
# carries a 2-minute auto-refresh — so every visitor (and every refresh, and any
# crawler) drove a fresh 24h history aggregation per device. Cache the rendered
# HTML for slightly less than the refresh interval: the page can't show anything
# newer than its own refresh cadence anyway, so this costs no freshness and makes
# the only unauthenticated compute path flat under load.
_PUBLIC_DASH_CACHE: tuple[float, str] | None = None
_PUBLIC_DASH_TTL_S = 100
# Serializes cache MISSES, mirroring _RECORDS_LOCKS. The TTL alone only flattens
# load once the cache is warm: every anonymous request arriving during a cold
# build started its own full 24h aggregation per device, so a burst on `/` (the
# 2-minute auto-refresh syncing up, or a crawler) multiplied the one compute this
# cache exists to avoid — on the only unauthenticated compute path.
# Built lazily, and reset alongside the cache by the test fixture: an
# asyncio.Lock binds to the first loop that awaits it, so a module-level
# instance raises "bound to a different event loop" once a second loop uses it
# (the suite runs asyncio.run() per test). Same reason _RECORDS_LOCKS is cleared.
_PUBLIC_DASH_LOCK: asyncio.Lock | None = None


async def _cached_public_dashboard(devices: list[dict], now_ms: int) -> str:
    global _PUBLIC_DASH_CACHE, _PUBLIC_DASH_LOCK
    hit = _PUBLIC_DASH_CACHE
    if hit is not None and time.time() - hit[0] < _PUBLIC_DASH_TTL_S:
        return hit[1]
    if _PUBLIC_DASH_LOCK is None:      # no await between test and assignment
        _PUBLIC_DASH_LOCK = asyncio.Lock()
    async with _PUBLIC_DASH_LOCK:
        # Re-check under the lock: a caller that queued behind an in-flight
        # build takes its result rather than running an identical second one.
        hit = _PUBLIC_DASH_CACHE
        if hit is not None and time.time() - hit[0] < _PUBLIC_DASH_TTL_S:
            return hit[1]
        html = await _build_public_dashboard(devices, now_ms)
        _PUBLIC_DASH_CACHE = (time.time(), html)
        return html


async def _build_public_dashboard(devices: list[dict], now_ms: int) -> str:
    """Gather current + 24h history for the selected station(s) and render the
    dashboard section. Selection: PUBLIC_DASHBOARD_MACS ('all' | csv | unset →
    primary/first device)."""
    from . import public_dashboard as pd
    fields = pd.resolve_fields(settings.public_dashboard_fields)
    sel = (settings.public_dashboard_macs or "").strip()
    by_mac = {d["mac"]: d for d in devices}
    if sel.lower() == "all":
        macs = [d["mac"] for d in devices]
    elif sel:
        # Match on the separator-stripped uppercase form so the operator can
        # write the MAC colonized or compact, lower or upper case.
        def _compact(m: str) -> str:
            return m.upper().replace("-", "").replace(":", "")
        want = {_compact(m) for m in sel.split(",") if m.strip()}
        macs = [d["mac"] for d in devices if _compact(d["mac"]) in want] or [devices[0]["mac"]]
    else:
        macs = [devices[0]["mac"]]  # primary = first device

    start_ms = now_ms - 24 * 3600 * 1000
    stations = []
    for mac in macs:
        d = by_mac.get(mac)
        if not d:
            continue
        obs = await db.latest_observation(mac)
        rows = await db.history(mac, start_ms, now_ms, limit=5000)
        # Always carry feelsLike too (overlaid on the temp chart), regardless
        # of the selected fields.
        series: dict[str, list] = {}
        for key in list(fields) + ["feelsLike"]:
            pts = []
            for r in rows:
                t = r.get("dateutc")
                # pd._num, not bare float(): raw-window rows come from
                # data_json and can carry non-numeric junk stored before the
                # ingest scrub — a ValueError here 500s the anonymous `/`
                # page (same guard as the wind-samples loop below).
                v = pd._num(r.get(key))
                if t is not None and v is not None:
                    pts.append((int(t), v))
            series[key] = pts
        # Paired (direction, speed) samples for the wind rose. Both values must
        # be finite: the cloud pollers write lastData straight through, and a
        # NaN direction reaching int() in the rose renderer 500s the public
        # status page for anonymous visitors.
        wind_samples = []
        for r in rows:
            wd, ws = pd._num(r.get("winddir")), pd._num(r.get("windspeedmph"))
            if wd is not None and ws is not None:
                wind_samples.append((wd, ws))
        # NON-blocking: never run the full-history records scan inside the
        # status-page request. Use the cache if warm; otherwise kick off a
        # background warm and render without the strip (it appears on a later
        # auto-refresh). Keeps the public page fast regardless of history size.
        recs = _records_cached_or_warm(mac)
        # The app main page's summary boards: 24h stats from the rows already
        # fetched, and the rain-periods row from the same enrichment /current
        # serves — so the public page replaces a screenshot of the app rather
        # than approximating one.
        if obs:
            await _fill_rain_periods(mac, obs)
        stations.append({"name": d.get("name") or mac, "obs": obs,
                         "series": series, "wind_samples": wind_samples,
                         "records": recs, "summary": pd.summary_stats(rows)})
    return pd.render_dashboard(stations, fields, tz_name=settings.timezone,
                               app_url=settings.public_dashboard_app_url,
                               location=settings.public_dashboard_location)


def _humanize_age(seconds: float) -> str:
    if seconds < 60:    return f"{int(seconds)}s ago"
    if seconds < 3600:  return f"{int(seconds // 60)}m ago"
    if seconds < 86400: return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


_DEFAULT_HERO_HTML = """<div class="hero">
      <div class="hero-shots">
        <div class="hero-shot">
          <img src="/static/dashboard.png" alt="Zasder Weather iOS app — Dashboard tab showing current conditions, 24h temperature chart, and stat tiles" loading="lazy">
          <div class="cap">Dashboard</div>
        </div>
        <div class="hero-shot">
          <img src="/static/charts.png" alt="Zasder Weather iOS app — Charts tab showing temperature time series with selectable field and time-range pickers" loading="lazy">
          <div class="cap">Charts</div>
        </div>
      </div>
      <div class="hero-copy">
        <p>A clean, dark, fast iOS app for personal weather stations. Bring your own backend (this one) and your station data is yours, end to end. No ads, no tracking, no subscriptions.</p>
        <p>Supports AmbientWeather and AcuRite Atlas out of the box. Multi-device dashboard, history charts across six fields, threshold-based local alerts, and a 7-day Open-Meteo forecast.</p>
      </div>
    </div>"""


def _render_status_html(rows: list[dict], total_obs: int, uptime_s: float,
                        latest_temp: dict | None = None,
                        now_ms: int | None = None,
                        update_info: dict | None = None,
                        dashboard_html: str = "") -> str:
    started = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    # Public dashboard on ⇒ swap the app screenshots for the live charts + an
    # App Store link, add its CSS, and auto-refresh the page.
    from . import public_dashboard as _pd
    if dashboard_html:
        dashboard_css = _pd.DASHBOARD_CSS
        refresh_meta = '<meta http-equiv="refresh" content="120">'
        # No banner: the App Store link rides beside the first station's
        # temperature now (Volney: the full-width row "takes up too much
        # room and looks strange"). render_dashboard placed it.
        hero_html = dashboard_html
    else:
        dashboard_css = ""
        refresh_meta = ""
        hero_html = _DEFAULT_HERO_HTML
    # Version line + "update available" banner (from the daily GitHub check).
    ui = update_info or {}
    _repo_url = "https://github.com/volneydouglas/zasder-weather-backend"
    version_html = f'<span class="ver">v{__version__}</span>'
    update_banner = ""
    if ui.get("update_available") and ui.get("latest"):
        update_banner = (
            f'<div class="update-banner">⬆ Update available: '
            f'<strong>v{_html.escape(str(ui["latest"]))}</strong> '
            f'(you have v{__version__}) — '
            f'<a href="{_repo_url}/releases" target="_blank" rel="noopener">'
            f'what\'s new →</a></div>'
        )
    # Escape every operator/source-supplied value before interpolating.
    # device.name and device.location flow in through /ingest/custom from
    # whoever is running the relay; the page is public so we can't trust them.
    # last_seen_class is internally-controlled (whitelisted strings) so it
    # doesn't need escaping.
    def esc(s: object) -> str: return _html.escape(str(s), quote=True)
    rows_html = "\n".join(
        f'<tr><td>{esc(r["name"])}</td>'
        f'<td class="mono">{esc(r["mac"])}</td><td class="num">{r["count"]:,}</td>'
        f'<td class="age {r["last_seen_class"]}">{esc(r["last_seen"])}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="4" class="muted">No devices yet — waiting for first poll.</td></tr>'
    days = int(uptime_s // 86400)
    hours = int((uptime_s % 86400) // 3600)
    mins = int((uptime_s % 3600) // 60)
    uptime_label = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"
    # Latest-temp tile contents. Renders "—" if no device has reported a
    # tempf yet (fresh deploy, AcuRite-only with hub silent, etc.).
    if latest_temp and now_ms:
        temp_val_html = f'{latest_temp["tempf"]:.1f}°F'
        age_s = max(0, (now_ms - latest_temp["ts_ms"]) / 1000)
        temp_sub_html = (f'<div class="stat-sub">{esc(latest_temp["device"])} · '
                        f'{esc(_humanize_age(age_s))}</div>')
    else:
        temp_val_html = "—"
        temp_sub_html = '<div class="stat-sub muted">no readings yet</div>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>Zasder Weather — Status</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ background: #0d0f12; color: #fff; font-family: system-ui, -apple-system, sans-serif;
            margin: 0; padding: 32px 16px; line-height: 1.4; }}
    .wrap {{ max-width: 720px; margin: 0 auto; }}
    h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.2px; }}
    .sub {{ font-size: 12px; color: rgba(255,255,255,0.55); margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 24px; }}
    .ver {{ font-size: 12px; font-weight: 600; color: rgba(255,255,255,0.4);
            vertical-align: middle; margin-left: 6px; }}
    .update-banner {{ margin: 14px 0 0; padding: 10px 14px; border-radius: 8px;
            background: rgba(212,168,83,0.14); border: 1px solid rgba(212,168,83,0.4);
            color: #e6c56a; font-size: 13px; }}
    .update-banner a {{ color: #e6c56a; font-weight: 700; }}
    .stat {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
              border-radius: 10px; padding: 12px; }}
    .stat .k {{ font-size: 9px; font-weight: 800; letter-spacing: 1.2px;
                 color: rgba(255,255,255,0.55); text-transform: uppercase; }}
    .stat .v {{ font-size: 22px; font-weight: 300; margin-top: 4px;
                 font-variant-numeric: tabular-nums; }}
    .stat-sub {{ font-size: 9px; color: rgba(255,255,255,0.45); margin-top: 4px;
                  letter-spacing: 0.3px; }}
    @media (max-width: 540px) {{
      .grid {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    table {{ width: 100%; border-collapse: collapse; background: rgba(255,255,255,0.03);
              border: 1px solid rgba(255,255,255,0.06); border-radius: 10px; overflow: hidden; }}
    th, td {{ text-align: left; padding: 10px 12px; font-size: 12px;
               border-bottom: 1px solid rgba(255,255,255,0.05); }}
    th {{ font-size: 9px; font-weight: 800; letter-spacing: 1px; color: rgba(255,255,255,0.55);
           text-transform: uppercase; background: rgba(255,255,255,0.02); }}
    tr:last-child td {{ border-bottom: none; }}
    .num, .age {{ font-variant-numeric: tabular-nums; }}
    .muted {{ color: rgba(255,255,255,0.5); }}
    .mono {{ font-family: ui-monospace, SF Mono, monospace; font-size: 10px; color: rgba(255,255,255,0.6); }}
    .fresh {{ color: oklch(78% 0.14 145); }}
    .warm  {{ color: oklch(78% 0.14 70); }}
    .stale {{ color: oklch(70% 0.20 28); }}
    .hero {{ margin-bottom: 24px; }}
    .hero-shots {{ display: flex; gap: 16px; justify-content: center; margin-bottom: 16px; }}
    .hero-shot {{ flex: 0 0 220px; }}
    .hero-shot img {{ width: 100%; height: auto; display: block;
                       border-radius: 28px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }}
    .hero-shot .cap {{ font-size: 10px; color: rgba(255,255,255,0.45); margin-top: 8px;
                        text-align: center; letter-spacing: 0.3px; }}
    .hero-copy p {{ font-size: 13px; color: rgba(255,255,255,0.75); margin: 0 0 10px;
                     max-width: 560px; margin-left: auto; margin-right: auto; text-align: center; }}
    @media (max-width: 540px) {{
      .hero-shots {{ flex-wrap: wrap; }}
      .hero-shot {{ flex: 0 0 calc(50% - 8px); max-width: calc(50% - 8px); }}
    }}
    footer {{ margin-top: 24px; font-size: 10px; color: rgba(255,255,255,0.35); }}
    a {{ color: oklch(70% 0.14 245); text-decoration: none; }}
    {dashboard_css}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Zasder Weather {version_html}</h1>
    <div class="sub">Read-only status — no auth required. The iOS app reads protected endpoints under <code>/api</code>.</div>
    {update_banner}
    {hero_html}
    <div class="grid">
      <div class="stat"><div class="k">Status</div><div class="v">Up</div></div>
      <div class="stat"><div class="k">Devices</div><div class="v">{len(rows)}</div></div>
      <div class="stat"><div class="k">Observations</div><div class="v">{total_obs:,}</div></div>
      <div class="stat"><div class="k">Latest temp</div><div class="v">{temp_val_html}</div>{temp_sub_html}</div>
    </div>
    <table>
      <thead><tr><th>Device</th><th>MAC</th><th>Rows</th><th>Last seen</th></tr></thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    <footer>
      Uptime {uptime_label} · Generated {started}
      · <a href="https://github.com/volneydouglas/zasder-weather-backend">source</a>
    </footer>
  </div>
</body>
</html>"""


@app.get("/api/config/backup", dependencies=[Depends(require_write_token)])
async def api_config_backup() -> dict[str, Any]:
    """Everything the operator configured by hand, so it survives losing the
    server. No tokens and no SMTP password — see app/config_backup.py.

    Write-gated despite being a GET: the export carries alert recipient email
    addresses, SMTP host/username/from and the operator's device coordinates —
    exactly what GET /api/alerts hides from the read-only reviewer token. A
    read-gated backup made that redaction a one-request bypass."""
    return await config_backup.export_config()


@app.post("/api/config/restore", dependencies=[Depends(require_write_token)])
async def api_config_restore(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """Apply a backup. Write-gated: this replaces alert rules for everyone
    using this backend, so the read-only reviewer token must not reach it."""
    try:
        summary = await config_backup.import_config(payload)
    except config_backup.RestoreError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "restored": summary,
            "note": ("SMTP password is never included in a backup — re-enter "
                     "it in Alerts if you use email alerts.")}


# ── App-minted read-only share tokens ("Share read-only access") ─────────
# The env-secret GUEST_API_TOKENS path required the fly CLI, which family
# sharing can't ask of anyone. These are the same read-only contract, minted
# from the app: valid on GETs (require_token unions db.guest_token_cache),
# never in write_tokens. ALL THREE routes are write-gated — a guest must not
# be able to mint further guests, enumerate other people's tokens, or revoke
# the operator's shares.

class GuestTokenBody(BaseModel):
    label: str | None = Field(default=None, max_length=64)


@app.post("/api/guest-tokens", dependencies=[Depends(require_write_token)])
async def api_create_guest_token(body: GuestTokenBody | None = None) -> dict[str, Any]:
    """Mint a read-only share token. The full token appears ONLY in this
    response — list/revoke work with the short id — so the operator's share
    sheet is the single place the credential ever surfaces."""
    token = "zwg_" + secrets.token_hex(16)
    label = (body.label or "").strip() if body and body.label else None
    now_ms = int(time.time() * 1000)
    await db.add_guest_token(token, label or None, now_ms)
    return {"token": token, "id": token[:db.GUEST_TOKEN_ID_LEN],
            "label": label or None, "created_ms": now_ms}


@app.get("/api/guest-tokens", dependencies=[Depends(require_write_token)])
async def api_list_guest_tokens() -> dict[str, Any]:
    rows = await db.list_guest_tokens()
    return {"tokens": [{"id": r["token"][:db.GUEST_TOKEN_ID_LEN],
                        "label": r["label"], "created_ms": r["created_ms"]}
                       for r in rows]}


@app.delete("/api/guest-tokens/{token_id}", dependencies=[Depends(require_write_token)])
async def api_revoke_guest_token(token_id: str) -> dict[str, Any]:
    n = await db.delete_guest_token(token_id)
    if n == 0:
        raise HTTPException(status_code=404, detail="no such share token")
    return {"ok": True, "revoked": n}


# ── Operator-triggered backend upgrade (Settings → "Update now") ─────────
# The push-button sibling of AUTO_UPDATE for operators who keep it off: the
# app checks /api/version (open, already carries update_available) and this
# write-gated endpoint applies the release ON DEMAND through the same
# machinery — image verified before the machine config is touched, same
# major only, never a downgrade. Operator intent replaces the maturity
# delay. The machine restarts on success, so the caller should treat a
# dropped connection as "probably applied" and re-poll /api/version.

@app.post("/api/update/apply", dependencies=[Depends(require_write_token)])
async def api_update_apply() -> dict[str, Any]:
    from . import self_update
    from .updates import is_newer, parse_version
    # One update at a time: a double-tap or HTTP retry otherwise POSTs the
    # machine update twice — a second restart for nothing (CODE_REVIEW_R5
    # R5-21). Non-blocking on purpose: the second caller learns instantly.
    lock: asyncio.Lock | None = getattr(app.state, "update_apply_lock", None)
    if lock is None:      # lazily built — binds to the running loop
        lock = app.state.update_apply_lock = asyncio.Lock()
    if lock.locked():
        raise HTTPException(status_code=409,
                            detail="an update is already being applied")
    async with lock:
        return await _update_apply_locked(self_update, is_newer, parse_version)


async def _update_apply_locked(self_update, is_newer, parse_version) -> dict[str, Any]:
    info = getattr(app.state, "update_info", None) or {}
    latest = info.get("latest")
    if not latest or not is_newer(latest, __version__):
        raise HTTPException(status_code=409,
                            detail=f"already up to date (v{__version__})")
    if parse_version(latest)[0] != parse_version(__version__)[0]:
        raise HTTPException(
            status_code=409,
            detail=f"v{latest} is a major upgrade and may carry manual "
                   "steps — follow the release notes to upgrade")
    if not self_update._fly_token():
        raise HTTPException(
            status_code=409,
            detail="no deploy token on this instance — create one with "
                   "`fly tokens create deploy` and set it as the "
                   "FLY_API_TOKEN secret, then try again")
    repo = self_update._image_repo()
    if not await self_update.image_exists(repo, latest):
        raise HTTPException(
            status_code=409,
            detail=f"release v{latest} has no published image yet — "
                   "try again in a few minutes")
    ok = await self_update.apply_update(latest)
    if not ok:
        raise HTTPException(status_code=502,
                            detail="the platform rejected the update — "
                                   "see the server logs")
    return {"ok": True, "applying": latest,
            "note": "the server restarts into the new release; "
                    "re-check /api/version shortly"}


# ── Cloud-source integrations (Settings → Integrations) ──────────────────
# Configure the AmbientWeather / WeatherLink / Tempest pollers from the app
# (server_kv, kv-over-env — the WU-key precedent) without a redeploy. ALL
# write-gated, and the GET is too: which providers an operator uses is
# operator business, and the response enumerates credential presence.

@app.get("/api/integrations", dependencies=[Depends(require_write_token)])
async def api_integrations_status() -> dict[str, Any]:
    from . import integrations
    return {"providers": await integrations.status()}


@app.put("/api/integrations/{provider}", dependencies=[Depends(require_write_token)])
async def api_integrations_put(provider: str,
                               body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """Store fields (omitted = unchanged, empty = clear back to env — the
    SMTP-password partial-update contract) and apply immediately: the
    provider's poller restarts with the effective credentials."""
    from . import integrations
    if provider not in integrations.PROVIDERS:
        raise HTTPException(status_code=404,
                            detail=f"unknown provider {provider!r}; "
                                   f"known: {sorted(integrations.PROVIDERS)}")
    allowed = {f for f, _, _ in integrations.PROVIDERS[provider]}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"unknown fields {sorted(unknown)}; "
                                   f"allowed: {sorted(allowed)}")
    try:
        await integrations.store(provider, body)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"bad value: {e}")
    running = await app.state.integration_manager.apply(provider)
    # One cheap authenticated call so wrong keys never save as a silent
    # success (CODE_REVIEW_R5 R5-07; the R3-21 serverNote precedent). The
    # values persist either way — an upstream outage must not block saving —
    # but the UI gets the failure to show next to the "On" pill.
    check = await integrations.probe(provider)
    return {"ok": True, "running": running, "check": check,
            "providers": await integrations.status()}


@app.delete("/api/integrations/{provider}", dependencies=[Depends(require_write_token)])
async def api_integrations_clear(provider: str) -> dict[str, Any]:
    """Clear every app-stored field for the provider. Env-configured
    credentials (if any) take back over; otherwise the poller stops."""
    from . import integrations
    if provider not in integrations.PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    await integrations.clear(provider)
    running = await app.state.integration_manager.apply(provider)
    return {"ok": True, "running": running,
            "providers": await integrations.status()}


@app.get("/api/sources", dependencies=[Depends(require_token)])
async def api_sources() -> dict[str, Any]:
    """Health of each ingest source.

    Exists because a cloud poller that quietly stops — expired API keys, a
    revoked token, an upstream outage — is indistinguishable from dead
    hardware at the station end. This says which leg last worked and what the
    last failure said.

    `wu_upload` mirrors that for the OUTBOUND leg (1.5 WU forwarding): per
    enabled mac, when the last accepted upload happened and what the last
    failure was (status/type only — never the key or URL, see wu_upload.py).
    """
    from . import wu_upload
    uploads: dict[str, Any] = {}
    for assoc in await db.list_wu_stations():
        if not assoc["upload_enabled"]:
            continue
        uploads[assoc["mac"]] = {
            "enabled": True,
            "station_id": assoc["station_id"],
            "configured": bool(assoc["station_id"] and assoc["upload_key"]),
            **wu_upload.stats(assoc["mac"]),
        }
    return {"sources": source_status.snapshot(), "wu_upload": uploads}


def _strip_device_pii(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the operator's home location from a device list.

    `location` is a free-text label that routinely names a house, and
    `info.coords` is the precise lat/lon — the same pair the public dashboard
    refuses to publish. The reviewer/demo token needs the weather, not the
    address."""
    out = []
    for d in devices:
        info = {k: v for k, v in (d.get("info") or {}).items()
                if k not in ("coords", "location")}
        out.append({**d, "location": None, "info": info})
    return out


@app.get("/api/devices", dependencies=[Depends(require_token)])
async def get_devices(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    devices = await db.list_devices()
    if _is_limited_read(authorization):
        devices = _strip_device_pii(devices)
    return JSONResponse(devices)


# ───────────────────────── alert preferences (app-managed) ─────────────────────────
# The iOS app reads/writes these to control device-down email alerts. The
# SMTP transport itself stays a server secret (env); only PREFERENCES live
# here. DB prefs override env defaults; the monitor re-reads each tick.

class AlertPrefsIn(BaseModel):
    enabled: bool | None = None
    default_threshold_minutes: float | None = Field(default=None, ge=1, le=1440)
    repeat_hours: float | None = Field(default=None, ge=0, le=168)
    recipients: list[str] | None = None
    # App-managed SMTP transport. Password is write-only (never returned).
    # Send "" to clear a field back to the env default; omit to leave as-is.
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_tls: bool | None = None
    smtp_ssl: bool | None = None
    # 'all' | 'device_down' — which alert kinds may email (push is unscoped).
    email_scope: str | None = None
    # Storm summary. Bounded rather than free: a quiet window under 5 minutes
    # would split one storm into several summaries. min_total's ge=0 is
    # DELIBERATE: 0 opts into a summary for any measurable rain at all
    # (desert stations count single tips); the default keeps the 0.05 floor
    # so nobody gets tip-spam without asking for it (R5-28).
    storm_summary: bool | None = None
    storm_quiet_minutes: float | None = Field(default=None, ge=5, le=360)
    storm_min_total_in: float | None = Field(default=None, ge=0, le=10)


class DeviceAlertIn(BaseModel):
    monitor: bool = True
    threshold_minutes: float | None = Field(default=None, ge=1, le=1440)


async def _alerts_state() -> dict[str, Any]:
    """Full alert config + per-device status — the shape the iOS app renders."""
    from .alerts import effective_config, _device_threshold
    cfg = await effective_config()
    prefs = await db.get_alert_prefs()
    dev_prefs = await db.get_device_alert_prefs()
    states = await db.get_alert_states()
    devices = await db.list_devices()
    dev_list = []
    for d in devices:
        mac = d["mac"]
        dp = dev_prefs.get(mac, {})
        thr = _device_threshold(mac, dev_prefs, cfg.default_threshold_min)
        dev_list.append({
            "mac": mac,
            "name": d.get("name") or mac,
            "monitor": thr is not None,
            "threshold_minutes": thr,                       # effective; None if unmonitored
            "threshold_override": dp.get("threshold_min"),  # raw per-device value or None
            "last_seen_ms": d.get("lastSeen"),
            "state": (states.get(mac) or {}).get("state"),  # 'ok'|'stale'|None
        })
    return {
        "transport_configured": cfg.transport_configured,
        "enabled": cfg.enabled,
        "enabled_override": prefs["enabled"],               # raw 0/1/None
        "default_threshold_minutes": cfg.default_threshold_min,
        "repeat_hours": cfg.repeat_hours,
        "recipients": cfg.recipients,
        "recipients_source": "app" if prefs["recipients"] else "env",
        # SMTP transport — everything EXCEPT the password (write-only).
        "smtp_host": cfg.smtp_host,
        "smtp_port": cfg.smtp_port,
        "smtp_username": cfg.smtp_username,
        "smtp_from": cfg.smtp_from,
        "smtp_tls": cfg.smtp_tls,
        "smtp_ssl": cfg.smtp_ssl,
        "smtp_password_set": bool(cfg.smtp_password),
        "smtp_source": "app" if prefs["smtp_host"] else ("env" if cfg.smtp_host else "none"),
        "email_scope": cfg.email_scope,
        # Storm summary — the effective values, plus whether they came from
        # the app or the server env, so the UI can say which is in charge.
        "storm_summary": cfg.storm_summary,
        "storm_quiet_minutes": cfg.storm_quiet_minutes,
        "storm_min_total_in": cfg.storm_min_total_in,
        "storm_source": "app" if prefs["storm_summary"] is not None else "env",
        # Smart-alert firing state, so a client with no push channel of its
        # own (the macOS app) can edge-detect these the way it now does
        # threshold rules. Rides on this response rather than a new endpoint
        # because the Mac already fetches it every minute.
        "smart_alerts_enabled": settings.smart_alerts,
        "smart_alerts": [
            {"mac": mac, "kind": kind, "triggered": bool(trig)}
            for (mac, kind), trig in sorted((await db.get_smart_alert_states()).items())
        ],
        "devices": dev_list,
    }


@app.get("/api/alerts", dependencies=[Depends(require_token)])
async def get_alerts(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    state = await _alerts_state()
    if _is_limited_read(authorization):
        # Any non-operator read token gets the alerts UI state but not the
        # SMTP transport identifiers (host/username/from reveal the
        # maintainer's mail infrastructure; password was already write-only).
        for k in ("smtp_host", "smtp_username", "smtp_from"):
            if state.get(k):
                state[k] = "(hidden)"
        # Recipient addresses are the same data class as smtp_from (often
        # literally the same mailbox) — share-link guests don't get the
        # operator's personal emails (CODE_REVIEW_R5 R5-04 / R3-07).
        state["recipients"] = []
    return JSONResponse(state)


def _has_ctl(s: str) -> bool:
    """True if the string contains an ASCII control character (incl. \\n)."""
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


@app.put("/api/alerts", dependencies=[Depends(require_write_token)])
async def put_alerts(body: AlertPrefsIn) -> JSONResponse:
    fields: dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.storm_summary is not None:
        fields["storm_summary"] = 1 if body.storm_summary else 0
    if body.storm_quiet_minutes is not None:
        fields["storm_quiet_minutes"] = body.storm_quiet_minutes
    if body.storm_min_total_in is not None:
        fields["storm_min_total_in"] = body.storm_min_total_in
    if body.default_threshold_minutes is not None:
        fields["default_threshold_min"] = body.default_threshold_minutes
    if body.repeat_hours is not None:
        fields["repeat_hours"] = body.repeat_hours
    if body.email_scope is not None:
        if body.email_scope not in ("all", "device_down"):
            raise HTTPException(status_code=400,
                                detail="email_scope must be 'all' or 'device_down'")
        fields["email_scope"] = body.email_scope
    if body.recipients is not None:
        clean = [r.strip() for r in body.recipients if r.strip()]
        for r in clean:
            # One address, no whitespace/control chars, and NO comma: the
            # stored form is comma-joined and re-split on ",", so a recipient
            # containing one would silently become two; a control char (\n)
            # would corrupt every alert send's headers.
            if not re.fullmatch(r"[^@\s,]+@[^@\s,]+", r) or _has_ctl(r):
                raise HTTPException(status_code=400, detail=f"invalid recipient: {r!r}")
        # Empty list clears the override → falls back to env recipients.
        fields["recipients"] = ",".join(clean) if clean else None
    # SMTP transport (DB over env). Empty string clears → env fallback.
    # Control characters are rejected on every header-bound value — a \n in
    # smtp_from breaks EmailMessage for every subsequent alert.
    for attr in ("smtp_host", "smtp_username", "smtp_from"):
        val = getattr(body, attr)
        if val is not None:
            if _has_ctl(val):
                raise HTTPException(status_code=400,
                                    detail=f"{attr} must not contain control characters")
            fields[attr] = val.strip() or None
    if body.smtp_port is not None:     fields["smtp_port"] = body.smtp_port
    if body.smtp_password is not None: fields["smtp_password"] = body.smtp_password or None
    if body.smtp_tls is not None:      fields["smtp_tls"] = 1 if body.smtp_tls else 0
    if body.smtp_ssl is not None:      fields["smtp_ssl"] = 1 if body.smtp_ssl else 0
    await db.set_alert_prefs(**fields)
    return JSONResponse(await _alerts_state())


@app.put("/api/devices/{mac}/alert", dependencies=[Depends(require_write_token)])
async def put_device_alert(mac: str, body: DeviceAlertIn) -> JSONResponse:
    from .ingest import _format_mac
    await db.upsert_device_alert_pref(_format_mac(mac), body.monitor, body.threshold_minutes)
    return JSONResponse(await _alerts_state())


class DeviceLocationIn(BaseModel):
    lat: float
    lon: float
    label: str | None = None


@app.put("/api/devices/{mac}/location", dependencies=[Depends(require_write_token)])
async def put_device_location(mac: str, body: DeviceLocationIn) -> JSONResponse:
    """Set a device's location (iOS per-device Location setting). Overrides the
    ingest-time default; the top-ordered device drives the forecast + sun dial."""
    from .ingest import _format_mac
    if not (-90.0 <= body.lat <= 90.0) or not (-180.0 <= body.lon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    norm = _format_mac(mac)
    await db.set_device_location(norm, body.lat, body.lon, body.label,
                                 int(time.time() * 1000))
    return JSONResponse({"ok": True, "mac": norm, "lat": body.lat,
                         "lon": body.lon, "label": body.label})


class WUStationIn(BaseModel):
    # All fields optional = "leave unchanged", so the app can flip the upload
    # toggle without re-sending the station ID (and 1.4 clients that only
    # ever send wu_station_id keep their exact semantics). Empty string
    # clears. WU IDs are short uppercase alphanumerics (KAZCHAND802);
    # bound + shape-check so junk can't land.
    wu_station_id: str | None = Field(default=None, max_length=32,
                                      pattern=r"^[A-Za-z0-9]*$")
    # WU *station* key for live upload — write-only, like /api/config/wu-key:
    # "" clears, and no endpoint ever returns it (GET reports upload_key_set).
    upload_key: str | None = Field(default=None, max_length=64,
                                   pattern=r"^[A-Za-z0-9]*$")
    # Turn live forwarding on/off (app/wu_upload.py).
    upload_enabled: bool | None = None


def _wu_station_view(norm: str, row: dict[str, Any] | None) -> dict[str, Any]:
    """API shape for a WU association: the key itself NEVER leaves the
    server — only whether one is set."""
    return {"mac": norm,
            "wu_station_id": row["station_id"] if row else None,
            "upload_enabled": bool(row and row["upload_enabled"]),
            "upload_key_set": bool(row and row["upload_key"])}


@app.get("/api/devices/{mac}/wu-station", dependencies=[Depends(require_token)])
async def get_wu_station(mac: str) -> JSONResponse:
    from .ingest import _format_mac
    norm = _format_mac(mac)
    return JSONResponse(_wu_station_view(norm, await db.get_wu_station(norm)))


@app.put("/api/devices/{mac}/wu-station", dependencies=[Depends(require_write_token)])
async def put_wu_station(mac: str, body: WUStationIn) -> JSONResponse:
    """Associate a Weather Underground station ID with a device — the WU
    importer's target mapping and (1.5) the live-upload config. Omitted
    fields are left unchanged; "" clears. Clearing the station ID drops the
    upload key + toggle with it (see db.set_wu_station)."""
    from .ingest import _format_mac
    norm = _format_mac(mac)
    # Known devices only (same check as start_wu_import): a typo'd MAC would
    # otherwise create a wu_station_map row for a nonexistent device that
    # silently attaches to whatever registers under that MAC later.
    if not any(d["mac"] == norm for d in await db.list_devices()):
        raise HTTPException(status_code=404, detail=f"unknown device {norm}")
    kwargs: dict[str, Any] = {}
    if body.wu_station_id is not None:
        kwargs["station_id"] = body.wu_station_id.strip().upper() or None
    if body.upload_key is not None:
        kwargs["upload_key"] = body.upload_key.strip() or None
    if body.upload_enabled is not None:
        kwargs["upload_enabled"] = body.upload_enabled
    # Upload config without a station to upload to would be silently dropped
    # by the row-deletion semantics — refuse it loudly instead.
    existing = await db.get_wu_station(norm)
    effective_sid = kwargs.get("station_id",
                               existing["station_id"] if existing else None)
    if effective_sid is None and (kwargs.get("upload_key")
                                  or kwargs.get("upload_enabled")):
        raise HTTPException(status_code=400,
                            detail="set a wu_station_id before configuring "
                                   "the WU upload key or enabling forwarding")
    if kwargs:
        await db.set_wu_station(norm, now_ms=int(time.time() * 1000), **kwargs)
    row = await db.get_wu_station(norm)
    return JSONResponse({"ok": True, **_wu_station_view(norm, row)})


class WUKeyIn(BaseModel):
    # Write-only, like the SMTP password: never returned by any endpoint.
    # "" clears back to the WU_API_KEY env fallback.
    api_key: str = Field(max_length=128, pattern=r"^[A-Za-z0-9]*$")


async def effective_wu_key() -> str | None:
    """App-managed key over env secret — the SMTP resolution pattern."""
    return await db.get_kv("wu_api_key") or settings.wu_api_key


@app.get("/api/config/wu-key", dependencies=[Depends(require_token)])
async def get_wu_key_status() -> JSONResponse:
    stored = await db.get_kv("wu_api_key")
    return JSONResponse({
        "configured": bool(stored or settings.wu_api_key),
        "source": "app" if stored else ("env" if settings.wu_api_key else "none"),
    })


@app.put("/api/config/wu-key", dependencies=[Depends(require_write_token)])
async def put_wu_key(body: WUKeyIn) -> JSONResponse:
    """Store the Weather Underground API key server-side (powers the TWC
    forecast source; the app's Import History screen syncs it here)."""
    key = body.api_key.strip() or None
    if key is not None and len(key) < 8:
        raise HTTPException(status_code=400, detail="api_key too short")
    await db.set_kv("wu_api_key", key)
    stored = await db.get_kv("wu_api_key")
    return JSONResponse({
        "ok": True,
        "configured": bool(stored or settings.wu_api_key),
        "source": "app" if stored else ("env" if settings.wu_api_key else "none"),
    })


@app.get("/api/insights", dependencies=[Depends(require_token)])
async def get_insights(mac: str = Query(...)) -> JSONResponse:
    """Station statistics over the rollup tables (heat ledger, rain seasons,
    normals/anomalies, diurnal grid, calendar). Opt-in: INSIGHTS=1."""
    from . import insights
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    norm = _format_mac(mac)
    payload = await insights.assemble(norm)
    if payload["day_count"] == 0:
        # Distinguish "no data" from "flag enabled after data existed".
        payload["hint"] = ("no rollups yet — if this station has history, "
                           "POST /api/insights/rebuild once")
    return JSONResponse(payload)


@app.get("/api/insights/daily", dependencies=[Depends(require_token)])
async def get_insights_daily(mac: str = Query(...),
                             days: int = Query(60, ge=7, le=366)) -> JSONResponse:
    """Per-day temperature series for one station (rollups only) — the
    sensor-drift card fetches this once per visible station and diffs the
    daily means client-side. Opt-in with the rest of Insights."""
    from . import insights
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    return JSONResponse(await insights.daily_series(_format_mac(mac), days))


@app.post("/api/insights/rebuild", dependencies=[Depends(require_write_token)])
async def rebuild_insights(mac: str | None = Query(None)) -> JSONResponse:
    """Recompute rollups from raw history — run once after enabling INSIGHTS
    on existing data, or after importing history while it was disabled."""
    from . import insights
    if not settings.insights:
        raise HTTPException(status_code=404, detail="insights not enabled")
    from .ingest import _format_mac
    return JSONResponse(await insights.rebuild(_format_mac(mac) if mac else None))


class WUImportIn(BaseModel):
    mac: str
    # Falls back to the device's stored wu_station_map association.
    wu_station_id: str | None = Field(default=None, max_length=32,
                                      pattern=r"^[A-Za-z0-9]+$")
    # Never persisted or logged; lives only in the import task's closure.
    # Optional: when omitted, the server-stored key (PUT /api/config/wu-key,
    # or the WU_API_KEY env var) is used — a LAN user who followed the app's
    # own cleartext-safety advice and configured the key server-side must
    # not be forced to POST it in the body to start an import.
    api_key: str | None = Field(default=None, min_length=8, max_length=128,
                                pattern=r"^[A-Za-z0-9]+$")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    dry_run: bool = False


@app.post("/api/import/wu", dependencies=[Depends(require_write_token)])
async def start_wu_import(body: WUImportIn) -> JSONResponse:
    """Begin a day-by-day WU history import into an existing device. One at a
    time; poll GET /api/import/wu/status. dry_run counts without inserting."""
    from datetime import date as _date
    from . import wu_import
    from .ingest import _format_mac
    mac = _format_mac(body.mac)
    if not any(d["mac"] == mac for d in await db.list_devices()):
        raise HTTPException(status_code=404, detail=f"unknown device {mac}")
    assoc = await db.get_wu_station(mac)
    station = ((body.wu_station_id or "").strip().upper()
               or (assoc["station_id"] if assoc else None))
    if not station:
        raise HTTPException(status_code=400,
                            detail="no wu_station_id given and none associated "
                                   "with this device (PUT .../wu-station first)")
    try:
        start = _date.fromisoformat(body.start_date)
        end = _date.fromisoformat(body.end_date) if body.end_date else _date.today()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad date: {e}")
    if start > end:
        raise HTTPException(status_code=400, detail="start_date is after end_date")
    api_key = body.api_key or await effective_wu_key()
    if not api_key:
        raise HTTPException(status_code=400,
                            detail="no api_key given and no server-stored WU "
                                   "key (PUT /api/config/wu-key first)")
    if not wu_import.start_import(mac, station, api_key, start, end,
                                  body.dry_run):
        raise HTTPException(status_code=409, detail="an import is already running")
    return JSONResponse({"ok": True, "mac": mac, "wu_station_id": station,
                         "days": (end - start).days + 1, "dry_run": body.dry_run})


@app.get("/api/import/wu/status", dependencies=[Depends(require_token)])
async def wu_import_status() -> JSONResponse:
    from . import wu_import
    return JSONResponse(wu_import.status())


@app.post("/api/import/wu/cancel", dependencies=[Depends(require_write_token)])
async def wu_import_cancel() -> JSONResponse:
    from . import wu_import
    return JSONResponse({"ok": wu_import.cancel()})


# Test-email throttle (R2-111 second half): /api/alerts/test is a real SMTP
# send to every configured recipient, so an unthrottled write-token holder
# could pump unlimited email through the operator's SMTP account (and trip
# provider abuse lockouts with failed logins). One send attempt per process
# per minute is plenty for the app's setup screen. Process-global like
# _AUTH_FAIL_LOG_TS.
_TEST_ALERT_TS: float | None = None
_TEST_ALERT_MIN_INTERVAL_S = 60.0


@app.post("/api/alerts/test", dependencies=[Depends(require_write_token)])
async def test_alert() -> JSONResponse:
    """Send a one-off test email to the current recipients — lets the app's
    setup screen verify delivery end to end. Throttled to one attempt per
    minute (429 on repeats)."""
    import asyncio as _asyncio
    from .alerts import effective_config, _send_sync
    global _TEST_ALERT_TS
    cfg = await effective_config()
    if not cfg.transport_configured:
        raise HTTPException(status_code=400,
                            detail="SMTP transport not configured (set SMTP_HOST + creds as secrets)")
    if not cfg.recipients:
        raise HTTPException(status_code=400, detail="no recipients configured")
    now = time.monotonic()
    if _TEST_ALERT_TS is not None and now - _TEST_ALERT_TS < _TEST_ALERT_MIN_INTERVAL_S:
        raise HTTPException(status_code=429,
                            detail="a test email was just sent — wait a "
                                   "minute before sending another")
    # Marked before the attempt: a FAILED send still hit the SMTP server
    # (repeated bad logins can lock the operator's account), so it counts.
    _TEST_ALERT_TS = now
    try:
        await _asyncio.to_thread(
            _send_sync, "[Zasder Weather] Test alert",
            "This is a test from your Zasder Weather backend — device-down "
            "alerts are wired up correctly.", cfg.recipients, cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"send failed: {e}")
    return JSONResponse({"ok": True, "sent_to": cfg.recipients})


# ───────────────────────── push notifications (APNs) ─────────────────────────

class PushRegisterIn(BaseModel):
    # Real APNs tokens are 64 hex chars and FCM registration tokens are
    # printable ASCII (base64url + ':') — but neither is multi-KB or carries
    # whitespace/control chars. Bound + shape-check so junk can't accumulate
    # as fake "device tokens" up to the body cap.
    token: str = Field(min_length=8, max_length=512, pattern=r"^[\x21-\x7e]+$")
    env: str | None = None            # "sandbox" (dev build) | "production"
    platform: str = "ios"


@app.post("/api/push/register", dependencies=[Depends(require_write_token)])
async def push_register(body: PushRegisterIn) -> JSONResponse:
    """The iOS app posts its APNs device token here after the user grants
    notification permission. Idempotent (upsert)."""
    env = body.env if body.env in ("sandbox", "production") else None
    await db.register_push_token(body.token, body.platform, env)
    return JSONResponse({"ok": True})


class PushRelayIn(BaseModel):
    # Both optional: omit a field to leave it unchanged, send "" to clear it.
    relay_url: str | None = None
    relay_token: str | None = None


def _validate_relay_url(url: str) -> None:
    """Reject relay URLs that could be used to exfiltrate APNs device tokens
    via SSRF (reviewer P3). https only; refuse loopback/private/link-local IP
    literals. Hostnames pass through — DNS-rebinding mitigation belongs at the
    egress layer, not here."""
    import ipaddress
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="relay_url is not a valid URL")
    if u.scheme != "https":
        raise HTTPException(status_code=400, detail="relay_url must be https://")
    host = (u.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="relay_url is missing a host")
    if host in ("localhost", "ip6-localhost", "broadcasthost"):
        raise HTTPException(status_code=400,
                            detail="relay_url cannot point at a local address")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return                                    # hostname (not an IP) → OK
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise HTTPException(status_code=400,
                            detail="relay_url cannot point at a private/local address")


@app.get("/api/push/relay", dependencies=[Depends(require_token)])
async def get_push_relay() -> JSONResponse:
    """Report the app-managed relay config. The token is WRITE-ONLY — never
    returned; only whether one is set + the effective enabled state."""
    from .apns import effective_relay
    cfg = await db.get_push_relay() or {}
    url, token = await effective_relay()
    return JSONResponse({"relay_url": cfg.get("url"),
                         "relay_token_set": bool(cfg.get("token")),
                         "relay_configured": bool(url and token)})


@app.put("/api/push/relay", dependencies=[Depends(require_write_token)])
async def put_push_relay(body: PushRelayIn) -> JSONResponse:
    """The iOS app stores the relay token it obtained (via App Attest against
    the relay) here so this backend can push through the relay. Write-only
    token, same pattern as SMTP creds."""
    cur = await db.get_push_relay() or {}
    url = cur.get("url")
    if body.relay_url is not None:
        if body.relay_url:
            _validate_relay_url(body.relay_url)
        url = body.relay_url or None
    token = cur.get("token")
    if body.relay_token is not None:
        token = body.relay_token or None
    await db.set_push_relay(url, token)
    return JSONResponse({"ok": True, "relay_url": url,
                         "relay_configured": bool(url and token)})


# ───────────────────────── threshold alert rules ─────────────────────────

class AlertRuleIn(BaseModel):
    field: str
    comparator: str
    threshold: float
    target_mac: str | None = None     # None = any device


@app.get("/api/alerts/rules", dependencies=[Depends(require_token)])
async def list_rules() -> JSONResponse:
    return JSONResponse(await db.list_alert_rules())


@app.post("/api/alerts/rules", dependencies=[Depends(require_write_token)])
async def create_rule(body: AlertRuleIn) -> JSONResponse:
    from .alerts import THRESHOLD_FIELDS, THRESHOLD_COMPARATORS
    from .ingest import _format_mac
    if body.field not in THRESHOLD_FIELDS:
        raise HTTPException(status_code=400,
                            detail=f"unknown field {body.field!r}; allowed: {sorted(THRESHOLD_FIELDS)}")
    if body.comparator not in THRESHOLD_COMPARATORS:
        raise HTTPException(status_code=400,
                            detail=f"comparator must be one of {sorted(THRESHOLD_COMPARATORS)}")
    mac = _format_mac(body.target_mac) if body.target_mac else None
    rule = await db.create_alert_rule(mac, body.field, body.comparator, body.threshold)
    return JSONResponse(rule)


class AlertRulePatch(BaseModel):
    enabled: bool


@app.patch("/api/alerts/rules/{rule_id}", dependencies=[Depends(require_write_token)])
async def patch_rule(rule_id: int, body: AlertRulePatch) -> JSONResponse:
    rule = await db.set_alert_rule_enabled(rule_id, body.enabled)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return JSONResponse(rule)


@app.delete("/api/alerts/rules/{rule_id}", dependencies=[Depends(require_write_token)])
async def delete_rule(rule_id: int) -> JSONResponse:
    if not await db.delete_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail="rule not found")
    return JSONResponse({"ok": True, "deleted": rule_id})


@app.delete("/api/devices/{mac}", dependencies=[Depends(require_write_token)])
async def delete_device(mac: str) -> JSONResponse:
    """Remove a device + all its observations + alert state. Useful after
    retiring a source (e.g. you stopped polling a cloud feed) so a stale
    device doesn't sit on the dashboard. Returns a count summary."""
    from .ingest import _format_mac
    counts = await db.delete_device(_format_mac(mac))
    if counts["devices"] == 0:
        raise HTTPException(status_code=404, detail="device not found")
    return JSONResponse({"ok": True, "deleted_mac": _format_mac(mac), **counts})


async def _fill_rain_periods(mac: str, obs: dict[str, Any]) -> None:
    """Rain rollup enrichment: fill period totals the source doesn't post.
    SDR posts only yearlyrainin (differenced at period boundaries), the
    Tempest posts only hourly+daily (summed per-day, rain_rollups tier 3 —
    before that tier, the old yearlyrainin-only gate here meant a Tempest
    dashboard simply had no week/month/year). AWN-sourced rows ship every
    bucket pre-computed and the fill-only-None leaves them untouched. Gated
    on SOME rain counter being present: a station with no rain sensor at
    all must stay absent everywhere, not gain zeros. Shared by /current and
    the public dashboard's rain-periods board."""
    has_rain_counter = any(obs.get(k) is not None for k in
                           ("yearlyrainin", "monthlyrainin", "dailyrainin"))
    if not has_rain_counter or not any(
        obs.get(k) is None for k in
        ("dailyrainin", "hourlyrainin", "weeklyrainin", "monthlyrainin",
         "yearlyrainin")
    ):
        return
    try:
        rollups = await db.rain_rollups(mac, settings.timezone)
    except Exception as e:
        log.warning("rain_rollups failed for %s: %s", mac, e)
        rollups = {}
    for k, v in (("dailyrainin",   rollups.get("daily_in")),
                  ("hourlyrainin",  rollups.get("hourly_in")),
                  ("weeklyrainin",  rollups.get("weekly_in")),
                  ("monthlyrainin", rollups.get("monthly_in")),
                  ("yearlyrainin",  rollups.get("yearly_in"))):
        if obs.get(k) is None and v is not None:
            obs[k] = v


@app.get("/api/devices/{mac}/current", dependencies=[Depends(require_token)])
async def get_current(mac: str) -> JSONResponse:
    # Read-side MAC normalization: storage keys are the uppercase colonized
    # form. Write endpoints already normalize; without the same here a
    # lowercase/compact MAC from a script 404s while the uppercase works.
    from .ingest import _format_mac
    mac = _format_mac(mac)
    obs = await db.latest_observation(mac)
    if not obs:
        raise HTTPException(status_code=404, detail="no data for device")
    await _fill_rain_periods(mac, obs)
    return JSONResponse(obs)


@app.get("/api/devices/{mac}/history", dependencies=[Depends(require_token)])
async def get_history(
    mac: str,
    # 31 days + 1 h, not 30: Explore requests whole calendar months and July
    # is 744 hours — the 720 cap 422'd every 31-day month. The extra hour is
    # for a 31-day month spanning a DST fall-back transition (US November,
    # EU October), which is 745 ABSOLUTE hours; at exactly 744 the app's
    # whole-month request 422'd every year in those zones.
    hours: int = Query(24, ge=1, le=24 * 31 + 1),
    limit: int = Query(2000, ge=1, le=10_000),
    # Optional window END (epoch ms). Default = now, preserving the original
    # trailing-window behavior; the History/Explore browser passes a past
    # month's end to page through imported archives.
    end_ms: int | None = Query(None, ge=0),
) -> JSONResponse:
    from .ingest import _format_mac
    mac = _format_mac(mac)              # read-side key normalization
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    rows = await db.history(mac, start, end, limit=limit)
    return JSONResponse({"start": start, "end": end, "count": len(rows), "rows": rows})


@app.get("/api/devices/{mac}/summary", dependencies=[Depends(require_token)])
async def get_summary(
    mac: str,
    field: str = Query("tempf"),
    hours: int = Query(24, ge=1, le=24 * 30),
) -> JSONResponse:
    from .ingest import _format_mac
    mac = _format_mac(mac)              # read-side key normalization
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    try:
        agg = await db.aggregate(mac, field, start, end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(agg)


# Records are expensive (all-time window scans the full per-mac history) and
# barely change minute-to-minute, so cache per-mac for a while. Shared by the
# API endpoint and the public dashboard.
_RECORDS_CACHE: dict[str, tuple[float, dict]] = {}
_RECORDS_TTL_S = 900  # 15 min
_RECORDS_MAX_ENTRIES = 64          # far above any real device count
_RECORDS_LOCKS: dict[str, "asyncio.Lock"] = {}
# Strong refs to in-flight background warms (see _records_cached_or_warm).
_WARM_TASKS: set = set()


async def _warm_records(mac: str) -> dict:
    """Compute records for a device + refresh the cache.

    Serialized per MAC: the all-time window scans the full per-mac history, so
    a second caller must WAIT for the in-flight compute, not race it. The old
    dedupe short-circuited to `{}` when a warm was already running, which
    handed concurrent clients a 200 with an empty body (blank Records screen)
    whenever the status page had kicked off a background warm.
    """
    lock = _RECORDS_LOCKS.setdefault(mac, asyncio.Lock())
    async with lock:
        # A caller that queued behind the compute gets its fresh result.
        hit = _RECORDS_CACHE.get(mac)
        if hit and time.time() - hit[0] < _RECORDS_TTL_S:
            return hit[1]
        data = await db.records(mac, settings.timezone)
        _RECORDS_CACHE[mac] = (time.time(), data)
        _prune_records_cache()
        return data


def _prune_records_cache() -> None:
    """Drop expired entries and bound the cache.

    `mac` is an unvalidated path param, so without this a token holder could
    request unlimited distinct MACs and each would leave a permanent entry
    (db.records happily returns an empty skeleton for an unknown device).
    """
    def _drop_lock(key: str) -> None:
        # Never evict a HELD lock: dropping it lets the next caller build a fresh
        # one and run a second concurrent compute for the same MAC, defeating the
        # serialization the lock exists for.
        lk = _RECORDS_LOCKS.get(key)
        if lk is not None and not lk.locked():
            del _RECORDS_LOCKS[key]

    now = time.time()
    for k, (ts, _) in list(_RECORDS_CACHE.items()):
        if now - ts >= _RECORDS_TTL_S:
            del _RECORDS_CACHE[k]
            _drop_lock(k)
    while len(_RECORDS_CACHE) > _RECORDS_MAX_ENTRIES:      # oldest-first
        oldest = min(_RECORDS_CACHE, key=lambda k: _RECORDS_CACHE[k][0])
        del _RECORDS_CACHE[oldest]
        _drop_lock(oldest)


async def _cached_records(mac: str) -> dict:
    """Fresh cached records, else compute synchronously. Used by the API
    endpoint (authenticated, infrequent — OK to wait for a cold compute)."""
    hit = _RECORDS_CACHE.get(mac)
    if hit and time.time() - hit[0] < _RECORDS_TTL_S:
        return hit[1]
    return await _warm_records(mac)


def _records_cached_or_warm(mac: str) -> dict | None:
    """NON-blocking: fresh cached records, else spawn a background warm and
    return None. Keeps the status-page render off the full-history scan."""
    hit = _RECORDS_CACHE.get(mac)
    if hit and time.time() - hit[0] < _RECORDS_TTL_S:
        return hit[1]
    lock = _RECORDS_LOCKS.get(mac)
    if lock is None or not lock.locked():
        # Hold a strong reference: a bare create_task can be garbage-collected
        # mid-flight, and without a done-callback any failure (e.g. "database
        # is locked" during maintenance) surfaces only as a GC-time warning and
        # the records strip silently never appears.
        t = asyncio.create_task(_warm_records(mac))
        _WARM_TASKS.add(t)
        t.add_done_callback(_warm_task_done)
    return None


def _warm_task_done(t: "asyncio.Task") -> None:
    _WARM_TASKS.discard(t)
    if not t.cancelled() and t.exception() is not None:
        log.warning("records warm failed: %s", t.exception())


@app.get("/api/devices/{mac}/records", dependencies=[Depends(require_token)])
async def get_records(mac: str) -> JSONResponse:
    """All-time / yearly / monthly / today highs & lows per metric, with the
    local time each record was set. Cached 15 min per device."""
    from .ingest import _format_mac
    mac = _format_mac(mac)              # read-side key normalization
    # 404 unknown MACs: db.records() returns an empty skeleton for any string,
    # so without this each bogus MAC burned 40 aggregate queries and left a
    # permanent cache entry.
    known = {d["mac"] for d in await db.list_devices()}
    if mac not in known:
        raise HTTPException(status_code=404, detail="unknown device")
    return JSONResponse(await _cached_records(mac))


@app.get("/api/captures/{slug}", dependencies=[Depends(require_write_token)])
async def get_captures(slug: str, tail: int = Query(50, ge=1, le=10_000)) -> JSONResponse:
    """Read recent capture-endpoint hits for a slug. Gated on the PRIMARY
    api_token only (require_write_token) — the read-only reviewer/demo token
    must NOT be able to read raw captured request bodies/headers, which can
    contain other sources' secrets. Random folks on the internet can't
    enumerate someone else's traffic either."""
    from .capture import _log_path
    path = _log_path(slug)
    if not path.exists():
        return JSONResponse({"slug": slug, "rows": []})
    import json as _json
    from collections import deque

    # Read only the requested tail into memory (bounded by `tail`, not the
    # whole file) so a large append-only capture log can't be turned into a
    # memory-exhaustion read. Off the event loop: the deque still SCANS the
    # whole file, and an ever-growing capture log would otherwise block every
    # other request for the duration.
    def _read_tail() -> deque[str]:
        # The exists() check above ran on the event loop; log rotation or
        # cleanup can remove the file before this thread opens it. Missing
        # then == missing now: same empty result, not a 500.
        try:
            with path.open("r", encoding="utf-8") as f:
                return deque(f, maxlen=tail)
        except FileNotFoundError:
            return deque()

    last_lines = await asyncio.to_thread(_read_tail)
    # Tolerate corrupt/partial JSONL — older log lines from a crashed
    # write can have a truncated trailing line. Skip rather than 500.
    rows: list[dict] = []
    skipped = 0
    for line in last_lines:
        try: rows.append(_json.loads(line))
        except _json.JSONDecodeError: skipped += 1
    out: dict[str, Any] = {"slug": slug, "count": len(rows), "rows": rows}
    if skipped:
        out["skipped_malformed"] = skipped
    return JSONResponse(out)


@app.get("/api/forecast", dependencies=[Depends(require_token)])
async def get_forecast(
    lat: float | None = None, lon: float | None = None,
    source: str | None = Query(None, pattern="^(open-meteo|twc)$"),
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Forecast. Default: 7-day Open-Meteo (free, no key). source=twc asks
    for The Weather Company's 5-day (needs the WU_API_KEY secret — free for
    PWS owners); ANY TWC failure falls back to Open-Meteo for this response
    only, marked with fallback_from so the app can label the strip. The
    preference itself lives in the app and is never flipped here."""
    flat = lat if lat is not None else settings.forecast_lat
    flon = lon if lon is not None else settings.forecast_lon
    if flat is None or flon is None:
        # Fallback: use the first device's known lat/lon if available
        devs = await db.list_devices()
        for d in devs:
            info = d.get("info") or {}
            coords = (info.get("coords") or {}).get("coords") or {}
            if "lat" in coords and "lon" in coords:
                flat, flon = coords["lat"], coords["lon"]
                break
    if flat is None or flon is None:
        raise HTTPException(status_code=400, detail="no lat/lon available; pass ?lat=&lon=")
    # The device-info fallback pulls from a JSON blob a custom ingest source
    # controls — coords stored as STRINGS would make the range comparison
    # below raise TypeError, i.e. a bare 500 for a bad-data condition.
    try:
        flat, flon = float(flat), float(flon)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="device coordinates are not numeric")
    # Same range check as put_device_location. Open-Meteo answers an
    # out-of-range coordinate with a 400, which used to surface here as a bare
    # 500 — the app then showed "server error" for what is a bad request.
    if not (-90.0 <= flat <= 90.0) or not (-180.0 <= flon <= 180.0):
        raise HTTPException(status_code=400, detail="lat/lon out of range")
    params = {
        "latitude": flat,
        "longitude": flon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,wind_direction_10m_dominant",
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": 7,
    }
    fallback_from: str | None = None
    if source == "twc":
        from . import forecast_twc
        wu_key = await effective_wu_key()
        if wu_key:
            try:
                return JSONResponse(await forecast_twc.fetch(
                    flat, flon, wu_key))
            except Exception as e:
                # Dead key (WU deactivates them when a station stops
                # uploading), WU outage, transform surprise — all routine.
                # Never log the exception repr: the key rides the URL.
                log.warning("TWC forecast failed (%s); falling back to "
                            "Open-Meteo", type(e).__name__)
                fallback_from = "twc"
        else:
            fallback_from = "twc"          # asked for TWC, no key configured
    # A third-party API that times out, 500s or returns an HTML error page is
    # routine, not a bug in this server — report it as an upstream failure so
    # the app can say "forecast unavailable" instead of "server error".
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
        body = r.json()
        if _is_limited_read(authorization):
            # Guests get the forecast (the family-sharing strip needs it)
            # but not the operator's home location: Open-Meteo echoes back
            # the grid-snapped lat/lon it was asked for, which on the
            # no-args call is the first device's stored coordinates — the
            # exact fields _strip_device_pii hides on /api/devices
            # (CODE_REVIEW_R5 R5-03 / R3-06).
            for k in ("latitude", "longitude", "elevation"):
                body.pop(k, None)
        body["source"] = "open-meteo"
        # Always present so the client decodes one shape from both sources.
        # Empty because Open-Meteo has no written forecast to give — only TWC
        # ships prose, which is why the app hides the card rather than
        # inventing one (see forecast_twc.transform).
        body["narrative"] = []
        if fallback_from:
            body["fallback_from"] = fallback_from
        return JSONResponse(body)
    except httpx.HTTPError as e:
        log.warning("forecast upstream failed: %s", e)
        raise HTTPException(status_code=502, detail="forecast upstream unavailable")
    except ValueError:
        log.warning("forecast upstream returned non-JSON")
        raise HTTPException(status_code=502, detail="forecast upstream unavailable")
