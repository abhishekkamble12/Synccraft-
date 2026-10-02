import json
import uuid
from collections.abc import Mapping
from typing import Any, cast

from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DetailView, ListView

from documents.models import Collaborator, Document
from documents.permissions import can_edit, get_role
from documents.services import get_document_state


class RegisterView(CreateView):
    """User registration view."""

    form_class = UserCreationForm
    template_name = "registration/register.html"
    success_url = reverse_lazy("document_list")

    def form_valid(self, form: Any) -> HttpResponse:
        response = super().form_valid(form)
        login(self.request, self.object)
        return response


class DocumentListView(LoginRequiredMixin, ListView):
    """List documents owned by or shared with the authenticated user."""

    model = Document
    template_name = "documents/list.html"
    context_object_name = "documents"

    def get_queryset(self) -> Any:
        user = self.request.user
        return (
            Document.objects.filter(Q(owner=user) | Q(collaborators__user=user))
            .select_related("owner")
            .prefetch_related("collaborators__user")
            .distinct()
        )


class DocumentCreateView(LoginRequiredMixin, View):
    """Create a new blank document and redirect to its editor."""

    def post(self, request: HttpRequest) -> HttpResponse:
        title = request.POST.get("title", "").strip() or "Untitled Document"
        doc = Document.objects.create(title=title, owner=cast(User, request.user))
        return redirect("document_editor", doc_id=doc.id)


class DocumentEditorView(LoginRequiredMixin, DetailView):
    """Main real-time collaborative editor view."""

    model = Document
    template_name = "documents/editor.html"
    pk_url_kwarg = "doc_id"
    context_object_name = "document"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = dict(super().get_context_data(**kwargs))
        doc = self.object
        role = get_role(doc, self.request.user)
        if role is None:
            raise Http404("Document not found")

        context["initial_text"] = get_document_state(doc.id).text
        context["user_role"] = role
        context["site_id"] = f"site_{self.request.user.pk}_{uuid.uuid4().hex[:6]}"
        return context


class DocumentDeleteView(LoginRequiredMixin, View):
    """Delete an owned document."""

    def post(self, request: HttpRequest, doc_id: uuid.UUID) -> HttpResponse:
        doc = get_object_or_404(Document, id=doc_id, owner=request.user)
        doc.delete()
        return redirect("document_list")


class ShareDocumentView(LoginRequiredMixin, View):
    """Manage collaborators for a document."""

    def get(self, request: HttpRequest, doc_id: uuid.UUID) -> HttpResponse:
        doc = get_object_or_404(Document, id=doc_id)
        if get_role(doc, request.user) is None:
            raise Http404("Document not found")

        collaborators = [
            {
                "id": c.id,
                "username": c.user.username,
                "role": c.role,
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M"),
            }
            for c in doc.collaborators.select_related("user").all()
        ]
        return JsonResponse({"collaborators": collaborators, "owner": doc.owner.username})

    def post(self, request: HttpRequest, doc_id: uuid.UUID) -> HttpResponse:
        doc = get_object_or_404(Document, id=doc_id)
        role = get_role(doc, request.user)
        if role is None:
            raise Http404("Document not found")
        if role != "owner":
            return JsonResponse(
                {"error": "Only document owner can manage collaborators"}, status=403
            )

        data: Mapping[str, Any] = request.POST
        if not data and request.body:
            try:
                data = json.loads(request.body)
            except Exception:
                data = {}

        action = data.get("action", "add")
        if action == "remove":
            collab_id = data.get("collab_id")
            username = data.get("username")
            if collab_id:
                doc.collaborators.filter(id=collab_id).delete()
            elif username:
                doc.collaborators.filter(user__username=username).delete()
            return JsonResponse({"success": True})

        username = data.get("username", "").strip()
        role = data.get("role", "editor").strip().lower()
        if role not in ["editor", "viewer"]:
            role = "editor"

        if not username:
            return JsonResponse({"error": "Username is required"}, status=400)

        target_user = User.objects.filter(username=username).first()
        if not target_user:
            return JsonResponse({"error": f"User '{username}' not found"}, status=404)

        if target_user == doc.owner:
            return JsonResponse({"error": "User is already the document owner"}, status=400)

        collab, created = Collaborator.objects.update_or_create(
            document=doc, user=target_user, defaults={"role": role}
        )
        return JsonResponse(
            {
                "success": True,
                "created": created,
                "collaborator": {
                    "id": collab.id,
                    "username": target_user.username,
                    "role": collab.role,
                },
            }
        )


class DocumentRenameView(LoginRequiredMixin, View):
    """Rename a document inline."""

    def post(self, request: HttpRequest, doc_id: uuid.UUID) -> HttpResponse:
        doc = get_object_or_404(Document, id=doc_id)
        role = get_role(doc, request.user)
        if role is None:
            raise Http404("Document not found")
        if not can_edit(role):
            return JsonResponse({"error": "Permission denied"}, status=403)

        data: Mapping[str, Any] = request.POST
        if not data and request.body:
            try:
                data = json.loads(request.body)
            except Exception:
                data = {}

        title = data.get("title", "").strip()
        if not title:
            return JsonResponse({"error": "Title cannot be empty"}, status=400)

        doc.title = title
        doc.save(update_fields=["title", "updated_at"])
        return JsonResponse({"success": True, "title": doc.title})
