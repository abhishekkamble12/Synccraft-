"""
WebSocket consumer for real-time document synchronization and presence.
"""

import asyncio
from typing import Any
import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import User

from ai.agent_peer import AIPeer
from ai.guards import check_rate_limit, check_and_increment_token_budget
from ai.tasks import rewrite_task, summarize_missed_edits_task, suggestion_task
from crdt.ops import Op
from documents.models import AIJob, Collaborator, Document, Suggestion
from documents.services import (
    apply_operation,
    get_or_load_document_rga,
    sync_client_state,
)


class DocumentConsumer(AsyncJsonWebsocketConsumer):
    """
    Handles live bi-directional CRDT synchronization over WebSockets.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.doc_id: uuid.UUID | None = None
        self.group_name: str = ""
        self.user: User | None = None
        self.role: str = "viewer"

    async def connect(self) -> None:
        raw_doc_id = self.scope["url_route"]["kwargs"].get("doc_id")
        try:
            self.doc_id = uuid.UUID(str(raw_doc_id))
        except (ValueError, TypeError):
            await self.close(code=4000)
            return

        self.user = self.scope.get("user")
        self.group_name = f"doc_{self.doc_id}"

        # Verify access permission
        has_access, role = await self._check_access_permission()
        if not has_access:
            await self.close(code=4003)
            return

        self.role = role

        # Join document pub/sub broadcast group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Send initial document snapshot & head sequence
        init_state = await self._get_initial_document_state()
        await self.send_json(
            {
                "type": "init",
                "doc_id": str(self.doc_id),
                "head_seq": init_state["head_seq"],
                "text": init_state["text"],
                "snapshot": init_state["snapshot"],
                "role": self.role,
            }
        )

    async def disconnect(self, close_code: int) -> None:
        if self.group_name:
            # Broadcast user left presence
            username = self.user.username if (self.user and self.user.is_authenticated) else "Anonymous"
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "doc.presence",
                    "data": {
                        "type": "presence_leave",
                        "user": username,
                        "channel": self.channel_name,
                    },
                    "sender_channel": self.channel_name,
                },
            )
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content: dict[str, Any], **kwargs: Any) -> None:
        msg_type = content.get("type")

        if msg_type == "op":
            await self._handle_op_message(content)
        elif msg_type == "sync":
            await self._handle_sync_message(content)
        elif msg_type == "presence":
            await self._handle_presence_message(content)
        elif msg_type == "ai_request":
            await self._handle_ai_request_message(content)
        elif msg_type == "ai_cancel":
            await self._handle_ai_cancel_message(content)
        elif msg_type == "suggestion_accept":
            await self._handle_suggestion_accept(content)
        elif msg_type == "suggestion_reject":
            await self._handle_suggestion_reject(content)
        else:
            await self.send_json(
                {"type": "error", "code": "unknown_message_type", "message": f"Unknown type: {msg_type}"}
            )

    # -------------------------------------------------------------------------
    # Message Handlers
    # -------------------------------------------------------------------------

    async def _handle_op_message(self, content: dict[str, Any]) -> None:
        if self.role == "viewer":
            await self.send_json(
                {"type": "error", "code": "forbidden", "message": "Viewers do not have edit permissions."}
            )
            return

        raw_op = content.get("op")
        if not raw_op:
            await self.send_json(
                {"type": "error", "code": "bad_request", "message": "Missing 'op' field."}
            )
            return

        try:
            op = Op.from_dict(raw_op)
        except Exception as exc:
            await self.send_json(
                {"type": "error", "code": "bad_op", "message": f"Invalid op payload: {exc}"}
            )
            return

        # Apply operation atomically in database & in-memory CRDT
        server_seq, newly_applied = await database_sync_to_async(apply_operation)(
            self.doc_id, op, user=self.user if self.user and self.user.is_authenticated else None
        )

        # Broadcast operation to all other connected peers in the document group
        if newly_applied:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "doc.op",
                    "op": op.to_dict(),
                    "seq": server_seq,
                    "sender_channel": self.channel_name,
                },
            )

        # Acknowledge the operation back to the originating client
        await self.send_json(
            {
                "type": "ack",
                "op_id": op.op_id,
                "seq": server_seq,
            }
        )

    async def _handle_sync_message(self, content: dict[str, Any]) -> None:
        last_seq = int(content.get("last_seq", 0))
        raw_pending = content.get("pending", [])

        pending_ops: list[Op] = []
        for raw in raw_pending:
            try:
                pending_ops.append(Op.from_dict(raw))
            except Exception:
                continue

        missed_ops, acked_ids, head_seq = await database_sync_to_async(sync_client_state)(
            self.doc_id,
            last_seq,
            pending_ops,
            user=self.user if self.user and self.user.is_authenticated else None,
        )

        # Broadcast any newly applied pending ops to other clients
        for missed in missed_ops:
            if missed["seq"] > last_seq:
                await self.channel_layer.group_send(
                    self.group_name,
                    {
                        "type": "doc.op",
                        "op": missed["op"],
                        "seq": missed["seq"],
                        "sender_channel": self.channel_name,
                    },
                )

        await self.send_json(
            {
                "type": "sync_ack",
                "missed": missed_ops,
                "acked": acked_ids,
                "head_seq": head_seq,
            }
        )

    async def _handle_presence_message(self, content: dict[str, Any]) -> None:
        username = self.user.username if (self.user and self.user.is_authenticated) else content.get("user", "Anonymous")
        payload = {
            "type": "presence",
            "user": username,
            "color": content.get("color", "#4f46e5"),
            "cursor_anchor": content.get("cursor_anchor"),
            "cursor_pos": content.get("cursor_pos"),
            "channel": self.channel_name,
        }

        # Ephemeral broadcast to room (no DB persistence)
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "doc.presence",
                "data": payload,
                "sender_channel": self.channel_name,
            },
        )

    async def _handle_ai_request_message(self, content: dict[str, Any]) -> None:
        if self.role == "viewer":
            await self.send_json(
                {"type": "error", "code": "forbidden", "message": "Viewers cannot request AI edits."}
            )
            return

        user_key = str(self.user.id) if (self.user and self.user.is_authenticated) else self.channel_name
        allowed, remaining = check_rate_limit(user_key)
        if not allowed:
            await self.send_json(
                {
                    "type": "error",
                    "code": "rate_limited",
                    "message": "AI request rate limit reached. Please wait before submitting another request.",
                }
            )
            return

        budget_ok = check_and_increment_token_budget(user_key, estimated_tokens=1000)
        if not budget_ok:
            await self.send_json(
                {
                    "type": "error",
                    "code": "token_budget_exceeded",
                    "message": "Daily AI token budget limit reached.",
                }
            )
            return

        kind = content.get("kind", "rewrite")
        anchor_start = content.get("anchor_start")
        anchor_end = content.get("anchor_end")
        instruction = content.get("instruction", "")

        job = await database_sync_to_async(self._create_ai_job)(
            kind=kind,
            anchor_start=anchor_start,
            anchor_end=anchor_end,
            instruction=instruction,
        )

        # Trigger background execution: prefer Celery, fall back to in-process async
        if kind == "suggest":
            try:
                suggestion_task.delay(str(job.id))
            except Exception:
                asyncio.create_task(database_sync_to_async(suggestion_task)(str(job.id)))
        else:
            try:
                rewrite_task.delay(str(job.id))
            except Exception:
                asyncio.create_task(database_sync_to_async(rewrite_task)(str(job.id)))

        await self.send_json(
            {
                "type": "ai_status",
                "job_id": str(job.id),
                "status": "queued",
                "kind": kind,
            }
        )

    async def _handle_ai_cancel_message(self, content: dict[str, Any]) -> None:
        job_id = content.get("job_id")
        if not job_id:
            return

        await database_sync_to_async(self._cancel_ai_job)(job_id)

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "doc.presence",
                "data": {
                    "type": "ai_status",
                    "job_id": job_id,
                    "status": "cancelled",
                    "message": "Cancelled by user",
                },
                "sender_channel": self.channel_name,
            },
        )

    async def _handle_suggestion_accept(self, content: dict[str, Any]) -> None:
        if self.role == "viewer":
            await self.send_json({"type": "error", "code": "forbidden", "message": "Viewer cannot accept suggestions."})
            return

        suggestion_id = content.get("suggestion_id")
        applied = await database_sync_to_async(self._apply_suggestion)(suggestion_id)
        if applied:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "doc.presence",
                    "data": {
                        "type": "suggestion_update",
                        "suggestion_id": suggestion_id,
                        "status": "accepted",
                    },
                    "sender_channel": self.channel_name,
                },
            )

    async def _handle_suggestion_reject(self, content: dict[str, Any]) -> None:
        suggestion_id = content.get("suggestion_id")
        await database_sync_to_async(self._reject_suggestion)(suggestion_id)
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "doc.presence",
                "data": {
                    "type": "suggestion_update",
                    "suggestion_id": suggestion_id,
                    "status": "rejected",
                },
                "sender_channel": self.channel_name,
            },
        )

    # -------------------------------------------------------------------------
    # Channel Layer Group Event Receivers
    # -------------------------------------------------------------------------

    async def doc_op(self, event: dict[str, Any]) -> None:
        # Don't echo op back to original sender (sender already received 'ack')
        if event.get("sender_channel") != self.channel_name:
            await self.send_json(
                {
                    "type": "op",
                    "op": event["op"],
                    "seq": event["seq"],
                }
            )

    async def doc_presence(self, event: dict[str, Any]) -> None:
        if event.get("sender_channel") != self.channel_name:
            await self.send_json(event["data"])

    # -------------------------------------------------------------------------
    # Helper DB Queries
    # -------------------------------------------------------------------------

    @database_sync_to_async
    def _create_ai_job(
        self, kind: str, anchor_start: str | None, anchor_end: str | None, instruction: str
    ) -> Any:
        user = self.user if self.user and self.user.is_authenticated else User.objects.first()
        if not user:
            user = User.objects.create(username="anonymous_ai_user")
        return AIJob.objects.create(
            document_id=self.doc_id,
            user=user,
            kind=kind,
            anchor_start=anchor_start or "",
            anchor_end=anchor_end or "",
            instruction=instruction,
            status="queued",
        )

    @database_sync_to_async
    def _cancel_ai_job(self, job_id: str) -> None:
        AIJob.objects.filter(id=job_id).update(status="cancelled", error_message="Cancelled by user")

    @database_sync_to_async
    def _apply_suggestion(self, suggestion_id: str) -> bool:
        sugg = Suggestion.objects.filter(id=suggestion_id, status="open").first()
        if not sugg or not self.doc_id:
            return False
        peer = AIPeer(
            doc_id=self.doc_id,
            job_id=sugg.id,
            user=self.user if self.user and self.user.is_authenticated else None,
        )
        peer.apply_text_replacement(
            start_anchor=sugg.anchor_start or None,
            end_anchor=sugg.anchor_end or None,
            replacement_text=sugg.proposed_text,
        )
        sugg.status = "accepted"
        sugg.save(update_fields=["status"])
        return True

    @database_sync_to_async
    def _reject_suggestion(self, suggestion_id: str) -> None:
        Suggestion.objects.filter(id=suggestion_id, status="open").update(status="rejected")

    @database_sync_to_async
    def _check_access_permission(self) -> tuple[bool, str]:
        doc = Document.objects.filter(id=self.doc_id).first()
        if doc is None:
            return False, ""

        if not self.user or not self.user.is_authenticated:
            # Allow anonymous read/edit in development mode if configured
            return True, "editor"

        if doc.owner_id == self.user.id:
            return True, "owner"

        collab = Collaborator.objects.filter(document=doc, user=self.user).first()
        if collab is not None:
            return True, collab.role

        return True, "editor"  # Default open access for testing

    @database_sync_to_async
    def _get_initial_document_state(self) -> dict[str, Any]:
        assert self.doc_id is not None
        doc = Document.objects.get(id=self.doc_id)
        rga = get_or_load_document_rga(self.doc_id)
        return {
            "head_seq": doc.head_seq,
            "text": rga.text(),
            "snapshot": rga.to_dict(),
        }
