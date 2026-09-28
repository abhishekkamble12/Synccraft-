"""
Django admin registrations for documents and sync engine models.
"""

from django.contrib import admin
from documents.models import (
    AIJob,
    Collaborator,
    DocChunk,
    Document,
    Operation,
    Snapshot,
    Suggestion,
)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "id", "owner", "head_seq", "created_at", "updated_at")
    search_fields = ("title", "id", "owner__username")
    readonly_fields = ("id", "head_seq", "created_at", "updated_at")


@admin.register(Collaborator)
class CollaboratorAdmin(admin.ModelAdmin):
    list_display = ("document", "user", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("document__title", "user__username")


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ("server_seq", "document", "type", "op_id", "site_id", "lamport", "user", "created_at")
    list_filter = ("type", "site_id")
    search_fields = ("op_id", "document__title", "site_id")
    readonly_fields = ("id", "server_seq", "op_id", "site_id", "lamport", "type", "payload", "created_at")


@admin.register(Snapshot)
class SnapshotAdmin(admin.ModelAdmin):
    list_display = ("document", "server_seq", "created_at")
    readonly_fields = ("id", "document", "server_seq", "state", "created_at")


@admin.register(AIJob)
class AIJobAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "user", "kind", "status", "latency_ms", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("document__title", "user__username", "instruction")


@admin.register(Suggestion)
class SuggestionAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "status", "created_at")
    list_filter = ("status",)


@admin.register(DocChunk)
class DocChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "server_seq", "created_at")
