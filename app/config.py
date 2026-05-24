import json as _json
from typing import ClassVar

from pydantic import field_validator, model_validator
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

    # WeatherLink v2 cloud poller (Davis Vantage Pro 2 + 6313 console).
    # All four must be set together to enable the poller; any unset and
    # the poller stays asleep (similar to aw_configured).
    weatherlink_api_key: str | None = None
    weatherlink_api_secret: str | None = None
    weatherlink_station_id: int | None = None
    weatherlink_poll_interval_seconds: int = 60
    # Friendly name + location used on the synthetic device row; if
    # unset, falls back to the WeatherLink station's own name / city.
    weatherlink_name: str | None = None
    weatherlink_location: str | None = None
    # Inches to ADD to whatever the WeatherLink API reports as
    # rainfall_year_in. Use case: the Davis ISS was installed
    # mid-year, so its own yearly counter started at 0 even though
    # actual cumulative rainfall was already higher. Other receivers
    # (AWN, Atlas) have the right total; we baseline Davis here.
    weatherlink_yearly_rain_baseline_in: float = 0.0

    # Per-MAC yearly-rain offset applied at ingest. Use case: LilyGO
    # firmware posts the sensor's raw lifetime cumulative counter as
    # rain.yearly_in (no firmware-side baselining), so each receiver
    # needs an offset that calibrates the stored value to actual YTD
    # rain. Env format is JSON: {"MAC1":offset1,"MAC2":offset2}.
    # Stored yearly_in = max(0, posted_yearly_in - offset[mac]).
    # MACs use the colonized AA:BB:CC:DD:EE:FF form.
    ingest_yearly_rain_offsets: dict[str, float] = {}

    @field_validator("ingest_yearly_rain_offsets", mode="before")
    @classmethod
    def _parse_offsets(cls, v):
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return {}
            try:
                return {k.upper(): float(val) for k, val in _json.loads(s).items()}
            except (ValueError, TypeError):
                return {}
        return v or {}

    # When set, every /current response for a device WITHOUT pressure
    # falls back to the freshest pressure (+ indoor temp/humidity) from
    # this source MAC. Use case: Atlas (no barometer) + co-located
    # WH32B-paired Crestview (or Davis) on the same property — operator
    # gets a pressure tile on the Atlas card by pointing this env at
    # whichever device actually has a barometer.
    # Set to e.g. "5D:5D:02:00:00:7D" (Crestview SDR with WH32B paired).
    shared_barometer_source_mac: str | None = None

    # Strings that ship as placeholders in .env.example and would let an
    # un-edited template run live if the operator forgets to substitute.
    # Treated as invalid by the token validators so the app refuses to
    # start instead of accepting them as live credentials.
    _PLACEHOLDER_TOKENS: ClassVar[tuple[str, ...]] = (
        "generate-a-long-random-string",
        "change-me",
        "replace_me",
        "your-token-here",
    )

    @field_validator("api_token", "ingest_token", "reviewer_api_token", mode="after")
    @classmethod
    def _reject_placeholder_tokens(cls, v, info):
        if v is None:
            return v
        s = v.strip()
        if not s:
            return None if info.field_name != "api_token" else v
        low = s.lower()
        if low in cls._PLACEHOLDER_TOKENS or "replace-with" in low:
            raise ValueError(
                f"{info.field_name} is set to a known placeholder "
                f"({s!r}). Generate a real one with `openssl rand -hex 32` "
                f"and set it via env/secret.")
        # Length floor for production tokens. Exempt the `test-` prefix
        # so unit tests can keep their human-readable token strings
        # without forcing every call site through a 64-char hex literal.
        if (len(s) < 32
                and info.field_name != "reviewer_api_token"
                and not s.startswith("test-")):
            raise ValueError(
                f"{info.field_name} must be at least 32 characters "
                f"(got {len(s)}). Generate with `openssl rand -hex 32`.")
        return s

    @model_validator(mode="after")
    def _reject_identical_tokens(self):
        if (self.api_token and self.ingest_token
                and self.api_token == self.ingest_token):
            raise ValueError(
                "api_token and ingest_token must differ. Revoking write "
                "(ingest) would otherwise lock the iOS app out, and vice "
                "versa. Generate two separate values.")
        return self

    @property
    def weatherlink_configured(self) -> bool:
        return bool(self.weatherlink_api_key
                    and self.weatherlink_api_secret
                    and self.weatherlink_station_id)

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
