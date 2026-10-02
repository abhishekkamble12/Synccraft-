"""
ASGI config for Real-Time Collaborative Sync Engine.

It exposes the ASGI callable as a module-level variable named ``application``.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402

from config.routing import websocket_urlpatterns  # noqa: E402

http_app = ASGIStaticFilesHandler(django_asgi_app) if settings.SERVE_STATIC else django_asgi_app

application = ProtocolTypeRouter(
    {
        "http": http_app,
        # Reject cross-site WebSocket upgrades: the session cookie would otherwise
        # authenticate a socket opened by any third-party page.
        "websocket": AllowedHostsOriginValidator(
            AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
        ),
    }
)
