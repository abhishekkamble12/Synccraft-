# ADR 005: Write Path, Cache Coherence, Group Commit, and Lossy Fan-out

## Status
Accepted

## Context
Every keystroke is one CRDT op that must be (1) given a per-document sequence number, (2) persisted, (3) applied to a server-side replica used for reads and snapshots, and (4) broadcast to every other socket on the document.

The first implementation had four problems, each found by running the code:

| Problem | Symptom |
|---|---|
| `apply_operation` held a per-document `threading.Lock` and then called a loader that took the same non-reentrant lock | Every write deadlocked, freezing all DB work in the process |
| Each process cached an RGA replica that it never refreshed | With 2 web nodes, each served stale text and wrote snapshots missing the other node's ops |
| One transaction plus one broadcast per keystroke, and each socket blocked on its own commit | 10 clients: propagation p50 ≈ 1.5 s |
| The channel layer silently drops group messages when a socket's queue is full (`ChannelFull` is swallowed in both the in-memory and Redis layers) | 10 clients: 676 of 1,800 broadcasts lost, clients permanently diverged |

## Decision

### 1. Postgres is the sequencer; replicas are caches that catch up
`apply_operations` takes `SELECT … FOR UPDATE` on the `Document` row, which serialises `server_seq` assignment across **all** processes. Each process's replica records the highest `server_seq` it has applied. A writer that holds the row lock compares that to `head_seq` and replays only the missing rows from the op log before it applies new ops. Readers always run the (indexed) catch-up query. If a transaction fails, the replica is evicted rather than left holding uncommitted ops.

The per-process lock still exists, but only to stop two threads from mutating one RGA linked list at once. It is never re-entered.

### 2. Group commit per document
WebSocket consumers don't write. They enqueue the op on a per-document `DocumentWriter` (`documents/write_queue.py`) and get a future back, so this socket keeps delivering peers' broadcasts while it waits. The writer drains whatever has queued (up to 256 ops), commits it as **one** transaction, and resolves the futures. Under light load a batch is one op, so there's no added latency. Under contention, batches grow by themselves. If a batch fails, it is retried op by op, so one bad op (e.g. an `op_id` collision) only rejects that op.

### 3. One broadcast frame per batch
The writer sends a single `ops` frame with every newly committed op in the batch. Each socket filters out its own ops (it gets an `ack` instead). This cuts fan-out from `ops × sockets` frames to `batches × sockets`.

### 4. Treat pub/sub as lossy; repair with sequence numbers
Clients track the highest **contiguous** `server_seq` they have applied, not the maximum seen. If a hole persists for 1 s, the client sends `sync{reason:"gap", last_seq}` and the server replays the log from there. Because ops are idempotent, replays are harmless. `reason` also distinguishes a real reconnect (`"reconnect"`, which may trigger the AI "what changed while you were away" summary) from gap repair, which never does.

## Consequences
- Correctness no longer depends on the broker delivering every message, which neither Channels layer guarantees.
- Multiple web nodes are safe without sticky sessions or a single-writer process per document.
- Measured on one laptop process (see `docs/BENCHMARKS.md`): at 10 clients, propagation p50 dropped from about 1.5 s (with divergence) to 40 ms with 0 divergence.
- **Ceiling:** a single Python process saturates at roughly 250–280 committed ops/s on the benchmark laptop. Beyond that, latency grows but replicas still converge. The next steps would be more Daphne processes (now safe, per §1) and moving per-op JSON work out of the event loop.
- **Not done:** tombstone garbage collection. RGA tombstones are kept forever; snapshots bound replay time but not memory.
