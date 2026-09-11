"""Configuration module for BeaconAI using Pydantic Settings supporting multi-provider LLMs."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Operational runtime defaults backed by environment variables
LLM_MODEL: Optional[str] = os.getenv("LLM_MODEL")
LLM_RATE_LIMIT_DELAY: float = float(os.getenv("LLM_RATE_LIMIT_DELAY", "7.0"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
HTTP_USER_AGENT: str = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Unified LLM API Credentials & Universal Model Routing
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = LLM_MODEL

    # Circuit Breaker Cap
    max_llm_evals_per_run: int = 20

    # Persistence
    db_path: Path = Path("matches.db")

    # Ingestion
    http_user_agent: str = HTTP_USER_AGENT
    request_timeout_seconds: int = 15
    target_feed_urls: Optional[str] = None

    # Rate Limiting & Throttling
    llm_rate_limit_delay: float = LLM_RATE_LIMIT_DELAY
    llm_max_retries: int = LLM_MAX_RETRIES

    @property
    def user_agent(self) -> str:
        """Backward-compatible alias for http_user_agent."""
        return self.http_user_agent

    @property
    def llm_rate_limit_delay_seconds(self) -> float:
        """Backward-compatible alias for llm_rate_limit_delay."""
        return self.llm_rate_limit_delay

    # Outbound Notifications (Resend)
    resend_api_key: Optional[str] = None
    notification_email_to: Optional[str] = None
    notification_email_from: str = "BeaconAI <alerts@example.com>"

    # Inbound Email Ingestion (IMAP)
    imap_server: Optional[str] = None
    imap_port: int = 993
    imap_username: Optional[str] = None
    imap_password: Optional[str] = None
    imap_mailbox: str = "INBOX"
    imap_search_criteria: str = "UNSEEN"
    imap_mark_seen: bool = True

    @property
    def is_imap_configured(self) -> bool:
        """Check if IMAP email ingestion credentials are fully configured."""
        return bool(self.imap_server and self.imap_username and self.imap_password)

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
        if self.llm_api_key or os.environ.get("LLM_API_KEY"):
            return True
        # Check standard provider env keys delegated cleanly by LiteLLM
        standard_provider_keys = [
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GROQ_API_KEY",
            "MISTRAL_API_KEY",
            "COHERE_API_KEY",
            "AZURE_API_KEY",
        ]
        return any(bool(os.environ.get(k)) for k in standard_provider_keys)


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    settings = Settings()
    settings.ensure_directories()
    settings.sync_litellm_env()
    return settings
