"""Settings for the test suite: deterministic secrets and fast password hashing."""

import os

os.environ.setdefault("SECRET_KEY", "test-only-not-secret")
# Never let a developer's .env (real LLM keys) leak into the test run.
os.environ["SKIP_DOTENV"] = "1"

from config.settings import *  # noqa: E402,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
AI_TASKS_INLINE = True
