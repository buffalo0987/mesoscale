"""Centralized typed configuration for Mesoscale."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables (MESOSCALE_*)."""

    model_config = SettingsConfigDict(
        env_prefix="MESOSCALE_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Path("./data")
    nws_user_agent: str = "(MesoscaleWeatherPWA, selfhosted@local.tailnet)"
    vapid_subject: str = "mailto:weather@local.tailnet"

    ring_radius_km: float = 8.0
    backfill_days: int = 365
    retention_days: int = 730
    thin_hourly_runs_after_days: int = 90
    enable_scheduler: bool = True
    open_meteo_timeout_s: float = 25.0

    @property
    def app_db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def telemetry_db_path(self) -> Path:
        return self.data_dir / "telemetry.duckdb"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def vapid_keys_path(self) -> Path:
        return self.data_dir / "vapid_keys.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
