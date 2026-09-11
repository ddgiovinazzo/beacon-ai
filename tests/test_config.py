"""Unit tests for operational configuration, defaults, and schema decoupling."""

import importlib
import os
from unittest.mock import patch

from src.config import (
    HTTP_USER_AGENT,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_RATE_LIMIT_DELAY,
    Settings,
)
from src.schemas import UserConstraints, UserProfile


def test_operational_defaults():
    """Verify default values of operational settings."""
    assert LLM_MODEL is None
    assert LLM_RATE_LIMIT_DELAY == 7.0
    assert LLM_MAX_RETRIES == 3
    assert "Mozilla/5.0" in HTTP_USER_AGENT
    assert "Chrome/120.0.0.0" in HTTP_USER_AGENT

    settings = Settings()
    assert settings.llm_model is None
    assert settings.llm_rate_limit_delay == 7.0
    assert settings.llm_rate_limit_delay_seconds == 7.0
    assert settings.llm_max_retries == 3
    assert settings.user_agent == HTTP_USER_AGENT
    assert settings.http_user_agent == HTTP_USER_AGENT

    # Confirm canonical fields exist in schema and aliases are properties (no duplicate schema fields)
    assert "http_user_agent" in Settings.model_fields
    assert "llm_rate_limit_delay" in Settings.model_fields
    assert "llm_max_retries" in Settings.model_fields
    assert "llm_model" in Settings.model_fields
    assert "user_agent" not in Settings.model_fields
    assert "llm_rate_limit_delay_seconds" not in Settings.model_fields


def test_operational_env_overrides():
    """Verify environment variables dynamically override operational defaults."""
    env_vars = {
        "LLM_MODEL": "anthropic/claude-3-5-sonnet-20241022",
        "LLM_RATE_LIMIT_DELAY": "12.5",
        "LLM_MAX_RETRIES": "5",
        "HTTP_USER_AGENT": "CustomScraper/1.0",
        "ANTHROPIC_API_KEY": "sk-ant-test-key",
    }
    with patch.dict(os.environ, env_vars):
        settings = Settings()
        assert settings.llm_model == "anthropic/claude-3-5-sonnet-20241022"
        assert settings.llm_rate_limit_delay == 12.5
        assert settings.llm_rate_limit_delay_seconds == 12.5
        assert settings.llm_max_retries == 5
        assert settings.user_agent == "CustomScraper/1.0"
        assert settings.http_user_agent == "CustomScraper/1.0"
        assert settings.has_llm_credentials() is True


def test_schemas_free_from_operational_parameters():
    """Verify UserProfile and UserConstraints do not have operational or rate-limiting fields."""
    profile_fields = set(UserProfile.model_fields.keys())
    constraints_fields = set(UserConstraints.model_fields.keys())

    operational_names = {
        "llm_model",
        "model",
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
