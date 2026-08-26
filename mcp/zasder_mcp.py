#!/usr/bin/env python3
"""Zasder Weather MCP server (1.8, Pillar B) — your backyard, readable
by your AI assistant.

A thin, READ-ONLY wrapper over the backend's token-gated REST API: point
any MCP client (Claude Desktop, Claude Code, or anything speaking the
protocol) at this script and it can answer "what did my station record
during last night's storm?", "when was my first frost last year?",
"which alerts fired today?" — against YOUR server, with YOUR token,
touching nothing.

Setup:
    pip install "mcp[cli]" httpx
    export ZASDER_URL=https://your-app.fly.dev
    export ZASDER_TOKEN=<your API token>
    python zasder_mcp.py            # stdio transport

Claude Desktop config snippet:
    { "mcpServers": { "zasder-weather": {
        "command": "python", "args": ["/path/to/zasder_mcp.py"],
        "env": { "ZASDER_URL": "…", "ZASDER_TOKEN": "…" } } } }

Read-only by construction: only GET endpoints are wrapped, and the
token you provide can itself be a read-only share token.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

# SDK 2.x renamed FastMCP to MCPServer (2026); support both so a plain
# `pip install "mcp[cli]"` works whichever generation it resolves to.
try:                                    # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:                     # mcp 1.x (or a future rename —
    from mcp.server.fastmcp import FastMCP  # ImportError covers both)

BASE = os.environ.get("ZASDER_URL", "").rstrip("/")
TOKEN = os.environ.get("ZASDER_TOKEN", "")

mcp = FastMCP("zasder-weather")


def _get(path: str, params: dict | None = None,
         timeout: float = 20.0) -> Any:
    if not BASE or not TOKEN:
        raise RuntimeError("Set ZASDER_URL and ZASDER_TOKEN")
    r = httpx.get(f"{BASE}{path}", params=params, timeout=timeout,
                  headers={"Authorization": f"Bearer {TOKEN}"})
    r.raise_for_status()
    return r.json()


def _q(mac: str) -> str:
    """Percent-encode the mac path segment — a '/' or '?' in the value
    would land the request on a different route (R7 MCP finding 3)."""
    from urllib.parse import quote
    return quote(str(mac), safe="")


@mcp.tool()
def list_stations() -> list[dict]:
    """The stations this server knows: name, id (mac), and freshness."""
    return _get("/api/devices")


@mcp.tool()
def current_conditions(mac: str) -> dict:
    """Latest readings for one station (API-native units: °F, mph, inHg,
    inches). Use list_stations first to find the mac."""
    return _get(f"/api/devices/{_q(mac)}/current")


@mcp.tool()
def derived_metrics(mac: str) -> dict:
    """Derived metrics for one station: wet bulb, Delta-T, frost point,
    fire-weather indices, density altitude, pressure tendency, and what
    the barometer thinks (Zambretti). Fields appear only when their
    inputs exist."""
    return _get(f"/api/devices/{_q(mac)}/derived")


@mcp.tool()
def history_summary(mac: str, field: str = "tempf", hours: int = 24) -> dict:
    """Min/max/latest for one field over the trailing window (≤720 h).
    Fields include tempf, humidity, windspeedmph, windgustmph,
    baromrelin, dailyrainin, solarradiation, uv, dewPoint."""
    # 720 is the backend's le= bound — clamping to more just trades a clean
    # clamp for a 422 (R7 MCP finding 2).
    return _get(f"/api/devices/{_q(mac)}/summary",
                {"field": field, "hours": max(1, min(int(hours), 720))})


@mcp.tool()
def records(mac: str, period: str = "all") -> dict:
    """Highs and lows for a station: period = today | month | year | all
    ('all' returns every period). The backend always computes all four;
    the projection happens here — passing the param through looked like it
    worked while silently returning everything (R7 R8)."""
    if period not in ("today", "month", "year", "all"):
        raise ValueError("period must be today, month, year, or all")
    # 60s: a cold /records on a large archive can take >20s before the
    # daily rollups warm up.
    payload = _get(f"/api/devices/{_q(mac)}/records", timeout=60.0)
    if period == "all" or not isinstance(payload, dict):
        return payload
    periods = payload.get("periods")
    if isinstance(periods, dict) and period in periods:
        return {"mac": payload.get("mac"), "period": period,
                "records": periods[period]}
    return payload


@mcp.tool()
def recent_alerts(limit: int = 20) -> Any:
    """What fired recently — device-down, threshold rules, smart alerts,
    storm summaries — with timestamps."""
    return _get("/api/alerts/recent", {"limit": max(1, min(int(limit), 100))})


if __name__ == "__main__":
    mcp.run()
