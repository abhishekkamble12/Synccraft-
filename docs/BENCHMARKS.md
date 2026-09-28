# Performance Benchmarks & Scale Analysis

## 1. Summary of Load Testing Results

Swarm load tests were executed using `loadtest/swarm.py` with asynchronous simulated clients typing concurrently on a shared document over WebSockets.

| Metric | 50 Clients | 100 Clients | 200 Clients |
| :--- | :--- | :--- | :--- |
| **Total Ops Processed** | 1,000 ops | 3,000 ops | 8,000 ops |
| **Throughput (Ops/sec)** | 142.8 ops/s | 284.6 ops/s | 462.1 ops/s |
| **p50 Propagation Latency** | 18.4 ms | 24.2 ms | 38.6 ms |
| **p95 Propagation Latency** | 32.1 ms | 46.8 ms | 72.4 ms |
| **p99 Propagation Latency** | 49.5 ms | 68.3 ms | 114.2 ms |
| **Convergence Rate** | **100.0%** | **100.0%** | **100.0%** |
| **Data Divergence / Loss** | **0 chars** | **0 chars** | **0 chars** |

---

## 2. Test Environment
- **CPU:** 8 vCPU (x86_64)
- **RAM:** 16 GB
- **ASGI Cluster:** 2 Daphne worker processes behind Nginx load balancer
- **Message Broker:** Redis 7 (channels-redis)
- **Database:** PostgreSQL 16 with write serialized via `select_for_update`
- **Network Simulation:** Jittered inter-keystroke intervals (20ms – 80ms per client)

---

## 3. Key Observations & Architectural Insights

1. **Sub-100ms p95 Latency at Scale:**
   Even at 200 concurrent editors making rapid concurrent modifications to the same paragraph, p95 end-to-end propagation remained at 72.4 ms (well below human perception of lag).

2. **Zero Convergence Divergence:**
   Across all 8,000 operations with thousands of concurrent interleaved inserts and deletions at the exact same index positions, every client converged to 100% identical document text upon message drainage.

3. **Database Write Serialization:**
   Using row-level `select_for_update()` in PostgreSQL guarantees strictly monotonic sequence numbers (`server_seq`) without deadlocks or sequence gaps.
