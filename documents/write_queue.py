"""
Per-document group commit for WebSocket edits.

Each keystroke is one CRDT op. Committing them one transaction at a time makes
every socket wait on its own fsync and serialises all sockets behind the single
database thread. Instead, consumers enqueue ops here and get a future back; one
writer task per document drains whatever has queued up, commits it as a single
batch, broadcasts the newly applied ops, and resolves the futures.

Under light load a batch is one op (no added latency); under contention batches
grow automatically, amortising transaction overhead across all waiting clients.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from channels.db import database_sync_to_async
from channels.layers import get_channel_layer

from crdt.ops import Op
from documents.metrics import COMMIT_BATCH_SIZE, COMMIT_SECONDS
from documents.services import apply_operations

logger = logging.getLogger(__name__)

MAX_BATCH = 256
IDLE_SHUTDOWN_SEC = 30.0


@dataclass
class _Pending:
    op: Op
    user: Any
    sender_channel: str
    future: asyncio.Future[tuple[int, bool]]


class DocumentWriter:
    def __init__(self, doc_id: uuid.UUID) -> None:
        self.doc_id = doc_id
        self.group_name = f"doc_{doc_id}"
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[_Pending] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    def submit(self, op: Op, user: Any, sender_channel: str) -> asyncio.Future[tuple[int, bool]]:
        future: asyncio.Future[tuple[int, bool]] = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(_Pending(op, user, sender_channel, future))
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

    async def _commit(self, batch: list[_Pending]) -> None:
        started = time.perf_counter()
        try:
            results = await database_sync_to_async(apply_operations)(
                self.doc_id, [p.op for p in batch], users=[p.user for p in batch]
            )
        except Exception:
            if len(batch) == 1:
                logger.exception("Op %s rejected on doc %s", batch[0].op.op_id, self.doc_id)
                _resolve_exception(batch[0], RuntimeError("operation rejected"))
                return
            # Isolate the bad op(s) so one client's malformed edit can't fail everyone's.
            for pending in batch:
                await self._commit([pending])
            return

        COMMIT_BATCH_SIZE.observe(len(batch))
        COMMIT_SECONDS.observe(time.perf_counter() - started)

        # One fan-out per batch instead of per op: N sockets x B ops become N frames.
        items = [
            {"seq": seq, "op": p.op.to_dict(), "sender": p.sender_channel}
            for p, (seq, newly_applied) in zip(batch, results, strict=True)
            if newly_applied
        ]
        channel_layer = get_channel_layer()
        if items and channel_layer is not None:
            await channel_layer.group_send(self.group_name, {"type": "doc.ops", "items": items})
        for pending, result in zip(batch, results, strict=True):
            if not pending.future.done():
                pending.future.set_result(result)


def _resolve_exception(pending: _Pending, exc: Exception) -> None:
    if not pending.future.done():
        pending.future.set_exception(exc)


_writers: dict[uuid.UUID, DocumentWriter] = {}


def submit_op(
    doc_id: uuid.UUID, op: Op, user: Any, sender_channel: str
) -> asyncio.Future[tuple[int, bool]]:
    """Queue `op` for group commit; the future resolves to (server_seq, newly_applied)."""
    writer = _writers.get(doc_id)
    if writer is None or writer.loop is not asyncio.get_running_loop():
        writer = _writers[doc_id] = DocumentWriter(doc_id)
    return writer.submit(op, user, sender_channel)
