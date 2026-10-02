"""
Template context shared by every page.
"""

from functools import cache

from django.conf import settings
from django.http import HttpRequest


@cache
def _static_version() -> str:
    """Newest mtime under the static dirs, computed once per process.

    Appended to asset URLs (`?v=...`) so a browser never keeps running a stale
    editor.js/editor.css after a deploy or a dev-server restart.
    """
    newest = 0.0
    for directory in settings.STATICFILES_DIRS:
        for path in directory.rglob("*"):
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
    return str(int(newest))


def static_version(request: HttpRequest) -> dict[str, str]:
    return {"static_version": _static_version()}
