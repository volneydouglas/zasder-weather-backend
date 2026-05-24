#!/usr/bin/env bash
# Zasder Weather — health checklist. Turns "I installed it but nothing
# shows up" into a checklist instead of a support thread.
#
# Usage:
#   ./bin/doctor.sh                 # auto-detect app from fly.toml
#   ./bin/doctor.sh --app NAME      # explicit Fly app name
#   ./bin/doctor.sh --url https://weather.example.com   # custom domain
#
# Token-dependent checks read the live values via `fly ssh console`
# (since `fly secrets list` only shows digests). To skip the SSH round
# trip, export API_TOKEN / INGEST_TOKEN in your shell first.

set -uo pipefail   # NOT -e: we want every check to run and report

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '  \033[33m!\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
PASS=0; FAIL=0; WARN=0

APP=""; URL=""
for arg in "$@"; do
  case "$arg" in
    --app=*) APP="${arg#*=}" ;;
    --app)   shift; APP="${1:-}" ;;
    --url=*) URL="${arg#*=}" ;;
    --url)   shift; URL="${1:-}" ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

# Allow `--app NAME` / `--url X` (space-separated) by re-scanning pairs.
prev=""
for arg in "$@"; do
  [ "$prev" = "--app" ] && APP="$arg"
  [ "$prev" = "--url" ] && URL="$arg"
  prev="$arg"
done

bold "Zasder Weather — doctor"
echo

# ── 1. fly CLI + auth ────────────────────────────────────────────────
if command -v fly >/dev/null; then ok "fly CLI installed"; else
  bad "fly CLI not found — https://fly.io/docs/install/"
  echo; bold "Can't continue without fly. $FAIL failed."; exit 1
fi
if fly auth whoami >/dev/null 2>&1; then ok "logged into Fly ($(fly auth whoami 2>/dev/null))"; else
  bad "not logged in — run: fly auth login"
fi

# ── 2. Resolve app + URL ─────────────────────────────────────────────
if [ -z "$APP" ] && [ -f fly.toml ]; then
  APP=$(grep -E '^app[[:space:]]*=' fly.toml | head -1 | sed -E 's/.*=[[:space:]]*"?([^"]+)"?.*/\1/')
fi
if [ -z "$APP" ]; then note "no app name (pass --app NAME or run from the repo with fly.toml)"; else ok "app: $APP"; fi
[ -z "$URL" ] && [ -n "$APP" ] && URL="https://$APP.fly.dev"
if [ -n "$URL" ]; then ok "backend URL: $URL"; else note "no URL resolved (pass --url)"; fi

if [ -n "$APP" ]; then
  if fly status --app "$APP" >/dev/null 2>&1; then ok "app exists on Fly"; else
    bad "fly status failed for '$APP' (wrong name, or not deployed?)"
  fi
fi

# ── 3. /healthz ──────────────────────────────────────────────────────
if [ -n "$URL" ]; then
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$URL/healthz" 2>/dev/null)
  if [ "$code" = "200" ]; then ok "/healthz → 200"; else
    bad "/healthz → ${code:-no response} (is the app awake at $URL ?)"
  fi
fi

# ── 4. Read live tokens (env override, else SSH) ─────────────────────
api_token="${API_TOKEN:-}"; ingest_token="${INGEST_TOKEN:-}"
ssh_env() { fly ssh console -a "$APP" -C "printenv $1" 2>/dev/null | tr -d '\r' | tail -1; }
if [ -n "$APP" ]; then
  [ -z "$api_token" ]    && api_token="$(ssh_env API_TOKEN)"
  [ -z "$ingest_token" ] && ingest_token="$(ssh_env INGEST_TOKEN)"
fi

