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
# fly and flyctl are the same CLI under two names — which one is on PATH
# depends on how it was installed. setup-fly.sh and doctor.sh use `fly`,
# and this script used to hard-require `flyctl`, so the exact command the
# install summary points users at ("Upgrade later with: ./bin/upgrade.sh")
# failed with "Couldn't detect your deployment" on fly-only installs.
FLY="$(command -v fly || command -v flyctl || true)"
detect() {
  [ -f fly.toml ] && [ -n "$FLY" ] && { echo fly; return; }
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
  # setup-fly.sh pins your app name + region into fly.toml, which is a TRACKED
  # file — so every Fly install has a permanently dirty working tree, and
  # `git pull --ff-only` dead-ends the first time a release edits fly.toml.
  # Carry the pin across the pull instead of asking the user to fix git.
  fly_app=""; fly_region=""
  if [ -f fly.toml ] && ! git diff --quiet -- fly.toml 2>/dev/null; then
    fly_app="$(sed -n 's/^app[[:space:]]*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' fly.toml | head -1)"
    fly_region="$(sed -n 's/^primary_region[[:space:]]*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}.*/\1/p' fly.toml | head -1)"
    if [ -n "$fly_app" ]; then
      cp fly.toml fly.toml.bak
      info "Saved your fly.toml as fly.toml.bak (app=$fly_app region=${fly_region:-unset})."
      git checkout -- fly.toml
    else
      # No app name to restore afterwards, so don't throw the file away.
      fly_app=""
      err "fly.toml is modified but has no 'app = \"...\"' line to carry over."
      err "Sort that out by hand, then re-run."
      exit 1
    fi
  fi
  git pull --ff-only || {
    # Put the user's fly.toml back BEFORE bailing. We reset it to the tracked
    # version above so the pull could fast-forward; exiting here would leave
    # that pristine file in place with the app/region pin living only in
    # fly.toml.bak — and a re-run would then see a clean fly.toml, take the
    # `git diff --quiet` branch, leave fly_app empty, and never restore it. The
    # pin would be silently gone and the next deploy would target the wrong app.
    if [ -n "$fly_app" ] && [ -f fly.toml.bak ]; then
      cp fly.toml.bak fly.toml
      info "Restored your fly.toml (app=$fly_app) — nothing was lost."
    fi
    err "git pull failed. Your checkout has local commits or edits that can't"
    err "fast-forward. Run 'git status' to see them, or re-clone into a fresh"
    err "directory and re-run ./bin/setup-fly.sh (your Fly app and data are"
    err "untouched by either)."
    exit 1
  }
  if [ -n "$fly_app" ]; then
    tmp_toml=$(mktemp)
    sed -e "s/^app *=.*/app = \"$fly_app\"/" fly.toml > "$tmp_toml"
    if [ -n "$fly_region" ]; then
      sed -e "s/^primary_region *=.*/primary_region = \"$fly_region\"/" \
          "$tmp_toml" > "$tmp_toml.2" && mv "$tmp_toml.2" "$tmp_toml"
    fi
    mv "$tmp_toml" fly.toml
    info "Re-applied app/region to the new fly.toml. Only those two lines are"
    info "carried over — any OTHER edits you made are in fly.toml.bak."
  fi
fi

case "$MODE" in
  fly)
    if [ -z "$FLY" ]; then
      err "fly CLI not found (looked for both 'fly' and 'flyctl')."
      err "Install it — https://fly.io/docs/install/ — then re-run."
      exit 1
    fi
    info "Deploying to Fly.io…"
    "$FLY" deploy
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
    err "Couldn't detect your deployment (no fly.toml + fly/flyctl CLI, and no docker-compose.yml + docker)."
    err "Run with --fly or --docker, or upgrade manually:"
    # Not plain 'git pull' for Fly: setup-fly.sh edits the tracked fly.toml, so
    # a bare pull stops with "local changes" as soon as a release touches it.
    # NOT `git stash pop`: if the release also edits fly.toml (exactly when the
    # stash was needed) the pop conflicts, the && chain stops, and the user is
    # left mid-conflict with no deploy.
    err "  Fly:    cp fly.toml fly.toml.bak && git checkout -- fly.toml \\"
    err "            && git pull --ff-only && fly deploy"
    err "          then copy app/primary_region back from fly.toml.bak"
    err "  Docker: git pull && docker compose pull && docker compose up -d"
    exit 1
    ;;
esac
