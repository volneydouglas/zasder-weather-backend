import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from . import db
from .ambient_client import AmbientWeatherClient
from .capture import router as capture_router
from .config import settings
from .ingest import router as ingest_router
from .poller import Poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    client = AmbientWeatherClient(settings.aw_application_key, settings.aw_api_key)
    poller = Poller(client)
    await poller.start()
    app.state.client = client
    app.state.poller = poller
    app.state.started_at = time.time()
    try:
        yield
    finally:
        await poller.stop()
        await client.aclose()


app = FastAPI(title="zasder weather", lifespan=lifespan)
app.include_router(capture_router)
app.include_router(ingest_router)


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid token")
    presented = authorization.removeprefix("Bearer ")
    if presented not in settings.valid_api_tokens:
        raise HTTPException(status_code=401, detail="invalid token")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
        rows.append({
            "name": d.get("name") or d["mac"],
            "location": d.get("location") or "—",
            "mac": d["mac"],
            "count": n,
            "last_seen": last_seen_label,
            "last_seen_class": last_seen_class,
        })

    uptime = time.time() - getattr(app.state, "started_at", time.time())
    return HTMLResponse(_render_status_html(rows, total_obs, uptime))


def _humanize_age(seconds: float) -> str:
    if seconds < 60:    return f"{int(seconds)}s ago"
    if seconds < 3600:  return f"{int(seconds // 60)}m ago"
    if seconds < 86400: return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _render_status_html(rows: list[dict], total_obs: int, uptime_s: float) -> str:
    started = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    rows_html = "\n".join(
        f'<tr><td>{r["name"]}</td><td class="muted">{r["location"]}</td>'
        f'<td class="mono">{r["mac"]}</td><td class="num">{r["count"]:,}</td>'
        f'<td class="age {r["last_seen_class"]}">{r["last_seen"]}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="5" class="muted">No devices yet — waiting for first poll.</td></tr>'
    days = int(uptime_s // 86400)
    hours = int((uptime_s % 86400) // 3600)
    mins = int((uptime_s % 3600) // 60)
    uptime_label = f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Zasder Weather — Status</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ background: #0d0f12; color: #fff; font-family: system-ui, -apple-system, sans-serif;
            margin: 0; padding: 32px 16px; line-height: 1.4; }}
    .wrap {{ max-width: 720px; margin: 0 auto; }}
    h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.2px; }}
    .sub {{ font-size: 12px; color: rgba(255,255,255,0.55); margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 24px; }}
    .stat {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06);
              border-radius: 10px; padding: 12px; }}
    .stat .k {{ font-size: 9px; font-weight: 800; letter-spacing: 1.2px;
                 color: rgba(255,255,255,0.55); text-transform: uppercase; }}
    .stat .v {{ font-size: 22px; font-weight: 300; margin-top: 4px;
                 font-variant-numeric: tabular-nums; }}
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
    footer {{ margin-top: 24px; font-size: 10px; color: rgba(255,255,255,0.35); }}
    a {{ color: oklch(70% 0.14 245); text-decoration: none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Zasder Weather</h1>
    <div class="sub">Read-only status — no auth required. The iOS app reads protected endpoints under <code>/api</code>.</div>
    <div class="grid">
      <div class="stat"><div class="k">Status</div><div class="v">Up</div></div>
      <div class="stat"><div class="k">Devices</div><div class="v">{len(rows)}</div></div>
      <div class="stat"><div class="k">Observations</div><div class="v">{total_obs:,}</div></div>
    </div>
    <table>
      <thead><tr><th>Device</th><th>Location</th><th>MAC</th><th>Rows</th><th>Last seen</th></tr></thead>
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


@app.get("/api/devices/{mac}/current", dependencies=[Depends(require_token)])
async def get_current(mac: str) -> JSONResponse:
    obs = await db.latest_observation(mac)
    if not obs:
        raise HTTPException(status_code=404, detail="no data for device")
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


@app.get("/api/captures/{slug}", dependencies=[Depends(require_token)])
async def get_captures(slug: str, tail: int = Query(50, ge=1, le=10_000)) -> JSONResponse:
    """Read recent capture-endpoint hits for a slug. Token-gated so random
    folks on the internet can't enumerate someone else's traffic."""
    from .capture import _log_path
    path = _log_path(slug)
    if not path.exists():
        return JSONResponse({"slug": slug, "rows": []})
    import json as _json
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()
    rows = [_json.loads(l) for l in lines[-tail:]]
    return JSONResponse({"slug": slug, "count": len(rows), "rows": rows})


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
