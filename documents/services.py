"""
Core domain services for CRDT document loading, operation application,
snapshotting, sync catch-up, tombstone garbage collection and history.

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

Untrusted input
---------------
Every op is validated against the up-to-date replica before it gets a seq
(`RGA.validate`: ids consistent with the site and the run length, parent and
delete targets exist, ids strictly increase per site and exceed the parent's).
A site_id is bound to the first user who writes with it. The first rejected op
retires its site, so a site's committed ops are always a prefix of what it sent.

Tombstone GC
------------
Each live client reports the oldest seq its unacknowledged ops can be based on.
`compact_document` drops every tombstone deleted at or before the minimum of
those reports (`stable_seq`), except tombstones still named by ops committed
after it. Ops based on a state older than `Document.gc_seq` are rejected as
`stale` and the client rebases them (crdt/rebase.py).
"""

import threading
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from crdt.ids import CharId
from crdt.ops import Op
from crdt.rga import RGA
from documents.metrics import (
    COMPACTIONS,
    OPS_COMMITTED,
    OPS_REJECTED,
    SNAPSHOTS_CREATED,
    TOMBSTONES_COLLECTED,
)
from documents.models import Document, Operation, SiteSession, Snapshot

SNAPSHOT_INTERVAL = 500


@dataclass
class _Replica:
    rga: RGA
    seq: int  # highest server_seq applied to `rga`
    gc_seq: int  # Document.gc_seq the replica was loaded against


@dataclass(frozen=True)
class DocumentState:
    text: str
    state: dict[str, Any]
    seq: int
    gc_seq: int = 0


@dataclass(frozen=True)
class OpResult:
    """Outcome of submitting one op: committed (new or duplicate) or rejected."""

    seq: int | None
    new: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


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
        _ack_buffer.clear()
        _ack_flushed_at.clear()


def _catch_up(doc_id: uuid.UUID, replica: _Replica) -> None:
    """Replay operations persisted (possibly by another process) after `replica.seq`."""
    newer = Operation.objects.filter(document_id=doc_id, server_seq__gt=replica.seq).order_by(
        "server_seq"
    )
    for record in newer.iterator():
        replica.rga.apply(Op.from_dict(record.payload))
        replica.seq = record.server_seq


def _load_replica_locked(doc_id: uuid.UUID, known_head: int, known_gc: int) -> _Replica:
    """
    Return the up-to-date replica for `doc_id`. The caller must hold the doc lock.

    `known_head` / `known_gc` come from the Document row. If another process has
    compacted the document since this replica was loaded, the replica is rebuilt
    from the (smaller) compacted snapshot.
    """
    replica = _replicas.get(doc_id)
    if replica is not None and replica.gc_seq < known_gc:
        replica = None
    if replica is None:
        snapshot = Snapshot.objects.filter(document_id=doc_id).order_by("-server_seq").first()
        if snapshot is not None:
            rga = RGA.from_dict(snapshot.state, site_id=f"server_{doc_id}")
            replica = _Replica(rga=rga, seq=snapshot.server_seq, gc_seq=known_gc)
        else:
            replica = _Replica(rga=RGA(site_id=f"server_{doc_id}"), seq=0, gc_seq=known_gc)
        _replicas[doc_id] = replica

    if replica.seq < known_head:
        _catch_up(doc_id, replica)
    return replica


def _load_replica(doc_id: uuid.UUID) -> _Replica:
    head, gc = Document.objects.values_list("head_seq", "gc_seq").get(id=doc_id)
    return _load_replica_locked(doc_id, known_head=head, known_gc=gc)


def get_or_load_document_rga(doc_id: uuid.UUID) -> RGA:
    """
    Return the live, up-to-date server replica. Callers must treat it as read-only;
    use `get_document_state` when you need a consistent copy.
    """
    with _get_doc_lock(doc_id):
        return _load_replica(doc_id).rga


def get_document_state(doc_id: uuid.UUID) -> DocumentState:
    """Return text, serialized state and the seq they correspond to, read atomically."""
    with _get_doc_lock(doc_id):
        replica = _load_replica(doc_id)
        return DocumentState(
            text=replica.rga.text(),
            state=replica.rga.to_dict(),
            seq=replica.seq,
            gc_seq=replica.gc_seq,
        )


# -----------------------------------------------------------------------------
# Write path
# -----------------------------------------------------------------------------


