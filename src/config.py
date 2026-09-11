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

    # Multi-Provider Model String (e.g. gemini/gemini-2.5-flash, claude-3-5-sonnet-20241022, gpt-4o-mini, ollama/llama3.2)
    llm_model: str = "gemini/gemini-2.5-flash"

    # Provider API Credentials
    gemini_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    ollama_api_base: Optional[str] = "http://localhost:11434"

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
    notification_email_from: str = "BeaconAI <alerts@ddgiovinazzo.com>"

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
        """Export configured API keys to standard environment variables for LiteLLM."""
        if self.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
            os.environ["GEMINI_API_KEY"] = self.gemini_api_key
        if self.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = self.anthropic_api_key
        if self.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = self.openai_api_key
        if self.ollama_api_base and not os.environ.get("OLLAMA_API_BASE"):
            os.environ["OLLAMA_API_BASE"] = self.ollama_api_base

    def has_llm_credentials(self) -> bool:
        """Check whether credentials exist for the selected LLM provider."""
        model = self.llm_model.lower()
        if "ollama" in model or "local" in model:
            return True
        if "gemini" in model or "google" in model:
            return bool(self.gemini_api_key or os.environ.get("GEMINI_API_KEY"))
        if "claude" in model or "anthropic" in model:
            return bool(self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))
        if "gpt" in model or "openai" in model:
            return bool(self.openai_api_key or os.environ.get("OPENAI_API_KEY"))
        return bool(
            self.gemini_api_key
            or self.anthropic_api_key
            or self.openai_api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    settings = Settings()
    settings.ensure_directories()
    settings.sync_litellm_env()
    return settings
