"""
AI Agent as a first-class CRDT peer in collaborative sessions.

The AI peer interacts with the document strictly through the same CRDT operations
(inserts, deletes, Lamport clock updates) as human users, so its edits merge with
concurrent human edits under the same convergence guarantees.

Design note: LLM output is buffered and validated *before* any op is emitted, so a
response rejected by the output guardrails never leaves half-written text in the
document. The ops themselves are then committed in one batch and broadcast.
"""

import logging
import time
import uuid
from typing import Any

from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import get_channel_layer
from django.contrib.auth.models import User

from ai.client import BaseLLMClient, get_configured_llm_client
from ai.guards import (
    sanitize_document_text,
    validate_input_bounds,
    validate_output_bounds,
)
from ai.prompts import (
    PROMPTS,
    SYSTEM_PROMPT_COAUTHOR,
)
from crdt.ids import CharId
from crdt.rga import RGA
from documents.metrics import AI_JOB_SECONDS, AI_JOBS
from documents.models import AIJob
from documents.services import (
    apply_operations,
    broadcast_ops,
    committed,
    diff_to_ops,
    get_document_state,
)

logger = logging.getLogger(__name__)

CANCEL_CHECK_INTERVAL_SEC = 0.5


class AnchorDeletedError(Exception):
    """Raised when an anchor character has been deleted by a human collaborator."""


class JobCancelledError(Exception):
    """Raised when the AI job is cancelled mid-execution."""


