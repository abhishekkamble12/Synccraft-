"""
URL configuration for Real-Time Collaborative Sync Engine.
"""

from django.contrib import admin
from django.urls import include, path

from documents.metrics import metrics_export_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("metrics", metrics_export_view, name="metrics"),
    path("", include("documents.urls")),
]
