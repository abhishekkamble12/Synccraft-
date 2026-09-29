"""
Property-based testing for RGA CRDT algebraic invariants using Hypothesis.

Invariants verified:
1. Idempotency: apply(op) == apply(apply(op))
2. Commutativity: apply(A, B) == apply(B, A)
3. Strong Eventual Consistency across randomized operation traces
"""

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from crdt.ops import Op
from crdt.rga import RGA

# Strategy to generate valid character payloads
char_strategy = st.characters(
    whitelist_categories=("Lu", "Ll", "Nd", "P"),
    min_codepoint=32,
    max_codepoint=126,
)


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["s1", "s2", "s3"]),
            st.sampled_from(["insert", "delete"]),
            st.integers(min_value=0, max_value=50),
            char_strategy,
        ),
        min_size=1,
        max_size=30,
    )
)
def test_property_commutativity_and_convergence(actions: list[tuple[str, str, int, str]]) -> None:
    """
    Property: Any valid trace of operations executed across multiple sites must
    converge to identical text when delivered to different replicas in different orders.
    """
    sites = {
        "s1": RGA(site_id="s1"),
        "s2": RGA(site_id="s2"),
        "s3": RGA(site_id="s3"),
    }

    generated_ops: list[Op] = []

    for site_id, action_type, raw_pos, char in actions:
        site = sites[site_id]
        v_len = site.visible_len()

        if action_type == "delete" and v_len > 0:
            pos = raw_pos % v_len
            op = site.local_delete(pos)
            generated_ops.append(op)
        else:
            pos = raw_pos % (v_len + 1)
            op = site.local_insert(pos, char)
            generated_ops.append(op)

    # Deliver all operations in original generation order to Replica A
    replica_a = RGA(site_id="replica_a")
    for op in generated_ops:
        replica_a.apply(op)

    # Deliver in reversed order to Replica B
    replica_b = RGA(site_id="replica_b")
    for op in reversed(generated_ops):
        replica_b.apply(op)

    # Deliver in randomly shuffled order to Replica C
    shuffled_ops = list(generated_ops)
    random.Random(999).shuffle(shuffled_ops)
    replica_c = RGA(site_id="replica_c")
    for op in shuffled_ops:
        replica_c.apply(op)

    # Invariant: All replicas MUST produce 100% identical visible text
    assert replica_a.text() == replica_b.text()
    assert replica_b.text() == replica_c.text()
    assert replica_a.visible_len() == replica_b.visible_len()


@settings(max_examples=50, deadline=None)
@given(st.lists(char_strategy, min_size=1, max_size=15))
def test_property_idempotency_duplicate_ops(chars: list[str]) -> None:
    """
    Property: Applying any operation more than once has zero side-effects.
    """
    doc = RGA(site_id="producer")
    ops: list[Op] = []
    for idx, c in enumerate(chars):
        ops.append(doc.local_insert(idx, c))

    consumer = RGA(site_id="consumer")

    # First pass: apply all ops
    for op in ops:
        assert consumer.apply(op) is True

    canonical_text = consumer.text()
    canonical_len = consumer.visible_len()

    # Second pass: apply all ops again (duplicates)
    for op in ops:
        assert consumer.apply(op) is False

    # Invariant: Document remains completely unchanged
    assert consumer.text() == canonical_text
    assert consumer.visible_len() == canonical_len
