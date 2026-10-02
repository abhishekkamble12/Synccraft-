"""
Per-document group commit for WebSocket edits.

Each edit is one CRDT op. Committing them one transaction at a time makes every
socket wait on its own fsync and serialises all sockets behind the single
database thread. Instead, consumers enqueue ops here and get a future back; one
writer task per document drains whatever has queued up, commits it as a single
batch, broadcasts the newly applied ops, and resolves the futures.

Under light load a batch is one op (no added latency); under contention batches
grow automatically, amortising transaction overhead across all waiting clients.

The writer also runs tombstone garbage collection for its document, at most
every GC_CHECK_INTERVAL_SEC, between batches.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.conf import settings

from crdt.ops import Op
from documents.metrics import COMMIT_BATCH_SIZE, COMMIT_SECONDS
from documents.services import OpResult, apply_operations, compact_document

logger = logging.getLogger(__name__)

MAX_BATCH = 256
IDLE_SHUTDOWN_SEC = 30.0


@dataclass
class _Pending:
    op: Op
    user: Any
    base_seq: int | None
    sender_channel: str
    future: asyncio.Future[OpResult]


class DocumentWriter:
    def __init__(self, doc_id: uuid.UUID) -> None:
        self.doc_id = doc_id
        self.group_name = f"doc_{doc_id}"
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[_Pending] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.next_gc_check = time.monotonic() + settings.GC_CHECK_INTERVAL_SEC

    def submit(
        self, op: Op, user: Any, base_seq: int | None, sender_channel: str
    ) -> asyncio.Future[OpResult]:
        future: asyncio.Future[OpResult] = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(_Pending(op, user, base_seq, sender_channel, future))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"writer-{self.doc_id}")
        return future

    async def _run(self) -> None:
        while True:
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=IDLE_SHUTDOWN_SEC)
            except TimeoutError:
                # No await between the emptiness check and deregistration, so a
                # concurrent submit() either lands before (and we loop) or finds
                # no writer and starts a fresh one.
                if self.queue.empty():
                    if _writers.get(self.doc_id) is self:
                        del _writers[self.doc_id]
                    return
                continue

            batch = [first]
            while len(batch) < MAX_BATCH and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            await self._commit(batch)
            await self._maybe_compact()

    async def _commit(self, batch: list[_Pending]) -> None:
        started = time.perf_counter()
        try:
            results = await database_sync_to_async(apply_operations)(
                self.doc_id,
                [p.op for p in batch],
                users=[p.user for p in batch],
                base_seqs=[p.base_seq for p in batch],
            )
        except Exception:
            if len(batch) == 1:
                logger.exception("Op %s failed on doc %s", batch[0].op.op_id, self.doc_id)
                _resolve(batch[0], OpResult(None, False, "internal_error"))
                return
            # Isolate the failing op so one client's edit can't fail everyone's.
            for pending in batch:
                await self._commit([pending])
            return

        COMMIT_BATCH_SIZE.observe(len(batch))
        COMMIT_SECONDS.observe(time.perf_counter() - started)

        # One fan-out per batch instead of per op: N sockets x B ops become N frames.
        items = [
            {"seq": result.seq, "op": p.op.to_dict(), "sender": p.sender_channel}
            for p, result in zip(batch, results, strict=True)
            if result.new
        ]
        channel_layer = get_channel_layer()
        if items and channel_layer is not None:
            try:
                await channel_layer.group_send(self.group_name, {"type": "doc.ops", "items": items})
            except Exception:
                # The ops are committed; only the fan-out failed (e.g. Redis restarting).
                # Still ack them. Peers see the seq gap (or a heartbeat head ahead of
                # them) and fetch the ops from the log.
                logger.exception("Broadcast failed on doc %s; peers will gap-repair", self.doc_id)
        for pending, result in zip(batch, results, strict=True):
            _resolve(pending, result)

    async def _maybe_compact(self) -> None:
        now = time.monotonic()
        if now < self.next_gc_check:
            return
        self.next_gc_check = now + settings.GC_CHECK_INTERVAL_SEC
        try:
            result = await database_sync_to_async(compact_document)(self.doc_id)
        except Exception:
            logger.exception("Compaction failed on doc %s", self.doc_id)
            return
        if result is not None:
            logger.info(
                "Compacted doc %s up to seq %s: %s tombstones removed (%s -> %s nodes)",
                self.doc_id,
                result.gc_seq,
                result.tombstones_removed,
                result.nodes_before,
                result.nodes_after,
            )


def _resolve(pending: _Pending, result: OpResult) -> None:
    if not pending.future.done():
        pending.future.set_result(result)


_writers: dict[uuid.UUID, DocumentWriter] = {}


def submit_op(
    doc_id: uuid.UUID, op: Op, user: Any, sender_channel: str, base_seq: int | None = None
) -> asyncio.Future[OpResult]:
    """Queue `op` for group commit; the future resolves to its OpResult."""
    writer = _writers.get(doc_id)
    if writer is None or writer.loop is not asyncio.get_running_loop():
        writer = _writers[doc_id] = DocumentWriter(doc_id)
    return writer.submit(op, user, base_seq, sender_channel)
