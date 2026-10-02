"""
Core domain services for CRDT document loading, operation application,
snapshotting, sync catch-up, and history replay/revert.

Concurrency model
-----------------
PostgreSQL is the source of truth. Every write takes a row lock on the Document
(`select_for_update`), which serialises `server_seq` assignment across *all*
server processes. Each process additionally keeps an in-memory RGA replica per
document as a read cache. A replica remembers the highest `server_seq` it has
applied; before it is read or written, it replays any newer rows from the
operation log. That makes the cache safe when several Daphne nodes or Celery
workers write to the same document.

A per-process `threading.Lock` guards the replica object itself, because the
RGA linked list is not safe to mutate and traverse concurrently.
"""

import threading
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.db import transaction

from crdt.ops import Op
from crdt.rga import RGA
from documents.metrics import OPS_COMMITTED, SNAPSHOTS_CREATED
from documents.models import Document, Operation, Snapshot

SNAPSHOT_INTERVAL = 500


@dataclass
class _Replica:
    rga: RGA
    seq: int  # highest server_seq applied to `rga`


@dataclass(frozen=True)
class DocumentState:
    text: str
    state: dict[str, Any]
    seq: int


_cache_lock = threading.Lock()
_replicas: dict[uuid.UUID, _Replica] = {}
_doc_locks: dict[uuid.UUID, threading.Lock] = {}


def _get_doc_lock(doc_id: uuid.UUID) -> threading.Lock:
    """Get or create the per-process lock guarding a document's replica."""
    with _cache_lock:
        lock = _doc_locks.get(doc_id)
        if lock is None:
            lock = _doc_locks[doc_id] = threading.Lock()
        return lock


def clear_replica_cache() -> None:
    """Drop every in-memory replica (used by tests to simulate a process restart)."""
    with _cache_lock:
        _replicas.clear()
        _doc_locks.clear()


def _catch_up(doc_id: uuid.UUID, replica: _Replica) -> None:
    """Replay operations persisted (possibly by another process) after `replica.seq`."""
    newer = Operation.objects.filter(document_id=doc_id, server_seq__gt=replica.seq).order_by(
        "server_seq"
    )
    for record in newer.iterator():
        replica.rga.apply(Op.from_dict(record.payload))
        replica.seq = record.server_seq


def _load_replica_locked(doc_id: uuid.UUID, known_head: int | None = None) -> _Replica:
    """
    Return the up-to-date replica for `doc_id`. The caller must hold the doc lock.

    `known_head` lets a writer that already read `head_seq` under the row lock skip
    the catch-up query when the cache is current (the common single-node case).
    """
    replica = _replicas.get(doc_id)
    if replica is None:
        snapshot = Snapshot.objects.filter(document_id=doc_id).order_by("-server_seq").first()
        if snapshot is not None:
            rga = RGA.from_dict(snapshot.state, site_id=f"server_{doc_id}")
            replica = _Replica(rga=rga, seq=snapshot.server_seq)
        else:
            replica = _Replica(rga=RGA(site_id=f"server_{doc_id}"), seq=0)
        _replicas[doc_id] = replica

    if known_head is None or replica.seq < known_head:
        _catch_up(doc_id, replica)
    return replica


def get_or_load_document_rga(doc_id: uuid.UUID) -> RGA:
    """
    Return the live, up-to-date server replica. Callers must treat it as read-only;
    use `get_document_state` when you need a consistent copy.
    """
    with _get_doc_lock(doc_id):
        return _load_replica_locked(doc_id).rga


def get_document_state(doc_id: uuid.UUID) -> DocumentState:
    """Return text, serialized state and the seq they correspond to, read atomically."""
    with _get_doc_lock(doc_id):
        replica = _load_replica_locked(doc_id)
        return DocumentState(text=replica.rga.text(), state=replica.rga.to_dict(), seq=replica.seq)


def apply_operations(
    doc_id: uuid.UUID,
    ops: Sequence[Op],
    user: User | None = None,
    users: Sequence[User | None] | None = None,
) -> list[tuple[int, bool]]:
    """
    Persist a batch of operations in one transaction and apply them to the replica.

    `user` attributes every op to one author; `users` gives one author per op.
    Returns one `(server_seq, newly_applied)` pair per input op, in order. Ops whose
    `op_id` was already persisted are acknowledged with their original seq.
    """
    if not ops:
        return []
    authors = list(users) if users is not None else [user] * len(ops)
    if len(authors) != len(ops):
        raise ValueError("users must match ops one-to-one")

    with _get_doc_lock(doc_id):
        try:
            with transaction.atomic():
                # Row lock serialises seq assignment across every server process.
                doc = Document.objects.select_for_update().get(id=doc_id)
                replica = _load_replica_locked(doc_id, known_head=doc.head_seq)

                seen: dict[str, int] = dict(
                    Operation.objects.filter(
                        document_id=doc_id, op_id__in=[op.op_id for op in ops]
                    ).values_list("op_id", "server_seq")
                )

                seq = doc.head_seq
                results: list[tuple[int, bool]] = []
                records: list[Operation] = []
                snapshots: list[Snapshot] = []
                for op, author in zip(ops, authors, strict=True):
                    if op.op_id in seen:
                        results.append((seen[op.op_id], False))
                        continue
                    seq += 1
                    seen[op.op_id] = seq
                    records.append(
                        Operation(
                            document=doc,
                            server_seq=seq,
                            op_id=op.op_id,
                            site_id=op.site_id,
                            lamport=op.lamport,
                            type=op.type,
                            payload=op.to_dict(),
                            user=author,
                        )
                    )
                    replica.rga.apply(op)
                    results.append((seq, True))
                    if seq % SNAPSHOT_INTERVAL == 0:
                        snapshots.append(
                            Snapshot(document=doc, server_seq=seq, state=replica.rga.to_dict())
                        )

                if records:
                    Operation.objects.bulk_create(records)
                    Snapshot.objects.bulk_create(snapshots)
                    doc.head_seq = seq
                    doc.save(update_fields=["head_seq", "updated_at"])
                    replica.seq = seq

                    def record_metrics() -> None:
                        for record in records:
                            OPS_COMMITTED.labels(type=record.type).inc()
                        SNAPSHOTS_CREATED.inc(len(snapshots))

                    transaction.on_commit(record_metrics)
        except Exception:
            # The replica may hold ops whose transaction rolled back; rebuild it lazily.
            _replicas.pop(doc_id, None)
            raise

    return results


