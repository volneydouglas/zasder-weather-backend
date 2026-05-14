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

    @property
    def valid_api_tokens(self) -> set[str]:
        return {t for t in [self.api_token, self.reviewer_api_token] if t}

    @property
    def aw_configured(self) -> bool:
        return bool(self.aw_application_key and self.aw_api_key)


settings = Settings()
