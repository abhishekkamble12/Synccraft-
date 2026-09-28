"""
Unit tests for RGA (Replicated Growable Array) CRDT.
"""

import pytest
from crdt.clock import LamportClock
from crdt.ids import CharId, ROOT
from crdt.ops import Op
from crdt.rga import RGA


def test_rga_initial_empty() -> None:
    doc = RGA(site_id="s1")
    assert doc.text() == ""
    assert doc.visible_len() == 0


def test_rga_local_sequential_inserts() -> None:
    doc = RGA(site_id="s1")
    doc.local_insert(0, "H")
    doc.local_insert(1, "i")
    doc.local_insert(2, "!")
    assert doc.text() == "Hi!"
    assert doc.visible_len() == 3


def test_rga_local_insert_at_start_and_middle() -> None:
    doc = RGA(site_id="s1")
    doc.local_insert(0, "B")
    doc.local_insert(0, "A")  # text: AB
    doc.local_insert(2, "D")  # text: ABD
    doc.local_insert(2, "C")  # text: ABCD
    assert doc.text() == "ABCD"


def test_rga_local_delete() -> None:
    doc = RGA(site_id="s1")
    doc.local_insert(0, "A")
    doc.local_insert(1, "B")
    doc.local_insert(2, "C")
    assert doc.text() == "ABC"

    op = doc.local_delete(1)  # delete 'B'
    assert doc.text() == "AC"
    assert doc.visible_len() == 2
    assert op.type == "delete"


def test_rga_two_replicas_exchange_ops() -> None:
    r1 = RGA(site_id="site1")
    r2 = RGA(site_id="site2")

    # Site 1 types "CAT"
    op_c = r1.local_insert(0, "C")
    op_a = r1.local_insert(1, "A")
    op_t = r1.local_insert(2, "T")

    # Deliver ops to Site 2
    r2.apply(op_c)
    r2.apply(op_a)
    r2.apply(op_t)

    assert r2.text() == "CAT"
    assert r1.text() == r2.text()


def test_rga_concurrent_insert_at_same_position() -> None:
    """
    Two replicas start with 'AB'.
    Replica 1 inserts 'X' between A and B.
    Replica 2 concurrently inserts 'Y' between A and B.
    Both replicas must converge to the exact same text.
    """
    r1 = RGA(site_id="site1")
    r2 = RGA(site_id="site2")

    op_a = r1.local_insert(0, "A")
    op_b = r1.local_insert(1, "B")
    r2.apply(op_a)
    r2.apply(op_b)

    # Concurrently insert between A and B (pos 1)
    op_x = r1.local_insert(1, "X")
    op_y = r2.local_insert(1, "Y")

    # Cross deliver
    r1.apply(op_y)
    r2.apply(op_x)

    assert r1.text() == r2.text()
    assert len(r1.text()) == 4
    assert r1.text() in ("AXYB", "AYXB")


def test_rga_duplicate_op_idempotency() -> None:
    doc = RGA(site_id="s1")
    op = doc.local_insert(0, "A")

    # Applying the same op again returns False and does not alter text
    assert doc.apply(op) is False
    assert doc.text() == "A"
    assert doc.visible_len() == 1


def test_rga_delete_of_already_deleted_is_noop() -> None:
    r1 = RGA(site_id="s1")
    r2 = RGA(site_id="s2")

    op_a = r1.local_insert(0, "A")
    r2.apply(op_a)

    op_del_1 = r1.local_delete(0)
    op_del_2 = r2.local_delete(0)

    # Cross deliver deletes
    r1.apply(op_del_2)
    r2.apply(op_del_1)

    assert r1.text() == ""
    assert r2.text() == ""


def test_rga_out_of_order_child_before_parent_buffered() -> None:
    """
    Child operation arrives at replica before the parent operation.
    The child must be buffered and applied automatically once parent arrives.
    """
    r1 = RGA(site_id="s1")
    r2 = RGA(site_id="s2")

    op1 = r1.local_insert(0, "A")
    op2 = r1.local_insert(1, "B")
    op3 = r1.local_insert(2, "C")

    # Deliver in reverse order: C, B, then A
    r2.apply(op3)
    assert r2.text() == ""  # buffered

    r2.apply(op2)
    assert r2.text() == ""  # buffered

    r2.apply(op1)  # parent ROOT arrives -> triggers drain of B and C
    assert r2.text() == "ABC"


def test_rga_char_id_at_and_pos_lookup() -> None:
    doc = RGA(site_id="s1")
    doc.local_insert(0, "A")
    doc.local_insert(1, "B")
    doc.local_insert(2, "C")

    cid_b = doc.char_id_at(1)
    assert doc.pos_of_char_id(cid_b) == 1

    # Delete A -> index of B becomes 0
    doc.local_delete(0)
    assert doc.text() == "BC"
    assert doc.pos_of_char_id(cid_b) == 0


def test_rga_snapshot_serialization_and_restore() -> None:
    original = RGA(site_id="s1")
    original.local_insert(0, "H")
    original.local_insert(1, "e")
    original.local_insert(2, "l")
    original.local_insert(3, "l")
    original.local_insert(4, "o")
    original.local_delete(1)  # text is "Hllo"

    state = original.to_dict()
    restored = RGA.from_dict(state, site_id="s2")

    assert restored.text() == original.text()
    assert restored.visible_len() == original.visible_len()
    assert restored.clock.value == original.clock.value
