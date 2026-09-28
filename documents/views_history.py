"""
REST API endpoints for document history, state-at-sequence time travel, and non-destructive revert.
"""

from typing import Any
import uuid

from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from documents.models import Document, Operation
from documents.services import (
    apply_operation,
    generate_revert_operations,
    get_or_load_document_rga,
    reconstruct_state_at_seq,
)


class DocumentHistoryApiView(LoginRequiredMixin, View):
    """
    GET /api/docs/<doc_id>/history/
    Returns operation history grouped by user and time windows.
    """

    def get(self, request: HttpRequest, doc_id: uuid.UUID) -> JsonResponse:
        doc = get_object_or_404(Document, id=doc_id)

        # Fetch operations
        ops_qs = Operation.objects.filter(document=doc).select_related("user").order_by("server_seq")

        history_entries: list[dict[str, Any]] = []
        for op in ops_qs:
            history_entries.append(
                {
                    "seq": op.server_seq,
                    "op_id": op.op_id,
                    "type": op.type,
                    "site_id": op.site_id,
                    "user": op.user.username if op.user else "Anonymous",
                    "timestamp": op.created_at.isoformat(),
                    "char": op.payload.get("char") if op.type == "insert" else None,
                }
            )

        return JsonResponse(
            {
                "doc_id": str(doc.id),
                "title": doc.title,
                "head_seq": doc.head_seq,
                "total_ops": len(history_entries),
                "history": history_entries,
            }
        )


class DocumentStateAtSeqApiView(LoginRequiredMixin, View):
    """
    GET /api/docs/<doc_id>/at/<int:seq>/
    Reconstructs and returns the exact document state and text at sequence number `seq`.
    """

    def get(self, request: HttpRequest, doc_id: uuid.UUID, seq: int) -> JsonResponse:
        doc = get_object_or_404(Document, id=doc_id)

        if seq < 0 or seq > doc.head_seq:
            return JsonResponse(
                {"error": f"Invalid sequence number {seq}. Document head_seq is {doc.head_seq}."},
                status=400,
            )

        rga, text = reconstruct_state_at_seq(doc.id, seq)

        return JsonResponse(
            {
                "doc_id": str(doc.id),
                "requested_seq": seq,
                "text": text,
                "visible_len": rga.visible_len(),
                "head_seq": doc.head_seq,
            }
        )


class DocumentRevertApiView(LoginRequiredMixin, View):
    """
    POST /api/docs/<doc_id>/revert/<int:seq>/
    Generates non-destructive compensating operations that transform the current
    document state into the state at `seq` and broadcasts them to all active editors.
    """

    def post(self, request: HttpRequest, doc_id: uuid.UUID, seq: int) -> JsonResponse:
        doc = get_object_or_404(Document, id=doc_id)

        if seq < 0 or seq > doc.head_seq:
            return JsonResponse(
                {"error": f"Invalid sequence number {seq}. Document head_seq is {doc.head_seq}."},
                status=400,
            )

        site_id = f"revert_{request.user.id}_{uuid.uuid4().hex[:6]}"
        compensating_ops = generate_revert_operations(doc.id, target_seq=seq, site_id=site_id)

        if not compensating_ops:
            return JsonResponse(
                {
                    "message": f"Document is already in the target state of sequence {seq}.",
                    "head_seq": doc.head_seq,
                    "ops_applied": 0,
                }
            )

        channel_layer = get_channel_layer()
        group_name = f"doc_{doc.id}"
        applied_count = 0

        # Apply each compensating op and broadcast to active editors
        for op in compensating_ops:
            new_seq, newly_applied = apply_operation(doc.id, op, user=request.user)
            if newly_applied:
                applied_count += 1
                if channel_layer:
                    async_to_sync(channel_layer.group_send)(
                        group_name,
                        {
                            "type": "doc.op",
                            "op": op.to_dict(),
                            "seq": new_seq,
                            "sender_channel": "revert_api",
                        },
                    )

        doc.refresh_from_db(fields=["head_seq"])
        current_rga = get_or_load_document_rga(doc.id)

        return JsonResponse(
            {
                "success": True,
                "message": f"Successfully reverted to sequence {seq}.",
                "target_seq": seq,
                "new_head_seq": doc.head_seq,
                "ops_applied": applied_count,
                "current_text": current_rga.text(),
            }
        )
