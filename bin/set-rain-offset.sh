#!/usr/bin/env bash
# Calibrate a LilyGO/RF device's yearly-rain so the iOS app shows real
# year-to-date inches instead of the sensor's lifetime cumulative counter.
#
# LilyGO boards POST the sensor's RAW LIFETIME rain total. The backend
# stores  yearly_in = max(0, posted - offset[mac]).  This helper computes
# the offset and merges it into the INGEST_YEARLY_RAIN_OFFSETS Fly secret
# (preserving any other MACs already configured).
#
# Usage:
#   ./bin/set-rain-offset.sh <MAC> <current-lifetime-rain-in> [--ytd=ACTUAL] [--app=NAME]
#
#   <MAC>                     device id, colonized or compact (case-insensitive)
#   <current-lifetime-rain-in> the yearlyrainin the board is posting right now
#                             (read it: curl .../api/devices/<MAC>/current)
#   --ytd=ACTUAL              your true year-to-date inches (default 0 = "start
#                             counting from zero as of now")
#   --app=NAME                Fly app (default: read from fly.toml)
#
# offset = lifetime - ytd. Example: board posts 3.58", real YTD is 0.73"
#   ./bin/set-rain-offset.sh 5D:5D:01:00:02:C7 3.58 --ytd=0.73   # → offset 2.85

set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$APP_DIR"
err() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }
bold(){ printf '\033[1m%s\033[0m\n' "$*"; }

command -v fly >/dev/null     || { err "fly CLI not found"; exit 1; }
command -v python3 >/dev/null || { err "python3 required for JSON merge"; exit 1; }

MAC=""; LIFETIME=""; YTD="0"; APP=""
for arg in "$@"; do
  case "$arg" in
    --ytd=*) YTD="${arg#*=}" ;;
    --app=*) APP="${arg#*=}" ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --*) err "unknown flag: $arg"; exit 2 ;;
    *) if [ -z "$MAC" ]; then MAC="$arg"; elif [ -z "$LIFETIME" ]; then LIFETIME="$arg"; fi ;;
  esac
done
[ -n "$MAC" ] && [ -n "$LIFETIME" ] || { err "need <MAC> and <current-lifetime-rain-in>"; sed -n '11,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

if [ -z "$APP" ] && [ -f fly.toml ]; then
  APP=$(grep -E '^app[[:space:]]*=' fly.toml | head -1 | sed -E 's/.*=[[:space:]]*"?([^"]+)"?.*/\1/')
fi
[ -n "$APP" ] || { err "no app (pass --app NAME or run from repo with fly.toml)"; exit 1; }

# Normalize MAC to UPPERCASE colonized — matches the backend validator.
mac_norm=$(printf '%s' "$MAC" | tr 'a-z' 'A-Z' | tr -d ':-')
if printf '%s' "$mac_norm" | grep -qE '^[0-9A-F]{12}$'; then
  mac_norm=$(printf '%s' "$mac_norm" | sed -E 's/(..)(..)(..)(..)(..)(..)/\1:\2:\3:\4:\5:\6/')
else
  err "MAC '$MAC' is not 12 hex digits"; exit 1
fi

offset=$(python3 -c "print(round(float('$LIFETIME') - float('$YTD'), 4))")
bold "Calibrating $mac_norm:  lifetime $LIFETIME - YTD $YTD  →  offset $offset"

current=$(fly ssh console -a "$APP" -C 'printenv INGEST_YEARLY_RAIN_OFFSETS' 2>/dev/null | tr -d '\r' | tail -1 || true)
merged=$(python3 - "$current" "$mac_norm" "$offset" <<'PY'
import json, sys
cur, mac, off = sys.argv[1], sys.argv[2], float(sys.argv[3])
try:
    d = json.loads(cur) if cur.strip() else {}
    if not isinstance(d, dict): d = {}
except Exception:
    d = {}
d[mac] = off
print(json.dumps(d, separators=(",", ":")))
PY
)

bold "New INGEST_YEARLY_RAIN_OFFSETS:"
printf '  %s\n' "$merged"
fly secrets set --app "$APP" "INGEST_YEARLY_RAIN_OFFSETS=$merged"
echo
bold "Done. The machine restarts automatically; new posts for $mac_norm will offset."
