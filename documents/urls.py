"""
URL routing for documents app, user authentication, and history REST APIs.
"""

from django.contrib.auth import views as auth_views
from django.urls import path

from documents.views import (
    DocumentCreateView,
    DocumentDeleteView,
    DocumentEditorView,
    DocumentListView,
    RegisterView,
)
from documents.views_history import (
    DocumentHistoryApiView,
    DocumentRevertApiView,
    DocumentStateAtSeqApiView,
)

urlpatterns = [
    # Document UI views
    path("", DocumentListView.as_view(), name="document_list"),
    path("docs/create/", DocumentCreateView.as_view(), name="document_create"),
    path("docs/<uuid:doc_id>/", DocumentEditorView.as_view(), name="document_editor"),
    path("docs/<uuid:doc_id>/delete/", DocumentDeleteView.as_view(), name="document_delete"),
    # REST History & Time-Travel APIs
    path(
        "api/docs/<uuid:doc_id>/history/", DocumentHistoryApiView.as_view(), name="api_doc_history"
    ),
    path(
        "api/docs/<uuid:doc_id>/at/<int:seq>/",
        DocumentStateAtSeqApiView.as_view(),
        name="api_doc_at_seq",
    ),
    path(
        "api/docs/<uuid:doc_id>/revert/<int:seq>/",
        DocumentRevertApiView.as_view(),
        name="api_doc_revert",
    ),
    # Authentication views
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/register/", RegisterView.as_view(), name="register"),
]
