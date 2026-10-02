"""
Document access control shared by HTTP views and the WebSocket consumer.

Access is deny-by-default: only the owner and explicit collaborators may open a
document, and only owners/editors may mutate it.
"""

from typing import Any

from documents.models import Collaborator, Document

EDIT_ROLES = frozenset({"owner", "editor"})


def get_role(doc: Document, user: Any) -> str | None:
    """Return the user's role on `doc`, or None if they have no access."""
    if user is None or not user.is_authenticated:
        return None
    if doc.owner_id == user.id:
        return "owner"
    role: str | None = (
        Collaborator.objects.filter(document=doc, user=user).values_list("role", flat=True).first()
    )
    return role


def can_edit(role: str | None) -> bool:
    return role in EDIT_ROLES
