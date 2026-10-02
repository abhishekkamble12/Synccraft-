"""
Database models for documents, operations, snapshots, collaborators, and AI jobs.
"""

import uuid

from django.contrib.auth.models import User
from django.db import models


class Document(models.Model):
    """
    A collaborative document entity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255, default="Untitled Document")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="owned_documents")
    head_seq = models.BigIntegerField(
        default=0, help_text="Monotonically increasing sequence number of the latest operation."
    )
    gc_seq = models.BigIntegerField(
        default=0,
        help_text="Tombstones deleted at or before this seq may have been garbage-collected. "
        "Ops based on an older state are rejected as stale.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.id})"


class Collaborator(models.Model):
    """
    Collaborator permission mapping for documents.
    """

    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("editor", "Editor"),
        ("viewer", "Viewer"),
    ]

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="collaborators")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="collaborations")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default="editor")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("document", "user")
        indexes = [
            models.Index(fields=["document", "user"]),
        ]

    def __str__(self) -> str:
        return f"{self.user.username} ({self.role}) on {self.document.title}"


class Operation(models.Model):
    """
    Append-only immutable log of every CRDT operation applied to a document.
    """

    TYPE_CHOICES = [
        ("insert", "Insert"),
        ("delete", "Delete"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="operations")
    server_seq = models.BigIntegerField(
        help_text="Globally ordered sequence number within this document."
    )
    op_id = models.CharField(
        max_length=128, unique=True, db_index=True, help_text="Globally unique idempotency key."
    )
    site_id = models.CharField(max_length=64, db_index=True)
    lamport = models.BigIntegerField()
    type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    payload = models.JSONField(help_text="Full serialized Op dictionary.")
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="operations"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["server_seq"]
        unique_together = ("document", "server_seq")
        indexes = [
            models.Index(fields=["document", "server_seq"]),
            models.Index(fields=["document", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Op #{self.server_seq} ({self.type}) on {self.document_id} by {self.site_id}"


class SiteSession(models.Model):
    """
    One CRDT site (a client replica, an AI job, a revert) on one document.

    * Binds the site_id to the user who first wrote with it, so nobody can submit
      ops under another user's site.
    * `acked_seq` is the oldest server seq any not-yet-acknowledged op from this
      site can be based on. Tombstone GC never goes past the minimum over live
      sessions (see documents/services.py:stable_seq).
    * A site is `retired` the first time one of its ops is rejected; its later
      ops are rejected too, so a site's committed ops are always a prefix of the
      ops it sent. The client then rebases under a new site_id.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="sites")
    site_id = models.CharField(max_length=64)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name="sites"
    )
    acked_seq = models.BigIntegerField(null=True, blank=True)
    retired = models.BooleanField(default=False)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("document", "site_id")
        indexes = [models.Index(fields=["document", "last_seen"])]

    def __str__(self) -> str:
        return f"Site {self.site_id} on {self.document_id} (acked {self.acked_seq})"


class Snapshot(models.Model):
    """
    Periodic full-state snapshot of the CRDT document for fast load and history replay.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="snapshots")
    server_seq = models.BigIntegerField(help_text="Sequence number up to which state is captured.")
    state = models.JSONField(help_text="Full serialized RGA document state.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-server_seq"]
        indexes = [
            models.Index(fields=["document", "-server_seq"]),
        ]

    def __str__(self) -> str:
        return f"Snapshot @ seq {self.server_seq} for {self.document_id}"


class AIJob(models.Model):
    """
    Tracks asynchronous AI co-author and summary requests.
    """

    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("running", "Running"),
        ("done", "Done"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    KIND_CHOICES = [
        ("rewrite", "Rewrite"),
        ("grammar", "Grammar & Fix"),
        ("shorten", "Shorten"),
        ("continue", "Continue Writing"),
        ("summary", "Summary of Missed Edits"),
        ("suggest", "Suggestion"),
        ("ask", "Ask Document"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="ai_jobs")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="ai_jobs")
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="queued")
    anchor_start = models.CharField(
        max_length=128, null=True, blank=True, help_text="Starting CharId"
    )
    anchor_end = models.CharField(max_length=128, null=True, blank=True, help_text="Ending CharId")
    instruction = models.TextField(blank=True, default="")
    prompt_version = models.CharField(max_length=32, default="v1")
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    latency_ms = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AIJob {self.kind} ({self.status}) on doc {self.document_id}"


class Suggestion(models.Model):
    """
    AI or collaborator proposed edit in suggestion mode.
    """

    STATUS_CHOICES = [
        ("open", "Open"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="suggestions")
    ai_job = models.ForeignKey(
        AIJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="suggestions"
    )
    anchor_start = models.CharField(max_length=128)
    anchor_end = models.CharField(max_length=128)
    proposed_text = models.TextField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="open")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
