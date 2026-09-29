# ADR 004: AI Co-Author as a First-Class CRDT Peer

## Status
Accepted

## Context
Integrating Generative AI assistants into live collaborative document editors presents a concurrency challenge:
- Traditional implementations lock the entire document, preventing human collaborators from typing while the AI is generating.
- Alternatively, models output text in bulk, causing full text replacement and clobbering concurrent human edits typed during the AI round trip.

We need an architecture where the AI co-author can stream responses into the document in real time while humans continue typing simultaneously, with zero lost keystrokes and guaranteed mathematical convergence.

## Decision
We treat the **AI Agent as a first-class CRDT peer (`AIPeer`)**:
1. **Dedicated Replica Identity:** The AI worker assigns itself a unique `site_id = "ai-{job_id}"` and advances its own Lamport logical clock.
2. **Anchor-Bound Targeting:** The user selection is anchored not by volatile integer indices, but by immutable `CharId`s (`anchor_start`, `anchor_end`).
3. **Diff-to-Op Decomposition:** As the LLM streams tokens, the `AIPeer` diffs the new text against the target anchor slice and translates modifications into standard atomic CRDT operations (`local_insert` and `local_delete`).
4. **Equal Protocol Citizen:** The generated operations pass through the identical database pipeline (`apply_operation`) and Redis Channels pub/sub group broadcast (`doc.op`) as human edits.
5. **Conflict Handling:** If human collaborators delete the anchor characters during generation, the AI peer detects the tombstone status and aborts gracefully (`AnchorDeletedError`).
6. **Cancellation Mid-Stream:** If a user clicks Cancel, the job state is updated and the streaming loop terminates immediately, leaving partially applied operations intact and revertible through version history.

## Consequences

### Positive
- **Zero Document Locking:** Humans can edit the same paragraph concurrently while the AI writes; CRDT total ordering cleanly merges both streams.
- **Auditable History:** AI operations are tagged with their distinct `site_id`, enabling full attribution in version history and op logs.
- **Graceful Degradation:** If the LLM provider fails, times out, or rate limits, the document CRDT engine and human collaboration remain 100% operational.
- **Revertible:** Because AI edits are standard CRDT operations, they can be reverted to any prior sequence number using compensating operations (ADR 003).

### Negative / Trade-offs
- **High Op Volume:** Streaming text character-by-character generates numerous operations.
- **Mitigation:** Batch ops in chunks of 5-15 characters and rely on periodic snapshotting every 500 operations.
