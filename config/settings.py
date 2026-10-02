"""
Django settings for Real-Time Collaborative Sync Engine.
"""

import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("true", "1", "yes")


# Pick up a local .env (GROQ_API_KEY etc.) when running outside Docker. Real
# environment variables always win.
if not env_bool("SKIP_DOTENV"):
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env", override=False)
    except ImportError:
        pass

DEBUG = env_bool("DEBUG", False)

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("SECRET_KEY must be set when DEBUG is off.")
    SECRET_KEY = "django-insecure-local-development-only"

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h
]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

# Application definition
INSTALLED_APPS = [
    "daphne",  # ASGI server must be first for runserver integration
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party apps
    "channels",
    # Local apps
    "documents.apps.DocumentsConfig",
    "ai.apps.AiConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "documents.context_processors.static_version",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database configuration
# Defaults to PostgreSQL when DB_HOST is set, otherwise falls back to SQLite for easy local test runs
DB_ENGINE = os.environ.get("DB_ENGINE", "postgresql" if os.environ.get("DB_HOST") else "sqlite")

if DB_ENGINE == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "collaborative_sync"),
            "USER": os.environ.get("DB_USER", "postgres"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        }
    }

# Redis backs the channel layer, cache (rate limits / budgets) and Celery broker.
# Without REDIS_URL everything runs in-process, which is fine for a single dev server.
REDIS_URL = os.environ.get("REDIS_URL", "")

# Per-socket inbound queue size. Both layers silently drop group messages once a
# queue is full; clients detect the resulting seq gap and re-sync (see sync.js).
CHANNEL_CAPACITY = int(os.environ.get("CHANNEL_CAPACITY", "1000"))

if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL], "capacity": CHANNEL_CAPACITY},
        },
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
            "CONFIG": {"capacity": CHANNEL_CAPACITY},
        }
    }
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Celery Configuration
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL or "memory://")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", REDIS_URL or None)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"

# Run AI jobs inside the web process instead of on a Celery worker.
AI_TASKS_INLINE = env_bool("AI_TASKS_INLINE", not REDIS_URL)

# Reconnecting clients that missed at least this many ops get an AI summary.
MISSED_SUMMARY_THRESHOLD = int(os.environ.get("MISSED_SUMMARY_THRESHOLD", "20"))

# Tombstone GC. A client that reported nothing for SITE_SESSION_TTL_SEC stops holding
# GC back; if it returns, its old ops are rejected as stale and it rebases them.
SITE_SESSION_TTL_SEC = int(os.environ.get("SITE_SESSION_TTL_SEC", str(24 * 3600)))
# Compact once the stable point has advanced this many ops past the last compaction ...
GC_MIN_OPS = int(os.environ.get("GC_MIN_OPS", "1000"))
# ... checking at most this often per document.
GC_CHECK_INTERVAL_SEC = float(os.environ.get("GC_CHECK_INTERVAL_SEC", "10"))
# Heartbeat watermarks are batched into one DB write per document per interval.
ACK_FLUSH_INTERVAL_SEC = float(os.environ.get("ACK_FLUSH_INTERVAL_SEC", "5"))

# Serve /static/ from the ASGI app (demo deployments without a CDN / nginx static root).
SERVE_STATIC = env_bool("SERVE_STATIC", DEBUG)

if not DEBUG:
    SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)
    CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth Redirects
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "document_list"
LOGOUT_REDIRECT_URL = "login"
