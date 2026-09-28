# ADR 002: Replicated Growable Array (RGA) Algorithm Selection

## Status
Accepted

## Context
Multiple sequence CRDT algorithms exist in academic literature:
- **WOOT** (Without Operational Transformation)
- **Logoot / LSEQ** (Fractional indexing)
- **Treedoc** (Binary tree positioning)
- **RGA** (Replicated Growable Array - Roh et al., 2011)

Fractional indexing systems (LSEQ) suffer from interleaving bugs under concurrent typing at identical positions and boundary tree balancing overhead. WOOT requires storing $O(N^2)$ relation sets.

## Decision
We implement **RGA** using a doubly linked list with a hash map index (`dict[CharId, Node]`) for $O(1)$ direct character lookup.

## Key Properties
- Character ID: `CharId(clock: int, site_id: str)`
- Insertion rule: When inserting $C$ after parent $P$, scan forward from $P$ and skip any successor nodes whose `CharId > C.CharId`.
- Deterministic convergence: Proven mathematically to maintain strict causality and total order across all replicas.