class AIPeer:
    """
    Autonomous AI co-author acting as a collaborative CRDT peer.

    All public methods are synchronous and safe to call from a Celery worker or a
    `database_sync_to_async` thread.
    """

    def __init__(
        self,
        doc_id: uuid.UUID,
        job_id: uuid.UUID | str,
        user: User | None = None,
        llm_client: BaseLLMClient | None = None,
        site_id: str | None = None,
    ) -> None:
        self.doc_id = doc_id
        self.job_id = str(job_id)
        self.user = user
        self.site_id = site_id or f"ai-{uuid.uuid4().hex[:12]}"
        self.llm_client = llm_client or get_configured_llm_client()
        self.channel_layer = get_channel_layer()
        self.group_name = f"doc_{self.doc_id}"

    def check_cancelled(self) -> bool:
        """Check if job was cancelled in the database."""
        return AIJob.objects.filter(id=self.job_id, status="cancelled").exists()

    def _group_send(self, data: dict[str, Any]) -> None:
        if not self.channel_layer:
            return
        try:
            async_to_sync(self.channel_layer.group_send)(
                self.group_name,
                {"type": "doc.presence", "data": data, "sender_channel": f"ai_{self.site_id}"},
            )
        except Exception as exc:
            logger.warning("AI broadcast failed for job %s: %s", self.job_id, exc)

    def broadcast_presence(self, status_text: str, cursor_pos: int | None = None) -> None:
        """Broadcast ephemeral AI presence cursor to other collaborators."""
        self._group_send(
            {
                "type": "presence",
                "user": f"AI Co-Author ({status_text})",
                "color": "#8b5cf6",
                "cursor_pos": cursor_pos,
                "is_ai": True,
            }
        )

    def broadcast_status(self, status: str, message: str = "") -> None:
        """Broadcast AI job lifecycle events to WebSocket clients."""
        self._group_send(
            {"type": "ai_status", "job_id": self.job_id, "status": status, "message": message}
        )

    def resolve_anchor_range(
        self, rga: RGA, anchor_start_str: str | None, anchor_end_str: str | None
    ) -> tuple[int, int, str]:
        """
        Locate the start and end visible positions for given anchor CharIds.
        Raises AnchorDeletedError if an explicit anchor was deleted.
        """
        full_text = rga.text()
        if not anchor_start_str and not anchor_end_str:
            return 0, len(full_text), full_text

        start_pos = 0
        if anchor_start_str:
            try:
                start_cid = CharId.from_str(anchor_start_str)
            except ValueError as exc:
                raise AnchorDeletedError(f"Malformed start anchor {anchor_start_str!r}.") from exc
            pos = rga.pos_of_char_id(start_cid)
            if pos is None:
                raise AnchorDeletedError(f"Start anchor {anchor_start_str} was deleted.")
            start_pos = pos

        end_pos = len(full_text)
        if anchor_end_str:
            try:
                end_cid = CharId.from_str(anchor_end_str)
            except ValueError as exc:
                raise AnchorDeletedError(f"Malformed end anchor {anchor_end_str!r}.") from exc
            pos = rga.pos_of_char_id(end_cid)
            if pos is None:
                raise AnchorDeletedError(f"End anchor {anchor_end_str} was deleted.")
            end_pos = pos + 1

        if start_pos > end_pos:
            start_pos, end_pos = end_pos, start_pos

        return start_pos, end_pos, full_text[start_pos:end_pos]

    def _build_prompt(self, kind: str, instruction: str, target_text: str) -> str:
        template = PROMPTS.get(f"{kind}_v1", PROMPTS["rewrite_v1"])
        sanitized_target = sanitize_document_text(target_text)
        if kind == "rewrite":
            return template.format(
                instruction=instruction or "Make it better", text=sanitized_target
            )
        if kind in ("grammar", "shorten", "continue"):
            return template.format(text=sanitized_target)
        return template.format(instruction=instruction, text=sanitized_target)

    async def _generate(self, prompt: str) -> str:
        """Collect the LLM stream, polling for cancellation at most every 0.5s."""
        chunks: list[str] = []
        last_check = 0.0
        async for chunk in self.llm_client.stream_completion(
            prompt=prompt, system_prompt=SYSTEM_PROMPT_COAUTHOR
        ):
            now = time.monotonic()
            if now - last_check >= CANCEL_CHECK_INTERVAL_SEC:
                last_check = now
                if await sync_to_async(self.check_cancelled)():
                    raise JobCancelledError("AI job was cancelled by user.")
            chunks.append(chunk)
        return "".join(chunks)

    def run(self, kind: str, instruction: str = "") -> dict[str, Any]:
        """
        Execute an AI job end to end: read the anchored range, call the LLM,
        validate the output, and commit the edit as CRDT ops.
        """
        start_time = time.monotonic()
        job = AIJob.objects.filter(id=self.job_id).first()
        if job is None:
            raise ValueError(f"AIJob {self.job_id} not found.")

        try:
            if job.status == "cancelled":
                raise JobCancelledError("AI job was cancelled before it started.")

            job.status = "running"
            job.save(update_fields=["status", "updated_at"])
            self.broadcast_status("running")
            self.broadcast_presence("Thinking...")

            rga = RGA.from_dict(get_document_state(self.doc_id).state, site_id=self.site_id)
            _, _, target_text = self.resolve_anchor_range(rga, job.anchor_start, job.anchor_end)

            valid, err = validate_input_bounds(target_text, instruction)
            if not valid:
                raise ValueError(err)

            self.broadcast_presence("Writing...")
            raw_generated = async_to_sync(self._generate)(
                self._build_prompt(kind, instruction, target_text)
            )
            valid, polished_text = validate_output_bounds(raw_generated)
            if not valid:
                raise ValueError(polished_text)

            if self.check_cancelled():
                raise JobCancelledError("AI job was cancelled by user.")

            ops_count = self.apply_text_replacement(
                start_anchor=job.anchor_start,
                end_anchor=job.anchor_end,
                replacement_text=polished_text,
            )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            AI_JOB_SECONDS.observe(latency_ms / 1000)
            AI_JOBS.labels(kind=job.kind, status="done").inc()
            job.status = "done"
            job.latency_ms = latency_ms
            job.output_tokens = len(polished_text.split())
            job.save(update_fields=["status", "latency_ms", "output_tokens", "updated_at"])

            self.broadcast_presence("Done")
            self.broadcast_status("done", f"Applied {ops_count} operations.")
            return {
                "status": "done",
                "ops_count": ops_count,
                "latency_ms": latency_ms,
                "text": polished_text,
            }

        except JobCancelledError as exc:
            logger.info("AIJob %s cancelled.", self.job_id)
            self._finish(job, "cancelled", str(exc))
            return {"status": "cancelled", "message": str(exc)}

        except AnchorDeletedError as exc:
            logger.warning("AIJob %s aborted because anchor was deleted: %s", self.job_id, exc)
            self._finish(job, "failed", str(exc), f"Anchor text was deleted by collaborator: {exc}")
            return {"status": "failed", "error": str(exc)}

        except Exception as exc:
            logger.exception("AIJob %s failed", self.job_id)
            self._finish(job, "failed", str(exc))
            return {"status": "failed", "error": str(exc)}

    def _finish(self, job: AIJob, status: str, error: str, message: str | None = None) -> None:
        AI_JOBS.labels(kind=job.kind, status=status).inc()
        job.status = status
        job.error_message = error
        job.save(update_fields=["status", "error_message", "updated_at"])
        self.broadcast_status(status, message if message is not None else error)

    def apply_text_replacement(
        self,
        start_anchor: str | None,
        end_anchor: str | None,
        replacement_text: str,
    ) -> int:
        """
        Convert a text transformation into CRDT insert & delete ops, commit them in
        one batch, and broadcast them to the room. Returns the number of ops applied.

        The ops are generated against the latest server state; any human edits that
        land between generation and commit are concurrent ops and merge normally.
        """
        state = get_document_state(self.doc_id)
        working = RGA.from_dict(state.state, site_id=self.site_id)
        start_pos, end_pos, _ = self.resolve_anchor_range(working, start_anchor, end_anchor)
        ops = diff_to_ops(working, start_pos, end_pos, replacement_text)

        # The ops are based on `state.seq`; if tombstone GC overtook that state in
        # the meantime the server rejects them as stale rather than guessing.
        results = apply_operations(
            self.doc_id, ops, user=self.user, base_seqs=[state.seq] * len(ops)
        )
        applied = committed(ops, results)
        broadcast_ops(self.doc_id, applied, sender_channel=f"ai_{self.site_id}")
        rejected = [r.error for r in results if r.error is not None]
        if rejected:
            raise RuntimeError(f"AI edit rejected by the server: {rejected[0]}")
        return len(applied)
