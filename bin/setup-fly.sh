#!/usr/bin/env bash
# Interactive Fly.io setup for the Zasder Weather backend.
# Idempotent — re-running on an existing app re-uses it.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }

command -v fly >/dev/null || { err "fly CLI not found. Install: https://fly.io/docs/install/"; exit 1; }
fly auth whoami >/dev/null 2>&1 || { err "not signed in. Run: fly auth login"; exit 1; }

bold "Zasder Weather — Fly.io setup"

# 1. App name
default_app="zasder-weather-$(whoami | tr -cd '[:alnum:]')"
read -r -p "App name [$default_app]: " app_name
app_name=${app_name:-$default_app}

# 2. Region
default_region="lax"
read -r -p "Region [$default_region] (https://fly.io/docs/reference/regions/): " region
region=${region:-$default_region}

# 3. Custom hostname
read -r -p "Custom hostname (blank to skip — you can add later): " custom_host

# 4. AWN credentials (optional — SDR-only deploys can skip)
echo
bold "AmbientWeather credentials (from https://ambientweather.net/account)"
info "Optional — only needed if you're polling the AWN cloud (Path A/B)."
info "SDR-direct (Path C/D) and legacy-relay (Path E) deploys can leave both blank."
read -r -p "AW_APPLICATION_KEY [skip]: " aw_app_key
read -r -p "AW_API_KEY [skip]: " aw_api_key
if [ -n "$aw_app_key" ] && [ -z "$aw_api_key" ]; then
  err "if AW_APPLICATION_KEY is set, AW_API_KEY is also required"; exit 1
fi

# 5. Local timezone for rain rollups
echo
bold "Local timezone (for daily/hourly/weekly/monthly rain rollups)"
info "Defaults to UTC. Set to an IANA zone for sensible 'today's rain' values."
info "Examples: America/Phoenix, America/New_York, Europe/London."
read -r -p "TIMEZONE [UTC]: " tz
tz=${tz:-UTC}

# 6. Generated bearer tokens
api_token=$(openssl rand -hex 32)
ingest_token=$(openssl rand -hex 32)
echo
info "Generated API_TOKEN (iOS app reads with this):"
printf '  \033[33m%s\033[0m\n' "$api_token"
info "Generated INGEST_TOKEN (SDR relay / legacy relay POST with this):"
printf '  \033[33m%s\033[0m\n' "$ingest_token"
echo

# 6. Create or reuse the app
if fly status --app "$app_name" >/dev/null 2>&1; then
  bold "App $app_name already exists — re-using"
else
  bold "Creating Fly app $app_name in $region"
  fly apps create "$app_name" --org personal
fi

# 7. fly.toml — make sure the app + region match what we just created
tmp_toml=$(mktemp)
sed -e "s/^app *=.*/app = \"$app_name\"/" \
    -e "s/^primary_region *=.*/primary_region = \"$region\"/" \
    fly.toml > "$tmp_toml"
mv "$tmp_toml" fly.toml

# 8. Volume
if fly volumes list --app "$app_name" 2>/dev/null | grep -q weather_data; then
  info "Volume weather_data already exists"
else
  bold "Creating 1 GB volume weather_data in $region"
  fly volumes create weather_data --app "$app_name" --region "$region" --size 1 --yes
fi

# 9. Secrets — set the required + opt-in for AWN if creds provided.
bold "Setting secrets"
secret_args=(API_TOKEN="$api_token" INGEST_TOKEN="$ingest_token" TIMEZONE="$tz")
if [ -n "$aw_app_key" ] && [ -n "$aw_api_key" ]; then
  secret_args+=(AW_APPLICATION_KEY="$aw_app_key" AW_API_KEY="$aw_api_key")
fi
fly secrets set --app "$app_name" "${secret_args[@]}"

# 10. Deploy
bold "Deploying"
fly deploy --app "$app_name"

# 11. Optional custom hostname
if [ -n "$custom_host" ]; then
  bold "Adding custom hostname $custom_host"
  fly certs add --app "$app_name" "$custom_host" || true
  echo
  info "Add the DNS records that 'fly certs show' prints, then re-run:"
  info "  fly certs check --app $app_name $custom_host"
fi

# 12. Done — summary
echo
bold "Done!"
url=${custom_host:+https://$custom_host}
url=${url:-https://$app_name.fly.dev}
info "Open the iOS app → Settings, paste:"
echo
printf "  Backend URL:    \033[36m%s\033[0m\n" "$url"
printf "  Bearer Token:   \033[33m%s\033[0m\n" "$api_token"
echo
info "If you're setting up an SDR (Path C/D) or legacy hub relay (Path E),"
info "use this INGEST_TOKEN in the relay's .env:"
printf "  INGEST_TOKEN:   \033[33m%s\033[0m\n" "$ingest_token"
echo
info "(Tokens are viewable later via: fly secrets list --app $app_name)"
