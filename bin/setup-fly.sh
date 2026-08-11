#!/usr/bin/env bash
# Interactive Fly.io setup for the Zasder Weather backend.
#
# Path-based: you pick which ingest sources you want FIRST, then it only
# asks for what those paths need — and at the end it prints (and saves)
# the exact next steps for each path you chose.
#
# Two modes (auto-detected: `fly status` exists ⇒ update; else create):
#   * create    — first deploy. Generates tokens, creates app + volume +
#                 secrets, deploys, writes zasder-install-summary.txt.
#   * update    — subsequent runs. Refuses to rotate tokens unless
#                 --rotate-tokens. Edits safe settings (timezone, source
#                 credentials, custom host) without touching tokens.
#
# Flags:
#   --force-create     treat as initial create even if the app exists
#   --rotate-tokens    (update mode) regenerate API_TOKEN + INGEST_TOKEN
#   --print-tokens     SSH in and print the live values (since `fly
#                      secrets list` only shows digests)
#
# Non-interactive overrides (used by the web planner at zasder.com/weather-helper;
# any value not supplied falls back to a prompt unless --yes is set):
#   --app=NAME            --region=CODE        --tz=IANA/Zone
#   --host=HOSTNAME       --sources=awn,davis,lilygo
#   --aw-app-key=…        --aw-api-key=…
#   --wl-key=…            --wl-secret=…        --wl-station=…
#   --yes                 don't prompt; use flags + defaults only

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Absolute path to this script, resolved BEFORE the cd below — a relative
# $0 (./bin/setup-fly.sh run from elsewhere) stops resolving after cd, and
# --help reads the script file to print its header.
SELF="$APP_DIR/bin/$(basename "${BASH_SOURCE[0]}")"
cd "$APP_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }

FORCE_CREATE=0
ROTATE_TOKENS=0
PRINT_TOKENS=0
NONINTERACTIVE=0
SOURCES_SET=0
SOURCES=""
APP_NAME_FLAG=""; REGION_FLAG=""; TZ_FLAG=""; HOST_FLAG=""
AW_APP_KEY_FLAG=""; AW_API_KEY_FLAG=""
WL_KEY_FLAG=""; WL_SECRET_FLAG=""; WL_STATION_FLAG=""

for arg in "$@"; do
  case "$arg" in
    --force-create)  FORCE_CREATE=1 ;;
    --rotate-tokens) ROTATE_TOKENS=1 ;;
    --print-tokens)  PRINT_TOKENS=1 ;;
    --yes|--non-interactive) NONINTERACTIVE=1 ;;
    --app=*)         APP_NAME_FLAG="${arg#*=}" ;;
    --sources=*)     SOURCES="${arg#*=}"; SOURCES_SET=1 ;;
    --region=*)      REGION_FLAG="${arg#*=}" ;;
    --tz=*)          TZ_FLAG="${arg#*=}" ;;
    --host=*)        HOST_FLAG="${arg#*=}" ;;
    --aw-app-key=*)  AW_APP_KEY_FLAG="${arg#*=}" ;;
    --aw-api-key=*)  AW_API_KEY_FLAG="${arg#*=}" ;;
    --wl-key=*)      WL_KEY_FLAG="${arg#*=}" ;;
    --wl-secret=*)   WL_SECRET_FLAG="${arg#*=}" ;;
    --wl-station=*)  WL_STATION_FLAG="${arg#*=}" ;;
    -h|--help)
      # Print the header comment block: from line 2 up to the first
      # non-comment line. A fixed sed range overran the header and dumped
      # raw code whenever the comment block changed length.
      awk 'NR==1 {next} !/^#/ {exit} {sub(/^# ?/, ""); print}' "$SELF" ; exit 0 ;;
    *) err "unknown flag: $arg"; exit 2 ;;
  esac
done

