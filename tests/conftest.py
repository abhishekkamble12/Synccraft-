"""
Pytest configuration and shared fixtures for collaborative sync tests.
"""

import os
import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
