"""Single source of truth for the backend version.

Bumped on each release; the release tag on the public repo
(github.com/volneydouglas/zasder-weather-backend) must match `v<__version__>`.
The self-check in app/updates.py compares this against the latest GitHub
release so operators see an "update available" banner. See CHANGELOG.md.
"""

__version__ = "1.2.0"
