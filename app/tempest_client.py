"""WeatherFlow Tempest REST client.

The Tempest hub uploads to WeatherFlow's cloud, which exposes:
  GET /swd/rest/stations                          → the token's stations
  GET /swd/rest/observations/station/{station_id} → latest derived obs

Auth is a Personal Access Token, free for station owners from the Tempest
web app (Settings → Data Authorizations). It rides as a QUERY PARAM, which
is the same credential-leak shape AmbientWeather and WeatherLink already
got bitten by — see TempestError.

Why the cloud API and not the hub's UDP broadcast: the hub does broadcast
JSON on port 50222 with no auth at all, which is lovely, but `obs_st`
reports rain as "accumulated in the previous minute" rather than a daily
total. Reconstructing `dailyrainin` from that means holding a counter
across restarts and resetting it at local midnight — exactly the class of
bug that produced maintenance.clean_cumulative_rain. The REST endpoint
hands us `precip_accum_local_day` already accumulated, so it is both less
code and less risk. UDP is still the right answer later for ~3s freshness.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx


log = logging.getLogger("tempest")

API_BASE = "https://swd.weatherflow.com/swd/rest"


class TempestError(Exception):
    """Tempest request failure with the token stripped out.

    The token travels as a query param and httpx's HTTPStatusError message
    embeds the full request URL, so a bare `log.exception` writes the
    credential into the logs in plaintext on every 401/403/5xx. This is the
    third time this shape has appeared in this backend (AmbientWeatherError,
    WeatherLinkError); keep the useful part, drop the query string.
    """

    @staticmethod
    def from_http(e: httpx.HTTPStatusError) -> "TempestError":
        r = e.response
        return TempestError(f"Tempest HTTP {r.status_code} on {r.request.url.path}")


class TempestClient:
    def __init__(self, token: str, timeout: float = 15.0) -> None:
        self._token = token
        self._timeout = timeout

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        url = f"{API_BASE}/{path}"
        query = {"token": self._token, **params}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, params=query)
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPStatusError as e:
            raise TempestError.from_http(e) from None
        except httpx.HTTPError as e:
            # Transport errors carry the URL too (and therefore the token).
            raise TempestError(f"Tempest request failed: {type(e).__name__}") from None
        except ValueError:
            raise TempestError("Tempest returned non-JSON") from None

        # The API answers 200 with a status envelope even for some failures,
        # so a 2xx alone is not success.
        status = (body.get("status") or {}) if isinstance(body, dict) else {}
        code = status.get("status_code")
        if code not in (None, 0):
            raise TempestError(
                f"Tempest status {code}: {status.get('status_message')}")
        return body if isinstance(body, dict) else {}

    async def stations(self) -> list[dict[str, Any]]:
        return (await self._get("stations")).get("stations") or []

    async def station_observation(self, station_id: int) -> dict[str, Any] | None:
        """Latest derived observation for a station, or None when the station
        has not reported (a hub that is unplugged answers SUCCESS with an
        empty `obs` list, which is not an error)."""
        body = await self._get(f"observations/station/{station_id}")
        obs = body.get("obs") or []
        return obs[-1] if obs else None


# ── unit conversion ───────────────────────────────────────────────────────
#
# The response is METRIC regardless of the owner's display preference. The
# `station_units` block in the same payload reports what the OWNER sees
# ("units_temp": "f"), NOT what the numbers are — Doren's station returns
# `air_temperature: 28.1` (°C) while advertising Fahrenheit. Reading
# station_units as the response's units is the obvious mistake here and it
# lands 50 °F off without anything looking broken.

_MPH_PER_MS = 2.2369362920544
_INHG_PER_MB = 1.0 / 33.8639
_IN_PER_MM = 1.0 / 25.4
# Lightning strike distance arrives in KILOMETRES, like everything else in
# this response regardless of what station_units claims.
_MI_PER_KM = 0.621371192237334


def _num(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def c_to_f(c: float | None) -> float | None:
    return None if c is None else round(c * 9.0 / 5.0 + 32.0, 2)


def ms_to_mph(ms: float | None) -> float | None:
    return None if ms is None else round(ms * _MPH_PER_MS, 2)


def mb_to_inhg(mb: float | None) -> float | None:
    return None if mb is None else round(mb * _INHG_PER_MB, 3)


def mm_to_in(mm: float | None) -> float | None:
    return None if mm is None else round(mm * _IN_PER_MM, 3)


def km_to_mi(km: float | None) -> float | None:
    """Strike distance to miles, matching the API-native convention the rest
    of this backend stores (°F, mph, inHg, inches) — see CLAUDE.md."""
    return None if km is None else round(km * _MI_PER_KM, 1)


def num(d: dict[str, Any], key: str) -> float | None:
    """Public alias — the transform reads fields through this so a NaN or a
    string in the payload becomes None rather than propagating."""
    return _num(d, key)