def apply_operation(
    doc_id: uuid.UUID,
    op: Op,
    user: User | None = None,
) -> tuple[int, bool]:
    """
    Persist a single operation with an atomic sequence number and apply it to the replica.

    Returns:
        (server_seq: int, newly_applied: bool)
    """
    return apply_operations(doc_id, [op], user=user)[0]


def broadcast_ops(
    doc_id: uuid.UUID, applied: Iterable[tuple[int, Op]], sender_channel: str
) -> None:
    """Publish applied ops to every WebSocket in the document group (sync callers only)."""
    channel_layer = get_channel_layer()
    items = [{"seq": seq, "op": op.to_dict(), "sender": sender_channel} for seq, op in applied]
    if channel_layer is None or not items:
        return
    async_to_sync(channel_layer.group_send)(f"doc_{doc_id}", {"type": "doc.ops", "items": items})


@dataclass(frozen=True)
class SyncResult:
    missed: list[dict[str, Any]]
    acked: list[str]
    head_seq: int
    newly_applied: list[tuple[int, Op]]


def sync_client_state(
    doc_id: uuid.UUID,
    last_seq: int,
    pending_ops: list[Op],
    user: User | None = None,
) -> SyncResult:
    """
    Handle client reconnect: persist ops the client buffered while offline, then
    return every op after the client's last acknowledged sequence number.
    """
    results = apply_operations(doc_id, pending_ops, user=user)
    newly_applied = [
        (seq, op) for op, (seq, is_new) in zip(pending_ops, results, strict=True) if is_new
    ]

    missed_qs = Operation.objects.filter(document_id=doc_id, server_seq__gt=last_seq).order_by(
        "server_seq"
    )
    missed = [{"seq": rec.server_seq, "op": rec.payload} for rec in missed_qs]
    head_seq = Document.objects.values_list("head_seq", flat=True).get(id=doc_id)

    return SyncResult(
        missed=missed,
        acked=[op.op_id for op in pending_ops],
        head_seq=head_seq,
        newly_applied=newly_applied,
    )


def reconstruct_state_at_seq(doc_id: uuid.UUID, target_seq: int) -> tuple[RGA, str]:
    """
    Reconstruct document state and text at any exact historical sequence number.
    """
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

    ops = Operation.objects.filter(
        document_id=doc_id, server_seq__gt=start_seq, server_seq__lte=target_seq
    ).order_by("server_seq")

    for op_rec in ops.iterator():
        rga.apply(Op.from_dict(op_rec.payload))

    return rga, rga.text()


def diff_to_ops(rga: RGA, start: int, end: int, replacement: str) -> list[Op]:
    """
    Mutate `rga` (a private working copy) so that `text[start:end]` becomes
    `replacement`, returning the minimal-ish insert/delete ops that do so.
    """
    current = rga.text()[start:end]
    ops: list[Op] = []
    # Walk opcodes right-to-left so earlier indices stay valid as we edit.
    for tag, i1, i2, j1, j2 in reversed(SequenceMatcher(None, current, replacement).get_opcodes()):
        if tag in ("replace", "delete"):
            for pos in range(start + i2 - 1, start + i1 - 1, -1):
                ops.append(rga.local_delete(pos))
        if tag in ("replace", "insert"):
            for offset, char in enumerate(replacement[j1:j2]):
                ops.append(rga.local_insert(start + i1 + offset, char))
    return ops


def generate_revert_operations(
    doc_id: uuid.UUID,
    target_seq: int,
    site_id: str,
) -> list[Op]:
    """
    Generate non-destructive compensating operations that transform the current
    document state into the historical state at `target_seq`.
    """
    current = get_document_state(doc_id)
    _, target_text = reconstruct_state_at_seq(doc_id, target_seq)
    working = RGA.from_dict(current.state, site_id=site_id)
    return diff_to_ops(working, 0, len(current.text), target_text)