command -v fly >/dev/null || { err "fly CLI not found. Install: https://fly.io/docs/install/"; exit 1; }
# openssl mints API_TOKEN/INGEST_TOKEN much later — check for it up front so
# a minimal system fails here with a clear message instead of dying mid-run
# under set -e after the app and volume already exist.
command -v openssl >/dev/null || { err "openssl not found — it's required to generate tokens. Install openssl, then re-run."; exit 1; }
fly auth whoami >/dev/null 2>&1 || { err "not signed in. Run: fly auth login"; exit 1; }

# ── helpers ─────────────────────────────────────────────────────────────
ask_yn() {  # ask_yn "Prompt" default(Y|N) → 0 if yes
  local prompt="$1" default="$2" ans
  if [ "$NONINTERACTIVE" -eq 1 ]; then
    [ "$default" = "Y" ]; return
  fi
  read -r -p "$prompt " ans
  ans=${ans:-$default}
  case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

source_enabled() { case ",$SOURCES," in *,"$1",*) return 0 ;; *) return 1 ;; esac; }

normalize_sources() {  # map aliases → canonical awn|davis|lilygo, dedup
  local out="" tok norm
  for tok in ${SOURCES//,/ }; do
    norm="$(printf '%s' "$tok" | tr 'A-Z' 'a-z')"
    case "$norm" in
      awn|aw|ambient|ambientweather) norm=awn ;;
      davis|wl|weatherlink)          norm=davis ;;
      lilygo|rf|sdr|433|915)         norm=lilygo ;;
      "") continue ;;
      *) warn "ignoring unknown source '$tok'"; continue ;;
    esac
    case ",$out," in *,"$norm",*) ;; *) out="${out:+$out,}$norm" ;; esac
  done
  SOURCES="$out"
}

# The backend resolves this with Python's zoneinfo, so validate the same way
# when Python is around; otherwise fall back to the system tz database. Both
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
  # Fallback when python3 is missing. Guard the path BEFORE testing it: the
  # bare -f test accepted "../../../../etc/passwd", which exists, is not a
  # timezone, and would be written into TIMEZONE and shipped to Fly.
  case "$1" in
    /*|*..*|*//*) return 1 ;;
    *[!A-Za-z0-9_/+-]*) return 1 ;;
  esac
  [ -f "/usr/share/zoneinfo/$1" ]
}

# Fly app names: lowercase letters, digits and dashes only, starting with a
# letter, max 63 chars. Validating here matters more than it looks — the most
# common mistake is pasting the NEXT command from the README into this prompt,
# which otherwise sails through to Fly and comes back as a baffling
# "Name blocked by abuse filter" instead of "that isn't an app name".
valid_app_name() {
  # Pin the C locale for the glob ranges below: under a UTF-8 collation
  # locale (the default on macOS/Linux desktops) [a-z] can match UPPERCASE
  # letters too (aAbB… ordering), so "Foo" slipped through to Fly and came
  # back as Fly's own confusing rejection instead of ours.
  local LC_ALL=C
  case "$1" in
    ""|*[!a-z0-9-]*) return 1 ;;
    [!a-z]*)         return 1 ;;
    *-)              return 1 ;;
  esac
  [ "${#1}" -le 63 ]
}

read_app_name() {
  if [ -n "$APP_NAME_FLAG" ]; then
    app_name="$APP_NAME_FLAG"
    valid_app_name "$app_name" || {
      err "--app='$app_name' isn't a valid Fly app name."
      err "Use lowercase letters, numbers and dashes, e.g. zasder-weather-doren"
      exit 1
    }
    return
  fi
  local default_app="zasder-weather-$(whoami | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
  if [ "$NONINTERACTIVE" -eq 1 ]; then app_name="$default_app"; return; fi
  while :; do
    read -r -p "App name [$default_app]: " app_name
    app_name=${app_name:-$default_app}
    valid_app_name "$app_name" && break
    echo
    case "$app_name" in
      git\ *|cd\ *|bash\ *|sh\ *|curl\ *|*/*|http*)
        err "That looks like a command, not a name."
        err "This prompt is asking you to NAME your app — it is not asking you"
        err "to paste the next line from the instructions. Just type a name and"
        err "press return, e.g.:  zasder-weather-doren"
        ;;
      *)
        err "'$app_name' isn't a valid Fly app name."
        err "Use only lowercase letters, numbers and dashes, starting with a"
        err "letter — for example:  zasder-weather-doren"
        ;;
    esac
    echo "  (or just press return to accept the default: $default_app)"
    echo
  done
}

