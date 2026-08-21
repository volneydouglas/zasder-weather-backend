"""Refuse to boot the wrong build variant onto a deployment that needs the
full one.

This repo publishes a GENERATED public mirror with the hosted push-relay
server stripped out (`bin/generate-public.sh`). Both trees carry a
`fly.toml` naming an app, so `fly deploy -a <app>` runs happily from either
one and reports success.

On 2026-08-16 the stripped mirror was deployed onto the hosted instance.
That silently deleted `/api/relay/*`, and because the iOS client hardcodes
its relay host, push registration AND delivery broke for the entire user
base for 8h17m. Nothing caught it: `/api/version` and `/healthz` stay
green when routes simply cease to exist, so every post-deploy check passed.

The guard closes that hole. A deployment that is supposed to host the relay
sets `REQUIRE_RELAY=1`; if the modules are then missing, startup raises, the
health check never passes, and Fly rolls the deploy back instead of serving
a silently-lobotomised API.

**Set it as a Fly SECRET, not fly.toml `[env]`:**

    fly secrets set REQUIRE_RELAY=1 -a zasder-weather

Secrets are attached to the app and survive deploys from any source tree.
An `[env]` entry lives in whichever `fly.toml` is being deployed, so
deploying the mirror would quietly drop it and disarm the guard — exactly
the failure being defended against.

Self-hosters never set the flag, so this is inert for them, which is why
the check is opt-in rather than automatic.
"""

from __future__ import annotations

import importlib.util
import os

# Modules the strip script removes for the public mirror. Exactly one entry,
# on purpose: this file IS mirrored, and the generator's residue sweep rejects
# the names of the other private modules outright. `app.relay` is enough —
# its absence proves the stripped build. The fuller set of private-only routes
# is checked by bin/deploy.sh, which is not mirrored.
_FULL_BUILD_MODULES = ("app.relay",)

_TRUTHY = ("1", "true", "yes", "on")


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def missing_full_build_modules() -> list[str]:
    """Names of full-build modules absent from this deployment. Empty on the
    monorepo build; the stripped mirror is missing all of them."""
    return [m for m in _FULL_BUILD_MODULES if importlib.util.find_spec(m) is None]


def assert_build_variant() -> None:
    """Raise if REQUIRE_RELAY is set but this is the stripped public build.

    Called during startup, before the app reports healthy. Deliberately fatal:
    a half-featured API that answers 200 on /healthz is worse than a failed
    deploy, because the failure is invisible until a user reports it.
    """
    if not _enabled("REQUIRE_RELAY"):
        return
    missing = missing_full_build_modules()
    if not missing:
        return
    raise RuntimeError(
        "REQUIRE_RELAY=1 but this is the STRIPPED public build — missing: "
        + ", ".join(missing)
        + ". The public mirror was deployed onto the hosted instance. Redeploy "
        "from the monorepo backend/ directory, NOT from the generated mirror."
    )
