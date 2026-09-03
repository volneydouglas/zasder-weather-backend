"""Ecowitt cloud REST client (api.ecowitt.net, API v3).

Every Ecowitt gateway uploads to ecowitt.net, which exposes (doc.ecowitt.net,
"Device Data API"):
  GET /api/v3/device/list       → the account's devices (mac, name, coords,
                                  stationtype = WiFi firmware string)
  GET /api/v3/device/real_time  → latest reading per metric for one MAC
  GET /api/v3/device/history    → per-metric time series, ≤ 1 day per call
                                  at 5-minute resolution (past 90 days)

Auth is an `application_key` + `api_key` pair from the ecowitt.net Private
Center. Both ride as QUERY PARAMS, which is the credential-leak shape
AmbientWeather, WeatherLink and Tempest already got bitten by — see
EcowittCloudError.

Why this exists next to /ingest/ecowitt (Path G): the gateway's own
"Customized" upload is plain HTTP and cannot reach an HTTPS-only host such
as a Fly deploy without a LAN forwarder. This poller reaches the same
readings through the vendor cloud from anywhere the backend runs. Same
station, two doors — a gateway pointed at BOTH would appear twice (the
local path keys the device by a synthetic EC:EC MAC, this one by the real
MAC), so users pick one.

Units: the API's defaults are already the API-native set this backend
stores (°F, inHg, mph, inches, W/m²), but they are requested EXPLICITLY
(UNIT_PARAMS) rather than trusted — the Tempest lesson was a payload whose
advertised units were not the payload's units.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx


log = logging.getLogger("ecowitt-cloud")

API_BASE = "https://api.ecowitt.net/api/v3"

# Unit ids from doc.ecowitt.net ("Getting Device Real-Time Data", request
# parameters). These ARE the documented defaults; sending them pins the
# contract so a default change upstream cannot silently re-unit the store.
#   temp_unitid: 1 = °C, 2 = °F
#   pressure_unitid: 3 = hPa, 4 = inHg, 5 = mmHg
#   wind_speed_unitid: 6 = m/s, 7 = km/h, 8 = knots, 9 = mph, 10 = BFT, 11 = fpm
#   rainfall_unitid: 12 = mm, 13 = in
#   solar_irradiance_unitid: 14 = lux, 15 = fc, 16 = W/m²
UNIT_PARAMS: dict[str, int] = {
    "temp_unitid": 2,
    "pressure_unitid": 4,
    "wind_speed_unitid": 9,
    "rainfall_unitid": 13,
    "solar_irradiance_unitid": 16,
}

# Groups the poller reads. history() requires call_back (the doc marks it
# mandatory there) and the transform reads exactly these; real_time asks
# for `all` so the battery and channel groups come too.
HISTORY_GROUPS = ("outdoor", "indoor", "solar_and_uvi", "rainfall",
                  "rainfall_piezo", "wind", "pressure")

# Result codes from doc.ecowitt.net "Common Error Codes", translated for the
# person reading /api/sources or the Integrations sheet. Anything else is
# reported by number.
_CODE_TEXT = {
    -1: "Ecowitt says the system is busy",
    40010: "invalid application key",
    40011: "invalid API key",
    40012: "unknown device MAC",
    40017: "missing application key",
    40018: "missing API key",
    45001: "over the request limit",
    48001: "these keys are not allowed to read this device",
}


class EcowittCloudError(Exception):
    """Ecowitt request failure with the keys stripped out.

    Both keys travel as query params and httpx's HTTPStatusError message
    embeds the full request URL, so a bare `log.exception` writes the
    credentials into the logs in plaintext. Fourth appearance of this shape
    in this backend (AmbientWeatherError, WeatherLinkError, TempestError);
    keep the useful part, drop the query string.
    """

    @staticmethod
    def from_http(e: httpx.HTTPStatusError) -> "EcowittCloudError":
        r = e.response
        return EcowittCloudError(
            f"Ecowitt HTTP {r.status_code} on {r.request.url.path}")


class EcowittCloudClient:
    def __init__(self, application_key: str, api_key: str,
                 timeout: float = 15.0) -> None:
        self._application_key = application_key
        self._api_key = api_key
        self._timeout = timeout

    def _scrub(self, text: str) -> str:
        """An upstream `msg` has never been seen to echo a key, but the
        error path is exactly where a credential must not be able to ride —
        mask the two values wherever they appear."""
        for k in (self._application_key, self._api_key):
            if k:
                text = text.replace(k, "<redacted>")
        return text

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        url = f"{API_BASE}/{path}"
        query = {"application_key": self._application_key,
                 "api_key": self._api_key, **params}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(url, params=query)
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPStatusError as e:
            raise EcowittCloudError.from_http(e) from None
        except httpx.HTTPError as e:
            # Transport errors carry the URL too (and therefore the keys).
            raise EcowittCloudError(
                f"Ecowitt request failed: {type(e).__name__}") from None
        except ValueError:
            raise EcowittCloudError("Ecowitt returned non-JSON") from None

        # The API answers 200 with a result envelope for every failure
        # (a bad key is HTTP 200 + code 40010), so a 2xx alone is nothing.
        if not isinstance(body, dict):
            raise EcowittCloudError("Ecowitt returned an unexpected body")
        code = body.get("code")
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            raise EcowittCloudError(
                f"Ecowitt returned no result code on {path}") from None
        if code_i != 0:
            text = _CODE_TEXT.get(code_i)
            msg = self._scrub(str(body.get("msg") or "")).strip()
            detail = text or msg or "unknown error"
            raise EcowittCloudError(f"Ecowitt code {code_i}: {detail}")
        return body

    async def list_devices(self) -> list[dict[str, Any]]:
        """Every device on the account, all pages. Weather stations are
        `type: 1`; cameras (`type: 2`) are returned too and filtered by the
        caller, since a camera has no MAC worth polling."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self._get("device/list", page=page, limit=50)
            data = body.get("data") or {}
            if not isinstance(data, dict):
                break
            items = data.get("list") or []
            out.extend(d for d in items if isinstance(d, dict))
            try:
                total_pages = int(data.get("totalPage") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages or not items:
                break
            page += 1
        return out

    async def real_time(self, mac: str) -> dict[str, Any]:
        """Latest reading per metric for one device — the `data` object
        of grouped leaves ({"time": "<epoch s>", "unit": "…", "value":
        "<str>"}). An empty dict means the station has not reported in
        the API's 2-hour window, which is a quiet station, not an error
        (the API answers code 0 with `data: []` there)."""
        body = await self._get("device/real_time", mac=mac, call_back="all",
                               **UNIT_PARAMS)
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def history(self, mac: str, start: datetime, end: datetime,
                      call_back: str = ",".join(HISTORY_GROUPS)) -> dict[str, Any]:
        """Per-metric series for one device between two instants. The doc
        limits one call to a complete day at 5-minute resolution (past 90
        days) and requires call_back; `start`/`end` are formatted the
        ISO-8601-with-a-space way the doc shows ("2023-12-10 12:00:00").
        Naive datetimes are sent as given; the API interprets them in the
        device's own time zone (`date_zone_id` on the list entry)."""
        fmt = "%Y-%m-%d %H:%M:%S"
        body = await self._get("device/history", mac=mac,
                               start_date=start.strftime(fmt),
                               end_date=end.strftime(fmt),
                               call_back=call_back, cycle_type="5min",
                               **UNIT_PARAMS)
        data = body.get("data")
        return data if isinstance(data, dict) else {}


# ── unit guards ──────────────────────────────────────────────────────────
#
# The poller asks for API-native units explicitly, and every leaf still
# carries its own `unit` string. These read that string and convert if it
# names anything else, so a default flip upstream (or a device that ignores
# the unitid params) lands in the store correctly rather than 50 °F off
# with nothing looking broken — the Tempest lesson, applied defensively.

_MPH_PER_MS = 2.2369362920544
_MPH_PER_KMH = 0.621371192237334
_MPH_PER_KNOT = 1.15077944802354
_INHG_PER_HPA = 1.0 / 33.8639
_INHG_PER_MMHG = 1.0 / 25.4
_IN_PER_MM = 1.0 / 25.4
_MI_PER_KM = 0.621371192237334


def to_float(v: Any) -> float | None:
    """The API's leaf values are STRINGS ("127.7"); a blank or a dash is a
    sensor that has nothing to say."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def native(kind: str, value: float | None, unit: Any) -> float | None:
    """Convert `value` (reported in `unit`) to the stored unit for `kind`.
    Unknown or blank unit strings are taken at face value as the requested
    native unit; a unit that cannot be converted (lux for solar) drops the
    reading rather than storing a number in the wrong scale."""
    if value is None:
        return None
    u = str(unit or "").strip().lower()
    if kind == "temp":
        # "ºF" / "℉" / "°F" vs "ºC" / "℃" / "°C" — match on the letter.
        if u.endswith("c") or u == "℃":
            return round(value * 9.0 / 5.0 + 32.0, 2)
        return value
    if kind == "pressure":
        if "hpa" in u or "mbar" in u or u == "mb":
            return round(value * _INHG_PER_HPA, 3)
        if "mmhg" in u:
            return round(value * _INHG_PER_MMHG, 3)
        return value
    if kind == "wind":
        if u == "m/s":
            return round(value * _MPH_PER_MS, 2)
        if "km/h" in u:
            return round(value * _MPH_PER_KMH, 2)
        if "knot" in u or u == "kn":
            return round(value * _MPH_PER_KNOT, 2)
        if u in ("bft", "fpm"):
            return None
        return value
    if kind == "rain":
        # "mm" and "mm/hr" both mean millimetres.
        if u.startswith("mm"):
            return round(value * _IN_PER_MM, 3)
        return value
    if kind == "solar":
        if "lux" in u or u == "fc":
            return None
        return value
    if kind == "distance":
        if u == "km":
            return round(value * _MI_PER_KM, 1)
        return value
    return value
