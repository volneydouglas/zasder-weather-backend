#!/usr/bin/env bash
# Guided local Docker setup — the LAN-only counterpart to setup-fly.sh.
# Generates strong tokens, asks which sources you want (+ only their
# credentials), writes .env, and starts the stack with docker compose.
#
# Usage:
#   ./bin/setup-local.sh                       # interactive
#   ./bin/setup-local.sh --sources=awn,lilygo --tz=America/Phoenix --yes
#
# Flags mirror setup-fly.sh: --sources= --tz= --yes
#   --aw-app-key= --aw-api-key= --wl-key= --wl-secret= --wl-station=

set -euo pipefail
# Everything this script writes holds tokens: .env, the .env.bak.* backup of a
# previous one, and zasder-install-summary.txt. Set the umask before the FIRST
# of them exists — it used to sit just above the .env write, so the backup was
# created under the user's default umask (022 → world-readable).
umask 077
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Absolute path to this script, resolved BEFORE the cd below — a relative
# $0 (./bin/setup-local.sh run from elsewhere) stops resolving after cd,
# and --help reads the script file to print its header.
SELF="$APP_DIR/bin/$(basename "${BASH_SOURCE[0]}")"
cd "$APP_DIR"

# umask only governs files this run CREATES. On a re-run — the common case,
# since this script is also the update path — .env and the summary already
# exist, and an earlier run (or a hand-edit) may have left them 0644. Appending
# to or overwriting them preserves that mode, so the tokens stay readable by
# every other account on the machine. Tighten what is already there before we
# write anything into it.
# Missing file = nothing to protect, fine. Present file we CANNOT lock down =
# stop. `|| true` here would have meant carrying on with world-readable tokens
# while printing a success message, which is worse than refusing.
harden() {
  local f
  for f in "$@"; do
    [ -e "$f" ] || continue
    if ! chmod 600 "$f"; then
      err "Couldn't set permissions on $f"
      err "It holds your tokens and is readable by other accounts on this"
      err "machine. Fix the ownership/permissions, then re-run."
      exit 1
    fi
  done
}

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }

# Called here, AFTER err() exists — harden's failure path uses it, and a
# not-yet-defined function would have turned a clear refusal into
# "err: command not found". Still before anything writes to these files.
harden "$APP_DIR/.env" "$APP_DIR/zasder-install-summary.txt"

NONINTERACTIVE=0; SOURCES=""; SOURCES_SET=0; TZ_FLAG=""
AW_APP_KEY_FLAG=""; AW_API_KEY_FLAG=""
WL_KEY_FLAG=""; WL_SECRET_FLAG=""; WL_STATION_FLAG=""
for arg in "$@"; do
  case "$arg" in
    --yes|--non-interactive) NONINTERACTIVE=1 ;;
    --sources=*) SOURCES="${arg#*=}"; SOURCES_SET=1 ;;
    --tz=*)      TZ_FLAG="${arg#*=}" ;;
    --aw-app-key=*) AW_APP_KEY_FLAG="${arg#*=}" ;;
    --aw-api-key=*) AW_API_KEY_FLAG="${arg#*=}" ;;
    --wl-key=*)     WL_KEY_FLAG="${arg#*=}" ;;
    --wl-secret=*)  WL_SECRET_FLAG="${arg#*=}" ;;
    --wl-station=*) WL_STATION_FLAG="${arg#*=}" ;;
    # Print the header comment block: from line 2 up to the first
    # non-comment line, via $SELF (absolute — $0 may be relative and we
    # already cd'd). A fixed sed range over $0 died under set -e when
    # invoked from another directory, and overran when the header grew.
    -h|--help) awk 'NR==1 {next} !/^#/ {exit} {sub(/^# ?/, ""); print}' "$SELF"; exit 0 ;;
    *) err "unknown flag: $arg"; exit 2 ;;
  esac
done

command -v docker >/dev/null || { err "docker not found — https://docs.docker.com/get-docker/"; exit 1; }
docker info >/dev/null 2>&1   || { err "docker daemon not running — start Docker Desktop / dockerd"; exit 1; }
command -v openssl >/dev/null || { err "openssl required to generate tokens"; exit 1; }

ask_yn() {
  local prompt="$1" default="$2" ans
  [ "$NONINTERACTIVE" -eq 1 ] && { [ "$default" = "Y" ]; return; }
  read -r -p "$prompt " ans; ans=${ans:-$default}
  case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}