def apply_operations(
    doc_id: uuid.UUID,
    ops: Sequence[Op],
    user: User | None = None,
    users: Sequence[User | None] | None = None,
    base_seqs: Sequence[int | None] | None = None,
) -> list[OpResult]:
    """
    Validate and persist a batch of operations in one transaction, applying the
    accepted ones to the replica.

    `user` attributes every op to one author; `users` gives one author per op.
    `base_seqs` gives, per op, the server seq of the state it was created from
    (None for trusted server-side callers that just read the latest state).

    Returns one `OpResult` per input op, in order. An op whose `op_id` was
    already persisted is acknowledged with its original seq (`new=False`).
    """
    if not ops:
        return []
    authors = list(users) if users is not None else [user] * len(ops)
    bases = list(base_seqs) if base_seqs is not None else [None] * len(ops)
    if len(authors) != len(ops) or len(bases) != len(ops):
        raise ValueError("users and base_seqs must match ops one-to-one")

    with _get_doc_lock(doc_id):
        try:
            with transaction.atomic():
                # Row lock serialises seq assignment across every server process.
                doc = Document.objects.select_for_update().get(id=doc_id)
                replica = _load_replica_locked(doc_id, doc.head_seq, doc.gc_seq)

                existing: dict[str, tuple[uuid.UUID, int]] = {
                    op_id: (document_id, seq)
                    for op_id, document_id, seq in Operation.objects.filter(
                        op_id__in=[op.op_id for op in ops]
                    ).values_list("op_id", "document_id", "server_seq")
                }
                sessions: dict[str, SiteSession] = {
                    s.site_id: s
                    for s in SiteSession.objects.filter(
                        document=doc, site_id__in={op.site_id for op in ops}
                    )
                }
                new_sessions: dict[str, SiteSession] = {}
                retired: set[str] = set()

                seq = doc.head_seq
                results: list[OpResult] = []
                records: list[Operation] = []
                snapshots: list[Snapshot] = []
                for op, author, base in zip(ops, authors, bases, strict=True):
                    prior = existing.get(op.op_id)
                    if prior is not None and prior[0] == doc.id:
                        results.append(OpResult(prior[1], False))
                        continue

                    session = sessions.get(op.site_id) or new_sessions.get(op.site_id)
                    if prior is not None:
                        error: str | None = "duplicate_op_id"
                    elif (
                        author is not None
                        and session is not None
                        and session.user_id is not None
                        and session.user_id != author.pk
                    ):
                        error = "site_owned_by_other_user"
                    elif op.site_id in retired or (session is not None and session.retired):
                        error = "site_retired"
                    elif base is not None and base < doc.gc_seq:
                        error = "stale"
                    else:
                        error = replica.rga.validate(op)

                    if session is None:
                        session = new_sessions[op.site_id] = SiteSession(
                            document=doc, site_id=op.site_id, user=author
                        )
                    if error is not None:
                        results.append(OpResult(None, False, error))
                        # Never let one user retire a site that belongs to another.
                        if error != "site_owned_by_other_user":
                            retired.add(op.site_id)
                        continue

                    seq += 1
                    existing[op.op_id] = (doc.id, seq)
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
                    results.append(OpResult(seq, True))
                    if seq % SNAPSHOT_INTERVAL == 0:
                        snapshots.append(
                            Snapshot(document=doc, server_seq=seq, state=replica.rga.to_dict())
                        )

                for site_id in retired:
                    if site_id in new_sessions:
                        new_sessions[site_id].retired = True
                if new_sessions:
                    SiteSession.objects.bulk_create(new_sessions.values(), ignore_conflicts=True)
                known_retired = [s for s in retired if s in sessions]
                if known_retired:
                    SiteSession.objects.filter(document=doc, site_id__in=known_retired).update(
                        retired=True
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
                    for result in results:
                        if result.error is not None:
                            OPS_REJECTED.labels(reason=result.error).inc()
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
    base_seq: int | None = None,
) -> OpResult:
    """Validate and persist a single operation."""
    return apply_operations(doc_id, [op], user=user, base_seqs=[base_seq])[0]


def broadcast_ops(
    doc_id: uuid.UUID, applied: Iterable[tuple[int, Op]], sender_channel: str
) -> None:
    """Publish applied ops to every WebSocket in the document group (sync callers only)."""
    channel_layer = get_channel_layer()
    items = [{"seq": seq, "op": op.to_dict(), "sender": sender_channel} for seq, op in applied]
    if channel_layer is None or not items:
        return
    async_to_sync(channel_layer.group_send)(f"doc_{doc_id}", {"type": "doc.ops", "items": items})


def committed(ops: Sequence[Op], results: Sequence[OpResult]) -> list[tuple[int, Op]]:
    """The `(seq, op)` pairs a batch newly persisted, for broadcasting."""
    return [
        (result.seq, op)
        for op, result in zip(ops, results, strict=True)
        if result.new and result.seq is not None
    ]


