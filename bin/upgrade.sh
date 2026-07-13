#!/usr/bin/env bash
# Upgrade the Zasder Weather backend to the latest release.
#
# Auto-detects your deployment:
#   * Fly.io  → git pull + fly deploy
#   * Docker  → pull the new published image (or rebuild) + recreate
# The SQLite schema auto-migrates on boot (idempotent CREATE/ALTER), so no
# manual migration step. Your data volume is untouched.
#
# Usage:  bin/upgrade.sh            # auto-detect
#         bin/upgrade.sh --fly      # force Fly.io path
#         bin/upgrade.sh --docker   # force Docker path
set -euo pipefail
cd "$(dirname "$0")/.."

info() { printf '\033[1;34m›\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

MODE="${1:-auto}"
detect() {
  [ -f fly.toml ] && command -v flyctl >/dev/null 2>&1 && { echo fly; return; }
  [ -f docker-compose.yml ] && command -v docker >/dev/null 2>&1 && { echo docker; return; }
  echo unknown
}
case "$MODE" in
  --fly)    MODE=fly ;;
  --docker) MODE=docker ;;
  auto|"")  MODE="$(detect)" ;;
esac

# Show what's running vs latest (best-effort; needs the app reachable/curl+jq not required).
info "Pulling the latest source…"
if [ -d .git ]; then
  git pull --ff-only || { err "git pull failed (local changes?). Resolve, then re-run."; exit 1; }
fi

case "$MODE" in
  fly)
    info "Deploying to Fly.io…"
    flyctl deploy
    info "Done. Check the status page or:  fly logs"
    ;;
  docker)
    # If docker-compose pins a published image, pull it; otherwise rebuild.
    if grep -qE '^\s*image:\s*ghcr\.io' docker-compose.yml; then
      info "Pulling the published image…"
      docker compose pull
    else
      info "Rebuilding the image from source…"
      docker compose build
    fi
    info "Recreating the container…"
    docker compose up -d
    info "Done. Verify:  curl -s localhost:8080/healthz"
    ;;
  *)
    err "Couldn't detect your deployment (no fly.toml+flyctl or docker-compose.yml+docker)."
    err "Run with --fly or --docker, or upgrade manually:"
    err "  Fly:    git pull && fly deploy"
    err "  Docker: git pull && docker compose pull && docker compose up -d"
    exit 1
    ;;
esac
