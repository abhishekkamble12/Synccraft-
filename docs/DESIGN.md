# Design Document: Real-Time Collaborative Sync Engine

## 1. Problem
Several people edit one document at the same time over unreliable networks, sometimes offline. Locks block people, and last-write-wins loses keystrokes. The system must let every replica apply edits in any order and still end with identical text, keep a full history, and survive disconnects, process restarts, and more than one server node.

## 2. Core decisions (details in `docs/adr/`)

| Decision | Choice | ADR |
|---|---|---|
| Conflict resolution | Operation-based RGA sequence CRDT, written from scratch in Python and JS | 001, 002 |
| Character identity | `CharId = (lamport, site_id)`; concurrent inserts after the same parent are ordered by `CharId` descending | 002 |
| Delivery semantics | At-least-once delivery + idempotent ops (`op_id`) = effectively-once application | 002 |
| History & revert | Append-only op log; revert = new compensating ops, never rewriting history | 003 |
| AI edits | The AI is a CRDT peer with its own `site_id`; output is validated, then diffed into ops | 004 |
| Write path | Postgres row lock sequences ops; per-process replicas catch up from the log; per-doc group commit; lossy fan-out repaired by sequence gaps | 005 |

## 3. Architecture

```
 browser (rga.js + sync.js)                     browser
        │  WebSocket                                │
        ▼                                           ▼
 ┌──────────────┐   nginx (least_conn)   ┌──────────────┐
 │ Daphne node 1│◄──────────────────────►│ Daphne node 2│
 │  consumer    │                        │  consumer    │
 │  DocWriter ──┼── group commit ──┐  ┌──┼── DocWriter  │
 │  RGA replica │                  ▼  ▼  │  RGA replica │
 └──────┬───────┘             PostgreSQL └──────┬───────┘
        │                 op log · snapshots     │
        └──────────── Redis channel layer ───────┘
                 (fan-out, Celery broker, rate limits)
```

Write of one keystroke:
1. The browser applies the op locally, stores it in `localStorage` until acked, and sends `op`.
2. The consumer enqueues it on the document's `DocumentWriter` and keeps processing other messages.
3. The writer commits the queued batch in one transaction: `SELECT … FOR UPDATE` on the document, catch up the replica if another node wrote, assign `server_seq`s, `bulk_create` the ops, snapshot every 500 ops.
4. One `ops` frame goes to the Redis group and each node forwards it to its sockets (minus the sender, who gets `ack`).
5. Clients advance their contiguous `last_seq`; a hole that lasts 1 s triggers `sync{reason:"gap"}`.

## 4. WebSocket protocol

| Direction | Type | Payload | Purpose |
|---|---|---|---|
| server → client | `init` | `{head_seq, text, snapshot, role}` | Initial state on connect |
| client → server | `op` | `{op}` | One local edit |
| server → client | `ack` | `{op_id, seq}` | Edit committed with sequence number |
| server → client | `ops` | `{ops: [{seq, op}, …]}` | Other clients' committed edits (one frame per commit batch) |
| client → server | `sync` | `{reason: "reconnect"\|"gap"\|"flush", last_seq, pending: [op…]}` | Catch up and upload offline edits |
| server → client | `sync_ack` | `{missed: [{seq, op}…], acked: [op_id…], head_seq}` | Every op after `last_seq` |
| client ↔ server | `presence` | `{color, cursor_anchor, cursor_pos}` | Live cursors (username is taken from the session) |
| client → server | `ai_request` / `ai_cancel` | `{kind, anchor_start, anchor_end, instruction}` / `{job_id}` | AI co-author jobs (editors only, scoped to this document) |
| server → client | `ai_status`, `new_suggestion`, `suggestion_update`, `missed_summary` | | AI job lifecycle |
| server → client | `error` | `{code, message}` | `forbidden`, `bad_op`, `op_rejected`, `rate_limited`, … |

## 5. Security model
- **Authentication:** Django sessions. WebSocket upgrades must carry an `Origin` matching `ALLOWED_HOSTS` (blocks cross-site WebSocket hijacking).
- **Authorization:** deny by default (`documents/permissions.py`). Only owners and collaborators can open a document, over HTTP or WebSocket. Only owners/editors can write, revert, or run AI jobs. Documents a user can't access return 404, so IDs can't be probed.
- **AI guardrails:** instruction injection heuristics, delimiter escaping of document text, per-user rate limits and daily token budgets in the shared cache.

## 6. Known limitations
- Tombstones are never garbage-collected.
- One Python process saturates at roughly 250–280 committed ops/s on a laptop (`docs/BENCHMARKS.md`).
- Presence is ephemeral and not persisted.
- The AI applies its edit when generation finishes; it does not stream text into the document.
