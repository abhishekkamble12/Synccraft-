"""
Replicated Growable Array (RGA) Conflict-free Replicated Data Type.

A pure Python, zero-dependency sequence CRDT for collaborative text editing.
Replicas that receive the same set of operations converge to the same text and
the same internal order, whatever the delivery order.

Data structures
---------------
* A doubly linked list holds every character (visible or tombstoned) in document
  order. RGA integration walks it from the parent forward.
* An order-statistic treap (`crdt.ostree`) indexes the same sequence, so visible
  position <-> node translation is O(log n) instead of a linear scan.
* `_nodes` maps `CharId -> node` for O(1) lookup of parents and delete targets.
* `_vv` is a version vector: the highest character clock integrated per site.
  It replaces the old set of every applied op id (O(sites) instead of O(ops)).
  Ops are naturally idempotent (an insert run is keyed by its first CharId, a
  delete only sets tombstones), and the version vector is what lets a server
  reject a forged or replayed id even after its tombstone was garbage-collected.
"""

from collections.abc import Iterable, Iterator
from typing import Any

from crdt.clock import LamportClock
from crdt.ids import ROOT, CharId
from crdt.ops import Op, spans_from_ids
from crdt.ostree import OSNode, OSTree

# Integrity limits applied by `validate` (server side). Clients enforce nothing.
MAX_OP_CHARS = 65_536
MAX_SITE_ID_LEN = 64
MAX_OP_ID_LEN = 128
DEFAULT_MAX_PENDING = 10_000


class PendingOverflowError(RuntimeError):
    """Raised when too many causally-unready ops are buffered; the replica must resync."""


class Node(OSNode):
    """One character in document order (plus treap bookkeeping inherited from OSNode)."""

    __slots__ = ("char_id", "char", "next", "prev")

    def __init__(self, char_id: CharId, char: str, deleted: bool = False) -> None:
        super().__init__(deleted)
        self.char_id = char_id
        self.char = char
        self.next: Node | None = None
        self.prev: Node | None = None


