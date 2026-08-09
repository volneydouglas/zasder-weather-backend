#!/bin/bash
# Double-clickable macOS setup for Zasder Weather.
#
# The point of this file is that the user never types a command. Finder runs
# it, it opens Terminal itself, and it handles the three things that actually
# stopped people: installing flyctl, signing in to Fly, and knowing which
# script to run next. The real work still lives in bin/setup-fly.sh and
# wll-poller/bin/setup-macos.sh — this is a friendly front door, not a fork.
#
# Double-click it in Finder. If macOS says it's from an unidentified
# developer, right-click the file and choose Open instead.
set -uo pipefail

# Finder launches .command files with the HOME directory as the working
# directory, not the folder the file is sitting in.
cd "$(dirname "$0")" || exit 1

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()   { printf '  \033[31m✗\033[0m %s\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
rule()  { printf '\033[2m%s\033[0m\n' "────────────────────────────────────────────────────────"; }

# Terminal closes on exit depending on the user's profile settings, and an
# error scrolling past unread is exactly how someone ends up stuck. Always
# hold the window open.
pause_exit() {
  echo
  rule
  printf 'Press return to close this window. '
  read -r _ || true
  exit "${1:-0}"
}
trap 'pause_exit $?' EXIT

ask() {  # ask <varname> <prompt> — EOF aborts rather than looping on empty
  local __var="$1" __prompt="$2" __val
  if ! read -r -p "$__prompt" __val; then
    echo; bad "Input closed — stopping here. Nothing was broken."; exit 1
  fi
  printf -v "$__var" '%s' "$__val"
}

confirm() {  # confirm <prompt> — default yes
  local a
  ask a "$1 [Y/n]: "
  case "$(echo "$a" | tr '[:upper:]' '[:lower:]')" in n|no) return 1 ;; *) return 0 ;; esac
}

clear
bold "Zasder Weather — Setup"
echo
info "This sets up your own private weather server and connects your station."
info "It will ask you a few questions. Type your answers and press return."
info "Nothing here is permanent — you can undo any of it later."
echo
rule
echo

# ── Sanity: are we in the right folder? ──────────────────────────────────
if [ ! -f "bin/setup-fly.sh" ]; then
  bad "This file has been moved out of its folder."
  info "It needs to stay inside the zasder-weather-backend folder, next to"
  info "the 'bin' folder. Move it back and double-click it again."
  exit 1
fi
ok "found the setup files"

# ── Step 1: flyctl ───────────────────────────────────────────────────────
# Fly's installer puts flyctl here; pick it up even if the user's shell
# profile hasn't been reloaded since a previous run.
export FLYCTL_INSTALL="${FLYCTL_INSTALL:-$HOME/.fly}"
export PATH="$FLYCTL_INSTALL/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if command -v fly >/dev/null 2>&1; then
  ok "Fly.io command line tool is installed"
else
  echo
  bold "Step 1 of 4 — install the Fly.io tool"
  info "Your weather server runs on Fly.io. Their free tier is enough."
  info "This installs their official command line tool into your home folder"
  info "(no admin password needed, nothing outside your account is touched)."
  echo
  if ! confirm "  Install it now?"; then
    bad "Can't continue without it."; exit 1
  fi
  echo
  # Fly's documented installer. Piped to sh because that is the method they
  # publish; the URL is theirs, over HTTPS.
  if curl -fsSL https://fly.io/install.sh | sh; then
    export PATH="$FLYCTL_INSTALL/bin:$PATH"
  fi
  if command -v fly >/dev/null 2>&1; then
    ok "installed"
  else
    bad "The install didn't finish."
    info "Check your internet connection and double-click this file again."
    exit 1
  fi
fi

# A deploy token in the environment can create an app but not read it back,
# which surfaces later as a pile of confusing 'unauthorized' errors during
# deploy. Catch it here instead.
if [ -n "${FLY_API_TOKEN:-}${FLY_ACCESS_TOKEN:-}" ]; then
  echo
  bad "A Fly access token is set in your environment."
  info "That token can create your app but not read it back, which shows up"
  info "later as confusing 'unauthorized' errors. Ignoring it for this run."
  unset FLY_API_TOKEN FLY_ACCESS_TOKEN
fi

# ── Step 2: sign in ──────────────────────────────────────────────────────
echo
bold "Step 2 of 4 — sign in to Fly.io"
if fly auth whoami >/dev/null 2>&1; then
  ok "already signed in as $(fly auth whoami 2>/dev/null)"
else
  info "Your browser will open. Sign in (or create a free account), then come"
  info "back to this window."
  echo
  confirm "  Open the browser now?" || { bad "Can't continue without signing in."; exit 1; }
  fly auth login || true
  if fly auth whoami >/dev/null 2>&1; then
    ok "signed in as $(fly auth whoami 2>/dev/null)"
  else
    bad "Still not signed in."
    info "Double-click this file again once you've finished signing in."
    exit 1
  fi
fi

# ── Step 3: the backend ──────────────────────────────────────────────────
echo
bold "Step 3 of 4 — create your weather server"
info "The next part asks a few questions of its own — an app name, a region,"
info "and which weather stations you have."
echo
info "IMPORTANT: those are questions, not places to paste commands. Type a"
info "short answer and press return. For the app name, letters, numbers and"
info "dashes only, for example:  zasder-weather-home"
echo
rule
bash bin/setup-fly.sh
SETUP_RC=$?
rule
if [ "$SETUP_RC" -ne 0 ]; then
  echo
  bad "The server setup didn't finish."
  info "Nothing is broken — you can double-click this file again to retry."
  info "If it keeps failing, send the last 20 lines above to the developer."
  exit 1
fi
ok "server setup finished"
info "Your tokens were saved to zasder-install-summary.txt in this folder."

# ── Step 4: local station hardware (optional) ────────────────────────────
echo
bold "Step 4 of 4 — connect a Davis WeatherLink Live (optional)"
info "Only needed if you have a Davis WeatherLink Live on your network and"
info "want this Mac to feed it to your server. Stations that report to the"
info "cloud (AmbientWeather, and Davis via WeatherLink cloud) need nothing here."
echo
if [ -f "wll-poller/bin/setup-macos.sh" ] && confirm "  Set up a WeatherLink Live now?"; then
  echo
  rule
  ( cd wll-poller && bash bin/setup-macos.sh )
  rule
else
  info "Skipped. To do it later, double-click this file again."
fi

echo
bold "All done."
echo
info "Next: open Zasder Weather on your iPhone, go to Settings, and enter"
info "your backend URL and API_TOKEN — both are in the file"
info "zasder-install-summary.txt in this folder."
echo
info "To open that file now, run:  open zasder-install-summary.txt"