# ── --print-tokens: short-circuit, read live values via SSH ─────────────
if [ "$PRINT_TOKENS" -eq 1 ]; then
  bold "Printing live token values from the running app"
  read_app_name
  for name in API_TOKEN INGEST_TOKEN; do
    val=$(fly ssh console -a "$app_name" -C "printenv $name" 2>/dev/null \
            | tr -d '\r' | tail -1)
    if [ -n "$val" ]; then
      printf "  %-15s \033[33m%s\033[0m\n" "$name" "$val"
    else
      warn "couldn't read $name from app (is it running?)"
    fi
  done
  exit 0
fi

# ── Detect mode ────────────────────────────────────────────────────────
bold "Zasder Weather — Fly.io setup"
read_app_name

mode=create
if fly status --app "$app_name" >/dev/null 2>&1; then
  mode=update
fi
if [ "$FORCE_CREATE" -eq 1 ]; then
  warn "--force-create set; will treat as initial create even if app exists"
  mode=create
fi
printf '  Mode: \033[1m%s\033[0m\n' "$mode"
echo

# ── --rotate-tokens guardrail (update mode only) ───────────────────────
if [ "$ROTATE_TOKENS" -eq 1 ] && [ "$mode" != "update" ]; then
  err "--rotate-tokens only makes sense for an existing app (update mode)"
  exit 2
fi
if [ "$ROTATE_TOKENS" -eq 1 ]; then
  warn "ABOUT TO ROTATE API_TOKEN + INGEST_TOKEN."
  warn "After this, every iOS device and LilyGO/SDR relay will need the NEW values."
  read -r -p "Type 'rotate' to confirm: " confirm
  [ "$confirm" = "rotate" ] || { err "aborted"; exit 1; }
fi

