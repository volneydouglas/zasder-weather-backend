# WeeWX → Zasder Weather bridge

Already running [WeeWX](https://weewx.com)? This extension sends every
archive record to your Zasder Weather server as well, so the apps, alerts,
and history ride your existing station — whatever hardware WeeWX speaks to
(70+ station families).

WeeWX keeps doing everything it does today; the bridge only adds one
outbound POST per archive interval to your server's `/ingest/custom`.

## Install

WeeWX 5:

```sh
weectl extension install https://github.com/volneydouglas/zasder-weather-backend/releases/latest/download/weewx-zasder.zip
# or from a local checkout:
weectl extension install /path/to/weewx-bridge
```

WeeWX 4:

```sh
wee_extension --install /path/to/weewx-bridge
```

Then edit `weewx.conf`:

```ini
[StdRESTful]
    [[Zasder]]
        server_url = https://your-server.fly.dev
        ingest_token = <your ingest token>
        # optional:
        # station_name = Backyard Vantage   # display name on first sight
        # device_id = weewx                 # storage key; keep it stable
```

The ingest token is in the Zasder Weather app under **Settings → Data**
(or your server's `INGEST_TOKEN` secret). Restart WeeWX; the station
appears in the app within one archive interval.

## Notes

- Records are converted to US units before upload regardless of your
  WeeWX database's unit system — the server stores API-native units.
- Absent sensors are omitted, never sent as zero.
- `device_id` is the key your history is stored under. Changing it later
  starts a new station in the app, so pick one and keep it.
- Failures queue and retry through WeeWX's standard RESTful machinery; a
  down server never blocks WeeWX itself.
- `server_url` must be `https://` unless the host is on your own network
  (loopback, private-LAN IPs, `.local` names) — a plain-http URL to a
  routable host would send the ingest token unencrypted, so the bridge
  refuses it at startup. Redirects are refused for the same reason.
  Note: CGNAT addresses (`100.64.0.0/10`, e.g. Tailscale IPs) only count
  as private on Python 3.13+; on older Pythons use the host's LAN IP or
  a MagicDNS `https://` name instead.

## Tests

```sh
python3 -m pytest tests/ -q
```

The transform is pure Python and tests run without WeeWX installed.