source_enabled() { case ",$SOURCES," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }
# Read a credential without echoing it (same helper as setup-fly.sh) —
# `-s` keeps API keys out of the terminal scrollback and any recorded
# session; a closed stdin yields "" instead of an errexit death.
ask_secret() {  # ask_secret <varname> <prompt>
  local __var="$1" __prompt="$2" __val=""
  read -r -s -p "$__prompt" __val || __val=""
  echo
  printf -v "$__var" '%s' "$__val"
}
normalize_sources() {
  local out="" tok norm
  for tok in ${SOURCES//,/ }; do
    norm="$(printf '%s' "$tok" | tr 'A-Z' 'a-z')"
    case "$norm" in
      awn|aw|ambient|ambientweather) norm=awn ;;
      davis|wl|weatherlink) norm=davis ;;
      lilygo|rf|sdr|433|915) norm=lilygo ;;
      "") continue ;; *) warn "ignoring unknown source '$tok'"; continue ;;
    esac
    case ",$out," in *,"$norm",*) ;; *) out="${out:+$out,}$norm" ;; esac
  done
  SOURCES="$out"
}

# Same guard as setup-fly.sh, for the same reason: the backend resolves the
# zone with Python's zoneinfo, so an invalid value ("EDT", a typo) doesn't
# fail here — it 500s the status page, records and rain rollups later, with
# no hint that setup was where it went wrong. Validate the same way when
# Python is around; otherwise fall back to the system tz database. Both
# accept "America/New_York" and reject "EDT".
valid_timezone() {
  [ -n "$1" ] || return 1
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PY' >/dev/null 2>&1
import sys
from zoneinfo import ZoneInfo
ZoneInfo(sys.argv[1])
PY
    return $?
  fi
  # Fallback when python3 is missing. Guard the path BEFORE testing it: a
  # bare -f test accepts "../../../../etc/passwd", which exists, is not a
  # timezone, and would be written into TIMEZONE.
  case "$1" in
    /*|*..*|*//*) return 1 ;;
    *[!A-Za-z0-9_/+-]*) return 1 ;;
  esac
  [ -f "/usr/share/zoneinfo/$1" ]
}

bold "Zasder Weather — local Docker setup"
echo

if [ -f .env ]; then
  warn ".env already exists."
  ask_yn "Overwrite it? [y/N]" N || { err "aborted — edit .env by hand or move it aside"; exit 1; }
  _bak=".env.bak.$(date +%s)"
  # Both steps checked before claiming success — the message used to print
  # even if the copy or the chmod had failed.
  if ! cp .env "$_bak"; then
    err "Couldn't back up your existing .env — refusing to overwrite it."
    exit 1
  fi
  if ! chmod 600 "$_bak"; then
    err "Couldn't set permissions on $_bak, which contains your tokens."
    err "Remove it or fix its permissions, then re-run."
    exit 1
  fi
  info "backed up existing .env"
fi

# Sources
if [ "$SOURCES_SET" -eq 0 ]; then
  bold "Which ingest sources do you want to enable?"
  sel=""
  ask_yn "  AmbientWeather cloud poller?    [y/N]" N && sel="${sel:+$sel,}awn"
  ask_yn "  Davis WeatherLink cloud poller? [y/N]" N && sel="${sel:+$sel,}davis"
  ask_yn "  LilyGO / RF direct (433/915)?   [y/N]" N && sel="${sel:+$sel,}lilygo"
  SOURCES="$sel"
fi
normalize_sources
info "Enabling: ${SOURCES:-none}"

# Credentials per source
aw_app_key="$AW_APP_KEY_FLAG"; aw_api_key="$AW_API_KEY_FLAG"
if source_enabled awn && [ "$NONINTERACTIVE" -eq 0 ]; then
  echo; bold "AmbientWeather credentials (https://ambientweather.net/account)"
  [ -n "$aw_app_key" ] || ask_secret aw_app_key "AW_APPLICATION_KEY (input hidden): "
  [ -n "$aw_api_key" ] || ask_secret aw_api_key "AW_API_KEY (input hidden): "
fi
wl_key="$WL_KEY_FLAG"; wl_secret="$WL_SECRET_FLAG"; wl_station="$WL_STATION_FLAG"
if source_enabled davis && [ "$NONINTERACTIVE" -eq 0 ]; then
  echo; bold "Davis WeatherLink v2 credentials (https://www.weatherlink.com/account)"
  [ -n "$wl_key" ]     || ask_secret wl_key    "WEATHERLINK_API_KEY (input hidden): "
  [ -n "$wl_secret" ]  || ask_secret wl_secret "WEATHERLINK_API_SECRET (input hidden): "
  [ -n "$wl_station" ] || read -r -p "WEATHERLINK_STATION_ID: " wl_station
fi

# Fail before writing a broken .env if a selected cloud source is missing
# credentials (easy to hit with e.g. --sources=davis --yes).
if source_enabled awn && { [ -z "$aw_app_key" ] || [ -z "$aw_api_key" ]; }; then
  err "AmbientWeather selected but AW_APPLICATION_KEY / AW_API_KEY missing"; exit 1
fi
if source_enabled davis && { [ -z "$wl_key" ] || [ -z "$wl_secret" ] || [ -z "$wl_station" ]; }; then
  err "Davis selected but WEATHERLINK_API_KEY / _SECRET / _STATION_ID missing"; exit 1
fi

# Timezone — validated up front so a bad zone can't reach .env (see
# valid_timezone above for why it only breaks later otherwise).
tz="$TZ_FLAG"
if [ -n "$tz" ] && ! valid_timezone "$tz"; then
  err "--tz='$tz' isn't a valid IANA timezone (try America/New_York)."; exit 1
fi
if [ -z "$tz" ] && [ "$NONINTERACTIVE" -eq 0 ]; then
  echo
  info "Use an IANA name like America/New_York or Europe/London."
  info "Abbreviations such as EDT or PST are not valid here."
  while :; do
    read -r -p "TIMEZONE [UTC]: " tz || tz=""
    tz=${tz:-UTC}
    valid_timezone "$tz" && break
    err "'$tz' isn't a timezone name this server can use."
    info "It needs the Region/City form — America/New_York, not EDT."
    info "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
    echo
  done
fi
tz=${tz:-UTC}

# Tokens
api_token=$(openssl rand -hex 32)
ingest_token=$(openssl rand -hex 32)

# Write .env  (umask 077 is set at the top of the script)
{
  echo "# Generated by setup-local.sh on $(date)"
  echo "API_TOKEN=$api_token"
  echo "INGEST_TOKEN=$ingest_token"
  echo "DATABASE_PATH=/data/weather.db"
  echo "TIMEZONE=$tz"
  echo "POLL_INTERVAL_SECONDS=60"
  if source_enabled awn; then
    echo "AW_APPLICATION_KEY=$aw_app_key"
    echo "AW_API_KEY=$aw_api_key"
  fi
  if source_enabled davis; then
    echo "WEATHERLINK_API_KEY=$wl_key"
    echo "WEATHERLINK_API_SECRET=$wl_secret"
    echo "WEATHERLINK_STATION_ID=$wl_station"
    echo "WEATHERLINK_NAME=Davis Vantage Pro2 (Cloud)"
  fi
} > .env
info "wrote .env (chmod 600)"

echo
bold "Starting the stack"
docker compose up -d

host_ip=$(ipconfig getifaddr en0 2>/dev/null || true)
[ -z "$host_ip" ] && host_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$host_ip" ] && host_ip="<your-host-ip>"

# Summary
summary_file="$APP_DIR/zasder-install-summary.txt"
: > "$summary_file"
emit() { printf '%s\n' "$*"; printf '%s\n' "$*" >> "$summary_file"; }

echo
bold "Done!"
emit "Zasder Weather — local install summary ($(date))"
emit "Sources enabled: ${SOURCES:-none}"
emit ""
emit "Backend listens on: http://localhost:8080/"
emit "iOS app → Settings (must be on the same LAN):"
emit "  Backend URL:   http://$host_ip:8080"
emit "  Bearer Token:  $api_token"
emit ""
emit "INGEST_TOKEN (any source that POSTs to /ingest/custom uses this —"
emit "the wll-poller, LilyGO boards, or a custom relay):"
emit "  $ingest_token"
if source_enabled lilygo; then
  emit ""
  emit "!! LilyGO boards require an HTTPS backend — the firmware is TLS-only."
  emit "   This local-Docker backend is plain HTTP (http://$host_ip:8080), so"
  emit "   boards provisioned against it will NOT deliver data. Deploy to Fly.io"
  emit "   (auto HTTPS) or front this backend with a TLS-terminating reverse"
  emit "   proxy and provision the board with that https:// URL instead."
  emit ""
  emit "LilyGO provisioning (flash, join 'ZasderLilyGO' AP — WPA2 password"
  emit "zasder-setup — set Wi-Fi, then):"
  emit "  export BACKEND_URL=\"http://$host_ip:8080\"   # <-- must be https:// for LilyGO"
  emit "  export INGEST_TOKEN=\"$ingest_token\""
  emit "  curl -X POST \"http://zasder-lilygo-XXXX.local/provision\" \\"
  emit "    --data-urlencode \"backend_url=\$BACKEND_URL\" \\"
  emit "    --data-urlencode \"ingest_token=\$INGEST_TOKEN\""
fi
emit ""
emit "Logs:    docker compose logs -f"
emit "Verify:  curl http://localhost:8080/healthz"
echo
warn "zasder-install-summary.txt holds your tokens — it's git-ignored."
warn "Note: this is LAN-only. Exposing it to the internet (port-forward,"
warn "tunnel, reverse proxy) is on you — Fly.io is the supported public path."
