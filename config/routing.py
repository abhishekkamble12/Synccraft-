"""
WebSocket routing configuration for Channels.
"""

from django.urls import re_path

from documents.consumers import DocumentConsumer

websocket_urlpatterns = [
    re_path(r"^ws/docs/(?P<doc_id>[0-9a-f-]+)/$", DocumentConsumer.as_asgi()),
]
