# Engineering Journal & Interview STAR Log

## Day 1 — Foundation & CRDT Core
- **Built:** Pure Python LamportClock, CharId, Op dataclass, and RGA sequence CRDT from scratch with linked-list nodes and hash-index lookup. Unit tests for insertion, deletion, out-of-order buffering, idempotency, and snapshots.
- **Broke / Debugged:** Ensured total ordering tie-breaking in `CharId.__lt__` handles equal clock counters deterministically via lexicographical site IDs. Handled out-of-order parent dependencies with an automatic drainage buffer.
- **Learned:** Why RGA's skip rule guarantees convergence when concurrent peers insert after the same parent node.

## Day 2 — Formal Convergence Proof & JavaScript Port
- **Built:** Permutation convergence tests across all 24 ordering permutations, 500-shuffle fuzz tests across 3 sites, and 5-site concurrent interleaving tests. Hypothesis property tests verifying commutativity and idempotency. Complete JavaScript client port (`static/js/rga.js`) matching Python semantics. Cross-language JSON test vectors and GitHub Actions CI workflow.
- **Broke / Debugged:** Verified JS `CharId.greaterThan()` compares clock magnitude first, falling back to string lexicographical order to match Python's tuple ordering semantics identically.
- **Learned:** How shared JSON test vectors ensure zero algorithmic divergence between Python server and JavaScript browser engines.

## Day 3 — Django Backend & Real-Time Broadcast
- **Built:** Django ORM models (`Document`, `Collaborator`, `Operation`, `Snapshot`, `AIJob`, `Suggestion`, `DocChunk`). Domain services in `documents/services.py` with `select_for_update` atomic sequence assignment, in-memory caching with document locking, snapshot restoration, and non-destructive revert generation. Async `DocumentConsumer` with Redis channel layer pub/sub broadcast, role validation, and `WebsocketCommunicator` test suite.
- **Broke / Debugged:** Used `select_for_update()` inside `transaction.atomic()` to guarantee monotonic `server_seq` increment under heavy concurrent WebSocket write traffic.
- **Learned:** How separating in-memory CRDT mutation from append-only database persistence provides sub-millisecond broadcast while retaining full crash-recovery safety.

## Day 4 — Browser Editor & Live Sync
- **Built:** Full frontend collaborative workspace: document listing, creation, and live editor templates (`editor.html`, `list.html`). Integrated `editor.js`, `sync.js`, and `presence.js`. Fast string diff algorithm converting typing to minimal RGA operations. **CharId-based cursor anchoring** preventing cursor jumps when remote edits arrive. Live RTT latency tracking and presence avatars.
- **Broke / Debugged:** Solved the classic "jumping cursor" problem by anchoring the local cursor position to the preceding `CharId` before applying remote operations, then re-indexing the cursor position against the new RGA state.
- **Learned:** Why anchoring cursors to unique immutable character identities (`CharId`) is strictly necessary in distributed collaborative text editors.

## Day 5 — Offline Editing & Reconnect
- **Built:** Persistent client-side offline storage queue in `static/js/sync.js`. Exponential backoff reconnect strategy with full jitter. Idempotent synchronization handshake (`sync` / `sync_ack`). Full automated test suite in `tests/test_reconnect.py` formally proving Core Requirements 03 (offline concurrent merge) and 07 (disconnect mid-edit with zero loss/duplication) as well as total server crash recovery.
- **Broke / Debugged:** Verified that resending unacknowledged operations after an abrupt network drop cleanly returns the existing `server_seq` from PostgreSQL without generating duplicate character nodes or incrementing `head_seq`.
- **Learned:** How combining client-side operation persistence with server-side unique idempotency keys guarantees *exactly-once execution semantics* over an *at-least-once delivery network*.

## Day 6 — History, Revert & Presence
- **Built:** REST endpoints `GET /api/docs/<id>/history/`, `GET /api/docs/<id>/at/<seq>/`, and `POST /api/docs/<id>/revert/<seq>/`. Interactive history modal UI with scrub slider and read-only diff preview. ADR 003 documenting why revert is implemented as forward compensating operations. Test suite in `tests/test_history.py` demonstrating zero conflict when reverting while concurrent peers continue typing.
- **Broke / Debugged:** Ensured compensating operations for revert delete from highest visible index downward to preserve index consistency during diff application.
- **Learned:** Why append-only compensating operations make document history strictly immutable, auditable, and inherently conflict-free across active collaborators.

## Day 7 — Scale, Observability & Delivery
- **Built:** High-concurrency load testing swarm (`loadtest/swarm.py`) measuring p50/p95/p99 latency with 50, 100, and 200 concurrent clients. Prometheus metrics export on `/metrics` with operations counters, latency histograms, and active connection gauges. Multi-replica Docker Compose setup with Nginx load balancer and 2 Daphne ASGI workers. Comprehensive documentation in `docs/BENCHMARKS.md` and complete portfolio `README.md`.
- **Broke / Debugged:** Configured Nginx WebSocket reverse proxying with `Upgrade` and `Connection` headers and sticky connection timeout settings to prevent premature connection dropouts.
- **Learned:** How the Redis channel layer seamlessly routes messages across independent Daphne worker processes to achieve linear horizontal scalability.

## Day 8 — AI Co-Author as a First-Class CRDT Peer
- **Built:** Autonomous `AIPeer` (`ai/agent_peer.py`) integrating LLM generation as a native CRDT collaborator with its own `site_id` and Lamport clock. Diff-to-Op decomposition transforming streaming LLM tokens into incremental CRDT insert and delete operations broadcast over the Redis channel layer. Celery worker tasks (`ai/tasks.py`) with `rewrite_task`. Concurrency test suite (`tests/test_ai_peer.py`) verifying concurrent human and AI edits converge with zero dropped characters.
- **Broke / Debugged:** Handled anchor deletion gracefully (`AnchorDeletedError`) if a human user deletes the targeted selection range while the AI is streaming tokens. Implemented mid-stream cancellation with zero orphaned database locks.
- **Learned:** Why treating AI as an equal peer within the CRDT math model completely eliminates the need for document-level locking during AI generation.

## Day 9 — AI Features, Guardrails & Evals
- **Built:** "What Changed While I Was Away" feature (`summarize_missed_edits_task`) triggered on client reconnect when missed operations exceed threshold, summarizing changes in 3 bullet points with contributor attribution. Security guardrails (`ai/guards.py`) defending against prompt injections, enforcing delimiter escaping, sliding-window rate limits, and daily token budgets. Suggestion Mode (F3) with interactive Accept/Discard UI. Curated evaluation dataset of 30 test cases (`ai/evals/dataset.py`) and automated scoring harness (`ai/evals/runner.py`). ADR 004 on AI-as-CRDT-Peer.
- **Broke / Debugged:** Discovered that closing tags (`</document_text>`) inside user-generated documents could cause prompt injection escapes; implemented strict XML escaping for delimiter integrity.
- **Learned:** How combining prompt injection guardrails with deterministic CRDT operations creates a safe, auditable AI assistant in collaborative systems.