# ── CREATE MODE ────────────────────────────────────────────────────────
if [ "$mode" = "create" ]; then
  # 1. Source selection FIRST — everything after is scoped to these.
  if [ "$SOURCES_SET" -eq 0 ]; then
    bold "Which ingest sources do you want to enable? (add more anytime by re-running)"
    info "AmbientWeather + Davis are cloud pollers (no hardware to flash)."
    info "LilyGO is real-time RF capture (needs an ESP32 board per band)."
    info "Have a Davis WeatherLink LIVE on your LAN? Skip Davis here and"
    info "set up wll-poller/ on a Pi — it POSTs through /ingest/custom"
    info "(needs no Fly secrets). See README Path E."
    echo
    sel=""
    ask_yn "  AmbientWeather cloud poller?    [y/N]" N && sel="${sel:+$sel,}awn"
    ask_yn "  Davis WeatherLink cloud poller? [y/N]" N && sel="${sel:+$sel,}davis"
    ask_yn "  LilyGO / RF direct (433/915)?   [y/N]" N && sel="${sel:+$sel,}lilygo"
    SOURCES="$sel"
  fi
  normalize_sources
  if [ -z "$SOURCES" ]; then
    warn "No sources selected. The backend will deploy but stay empty until you"
    warn "enable a source. Re-run with --sources=… or add one from the iOS app docs."
  else
    printf '  Enabling: \033[1m%s\033[0m\n' "$SOURCES"
  fi
  echo

  # 2. Region
  default_region="${REGION_FLAG:-lax}"
  if [ "$NONINTERACTIVE" -eq 1 ] || [ -n "$REGION_FLAG" ]; then
    region="$default_region"
  else
    read -r -p "Region [$default_region] (https://fly.io/docs/reference/regions/): " region
    region=${region:-$default_region}
  fi

  # 3. Custom hostname
  custom_host="$HOST_FLAG"
  if [ -z "$custom_host" ] && [ "$NONINTERACTIVE" -eq 0 ]; then
    read -r -p "Custom hostname (blank to skip — you can add later): " custom_host
  fi

  # 4. AmbientWeather credentials — only if Path A enabled
  aw_app_key="$AW_APP_KEY_FLAG"; aw_api_key="$AW_API_KEY_FLAG"
  if source_enabled awn; then
    echo
    bold "AmbientWeather credentials (https://ambientweather.net/account)"
    if [ "$NONINTERACTIVE" -eq 0 ]; then
      [ -n "$aw_app_key" ] || read -r -p "AW_APPLICATION_KEY: " aw_app_key
      [ -n "$aw_api_key" ] || read -r -p "AW_API_KEY: " aw_api_key
    fi
    if [ -z "$aw_app_key" ] || [ -z "$aw_api_key" ]; then
      err "AmbientWeather selected but AW_APPLICATION_KEY / AW_API_KEY missing"; exit 1
    fi
  fi

  # 5. Davis WeatherLink credentials — only if Path B enabled
  wl_key="$WL_KEY_FLAG"; wl_secret="$WL_SECRET_FLAG"; wl_station="$WL_STATION_FLAG"
  if source_enabled davis; then
    echo
    bold "Davis WeatherLink v2 credentials (https://www.weatherlink.com/account)"
    info "The 'API Key v2' section is at the BOTTOM-LEFT of the account page."
    if [ "$NONINTERACTIVE" -eq 0 ]; then
      [ -n "$wl_key" ]     || read -r -p "WEATHERLINK_API_KEY: " wl_key
      [ -n "$wl_secret" ]  || read -r -p "WEATHERLINK_API_SECRET: " wl_secret
      [ -n "$wl_station" ] || read -r -p "WEATHERLINK_STATION_ID (find via /v2/stations): " wl_station
    fi
    if [ -z "$wl_key" ] || [ -z "$wl_secret" ] || [ -z "$wl_station" ]; then
      err "Davis selected but one of WEATHERLINK_API_KEY/_SECRET/_STATION_ID is missing"; exit 1
    fi
  fi

  # 6. Timezone (affects rain rollups regardless of source)
  #
  # Must be an IANA name. Abbreviations are the obvious thing to type and are
  # NOT valid: ZoneInfo("EDT") raises, and nothing here validated it, so the
  # value sailed into a secret and then 500'd the status page, records and
  # rain rollups later — with no hint that setup was where it went wrong.
  echo
  bold "Local timezone (for daily/hourly/weekly/monthly rain rollups)"
  # Offer the machine's own zone as the default; it's almost always right and
  # saves the user guessing at a format they've never seen.
  sys_tz="UTC"
  if [ -L /etc/localtime ]; then
    sys_tz="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
  fi
  [ -n "$sys_tz" ] || sys_tz="UTC"
  tz="$TZ_FLAG"
  if [ -z "$tz" ] && [ "$NONINTERACTIVE" -eq 0 ]; then
    info "Use an IANA name like America/New_York or Europe/London."
    info "Abbreviations such as EDT or PST are not valid here."
    while :; do
      read -r -p "TIMEZONE [$sys_tz]: " tz || tz=""
      tz=${tz:-$sys_tz}
      valid_timezone "$tz" && break
      err "'$tz' isn't a timezone name this server can use."
      info "It needs the Region/City form — America/New_York, not EDT."
      info "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
      echo
    done
  fi
  tz=${tz:-$sys_tz}
  if ! valid_timezone "$tz"; then
    err "--tz='$tz' isn't a valid IANA timezone (try America/New_York)."; exit 1
  fi

  # 7. Fresh tokens
  api_token=$(openssl rand -hex 32)
  ingest_token=$(openssl rand -hex 32)
  echo
  info "Generated API_TOKEN (iOS app reads with this):"
  printf '  \033[33m%s\033[0m\n' "$api_token"
  info "Generated INGEST_TOKEN (LilyGO boards / WLL poller / any /ingest/custom source POST with this):"
  printf '  \033[33m%s\033[0m\n' "$ingest_token"
  echo

  # 8. Create app + volume
  bold "Creating Fly app $app_name in $region"
  fly apps create "$app_name" --org personal

  bold "Creating 1 GB volume weather_data in $region"
  fly volumes create weather_data --app "$app_name" --region "$region" --size 1 --yes

  # 9. fly.toml — pinning the app name here is what makes bare `fly deploy`,
  #    `fly status` and `fly logs` work from this directory. The cost is that
  #    this TRACKED file is now permanently modified, so a plain `git pull`
  #    will refuse to fast-forward the day a release edits it. bin/upgrade.sh
  #    carries the pin across the pull; that's why the summary below points at
  #    it instead of at `git pull && fly deploy`.
  tmp_toml=$(mktemp)
  sed -e "s/^app *=.*/app = \"$app_name\"/" \
      -e "s/^primary_region *=.*/primary_region = \"$region\"/" \
      fly.toml > "$tmp_toml"
  mv "$tmp_toml" fly.toml

  # 10. Secrets — only the ones the chosen paths need
  bold "Setting secrets"
  secret_args=(API_TOKEN="$api_token" INGEST_TOKEN="$ingest_token" TIMEZONE="$tz")
  source_enabled awn   && secret_args+=(AW_APPLICATION_KEY="$aw_app_key" AW_API_KEY="$aw_api_key")
  source_enabled davis && secret_args+=(WEATHERLINK_API_KEY="$wl_key" WEATHERLINK_API_SECRET="$wl_secret" WEATHERLINK_STATION_ID="$wl_station")
  fly secrets set --app "$app_name" "${secret_args[@]}"

  # 11. Deploy
  bold "Deploying"
  fly deploy --app "$app_name"

  # 12. Optional custom hostname
  if [ -n "$custom_host" ]; then
    bold "Adding custom hostname $custom_host"
    fly certs add --app "$app_name" "$custom_host" || true
    echo
    info "Add the DNS records that 'fly certs show' prints, then re-run:"
    info "  fly certs check --app $app_name $custom_host"
  fi

  # 13. Compose the next-steps summary (printed AND written to a file —
  #     terminal scrollback gets lost; a local file is kinder, and Fly
  #     secrets can't be re-read without SSH).
  url=${custom_host:+https://$custom_host}
  url=${url:-https://$app_name.fly.dev}
  summary_file="$APP_DIR/zasder-install-summary.txt"

  emit() {  # write a block to BOTH stdout and the summary file
    printf '%s\n' "$*"
    printf '%s\n' "$*" >> "$summary_file"
  }

  umask 077
  : > "$summary_file"   # truncate; 600 perms via umask
  {
    echo "Zasder Weather — install summary"
    echo "Generated $(date)"
    echo "Sources enabled: ${SOURCES:-none}"
    echo "==================================================================="
  } >> "$summary_file"

  echo
  bold "Done!"
  echo "--- next steps (also saved to zasder-install-summary.txt) ---" >> "$summary_file"

  emit ""
  emit "iOS app → Settings, paste:"
  emit "  Backend URL:   $url"
  emit "  Bearer Token:  $api_token"

  emit ""
  emit "INGEST_TOKEN (any source that POSTs to /ingest/custom uses this —"
  emit "LilyGO boards, the wll-poller, or a custom relay):"
  emit "  $ingest_token"

  if source_enabled lilygo; then
    emit ""
    emit "LilyGO provisioning — flash, join the 'ZasderLilyGO' Wi-Fi AP (WPA2"
    emit "password: zasder-setup), enter"
    emit "your home Wi-Fi, then from any LAN device run (board mDNS name is"
    emit "zasder-lilygo-XXXX.local, XXXX = last 2 bytes of its MAC):"
    emit ""
    emit "  export BACKEND_URL=\"$url\""
    emit "  export INGEST_TOKEN=\"$ingest_token\""
    emit ""
    emit "  # 433 MHz board (AcuRite Atlas):"
    emit "  curl -X POST \"http://zasder-lilygo-XXXX.local/provision\" \\"
    emit "    --data-urlencode \"backend_url=\$BACKEND_URL\" \\"
    emit "    --data-urlencode \"ingest_token=\$INGEST_TOKEN\""
    emit ""
    emit "  # 915 MHz board (Fine Offset / AmbientWeather):"
    emit "  curl -X POST \"http://zasder-lilygo-YYYY.local/provision\" \\"
    emit "    --data-urlencode \"backend_url=\$BACKEND_URL\" \\"
    emit "    --data-urlencode \"ingest_token=\$INGEST_TOKEN\""
    emit ""
    emit "After data flows, calibrate yearly-rain (lifetime counter → real YTD):"
    emit "  ./bin/set-rain-offset.sh <MAC> <current-lifetime-rain-in> [--ytd=ACTUAL]"
  fi

  emit ""
  emit "Verify the backend:"
  emit "  curl $url/healthz"
  emit "  curl -H \"Authorization: Bearer $api_token\" $url/api/devices"
  emit "  ./bin/doctor.sh --app $app_name      # full health checklist"
  emit ""
  emit "Re-print live tokens later:  ./bin/setup-fly.sh --print-tokens"
  emit "(\`fly secrets list\` shows only digests, not the values.)"
  emit ""
  emit "Upgrade later with:  ./bin/upgrade.sh"
  emit "Use that, not 'git pull' — this setup wrote your app name into"
  emit "fly.toml, which git tracks, so a plain pull will refuse to update"
  emit "once a release changes that file. upgrade.sh handles it."

  echo
  warn "zasder-install-summary.txt contains your tokens (chmod 600). It is"
  warn "git-ignored — store it in your password manager and delete when done."
  exit 0
fi

# ── UPDATE MODE ────────────────────────────────────────────────────────
bold "Updating existing app $app_name"
info "Tokens will NOT be rotated. Re-run with --rotate-tokens to do that."
info "To enable a new source later, just set its secrets here (or via"
info "\`fly secrets set\`): AWN = AW_APPLICATION_KEY + AW_API_KEY; Davis ="
info "WEATHERLINK_API_KEY + _SECRET + _STATION_ID; LilyGO = no backend"
info "secret beyond INGEST_TOKEN (provision the board itself)."
echo

# Allow editing: TIMEZONE, AWN + WeatherLink credentials, custom hostname.
# Seed every value from the already-parsed flags and only prompt for what
# wasn't supplied — and never prompt under --yes. Update mode used to ignore
# both, so the web planner's non-interactive runs blocked on stdin (or read
# EOF garbage) as soon as the app already existed.
#
# Timezone: same guard as create mode, for the same reason — an unvalidated
# zone sails into a secret here and only shows up later as a 500 on the
# status page, records and rain rollups.
tz="$TZ_FLAG"
if [ -n "$tz" ] && ! valid_timezone "$tz"; then
  err "--tz='$tz' isn't a valid IANA timezone (try America/New_York)."; exit 1
fi
aw_app_key="$AW_APP_KEY_FLAG"; aw_api_key="$AW_API_KEY_FLAG"
wl_key="$WL_KEY_FLAG"; wl_secret="$WL_SECRET_FLAG"; wl_station="$WL_STATION_FLAG"
custom_host="$HOST_FLAG"
if [ "$NONINTERACTIVE" -eq 0 ]; then
  if [ -z "$tz" ]; then
    while :; do
      read -r -p "New TIMEZONE (blank to keep current): " tz || tz=""
      [ -z "$tz" ] && break
      valid_timezone "$tz" && break
      err "'$tz' isn't a timezone name this server can use."
      info "It needs the Region/City form — America/New_York, not EDT."
      info "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
    done
  fi
  # Secrets: -s keeps them out of the terminal scrollback and any recorded
  # session (same reasoning as wll-poller's ask_secret). `|| val=""` turns a
  # closed stdin into "keep current" instead of an errexit death mid-update.
  ask_secret_keep() {  # ask_secret_keep <varname> <prompt>
    local __var="$1" __prompt="$2" __val=""
    read -r -s -p "$__prompt" __val || __val=""
    echo
    printf -v "$__var" '%s' "$__val"
  }
  [ -n "$aw_app_key" ] || ask_secret_keep aw_app_key "New AW_APPLICATION_KEY (blank to keep, '-' to clear): "
  [ -n "$aw_api_key" ] || ask_secret_keep aw_api_key "New AW_API_KEY         (blank to keep, '-' to clear): "
  [ -n "$wl_key" ]     || ask_secret_keep wl_key     "New WEATHERLINK_API_KEY    (blank to keep, '-' to clear): "
  [ -n "$wl_secret" ]  || ask_secret_keep wl_secret  "New WEATHERLINK_API_SECRET (blank to keep, '-' to clear): "
  [ -n "$wl_station" ] || read -r -p "New WEATHERLINK_STATION_ID (blank to keep, '-' to clear): " wl_station || wl_station=""
  [ -n "$custom_host" ] || read -r -p "Add custom hostname    (blank to skip): " custom_host || custom_host=""
  echo
fi

# Three states per credential: blank = keep, '-' = clear, anything else =
# set. Clearing uses `fly secrets unset`, which REMOVES the variable —
# staging `KEY=` with `fly secrets set` leaves it set to the empty string,
# and "present but empty" is not the same as "absent" to the backend's
# is-this-source-configured checks. (The old && / || chains here also had
# the classic precedence bug: typing '-' staged BOTH `KEY=` and `KEY=-`,
# and the literal '-' won.)
upd_args=(); unset_names=()
stage() {  # stage SECRET_NAME VALUE
  case "$2" in
    "") ;;
    -)  unset_names+=("$1") ;;
    *)  upd_args+=("$1=$2") ;;
  esac
}
[ -n "$tz" ] && upd_args+=(TIMEZONE="$tz")
stage AW_APPLICATION_KEY     "$aw_app_key"
stage AW_API_KEY             "$aw_api_key"
stage WEATHERLINK_API_KEY    "$wl_key"
stage WEATHERLINK_API_SECRET "$wl_secret"
stage WEATHERLINK_STATION_ID "$wl_station"

