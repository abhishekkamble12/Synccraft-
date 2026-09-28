"""
Core domain services for CRDT document loading, operation application,
snapshotting, sync catch-up, and history replay/revert.
"""

from difflib import SequenceMatcher
import threading
from typing import Any
import uuid

from django.contrib.auth.models import User
from django.db import transaction

from crdt.clock import LamportClock
from crdt.ops import Op
from crdt.rga import RGA
from documents.models import Document, Operation, Snapshot


# In-memory document replica cache and per-document locking mechanism
_cache_lock = threading.Lock()
_doc_cache: dict[uuid.UUID, RGA] = {}
_doc_locks: dict[uuid.UUID, threading.Lock] = {}


def _get_doc_lock(doc_id: uuid.UUID) -> threading.Lock:
    """Get or create a threading lock for a specific document."""
    with _cache_lock:
        if doc_id not in _doc_locks:
            _doc_locks[doc_id] = threading.Lock()
        return _doc_locks[doc_id]


def get_or_load_document_rga(doc_id: uuid.UUID) -> RGA:
    """
    Load an RGA document replica into memory from database snapshots and operation logs.
    """
    doc_lock = _get_doc_lock(doc_id)
    with doc_lock:
        if doc_id in _doc_cache:
            return _doc_cache[doc_id]

        # 1. Fetch latest snapshot (if any)
        latest_snapshot = (
            Snapshot.objects.filter(document_id=doc_id).order_by("-server_seq").first()
        )

        if latest_snapshot is not None:
            rga = RGA.from_dict(latest_snapshot.state, site_id=f"server_{doc_id}")
            start_seq = latest_snapshot.server_seq
        else:
            rga = RGA(site_id=f"server_{doc_id}")
            start_seq = 0

        # 2. Replay subsequent operations
        pending_ops = (
            Operation.objects.filter(document_id=doc_id, server_seq__gt=start_seq)
            .order_by("server_seq")
        )

        for op_record in pending_ops:
            op = Op.from_dict(op_record.payload)
            rga.apply(op)

        _doc_cache[doc_id] = rga
        return rga


def apply_operation(
    doc_id: uuid.UUID,
    op: Op,
    user: User | None = None,
) -> tuple[int, bool]:
    """
    Persist an operation to PostgreSQL with an atomic sequence number and apply to the in-memory CRDT.

    Returns:
        (server_seq: int, newly_applied: bool)
    """
    doc_lock = _get_doc_lock(doc_id)
    with doc_lock:
        with transaction.atomic():
            # Lock the document row to serialize server_seq assignment
            doc = Document.objects.select_for_update().get(id=doc_id)

            # Idempotency check
            existing_op = Operation.objects.filter(op_id=op.op_id).first()
            if existing_op is not None:
                return existing_op.server_seq, False

            # Increment sequence number
            doc.head_seq += 1
            server_seq = doc.head_seq
            doc.save(update_fields=["head_seq", "updated_at"])

            # Persist Operation record
            Operation.objects.create(
                document=doc,
                server_seq=server_seq,
                op_id=op.op_id,
                site_id=op.site_id,
                lamport=op.lamport,
                type=op.type,
                payload=op.to_dict(),
                user=user,
            )

            # Apply to in-memory CRDT cache
            rga = get_or_load_document_rga(doc_id)
            rga.apply(op)

            # Periodic snapshotting (every 500 operations)
            if server_seq % 500 == 0:
                Snapshot.objects.create(
                    document=doc,
                    server_seq=server_seq,
                    state=rga.to_dict(),
                )

            return server_seq, True


def sync_client_state(
    doc_id: uuid.UUID,
    last_seq: int,
    pending_ops: list[Op],
    user: User | None = None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """
    Handle client reconnect synchronization.

    Returns:
        (missed_operations: list[dict], acked_op_ids: list[str], head_seq: int)
    """
    doc = Document.objects.get(id=doc_id)
    acked_op_ids: list[str] = []

    # 1. Apply any pending operations buffered by the client while offline
    for op in pending_ops:
        _, _ = apply_operation(doc_id, op, user=user)
        acked_op_ids.append(op.op_id)

    # 2. Fetch all missed operations since the client's last acknowledged sequence number
    missed_ops_qs = (
        Operation.objects.filter(document_id=doc_id, server_seq__gt=last_seq)
        .order_by("server_seq")
    )
    missed_ops = [
        {"seq": op_record.server_seq, "op": op_record.payload}
        for op_record in missed_ops_qs
    ]

    # Refresh head sequence
    doc.refresh_from_db(fields=["head_seq"])
    return missed_ops, acked_op_ids, doc.head_seq


def reconstruct_state_at_seq(doc_id: uuid.UUID, target_seq: int) -> tuple[RGA, str]:
    """
    Reconstruct document state and text at any exact historical sequence number.
    """
    # 1. Find the closest preceding snapshot
    snapshot = (
        Snapshot.objects.filter(document_id=doc_id, server_seq__lte=target_seq)
        .order_by("-server_seq")
        .first()
    )

    if snapshot is not None:
        rga = RGA.from_dict(snapshot.state, site_id=f"history_{doc_id}_{target_seq}")
        start_seq = snapshot.server_seq
    else:
        rga = RGA(site_id=f"history_{doc_id}_{target_seq}")
        start_seq = 0

    # 2. Replay operations up to target_seq
    ops = (
        Operation.objects.filter(
            document_id=doc_id, server_seq__gt=start_seq, server_seq__lte=target_seq
        )
        .order_by("server_seq")
    )

    for op_rec in ops:
        op = Op.from_dict(op_rec.payload)
        rga.apply(op)

    return rga, rga.text()


def generate_revert_operations(
    doc_id: uuid.UUID,
    target_seq: int,
    site_id: str,
) -> list[Op]:
    """
    Generate non-destructive compensating operations that transform the current
    document state into the historical state at `target_seq`.
    """
    current_rga = get_or_load_document_rga(doc_id)
    current_text = current_rga.text()

    _, target_text = reconstruct_state_at_seq(doc_id, target_seq)

    # Compute diff between current text and target text
    matcher = SequenceMatcher(None, current_text, target_text)
    compensating_ops: list[Op] = []

    # Temporary local RGA replica to generate sequential ops
    temp_rga = RGA.from_dict(current_rga.to_dict(), site_id=site_id)

    # Apply diff opcodes from highest index to lowest for deletes to preserve indices
    opcodes = matcher.get_opcodes()

    # We iterate and apply transformations
    for tag, i1, i2, j1, j2 in opcodes:
        if tag in ("replace", "delete"):
            # Delete characters from i2 - 1 down to i1
            for pos in range(i2 - 1, i1 - 1, -1):
                if pos < temp_rga.visible_len():
                    op = temp_rga.local_delete(pos)
                    compensating_ops.append(op)

        if tag in ("replace", "insert"):
            # Insert characters from target_text[j1:j2] at position i1
            for offset, char in enumerate(target_text[j1:j2]):
                insert_pos = min(i1 + offset, temp_rga.visible_len())
                op = temp_rga.local_insert(insert_pos, char)
                compensating_ops.append(op)

    return compensating_ops
