# Zasder Weather API: the stable endpoints

This page is for developers building their own client, dashboard or
integration on a Zasder Weather server. It covers the endpoints we
promise to keep stable, what each one returns, the units, how missing
readings look, and how to check what a server supports.

The server has many more routes than the ones here (about 120 in all).
Those work, and the [README](README.md#api-selected-endpoints)
lists the useful ones, but they can change between releases. If you
build on something that is not on this page, pin your server version or
expect to follow along.

## What "stable" means

The endpoints below, and the fields listed for each, are stable:

- **Additive only.** Within a major version (all of 2.x), a stable field
  is never removed, never renamed, and never changes its JSON type. A
  number stays a number, a string stays a string, and a field that can
  be null is listed as nullable here from the start.
- **New fields can appear at any time**, on any endpoint, including
  inside stable objects. Ignore fields you do not know.
- **Fields not listed here are not promised**, even on a stable
  endpoint. Some exist for the apps' own screens (for example
  `source_health` and `arrival_24h` on `/api/devices`) and may change.
- **Open value sets grow.** An alert `kind`, a forecast `source` or a
  capability string can gain new values. Handle values you do not
  recognize instead of failing.
- **Key order, whitespace and number formatting are not part of the
  promise.** A whole-number reading can arrive as `40` or `40.0`; decode
  every measurement as a floating-point number.
- A breaking change would only come with a new major version (3.0), and
  the [CHANGELOG](CHANGELOG.md) would say so.

The promise is checked on every change: `tests/fixtures/stable_api.json`
lists each stable field and its type, and `tests/test_stable_api.py`
calls every endpoint below on a seeded database, with the owner token
and with a read-only guest token, and fails if a field goes missing or
changes type.

There are no versioned URLs and no version header. Use `/api/version`
(below) to find out what a server supports.

## Authentication

Every route on this page except `/api/version` needs a bearer token:

```
Authorization: Bearer <token>
```

Two kinds of token read the stable endpoints:

