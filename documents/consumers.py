"""
WebSocket consumer for real-time document synchronization and presence.
"""

import asyncio
import logging
import uuid
from typing import Any

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.contrib.auth.models import User

from ai.agent_peer import AIPeer
from ai.guards import check_and_increment_token_budget, check_rate_limit
from ai.tasks import rewrite_task, suggestion_task, summarize_missed_edits_task
from crdt.ops import Op
from documents.metrics import ACTIVE_CONNECTIONS, SYNCS
from documents.models import AIJob, Document, Suggestion
from documents.permissions import can_edit, get_role
from documents.services import get_document_state, sync_client_state
from documents.write_queue import submit_op

logger = logging.getLogger(__name__)

MAX_PENDING_OPS_PER_SYNC = 5_000
AI_KINDS = frozenset(k for k, _ in AIJob.KIND_CHOICES) - {"summary"}

# Strong references to in-process AI tasks so they are not garbage collected mid-run.
_background_tasks: set[asyncio.Task[Any]] = set()


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
        self._tasks: set[asyncio.Task[Any]] = set()
        self._summary_requested = False

    async def connect(self) -> None:
        raw_doc_id = self.scope["url_route"]["kwargs"].get("doc_id")
        try:
            self.doc_id = uuid.UUID(str(raw_doc_id))
        except (ValueError, TypeError):
            await self.close(code=4000)
            return

        self.user = self.scope.get("user")
        role = await self._get_role()
        if role is None:
            await self.close(code=4003)
            return
        self.role = role

        self.group_name = f"doc_{self.doc_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        ACTIVE_CONNECTIONS.inc()

        state = await database_sync_to_async(get_document_state)(self.doc_id)
        await self.send_json(
            {
                "type": "init",
                "doc_id": str(self.doc_id),
                "head_seq": state.seq,
                "text": state.text,
                "snapshot": state.state,
                "role": self.role,
            }
        )

    async def disconnect(self, close_code: int) -> None:
        if self.group_name:
            ACTIVE_CONNECTIONS.dec()
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "doc.presence",
                    "data": {
                        "type": "presence_leave",
                        "user": self._username,
                        "channel": self.channel_name,
                    },
                    "sender_channel": self.channel_name,
                },
            )
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        if not isinstance(content, dict):
            await self._send_error("bad_request", "Messages must be JSON objects.")
            return

        handlers = {
            "op": self._handle_op_message,
            "sync": self._handle_sync_message,
            "presence": self._handle_presence_message,
            "ai_request": self._handle_ai_request_message,
            "ai_cancel": self._handle_ai_cancel_message,
            "suggestion_accept": self._handle_suggestion_accept,
            "suggestion_reject": self._handle_suggestion_reject,
        }
        msg_type = content.get("type")
        handler = handlers.get(msg_type) if isinstance(msg_type, str) else None
        if handler is None:
            await self._send_error("unknown_message_type", f"Unknown type: {msg_type}")
            return

        try:
            await handler(content)
        except Exception:
            logger.exception("Error handling %s message on doc %s", msg_type, self.doc_id)
            await self._send_error("internal_error", "The server failed to process the message.")

    # -------------------------------------------------------------------------
    # Message Handlers
    # -------------------------------------------------------------------------

    async def _handle_op_message(self, content: dict[str, Any]) -> None:
        if not can_edit(self.role):
            await self._send_error("forbidden", "Viewers do not have edit permissions.")
            return

        try:
            op = Op.from_dict(content["op"])
        except Exception as exc:
            await self._send_error("bad_op", f"Invalid op payload: {exc}")
            return

        # Enqueue synchronously (preserving this client's op order), then ack from a
        # task so this consumer keeps delivering peers' broadcasts while it waits.
        future = submit_op(self._doc, op, self.user, self.channel_name)
        self._track(asyncio.create_task(self._ack_when_committed(op.op_id, future)))

    async def _ack_when_committed(
        self, op_id: str, future: "asyncio.Future[tuple[int, bool]]"
    ) -> None:
        try:
            server_seq, _ = await future
        except Exception:
            await self._safe_send({"type": "error", "code": "op_rejected", "op_id": op_id})
            return
        await self._safe_send({"type": "ack", "op_id": op_id, "seq": server_seq})

    async def _handle_sync_message(self, content: dict[str, Any]) -> None:
        try:
            last_seq = max(0, int(content.get("last_seq", 0)))
        except (TypeError, ValueError):
            await self._send_error("bad_request", "'last_seq' must be an integer.")
            return

        reason = content.get("reason")
        SYNCS.labels(reason=reason if reason in ("reconnect", "gap", "flush") else "other").inc()

        raw_pending = content.get("pending") or []
        if not isinstance(raw_pending, list) or len(raw_pending) > MAX_PENDING_OPS_PER_SYNC:
            await self._send_error("bad_request", "'pending' must be a list of ops.")
            return

        pending_ops: list[Op] = []
        if can_edit(self.role):
            for raw in raw_pending:
                try:
                    pending_ops.append(Op.from_dict(raw))
                except Exception:
                    logger.info("Dropping malformed pending op on doc %s", self.doc_id)

        result = await database_sync_to_async(sync_client_state)(
            self.doc_id, last_seq, pending_ops, user=self.user
        )

        # Only ops this sync actually persisted are new to the other clients.
        if result.newly_applied:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "doc.ops",
                    "items": [
                        {"seq": seq, "op": op.to_dict(), "sender": self.channel_name}
                        for seq, op in result.newly_applied
                    ],
                },
            )

        await self.send_json(
            {
                "type": "sync_ack",
                "missed": result.missed,
                "acked": result.acked,
                "head_seq": result.head_seq,
            }
        )

        # Only a reconnect handshake means "I was away". Gap-repair syncs
        # (reason="gap") happen under load and must never spawn AI jobs.
        others_missed = len(result.missed) - len(result.newly_applied)
        if (
            reason == "reconnect"
            and not self._summary_requested
            and last_seq > 0
            and others_missed >= settings.MISSED_SUMMARY_THRESHOLD
        ):
            self._summary_requested = True
            await self._request_missed_summary(last_seq, result.head_seq)

    async def _handle_presence_message(self, content: dict[str, Any]) -> None:
        payload = {
            "type": "presence",
            "user": self._username,
            "color": str(content.get("color", "#4f46e5"))[:32],
            "cursor_anchor": content.get("cursor_anchor"),
            "cursor_pos": content.get("cursor_pos"),
            "channel": self.channel_name,
        }
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "doc.presence", "data": payload, "sender_channel": self.channel_name},
        )

    async def _handle_ai_request_message(self, content: dict[str, Any]) -> None:
        if not can_edit(self.role):
            await self._send_error("forbidden", "Viewers cannot request AI edits.")
            return

        kind = content.get("kind", "rewrite")
        if kind not in AI_KINDS:
            await self._send_error("bad_request", f"Unsupported AI request kind: {kind!r}")
            return

        if not await self._consume_ai_quota():
            return

        job = await self._create_ai_job(
            kind=kind,
            anchor_start=content.get("anchor_start"),
            anchor_end=content.get("anchor_end"),
            instruction=str(content.get("instruction", ""))[:2000],
        )
        task = suggestion_task if kind == "suggest" else rewrite_task
        await self._dispatch(task, str(job.id))

        await self.send_json(
            {"type": "ai_status", "job_id": str(job.id), "status": "queued", "kind": kind}
        )

    async def _handle_ai_cancel_message(self, content: dict[str, Any]) -> None:
        job_id = content.get("job_id")
        if not can_edit(self.role) or not job_id:
            return

        cancelled = await self._cancel_ai_job(str(job_id))
        if not cancelled:
            return

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "doc.presence",
                "data": {
                    "type": "ai_status",
                    "job_id": str(job_id),
                    "status": "cancelled",
                    "message": "Cancelled by user",
                },
                "sender_channel": self.channel_name,
            },
        )

    async def _handle_suggestion_accept(self, content: dict[str, Any]) -> None:
        if not can_edit(self.role):
            await self._send_error("forbidden", "Viewers cannot accept suggestions.")
            return

        suggestion_id = str(content.get("suggestion_id", ""))
        if await self._apply_suggestion(suggestion_id):
            await self._broadcast_suggestion_update(suggestion_id, "accepted")

    async def _handle_suggestion_reject(self, content: dict[str, Any]) -> None:
        if not can_edit(self.role):
            await self._send_error("forbidden", "Viewers cannot reject suggestions.")
            return

        suggestion_id = str(content.get("suggestion_id", ""))
        if await self._reject_suggestion(suggestion_id):
            await self._broadcast_suggestion_update(suggestion_id, "rejected")

    # -------------------------------------------------------------------------
    # Channel Layer Group Event Receivers
    # -------------------------------------------------------------------------

    async def doc_ops(self, event: dict[str, Any]) -> None:
        # Skip this socket's own ops: it already gets an 'ack' for each of them.
        ops = [
            {"seq": item["seq"], "op": item["op"]}
            for item in event["items"]
            if item["sender"] != self.channel_name
        ]
        if ops:
            await self.send_json({"type": "ops", "ops": ops})

    async def doc_presence(self, event: dict[str, Any]) -> None:
        if event.get("sender_channel") != self.channel_name:
            await self.send_json(event["data"])

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @property
    def _doc(self) -> uuid.UUID:
        assert self.doc_id is not None, "only valid after connect()"
        return self.doc_id

    @property
    def _author(self) -> User:
        assert isinstance(self.user, User), "only authenticated users get past connect()"
        return self.user

    @property
    def _username(self) -> str:
        return self.user.username if self.user and self.user.is_authenticated else "Anonymous"

    def _track(self, task: "asyncio.Task[Any]") -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_send(self, content: dict[str, Any]) -> None:
        """Send unless the socket already closed (late acks after a disconnect)."""
        try:
            await self.send_json(content)
        except Exception:
            logger.debug("Dropped message to closed socket on doc %s", self.doc_id)

    async def _send_error(self, code: str, message: str) -> None:
        await self.send_json({"type": "error", "code": code, "message": message})

    async def _broadcast_suggestion_update(self, suggestion_id: str, status: str) -> None:
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "doc.presence",
                "data": {
                    "type": "suggestion_update",
                    "suggestion_id": suggestion_id,
                    "status": status,
                },
                "sender_channel": self.channel_name,
            },
        )

    async def _consume_ai_quota(self) -> bool:
        assert self.user is not None
        user_key = str(self.user.pk)
        allowed, _ = await database_sync_to_async(check_rate_limit)(user_key)
        if not allowed:
            await self._send_error(
                "rate_limited",
                "AI request rate limit reached. Please wait before submitting another request.",
            )
            return False
        budget_ok = await database_sync_to_async(check_and_increment_token_budget)(
            user_key, estimated_tokens=1000
        )
        if not budget_ok:
            await self._send_error("token_budget_exceeded", "Daily AI token budget limit reached.")
            return False
        return True

    async def _dispatch(self, task: Any, *args: Any) -> None:
        """Run a Celery task on a worker, or in this process when AI_TASKS_INLINE is set."""
        if settings.AI_TASKS_INLINE:
            bg = asyncio.create_task(database_sync_to_async(task)(*args))
            _background_tasks.add(bg)
            bg.add_done_callback(_background_tasks.discard)
        else:
            await database_sync_to_async(task.delay)(*args)

    async def _request_missed_summary(self, from_seq: int, to_seq: int) -> None:
        if not await self._consume_ai_quota():
            return
        job = await self._create_ai_job(kind="summary", anchor_start=None, anchor_end=None)
        await self._dispatch(
            summarize_missed_edits_task, str(job.id), from_seq, to_seq, self.channel_name
        )

    @database_sync_to_async
    def _get_role(self) -> str | None:
        doc = Document.objects.filter(id=self._doc).first()
        if doc is None:
            return None
        return get_role(doc, self.user)

    @database_sync_to_async
    def _create_ai_job(
        self,
        kind: str,
        anchor_start: str | None,
        anchor_end: str | None,
        instruction: str = "",
    ) -> AIJob:
        return AIJob.objects.create(
            document_id=self._doc,
            user=self._author,
            kind=kind,
            anchor_start=anchor_start or "",
            anchor_end=anchor_end or "",
            instruction=instruction,
            status="queued",
        )

    @database_sync_to_async
    def _cancel_ai_job(self, job_id: str) -> bool:
        try:
            uuid.UUID(job_id)
        except ValueError:
            return False
        updated: int = AIJob.objects.filter(
            id=job_id, document_id=self._doc, status__in=["queued", "running"]
        ).update(status="cancelled", error_message="Cancelled by user")
        return updated > 0

    @database_sync_to_async
    def _apply_suggestion(self, suggestion_id: str) -> bool:
        try:
            uuid.UUID(suggestion_id)
        except ValueError:
            return False
        sugg = Suggestion.objects.filter(
            id=suggestion_id, document_id=self._doc, status="open"
        ).first()
        if sugg is None or self.doc_id is None:
            return False
        peer = AIPeer(doc_id=self.doc_id, job_id=sugg.id, user=self.user)
        peer.apply_text_replacement(
            start_anchor=sugg.anchor_start or None,
            end_anchor=sugg.anchor_end or None,
            replacement_text=sugg.proposed_text,
        )
        sugg.status = "accepted"
        sugg.save(update_fields=["status"])
        return True

    @database_sync_to_async
    def _reject_suggestion(self, suggestion_id: str) -> bool:
        try:
            uuid.UUID(suggestion_id)
        except ValueError:
            return False
        updated: int = Suggestion.objects.filter(
            id=suggestion_id, document_id=self._doc, status="open"
        ).update(status="rejected")
        return updated > 0
