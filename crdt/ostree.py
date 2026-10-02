"""
Order-statistic treap over the RGA's document order.

The RGA keeps every character, visible or tombstoned, in one total order. Editors
talk in visible positions ("insert at index 4,812"), so the replica constantly has
to translate between a position and a node. Walking the linked list makes that
O(n) per keystroke, and a paste of k characters into an n-character document
costs O(k * n).

This treap stores the same nodes in the same in-order sequence. Every node caches
the size of its subtree and the number of visible (non-tombstoned) nodes in it, so:

* `kth_visible(k)`      visible position -> node        O(log n)
* `visible_rank(node)`  node -> visible position        O(log n)
* `insert_after(a, x)`  splice x right after node a     O(log n)
* `set_deleted(node)`   tombstone and fix the counts     O(log n)

All bounds are expected, from the random priorities. Nodes are located by
identity (the RGA's id -> node map), never by key, so the tree needs no ordering
comparisons at all: its shape only has to agree with the in-order sequence.
"""

import random
from collections import deque
from collections.abc import Sequence


class OSNode:
    """Treap bookkeeping embedded in every RGA node."""

    __slots__ = ("deleted", "left", "right", "up", "prio", "size", "vis")

    def __init__(self, deleted: bool = False) -> None:
        self.deleted: bool = deleted
        self.left: OSNode | None = None
        self.right: OSNode | None = None
        self.up: OSNode | None = None
        self.prio: float = 0.0
        self.size: int = 1
        self.vis: int = 0 if deleted else 1


def _size(node: OSNode | None) -> int:
    return node.size if node is not None else 0


def _vis(node: OSNode | None) -> int:
    return node.vis if node is not None else 0


def _pull(node: OSNode) -> None:
    node.size = 1 + _size(node.left) + _size(node.right)
    node.vis = (0 if node.deleted else 1) + _vis(node.left) + _vis(node.right)


class OSTree:
    """Implicit treap: in-order position is the key, subtree counts are the index."""

    __slots__ = ("root", "_rng")

    def __init__(self, seed: int | None = None) -> None:
        self.root: OSNode | None = None
        self._rng = random.Random(seed)

    def __len__(self) -> int:
        return _size(self.root)

    @property
    def visible_count(self) -> int:
        return _vis(self.root)

    # -------------------------------------------------------------------------
    # Mutation
    # -------------------------------------------------------------------------

    def insert_after(self, anchor: OSNode | None, node: OSNode) -> None:
        """Place `node` immediately after `anchor` in order (`None` means at the front)."""
        node.left = node.right = node.up = None
        node.prio = self._rng.random()
        node.size = 1
        node.vis = 0 if node.deleted else 1

        if self.root is None:
            self.root = node
            return

        # The in-order successor slot of `anchor` is its right child if free,
        # otherwise the left child of the leftmost node of its right subtree.
        if anchor is None:
            parent = self.root
            while parent.left is not None:
                parent = parent.left
            parent.left = node
        elif anchor.right is None:
            parent = anchor
            parent.right = node
        else:
            parent = anchor.right
            while parent.left is not None:
                parent = parent.left
            parent.left = node
        node.up = parent

        weight = node.vis
        walk: OSNode | None = parent
        while walk is not None:
            walk.size += 1
            walk.vis += weight
            walk = walk.up

        while node.up is not None and node.up.prio < node.prio:
            self._rotate_up(node)

    def set_deleted(self, node: OSNode, deleted: bool = True) -> None:
        """Flip a node's tombstone flag and fix the visible counts on its root path."""
        if node.deleted == deleted:
            return
        node.deleted = deleted
        delta = -1 if deleted else 1
        walk: OSNode | None = node
        while walk is not None:
            walk.vis += delta
            walk = walk.up

    def build(self, nodes: Sequence[OSNode]) -> None:
        """Replace the tree with a balanced one holding `nodes` in the given order. O(n log n)."""
        self.root = None
        count = len(nodes)
        if count == 0:
            return

        # Parents must out-rank their children. Handing out a sorted sample of
        # random priorities in breadth-first order guarantees that, and keeps the
        # priorities distributed exactly like the ones later inserts draw.
        prios = sorted((self._rng.random() for _ in range(count)), reverse=True)
        next_prio = iter(prios)

        root_idx = (count - 1) // 2
        self.root = nodes[root_idx]
        queue: deque[tuple[int, int, int]] = deque([(0, count - 1, root_idx)])
        while queue:
            lo, hi, mid = queue.popleft()
            node = nodes[mid]
            node.prio = next(next_prio)
            node.left = node.right = None
            if lo <= mid - 1:
                child_idx = (lo + mid - 1) // 2
                child = nodes[child_idx]
                node.left = child
                child.up = node
                queue.append((lo, mid - 1, child_idx))
            if mid + 1 <= hi:
                child_idx = (mid + 1 + hi) // 2
                child = nodes[child_idx]
                node.right = child
                child.up = node
                queue.append((mid + 1, hi, child_idx))
        self.root.up = None

        # Fix subtree counts bottom-up (reverse breadth-first order visits children first).
        order: list[OSNode] = []
        frontier: deque[OSNode] = deque([self.root])
        while frontier:
            current = frontier.popleft()
            order.append(current)
            if current.left is not None:
                frontier.append(current.left)
            if current.right is not None:
                frontier.append(current.right)
        for current in reversed(order):
            _pull(current)

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    def kth_visible(self, k: int) -> OSNode:
        """Return the visible node at 0-indexed visible position `k`."""
        if k < 0 or k >= self.visible_count:
            raise IndexError(f"Position {k} out of range (visible length: {self.visible_count})")
        node = self.root
        while node is not None:
            left_vis = _vis(node.left)
            if k < left_vis:
                node = node.left
                continue
            k -= left_vis
            if not node.deleted:
                if k == 0:
                    return node
                k -= 1
            node = node.right
        raise AssertionError("subtree visible counts are inconsistent")

    def visible_rank(self, node: OSNode) -> int:
        """Number of visible nodes strictly before `node` in order."""
        rank = _vis(node.left)
        current = node
        while current.up is not None:
            parent = current.up
            if parent.right is current:
                rank += _vis(parent.left) + (0 if parent.deleted else 1)
            current = parent
        return rank

    def depth(self) -> int:
        """Height of the tree (diagnostics and tests)."""
        best = 0
        stack: list[tuple[OSNode, int]] = [(self.root, 1)] if self.root is not None else []
        while stack:
            node, d = stack.pop()
            best = max(best, d)
            if node.left is not None:
                stack.append((node.left, d + 1))
            if node.right is not None:
                stack.append((node.right, d + 1))
        return best

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _rotate_up(self, node: OSNode) -> None:
        parent = node.up
        assert parent is not None
        grand = parent.up
        if parent.left is node:
            moved = node.right
            parent.left = moved
            node.right = parent
        else:
            moved = node.left
            parent.right = moved
            node.left = parent
        if moved is not None:
            moved.up = parent
        parent.up = node
        node.up = grand
        if grand is None:
            self.root = node
        elif grand.left is parent:
            grand.left = node
        else:
            grand.right = node
        _pull(parent)
        _pull(node)
