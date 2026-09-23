"""Application configuration management.

Uses pydantic-settings for environment-based configuration with validation.
Integrates with cloud_quant database configuration.
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opendata.utils.constants import DEFAULT_SECRET_KEY


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application Settings
    app_name: str = Field(default="opendata", description="Application name")
    app_env: Literal["development", "testing", "production"] = Field(
        default="development", description="Application environment"
    )
    app_debug: bool = Field(default=False, description="Debug mode")
    app_version: str = Field(default="0.1.0", description="Application version")

    # Server Settings (0.0.0.0 intentional for Docker/cloud - listen on all interfaces)
    host: str = Field(
        default="0.0.0.0",  # noqa: S104  # intentional for Docker/cloud (bandit B104 rationale)
        description="Server host",
    )  # B104 skipped in bandit.yaml (Docker)
    port: int = Field(default=8000, description="Server port")
    workers: int = Field(default=1, description="Number of worker processes")

    # Database Settings (Shared with cloud_quant)
    mysql_host: str = Field(default="localhost", description="MySQL host")
    mysql_port: int = Field(default=3306, description="MySQL port")
    mysql_user: str = Field(default="root", description="MySQL user")
    mysql_password: str = Field(default="", description="MySQL password")
    mysql_database: str = Field(
        default="opendata", description="Main database name for opendata business tables"
    )

    # Data warehouse DB for opendata data pipelines
    data_mysql_host: str = Field(default="localhost", description="Data MySQL host")
    data_mysql_port: int = Field(default=3306, description="Data MySQL port")
    data_mysql_user: str = Field(default="root", description="Data MySQL user")
    data_mysql_password: str = Field(default="", description="Data MySQL password")
    data_mysql_database: str = Field(
        default="opendata_data", description="Data warehouse database name"
    )

    # Database Connection Pool
    database_pool_size: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Database connection pool size",
    )
    database_max_overflow: int = Field(
        default=10,
        ge=0,
        le=100,
        description="Database pool max overflow",
    )

    # Redis Settings (optional)
    redis_url: str | None = Field(default=None, description="Redis connection URL")

    # Authentication Settings
    secret_key: str = Field(
        default=DEFAULT_SECRET_KEY,
        description="JWT secret key",
    )
    access_token_expire_minutes: int = Field(
        default=1440, description="Access token expiration in minutes (24 hours)"
    )
    refresh_token_expire_days: int = Field(
        default=30, description="Refresh token expiration in days"
    )
    algorithm: str = Field(default="HS256", description="JWT algorithm")

    # Email Settings
    smtp_host: str | None = Field(default=None, description="SMTP server host")
    smtp_port: int = Field(default=587, description="SMTP server port")
    smtp_user: str | None = Field(default=None, description="SMTP username")
    smtp_password: str | None = Field(default=None, description="SMTP password")
    emails_from_email: str | None = Field(default=None, description="From email address")
    emails_from_name: str = Field(default="opendata", description="From email name")

    # Task Scheduler Settings
    enable_scheduler: bool | None = Field(
        default=None,
        description=(
            "ENABLE_SCHEDULER: explicit scheduler ownership (design §9.3). "
            "Required in production; unset means the scheduler must not start."
        ),
    )
    scheduler_bootstrap_on_startup: bool = Field(
        default=True, description="Bootstrap scheduler on startup"
    )
    scheduler_max_workers: int = Field(
        default=3, description="Maximum number of concurrent task workers"
    )
    task_retry_max_attempts: int = Field(
        default=3, description="Maximum number of task retry attempts"
    )
    task_retry_base_delay: int = Field(default=60, description="Base delay for retry in seconds")

    # fuyao (同花顺扶摇) transport credentials (A3.1/A3.2). Read from .env by
    # pydantic-settings so the app path has the key without exporting it.
    fuyao_api_key: str | None = Field(
        default=None, description="FUYAO_API_KEY: fuyao (THS) API key; unset disables fuyao"
    )
    fuyao_api_base_url: str | None = Field(
        default=None,
        description="FUYAO_API_BASE_URL: override the fuyao base URL (default fuyao.aicubes.cn)",
    )

    # fred transport credentials (C1 P0). Read from .env by pydantic-settings;
    # unset disables fred routing (R2: implemented first, verified when the
    # key is provided).
    fred_api_key: str | None = Field(
        default=None, description="FRED_API_KEY: FRED web service key; unset disables fred"
    )
    fred_api_base_url: str | None = Field(
        default=None,
        description="FRED_API_BASE_URL: override the FRED base URL (default api.stlouisfed.org)",
    )

    # ecb transport endpoint (C1 P0). The ECB Data Portal is public (no key);
    # the override exists for tests and proxies.
    ecb_api_base_url: str | None = Field(
        default=None,
        description="ECB_API_BASE_URL: override the ECB Data Portal base URL",
    )

    # Consumer API keys (design §10.3, FR-19)
    api_key_pepper: str | None = Field(
        default=None,
        description=(
            "API_KEY_PEPPER: additional secret mixed into the api_keys "
            "hash; falls back to SECRET_KEY when unset"
        ),
    )
    api_key_failure_delay_seconds: float = Field(
        default=0.05,
        description=(
            "API_KEY_FAILURE_DELAY_SECONDS: constant delay on failed API-key "
            "authentication, flattening the timing signal used for enumeration"
        ),
    )

    # Retention (design §8.5, milestone A4.10)
    cache_dir: Path = Field(
        default=Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "opendata",
        description="CACHE_DIR: root for raw-response cache files (never versioned)",
    )
    cache_ttl_seconds: int = Field(
        default=900, description="CACHE_TTL_SECONDS: raw-response cache TTL in seconds"
    )
    retention_diff_report_days: int = Field(
        default=90,
        description="RETENTION_DIFF_REPORT_DAYS: dq_diff_report aggregate retention in days",
    )
    retention_minute_years: int = Field(
        default=10,
        description="RETENTION_MINUTE_YEARS: minute-line archive retention in years",
    )

    # Rate Limiting
    rate_limit_per_minute: int = Field(default=100, description="Rate limit per minute per user")
    rate_limit_burst: int = Field(default=200, description="Rate limit burst size")

    # CORS Settings
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:3000",
            "http://localhost:6600",
        ],
        description="Allowed CORS origins",
    )
    cors_allow_credentials: bool = Field(default=True, description="Allow CORS credentials")

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_file: str = Field(default="logs/app.log", description="Log file path")
    log_json: bool = Field(
        default=False,
        description="Output logs in JSON format (recommended for production log aggregators)",
    )

    # Error Tracking (Sentry)
    sentry_dsn: str | None = Field(
        default=None,
        description="Sentry DSN for error tracking. Leave empty to disable.",
    )
    sentry_traces_sample_rate: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Sentry performance traces sample rate (0.0 to 1.0)",
    )

    # Data Storage
    data_dir: Path = Field(default=Path("./data"), description="Data storage directory")
    upload_dir: Path = Field(default=Path("./uploads"), description="Upload directory")

    # akshare Settings
    akshare_timeout: int = Field(default=120, description="akshare request timeout")
    akshare_call_timeout: int = Field(default=120, description="akshare call timeout")
    akshare_retry_attempts: int = Field(default=3, description="akshare retry attempts")

    # A1.7 compatibility switch (FR-17): legacy akshare reflection vs
    # provider-registry capabilities as the interface catalog source.
    # registry stays empty until P0 fetchers register (A2.4).
    interface_scan_source: str = Field(
        default="legacy",
        description="Interface catalog scan source: legacy | registry",
    )

    @field_validator("secret_key", mode="after")
    @classmethod
    def validate_secret_key(cls, v: str, info: ValidationInfo) -> str:
        """Warn or reject default secret key based on environment."""
        if v == DEFAULT_SECRET_KEY:
            env = os.getenv("APP_ENV", "development")
            if env == "production":
                raise ValueError(
                    "SECURITY ERROR: Default secret key detected in production! "
                    "Set SECRET_KEY to a unique random value."
                )
        return v

    @field_validator(
        "cache_ttl_seconds",
        "retention_diff_report_days",
        "retention_minute_years",
        mode="after",
    )
    @classmethod
    def validate_positive_retention(cls, v: int, info: ValidationInfo) -> int:
        """Reject non-positive retention limits (a zero would purge everything)."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        """Parse CORS origins from string or list."""
        if isinstance(v, str):
            try:
                parsed: list[str] = json.loads(v)
                return parsed
            except json.JSONDecodeError:
                return [origin.strip() for origin in v.split(",")]
        return list(v)

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.app_env == "production"

    @property
    def database_url(self) -> str:
        """Get async database URL for SQLAlchemy."""
        password = quote_plus(self.mysql_password)
        host = f"{self.mysql_host}:{self.mysql_port}"
        return f"mysql+aiomysql://{self.mysql_user}:{password}@{host}/{self.mysql_database}"

    @property
    def database_url_sync(self) -> str:
        """Get synchronous database URL for Alembic."""
        password = quote_plus(self.mysql_password)
        host = f"{self.mysql_host}:{self.mysql_port}"
        return f"mysql+pymysql://{self.mysql_user}:{password}@{host}/{self.mysql_database}"

    @property
    def data_database_url(self) -> str:
        """Get data warehouse database URL (sync, for akshare scripts)."""
        password = quote_plus(self.data_mysql_password)
        host = f"{self.data_mysql_host}:{self.data_mysql_port}"
        return (
            f"mysql+pymysql://{self.data_mysql_user}:{password}@{host}/{self.data_mysql_database}"
        )

    @property
    def data_database_url_async(self) -> str:
        """Get data warehouse async database URL."""
        password = quote_plus(self.data_mysql_password)
        host = f"{self.data_mysql_host}:{self.data_mysql_port}"
        return (
            f"mysql+aiomysql://{self.data_mysql_user}:{password}@{host}/{self.data_mysql_database}"
        )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
