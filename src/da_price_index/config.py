"""Application configuration via environment variables and pydantic-settings."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Global application settings loaded from environment or `.env`."""

    # --- Database ---
    db_url: str = "sqlite:///data/da_price_index.db"

    # --- Scraper ---
    portal_url: str = "https://www.da.gov.ph/price-monitoring/"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    request_timeout: float = 30.0
    retry_max_attempts: int = 4
    retry_min_wait: float = 2.0
    retry_max_wait: float = 16.0

    # --- Filesystem ---
    data_dir: Path = Path("data")
    raw_pdf_dir: Path = Path("data/raw")
    manifest_path: Path = Path("data/manifest.json")

    # --- Parser ---
    parser_version: str = "0.1.1"
    min_expected_rows: int = 100
    max_price_threshold: float = 10_000.0

    # --- HTTP API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cache_max_age_seconds: int = 3600

    # --- Rate limiting (per client IP, fixed windows; 0 disables a window) ---
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    rate_limit_per_day: int = 5000
    # Header the hosting proxy sets to the real client IP. Railway's edge sets
    # X-Real-IP; falls back to the socket peer address when absent (local runs).
    client_ip_header: str = "x-real-ip"

    # --- Alerting for the scheduled run-daily job ---
    # Slack or Discord incoming-webhook URL. Unset = alerts are logged only.
    alert_webhook_url: str | None = None
    alert_after_failures: int = 2

    model_config = {"env_prefix": "DA_DPI_", "env_file": ".env", "extra": "ignore"}


# Module-level singleton — import and use directly
settings = Settings()
