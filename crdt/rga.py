"""
Replicated Growable Array (RGA) Conflict-free Replicated Data Type.

A pure Python, zero-dependency implementation of a sequence CRDT for collaborative text editing.
Guarantees strong eventual consistency: all replicas that receive the same set of operations
converge to the exact same text and internal state regardless of delivery order.
"""

from dataclasses import dataclass
from typing import Any

from crdt.clock import LamportClock
from crdt.ids import ROOT, CharId
from crdt.ops import Op


@dataclass(slots=True)
class Node:
    """
    A single node in the RGA doubly-linked list.

    Attributes:
        char_id: Globally unique, immutable character identifier.
        char: Single character string.
        deleted: Tombstone flag indicating if character was deleted.
        parent_id: CharId of the preceding node when this node was created.
        next: Pointer to next node in document order.
        prev: Pointer to previous node in document order.
    """

    char_id: CharId
    char: str
    deleted: bool = False
    parent_id: CharId | None = None
    next: "Node | None" = None
    prev: "Node | None" = None


class RGA:
    """
    RGA Sequence CRDT document replica.

    Supports:
    - local_insert(pos, char) -> Op
    - local_delete(pos) -> Op
    - apply(op) -> bool (idempotent, order-independent)
    - text() -> str
    - visible_len() -> int
    - char_id_at(pos) -> CharId
    - State serialization / deserialization for snapshots
    """

    def __init__(self, site_id: str, clock: LamportClock | None = None) -> None:
        self.site_id: str = site_id
        self.clock: LamportClock = clock if clock is not None else LamportClock()

        # Sentinel head node representing ROOT
        self._head: Node = Node(
            char_id=ROOT,
            char="",
            deleted=False,
            parent_id=None,
            next=None,
            prev=None,
        )

        # Fast O(1) node lookup index: CharId -> Node
        self._nodes: dict[CharId, Node] = {ROOT: self._head}

        # Set of applied op_ids for idempotency
        self._applied_op_ids: set[str] = set()

        # Buffer for out-of-order ops (e.g., child arrives before parent)
        self._pending_ops: list[Op] = []

    # -------------------------------------------------------------------------
    # Local Operations
    # -------------------------------------------------------------------------

    def local_insert(self, pos: int, char: str, op_id: str | None = None) -> Op:
        """
        Insert a character locally at 0-indexed visible position `pos`.

        Args:
            pos: Visible index (0 <= pos <= visible_len()). 0 inserts at start.
            char: Single character string to insert.
            op_id: Optional custom idempotency key.

        Returns:
            The generated insert Op to be broadcast to peers.
        """
        if len(char) != 1:
            raise ValueError(f"local_insert expects a single character, got: {char!r}")

        # Find the visible predecessor node (or head if pos == 0)
        parent_node = self._find_visible_node_at_index(pos - 1) if pos > 0 else self._head
        parent_id = parent_node.char_id

        # Advance local logical clock
        current_clock = self.clock.tick()
        new_char_id = CharId(clock=current_clock, site_id=self.site_id)

        op = Op.create_insert(
            site_id=self.site_id,
            lamport=current_clock,
            char_id=new_char_id,
            parent_id=parent_id,
            char=char,
            op_id=op_id,
        )

        self._apply_insert(op)
        self._applied_op_ids.add(op.op_id)
        return op

    def local_delete(self, pos: int, op_id: str | None = None) -> Op:
        """
        Delete a character locally at 0-indexed visible position `pos`.

        Args:
            pos: Visible index (0 <= pos < visible_len()).
            op_id: Optional custom idempotency key.

        Returns:
            The generated delete Op to be broadcast to peers.
        """
        target_node = self._find_visible_node_at_index(pos)
        if target_node is self._head or target_node.deleted:
            raise IndexError(f"No visible character at position {pos}")

        current_clock = self.clock.tick()
        op = Op.create_delete(
            site_id=self.site_id,
            lamport=current_clock,
            char_id=target_node.char_id,
            op_id=op_id,
        )

        self._apply_delete(op)
        self._applied_op_ids.add(op.op_id)
        return op

    # -------------------------------------------------------------------------
    # Remote / Generic Operation Application
    # -------------------------------------------------------------------------

    def apply(self, op: Op) -> bool:
        """
        Apply an operation (local or remote) to the RGA document.
        Guarantees idempotency (applying the same op twice is a safe no-op).

        Args:
            op: The Op to apply.

        Returns:
            True if the operation was newly applied, False if ignored (duplicate/buffered).
        """
        if op.op_id in self._applied_op_ids:
            return False

        # Advance logical clock to at least remote timestamp
        self.clock.update(op.lamport)

        applied = False
        if op.type == "insert":
            # Check if parent is available
            if op.parent_id in self._nodes:
                self._apply_insert(op)
                self._applied_op_ids.add(op.op_id)
                applied = True
                self._drain_pending_ops()
            else:
                # Buffer until parent arrives
                self._pending_ops.append(op)
                return False
        elif op.type == "delete":
            if op.char_id in self._nodes:
                self._apply_delete(op)
                self._applied_op_ids.add(op.op_id)
                applied = True
            else:
                # Buffer delete until target character is inserted
                self._pending_ops.append(op)
                return False

        return applied

    def _apply_insert(self, op: Op) -> None:
        """Internal helper to insert a node according to RGA total ordering."""
        assert op.parent_id is not None
        assert op.char is not None

        if op.char_id in self._nodes:
            # Already inserted
            return

        parent_node = self._nodes[op.parent_id]
        new_node = Node(
            char_id=op.char_id,
            char=op.char,
            deleted=False,
            parent_id=op.parent_id,
        )

        # RGA Insertion Rule:
        # Start at parent_node. Scan forward through siblings/descendants as long as
        # next node's char_id is strictly greater than the new node's char_id.
        curr = parent_node
        while curr.next is not None:
            next_node = curr.next
            if next_node.char_id > op.char_id:
                curr = next_node
            else:
                break

        # Splice new_node after curr
        new_node.next = curr.next
        new_node.prev = curr
        if curr.next is not None:
            curr.next.prev = new_node
        curr.next = new_node

        self._nodes[op.char_id] = new_node

    def _apply_delete(self, op: Op) -> None:
        """Internal helper to mark a node as a tombstone."""
        if op.char_id in self._nodes:
            node = self._nodes[op.char_id]
            node.deleted = True

    def _drain_pending_ops(self) -> None:
        """Drain buffered out-of-order operations whose parents/targets are now present."""
        if not self._pending_ops:
            return

        progress = True
        while progress:
            progress = False
            remaining: list[Op] = []
            for op in self._pending_ops:
                if op.op_id in self._applied_op_ids:
                    continue
                if op.type == "insert" and op.parent_id in self._nodes:
                    self._apply_insert(op)
                    self._applied_op_ids.add(op.op_id)
                    progress = True
                elif op.type == "delete" and op.char_id in self._nodes:
                    self._apply_delete(op)
                    self._applied_op_ids.add(op.op_id)
                    progress = True
                else:
                    remaining.append(op)
            self._pending_ops = remaining

    # -------------------------------------------------------------------------
    # Queries & Inspection
    # -------------------------------------------------------------------------

    def text(self) -> str:
        """Return the current visible document text."""
        chars: list[str] = []
        curr = self._head.next
        while curr is not None:
            if not curr.deleted:
                chars.append(curr.char)
            curr = curr.next
        return "".join(chars)

    def visible_len(self) -> int:
        """Return the count of visible (non-deleted) characters."""
        count = 0
        curr = self._head.next
        while curr is not None:
            if not curr.deleted:
                count += 1
            curr = curr.next
        return count

    def char_id_at(self, pos: int) -> CharId:
        """
        Return the CharId of the visible character at 0-indexed `pos`.

        Args:
            pos: Visible index (0 <= pos < visible_len()).
        """
        node = self._find_visible_node_at_index(pos)
        if node is self._head:
            raise IndexError("Index out of range for char_id_at")
        return node.char_id

    def pos_of_char_id(self, char_id: CharId) -> int | None:
        """
        Return the 0-indexed visible position of a CharId, or None if deleted/not found.
        """
        if char_id not in self._nodes:
            return None
        target_node = self._nodes[char_id]
        if target_node.deleted or target_node is self._head:
            return None

        pos = 0
        curr = self._head.next
        while curr is not None:
            if curr is target_node:
                return pos
            if not curr.deleted:
                pos += 1
            curr = curr.next
        return None

    def _find_visible_node_at_index(self, index: int) -> Node:
        """Find the visible node at a 0-indexed position (-1 returns head sentinel)."""
        if index < -1:
            raise IndexError(f"Negative index {index} out of bounds.")
        if index == -1:
            return self._head

        current_idx = 0
        curr = self._head.next
        while curr is not None:
            if not curr.deleted:
                if current_idx == index:
                    return curr
                current_idx += 1
            curr = curr.next

        raise IndexError(f"Position {index} out of range (visible length: {current_idx})")

    # -------------------------------------------------------------------------
    # Snapshot Serialization & Rehydration
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize full document state into a JSON-compatible dictionary."""
        nodes_data: list[dict[str, Any]] = []
        curr = self._head.next
        while curr is not None:
            nodes_data.append(
                {
                    "char_id": str(curr.char_id),
                    "char": curr.char,
                    "deleted": curr.deleted,
                    "parent_id": str(curr.parent_id) if curr.parent_id is not None else None,
                }
            )
            curr = curr.next

        return {
            "site_id": self.site_id,
            "clock": self.clock.value,
            "applied_op_ids": list(self._applied_op_ids),
            "nodes": nodes_data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], site_id: str | None = None) -> "RGA":
        """Reconstruct an RGA document from serialized dictionary state."""
        rga = cls(
            site_id=site_id or data["site_id"],
            clock=LamportClock(initial_value=int(data["clock"])),
        )
        rga._applied_op_ids = set(data.get("applied_op_ids", []))

        curr = rga._head
        for node_dict in data.get("nodes", []):
            char_id = CharId.from_str(node_dict["char_id"])
            parent_id = (
                CharId.from_str(node_dict["parent_id"])
                if node_dict.get("parent_id") is not None
                else None
            )
            new_node = Node(
                char_id=char_id,
                char=node_dict["char"],
                deleted=bool(node_dict["deleted"]),
                parent_id=parent_id,
                prev=curr,
                next=None,
            )
            curr.next = new_node
            rga._nodes[char_id] = new_node
            curr = new_node

        return rga

    def __repr__(self) -> str:
        return f"RGA(site_id={self.site_id!r}, text={self.text()!r}, clock={self.clock.value})"
