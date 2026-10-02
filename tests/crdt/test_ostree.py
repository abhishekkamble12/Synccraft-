"""
The order-statistic treap must agree with a plain Python list under any mix of
inserts, tombstones and rebuilds.
"""

import math
import random

from crdt.ostree import OSNode, OSTree


class Item(OSNode):
    __slots__ = ("label",)

    def __init__(self, label: int) -> None:
        super().__init__()
        self.label = label


def in_order(tree: OSTree) -> list[Item]:
    out: list[Item] = []
    stack: list[OSNode] = []
    node = tree.root
    while stack or node is not None:
        while node is not None:
            stack.append(node)
            node = node.left
        node = stack.pop()
        assert isinstance(node, Item)
        out.append(node)
        node = node.right
    return out


def check_invariants(tree: OSTree) -> None:
    stack = [tree.root] if tree.root is not None else []
    while stack:
        node = stack.pop()
        assert node is not None
        for child in (node.left, node.right):
            if child is not None:
                assert child.up is node
                assert child.prio <= node.prio
                stack.append(child)
        size = 1 + (node.left.size if node.left else 0) + (node.right.size if node.right else 0)
        vis = (
            (0 if node.deleted else 1)
            + (node.left.vis if node.left else 0)
            + (node.right.vis if node.right else 0)
        )
        assert node.size == size and node.vis == vis


def test_random_operations_match_a_list() -> None:
    rng = random.Random(7)
    tree = OSTree(seed=1)
    ref: list[Item] = []
    for label in range(4000):
        roll = rng.random()
        if ref and roll < 0.25:
            victim = rng.choice(ref)
            tree.set_deleted(victim, not victim.deleted)
        elif roll < 0.27 and ref:
            tree.build(ref)
        else:
            item = Item(label)
            idx = rng.randint(0, len(ref))
            tree.insert_after(ref[idx - 1] if idx > 0 else None, item)
            ref.insert(idx, item)

    check_invariants(tree)
    assert in_order(tree) == ref
    visible = [item for item in ref if not item.deleted]
    assert tree.visible_count == len(visible)
    for k, item in enumerate(visible):
        assert tree.kth_visible(k) is item
        assert tree.visible_rank(item) == k


def test_depth_stays_logarithmic_for_sequential_typing() -> None:
    """Appending at the end is the editor's common case and the worst one for naive BSTs."""
    tree = OSTree(seed=3)
    last: Item | None = None
    for label in range(50_000):
        item = Item(label)
        tree.insert_after(last, item)
        last = item
    assert tree.depth() < 4 * math.log2(50_000)


def test_build_is_balanced_and_valid() -> None:
    items = [Item(i) for i in range(1000)]
    tree = OSTree(seed=5)
    tree.build(items)
    check_invariants(tree)
    assert in_order(tree) == items
    assert tree.depth() == math.ceil(math.log2(1001))
