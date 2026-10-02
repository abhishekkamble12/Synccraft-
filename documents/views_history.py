"""
REST API endpoints for document history, state-at-sequence time travel, and non-destructive revert.
"""

import uuid
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from documents.models import Document, Operation
from documents.permissions import can_edit, get_role
from documents.services import (
    apply_operations,
    broadcast_ops,
    committed,
    generate_revert_operations,
    get_document_state,
    reconstruct_state_at_seq,
)

HISTORY_PAGE_SIZE = 500


def _get_accessible_document(request: HttpRequest, doc_id: uuid.UUID) -> tuple[Document, str]:
    doc = get_object_or_404(Document, id=doc_id)
    role = get_role(doc, request.user)
    if role is None:
        # 404 rather than 403 so document ids cannot be probed.
        raise Http404("Document not found")
    return doc, role


class DocumentHistoryApiView(LoginRequiredMixin, View):
    """
    GET /api/docs/<doc_id>/history/?after=<seq>
    Returns a page of the operation log, oldest first.
    """

    def get(self, request: HttpRequest, doc_id: uuid.UUID) -> JsonResponse:
        doc, _ = _get_accessible_document(request, doc_id)
        try:
            after = max(0, int(request.GET.get("after", 0)))
        except ValueError:
            return JsonResponse({"error": "'after' must be an integer"}, status=400)

        ops_qs = (
            Operation.objects.filter(document=doc, server_seq__gt=after)
            .select_related("user")
            .order_by("server_seq")[:HISTORY_PAGE_SIZE]
        )

        history_entries: list[dict[str, Any]] = [
            {
                "seq": op.server_seq,
                "op_id": op.op_id,
                "type": op.type,
                "site_id": op.site_id,
                "user": op.user.username if op.user else "system",
                "timestamp": op.created_at.isoformat(),
                # "text" for run-length ops, "char" for ops logged before RLE.
                "text": op.payload.get("text", op.payload.get("char"))
                if op.type == "insert"
                else None,
            }
            for op in ops_qs
        ]

        return JsonResponse(
            {
                "doc_id": str(doc.id),
                "title": doc.title,
                "head_seq": doc.head_seq,
                "total_ops": doc.head_seq,
                "history": history_entries,
                "next_after": history_entries[-1]["seq"]
                if len(history_entries) == HISTORY_PAGE_SIZE
                else None,
            }
        )


class DocumentStateAtSeqApiView(LoginRequiredMixin, View):
    """
    GET /api/docs/<doc_id>/at/<int:seq>/
    Reconstructs and returns the exact document state and text at sequence number `seq`.
    """

    def get(self, request: HttpRequest, doc_id: uuid.UUID, seq: int) -> JsonResponse:
        doc, _ = _get_accessible_document(request, doc_id)

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
        doc, role = _get_accessible_document(request, doc_id)
        if not can_edit(role):
            return JsonResponse({"error": "Viewers cannot revert documents."}, status=403)

        if seq < 0 or seq > doc.head_seq:
            return JsonResponse(
                {"error": f"Invalid sequence number {seq}. Document head_seq is {doc.head_seq}."},
                status=400,
            )

        site_id = f"revert_{request.user.pk}_{uuid.uuid4().hex[:6]}"
        compensating_ops, base_seq = generate_revert_operations(
            doc.id, target_seq=seq, site_id=site_id
        )

        if not compensating_ops:
            return JsonResponse(
                {
                    "success": True,
                    "message": f"Document is already in the target state of sequence {seq}.",
                    "new_head_seq": doc.head_seq,
                    "ops_applied": 0,
                    "current_text": get_document_state(doc.id).text,
                }
            )

        results = apply_operations(
            doc.id,
            compensating_ops,
            user=request.user,  # type: ignore[arg-type]
            base_seqs=[base_seq] * len(compensating_ops),
        )
        applied = committed(compensating_ops, results)
        broadcast_ops(doc.id, applied, sender_channel="revert_api")
        if any(not r.ok for r in results):
            return JsonResponse(
                {"error": "The document changed underneath the revert; retry."}, status=409
            )

        state = get_document_state(doc.id)
        return JsonResponse(
            {
                "success": True,
                "message": f"Successfully reverted to sequence {seq}.",
                "target_seq": seq,
                "new_head_seq": state.seq,
                "ops_applied": len(applied),
                "current_text": state.text,
            }
        )
