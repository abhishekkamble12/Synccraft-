"""
Django project configuration package.
"""

try:
    from .celery import app as celery_app

    __all__: tuple[str, ...] = ("celery_app",)
except ImportError:
    __all__ = ()
