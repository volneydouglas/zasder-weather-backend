#!/usr/bin/env bash
# Set up the WeatherLink Live poller on a Mac — no Docker, no Homebrew, no pip.
#
# poller.py is pure-stdlib Python, and macOS already has python3, so all this
# does is ask three questions, check they work, and register a LaunchAgent so
# the poller starts at login and restarts if it ever stops.
#
#   bash bin/setup-macos.sh
#
# Undo everything:  bash bin/setup-macos.sh --uninstall
set -uo pipefail

LABEL="com.zasder.wll-poller"
SUPPORT_DIR="$HOME/Library/Application Support/ZasderWeather"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/zasder-wll-poller.log"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()   { printf '  \033[31m✗\033[0m %s\n' "$*"; }
info()  { printf '  %s\n' "$*"; }

# A station name is free text and can legitimately contain & or < — which
# would produce a plist launchd refuses to parse.
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

unload_agent() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null \
    || launchctl unload "$PLIST" 2>/dev/null || true
}

if [ "${1:-}" = "--uninstall" ]; then
  bold "Removing the WeatherLink Live poller"
  unload_agent
  rm -f "$PLIST"
  info "Removed the startup entry. Your settings are still in:"
  info "  $SUPPORT_DIR"
  info "Delete that folder too if you want it completely gone."
  exit 0
fi

bold "Zasder Weather — WeatherLink Live poller setup (macOS)"
echo
info "This connects your Davis WeatherLink Live to your backend."
info "It runs quietly in the background and starts automatically at login."
echo

# Prefer Apple's /usr/bin/python3 — it's a stable absolute path that a
# Homebrew upgrade can't move out from under the LaunchAgent later.
if /usr/bin/python3 -c 'pass' 2>/dev/null; then
  PYBIN=/usr/bin/python3
elif PYBIN="$(command -v python3)"; then
  :
else
  bad "python3 not found."
  info "Install Apple's command line tools first, then re-run this:"
  info "    xcode-select --install"
  exit 1
fi
ok "python3 found ($("$PYBIN" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))') at $PYBIN)"

# ── 1. WeatherLink Live address ──────────────────────────────────────────
echo
bold "1. Your WeatherLink Live"
info "The IP address of the little WeatherLink Live box on your network."
info "Find it in the WeatherLink app: Account → Devices → your WLL → Device Info,"
info "or in your router's list of connected devices. Looks like 192.168.1.42"
echo
while :; do
  read -r -p "  WeatherLink Live IP address: " WLL_HOST
  WLL_HOST="$(echo "$WLL_HOST" | tr -d '[:space:]')"
  [ -z "$WLL_HOST" ] && { bad "Please enter an address."; continue; }
  printf '  checking http://%s/v1/current_conditions ... ' "$WLL_HOST"
  if curl -fsS -m 8 "http://$WLL_HOST/v1/current_conditions" 2>/dev/null | grep -q '"data"'; then
    printf '\033[32mreached it\033[0m\n'; break
  fi
  printf '\033[31mno response\033[0m\n'
  info "Couldn't reach a WeatherLink Live there. Check the address and that"
  info "this Mac is on the same network/Wi-Fi as the WLL, then try again."
done

# ── 2. Backend ───────────────────────────────────────────────────────────
echo
bold "2. Your backend"
info "The https:// address of the backend you deployed (Fly.io or otherwise)."
echo
while :; do
  read -r -p "  Backend URL: " BACKEND_URL
  BACKEND_URL="$(echo "$BACKEND_URL" | tr -d '[:space:]')"
  BACKEND_URL="${BACKEND_URL%/}"
  case "$BACKEND_URL" in
    http://*|https://*) : ;;
    "") bad "Please enter a URL."; continue ;;
    *) BACKEND_URL="https://$BACKEND_URL"; info "assuming https:// → $BACKEND_URL" ;;
  esac
  printf '  checking %s/healthz ... ' "$BACKEND_URL"
  if curl -fsS -m 12 "$BACKEND_URL/healthz" >/dev/null 2>&1; then
    printf '\033[32mreached it\033[0m\n'; break
  fi
  printf '\033[31mno response\033[0m\n'
  info "Couldn't reach that backend. Check the address and that it's deployed."
done

