# ADR 003: Document Revert via Compensating Operations (Never Rewriting History)

## Status
Accepted

## Context
In a distributed collaborative text editing system, users need the ability to "revert" or time-travel a document to any earlier point in its history (e.g. sequence number 42).

There are two primary ways to implement revert:
1. **Log Truncation / Rollback:** Delete or mark inactive all operations after sequence 42 and reset `head_seq`.
2. **Compensating Operations (Forward Revert):** Compute the difference between the current state and the target state at sequence 42, generate *new* insert and delete CRDT operations that bring the document into that target state, and append them as fresh operations to the log.

## Decision
We implement **Revert as Forward Compensating Operations**.

## Consequences
### Positive
- **Convergence with Concurrent Editors:** If Client A reverts the document to an earlier version while Client B is actively typing in another tab or offline, the revert operations merge conflict-free through standard RGA total ordering without causing race conditions or crashes.
- **Strict Auditability & Immutability:** The operation log remains an immutable append-only ledger. A revert event is explicitly recorded as a user action with its own timestamp and sequence numbers.
- **Undo of Revert:** Because reverting is simply another sequence of operations, reverting a revert is naturally supported.

### Negative / Trade-offs
- The log size increases with each revert.
- Handled by periodic snapshotting (every 500 ops) so loading performance remains $O(1)$ relative to total log length.