- **The API token** (`API_TOKEN` in the server's environment). Full
  access, including every write route. Keep it on devices you control.
- **Read-only guest tokens.** Minted per person in the iOS app (Settings,
  Share read-only access; they start with `zwg_` and can be revoked one
  at a time), or set by the operator in `GUEST_API_TOKENS`. A guest
  token reads every endpoint on this page and cannot write anything.
  If you are building something for other people, ask the station's
  owner for one of these rather than the API token.

Guest tokens see the weather but not the operator's private details. On
the stable endpoints that means:

- `/api/devices`: `location` is always `null`, and station coordinates
  are rounded to one decimal place (about 11 km).
- `/api/forecast`: the coordinate echo (`latitude`, `longitude`,
  `elevation`) is removed. Those were never stable fields.

Every stable field on this page is present for both kinds of token.

A missing or wrong token answers `401`. A guest token on a write route
answers `403` with a "read-only" message.

## Units

Readings are stored and served in the units weather-station APIs
commonly use, whatever the station itself measures in. Convert for
display on your side.

| Quantity | Unit |
|---|---|
| Temperature, dew point, feels like | °F |
| Wind speed and gust | mph |
| Wind direction | degrees from true north (0 to 360) |
| Pressure | inHg |
| Rain | inches |
| Solar radiation | W/m² |
| UV | UV index |
| Humidity, precipitation probability | percent (0 to 100) |
| PM2.5, PM10 | µg/m³ |
| CO₂ | ppm |
| Lightning distance | miles |
| Timestamps | milliseconds since the Unix epoch, UTC |

## Missing readings: absent is not zero

A station only reports what its sensors measure. A rain gauge with no
anemometer has no wind; a station without an air-quality sensor has no
PM2.5. The server never fills those in with zero.

- A reading the station does not report is either **`null` or missing
  from the object**. Which one depends on the endpoint and the field
  (below). Treat both the same way: "no reading". Never show either one
  as `0`.
- `0` is a real measurement: no rain fell, the UV index is zero at
  night, the wind is calm.
- On `/api/devices/{mac}/current` and in `lastData`, the core weather
  fields (temperature, humidity, pressure, wind, rain, solar, UV) are
  `null` when the source does not send them. Fields from add-on sensors
  (`pm25`, `pm10`, `co2`, `lightning_last_1hr`, `lightning_distance_mi`)
  are left out entirely.
- On `/api/devices/{mac}/history`, rows from short windows (6 hours or
  less) leave a field out when it had no value. Bucketed rows from
  longer windows carry the field with `null`.

Every field in the tables below marked *optional* can be missing, and
every field marked *nullable* can be `null`. A field marked neither is
always present with a real value.

## Checking a server: `/api/version`

`GET /api/version` needs no token. Call it first to learn what you are
talking to.

```json
{
  "version": "2.7.0",
  "latest": "2.7.0",
  "update_available": false,
  "checked_ms": 1792112400000,
  "enabled": true,
  "capabilities": ["stable-api"],
  "disk": {"total_bytes": 3170893824, "free_bytes": 2147483648, "used_pct": 32.3},
  "one_tap": null,
  "update_request": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `version` | string | The running server version, `major.minor.patch`. |
| `latest` | string, nullable | The newest published release, once the daily update check has run. `null` before the first check, or when the operator turned the check off. |
| `update_available` | boolean | `true` when `latest` is newer than `version`. |
| `checked_ms` | integer, nullable | When the update check last ran (epoch ms). |
| `capabilities` | array of strings | Features this server promises. See below. |

`enabled`, `disk`, `one_tap` and `update_request` drive the apps'
update and storage screens and are not part of the stable promise.

### Capabilities

`capabilities` is how a client checks for a feature without comparing
version numbers. Each string is a feature the server supports. Like the
stable fields, it is additive: once a release ships a capability, every
later release in the same major version keeps it.

| Capability | Meaning |
|---|---|
| `stable-api` | Everything on this page is served as described and kept to the promise above. |

Servers older than 2.7 have no `capabilities` field at all. They serve
the same endpoints and fields, but without the promise; treat a missing
field as "unknown" and fall back on `version`.

New capabilities are added here, in this table, in the release that
ships them. Ignore strings you do not recognize.

## The stable endpoints

| Method | Path | What it returns |
|---|---|---|
| GET | `/api/version` | Server version and capabilities (no token). |
| GET | `/api/devices` | Every station, with its latest reading. |
| GET | `/api/devices/{mac}/current` | One station's current conditions. |
| GET | `/api/devices/{mac}/history` | One station's readings over a time window. |
| GET | `/api/forecast` | A daily forecast for the station's location. |
| GET | `/api/alerts/recent` | The alerts the server has sent, newest first. |

`{mac}` is the station's identifier from `/api/devices`. Most are a
hardware address in the form `AA:BB:CC:DD:EE:FF`; the server also
accepts it in lowercase or without colons (`aabbccddeeff`). A few
sources use another identifier, which is passed through as is. Encode
the value for a URL if it contains anything other than letters, digits
and colons.

### `GET /api/devices`

Every station the server knows, in the order they were first seen.

```json
[
  {
    "mac": "AA:BB:CC:DD:EE:01",
    "name": "Garden",
    "display_name": "Garden",
    "source_name": "Back yard",
    "location": "Home",
    "lastSeen": 1792115494000,
    "lastData": {
      "dateutc": 1792115494000,
      "tempf": 73.0,
      "humidity": 40,
      "baromrelin": 29.92,
      "windspeedmph": 5.0,
      "dailyrainin": 0.1,
      "uv": 3
    },
    "info": {
      "coords": {"location": "Home", "coords": {"lat": 33.3004, "lon": -111.9378}}
    }
  }
]
```

(`lastData` trimmed; it carries the same fields as `current` below.)

| Field | Type | Meaning |
|---|---|---|
| `mac` | string | The station's identifier. Use it in the per-station URLs. |
| `name` | string | The name to show: the owner's rename if there is one, otherwise the name the station reports. |
| `display_name` | string, nullable | The owner's rename alone, `null` when there is none. |
| `source_name` | string, nullable | The name the station itself reports. |
| `location` | string, nullable | A free-text place label. Always `null` for guest tokens. |
| `lastSeen` | integer, nullable | When the newest reading arrived (epoch ms). `null` for a station that has never reported. |
| `lastData` | object, nullable | The newest reading, same fields as `/api/devices/{mac}/current`. Use `current` when you want the fullest picture: it merges the last few minutes of readings from every source feeding the station. |
| `info` | object | Station details. |
| `info.coords` | object, optional | Present when the station has a location. `info.coords.coords` holds `lat` and `lon` (numbers, degrees). Rounded to one decimal place for guest tokens. |

### `GET /api/devices/{mac}/current`

The station's current conditions. The server combines the newest
non-empty value of each field from the last five minutes of readings,
so a station fed by more than one source (a console and a separate
sensor relay, say) reports everything it has. `dateutc` is the time of
the newest reading. Rain totals the source does not send (daily,
weekly, monthly, yearly) are filled in from the stored history.

```json
{
  "dateutc": 1792115494000,
  "tempf": 73.0,
  "feelsLike": 73.0,
  "dewPoint": 45.0,
  "humidity": 40,
  "tempinf": 72.0,
  "humidityin": 35,
  "baromrelin": 29.92,
  "baromabsin": 28.71,
  "windspeedmph": 5.0,
  "windgustmph": 9.0,
  "maxdailygust": 9.0,
  "winddir": 180,
  "hourlyrainin": 0.0,
  "eventrainin": 0.1,
  "dailyrainin": 0.1,
  "weeklyrainin": 0.2,
  "monthlyrainin": 0.5,
  "yearlyrainin": 3.2,
  "uv": 3,
  "solarradiation": 400.5,
  "pm25": 8.0,
  "pm10": 12.0,
  "co2": 420,
  "lightning_last_1hr": 2,
  "lightning_distance_mi": 6.0
}
```

Answers `404` when the station has no readings.

| Field | Type | Meaning |
|---|---|---|
| `dateutc` | integer | Time of the newest reading (epoch ms). Always present. |
| `tempf` | number, nullable, optional | Outdoor temperature, °F. |
| `feelsLike` | number, nullable, optional | Feels-like temperature (heat index or wind chill), °F. |
| `dewPoint` | number, nullable, optional | Outdoor dew point, °F. |
| `humidity` | number, nullable, optional | Outdoor relative humidity, %. |
| `tempinf` | number, nullable, optional | Indoor temperature, °F. |
| `humidityin` | number, nullable, optional | Indoor relative humidity, %. |
| `baromrelin` | number, nullable, optional | Sea-level (relative) pressure, inHg. |
| `baromabsin` | number, nullable, optional | Station (absolute) pressure, inHg. |
| `windspeedmph` | number, nullable, optional | Wind speed, mph. |
| `windgustmph` | number, nullable, optional | Wind gust, mph. |
| `maxdailygust` | number, nullable, optional | Highest gust today, mph. |
| `winddir` | number, nullable, optional | Wind direction, degrees from north. |
| `hourlyrainin` | number, nullable, optional | Rain in the last hour, in. |
| `eventrainin` | number, nullable, optional | Rain in the current rain event, in. |
| `dailyrainin` | number, nullable, optional | Rain since local midnight, in. |
| `weeklyrainin` | number, nullable, optional | Rain this week, in. |
| `monthlyrainin` | number, nullable, optional | Rain this month, in. |
| `yearlyrainin` | number, nullable, optional | Rain this calendar year, in. |
| `totalrainin` | number, nullable, optional | The gauge's lifetime counter, in. Only for gauges that report a running total; on those, `yearlyrainin` is worked out from it. |
| `uv` | number, nullable, optional | UV index. |
| `solarradiation` | number, nullable, optional | Solar radiation, W/m². |
| `pm25` | number, nullable, optional | PM2.5, µg/m³ (air-quality sensor). |
| `pm10` | number, nullable, optional | PM10, µg/m³ (air-quality sensor). |
| `co2` | number, nullable, optional | CO₂, ppm (air-quality sensor). |
| `lightning_last_1hr` | number, nullable, optional | Lightning strikes in the last hour (a whole number). |
| `lightning_distance_mi` | number, nullable, optional | Distance to the most recent strike, mi. |

Stations with extra channels (more temperature/humidity probes, soil,
leak, leaf wetness, batteries) also report fields such as `temp1f`,
`soilhum1` or `leak1`, named the way Ambient Weather names them. They
are served but are not yet part of the stable promise. Readings from a
read-only guest token never include `_source`, the raw payload kept for
the owner's debugging; it is not stable either.

### `GET /api/devices/{mac}/history`

A station's readings over a window of time, oldest first.

| Parameter | Default | Meaning |
|---|---|---|
| `hours` | 24 | Window length, 1 to 745 hours (a 31-day month plus one hour for a daylight-saving change). |
| `end_ms` | now | Window end (epoch ms). Use it to page back through older history. |
| `limit` | 2000 | Most rows to return, 1 to 10000. On a short window that has more readings than this, the newest are kept. |

Windows of 6 hours or less return every stored reading. Longer windows
are averaged into buckets so a chart gets a manageable number of
points: 1 minute up to a day, 5 minutes up to 3 days, 15 minutes up to
a week, and 1 hour beyond that. A bucketed row's `dateutc` is the
middle of its bucket, and it also carries the lowest and highest value
seen in the bucket for the main fields, so a chart can draw a range
band around the average.

```json
{
  "start": 1791942694885,
  "end": 1792115494885,
  "count": 1,
  "rows": [
    {
      "dateutc": 1792114350000,
      "tempf": 71.5,
      "tempf_min": 70.0,
      "tempf_max": 73.0,
      "humidity": 40.0,
      "baromrelin": 29.92,
      "windspeedmph": 5.0,
      "windgustmph": 9.0,
      "windgustmph_max": 9.0,
      "winddir": 180.0,
      "dailyrainin": 0.1,
      "pm10": null
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `start` | integer | Window start (epoch ms). |
| `end` | integer | Window end (epoch ms). |
| `count` | integer | Number of rows. |
| `rows` | array | The readings, oldest first. |

Each row has `dateutc` (integer, always present) and, when the station
measured them, these fields with the meanings and units given for
`current`: `tempf`, `feelsLike`, `dewPoint`, `humidity`, `tempinf`,
`humidityin`, `baromrelin`, `windspeedmph`, `windgustmph`, `winddir`,
`hourlyrainin`, `dailyrainin`, `yearlyrainin`, `uv`, `solarradiation`,
`lightning_last_1hr`, `lightning_distance_mi`, `pm25`, `pm10`, `co2`.
All are numbers, optional and nullable. Short-window rows also carry
the other `current` fields the station reported.

Bucketed rows add the range of the bucket (numbers, nullable, only on
windows longer than 6 hours): `tempf_min`, `tempf_max`, `humidity_min`,
`humidity_max`, `baromrelin_min`, `baromrelin_max`, `windspeedmph_max`,
`windgustmph_max`, `uv_max`, `solarradiation_max`. In a bucket,
`lightning_distance_mi` is the closest strike and `lightning_last_1hr`
the highest count; the rest are averages (wind direction is a proper
circular average).

An unknown station returns an empty `rows` list, not an error.

### `GET /api/forecast`

A daily forecast, in the same units as the readings.

| Parameter | Default | Meaning |
|---|---|---|
| `lat`, `lon` | the server's forecast location, then the first station with coordinates | Where to forecast for. |
| `source` | `open-meteo` | `open-meteo` (7 days, no key needed) or `twc` (The Weather Company, 5 days, needs a Weather Underground key on the server). |

If `twc` is asked for and fails (no key, an expired key, an outage),
the server answers with the Open-Meteo forecast instead and sets
`fallback_from` to `"twc"`.

```json
{
  "source": "open-meteo",
  "daily": {
    "time": ["2026-10-15", "2026-10-16"],
    "weather_code": [0, 3],
    "temperature_2m_max": [91.2, 89.0],
    "temperature_2m_min": [66.1, 64.8],
    "precipitation_probability_max": [0, 10],
    "wind_speed_10m_max": [9.8, 12.1],
    "wind_direction_10m_dominant": [210, 190]
  },
  "narrative": []
}
```

| Field | Type | Meaning |
|---|---|---|
| `source` | string | Who made the forecast: `open-meteo` or `twc`. |
| `fallback_from` | string, optional | Present when the source you asked for failed; names it. |
| `daily` | object | Parallel arrays, one entry per day, all the same length. |
| `daily.time` | array of strings | The local date of each day, `YYYY-MM-DD`. |
| `daily.weather_code` | array of integers (nullable) | WMO weather code for each day. |
| `daily.temperature_2m_max` | array of numbers (nullable) | High, °F. |
| `daily.temperature_2m_min` | array of numbers (nullable) | Low, °F. |
| `daily.precipitation_probability_max` | array of numbers (nullable) | Highest chance of precipitation, %. |
| `daily.wind_speed_10m_max` | array of numbers (nullable) | Strongest wind, mph. |
| `daily.wind_direction_10m_dominant` | array of numbers (nullable) | Prevailing wind direction, degrees. |
| `narrative` | array | The written forecast, in order, starting with the part of the day you are in. Empty for Open-Meteo, which has no written forecast. |
| `narrative[].name` | string | Heading, such as "Tonight" or "Tomorrow". |
| `narrative[].text` | string | The forecast text. |
| `narrative[].night` | boolean | `true` for a night period. |
| `narrative[].date` | string, optional | The date (`YYYY-MM-DD`) the period belongs to, matching an entry in `daily.time`. |

An entry in a `daily` array can be `null` when the forecaster has no
value for that day (The Weather Company drops today's high in the late
afternoon, for example).

The Open-Meteo response is passed through, so it also carries
Open-Meteo's own fields (`current`, `daily_units`, `timezone` and others).
Those are Open-Meteo's to change and are not part of this promise.

Answers `400` when there is no location to forecast for and `502` when
the forecast service cannot be reached.

### `GET /api/alerts/recent`

The alerts the server has handled, newest first: threshold rules,
station-down notices, lightning, weather service alerts, storm and rain
notices, reports. It is the same list on every device, so a client can
show what a dismissed notification said.

| Parameter | Default | Meaning |
|---|---|---|
| `limit` | 50 | How many, 1 to 200. The server keeps about the last 200. |

```json
{
  "alerts": [
    {
      "id": 412,
      "ts_ms": 1792115400000,
      "kind": "rule",
      "mac": "AA:BB:CC:DD:EE:01",
      "title": "Hot",
      "body": "tempf above 70",
      "delivered": 0,
      "severity": "info",
      "rule_id": 7,
      "report_id": null
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `alerts` | array | The alerts, newest first. |
| `id` | integer | Increases with every alert. |
| `ts_ms` | integer | When it was handled (epoch ms). |
| `kind` | string | What kind of alert, for example `rule`, `lightning`, `nws`, `rain_start` or `outlook`. New kinds are added over time. |
| `mac` | string, nullable | The station it is about, `null` for server-wide alerts. |
| `title` | string | The notification title. |
| `body` | string, nullable | The notification text. |
| `delivered` | integer | `1` when it was sent, `0` when it was held back (quiet hours, a muted kind) and only recorded. |
| `severity` | string, nullable | `warning`, `major`, `watch` or `info`, most urgent first. `null` only on alerts recorded by very old versions. |
| `rule_id` | integer, nullable | The threshold rule that fired, for `rule` alerts. |
| `report_id` | integer, nullable | The stored report the alert announced (a morning report, outlook, storm summary or sky note), the id `GET /api/reports/{id}` serves. `null` for other kinds, for a report the server could not store, and for alerts recorded before 2.7. |

## Example

```sh
TOKEN=zwg_...                       # a read-only guest token
SERVER=https://weather.example.com

curl -s "$SERVER/api/version"
curl -s -H "Authorization: Bearer $TOKEN" "$SERVER/api/devices"
curl -s -H "Authorization: Bearer $TOKEN" \
  "$SERVER/api/devices/AA:BB:CC:DD:EE:01/history?hours=6"
```

## Questions and requests

Open an issue on the
[GitHub repository](https://github.com/volneydouglas/zasder-weather-backend/issues).
If you need an endpoint or field that is not on this page, say what you
are building; that is how the stable list grows.
