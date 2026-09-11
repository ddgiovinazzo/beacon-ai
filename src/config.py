"""Configuration module for BeaconAI using Pydantic Settings supporting multi-provider LLMs."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Unified LLM API Credentials & Fallback Model
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None

    # Circuit Breaker Cap
    max_llm_evals_per_run: int = 20

    # Persistence
    db_path: Path = Path("matches.db")

    # Ingestion
    user_agent: str = "BeaconAI/1.0 (+https://github.com/beacon-ai; polite-job-crawler)"
    request_timeout_seconds: int = 15
    target_feed_urls: Optional[str] = None

    # Outbound Notifications (Resend)
    resend_api_key: Optional[str] = None
    notification_email_to: Optional[str] = None
    notification_email_from: str = "BeaconAI <alerts@example.com>"

    @field_validator("notification_email_to")
    @classmethod
    def validate_email_to(cls, v: Optional[str]) -> Optional[str]:
        """Validate destination notification email address format."""
        if v is not None:
            v = v.strip()
            if not v:
                return None
            if "@" not in v or "." not in v.split("@")[-1]:
                raise ValueError(f"Invalid email address provided for notification_email_to: {v}")
        return v

    # Artifact output directories
    artifacts_dir: Path = Path("artifacts")
    matches_dir: Path = Path("artifacts/matches")

    def ensure_directories(self) -> None:
        """Ensure necessary output and artifact directories exist."""
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.matches_dir.mkdir(parents=True, exist_ok=True)

    def sync_litellm_env(self) -> None:
        """Export configured unified API key to environment for LiteLLM."""
        if self.llm_api_key and not os.environ.get("LLM_API_KEY"):
            os.environ["LLM_API_KEY"] = self.llm_api_key

    def has_llm_credentials(self, model: Optional[str] = None) -> bool:
        """Check whether LLM API credentials or local execution base exists."""
        if model and (model.startswith("ollama/") or model.startswith("local/")):
            return True
        return bool(self.llm_api_key or os.environ.get("LLM_API_KEY"))


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    settings = Settings()
    settings.ensure_directories()
    settings.sync_litellm_env()
    return settings
