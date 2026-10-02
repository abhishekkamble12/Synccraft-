# Real-Time Collaborative Sync Engine

A Google-Docs-style collaborative text editor whose consistency layer is built from first principles: a hand-written **RGA sequence CRDT** (Python and JavaScript, no CRDT libraries), a Django Channels WebSocket server with an idempotent offline-sync protocol, a Postgres op log with time-travel and non-destructive revert, and an **AI co-author that edits as just another CRDT peer**.

[![CI](https://github.com/abhishekkamble12/Monach_AI_Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/abhishekkamble12/Monach_AI_Platform/actions/workflows/ci.yml)

## What's interesting here

- **CRDT from scratch, verified two ways.** `crdt/` (strict mypy) and `static/js/rga.js` implement the same RGA. Convergence is checked with exhaustive permutation tests, randomised delivery-order fuzzing and Hypothesis property tests, and the two implementations are held to shared JSON test vectors so the browser and server can never disagree.
- **Correct with more than one server.** Postgres `SELECT … FOR UPDATE` is the sequencer. Each process's in-memory replica records the last `server_seq` it applied and replays newer rows from the op log before it is read or written, so two Daphne nodes behind nginx stay coherent without sticky sessions ([ADR 005](docs/adr/005-write-path-and-fanout.md)).
- **Delivery treated as lossy.** Django Channels' layers silently drop broadcasts when a socket's queue fills. The load test caught this: 676 of 1,800 broadcasts lost and clients diverged. Clients now track their highest *contiguous* sequence number and re-sync when a gap persists, so correctness rests on sequence numbers, not on the broker.
- **Group commit and batched fan-out.** Sockets enqueue ops on a per-document writer that commits whatever has queued as one transaction and broadcasts one frame per batch. At 10 concurrent typists, propagation p50 fell from about 1.5 s (with divergence) to 40 ms with zero divergence.
- **Offline-first.** Unacked ops persist in `localStorage`. On reconnect the client sends `sync{last_seq, pending}`, the server commits the pending ops idempotently by `op_id` and returns everything missed. A disconnect mid-edit never duplicates or loses characters.
- **History without rewriting history.** Any past state can be rebuilt from snapshot plus log replay, and revert appends a minimal set of compensating ops, so it merges with concurrent edits ([ADR 003](docs/adr/003-revert-as-new-ops.md)).
- **AI as a CRDT peer.** The co-author gets its own `site_id`, anchors its target range by `CharId`, validates the model's output, then diffs it into ops that merge with concurrent human typing ([ADR 004](docs/adr/004-ai-as-crdt-peer.md)). Prompt-injection heuristics, delimiter escaping, rate limits and token budgets live in `ai/guards.py`.
- **Deny-by-default access.** Owner/editor/viewer roles are enforced on every HTTP view and WebSocket message, inaccessible documents return 404, and WebSocket upgrades are `Origin`-checked against cross-site hijacking.

## Measured performance

Single Daphne process, SQLite, in-memory channel layer, with the load generator on the **same laptop** (Ryzen 5 5600H, 6C/12T, Windows 11). Every client types at 7–20 keystrokes/s (15% deletes) into one shared document, and the run fails unless every client replica *and* the server end up byte-identical. Medians of 3 runs ([method and raw JSON](docs/BENCHMARKS.md)).

| Concurrent typists | Committed ops/s | Edit propagation p50 / p95 / p99 | Converged |
|---:|---:|---|:---:|
| 5 | 47 | 24 / 34 / 35 ms | 3/3 runs |
| 10 | 102 | 40 / 64 / 72 ms | 3/3 runs |
| 25 | 244 | 529 / 767 / 801 ms | 3/3 runs |
| 50 | 282 (saturated) | 2.2 / 3.3 / 3.4 s | 3/3 runs |

Up to about 10 simultaneous typists in one document, edits reach every peer in tens of milliseconds. Beyond that, a single Python process saturates at about 250–280 committed ops/s. Latency then grows, but every run still converges. CI runs the same load test against the two-node Postgres + Redis cluster on every push.

## Architecture

```
browser (rga.js, sync.js) ──WS──► nginx ──► Daphne × 2 ──► per-doc group-commit writer
                                               │                   │
                                     Redis channel layer     PostgreSQL op log
                                     (fan-out, Celery)       (row-lock sequencing, snapshots)
                                               │
                                        Celery worker (AI co-author)
```

Protocol, data flow and security model: [docs/DESIGN.md](docs/DESIGN.md). Design decisions: [docs/adr/](docs/adr/).

## Run it

**Full stack (two web nodes, Postgres, Redis, Celery, nginx):**

```bash
docker compose up --build
# open http://localhost:8080, register, create a doc, and open it in two windows
```

**Local, no Docker (single process, SQLite, in-memory channel layer, AI jobs in-process):**

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
export DEBUG=1                                       # PowerShell: $env:DEBUG=1
python manage.py migrate
daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

Set `GROQ_API_KEY` (or `LLM_API_KEY`) for a real model. When `GROQ_API_KEY` or a key starting with `gsk_` is set, requests are automatically routed to Groq's API (`openai/gpt-oss-120b`). You can also configure other OpenAI-compatible endpoints with `LLM_BASE_URL` and `LLM_MODEL`. Without an API key, AI features use a deterministic offline fake.

## Tests

```bash
pytest                                   # 76 tests: CRDT properties, services, WebSocket protocol,
                                         # permissions, group commit, multi-node cache coherence, AI peer
python -m tests.vectors.generate_vectors
node --test tests/vectors/test_rga_js.mjs tests/js/test_sync_gaps.mjs

python -m loadtest.swarm --url http://127.0.0.1:8000 --clients 10 --ops 20
python -m ai.evals.runner                # guardrail/format evals (deterministic fake model)
LLM_API_KEY=... python -m ai.evals.runner --live
```

The eval runner only scores properties it can check deterministically. Semantic expectations (tone, meaning) are reported as `unchecked` rather than counted as passes. With the fake model, 27/30 cases pass, including all 6 security cases. The 3 failures expect bullets or longer text, which a canned response can't produce.

## Limitations

- Tombstones are never garbage-collected (snapshots bound replay time, not memory).
- One Python process tops out at about 250–280 ops/s per document on a laptop; the next step is spreading documents across more processes.
- The AI applies its edit when generation finishes rather than streaming text into the document.
- Presence is ephemeral.

## License
MIT
