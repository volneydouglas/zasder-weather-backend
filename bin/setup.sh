#!/usr/bin/env bash
# Zasder Weather — interactive setup wizard.
#
# Walks through the four supported deploy paths and dispatches to the
# right scripts / docker-compose targets. Idempotent — re-run any time
# to add a station type or repoint the relay.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bold()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
err()   { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }

ask() {
  # ask "question" "default" → echoes the answer
  local q=$1 default=${2:-}
  local prompt="$q"
  [ -n "$default" ] && prompt="$q [$default]"
  read -r -p "$prompt: " answer
  echo "${answer:-$default}"
}

ask_choice() {
  # ask_choice "question" "a b c" → numbered menu, echoes the choice
  local q=$1; shift
  local opts=("$@")
  echo "  $q"
  local i=1
  for o in "${opts[@]}"; do printf "    %d) %s\n" $i "$o"; i=$((i+1)); done
  while true; do
    read -r -p "  Pick 1-${#opts[@]}: " n
    [[ "$n" =~ ^[1-9][0-9]*$ ]] && [ "$n" -le "${#opts[@]}" ] && { echo "${opts[$((n-1))]}"; return; }
  done
}

bold "Zasder Weather — setup wizard"
echo "Run me from the project root: ./bin/setup.sh"
echo

# ─── 1. Sources ─────────────────────────────────────────────────────────
bold "1. What weather stations will you be ingesting from?"
src=$(ask_choice "Pick one:" \
  "AmbientWeather only" \
  "AcuRite (Atlas/Access) only" \
  "Both AmbientWeather and AcuRite")
echo
case "$src" in
  *AmbientWeather*only*) HAS_AWN=1; HAS_ACURITE=0 ;;
  *AcuRite*only*)        HAS_AWN=0; HAS_ACURITE=1 ;;
  *Both*)                HAS_AWN=1; HAS_ACURITE=1 ;;
esac

# ─── 2. Backend host ────────────────────────────────────────────────────
bold "2. Where should the backend run?"
host=$(ask_choice "Pick one:" \
  "Fly.io (cloud, free tier)" \
  "This machine / a LAN box (Docker)")
echo
case "$host" in
  *Fly.io*) BACKEND_HOST="fly" ;;
  *)        BACKEND_HOST="local" ;;
esac

# ─── 3. Backend setup ───────────────────────────────────────────────────
if [ "$BACKEND_HOST" = "fly" ]; then
  bold "3. Setting up the Fly.io backend"
  if [ ! -x bin/setup-fly.sh ]; then
    err "bin/setup-fly.sh not found. Are you running from the repo root?"
    exit 1
  fi
  ./bin/setup-fly.sh
  ok "Fly backend deployed."
  warn "Save the Backend URL + Bearer Token printed above — you'll paste both into the iOS app."
else
  bold "3. Setting up the local backend"
  if [ ! -f .env ]; then
    cp .env.example .env
    info "Created .env from template."
    if [ "$HAS_AWN" -eq 1 ]; then
      aw_app=$(ask "AW_APPLICATION_KEY (from ambientweather.net/account)")
      aw_api=$(ask "AW_API_KEY")
      sed -i.bak "s|^AW_APPLICATION_KEY=.*|AW_APPLICATION_KEY=$aw_app|" .env
      sed -i.bak "s|^AW_API_KEY=.*|AW_API_KEY=$aw_api|" .env
      rm -f .env.bak
    fi
    api_token=$(openssl rand -hex 32)
    sed -i.bak "s|^API_TOKEN=.*|API_TOKEN=$api_token|" .env
    rm -f .env.bak
    if [ "$HAS_ACURITE" -eq 1 ]; then
      ingest_token=$(openssl rand -hex 32)
      echo "INGEST_TOKEN=$ingest_token" >> .env
    fi
    chmod 600 .env
    ok "Generated API_TOKEN ($api_token)"
    [ "$HAS_ACURITE" -eq 1 ] && ok "Generated INGEST_TOKEN ($(grep ^INGEST_TOKEN .env | cut -d= -f2))"
  else
    info ".env already exists — re-using."
  fi
  command -v docker >/dev/null || { err "docker not installed. See https://docs.docker.com/engine/install/"; exit 1; }
  docker compose up -d
  ok "Local backend running on http://0.0.0.0:8080"
  lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "<your-lan-ip>")
  warn "iOS app → Settings → Backend URL: http://$lan_ip:8080"
  warn "iOS app → Settings → Bearer Token: $(grep ^API_TOKEN .env | cut -d= -f2)"
fi

# ─── 4. Relay setup (only if AcuRite) ──────────────────────────────────
if [ "$HAS_ACURITE" -eq 1 ]; then
  echo
  bold "4. Setting up the AcuRite relay"
  info "The relay needs to run on a LAN host (this machine, a Pi, etc)."
  do_now=$(ask_choice "Set up the relay now (on this machine)?" "Yes" "No, I'll do it on a different machine")
  if [[ "$do_now" == Yes* ]]; then
    cd relay
    if [ ! -f .env ]; then
      cp .env.example .env
      if [ "$BACKEND_HOST" = "fly" ]; then
        backend_url=$(ask "Backend URL (Fly app's URL)" "https://your-app.fly.dev")
        ingest_token=$(ask "INGEST_TOKEN (the one fly secrets set INGEST_TOKEN=...)")
      else
        backend_url=$(ask "Backend URL" "http://${lan_ip:-localhost}:8080")
        ingest_token=$(grep ^INGEST_TOKEN ../.env | cut -d= -f2)
      fi
      sed -i.bak "s|^BACKEND_URL=.*|BACKEND_URL=$backend_url|" .env
      sed -i.bak "s|^INGEST_TOKEN=.*|INGEST_TOKEN=$ingest_token|" .env
      rm -f .env.bak
      station_loc=$(ask "Station location label (optional, e.g. Chandler)" "")
      [ -n "$station_loc" ] && echo "STATION_LOCATION=$station_loc" >> .env
      chmod 600 .env
      ok "relay/.env written"
    fi
    docker compose up -d --build
    ok "Relay container running. Logs: docker logs -f acurite-relay"
    cd ..
    echo
    warn "Two things you still need to do manually:"
    warn "  1. Add a DNS override in your router for atlasapi.myacurite.com"
    warn "     pointing to this machine's LAN IP ($lan_ip)."
    warn "     UniFi: Settings → Routing → DNS → Custom DNS Records"
    warn "     Pi-hole / dnsmasq: see relay/README.md for snippets."
    warn "  2. Power-cycle the AcuRite hub so it re-resolves DNS."
    warn "     Within ~1 min you should see live POSTs in 'docker logs -f acurite-relay'."
  else
    info "Skipping relay setup. On the other machine:"
    info "  cd relay && cp .env.example .env"
    info "  edit .env (set BACKEND_URL + INGEST_TOKEN)"
    info "  docker compose up -d"
  fi
fi

bold "Done!"
ok "iOS app: open Settings → paste Backend URL + Bearer Token from above."
echo
