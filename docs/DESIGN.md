# Design Document — Real-Time Collaborative Sync Engine

## 1. Problem Statement
Modern collaborative applications (Google Docs, Figma, Notion) allow multiple distributed users to simultaneously edit shared state across high-latency, unreliable networks. When multiple clients edit the exact same document location concurrently or while disconnected, simple lock-based or last-write-wins strategies cause data loss, corruption, or user frustration.

This project implements a **Real-Time Collaborative Sync Engine** from first principles, providing strong eventual consistency, offline editing resilience, and non-destructive version history.

---

## 2. Core Architectural Decisions

### 2.1 Conflict Resolution: CRDT (RGA) over Operational Transformation (OT)
- **Decision:** We use an **Operation-based Replicated Growable Array (RGA)** CRDT.
- **Rationale:** Operational Transformation (OT) requires a centralized server to serialize and transform concurrent operations against a global history buffer. OT is notoriously difficult to verify for correctness under complex concurrency patterns and performs poorly in peer-to-peer or extended offline editing scenarios.
- **CRDT Advantage:** State and operation CRDTs are mathematically commutative, associative, and idempotent. Edits can be applied in any arrival order and guarantee convergence across all replicas.
- **Trade-off:** RGA requires tombstone markers for deleted characters, increasing memory consumption. We address this with periodic snapshotting and tombstone garbage collection.

### 2.2 Character Identity & Total Ordering
- Every character is uniquely identified by `CharId = (lamport_clock: int, site_id: str)`.
- `ROOT = CharId(0, "")` serves as the document start sentinel.
- Concurrent insertions at the same parent position are ordered deterministically by comparing `CharId`: higher `lamport_clock` comes first; ties are broken by lexicographical order of `site_id`.

### 2.3 Idempotency & Delivery Guarantees
- Every operation generated carries a unique `op_id` (UUID).
- Replicas maintain an `applied_op_ids` set. Receiving an operation multiple times (e.g. on network retry / reconnect) is guaranteed to be a safe no-op.

### 2.4 Transport & Broadcast Architecture
- **WebSockets via Django Channels:** Low-latency bi-directional transport.
- **Redis Channel Layer:** Decoupled pub/sub message broker enabling horizontal scaling across multiple Daphne ASGI worker processes.

### 2.5 Persistence & Revert Strategy
- **Append-Only Operation Log:** Operations are persisted sequentially in PostgreSQL with per-document sequence numbers (`server_seq`).
- **Revert as New Ops:** History is immutable and never rewritten. Reverting to an earlier timestamp generates *new compensating operations*, preserving auditability and convergence.

---

## 3. WebSocket Protocol Specification

| Direction | Message Type | Payload | Purpose |
|---|---|---|---|
| `client → server` | `sync` | `{"type":"sync", "last_seq": 42, "pending": [op,...]}` | Reconnect & catch up |
| `server → client` | `sync_ack` | `{"type":"sync_ack", "missed": [op,...], "acked": [id,...], "head_seq": 57}` | Catch-up response |
| `client → server` | `op` | `{"type":"op", "op": {...}}` | Client edit |
| `server → group` | `op` | `{"type":"op", "op": {...}, "seq": 58}` | Broadcast edit |
| `server → client` | `ack` | `{"type":"ack", "op_id": "...", "seq": 58}` | Edit acknowledgement |
| `client ↔ server` | `presence` | `{"type":"presence", "user": "A", "color": "#e66", "cursor_anchor": "12@site3"}` | Remote cursor & presence |
| `client → server` | `ai_request` | `{"type":"ai_request", "kind": "rewrite", "anchor_start": "...", "anchor_end": "..."}` | Async AI co-author task |
