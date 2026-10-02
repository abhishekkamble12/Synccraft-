# ADR 004: AI Co-Author as a First-Class CRDT Peer

## Status
Accepted (revised after the hardening pass. See "Revision" below.)

## Context
An AI assistant that rewrites part of a shared document runs into a concurrency problem:
- Locking the document while the model generates blocks every human collaborator.
- Replacing the selected text wholesale when the model returns throws away any keystrokes humans typed during the round trip.

We want the AI's edit to merge with concurrent human edits under the same convergence guarantee that human edits get.

## Decision
The AI is a CRDT peer (`AIPeer`, `ai/agent_peer.py`):

1. **Own replica identity.** Each job gets a unique `site_id` (`ai-<random>`) and Lamport clock, so its `CharId`s never collide with human ones.
2. **Anchors, not indices.** The selection is identified by immutable `CharId`s (`anchor_start`, `anchor_end`). Positions are resolved against the latest server state when the edit is applied, not when it was requested.
3. **Buffer, validate, then apply.** LLM output is collected in full and passed through the output guardrails *before* any op is emitted. A rejected or cancelled response therefore never leaves half-written text in the document. Cancellation is polled from the DB (at most every 0.5 s) while the stream is read.
4. **Diff-to-ops.** The accepted text is diffed against the anchored range (`difflib.SequenceMatcher`), and only the changed characters become insert/delete ops, generated right-to-left so earlier indices stay valid.
5. **Same pipeline as humans.** The ops are committed in one batch via `apply_operations` (one transaction, one row lock) and broadcast as a single `ops` frame.
6. **Anchor deleted → abort.** If a collaborator deleted an anchor character before the edit lands, the job fails with `AnchorDeletedError` instead of guessing.

## Consequences

### Positive
- **No document locking.** Human ops that are concurrent with the AI's ops merge by RGA ordering. `tests/test_ai_peer.py::test_ai_and_human_concurrent_edits_converge` delivers the full log to replicas in different orders and checks they converge with every human character kept.
- **Attribution and undo.** AI ops carry their own `site_id` in the op log and can be reverted like any other edit (ADR 003).
- **Graceful degradation.** LLM failures mark the job `failed`; editing is unaffected.

### Negative / trade-offs
- **No token-by-token streaming into the document.** Users see an "AI is writing…" presence indicator, not text appearing live. This was a deliberate choice for guardrail safety; streaming would need a "draft" layer that can be discarded atomically.
- **Diff granularity is characters.** A long rewrite becomes many ops (bounded by output size); they share one transaction and one broadcast frame.

## Revision
The first version claimed that the AI "streams tokens as ops" and called the async ORM from inside an event loop (`SynchronousOnlyOperation` on every job). The peer is now synchronous, which fits a Celery worker. Only the LLM stream runs async, through `async_to_sync`, and the "streaming" claim was replaced by the buffer-validate-apply design above.
