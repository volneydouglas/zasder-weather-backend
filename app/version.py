"""Single source of truth for the backend version.

Bumped on each release; the release tag on the public repo
(github.com/volneydouglas/zasder-weather-backend) must match `v<__version__>`.
The self-check in app/updates.py compares this against the latest GitHub
release so operators see an "update available" banner. See CHANGELOG.md.
"""

__version__ = "2.7.1"

# What a client may rely on, served on GET /api/version (2.7). A feature a
# third-party client might want to detect adds a string here in the
# release that ships it; within a major version a string is never removed
# or renamed, the same additive-only rule as the stable fields in API.md.
# Build-level only: every server running this version answers the same
# list, so it cannot fingerprint one install the way a setting would.
#   stable-api  the stable endpoints and fields documented in API.md
#               are served and kept (tests/test_stable_api.py)
CAPABILITIES: tuple[str, ...] = ("stable-api",)
