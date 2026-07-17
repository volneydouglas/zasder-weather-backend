import html as _html
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .limits import BodySizeLimitMiddleware
from .updates import UpdateChecker
from .version import __version__

from . import db
from .alerts import AlertMonitor
from .ambient_client import AmbientWeatherClient
from .capture import router as capture_router
from .config import settings, tokens_match
from .discovery import router as discovery_router
from .ingest import router as ingest_router
from .poller import Poller
from .weatherlink_client import WeatherLinkClient
from .weatherlink_poller import WeatherlinkPoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    app.state.started_at = time.time()
    # AmbientWeather poller only starts when both keys are set. AcuRite-only
    # deploys leave them unset and rely entirely on /ingest/custom.
    client = None
    poller = None
    if settings.aw_configured:
        client = AmbientWeatherClient(settings.aw_application_key,  # type: ignore[arg-type]
                                      settings.aw_api_key)          # type: ignore[arg-type]
        poller = Poller(client)
        await poller.start()
        log.info("AmbientWeather poller started")
    else:
        log.info("AmbientWeather keys not set — poller disabled "
                 "(custom ingest endpoints are still active)")
    app.state.client = client
    app.state.poller = poller

    # WeatherLink v2 cloud poller — independent from AWN. Same lifespan
    # gating (start only if all 3 creds set; explicit log if disabled
    # so it's obvious in deploy logs whether the secrets landed).
    wl_client = None
    wl_poller = None
    if settings.weatherlink_configured:
        wl_client = WeatherLinkClient(settings.weatherlink_api_key,  # type: ignore[arg-type]
                                      settings.weatherlink_api_secret)  # type: ignore[arg-type]
        wl_poller = WeatherlinkPoller(wl_client,
                                      settings.weatherlink_station_id,  # type: ignore[arg-type]
                                      settings.weatherlink_poll_interval_seconds)
        await wl_poller.start()
        log.info("WeatherLink poller started (station_id=%s)",
                 settings.weatherlink_station_id)
    else:
        log.info("WeatherLink not configured — skipping Davis cloud poller")
    app.state.wl_client = wl_client
    app.state.wl_poller = wl_poller

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

    # Daily "is there a newer release?" check → status-page banner + /api/version.
    update_checker = UpdateChecker(app)
    update_checker.start()
    app.state.update_checker = update_checker

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
        if poller is not None: await poller.stop()
        if client is not None: await client.aclose()
        if wl_poller is not None: await wl_poller.stop()
        if wl_client is not None: await wl_client.aclose()
        if alert_monitor is not None: await alert_monitor.stop()
        await update_checker.stop()
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


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """READ-allowing dep: accepts api_token OR reviewer_api_token. Use on GETs."""
    if not tokens_match(_extract_bearer(authorization), settings.valid_api_tokens):
        raise HTTPException(status_code=401, detail="invalid token")


def require_write_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """MUTATING dep: only api_token. The reviewer/demo token is read-only,
    so it can't alter user state if the reviewer hits a write route. Use on
    every POST/PUT/PATCH/DELETE under /api/*."""
    if not tokens_match(_extract_bearer(authorization), settings.write_tokens):
        raise HTTPException(status_code=401, detail="invalid token")


def _is_reviewer(authorization: str | None) -> bool:
    """True when the presented bearer is the read-only reviewer/demo token."""
    return tokens_match(_extract_bearer(authorization), settings.reviewer_api_token)


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
        obs = await db.latest_observation(d["mac"])
        if obs and obs.get("tempf") is not None:
            obs_ms = obs.get("dateutc")
            if obs_ms and (latest_temp is None or obs_ms > latest_temp["ts_ms"]):
                latest_temp = {
                    "tempf": float(obs["tempf"]),
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
        dashboard_html = await _build_public_dashboard(devices, now_ms)

    return HTMLResponse(_render_status_html(
        rows, total_obs, uptime, latest_temp, now_ms, update_info,
        dashboard_html=dashboard_html,
        app_url=settings.public_dashboard_app_url))


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
                v = r.get(key)
                if t is not None and v is not None:
                    pts.append((int(t), float(v)))
            series[key] = pts
        # Paired (direction, speed) samples for the wind rose.
        wind_samples = []
        for r in rows:
            wd, ws = r.get("winddir"), r.get("windspeedmph")
            if wd is not None and ws is not None:
                wind_samples.append((float(wd), float(ws)))
        try:
            recs = await _cached_records(mac)
        except Exception as e:
            log.warning("records failed for %s: %s", mac, e)
            recs = None
        stations.append({"name": d.get("name") or mac, "obs": obs,
                         "series": series, "wind_samples": wind_samples,
                         "records": recs})
    return pd.render_dashboard(stations, fields, tz_name=settings.timezone)


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
                        dashboard_html: str = "",
                        app_url: str = "") -> str:
    started = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    # Public dashboard on ⇒ swap the app screenshots for the live charts + an
    # App Store link, add its CSS, and auto-refresh the page.
    from . import public_dashboard as _pd
    if dashboard_html:
        dashboard_css = _pd.DASHBOARD_CSS
        refresh_meta = '<meta http-equiv="refresh" content="120">'
        _cta = (f'<a href="{_html.escape(app_url, quote=True)}" target="_blank" '
                f'rel="noopener">Get the iOS app ↗</a>'
                if app_url else "")
        hero_html = (
            f'<div class="app-cta">{_cta}'
            f'<span class="sub">Live conditions below · same data in the app</span></div>'
            f'{dashboard_html}'
        )
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