# ── 3. Ingest token ──────────────────────────────────────────────────────
echo
bold "3. Your ingest token"
info "The INGEST_TOKEN from your backend setup — a long random string."
info "The installer saved it in a file called zasder-install-summary.txt,"
info "inside the folder you downloaded. To see it, run:"
info "    open \"\$(find ~ -name zasder-install-summary.txt -maxdepth 6 2>/dev/null | head -1)\""
echo
while :; do
  read -r -p "  INGEST_TOKEN: " INGEST_TOKEN
  INGEST_TOKEN="$(echo "$INGEST_TOKEN" | tr -d '[:space:]')"
  [ -n "$INGEST_TOKEN" ] && break
  bad "Please paste the token."
done

# ── 4. Station name ──────────────────────────────────────────────────────
echo
bold "4. Station name"
info "What this station should be called in the app. Press return for the default."
echo
read -r -p "  Station name [Davis WeatherLink Live]: " WLL_DEVICE_NAME
WLL_DEVICE_NAME="${WLL_DEVICE_NAME:-Davis WeatherLink Live}"

# Send exactly one real reading through the poller's own code path, so a bad
# token surfaces HERE instead of as silence hours from now.
echo
printf '  sending one test reading ... '
RESULT=$(WLL_HOST="$WLL_HOST" BACKEND_URL="$BACKEND_URL" \
         INGEST_TOKEN="$INGEST_TOKEN" WLL_DEVICE_NAME="$WLL_DEVICE_NAME" \
         python3 - "$SRC_DIR" <<'PY' 2>&1
import sys, urllib.error
sys.path.insert(0, sys.argv[1])
import poller
try:
    obs = poller.to_observation(poller.fetch_wll())
    if not obs:
        print("NODATA"); raise SystemExit
    poller.post_observation(obs)
    t = (obs.get("outdoor") or {}).get("tempf")
    print(f"OK {t}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}")
except Exception as e:
    print(f"ERR {e}")
PY
)
case "$RESULT" in
  OK*)      printf '\033[32maccepted\033[0m — it read %s°F\n' "${RESULT#OK }" ;;
  "HTTP 401"|"HTTP 403")
            printf '\033[31mrejected\033[0m\n'
            bad "The backend refused that token."
            info "Check you used INGEST_TOKEN (not API_TOKEN — they're different)."
            exit 1 ;;
  NODATA)   printf '\033[33mno sensor data yet\033[0m\n'
            info "The WeatherLink Live answered but hasn't heard from the weather"
            info "station yet. Continuing — it'll start posting once it does." ;;
  *)        printf '\033[33m%s\033[0m\n' "$RESULT"
            info "Continuing anyway — the poller will log any problem." ;;
esac

# ── Install ──────────────────────────────────────────────────────────────
echo
bold "Installing"
mkdir -p "$SUPPORT_DIR" "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
cp "$SRC_DIR/poller.py" "$SUPPORT_DIR/poller.py"
ok "copied the poller to $SUPPORT_DIR"

unload_agent   # replace any previous install cleanly

# The poller reads plain environment variables, so launchd can supply them
# directly — no .env file to hand-edit.
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYBIN</string>
    <string>$SUPPORT_DIR/poller.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WLL_HOST</key><string>$WLL_HOST</string>
    <key>BACKEND_URL</key><string>$BACKEND_URL</string>
    <key>INGEST_TOKEN</key><string>$INGEST_TOKEN</string>
    <key>WLL_DEVICE_NAME</key><string>$(xml_escape "$WLL_DEVICE_NAME")</string>
    <key>WLL_POLL_SECONDS</key><string>10</string>
  </dict>
  <key>ProcessType</key><string>Background</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF
chmod 600 "$PLIST"          # it holds your token
ok "created the startup entry"

launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
  || launchctl load -w "$PLIST" 2>/dev/null
sleep 3
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  ok "poller is running"
else
  bad "poller didn't start — see the log below"
fi

echo
bold "Done."
info "It runs in the background, starts at login, and restarts by itself."
echo
info "Watch it live:    tail -f \"$LOG\""
info "Stop / remove:    bash bin/setup-macos.sh --uninstall"
echo
bold "Last few log lines:"
sleep 4
tail -n 12 "$LOG" 2>/dev/null | sed 's/^/  /' || info "(nothing logged yet — give it a few seconds)"
echo
info "Your station should appear in the app within a minute."
