"""
Celery background tasks for AI co-authoring, summarization, and vector indexing.
"""

import asyncio
import logging
import os
import uuid

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer

from ai.agent_peer import AIPeer
from ai.client import FakeLLMClient, OpenAICompatibleLLMClient
from ai.prompts import PROMPTS, SYSTEM_PROMPT_COAUTHOR, SYSTEM_PROMPT_SUMMARY
from documents.models import AIJob, DocChunk, Operation, Suggestion
from documents.services import get_or_load_document_rga, reconstruct_state_at_seq

logger = logging.getLogger(__name__)


def get_configured_llm_client():
    """Returns appropriate LLM client depending on environment configuration."""
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return FakeLLMClient()
    return OpenAICompatibleLLMClient(api_key=api_key)


@shared_task
def rewrite_task(job_id_str: str) -> dict:
    """
    Background Celery task to execute AI co-author rewriting.
    """
    job = AIJob.objects.filter(id=job_id_str).first()
    if not job:
        logger.error("AIJob %s does not exist.", job_id_str)
        return {"status": "error", "error": "Job not found"}

    client = get_configured_llm_client()
    peer = AIPeer(
        doc_id=job.document_id,
        job_id=job.id,
        user=job.user,
        llm_client=client,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            peer.execute_task(kind=job.kind, instruction=job.instruction)
        )
        return result
    finally:
        loop.close()


@shared_task
def summarize_missed_edits_task(
    job_id_str: str,
    doc_id_str: str,
    from_seq: int,
    to_seq: int,
    recipient_channel: str | None = None,
) -> dict:
    """
    Task 9.1: "What Changed While I Was Away" (F2).
    Generates a 3-bullet summary of changes occurring between from_seq and to_seq.
    """
    doc_id = uuid.UUID(doc_id_str)
    job = AIJob.objects.filter(id=job_id_str).first()

    # Reconstruct text before from_seq and current text at to_seq
    _, text_before = reconstruct_state_at_seq(doc_id, from_seq)
    _, text_after = reconstruct_state_at_seq(doc_id, to_seq)

    # Find authors who edited in this window
    ops = (
        Operation.objects.filter(
            document_id=doc_id, server_seq__gt=from_seq, server_seq__lte=to_seq
        )
        .select_related("user")
        .all()
    )
    authors = set()
    for op in ops:
        if op.user:
            authors.add(op.user.username)
        else:
            authors.add(op.site_id)
    authors_str = ", ".join(authors) if authors else "collaborators"

    prompt_template = PROMPTS.get("summary_v1", PROMPTS["summary_v1"])
    prompt = prompt_template.format(
        before_text=text_before[:2000] if text_before else "(empty document)",
        after_text=text_after[:2000] if text_after else "(empty document)",
    )

    client = get_configured_llm_client()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        summary = loop.run_until_complete(
            client.generate_text(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT_SUMMARY,
            )
        )
    finally:
        loop.close()

    summary_bullets = summary.strip()
    if job:
        job.status = "done"
        job.instruction = f"Missed edits between seq {from_seq} and {to_seq} by {authors_str}"
        job.error_message = summary_bullets
        job.save(update_fields=["status", "instruction", "error_message", "updated_at"])

    # Broadcast summary to the requesting channel or room
    channel_layer = get_channel_layer()
    if channel_layer:
        payload = {
            "type": "doc.presence",
            "data": {
                "type": "missed_summary",
                "from_seq": from_seq,
                "to_seq": to_seq,
                "authors": list(authors),
                "summary": summary_bullets,
            },
            "sender_channel": "ai_worker",
        }
        if recipient_channel:
            async_to_sync(channel_layer.send)(recipient_channel, payload["data"])
        else:
            async_to_sync(channel_layer.group_send)(f"doc_{doc_id}", payload)

    return {"status": "done", "summary": summary_bullets}


@shared_task
def suggestion_task(job_id_str: str) -> dict:
    """
    Task 9.4: Suggestion Mode (F3).
    Proposes edits as a Suggestion entity without directly mutating the CRDT.
    """
    job = AIJob.objects.filter(id=job_id_str).first()
    if not job:
        return {"status": "error", "error": "Job not found"}

    rga = get_or_load_document_rga(job.document_id)
    peer = AIPeer(doc_id=job.document_id, job_id=job.id, user=job.user)
    start_pos, end_pos, target_text = peer.resolve_anchor_range(
        rga, job.anchor_start, job.anchor_end
    )

    client = get_configured_llm_client()
    prompt = PROMPTS["rewrite_v1"].format(
        instruction=job.instruction or "Propose improvements", text=target_text
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        proposed_text = loop.run_until_complete(
            client.generate_text(prompt=prompt, system_prompt=SYSTEM_PROMPT_COAUTHOR)
        )
    finally:
        loop.close()

    suggestion = Suggestion.objects.create(
        document=job.document,
        ai_job=job,
        anchor_start=job.anchor_start or "",
        anchor_end=job.anchor_end or "",
        proposed_text=proposed_text.strip(),
        status="open",
    )

    job.status = "done"
    job.save(update_fields=["status", "updated_at"])

    # Broadcast new suggestion to room
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f"doc_{job.document_id}",
            {
                "type": "doc.presence",
                "data": {
                    "type": "new_suggestion",
                    "suggestion_id": str(suggestion.id),
                    "anchor_start": suggestion.anchor_start,
                    "anchor_end": suggestion.anchor_end,
                    "proposed_text": suggestion.proposed_text,
                },
                "sender_channel": "ai_worker",
            },
        )

    return {"status": "done", "suggestion_id": str(suggestion.id)}


@shared_task
def semantic_embed_task(doc_id_str: str, seq: int) -> dict:
    """
    Task 9.5: Semantic Search / RAG (F4).
    Splits document into chunks and saves embeddings.
    """
    doc_id = uuid.UUID(doc_id_str)
    rga = get_or_load_document_rga(doc_id)
    text = rga.text()

    if not text:
        return {"status": "skipped", "reason": "empty"}

    # Simple 500-char window chunking
    chunk_size = 500
    overlap = 50
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk_text = text[start:end]
        chunks.append(chunk_text)
        if end == len(text):
            break
        start += chunk_size - overlap

    for chunk_text in chunks:
        DocChunk.objects.create(
            document_id=doc_id,
            server_seq=seq,
            text=chunk_text,
            embedding=[0.01 * (ord(c) % 50) for c in chunk_text[:64]],  # deterministic vector mock
        )

    return {"status": "indexed", "chunks_count": len(chunks)}
