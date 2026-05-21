from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Optional — only required for AmbientWeather ingest. AcuRite-only
    # deploys can leave both unset; the poller stays asleep and only the
    # /ingest/custom path is active.
    aw_application_key: str | None = None
    aw_api_key: str | None = None
    api_token: str
    # Optional secondary bearer token accepted alongside `api_token`. Use cases:
    #   * App Store submission — give the reviewer a dedicated token in the
    #     "App Review Information" section, revoke after approval.
    #   * Token rotation — set both, switch clients to the new one, then drop
    #     the old one without downtime.
    # Leave unset to require the primary token only.
    reviewer_api_token: str | None = None
    # Bearer token for /ingest/custom — write-only, used by sources that POST
    # observations (relay containers, custom SDR, etc.). Distinct from
    # api_token (read) so revoking write doesn't lock the iOS app out.
    ingest_token: str | None = None
    poll_interval_seconds: int = 60
    database_path: str = "./data/weather.db"
    forecast_lat: float | None = None
    forecast_lon: float | None = None
    # IANA timezone for daily/hourly/weekly/monthly rain rollups. Defaults to
    # UTC so the public template works anywhere; personal deploys set it to
    # their actual local zone (e.g., "America/Phoenix") so "today's rain"
    # means today in the user's wall-clock sense, not UTC's.
    timezone: str = "UTC"

    @property
    def valid_api_tokens(self) -> set[str]:
        return {t for t in [self.api_token, self.reviewer_api_token] if t}

    @property
    def aw_configured(self) -> bool:
        """True only when BOTH AWN keys are set to real-looking values.
        Defensive: also rejects placeholder strings like 'replace-with-*'
        so a half-edited .env.example doesn't start the AWN poller with
        garbage credentials (which hammers the AWN API with 401s and
        eventually gets rate-limited)."""
        def _real(v: str | None) -> bool:
            if not v: return False
            s = v.strip().lower()
            return bool(s) and "replace-with" not in s and s != "replace_me"
        return _real(self.aw_application_key) and _real(self.aw_api_key)


settings = Settings()