@app.get("/api/devices", dependencies=[Depends(require_token)])
async def get_devices() -> JSONResponse:
    return JSONResponse(await db.list_devices())


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
        "devices": dev_list,
    }


@app.get("/api/alerts", dependencies=[Depends(require_token)])
async def get_alerts(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    state = await _alerts_state()
    if _is_reviewer(authorization):
        # The read-only reviewer/demo token gets the alerts UI state but not
        # the SMTP transport identifiers (host/username/from reveal the
        # maintainer's mail infrastructure; password was already write-only).
        for k in ("smtp_host", "smtp_username", "smtp_from"):
            if state.get(k):
                state[k] = "(hidden)"
    return JSONResponse(state)


@app.put("/api/alerts", dependencies=[Depends(require_write_token)])
async def put_alerts(body: AlertPrefsIn) -> JSONResponse:
    fields: dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.default_threshold_minutes is not None:
        fields["default_threshold_min"] = body.default_threshold_minutes
    if body.repeat_hours is not None:
        fields["repeat_hours"] = body.repeat_hours
    if body.recipients is not None:
        clean = [r.strip() for r in body.recipients if r.strip()]
        for r in clean:
            if "@" not in r or " " in r:
                raise HTTPException(status_code=400, detail=f"invalid recipient: {r!r}")
        # Empty list clears the override → falls back to env recipients.
        fields["recipients"] = ",".join(clean) if clean else None
    # SMTP transport (DB over env). Empty string clears → env fallback.
    if body.smtp_host is not None:     fields["smtp_host"] = body.smtp_host.strip() or None
    if body.smtp_port is not None:     fields["smtp_port"] = body.smtp_port
    if body.smtp_username is not None: fields["smtp_username"] = body.smtp_username.strip() or None
    if body.smtp_password is not None: fields["smtp_password"] = body.smtp_password or None
    if body.smtp_from is not None:     fields["smtp_from"] = body.smtp_from.strip() or None
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


@app.post("/api/alerts/test", dependencies=[Depends(require_write_token)])
async def test_alert() -> JSONResponse:
    """Send a one-off test email to the current recipients — lets the app's
    setup screen verify delivery end to end."""
    import asyncio as _asyncio
    from .alerts import effective_config, _send_sync
    cfg = await effective_config()
    if not cfg.transport_configured:
        raise HTTPException(status_code=400,
                            detail="SMTP transport not configured (set SMTP_HOST + creds as secrets)")
    if not cfg.recipients:
        raise HTTPException(status_code=400, detail="no recipients configured")
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
    token: str = Field(min_length=8)
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


@app.get("/api/devices/{mac}/current", dependencies=[Depends(require_token)])
async def get_current(mac: str) -> JSONResponse:
    obs = await db.latest_observation(mac)
    if not obs:
        raise HTTPException(status_code=404, detail="no data for device")
    # Rain rollup enrichment: if the source posts yearlyrainin (SDR path)
    # but not the bucketed values (daily/hourly/etc.), compute them from
    # historical yearlyrainin deltas at local-time period boundaries.
    # AWN-sourced rows ship pre-computed rollups already and the conditional
    # leaves those untouched.
    if obs.get("yearlyrainin") is not None and any(
        obs.get(k) is None for k in
        ("dailyrainin", "hourlyrainin", "weeklyrainin", "monthlyrainin")
    ):
        try:
            rollups = await db.rain_rollups(mac, settings.timezone)
        except Exception as e:
            log.warning("rain_rollups failed for %s: %s", mac, e)
            rollups = {}
        for k, v in (("dailyrainin",   rollups.get("daily_in")),
                      ("hourlyrainin",  rollups.get("hourly_in")),
                      ("weeklyrainin",  rollups.get("weekly_in")),
                      ("monthlyrainin", rollups.get("monthly_in"))):
            if obs.get(k) is None and v is not None:
                obs[k] = v
    return JSONResponse(obs)


@app.get("/api/devices/{mac}/history", dependencies=[Depends(require_token)])
async def get_history(
    mac: str,
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(2000, ge=1, le=10_000),
) -> JSONResponse:
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    rows = await db.history(mac, start, end, limit=limit)
    return JSONResponse({"start": start, "end": end, "count": len(rows), "rows": rows})


@app.get("/api/devices/{mac}/summary", dependencies=[Depends(require_token)])
async def get_summary(
    mac: str,
    field: str = Query("tempf"),
    hours: int = Query(24, ge=1, le=24 * 30),
) -> JSONResponse:
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


async def _cached_records(mac: str) -> dict:
    now = time.time()
    hit = _RECORDS_CACHE.get(mac)
    if hit and now - hit[0] < _RECORDS_TTL_S:
        return hit[1]
    data = await db.records(mac, settings.timezone)
    _RECORDS_CACHE[mac] = (now, data)
    return data


@app.get("/api/devices/{mac}/records", dependencies=[Depends(require_token)])
async def get_records(mac: str) -> JSONResponse:
    """All-time / yearly / monthly / today highs & lows per metric, with the
    local time each record was set. Cached 15 min per device."""
    return JSONResponse(await _cached_records(mac))


from typing import Any  # noqa: E402

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
    # memory-exhaustion read.
    with path.open("r", encoding="utf-8") as f:
        last_lines = deque(f, maxlen=tail)
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
    lat: float | None = None, lon: float | None = None
) -> JSONResponse:
    """7-day forecast via Open-Meteo (free, no key)."""
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
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
    return JSONResponse(r.json())
