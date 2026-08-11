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
#
# -e as well as -u/pipefail: a failed cp/mkdir/plist-write/chmod used to
# print its error and then march on to "Done." — a broken install reported
# as success. Steps that are ALLOWED to fail are marked `|| true` (or run
# inside if/&&/|| where -e doesn't apply).
set -euo pipefail

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

# Every prompt goes through this. A bare `read` in a retry loop spins forever
# printing errors if stdin closes (Ctrl-D, or a piped run that runs dry), so
# EOF has to abort rather than fall through with an empty value.
ask() {  # ask <varname> <prompt>
  local __var="$1" __prompt="$2" __val
  if ! read -r -p "$__prompt" __val; then
    echo
    bad "Input closed — nothing was installed. Re-run when you're ready."
    exit 1
  fi
  printf -v "$__var" '%s' "$__val"
}

# Secrets go through this instead: -s stops the token from being echoed to
# the terminal, scrollback, and any session recording. The trailing echo
# restores the newline that -s swallows.
ask_secret() {  # ask_secret <varname> <prompt>
  local __var="$1" __prompt="$2" __val
  if ! read -r -s -p "$__prompt" __val; then
    echo
    bad "Input closed — nothing was installed. Re-run when you're ready."
    exit 1
  fi
  echo
  printf -v "$__var" '%s' "$__val"
}

# ── Backend-URL validation ────────────────────────────────────────────────
# Normalizes the scheme, strips a trailing slash, and refuses cleartext
# http:// to anything that isn't a private/LAN address (the poller sends
# "Authorization: Bearer <INGEST_TOKEN>" on every tick — over public http
# that write credential is readable by anyone on the path).
#
# A function (not inline in the prompt loop) so `--check-url` below can
# drive it from tests. Sets VALIDATED_URL on success; on failure sets
# URL_FAIL_REASON (empty|scheme|userinfo|cleartext) and URL_FAIL_HOST.
validate_backend_url() {  # validate_backend_url <url>
  local url="$1" url_auth url_host o1 o2 rest lan_ok
  VALIDATED_URL=""; URL_FAIL_REASON=""; URL_FAIL_HOST=""
  url="${url%/}"
  # Normalise the scheme's case before anything inspects it. "HTTP://host"
  # otherwise fell through to the catch-all below and became
  # "https://HTTP://host" — nonsense that then skipped the cleartext check.
  case "$url" in
    [Hh][Tt][Tt][Pp]://*)
      url="http://${url#*://}" ;;
    [Hh][Tt][Tt][Pp][Ss]://*)
      url="https://${url#*://}" ;;
    "") URL_FAIL_REASON="empty"; return 1 ;;
    *://*)
      URL_FAIL_REASON="scheme"; return 1 ;;
    *) url="https://$url" ;;
  esac
  if [ "${url#http://}" != "$url" ]; then
    # Resolve the AUTHORITY down to a bare host before judging it. Matching
    # the authority string directly let two public hosts through:
    #   10.attacker.example        — matched the old "10.*" glob, but "10."
    #                                here is a DNS label, not an octet
    #   10.0.0.1@attacker.example  — userinfo; the real host is after the "@"
    url_auth="${url#http://}"; url_auth="${url_auth%%/*}"
    case "$url_auth" in
      *@*) URL_FAIL_REASON="userinfo"; return 1 ;;
    esac
    # Strip the port. Bracketed IPv6 keeps its brackets.
    case "$url_auth" in
      \[*\]*) url_host="${url_auth%%\]*}]" ;;
      *)       url_host="${url_auth%%:*}" ;;
    esac

    lan_ok=0
    # A dotted quad is checked NUMERICALLY, so "10.attacker.example" — which
    # is not four numeric octets — can never pass as 10.0.0.0/8.
    if printf '%s' "$url_host" | /usr/bin/grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
      o1="${url_host%%.*}"; rest="${url_host#*.}"
      o2="${rest%%.*}"
      case "$o1" in
        10)  lan_ok=1 ;;
        127) lan_ok=1 ;;
        192) [ "$o2" = "168" ] && lan_ok=1 ;;
        172) { [ "$o2" -ge 16 ] && [ "$o2" -le 31 ]; } 2>/dev/null && lan_ok=1 ;;
      esac
    else
      case "$url_host" in
        localhost|\[::1\]|::1)   lan_ok=1 ;;
        *.local)                 lan_ok=1 ;;   # mDNS, link-local by definition
        # Private IPv6: ULA fc00::/7 ([fcxx:...], [fdxx:...]) and link-local
        # fe80::/10. These are LAN by definition, same as the IPv4 ranges
        # above — the original allowlist covered only v4 and rejected every
        # v6 LAN backend as "not private".
        \[[Ff][CcDd]*)           lan_ok=1 ;;
        \[[Ff][Ee]8*|\[[Ff][Ee]9*|\[[Ff][Ee][AaBb]*) lan_ok=1 ;;
      esac
    fi

    if [ "$lan_ok" -ne 1 ]; then
      URL_FAIL_REASON="cleartext"; URL_FAIL_HOST="$url_host"; return 1
    fi
  fi
  VALIDATED_URL="$url"
  return 0
}

unload_agent() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null \
    || launchctl unload "$PLIST" 2>/dev/null || true
}