@dataclass(frozen=True)
class SyncResult:
    missed: list[dict[str, Any]]
    acked: list[str]
    rejected: list[dict[str, str]]
    head_seq: int
    gc_seq: int
    newly_applied: list[tuple[int, Op]]


def sync_client_state(
    doc_id: uuid.UUID,
    last_seq: int,
    pending_ops: list[Op],
    user: User | None = None,
    base_seqs: Sequence[int | None] | None = None,
) -> SyncResult:
    """
    Handle a client sync: persist ops the client buffered (e.g. while offline),
    then return every op after the client's last contiguous sequence number.
    """
    results = apply_operations(doc_id, pending_ops, user=user, base_seqs=base_seqs)

    missed_qs = Operation.objects.filter(document_id=doc_id, server_seq__gt=last_seq).order_by(
        "server_seq"
    )
    missed = [{"seq": rec.server_seq, "op": rec.payload} for rec in missed_qs]
    head_seq, gc_seq = Document.objects.values_list("head_seq", "gc_seq").get(id=doc_id)

    return SyncResult(
        missed=missed,
        acked=[op.op_id for op, r in zip(pending_ops, results, strict=True) if r.ok],
        rejected=[
            {"op_id": op.op_id, "reason": r.error}
            for op, r in zip(pending_ops, results, strict=True)
            if r.error is not None
        ],
        head_seq=head_seq,
        gc_seq=gc_seq,
        newly_applied=committed(pending_ops, results),
    )


# -----------------------------------------------------------------------------
# Site sessions & tombstone garbage collection
# -----------------------------------------------------------------------------


def record_site_ack(doc_id: uuid.UUID, site_id: str, user: User | None, acked_seq: int) -> bool:
    """
    Record that `site_id` will never again submit an op based on a state older
    than `acked_seq`. Returns False if the site belongs to another user.
    """
    session, created = SiteSession.objects.get_or_create(
        document_id=doc_id, site_id=site_id, defaults={"user": user, "acked_seq": acked_seq}
    )
    if created:
        return True
    if user is not None and session.user_id is not None and session.user_id != user.pk:
        return False
    SiteSession.objects.filter(pk=session.pk).update(acked_seq=acked_seq, last_seen=timezone.now())
    return True


# Heartbeat watermarks are buffered per process and written in one transaction
# per document at most every ACK_FLUSH_INTERVAL_SEC. Writing each heartbeat on its
# own costs an fsync on the same DB thread the group commit uses, which showed up
# as p99 commit latency. A buffered watermark is only ever older than the truth,
# which makes GC more conservative, never less safe.
_ack_buffer: dict[uuid.UUID, dict[str, tuple[int | None, int, Any]]] = {}
_ack_flushed_at: dict[uuid.UUID, float] = {}


def heartbeat(doc_id: uuid.UUID, site_id: str | None, user: User | None, acked_seq: int) -> int:
    """Buffer a client's GC watermark (if it edits as `site_id`) and return the head seq."""
    now = time.monotonic()
    with _cache_lock:
        if site_id is not None:
            _ack_buffer.setdefault(doc_id, {})[site_id] = (
                user.pk if user is not None else None,
                acked_seq,
                timezone.now(),
            )
        due = now - _ack_flushed_at.get(doc_id, 0.0) >= settings.ACK_FLUSH_INTERVAL_SEC
    if due:
        flush_site_acks(doc_id)
    head: int = Document.objects.values_list("head_seq", flat=True).get(id=doc_id)
    return head


def flush_site_acks(doc_id: uuid.UUID) -> None:
    """Write this process's buffered heartbeat watermarks for one document."""
    with _cache_lock:
        entries = _ack_buffer.pop(doc_id, {})
        _ack_flushed_at[doc_id] = time.monotonic()
    if not entries:
        return
    with transaction.atomic():
        existing = {
            s.site_id: s
            for s in SiteSession.objects.filter(document_id=doc_id, site_id__in=list(entries))
        }
        to_create: list[SiteSession] = []
        to_update: list[SiteSession] = []
        for site_id, (user_id, acked_seq, seen_at) in entries.items():
            session = existing.get(site_id)
            if session is None:
                to_create.append(
                    SiteSession(
                        document_id=doc_id,
                        site_id=site_id,
                        user_id=user_id,
                        acked_seq=acked_seq,
                        last_seen=seen_at,
                    )
                )
            elif user_id is None or session.user_id is None or session.user_id == user_id:
                session.acked_seq = acked_seq
                session.last_seen = seen_at
                to_update.append(session)
        SiteSession.objects.bulk_create(to_create, ignore_conflicts=True)
        SiteSession.objects.bulk_update(to_update, ["acked_seq", "last_seen"])


