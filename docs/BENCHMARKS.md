# Benchmarks

Every number here comes from `loadtest/swarm.py`. Raw results are in [`docs/benchmarks/`](benchmarks/), and the table is regenerated with `python -m loadtest.summarize`.

## Method

- **What a client does.** It opens a WebSocket (session-authenticated), seeds a local RGA replica from `init`, then sends 20 ops at random positions (85% insert / 15% delete) with a 50–150 ms pause between keystrokes (about 7–20 keystrokes/s per client). All clients share one freshly created document, which is the worst case for contention.
- **Propagation latency** is measured from when client A sends an op to when client B receives that op's broadcast. All clients run in one process, so both timestamps come from the same clock. **Ack RTT** is from when A sends to when A gets its `ack`.
- **Pass condition.** After draining, every client's text and the server's persisted text (rebuilt from the op log over HTTP) must be identical, and every op must be acked. Otherwise the script exits non-zero.
- **Setup.** One Daphne process, SQLite, in-memory channel layer. The load generator runs on the **same machine** (AMD Ryzen 5 5600H, 6 cores / 12 threads, 16 GB, Windows 11, on AC power). Each client count was run 3 times with a 20 s cool-down between runs; medians are shown.

## Results (current code)

| Clients | Offered ops/s | Committed ops/s | Propagation p50 / p95 / p99 (ms) | Ack RTT p50 (ms) | Gap re-syncs | Converged |
|---:|---:|---:|---|---:|---:|:---:|
| 5 | 45 | 47 | 24 / 34 / 35 | 17 | 0 | 3/3 |
| 10 | 97 | 102 | 40 / 64 / 72 | 33 | 0 | 3/3 |
| 25 | 239 | 244 | 529 / 767 / 801 | 91 | 48 | 3/3 |
| 50 | 457 | 282 | 2,215 / 3,327 / 3,391 | 399 | 109 | 3/3 |

**Reading it:**
- Up to 10 concurrent typists, edits reach every peer with p99 under 75 ms.
- The single process saturates at about **245–280 committed ops/s**, which means about 6,000–7,000 op deliveries per second to the other clients at 25 clients. Beyond that, latency climbs and clients fall back on gap re-syncs, but **every run still converged byte-for-byte with the server**. In two of the 25-client runs, the channel layer dropped 3 and 1 broadcasts; gap repair recovered them.
- The load generator shares the CPU with the server, so these are conservative numbers for one process.

## How the write path got here (10 clients, same machine)

| Version | Propagation p50 / p95 | Broadcasts lost | Converged |
|---|---|---|:---:|
| Original code | (deadlocks on first write) | n/a | no |
| Deadlock fixed; one transaction + one broadcast per op; client tracks `max(seq)` | 1,533 / 1,856 ms | 676 of 1,800 | **no** |
| + per-doc group commit, channel capacity 1000, contiguous-seq gap repair | 141–456 / 690–1,090 ms | 0 | yes |
| + one broadcast frame per committed batch (current) | 40 / 64 ms | 0 | yes |

(The first three rows are single runs, recorded while fixing; the last row is the 3-run median above.) Details: [ADR 005](adr/005-write-path-and-fanout.md).

## Not measured here

- **The two-node Postgres + Redis cluster.** CI's `e2e` job boots `docker compose` (nginx, 2 Daphne nodes, Postgres, Redis, Celery) and runs the load test with 20 clients on every push, failing on any divergence; its JSON is uploaded as a build artifact. Run it locally with `docker compose up --build` and `python -m loadtest.swarm --url http://localhost:8080 --clients 20`.
- **Many documents at once.** Every client here edits the same document. Separate documents have separate writers and row locks, so they don't contend with each other.

## AI evals

`python -m ai.evals.runner` scores only what can be checked deterministically (non-empty, no code fences, length ratios, bullet counts, guardrail blocks and escaping). With the built-in fake model: **27/30 pass; all 6 security cases pass.** The 3 failures expect bullets or longer text, which a canned response can't produce. 18 semantic expectations (tone, meaning preservation) are reported as `unchecked`. Run with `--live` and `LLM_API_KEY` to score a real model.
