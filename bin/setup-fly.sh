#!/usr/bin/env bash
# Interactive Fly.io setup for the Zasder Weather backend.
#
# Two modes:
#   * create    — first deploy. Generates tokens, creates app + volume + secrets, deploys.
#   * update    — subsequent runs. Refuses to rotate tokens unless --rotate-tokens.
#                 Updates safe-to-change settings (timezone, AWN keys, custom host)
#                 without touching API_TOKEN / INGEST_TOKEN.
#
# Mode is auto-detected: `fly status` exists ⇒ update; otherwise create.
# `setup-fly.sh --force-create` overrides to wipe + restart (with explicit prompt).
#
# Subcommands you can layer on:
#   setup-fly.sh --rotate-tokens   # only allowed in update mode; warns first
#   setup-fly.sh --print-tokens    # SSH in and print the live values (since
#                                  # `fly secrets list` only shows digests)

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }

FORCE_CREATE=0
ROTATE_TOKENS=0
PRINT_TOKENS=0
for arg in "$@"; do
  case "$arg" in
    --force-create)  FORCE_CREATE=1 ;;
    --rotate-tokens) ROTATE_TOKENS=1 ;;
    --print-tokens)  PRINT_TOKENS=1 ;;
    -h|--help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//' ; exit 0 ;;
    *) err "unknown flag: $arg"; exit 2 ;;
  esac
done

command -v fly >/dev/null || { err "fly CLI not found. Install: https://fly.io/docs/install/"; exit 1; }
fly auth whoami >/dev/null 2>&1 || { err "not signed in. Run: fly auth login"; exit 1; }

read_app_name() {
  local default_app="zasder-weather-$(whoami | tr -cd '[:alnum:]')"
  read -r -p "App name [$default_app]: " app_name
  app_name=${app_name:-$default_app}
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
info "Mode: \033[1m$mode\033[0m"
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
  # 1. Region
  default_region="lax"
  read -r -p "Region [$default_region] (https://fly.io/docs/reference/regions/): " region
  region=${region:-$default_region}

  # 2. Custom hostname
  read -r -p "Custom hostname (blank to skip — you can add later): " custom_host

  # 3. AWN credentials (optional)
  echo
  bold "AmbientWeather credentials (from https://ambientweather.net/account)"
  info "Optional — only needed if you're polling the AWN cloud."
  read -r -p "AW_APPLICATION_KEY [skip]: " aw_app_key
  read -r -p "AW_API_KEY [skip]: " aw_api_key
  if [ -n "$aw_app_key" ] && [ -z "$aw_api_key" ]; then
    err "AW_APPLICATION_KEY set but AW_API_KEY missing"; exit 1
  fi

  # 4. Timezone
  echo
  bold "Local timezone (for daily/hourly/weekly/monthly rain rollups)"
  read -r -p "TIMEZONE [UTC]: " tz
  tz=${tz:-UTC}

  # 5. Fresh tokens
  api_token=$(openssl rand -hex 32)
  ingest_token=$(openssl rand -hex 32)
  echo
  info "Generated API_TOKEN (iOS app reads with this):"
  printf '  \033[33m%s\033[0m\n' "$api_token"
  info "Generated INGEST_TOKEN (LilyGO / SDR relay POST with this):"
  printf '  \033[33m%s\033[0m\n' "$ingest_token"
  echo

  # 6. Create app + volume
  bold "Creating Fly app $app_name in $region"
  fly apps create "$app_name" --org personal

  bold "Creating 1 GB volume weather_data in $region"
  fly volumes create weather_data --app "$app_name" --region "$region" --size 1 --yes

  # 7. fly.toml
  tmp_toml=$(mktemp)
  sed -e "s/^app *=.*/app = \"$app_name\"/" \
      -e "s/^primary_region *=.*/primary_region = \"$region\"/" \
      fly.toml > "$tmp_toml"
  mv "$tmp_toml" fly.toml

  # 8. Secrets
  bold "Setting secrets"
  secret_args=(API_TOKEN="$api_token" INGEST_TOKEN="$ingest_token" TIMEZONE="$tz")
  if [ -n "$aw_app_key" ] && [ -n "$aw_api_key" ]; then
    secret_args+=(AW_APPLICATION_KEY="$aw_app_key" AW_API_KEY="$aw_api_key")
  fi
  fly secrets set --app "$app_name" "${secret_args[@]}"

  # 9. Deploy
  bold "Deploying"
  fly deploy --app "$app_name"

  # 10. Optional custom hostname
  if [ -n "$custom_host" ]; then
    bold "Adding custom hostname $custom_host"
    fly certs add --app "$app_name" "$custom_host" || true
    echo
    info "Add the DNS records that 'fly certs show' prints, then re-run:"
    info "  fly certs check --app $app_name $custom_host"
  fi

  # 11. Done
  echo
  bold "Done!"
  url=${custom_host:+https://$custom_host}
  url=${url:-https://$app_name.fly.dev}
  info "Open the iOS app → Settings, paste:"
  echo
  printf "  Backend URL:    \033[36m%s\033[0m\n" "$url"
  printf "  Bearer Token:   \033[33m%s\033[0m\n" "$api_token"
  echo
  info "For LilyGO / SDR relay .env:"
  printf "  INGEST_TOKEN:   \033[33m%s\033[0m\n" "$ingest_token"
  echo
  info "Re-print live token values later: setup-fly.sh --print-tokens"
  info "(\`fly secrets list\` shows only digests, not the values themselves.)"
  exit 0
fi

# ── UPDATE MODE ────────────────────────────────────────────────────────
bold "Updating existing app $app_name"
info "Tokens will NOT be rotated. Re-run with --rotate-tokens to do that."
echo

# Allow editing: TIMEZONE, AWN credentials, custom hostname.
read -r -p "New TIMEZONE (blank to keep current): " tz
read -r -p "New AW_APPLICATION_KEY (blank to keep, '-' to clear): " aw_app_key
read -r -p "New AW_API_KEY         (blank to keep, '-' to clear): " aw_api_key
read -r -p "Add custom hostname    (blank to skip): " custom_host
echo

upd_args=()
[ -n "$tz" ]         && upd_args+=(TIMEZONE="$tz")
[ "$aw_app_key" = "-" ] && upd_args+=(AW_APPLICATION_KEY=) || \
  [ -n "$aw_app_key" ] && upd_args+=(AW_APPLICATION_KEY="$aw_app_key")
[ "$aw_api_key" = "-" ] && upd_args+=(AW_API_KEY=) || \
  [ -n "$aw_api_key" ] && upd_args+=(AW_API_KEY="$aw_api_key")

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
else
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
info "Re-print live token values later: setup-fly.sh --print-tokens"
