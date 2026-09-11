"""Unit tests for operational configuration, defaults, and schema decoupling."""

import importlib
import os
from unittest.mock import patch

from src.config import HTTP_USER_AGENT, LLM_MAX_RETRIES, LLM_RATE_LIMIT_DELAY, Settings
from src.schemas import UserConstraints, UserProfile


def test_operational_defaults():
    """Verify default values of operational settings."""
    assert LLM_RATE_LIMIT_DELAY == 7.0
    assert LLM_MAX_RETRIES == 3
    assert "Mozilla/5.0" in HTTP_USER_AGENT
    assert "Chrome/120.0.0.0" in HTTP_USER_AGENT

    settings = Settings()
    assert settings.llm_rate_limit_delay == 7.0
    assert settings.llm_rate_limit_delay_seconds == 7.0
    assert settings.llm_max_retries == 3
    assert settings.user_agent == HTTP_USER_AGENT
    assert settings.http_user_agent == HTTP_USER_AGENT


def test_operational_env_overrides():
    """Verify environment variables dynamically override operational defaults."""
    env_vars = {
        "LLM_RATE_LIMIT_DELAY": "12.5",
        "LLM_MAX_RETRIES": "5",
        "HTTP_USER_AGENT": "CustomScraper/1.0",
    }
    with patch.dict(os.environ, env_vars):
        import src.config as cfg
        importlib.reload(cfg)

        assert cfg.LLM_RATE_LIMIT_DELAY == 12.5
        assert cfg.LLM_MAX_RETRIES == 5
        assert cfg.HTTP_USER_AGENT == "CustomScraper/1.0"

        settings = cfg.Settings()
        assert settings.llm_rate_limit_delay == 12.5
        assert settings.llm_rate_limit_delay_seconds == 12.5
        assert settings.llm_max_retries == 5
        assert settings.user_agent == "CustomScraper/1.0"

    # Reload back to clean state
    importlib.reload(cfg)


def test_schemas_free_from_operational_parameters():
    """Verify UserProfile and UserConstraints do not have operational or rate-limiting fields."""
    profile_fields = set(UserProfile.model_fields.keys())
    constraints_fields = set(UserConstraints.model_fields.keys())

    operational_names = {
        "llm_rate_limit_delay",
        "llm_rate_limit_delay_seconds",
        "llm_max_retries",
        "user_agent",
        "http_user_agent",
        "rate_limit",
        "retries",
    }

    assert operational_names.isdisjoint(profile_fields), "UserProfile schema contains operational fields!"
    assert operational_names.isdisjoint(constraints_fields), "UserConstraints schema contains operational fields!"