class RGA:
    """
    RGA sequence CRDT document replica.

    Local edits:   local_insert(pos, text), local_insert_after(parent_id, text),
                   local_delete(pos, length)
    Remote edits:  apply(op)  (idempotent, buffers ops whose dependencies are missing)
    Server checks: validate(op)
    Queries:       text(), visible_len(), char_id_at(pos), pos_of_char_id(cid)
    Maintenance:   compact(keep) drops tombstones; to_dict()/from_dict() snapshot.
    """

    def __init__(
        self,
        site_id: str,
        clock: LamportClock | None = None,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self.site_id: str = site_id
        self.clock: LamportClock = clock if clock is not None else LamportClock()
        self.max_pending = max_pending

        # Sentinel head representing ROOT. It is in the linked list but not in the tree.
        self._head: Node = Node(ROOT, "")
        self._tree = OSTree()
        self._nodes: dict[CharId, Node] = {ROOT: self._head}
        self._vv: dict[str, int] = {}
        # Ops whose parent / delete targets have not arrived yet, keyed by op_id.
        self._pending: dict[str, Op] = {}

    # -------------------------------------------------------------------------
    # Local operations
    # -------------------------------------------------------------------------

    def local_insert(self, pos: int, text: str, op_id: str | None = None) -> Op:
        """Insert `text` so that its first character lands at visible position `pos`."""
        if not text:
            raise ValueError("local_insert needs at least one character")
        if pos < 0 or pos > self.visible_len():
            raise IndexError(f"Insert position {pos} out of range (0..{self.visible_len()})")
        parent = self._head if pos == 0 else self._visible_node(pos - 1)
        return self.local_insert_after(parent.char_id, text, op_id=op_id)

    def local_insert_after(self, parent_id: CharId, text: str, op_id: str | None = None) -> Op:
        """Insert `text` directly after the character `parent_id` (ROOT for the start)."""
        if not text:
            raise ValueError("local_insert_after needs at least one character")
        if parent_id not in self._nodes:
            raise KeyError(f"Unknown parent {parent_id}")
        first = self.clock.reserve(len(text))
        op = Op.create_insert(
            site_id=self.site_id,
            char_id=CharId(first, self.site_id),
            parent_id=parent_id,
            text=text,
            op_id=op_id,
        )
        self._integrate(op)
        return op

    def local_delete(self, pos: int, length: int = 1, op_id: str | None = None) -> Op:
        """Delete `length` visible characters starting at visible position `pos`."""
        if length < 1 or pos < 0 or pos + length > self.visible_len():
            raise IndexError(
                f"No visible range [{pos}, {pos + length}) (visible length: {self.visible_len()})"
            )
        node: Node | None = self._visible_node(pos)
        ids: list[CharId] = []
        while len(ids) < length:
            assert node is not None
            if not node.deleted:
                ids.append(node.char_id)
            node = node.next

        op = Op.create_delete(
            site_id=self.site_id,
            lamport=self.clock.tick(),
            spans=spans_from_ids(ids),
            op_id=op_id,
        )
        self._apply_delete(op)
        return op

    def local_delete_ids(self, ids: Iterable[CharId], op_id: str | None = None) -> Op | None:
        """Delete the given characters (skipping unknown ones). None if nothing remains."""
        present = [cid for cid in ids if cid in self._nodes and cid != ROOT]
        if not present:
            return None
        op = Op.create_delete(
            site_id=self.site_id,
            lamport=self.clock.tick(),
            spans=spans_from_ids(present),
            op_id=op_id,
        )
        self._apply_delete(op)
        return op

    # -------------------------------------------------------------------------
    # Remote / generic operation application
    # -------------------------------------------------------------------------

    def apply(self, op: Op) -> bool:
        """
        Apply an operation (local or remote). Idempotent and order-independent.

        Returns True if the document changed, False for duplicates, no-op deletes
        and ops buffered until their dependencies arrive.
        """
        self.clock.update(op.lamport)

        if op.type == "insert":
            assert op.char_id is not None and op.parent_id is not None
            if op.char_id in self._nodes:
                return False  # runs are atomic: the first id present means all are
            if op.parent_id not in self._nodes:
                self._buffer(op)
                return False
            self._integrate(op)
            self._drain_pending()
            return True

        if not all(cid in self._nodes for cid in op.target_ids()):
            self._buffer(op)
            return False
        return self._apply_delete(op)

    def validate(self, op: Op) -> str | None:
        """
        Integrity checks a server runs before accepting an op from an untrusted
        client. Returns a rejection reason, or None if the op is acceptable.

        The server applies ops in sequence order, so every dependency of an honest
        op is already present. Rejecting orphans therefore never rejects an honest
        client, and it means the server never has to buffer anything.
        """
        if not op.site_id or len(op.site_id) > MAX_SITE_ID_LEN:
            return "bad_site_id"
        if not op.op_id or len(op.op_id) > MAX_OP_ID_LEN:
            return "bad_op_id"
        if len(op) > MAX_OP_CHARS:
            return "op_too_large"

        if op.type == "insert":
            assert op.char_id is not None and op.parent_id is not None and op.text is not None
            first = op.char_id
            # The unit is the Unicode code point. A lone surrogate means the client
            # split a UTF-16 pair; it would also be rejected by Postgres JSONB.
            if any(0xD800 <= ord(char) <= 0xDFFF for char in op.text):
                return "bad_text"
            if first.site_id != op.site_id:
                return "char_id_site_mismatch"
            if first.clock < 1 or op.lamport != first.clock + len(op.text) - 1:
                return "lamport_mismatch"
            parent = self._nodes.get(op.parent_id)
            if parent is None:
                return "unknown_parent"
            # RGA's skip-greater-ids rule is only correct if a character's id is
            # larger than its parent's, which a Lamport clock guarantees for honest
            # clients. A forged smaller id could be ordered differently on replicas.
            if first.clock <= parent.char_id.clock:
                return "clock_not_after_parent"
            # Per-site ids must strictly increase. This also rejects reuse of an id
            # that existed once, even if compaction has since removed it.
            if first.clock <= self._vv.get(first.site_id, 0):
                return "clock_not_after_site_history"
            return None

        if op.lamport < 1:
            return "lamport_mismatch"
        if not all(cid in self._nodes for cid in op.target_ids()):
            return "unknown_target"
        if any(first == ROOT for first, _ in op.spans):
            return "unknown_target"
        return None

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _visible_node(self, pos: int) -> Node:
        node = self._tree.kth_visible(pos)
        assert isinstance(node, Node)
        return node

    def _integrate(self, op: Op) -> None:
        """Splice an insert run into the sequence according to RGA's ordering rule."""
        assert op.char_id is not None and op.parent_id is not None and op.text is not None
        first = op.char_id

        # RGA rule: from the parent, skip every following node with a greater id.
        # Those are concurrent siblings that win the tie, plus their descendants.
        anchor = self._nodes[op.parent_id]
        following = anchor.next
        while following is not None and following.char_id > first:
            anchor = following
            following = anchor.next

        # The rest of the run follows its predecessor directly: nothing after the
        # anchor can carry an id greater than the run's own consecutive ids.
        site, clock = first.site_id, first.clock
        for offset, char in enumerate(op.text):
            node = Node(CharId(clock + offset, site), char)
            node.prev = anchor
            node.next = anchor.next
            if anchor.next is not None:
                anchor.next.prev = node
            anchor.next = node
            self._tree.insert_after(None if anchor is self._head else anchor, node)
            self._nodes[node.char_id] = node
            anchor = node

        last_clock = clock + len(op.text) - 1
        if last_clock > self._vv.get(site, 0):
            self._vv[site] = last_clock

    def _apply_delete(self, op: Op) -> bool:
        changed = False
        for cid in op.target_ids():
            node = self._nodes.get(cid)
            if node is not None and node is not self._head and not node.deleted:
                self._tree.set_deleted(node)
                changed = True
        return changed

    def _buffer(self, op: Op) -> None:
        if op.op_id in self._pending:
            return
        if len(self._pending) >= self.max_pending:
            raise PendingOverflowError(
                f"{len(self._pending)} ops are waiting on missing dependencies; resync needed"
            )
        self._pending[op.op_id] = op

    def _drain_pending(self) -> None:
        """Apply buffered ops whose dependencies are now present, until a fixpoint."""
        progress = True
        while progress and self._pending:
            progress = False
            for op_id, op in list(self._pending.items()):
                if op.type == "insert":
                    assert op.char_id is not None and op.parent_id is not None
                    if op.char_id in self._nodes:
                        del self._pending[op_id]
                    elif op.parent_id in self._nodes:
                        del self._pending[op_id]
                        self._integrate(op)
                        progress = True
                elif all(cid in self._nodes for cid in op.target_ids()):
                    del self._pending[op_id]
                    self._apply_delete(op)

    # -------------------------------------------------------------------------
    # Queries & inspection
    # -------------------------------------------------------------------------

    def iter_nodes(self) -> Iterator[Node]:
        """Every node in document order, tombstones included (head excluded)."""
        node = self._head.next
        while node is not None:
            yield node
            node = node.next

    def text(self) -> str:
        """Return the current visible document text."""
        return "".join(node.char for node in self.iter_nodes() if not node.deleted)

    def visible_len(self) -> int:
        """Count of visible (non-deleted) characters. O(1)."""
        return self._tree.visible_count

    def total_len(self) -> int:
        """Count of all nodes, tombstones included. O(1)."""
        return len(self._tree)

    def has(self, char_id: CharId) -> bool:
        return char_id in self._nodes

    def char_id_at(self, pos: int) -> CharId:
        """Return the CharId of the visible character at 0-indexed `pos`. O(log n)."""
        return self._visible_node(pos).char_id

    def pos_of_char_id(self, char_id: CharId) -> int | None:
        """Visible position of a CharId, or None if it is deleted or unknown. O(log n)."""
        node = self._nodes.get(char_id)
        if node is None or node is self._head or node.deleted:
            return None
        return self._tree.visible_rank(node)

    @property
    def version_vector(self) -> dict[str, int]:
        return dict(self._vv)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # -------------------------------------------------------------------------
    # Tombstone garbage collection
    # -------------------------------------------------------------------------

    def compact(self, keep: Iterable[CharId] = ()) -> int:
        """
        Drop every tombstone except those in `keep`, returning how many were removed.

        Only safe when no op that will ever be applied to any replica can still
        name a dropped tombstone as its parent or delete target. The server decides
        that (see documents/gc.py); this method just does the removal.
        """
        keep_set = set(keep)
        survivors = [
            node for node in self.iter_nodes() if not node.deleted or node.char_id in keep_set
        ]
        removed = self.total_len() - len(survivors)
        if removed:
            self._relink(survivors)
        return removed

    def _relink(self, nodes: list[Node]) -> None:
        previous = self._head
        self._nodes = {ROOT: self._head}
        for node in nodes:
            node.prev = previous
            previous.next = node
            self._nodes[node.char_id] = node
            previous = node
        previous.next = None
        self._tree.build(nodes)

    # -------------------------------------------------------------------------
    # Snapshot serialization & rehydration
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the full state compactly. Nodes are grouped into runs of
        consecutive ids from one site with the same tombstone flag, so a typed
        paragraph is one `[first_id, text, deleted]` triple, not one dict per char.
        """
        runs: list[list[Any]] = []
        run_site = ""
        run_next_clock = -1
        run_deleted = False
        chars: list[str] = []

        for node in self.iter_nodes():
            cid = node.char_id
            if chars and cid.site_id == run_site and cid.clock == run_next_clock and (
                node.deleted == run_deleted
            ):
                chars.append(node.char)
                run_next_clock += 1
                continue
            if chars:
                runs[-1][1] = "".join(chars)
            runs.append([str(cid), "", 1 if node.deleted else 0])
            chars = [node.char]
            run_site, run_next_clock, run_deleted = cid.site_id, cid.clock + 1, node.deleted
        if chars:
            runs[-1][1] = "".join(chars)

        return {
            "v": 2,
            "site_id": self.site_id,
            "clock": self.clock.value,
            "vv": dict(self._vv),
            "runs": runs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], site_id: str | None = None) -> "RGA":
        """Rebuild a replica from `to_dict()` output (or the legacy per-node format)."""
        rga = cls(
            site_id=site_id or data.get("site_id", ""),
            clock=LamportClock(initial_value=int(data.get("clock", 0))),
        )
        nodes: list[Node] = []
        if "runs" in data:
            for first_raw, text, deleted in data["runs"]:
                first = CharId.from_str(first_raw)
                for offset, char in enumerate(text):
                    nodes.append(Node(CharId(first.clock + offset, first.site_id), char, bool(deleted)))
        else:
            for item in data.get("nodes", []):
                nodes.append(
                    Node(CharId.from_str(item["char_id"]), item["char"], bool(item["deleted"]))
                )
        rga._relink(nodes)

        vv: dict[str, int] = {str(k): int(v) for k, v in (data.get("vv") or {}).items()}
        for node in nodes:
            cid = node.char_id
            if cid.clock > vv.get(cid.site_id, 0):
                vv[cid.site_id] = cid.clock
        rga._vv = vv
        return rga

    def __repr__(self) -> str:
        return f"RGA(site_id={self.site_id!r}, text={self.text()!r}, clock={self.clock.value})"
