"""Configuration module for BeaconAI using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM Settings
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.5-flash"
    max_llm_evals_per_run: int = 20

    # Persistence
    db_path: Path = Path("matches.db")

    # Ingestion
    user_agent: str = "BeaconAI/1.0 (+https://github.com/beacon-ai; polite-job-crawler)"
    request_timeout_seconds: int = 15

    # Artifact output directories
    artifacts_dir: Path = Path("artifacts")
    matches_dir: Path = Path("artifacts/matches")

    def ensure_directories(self) -> None:
        """Ensure necessary output and artifact directories exist."""
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.matches_dir.mkdir(parents=True, exist_ok=True)


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    settings = Settings()
    settings.ensure_directories()
    return settings