def touch_site(doc_id: uuid.UUID, site_id: str) -> None:
    """Refresh a session's last_seen (on disconnect, so the TTL counts from then)."""
    SiteSession.objects.filter(document_id=doc_id, site_id=site_id).update(last_seen=timezone.now())


def stable_seq(doc: Document) -> int:
    """
    The highest seq S such that no live site can still submit an op based on a
    state older than S. Sessions not seen for SITE_SESSION_TTL_SEC stop counting;
    if they come back, their old ops are rejected as stale and they rebase.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.SITE_SESSION_TTL_SEC)
    low = SiteSession.objects.filter(
        document_id=doc.id, retired=False, acked_seq__isnull=False, last_seen__gte=cutoff
    ).aggregate(low=Min("acked_seq"))["low"]
    return doc.head_seq if low is None else min(int(low), doc.head_seq)


@dataclass(frozen=True)
class CompactionResult:
    gc_seq: int
    head_seq: int
    tombstones_removed: int
    nodes_before: int
    nodes_after: int


def compact_document(doc_id: uuid.UUID, min_ops: int | None = None) -> CompactionResult | None:
    """
    Garbage-collect tombstones up to the stable seq, if it advanced by at least
    `min_ops` (default settings.GC_MIN_OPS) since the last compaction.

    Writes a compacted snapshot at the current head and swaps it in as this
    process's replica. Other processes notice `Document.gc_seq` moved and reload.
    """
    threshold = settings.GC_MIN_OPS if min_ops is None else min_ops
    flush_site_acks(doc_id)
    with _get_doc_lock(doc_id):
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=doc_id)
                stable = stable_seq(doc)
                if stable <= doc.gc_seq or stable - doc.gc_seq < threshold:
                    return None

                rga, _ = reconstruct_state_at_seq(doc_id, stable)
                rga.site_id = f"server_{doc_id}"
                nodes_before = rga.total_len()

                # Ops committed after the stable point may have been created before
                # their author saw a deletion, so the tombstones they name must stay.
                later = [
                    Op.from_dict(payload)
                    for payload in Operation.objects.filter(document=doc, server_seq__gt=stable)
                    .order_by("server_seq")
                    .values_list("payload", flat=True)
                ]
                keep: set[CharId] = set()
                for op in later:
                    if op.type == "insert":
                        assert op.parent_id is not None
                        keep.add(op.parent_id)
                    else:
                        keep.update(op.target_ids())
                removed = rga.compact(keep)
                for op in later:
                    rga.apply(op)

                Snapshot.objects.filter(document=doc, server_seq=doc.head_seq).delete()
                Snapshot.objects.create(document=doc, server_seq=doc.head_seq, state=rga.to_dict())
                doc.gc_seq = stable
                doc.save(update_fields=["gc_seq"])
                replica = _Replica(rga=rga, seq=doc.head_seq, gc_seq=stable)
        except Exception:
            _replicas.pop(doc_id, None)
            raise
        _replicas[doc_id] = replica

    COMPACTIONS.inc()
    TOMBSTONES_COLLECTED.inc(removed)
    return CompactionResult(
        gc_seq=stable,
        head_seq=replica.seq,
        tombstones_removed=removed,
        nodes_before=nodes_before,
        nodes_after=nodes_before - removed,
    )


# -----------------------------------------------------------------------------
# History
# -----------------------------------------------------------------------------


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
    `replacement`, returning one run op per changed region.
    """
    current = rga.text()[start:end]
    ops: list[Op] = []
    # Walk opcodes right-to-left so earlier indices stay valid as we edit.
    for tag, i1, i2, j1, j2 in reversed(SequenceMatcher(None, current, replacement).get_opcodes()):
        if tag in ("replace", "delete"):
            ops.append(rga.local_delete(start + i1, i2 - i1))
        if tag in ("replace", "insert"):
            ops.append(rga.local_insert(start + i1, replacement[j1:j2]))
    return ops


def generate_revert_operations(
    doc_id: uuid.UUID,
    target_seq: int,
    site_id: str,
) -> tuple[list[Op], int]:
    """
    Generate non-destructive compensating operations that transform the current
    document state into the historical state at `target_seq`. Returns the ops and
    the seq of the state they were computed against (their base).
    """
    current = get_document_state(doc_id)
    _, target_text = reconstruct_state_at_seq(doc_id, target_seq)
    working = RGA.from_dict(current.state, site_id=site_id)
    return diff_to_ops(working, 0, len(current.text), target_text), current.seq
