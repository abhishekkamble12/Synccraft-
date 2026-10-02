"""Shared test helpers."""

from typing import Any

from channels.testing import WebsocketCommunicator

from config.asgi import application


def ws_communicator(
    doc_id: Any, user: Any, origin: bytes = b"http://localhost"
) -> WebsocketCommunicator:
    """Build a WebsocketCommunicator authenticated as `user` with a same-site Origin."""
    communicator = WebsocketCommunicator(
        application,
        f"/ws/docs/{doc_id}/",
        headers=[(b"origin", origin), (b"host", b"localhost")],
    )
    if user is not None:
        communicator.scope["user"] = user
    return communicator