# Hidden test seam: validate one URL and exit. Prints the normalized URL on
# success; prints the failure reason (empty|scheme|userinfo|cleartext) on
# failure. Lets the test suite exercise the cleartext allowlist without
# driving the interactive prompts.
if [ "${1:-}" = "--check-url" ]; then
  if validate_backend_url "${2:-}"; then
    echo "$VALIDATED_URL"; exit 0
  else
    echo "$URL_FAIL_REASON"; exit 1
  fi
fi

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
  ask WLL_HOST "  WeatherLink Live IP address: "
  WLL_HOST="$(echo "$WLL_HOST" | tr -d '[:space:]')"
  [ -z "$WLL_HOST" ] && { bad "Please enter an address."; continue; }
  printf '  checking http://%s/v1/current_conditions ... ' "$WLL_HOST"
  # Match WLL-specific response keys, not the bare substring "data" — any
  # router/captive-portal page containing the word "data" used to pass as a
  # WeatherLink Live. "did" (device id) is present even when no sensors have
  # reported yet; "data_structure_type" appears once conditions exist.
  if curl -fsS -m 8 "http://$WLL_HOST/v1/current_conditions" 2>/dev/null \
      | grep -qE '"data_structure_type"|"did"'; then
    printf '\033[32mreached it\033[0m\n'; break
  fi
  printf '\033[31mno response\033[0m\n'
  info "Couldn't reach a WeatherLink Live there. Check the address and that"
  info "this Mac is on the same network/Wi-Fi as the WLL, then try again."
done

# ── 2. Backend ───────────────────────────────────────────────────────────
echo
bold "2. Your backend"
info "This is the address of the server you deployed — NOT an address on your"
info "home network. If you used Fly.io it looks like:"
info "    https://your-app-name.fly.dev"
info "It's written in zasder-install-summary.txt from the backend setup."
echo
while :; do
  ask BACKEND_URL "  Backend URL: "
  BACKEND_URL="$(echo "$BACKEND_URL" | tr -d '[:space:]')"
  HAD_SCHEME=1
  case "$BACKEND_URL" in *://*) : ;; "") : ;; *) HAD_SCHEME=0 ;; esac
  if ! validate_backend_url "$BACKEND_URL"; then
    case "$URL_FAIL_REASON" in
      empty)  bad "Please enter a URL." ;;
      scheme) bad "Only http:// and https:// are supported." ;;
      userinfo)
        bad "That address has a username in it (the part before \"@\")."
        info "Enter just the host, like http://192.168.1.50:8080" ;;
      cleartext)
        bad "http:// would send your ingest token in cleartext on every reading."
        info "Only private/LAN addresses may use http:// — this one ($URL_FAIL_HOST) is"
        info "not one. Use the https:// address instead; Fly.io backends serve https:"
        info "    https://$URL_FAIL_HOST" ;;
    esac
    continue
  fi
  BACKEND_URL="$VALIDATED_URL"
  [ "$HAD_SCHEME" = 0 ] && info "assuming https:// → $BACKEND_URL"
  printf '  checking %s/healthz ... ' "$BACKEND_URL"
  if curl -fsS -m 12 "$BACKEND_URL/healthz" >/dev/null 2>&1; then
    printf '\033[32mreached it\033[0m\n'; break
  fi
  printf '\033[31mno response\033[0m\n'
  # A private/LAN address here is almost always the WeatherLink Live's IP or a
  # guess, not the deployed backend — say so rather than "check the address".
  case "$BACKEND_URL" in
    http://10.*|http://192.168.*|http://172.1[6-9].*|http://172.2[0-9].*|\
    http://172.3[01].*|http://localhost*|http://127.*|https://10.*|https://192.168.*)
      bad "That's an address on your home network, not your deployed backend."
      info "Unless you deliberately self-hosted on your own LAN, you want the"
      info "public address from the backend setup — usually"
      info "    https://your-app-name.fly.dev"
      info "Find it with:  fly apps list      (the app you created)"
      ;;
    *)
      info "Couldn't reach that backend. Check the address, and that the deploy"
      info "actually finished — 'fly status -a your-app-name' should show the"
      info "machine 'started'. If it's crash-looping, run 'fly logs -a your-app-name'."
      ;;
  esac
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
  # ask_secret: the token is a write credential — don't echo it into the
  # terminal, scrollback, or a recorded session.
  ask_secret INGEST_TOKEN "  INGEST_TOKEN (input hidden): "
  INGEST_TOKEN="$(echo "$INGEST_TOKEN" | tr -d '[:space:]')"
  [ -n "$INGEST_TOKEN" ] && break
  bad "Please paste the token."
