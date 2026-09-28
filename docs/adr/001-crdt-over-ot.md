# ADR 001: CRDT (RGA) over Operational Transformation (OT)

## Status
Accepted

## Context
A real-time collaborative text editor requires a mechanism to merge concurrent edits made by multiple distributed clients without conflict or data loss. The two primary paradigms are:
1. **Operational Transformation (OT)** (used by Google Docs, Etherpad)
2. **Conflict-free Replicated Data Types (CRDT)** (used by Figma, Apple Notes, Yjs, Automerge)

## Decision
We chose **CRDT (specifically RGA - Replicated Growable Array)** implemented from first principles in pure Python.

## Consequences
### Positive
- **Decentralized Convergence:** Operations can be applied out-of-order or after long offline periods and will naturally converge to identical state without requiring a single sequencing coordinator.
- **Formal Correctness:** Commutativity, associativity, and idempotency can be formally verified using property-based testing (Hypothesis).
- **Simpler Reconnect Protocol:** Clients can buffer operations locally in IndexedDB/localStorage and replay them upon reconnection without complex transformation matrix math.

### Negative / Trade-offs
- **Tombstones:** Deleted characters remain in memory as tombstone nodes to preserve causal ordering for delayed concurrent operations.
- **Mitigation:** Implement periodic snapshots and causal stability garbage collection.