if [ "$ROTATE_TOKENS" -eq 1 ]; then
  new_api=$(openssl rand -hex 32)
  new_ingest=$(openssl rand -hex 32)
  upd_args+=(API_TOKEN="$new_api" INGEST_TOKEN="$new_ingest")
  echo
  warn "New tokens generated. Save these BEFORE pressing enter on the next step:"
  printf "  API_TOKEN     \033[33m%s\033[0m\n" "$new_api"
  printf "  INGEST_TOKEN  \033[33m%s\033[0m\n" "$new_ingest"
  read -r -p "Press Enter to commit the rotation..."
fi

if [ "${#upd_args[@]}" -gt 0 ]; then
  bold "Updating secrets"
  fly secrets set --app "$app_name" "${upd_args[@]}"
fi
if [ "${#unset_names[@]}" -gt 0 ]; then
  bold "Clearing secrets: ${unset_names[*]}"
  fly secrets unset --app "$app_name" "${unset_names[@]}"
fi
if [ "${#upd_args[@]}" -eq 0 ] && [ "${#unset_names[@]}" -eq 0 ]; then
  info "No secret changes."
fi

if [ -n "$custom_host" ]; then
  bold "Adding custom hostname $custom_host"
  fly certs add --app "$app_name" "$custom_host" || true
  echo
  info "Add the DNS records that 'fly certs show' prints, then re-run:"
  info "  fly certs check --app $app_name $custom_host"
fi

echo
bold "Done!"
info "Re-print live token values later: ./bin/setup-fly.sh --print-tokens"
info "Run the health checklist:          ./bin/doctor.sh --app $app_name"
