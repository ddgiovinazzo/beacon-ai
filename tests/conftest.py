"""Global pytest fixtures and test lifecycle management."""

import pytest
from src.config import get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache():
    """Ensure settings cache is cleared before and after every test to prevent environment leakage."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
