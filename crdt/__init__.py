"""
CRDT (Conflict-free Replicated Data Types) Core Library.

A standalone, pure-Python library implementing the Replicated Growable Array (RGA)
algorithm for real-time collaborative text editing.

Exports:
    - LamportClock: Logical clock for causal event ordering.
    - CharId, ROOT: Immutable, totally-ordered character identifiers.
    - Op: Immutable operation dataclass (insert / delete).
    - RGA, Node: Replicated Growable Array document replica.
"""

from crdt.clock import LamportClock
from crdt.ids import CharId, ROOT
from crdt.ops import Op
from crdt.rga import Node, RGA

__all__ = [
    "LamportClock",
    "CharId",
    "ROOT",
    "Op",
    "Node",
    "RGA",
]