done

# ── 4. Station name ──────────────────────────────────────────────────────
echo
bold "4. Station name"
info "What this station should be called in the app. Press return for the default."
echo
ask WLL_DEVICE_NAME "  Station name [Davis WeatherLink Live]: "
WLL_DEVICE_NAME="${WLL_DEVICE_NAME:-Davis WeatherLink Live}"

# Give this install its own device identity. poller.py's built-in default is a
# single fixed MAC, which is fine for one person but means every install in the
# world claims the SAME station id — on a shared/hosted backend the second user
# to connect would collide with the first. Derived once here and pinned in the
# LaunchAgent, so re-running setup keeps the same station rather than creating
# a duplicate. Reuses the existing value if this Mac is already set up.
if [ -f "$PLIST" ]; then
  EXISTING_MAC="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:WLL_DEVICE_MAC' "$PLIST" 2>/dev/null || true)"
  if [ -z "$EXISTING_MAC" ]; then
    # Installed before this script pinned an id, so it has been reporting
    # under poller.py's built-in default. Keep that: minting a fresh one on
    # upgrade would start a SECOND station and strand the existing history.
    EXISTING_MAC="5D:5D:05:00:00:01"
    info "keeping the station id this Mac already reports under ($EXISTING_MAC)"
  fi
fi
if [ -n "${EXISTING_MAC:-}" ]; then
  WLL_DEVICE_MAC="$EXISTING_MAC"
  info "keeping this Mac's existing station id ($WLL_DEVICE_MAC)"
else
  # 5D:5D:05 is the project's WeatherLink-Live prefix; the last three bytes
  # are random so two installs practically never collide.
  WLL_DEVICE_MAC="5D:5D:05:$("$PYBIN" -c '
import secrets
print(":".join(f"{b:02X}" for b in secrets.token_bytes(3)))')"
fi

# Send exactly one real reading through the poller's own code path, so a bad
# token surfaces HERE instead of as silence hours from now.
#
# Capture STDOUT ONLY. The old `2>&1` merged the poller's log lines (which go
# to stderr — e.g. the multi-transmitter warning that fires on the first
# to_observation() call) into RESULT ahead of the status token, so the exact-
# match case below stopped recognising "HTTP 401" and a bad token installed
# silently. Stderr now passes through to the terminal, where a warning is
# useful anyway. The status token is printed on the LAST stdout line, and the
# patterns match anywhere in RESULT as a second line of defence.
echo
printf '  sending one test reading ... '
RESULT=$(WLL_HOST="$WLL_HOST" BACKEND_URL="$BACKEND_URL" \
         INGEST_TOKEN="$INGEST_TOKEN" WLL_DEVICE_NAME="$WLL_DEVICE_NAME" \
         WLL_DEVICE_MAC="$WLL_DEVICE_MAC" \
         "$PYBIN" - "$SRC_DIR" <<'PY'
import sys, urllib.error
sys.path.insert(0, sys.argv[1])
import poller
try:
    obs = poller.to_observation(poller.fetch_wll())
    if not obs:
        print("NODATA"); raise SystemExit
    poller.post_observation(obs)
    t = (obs.get("outdoor") or {}).get("tempf")
    # OKDATA = accepted but the ISS hasn't reported a temperature yet.
    # Printing "OK None" here read as "it read None°F".
    print(f"OK {t}" if t is not None else "OKDATA")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}")
except Exception as e:
    print(f"ERR {e}")
PY
) || true
case "$RESULT" in
  *"HTTP 401"*|*"HTTP 403"*)
            printf '\033[31mrejected\033[0m\n'
            bad "The backend refused that token."
            info "Check you used INGEST_TOKEN (not API_TOKEN — they're different)."
            exit 1 ;;
  *OKDATA*) printf '\033[32maccepted\033[0m — no temperature reading yet\n'
            info "The backend accepted the post; the station hasn't reported a"
            info "temperature yet. Continuing — it'll fill in as sensors report." ;;
  "OK "*)   printf '\033[32maccepted\033[0m — it read %s°F\n' "${RESULT#OK }" ;;
  *NODATA*) printf '\033[33mno sensor data yet\033[0m\n'
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
    <key>WLL_HOST</key><string>$(xml_escape "$WLL_HOST")</string>
    <key>BACKEND_URL</key><string>$(xml_escape "$BACKEND_URL")</string>
    <key>INGEST_TOKEN</key><string>$(xml_escape "$INGEST_TOKEN")</string>
    <key>WLL_DEVICE_NAME</key><string>$(xml_escape "$WLL_DEVICE_NAME")</string>
    <key>WLL_DEVICE_MAC</key><string>$(xml_escape "$WLL_DEVICE_MAC")</string>
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

# `|| true` matters under set -e: if BOTH bootstrap and load fail we still
# want to reach the diagnostics below ("poller didn't start — see the log")
# rather than dying silently between them.
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
  || launchctl load -w "$PLIST" 2>/dev/null || true
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