# ── 5. API_TOKEN works ───────────────────────────────────────────────
devices_json=""
if [ -n "$URL" ] && [ -n "$api_token" ]; then
  resp=$(curl -s -m 10 -w $'\n%{http_code}' -H "Authorization: Bearer $api_token" "$URL/api/devices" 2>/dev/null)
  code="${resp##*$'\n'}"; devices_json="${resp%$'\n'*}"
  if [ "$code" = "200" ]; then ok "API_TOKEN accepted (GET /api/devices → 200)"; else
    bad "GET /api/devices → $code (API_TOKEN wrong, or backend down)"
  fi
else
  note "skipped API_TOKEN check (no token resolved — export API_TOKEN to test)"
fi

# ── 6. INGEST_TOKEN works (auth probe — empty body, expect 4xx not 401)
if [ -n "$URL" ] && [ -n "$ingest_token" ]; then
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $ingest_token" -H "Content-Type: application/json" \
    --data '{}' "$URL/ingest/custom" 2>/dev/null)
  case "$code" in
    401|403) bad "INGEST_TOKEN rejected ($code) — LilyGO boards can't post" ;;
    "")      bad "no response from /ingest/custom" ;;
    *)       ok "INGEST_TOKEN accepted (POST /ingest/custom → $code, auth ok)" ;;
  esac
else
  note "skipped INGEST_TOKEN check (no token resolved — export INGEST_TOKEN to test)"
fi

# ── 7. Volume mounted ────────────────────────────────────────────────
if [ -n "$APP" ]; then
  if fly volumes list --app "$APP" 2>/dev/null | grep -q weather_data; then
    ok "weather_data volume present"
  else
    bad "weather_data volume not found (DB won't persist across restarts)"
  fi
fi

# ── 8. Configured pollers ────────────────────────────────────────────
if [ -n "$APP" ]; then
  [ -n "$(ssh_env AW_APPLICATION_KEY)" ] && ok "AmbientWeather poller configured" \
    || note "AmbientWeather poller not configured (fine if you don't use it)"
  [ -n "$(ssh_env WEATHERLINK_API_KEY)" ] && ok "Davis WeatherLink poller configured" \
    || note "Davis WeatherLink poller not configured (fine if you don't use it)"
fi

# ── 9. Recent data (device count + freshest obs age) ─────────────────
if [ -n "$devices_json" ] && command -v python3 >/dev/null; then
  python3 - "$devices_json" <<'PY' && DATA_RC=$? || DATA_RC=$?
import json, sys, time
try:
    devs = json.loads(sys.argv[1])
except Exception:
    print("  ! /api/devices returned non-JSON"); sys.exit(2)
devs = devs if isinstance(devs, list) else devs.get("devices", [])
if not devs:
    print("  ! no devices yet — nothing has posted (check a source/board)"); sys.exit(2)
now = time.time(); freshest = None
def ts(d):
    for k in ("dateutc","last_seen","timestamp","ts"):
        v = (d.get("latest") or d).get(k) if isinstance(d.get("latest") or d, dict) else None
        if isinstance(v,(int,float)): return v/1000 if v>1e12 else v
    return None
for d in devs:
    t = ts(d)
    if t and (freshest is None or t>freshest): freshest=t
label = f"{len(devs)} device(s)"
if freshest:
    mins=int((now-freshest)/60)
    label += f", freshest obs {mins} min ago"
    print(f"  {'✓' if mins<15 else '!'} {label}")
    sys.exit(0 if mins<15 else 2)
print(f"  ✓ {label}"); sys.exit(0)
PY
  case "${DATA_RC:-1}" in 0) PASS=$((PASS+1)) ;; *) WARN=$((WARN+1)) ;; esac
elif [ -n "$devices_json" ]; then
  n=$(printf '%s' "$devices_json" | grep -o '"mac"' | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] && ok "$n device(s) reported by /api/devices" \
    || note "no devices yet — nothing has posted"
fi

echo
bold "Summary: $PASS passed, $WARN warnings, $FAIL failed"
[ "$FAIL" -eq 0 ] || { echo; printf '\033[31mSome checks failed — see ✗ lines above.\033[0m\n'; exit 1; }
