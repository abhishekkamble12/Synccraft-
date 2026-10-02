"""
Celery background tasks for AI co-authoring, suggestions, and missed-edit summaries.
"""

import logging
import uuid
from typing import Any

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer

from ai.agent_peer import AIPeer
from ai.client import get_configured_llm_client
from ai.prompts import PROMPTS, SYSTEM_PROMPT_COAUTHOR, SYSTEM_PROMPT_SUMMARY
from crdt.rga import RGA
from documents.models import AIJob, Operation, Suggestion
from documents.services import get_document_state, reconstruct_state_at_seq

logger = logging.getLogger(__name__)


def _send_to_room(doc_id: uuid.UUID, data: dict[str, Any], channel: str | None = None) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    message = {"type": "doc.presence", "data": data, "sender_channel": "ai_worker"}
    if channel:
        async_to_sync(channel_layer.send)(channel, message)
    else:
        async_to_sync(channel_layer.group_send)(f"doc_{doc_id}", message)


@shared_task
def rewrite_task(job_id_str: str) -> dict[str, Any]:
    """Execute an AI co-author edit (rewrite / grammar / shorten / continue)."""
    job = AIJob.objects.select_related("user").filter(id=job_id_str).first()
    if job is None:
        logger.error("AIJob %s does not exist.", job_id_str)
        return {"status": "error", "error": "Job not found"}

    peer = AIPeer(
        doc_id=job.document_id,
        job_id=job.id,
        user=job.user,
        llm_client=get_configured_llm_client(),
    )
    return peer.run(kind=job.kind, instruction=job.instruction)


@shared_task
def summarize_missed_edits_task(
    job_id_str: str,
    from_seq: int,
    to_seq: int,
    recipient_channel: str | None = None,
) -> dict[str, Any]:
    """
    "What changed while I was away": summarise edits between `from_seq` and `to_seq`
    and send the summary to the reconnecting client.
    """
    job = AIJob.objects.filter(id=job_id_str).first()
    if job is None:
        return {"status": "error", "error": "Job not found"}
    doc_id = job.document_id

    _, text_before = reconstruct_state_at_seq(doc_id, from_seq)
    _, text_after = reconstruct_state_at_seq(doc_id, to_seq)

    authors = sorted(
        {
            username or site_id
            for username, site_id in Operation.objects.filter(
                document_id=doc_id, server_seq__gt=from_seq, server_seq__lte=to_seq
            )
            .values_list("user__username", "site_id")
            .distinct()
        }
    )

    prompt = PROMPTS["summary_v1"].format(
        before_text=text_before[:2000] or "(empty document)",
        after_text=text_after[:2000] or "(empty document)",
    )
    try:
        summary = async_to_sync(get_configured_llm_client().generate_text)(
            prompt=prompt, system_prompt=SYSTEM_PROMPT_SUMMARY
        ).strip()
    except Exception as exc:
        logger.exception("Summary job %s failed", job_id_str)
        job.status = "failed"
        job.error_message = str(exc)
        job.save(update_fields=["status", "error_message", "updated_at"])
        return {"status": "failed", "error": str(exc)}

    job.status = "done"
    job.save(update_fields=["status", "updated_at"])

    _send_to_room(
        doc_id,
        {
            "type": "missed_summary",
            "from_seq": from_seq,
            "to_seq": to_seq,
            "authors": authors,
            "summary": summary,
        },
        channel=recipient_channel,
    )
    return {"status": "done", "summary": summary}


@shared_task
def suggestion_task(job_id_str: str) -> dict[str, Any]:
    """
    Suggestion mode: propose an edit as a Suggestion row without mutating the CRDT.
    """
    job = AIJob.objects.select_related("user").filter(id=job_id_str).first()
    if job is None:
        return {"status": "error", "error": "Job not found"}

    peer = AIPeer(doc_id=job.document_id, job_id=job.id, user=job.user)
    rga = RGA.from_dict(get_document_state(job.document_id).state, site_id=peer.site_id)
    try:
        _, _, target_text = peer.resolve_anchor_range(rga, job.anchor_start, job.anchor_end)
        prompt = PROMPTS["rewrite_v1"].format(
            instruction=job.instruction or "Propose improvements", text=target_text
        )
        proposed_text = async_to_sync(get_configured_llm_client().generate_text)(
            prompt=prompt, system_prompt=SYSTEM_PROMPT_COAUTHOR
        ).strip()
    except Exception as exc:
        logger.exception("Suggestion job %s failed", job_id_str)
        job.status = "failed"
        job.error_message = str(exc)
        job.save(update_fields=["status", "error_message", "updated_at"])
        peer.broadcast_status("failed", str(exc))
        return {"status": "failed", "error": str(exc)}

    suggestion = Suggestion.objects.create(
        document_id=job.document_id,
        ai_job=job,
        anchor_start=job.anchor_start or "",
        anchor_end=job.anchor_end or "",
        proposed_text=proposed_text,
        status="open",
    )

    job.status = "done"
    job.save(update_fields=["status", "updated_at"])
    peer.broadcast_status("done", "Suggestion ready for review.")

    _send_to_room(
        job.document_id,
        {
            "type": "new_suggestion",
            "suggestion_id": str(suggestion.id),
            "anchor_start": suggestion.anchor_start,
            "anchor_end": suggestion.anchor_end,
            "proposed_text": suggestion.proposed_text,
        },
    )
    return {"status": "done", "suggestion_id": str(suggestion.id)}
