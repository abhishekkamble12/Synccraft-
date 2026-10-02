"""
Pytest configuration and shared fixtures for collaborative sync tests.
"""

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.test_settings")


@pytest.fixture(autouse=True)
def _fresh_replica_cache() -> Iterator[None]:
    """Each test starts with an empty in-memory replica cache, like a fresh process."""
    from documents.services import clear_replica_cache

    clear_replica_cache()
    yield
    clear_replica_cache()
