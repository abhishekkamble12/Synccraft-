"""
URL configuration for Real-Time Collaborative Sync Engine.
"""

from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("documents.urls")),
]
