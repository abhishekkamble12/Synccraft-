"""
Django views for document management, collaborative editor, and user authentication.
"""

import uuid
from typing import Any

from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DetailView, ListView

from documents.models import Collaborator, Document
from documents.services import get_or_load_document_rga


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
        return Document.objects.filter(Q(owner=user) | Q(collaborators__user=user)).distinct()


class DocumentCreateView(LoginRequiredMixin, View):
    """Create a new blank document and redirect to its editor."""

    def post(self, request: HttpRequest) -> HttpResponse:
        title = request.POST.get("title", "").strip() or "Untitled Document"
        doc = Document.objects.create(title=title, owner=request.user)
        return redirect("document_editor", doc_id=doc.id)


class DocumentEditorView(LoginRequiredMixin, DetailView):
    """Main real-time collaborative editor view."""

    model = Document
    template_name = "documents/editor.html"
    pk_url_kwarg = "doc_id"
    context_object_name = "document"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        doc = self.get_object()
        rga = get_or_load_document_rga(doc.id)

        # Determine user role
        role = "viewer"
        if doc.owner == self.request.user:
            role = "owner"
        else:
            collab = Collaborator.objects.filter(document=doc, user=self.request.user).first()
            if collab:
                role = collab.role

        context["initial_text"] = rga.text()
        context["user_role"] = role
        context["site_id"] = f"site_{self.request.user.id}_{uuid.uuid4().hex[:6]}"
        return context


class DocumentDeleteView(LoginRequiredMixin, View):
    """Delete an owned document."""

    def post(self, request: HttpRequest, doc_id: uuid.UUID) -> HttpResponse:
        doc = get_object_or_404(Document, id=doc_id, owner=request.user)
        doc.delete()
        return redirect("document_list")
