"""
AI Agent as a first-class CRDT peer in collaborative sessions.

The AI peer interacts with the document strictly through the same CRDT operations
(inserts, deletes, Lamport clock updates) as human users, ensuring mathematical convergence.
"""

import asyncio
from difflib import SequenceMatcher
import logging
import time
from typing import Any, AsyncIterator
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User

from ai.client import BaseLLMClient, FakeLLMClient, OpenAICompatibleLLMClient
from ai.guards import (
    sanitize_document_text,
    validate_input_bounds,
    validate_output_bounds,
)
from ai.prompts import (
    PROMPTS,
    SYSTEM_PROMPT_COAUTHOR,
    SYSTEM_PROMPT_SUMMARY,
)
from crdt.ids import CharId, ROOT
from crdt.ops import Op
from crdt.rga import RGA
from documents.models import AIJob, Document
from documents.services import apply_operation, get_or_load_document_rga

logger = logging.getLogger(__name__)


class AnchorDeletedError(Exception):
    """Raised when an anchor character has been deleted by a human collaborator."""
    pass


class JobCancelledError(Exception):
    """Raised when the AI job is cancelled mid-execution."""
    pass


class AIPeer:
    """
    Autonomous AI co-author acting as a collaborative CRDT peer.
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
        self.site_id = site_id or f"ai-{self.job_id[:8]}"
        self.llm_client = llm_client or OpenAICompatibleLLMClient()
        self.channel_layer = get_channel_layer()
        self.group_name = f"doc_{self.doc_id}"

    def check_cancelled(self) -> bool:
        """Check if job was cancelled in the database."""
        job = AIJob.objects.filter(id=self.job_id).first()
        return bool(job is not None and job.status == "cancelled")

    def broadcast_presence(self, status_text: str, cursor_pos: int | None = None) -> None:
        """Broadcast ephemeral AI presence cursor to other collaborators."""
        if not self.channel_layer:
            return
        payload = {
            "type": "doc.presence",
            "data": {
                "type": "presence",
                "user": f"AI Co-Author ({status_text}) 🤖",
                "color": "#8b5cf6",
                "cursor_pos": cursor_pos,
                "is_ai": True,
            },
            "sender_channel": f"ai_{self.site_id}",
        }
        try:
            async_to_sync(self.channel_layer.group_send)(self.group_name, payload)
        except Exception as exc:
            logger.debug("Failed to broadcast AI presence: %s", exc)

    def broadcast_status(self, status: str, message: str = "") -> None:
        """Broadcast AI job lifecycle events to WebSocket clients."""
        if not self.channel_layer:
            return
        payload = {
            "type": "doc.presence",
            "data": {
                "type": "ai_status",
                "job_id": self.job_id,
                "status": status,
                "message": message,
            },
            "sender_channel": f"ai_{self.site_id}",
        }
        try:
            async_to_sync(self.channel_layer.group_send)(self.group_name, payload)
        except Exception as exc:
            logger.debug("Failed to broadcast AI status: %s", exc)

    def resolve_anchor_range(
        self, rga: RGA, anchor_start_str: str | None, anchor_end_str: str | None
    ) -> tuple[int, int, str]:
        """
        Locate the start and end visible positions for given anchor CharIds.
        Raises AnchorDeletedError if an explicit anchor was deleted.
        """
        if not anchor_start_str and not anchor_end_str:
            # Full document scope
            return 0, rga.visible_len(), rga.text()

        start_pos = 0
        if anchor_start_str:
            try:
                start_cid = CharId.from_str(anchor_start_str)
                pos = rga.pos_of_char_id(start_cid)
                if pos is None:
                    raise AnchorDeletedError(f"Start anchor {anchor_start_str} was deleted.")
                start_pos = pos
            except ValueError:
                start_pos = 0

        end_pos = rga.visible_len()
        if anchor_end_str:
            try:
                end_cid = CharId.from_str(anchor_end_str)
                pos = rga.pos_of_char_id(end_cid)
                if pos is None:
                    raise AnchorDeletedError(f"End anchor {anchor_end_str} was deleted.")
                end_pos = pos + 1
            except ValueError:
                end_pos = rga.visible_len()

        if start_pos > end_pos:
            start_pos, end_pos = end_pos, start_pos

        full_text = rga.text()
        target_text = full_text[start_pos:end_pos]
        return start_pos, end_pos, target_text

    async def execute_task(self, kind: str, instruction: str = "") -> dict[str, Any]:
        """
        Main entry point for executing an AI job.
        Streams/generates new text and applies CRDT changes atomically.
        """
        start_time = time.time()
        job = AIJob.objects.filter(id=self.job_id).first()
        if not job:
            raise ValueError(f"AIJob {self.job_id} not found.")

        job.status = "running"
        job.save(update_fields=["status", "updated_at"])
        self.broadcast_status("running")
        self.broadcast_presence("Thinking...")

        try:
            rga = get_or_load_document_rga(self.doc_id)
            start_pos, end_pos, target_text = self.resolve_anchor_range(
                rga, job.anchor_start, job.anchor_end
            )

            # Guardrails check
            valid, err = validate_input_bounds(target_text, instruction)
            if not valid:
                raise ValueError(err)

            # Build prompt
            prompt_key = f"{kind}_v1"
            template = PROMPTS.get(prompt_key, PROMPTS["rewrite_v1"])
            sanitized_target = sanitize_document_text(target_text)

            if kind == "rewrite":
                prompt = template.format(instruction=instruction or "Make it better", text=sanitized_target)
            elif kind in ("grammar", "shorten", "continue"):
                prompt = template.format(text=sanitized_target)
            else:
                prompt = template.format(instruction=instruction, text=sanitized_target)

            # Stream LLM tokens
            self.broadcast_presence("Writing...")
            accumulated_chunks: list[str] = []

            async for chunk in self.llm_client.stream_completion(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT_COAUTHOR,
            ):
                if self.check_cancelled():
                    raise JobCancelledError("AI job was cancelled by user.")

                accumulated_chunks.append(chunk)

            raw_generated = "".join(accumulated_chunks)
            valid, polished_text = validate_output_bounds(raw_generated)
            if not valid:
                raise ValueError(polished_text)

            # Apply changes as CRDT peer operations
            ops_count = self.apply_text_replacement(
                start_anchor=job.anchor_start,
                end_anchor=job.anchor_end,
                replacement_text=polished_text,
            )

            latency_ms = int((time.time() - start_time) * 1000)
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
            job.status = "cancelled"
            job.error_message = str(exc)
            job.save(update_fields=["status", "error_message", "updated_at"])
            self.broadcast_status("cancelled", str(exc))
            return {"status": "cancelled", "message": str(exc)}

        except AnchorDeletedError as exc:
            logger.warning("AIJob %s aborted because anchor was deleted: %s", self.job_id, exc)
            job.status = "failed"
            job.error_message = str(exc)
            job.save(update_fields=["status", "error_message", "updated_at"])
            self.broadcast_status("failed", f"Anchor text was deleted by collaborator: {exc}")
            return {"status": "failed", "error": str(exc)}

        except Exception as exc:
            logger.exception("AIJob %s failed: %s", self.job_id, exc)
            job.status = "failed"
            job.error_message = str(exc)
            job.save(update_fields=["status", "error_message", "updated_at"])
            self.broadcast_status("failed", str(exc))
            return {"status": "failed", "error": str(exc)}

    def apply_text_replacement(
        self,
        start_anchor: str | None,
        end_anchor: str | None,
        replacement_text: str,
    ) -> int:
        """
        Convert text transformation into an exact sequence of CRDT insert & delete ops
        and publish them to both the database and live broadcast channels.
        """
        rga = get_or_load_document_rga(self.doc_id)
        start_pos, end_pos, current_slice = self.resolve_anchor_range(
            rga, start_anchor, end_anchor
        )

        matcher = SequenceMatcher(None, current_slice, replacement_text)
        opcodes = matcher.get_opcodes()

        # Local replica with AI site_id to synthesize operations cleanly
        ai_rga = RGA.from_dict(rga.to_dict(), site_id=self.site_id)
        generated_ops: list[Op] = []

        # Iterate opcodes: deletions first from end to start to preserve relative indices
        for tag, i1, i2, j1, j2 in opcodes:
            if tag in ("replace", "delete"):
                abs_i1 = start_pos + i1
                abs_i2 = start_pos + i2
                for pos in range(abs_i2 - 1, abs_i1 - 1, -1):
                    if pos < ai_rga.visible_len():
                        op = ai_rga.local_delete(pos)
                        generated_ops.append(op)

            if tag in ("replace", "insert"):
                insert_text = replacement_text[j1:j2]
                abs_i1 = start_pos + i1
                for offset, char in enumerate(insert_text):
                    pos = min(abs_i1 + offset, ai_rga.visible_len())
                    op = ai_rga.local_insert(pos, char)
                    generated_ops.append(op)

        # Commit operations to database and broadcast to room
        applied_count = 0
        for op in generated_ops:
            if self.check_cancelled():
                raise JobCancelledError("Cancelled during operation broadcast.")

            server_seq, newly_applied = apply_operation(self.doc_id, op, user=self.user)
            if newly_applied:
                applied_count += 1
                if self.channel_layer:
                    try:
                        async_to_sync(self.channel_layer.group_send)(
                            self.group_name,
                            {
                                "type": "doc.op",
                                "op": op.to_dict(),
                                "seq": server_seq,
                                "sender_channel": f"ai_{self.site_id}",
                            },
                        )
                    except Exception as exc:
                        logger.debug("Failed group_send: %s", exc)

        return applied_count
